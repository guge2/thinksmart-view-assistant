import math
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import device_stubs  # noqa: F401
import speak
import speech_text as T
from session import Session, TurnCancelled
from wake import RAW_CAPTURE_TAIL, WakeWord


class TextTests(unittest.TestCase):
    def test_word_split_across_fragments(self):
        text = "你好小度 小度小度小度。还有小度"
        for split in range(len(text) + 1):
            parts = list(T.wake_word_parts([text[:split], text[split:]], "小度小度"))
            self.assertEqual("".join(s for _, s in parts), text)
            self.assertEqual([s for guarded, s in parts if guarded], ["小度 小度", "小度小度"])

    def test_recorded_wake_word_is_removed_only_from_the_start(self):
        strip = T.strip_wake_prefix
        self.assertEqual(strip("小度，小度。小度小度，下一首吧。", "小度小度"), "下一首吧。")
        self.assertEqual(strip("小度小度。", "小度小度"), "")
        self.assertEqual(strip("播放小度小度这首歌", "小度小度"), "播放小度小度这首歌")

    def test_links_and_citations_are_not_read(self):
        cases = [
            ("据报道([新华网](https://news.cn/a))今天放晴。", "据报道今天放晴。"),
            ("详见[官网](https://example.com)。", "详见官网。"),
            ("来源：https://example.com/x 结束", "来源： 结束"),
            ("**重点**内容[1]", "重点内容"),
            ("# 标题\n> 引用", "标题\n引用"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(T.spoken_text(text), expected)

    def test_stream_holds_back_an_unfinished_link(self):
        stream = T.SpokenStream()
        out = stream.feed("今天晴[新华")
        out += stream.feed("网](https://news")
        self.assertEqual(out, "今天晴")
        out += stream.feed(".cn/a)。")
        out += stream.flush()
        self.assertEqual(out, "今天晴新华网。")

    def test_unfinished_and_question_endings(self):
        self.assertTrue(T.UNFINISHED.search("我想听，"))
        self.assertTrue(T.UNFINISHED.search("然后"))
        self.assertFalse(T.UNFINISHED.search("放首歌。"))
        self.assertTrue(T.QUESTION_END.search("你想听什么歌？"))
        self.assertTrue(T.QUESTION_END.search("要我打开吗"))
        self.assertFalse(T.QUESTION_END.search("好的。"))
        self.assertTrue(T.FILLER.fullmatch(T.compact("嗯，啊。")))


def make_speaker():
    session = Session()
    session.turn_models = {}
    word = WakeWord("小度小度", Mock())
    models = SimpleNamespace(tts_model="tts", tts_voice="voice")
    speaker = speak.Speaker(Mock(), Mock(), session, Mock(), word, models, SimpleNamespace(speech_rate=1.5), Mock())
    return speaker, session, word


class SpeakerTests(unittest.TestCase):
    def test_only_the_played_word_guards_raw(self):
        speaker, _, word = make_speaker()
        segments = []

        def segment(fragments, on_play=None):
            text = "".join(fragments)
            self.assertNotEqual(word.raw_muted_until, math.inf)
            if on_play:
                on_play()
            segments.append((text, word.raw_muted_until == math.inf))
            speaker.heard += text

        speaker.segment = segment
        reply = speaker.stream(["你可以叫我小", "度小度", "，下面继续。"])
        self.assertEqual(segments, [("你可以叫我", False), ("小度小度", True), ("，下面继续。", False)])
        self.assertEqual(reply, "你可以叫我小度小度，下面继续。")
        remaining = word.raw_muted_until - time.monotonic()
        self.assertLessEqual(remaining, RAW_CAPTURE_TAIL)
        self.assertGreater(remaining, 0)

    def test_local_fallback_only_speaks_unheard_text(self):
        speaker, session, _ = make_speaker()
        speaker.heard = "小度小度，"
        with patch.object(speak.offline, "speak", return_value=True) as say:
            speaker.local("小度小度，继续播放")
        self.assertEqual(say.call_count, 1)
        self.assertEqual(say.call_args.args[0], "继续播放")
        self.assertIsNone(say.call_args.kwargs["on_play"])
        self.assertEqual(speaker.heard, "小度小度，继续播放")
        self.assertEqual(session.turn_models["speech"], "local")

    def test_failed_local_playback_is_a_speaker_error(self):
        speaker, _, _ = make_speaker()
        with patch.object(speak.offline, "speak", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "speaker"):
                speaker.local("你好")

    def test_cloud_failure_falls_back_to_local_but_not_cancellation(self):
        speaker, _, _ = make_speaker()
        speaker.stream = Mock(side_effect=RuntimeError("tts down"))
        speaker.local = Mock()
        speaker.say("你好")
        speaker.local.assert_called_once_with("你好")
        speaker.stream = Mock(side_effect=TurnCancelled())
        with self.assertRaises(TurnCancelled):
            speaker.say("你好")
        speaker.local.reset_mock()
        speaker.say("你好", offline_mode=True)
        speaker.local.assert_called_once_with("你好")

    def test_punctuation_only_segment_needs_no_tts(self):
        speaker, _, _ = make_speaker()
        speaker.segment(iter(["。", "！"]))
        self.assertEqual(speaker.heard, "。！")
        speaker.link.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
