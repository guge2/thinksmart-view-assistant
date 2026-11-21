import math
import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

# These tests exercise the audio routing and state machine without device SDKs.
with patch.dict(sys.modules, {"dashscope": Mock(), "sherpa_onnx": Mock(), "system": Mock()}):
    import assistant as A


class WakeTests(unittest.TestCase):
    def setUp(self):
        self.a = A.ThinkSmartAssistant.__new__(A.ThinkSmartAssistant)
        self.a.wake_phrase = "小度小度"
        self.a.raw_muted_until = 0

    def test_word_split_across_fragments(self):
        text = "你好小度 小度小度小度。还有小度"
        for split in range(len(text) + 1):
            parts = list(A.wake_word_parts([text[:split], text[split:]], self.a.wake_phrase))
            self.assertEqual("".join(s for _, s in parts), text)
            self.assertEqual([s for guarded, s in parts if guarded], ["小度 小度", "小度小度"])

    def test_recorded_wake_word_is_removed_only_from_the_start(self):
        strip = A.strip_wake_prefix
        phrase = self.a.wake_phrase
        self.assertEqual(strip("小度，小度。小度小度，下一首吧。", phrase), "下一首吧。")
        self.assertEqual(strip("小度小度。", phrase), "")
        self.assertEqual(strip("播放小度小度这首歌", phrase), "播放小度小度这首歌")

    def test_only_the_played_word_guards_raw(self):
        segments = []

        def speak(fragments, on_play=None):
            text = "".join(fragments)
            self.assertNotEqual(self.a.raw_muted_until, math.inf)
            if on_play:
                on_play()
            segments.append((text, self.a.raw_muted_until == math.inf))
            self.a.heard_reply += text

        self.a.speak_segment = speak
        reply = self.a.speak_stream(["你可以叫我小", "度小度", "，下面继续。"])
        self.assertEqual(segments, [("你可以叫我", False), ("小度小度", True), ("，下面继续。", False)])
        self.assertEqual(reply, "你可以叫我小度小度，下面继续。")
        remaining = self.a.raw_muted_until - time.monotonic()
        self.assertLessEqual(remaining, A.RAW_CAPTURE_TAIL)
        self.assertGreater(remaining, 0)

    def test_local_fallback_only_speaks_unheard_text(self):
        self.a.heard_reply = "小度小度，"
        self.a.turn_models = {}
        self.a.ui = Mock()
        self.a.check_cancel = self.a.set_phase = Mock()
        with patch.object(A.offline, "speak", return_value=True) as speak:
            self.a.speak_local("小度小度，继续播放")
        self.assertEqual(speak.call_count, 1)
        self.assertEqual(speak.call_args.args[0], "继续播放")
        self.assertIsNone(speak.call_args.kwargs["on_play"])

    def test_only_accepted_hits_start_cooldown(self):
        a = self.a
        a.phase = "listen"
        a.barge_in_requested = threading.Event()
        a.cancel_requested = threading.Event()
        a.last_wake_mono = -math.inf
        a.alarms = SimpleNamespace(ringing=False)
        a.wake_hits = 0
        a.clear_audio = a.queue_audio = Mock()
        with patch.object(A.time, "monotonic", side_effect=[10, 11, 12]):
            a.accept_wake("小度小度", "raw", ())
            a.phase = "speak"
            a.accept_wake("小度小度", "raw", ())
            self.assertTrue(a.cancel_requested.is_set())
            a.barge_in_requested.clear()
            a.accept_wake("小度小度", "AEC", ())
        self.assertEqual(a.wake_hits, 1)
        self.assertEqual(a.last_wake_mono, 11)

    def test_music_prefix_quiets_song_but_needs_confirmation(self):
        player = Mock()
        stream = A.MusicWake(Mock(), ("full", "prefix"), "小度小度")
        stream.full.feed = Mock(return_value="")
        stream.prefix.feed = Mock(return_value="music-prefix")
        self.assertEqual(stream.feed([], player), "")
        player.make_room_for_voice.assert_called_once()
        stream.frames += 12
        self.assertEqual(stream.feed([], player), "小度小度")
        stream.frames += 90
        self.assertEqual(stream.feed([], player), "")

    def test_custom_name_requires_its_own_ending(self):
        stream = A.MusicWake(Mock(), ("full", "prefix"), "小爱同学")
        stream.full.feed = Mock(return_value="")
        stream.prefix.feed = Mock(side_effect=["music-prefix", "music-prefix", "小爱同学"])
        for _ in range(2):
            stream.frames += 12
            self.assertEqual(stream.feed([], Mock()), "")
        self.assertEqual(stream.feed([], Mock()), "小爱同学")

    def test_raw_hit_while_aec_frame_is_incomplete(self):
        a = self.a
        a.keyword_spotter = Mock()
        a.music = SimpleNamespace(player=SimpleNamespace(state="stopped"))
        a.phase = "idle"
        a.keyword_spotter.get_result.return_value = "小度小度"
        a.mic_enabled = threading.Event()
        a.mic_enabled.set()
        a.raw_progress = 0
        a.capture_progress = time.monotonic()
        a.capture_levels = {}
        pipes = [os.pipe(), os.pipe()]
        for _, write_fd in pipes:
            self.addCleanup(os.close, write_fd)
        (aec, aec_write), (raw, raw_write) = pipes
        aec = self.enter_pipe(aec)
        raw = self.enter_pipe(raw)
        a.create_capture = lambda _: raw
        detector = A.WakeDetector(a)
        detector.decode = lambda stream, data, gain: "小度小度" if stream is detector.raw_stream else ""
        os.write(aec_write, b"\0\0")
        os.write(raw_write, bytes(A.FRAME_BYTES))
        self.assertEqual(detector.poll(aec), (None, "小度小度", "raw"))
        os.write(aec_write, bytes(A.FRAME_BYTES - 2))
        self.assertEqual(detector.poll(aec)[0], bytes(A.FRAME_BYTES))
        for phase in ("wake", "listen"):
            a.phase = phase
            os.write(aec_write, bytes(A.FRAME_BYTES))
            os.write(raw_write, bytes(A.FRAME_BYTES))
            self.assertEqual(detector.poll(aec), (bytes(A.FRAME_BYTES), "", "raw"))
        a.phase = "speak"
        os.write(raw_write, bytes(A.FRAME_BYTES))
        self.assertEqual(detector.poll(aec), (None, "小度小度", "raw"))

    def enter_pipe(self, fd):
        pipe = os.fdopen(fd, "rb", buffering=0)
        self.addCleanup(pipe.close)
        return SimpleNamespace(stdout=pipe)
