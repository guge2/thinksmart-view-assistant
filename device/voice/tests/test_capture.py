import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import device_stubs  # noqa: F401
import capture
from session import Session, TurnCancelled


def make_mic(session=None):
    return capture.Microphone(session or Session(), Mock(), Mock(), SimpleNamespace(ringing=False), Mock(), Mock())


class MicrophoneTests(unittest.TestCase):
    def test_full_queue_drops_the_oldest_frame_and_flags_overflow(self):
        mic = make_mic()
        for index in range(capture.QUEUE_FRAMES + 1):
            mic.put(bytes([index % 256]))
        self.assertTrue(mic.overflow.is_set())
        self.assertEqual(mic.dropped_frames, 1)
        self.assertEqual(mic.frames.get_nowait(), bytes([1]))
        with self.assertRaisesRegex(RuntimeError, "overflow"):
            mic.read()

    def test_restart_queue(self):
        mic = make_mic()
        mic.put(b"old")
        mic.overflow.set()
        mic.restart_queue([b"a", b"b"])
        self.assertFalse(mic.overflow.is_set())
        self.assertEqual([mic.frames.get_nowait(), mic.frames.get_nowait()], [b"a", b"b"])

    def test_read_stops_on_cancel_and_dead_capture(self):
        session = Session()
        mic = make_mic(session)
        with self.assertRaisesRegex(RuntimeError, "stream ended"):
            mic.read()
        session.cancel_requested.set()
        with self.assertRaises(TurnCancelled):
            mic.read()

    def test_clock_counts_bad_windows(self):
        mic = make_mic()
        mic.clock_start = time.monotonic() - 5
        mic._track(bytes(1280))
        self.assertLess(mic.clock["ratio"], capture.MIN_CLOCK_RATIO)
        self.assertEqual(mic.bad_windows, 1)
        self.assertFalse(capture.clock_ok(mic.clock))
        self.assertTrue(capture.clock_ok({"ratio": 1.0}))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = os.path.join(directory.name, "recovery.json")
        self.session = Session()
        self.alarms = SimpleNamespace(ringing=False)

    def make(self):
        with patch.object(capture, "boot_id", return_value="boot-1"):
            return capture.AudioRecovery(make_mic(self.session), self.session, Mock(), self.alarms, self.state)

    def test_request_cancels_the_turn_unless_an_alarm_rings(self):
        recovery = self.make()
        self.alarms.ringing = True
        recovery.request("stalled")
        self.assertTrue(recovery.requested.is_set())
        self.assertFalse(self.session.cancel_requested.is_set())
        self.assertFalse(recovery.pending())
        self.alarms.ringing = False
        self.assertTrue(recovery.pending())
        self.session.interrupted = recovery.pending
        with self.assertRaises(TurnCancelled):
            self.session.check_cancel()

    def test_limit_survives_restarts_within_one_boot(self):
        recovery = self.make()
        now = time.monotonic()
        with open(self.state, "w") as target:
            json.dump({"boot": recovery.boot, "attempts": [now - 30, now - 20, now - 700]}, target)
        recovery = self.make()
        self.assertEqual(len(recovery.attempts), 2)
        with self.assertLogs(level="ERROR"):
            recovery.request("stalled")
        self.assertEqual(recovery.status, "limited")
        self.assertFalse(recovery.requested.is_set())

    def test_recover_restarts_pipewire_and_waits_for_capture(self):
        recovery = self.make()
        mic = recovery.mic
        mic.close = Mock()
        mic.alive = Mock(return_value=True)

        def reopen():
            mic.clock, mic.progress = {"ratio": 1.0}, time.monotonic()
        mic.open = Mock(side_effect=reopen)
        recovery.request("stalled")
        with patch.object(capture.subprocess, "run") as run, self.assertLogs(level="INFO"):
            recovery.recover()
        self.assertIn("pipewire.service", run.call_args.args[0])
        self.assertEqual(recovery.status, "recovered")
        self.assertFalse(recovery.requested.is_set())
        self.assertFalse(self.session.cancel_requested.is_set())
        recovery.music.release.assert_called_once()
        with open(self.state) as source:
            self.assertEqual(len(json.load(source)["attempts"]), 1)


if __name__ == "__main__":
    unittest.main()
