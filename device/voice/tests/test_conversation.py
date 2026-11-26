import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import device_stubs  # noqa: F401
import conversation as C
import device_skills
from alarms import AlarmService
from audio import LISTEN_TONE, OK_TONE
from config import Preferences, Settings
from memory import Memory
from music import MusicService
from recipe import RecipeService
from session import Session
from skills import OFFLINE_HINT, Result


class FakeSpeaker:
    def __init__(self):
        self.heard = ""
        self.said = []

    def say(self, reply, offline_mode=False):
        self.said.append((reply, offline_mode))
        self.heard = reply

    def stream(self, fragments):
        self.heard = "".join(fragments)
        return self.heard.strip()

    def local(self, reply):
        self.heard = reply


def make_conversation():
    directory = tempfile.TemporaryDirectory()
    session = Session()
    ui = Mock()
    music = Mock(spec=MusicService)
    music.active.return_value = False
    alarms = Mock(spec=AlarmService)
    alarms.ringing = False
    settings = Settings()
    prefs = Preferences(speech_rate=1.5, thinking=True)
    brain = Mock(messages=[], last_active=0.0)
    speaker = FakeSpeaker()
    device = device_skills.DeviceSkills(prefs, ui, alarms, session)
    conv = C.Conversation(session, ui, brain, speaker,
                          SimpleNamespace(chat_model="chat", chat_provider="bailian", expert_model=None,
                                          expert_provider=None),
                          prefs, settings, SimpleNamespace(phrase="小度小度"), music, alarms,
                          Memory(os.path.join(directory.name, "memory.json")), device,
                          RecipeService(None, alarms, os.path.join(directory.name, "recipe.json")), Mock())
    conv.cue = Mock()
    conv.context = lambda: "设备状态"
    conv._directory = directory
    return conv


class TurnTests(unittest.TestCase):
    def setUp(self):
        self.conv = make_conversation()
        self.addCleanup(self.conv._directory.cleanup)

    def test_quiet_local_command_answers_with_a_tone(self):
        conv = self.conv
        conv.music.active.return_value = True
        conv.music.control_music.return_value = Result("已暂停", True)
        conv.handle_turn("暂停")
        conv.music.control_music.assert_called_once_with({"action": "pause"})
        conv.cue.assert_called_once_with(OK_TONE)
        self.assertEqual(conv.speaker.said, [])
        self.assertEqual(conv.next_followup, 0)
        conv.brain.note.assert_called_once_with("暂停", "已暂停（设备已执行：已暂停）")

    def test_spoken_local_command_keeps_listening_briefly(self):
        conv = self.conv
        conv.handle_turn("现在几点了")
        self.assertEqual(len(conv.speaker.said), 1)
        self.assertFalse(conv.model_turn)
        self.assertEqual(conv.next_followup, conv.settings.followup_seconds)
        conv.brain.chat.assert_not_called()

    def test_wake_word_alone_waits_for_the_request(self):
        conv = self.conv
        conv.handle_turn("小度小度。")
        conv.cue.assert_called_once_with(LISTEN_TONE)
        self.assertEqual(conv.next_followup, conv.settings.speech_wait_seconds)

    def test_offline_turn_explains_what_works(self):
        conv = self.conv
        conv.offline_mode = True
        conv.handle_turn("讲个笑话")
        self.assertEqual(conv.speaker.said, [(OFFLINE_HINT, True)])
        self.assertEqual(conv.next_followup, 0)

    def test_model_reply_that_asks_waits_longer(self):
        conv = self.conv
        conv.brain.chat.return_value = iter([("delta", "你想听"), ("delta", "什么歌？")])
        conv.handle_turn("陪我聊聊天吧")
        self.assertTrue(conv.model_turn)
        self.assertEqual(conv.speaker.heard, "你想听什么歌？")
        self.assertEqual(conv.next_followup, conv.settings.question_followup_seconds)
        conv.brain.heard.assert_called_once_with("你想听什么歌？", interrupted=False)
        conv.brain.note.assert_not_called()

    def test_model_ending_the_conversation(self):
        conv = self.conv
        conv.brain.chat.return_value = iter([("tool", "call-1", "end_conversation", {"screen_off": True}),
                                             ("delta", "好的，再见。")])
        conv.handle_turn("我先睡了你关屏吧")
        conv.brain.tool_result.assert_called_once_with("call-1", "好的", True, True)
        self.assertTrue(conv.over)
        self.assertTrue(conv.session.screen_off_pending)
        self.assertEqual(conv.next_followup, 0)

    def test_tool_failing_twice_ends_the_turn(self):
        conv = self.conv
        conv.alarms.manage_alarm.return_value = Result("时间不对", False)
        self.assertEqual(conv.run_tool("manage_alarm", {"action": "create"}, False), ("时间不对", False, False))
        self.assertEqual(conv.run_tool("manage_alarm", {"action": "create"}, False), ("时间不对", False, True))

    def test_empty_model_reply_is_an_error(self):
        conv = self.conv
        conv.brain.chat.return_value = iter([])
        with self.assertRaisesRegex(RuntimeError, "empty reply"):
            conv.handle_turn("你好呀今天怎么样")
        conv.brain.heard.assert_called_once_with("", interrupted=True)

    def test_expert_and_thinking_routing(self):
        conv = self.conv
        conv.models.expert_model = "gpt"
        conv.models.expert_provider = "openai"
        conv.brain.chat.return_value = iter([("delta", "好。")])
        conv.handle_turn("帮我详细分析一下这个算法")
        self.assertTrue(conv.brain.chat.call_args.kwargs["expert"])
        self.assertEqual(conv.session.turn_models["role"], "expert")
        conv.prefs.thinking = False
        conv.brain.chat.return_value = iter([("delta", "好。")])
        conv.handle_turn("帮我详细分析一下这个算法")
        self.assertFalse(conv.brain.chat.call_args.kwargs["expert"])

    def test_followup_window(self):
        conv = self.conv
        conv.music_started = True
        self.assertEqual(conv.followup_window("好的"), 0)
        conv.music_started = False
        conv.effects = ["action"]
        self.assertEqual(conv.followup_window("已调大音量"), 0)
        conv.effects = ["info"]
        self.assertEqual(conv.followup_window("今天晴"), conv.settings.followup_seconds)

    def test_gap_tells_the_model_how_long_ago(self):
        conv = self.conv
        self.assertEqual(conv.gap(), "")
        conv.brain.messages = [{}]
        with patch.object(C.time, "monotonic", return_value=conv.brain.last_active + 600):
            self.assertIn("约10分钟前", conv.gap())


class DeviceSkillTests(unittest.TestCase):
    def setUp(self):
        self.session = Session()
        self.ui = Mock()
        self.prefs = Preferences(speech_rate=1.5, thinking=True, brightness=50)
        self.skills = device_skills.DeviceSkills(self.prefs, self.ui, Mock(), self.session)

    def test_adjustments(self):
        result = self.skills.control_device({"brightness_change": -20, "speech_rate_change": 0.3,
                                             "thinking_mode": "off"})
        self.assertTrue(result.ok)
        self.ui.emit.assert_called_once_with("ui_set", brightness=30, speech_rate=1.8, thinking=False)
        self.assertEqual((self.prefs.brightness, self.prefs.speech_rate, self.prefs.thinking), (30, 1.8, False))

    def test_screen(self):
        self.assertTrue(self.skills.control_device({"screen": "off"}).ok)
        self.assertTrue(self.session.screen_off_pending)
        self.ui.emit.assert_not_called()
        self.skills.control_device({"screen": "weather"})
        self.ui.emit.assert_called_once_with("ui_set", screen="weather")

    def test_city(self):
        with patch.object(device_skills.system, "search_places", return_value=[]):
            self.assertFalse(self.skills.control_device({"weather_city": "不存在"}).ok)
        with patch.object(device_skills.system, "search_places",
                          return_value=[{"name": "杭州", "lat": 30.3, "lon": 120.2, "extra": 1}]):
            result = self.skills.control_device({"weather_city": "杭州"})
        self.assertEqual(result.message, "默认天气城市已设为杭州")
        self.ui.emit.assert_called_once_with("ui_set", location={"name": "杭州", "lat": 30.3, "lon": 120.2})

    def test_nothing_understood(self):
        self.assertFalse(self.skills.control_device({"brightness": True}).ok)


if __name__ == "__main__":
    unittest.main()
