import unittest

import skills
from skills import Result

# (utterance, music playing, expected (skill, arguments) or None for the model)
LOCAL = [
    ("音量调到五十", False, ("set_volume", {"level": 50})),
    ("声音大一点", False, ("set_volume", {"change": 10})),
    ("太吵了", False, ("set_volume", {"change": -10})),
    ("静音", False, ("set_volume", {"mute": "mute"})),
    ("暂停", True, ("control_music", {"action": "pause"})),
    ("请帮我暂停。", True, ("control_music", {"action": "pause"})),
    ("暂停", False, None),
    ("下一首", True, ("control_music", {"action": "next"})),
    ("三十分钟后停止播放", True, ("control_music", {"action": "sleep", "minutes": 30})),
    ("单曲循环", False, ("control_music", {"action": "repeat_one"})),
    ("播放周杰伦的晴天", False, ("play_music", {"query": "周杰伦的晴天"})),
    ("我想听一首轻音乐吧", False, ("play_music", {"query": "轻音乐"})),
    ("现在几点了", False, ("get_current_time", {"clock_only": True})),
    ("今天星期几", False, ("get_current_time", {"clock_only": False})),
    ("关闭闹钟", False, ("manage_alarm", {"action": "dismiss"})),
    ("再睡十分钟", False, ("manage_alarm", {"action": "snooze", "minutes": 10})),
    ("定个五分钟的倒计时", False, ("manage_alarm", {"action": "create", "kind": "timer", "minutes": 5, "seconds": 0})),
    ("帮我设置一个半小时的计时器", False, ("manage_alarm", {"action": "create", "kind": "timer", "hours": 0, "minutes": 30})),
    ("退下吧谢谢", False, ("end_conversation", {"screen_off": False, "stop_music": False})),
    ("好了没事了", False, ("end_conversation", {"screen_off": False, "stop_music": False})),
    ("晚安", True, ("end_conversation", {"screen_off": True, "stop_music": True})),
    ("关屏", True, ("end_conversation", {"screen_off": True, "stop_music": False})),
    ("这首歌叫什么", True, ("playback_status", {"item": "song"})),
    ("今天天气怎么样", False, None),
    ("记住我女儿叫小米", False, None),
    ("播放一首" + "很长" * 20 + "的歌", False, None),
]

FORCED = [
    ("明天杭州会下雨吗", "get_weather"),
    ("外面冷不冷", "get_weather"),
    ("早上好", "daily_briefing"),
    ("今天有什么安排", "daily_briefing"),
    ("记住我不吃香菜", "manage_memory"),
    ("你都记得我什么", "manage_memory"),
    ("提醒我别忘了带钥匙", None),
    ("明天早上七点叫我，记住了", None),
    ("你好", None),
]


class LocalPhrases(unittest.TestCase):
    def test_match(self):
        for text, music, expected in LOCAL:
            with self.subTest(text=text, music=music):
                self.assertEqual(skills.match(text, music), expected)

    def test_forced(self):
        for text, expected in FORCED:
            with self.subTest(text=text):
                self.assertEqual(skills.forced(text), expected)

    def test_offline_keywords(self):
        for line in skills.offline_keywords().splitlines():
            phrase, label = line.split(" @")
            self.assertTrue(phrase and label, line)


class Definitions(unittest.TestCase):
    def test_tools(self):
        tools = skills.tools()
        names = [tool["function"]["name"] for tool in tools]
        self.assertEqual(len(names), len(set(names)))
        self.assertNotIn("playback_status", names)
        for tool in tools:
            schema = tool["function"]["parameters"]
            self.assertEqual(schema["type"], "object")
            self.assertLessEqual(set(schema.get("required", [])), set(schema["properties"]), names)


class Provider:
    def __init__(self, results):
        for skill in skills.SKILLS:
            setattr(self, skill.name, lambda args, name=skill.name: results.get(name, Result("好的", True)))


class Registry(unittest.TestCase):
    def test_one_handler_per_skill(self):
        with self.assertRaises(RuntimeError):
            skills.Skills()
        with self.assertRaises(RuntimeError):
            skills.Skills(Provider({}), Provider({}))

    def test_run(self):
        failed = Result("失败", False)
        registry = skills.Skills(Provider({"set_volume": failed, "manage_alarm": failed, "get_weather": failed}))
        # (name, args, has_reply) -> (message, effect, final)
        cases = [
            ("get_current_time", {}, False, "好的", "info", False),
            ("control_music", {"action": "pause"}, False, "好的", "action", True),
            ("set_volume", {"change": 10}, False, "失败", "failed", True),
            ("manage_alarm", {"action": "create"}, False, "失败", "failed", False),
            ("get_weather", {}, False, "失败", "failed", False),
            ("manage_memory", {"action": "list"}, False, "好的", "info", False),
            ("manage_memory", {"action": "add"}, False, "好的", "action", True),
            ("end_conversation", {}, True, "", None, True),
            ("end_conversation", {}, False, "好的", None, True),
            ("no_such_tool", {}, False, "不支持这个操作", "failed", True),
        ]
        for name, args, has_reply, message, effect, final in cases:
            with self.subTest(name=name, args=args):
                result, got_effect, got_final = registry.run(name, args, has_reply)
                self.assertEqual((result.message, got_effect, got_final), (message, effect, final))


if __name__ == "__main__":
    unittest.main()
