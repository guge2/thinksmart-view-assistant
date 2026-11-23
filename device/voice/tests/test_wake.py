import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import device_stubs  # noqa: F401
import session as S
import wake
from config import FRAME_BYTES
from session import Session


class WakeCooldownTests(unittest.TestCase):
    def test_only_accepted_hits_start_cooldown(self):
        s = Session()
        s.phase = "listen"
        with patch.object(S.time, "monotonic", side_effect=[10, 11, 12]):
            self.assertIsNone(s.accept_wake("raw"))
            s.phase = "speak"
            self.assertEqual(s.accept_wake("raw"), "barge")
            self.assertTrue(s.cancel_requested.is_set())
            s.barge_in_requested.clear()
            self.assertIsNone(s.accept_wake("AEC"))
        self.assertEqual(s.wake_hits, 1)
        self.assertEqual(s.last_wake_mono, 11)
        self.assertEqual(s.timing["source"], "raw")

    def test_idle_wake_starts_a_turn_and_keeps_the_screen_on(self):
        s = Session()
        s.screen_off_pending = True
        self.assertEqual(s.accept_wake("AEC"), "start")
        self.assertFalse(s.cancel_requested.is_set())
        self.assertFalse(s.screen_off_pending)


class WakeGuardTests(unittest.TestCase):
    def test_guard_mutes_raw_only_while_and_just_after_playing(self):
        word = wake.WakeWord("小度小度", Mock())
        with word.guard(False) as on_play:
            self.assertIsNone(on_play)
        self.assertTrue(word.raw_allowed())
        with word.guard(True) as on_play:
            on_play()
            self.assertFalse(word.raw_allowed())
        remaining = word.raw_muted_until - time.monotonic()
        self.assertLessEqual(remaining, wake.RAW_CAPTURE_TAIL)
        self.assertGreater(remaining, 0)

    def test_guard_unused_when_the_word_never_played(self):
        word = wake.WakeWord("小度小度", Mock())
        with word.guard(True):
            pass
        self.assertTrue(word.raw_allowed())


class MusicWakeTests(unittest.TestCase):
    def test_music_prefix_quiets_song_but_needs_confirmation(self):
        player = Mock()
        stream = wake.MusicWake(Mock(), ("full", "prefix"), "小度小度")
        stream.full.feed = Mock(return_value="")
        stream.prefix.feed = Mock(return_value="music-prefix")
        self.assertEqual(stream.feed([], player), "")
        player.make_room_for_voice.assert_called_once()
        stream.frames += 12
        self.assertEqual(stream.feed([], player), "小度小度")
        stream.frames += 90
        self.assertEqual(stream.feed([], player), "")

    def test_custom_name_requires_its_own_ending(self):
        stream = wake.MusicWake(Mock(), ("full", "prefix"), "小爱同学")
        stream.full.feed = Mock(return_value="")
        stream.prefix.feed = Mock(side_effect=["music-prefix", "music-prefix", "小爱同学"])
        for _ in range(2):
            stream.frames += 12
            self.assertEqual(stream.feed([], Mock()), "")
        self.assertEqual(stream.feed([], Mock()), "小爱同学")

    def test_music_keywords_need_a_chinese_name(self):
        settings = SimpleNamespace(kws_score=1.0, kws_threshold=0.25)
        self.assertIsNone(wake.music_keywords("hey", settings))
        with patch.object(wake.sherpa_onnx, "text2token", return_value=[["x", "iao3"], ["x"]]):
            full, prefix = wake.music_keywords("小度小度", settings)
        self.assertEqual(full, "x iao3 :2 #0.16 @小度小度")
        self.assertEqual(prefix, "x :3 #0.18 @music-prefix")


class DetectorTests(unittest.TestCase):
    def test_raw_hit_while_aec_frame_is_incomplete(self):
        session = Session()
        session.mic_enabled.set()
        spotter = Mock()
        spotter.get_result.return_value = "小度小度"
        mic = SimpleNamespace(progress=time.monotonic(), raw_progress=0, levels={}, raw_dropped_frames=0)
        music = SimpleNamespace(player=SimpleNamespace(state="stopped"))
        alarms = SimpleNamespace(ringing=False)
        pipes = [os.pipe(), os.pipe()]
        for _, write_fd in pipes:
            self.addCleanup(os.close, write_fd)
        (aec, aec_write), (raw, raw_write) = pipes
        aec = self.enter_pipe(aec)
        raw = self.enter_pipe(raw)
        detector = wake.WakeDetector(mic, session, wake.WakeWord("小度小度", spotter), music, alarms,
                                     open_raw=lambda _: raw)
        detector.decode = lambda stream, data, gain: "小度小度" if stream is detector.raw_stream else ""
        os.write(aec_write, b"\0\0")
        os.write(raw_write, bytes(FRAME_BYTES))
        self.assertEqual(detector.poll(aec), (None, "小度小度", "raw"))
        os.write(aec_write, bytes(FRAME_BYTES - 2))
        self.assertEqual(detector.poll(aec)[0], bytes(FRAME_BYTES))
        for phase in ("wake", "listen"):
            session.phase = phase
            os.write(aec_write, bytes(FRAME_BYTES))
            os.write(raw_write, bytes(FRAME_BYTES))
            self.assertEqual(detector.poll(aec), (bytes(FRAME_BYTES), "", "raw"))
        session.phase = "speak"
        os.write(raw_write, bytes(FRAME_BYTES))
        self.assertEqual(detector.poll(aec), (None, "小度小度", "raw"))

    def test_capture_timeout(self):
        session = Session()
        mic = SimpleNamespace(progress=time.monotonic() - 5, raw_progress=0, levels={}, raw_dropped_frames=0)
        detector = wake.WakeDetector(mic, session, wake.WakeWord("小度小度", Mock()), Mock(), Mock())
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, write_fd)
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            detector.poll(self.enter_pipe(read_fd))

    def enter_pipe(self, fd):
        pipe = os.fdopen(fd, "rb", buffering=0)
        self.addCleanup(pipe.close)
        return SimpleNamespace(stdout=pipe)


if __name__ == "__main__":
    unittest.main()
