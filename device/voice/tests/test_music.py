import threading
import time
import unittest
from unittest.mock import patch

import music


class MusicRegressionTest(unittest.TestCase):
    def test_describe_sleep_timer_uses_monotonic(self):
        service = music.MusicService.__new__(music.MusicService)
        service.account = {"s": "none"}
        service.held = False
        service.player = music.Player(
            resolve=lambda t: (None, None),
            extend=lambda t: [],
            notify=lambda *a, **k: None,
            duck_level=0.3,
            mode="shuffle",
        )
        service.player.queue = [{"src": "qq", "id": "1", "title": "稻香", "artist": "周杰伦"}]
        service.player.index = 0
        service.player.state = "playing"
        service.player.set_sleep(15)
        desc = service.describe()
        self.assertIn("剩余约15分钟停止", desc)

    def test_wake_hint_restores_only_its_own_gain(self):
        player = music.Player.__new__(music.Player)
        player.ducked = False
        player.duck_level = 0.2
        with patch.object(music.time, "monotonic", side_effect=[10, 11, 12.15, 13, 14]):
            player.make_room_for_voice()
            self.assertAlmostEqual(player.playback_gain(), 0.12)
            self.assertAlmostEqual(player.playback_gain(), 0.56)
            self.assertEqual(player.playback_gain(), 1)
            player.ducked = True
            self.assertEqual(player.playback_gain(), 0.2)

    def test_download_rejects_non_http_scheme(self):
        dl = music.Download("file:///etc/passwd", {}, threading.Condition())
        deadline = time.monotonic() + 2
        while not dl.finished and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(dl.finished)
        self.assertIsInstance(dl.error, ValueError)

    def test_track_change_wakes_decoder_waiting_for_download(self):
        condition = threading.Condition()
        track_changed = threading.Event()
        waiting = threading.Event()
        with patch.object(music.Download, "run"):
            download = music.Download("https://example.com/song.mp3", {}, condition)

        def alive():
            waiting.set()
            return not track_changed.is_set()

        chunks = []
        reader = threading.Thread(target=lambda: chunks.append(download.get(0, alive)), daemon=True)
        reader.start()
        self.assertTrue(waiting.wait(1))
        with condition:
            track_changed.set()
            condition.notify_all()
        reader.join(1)
        self.assertEqual(chunks, [None])


if __name__ == "__main__":
    unittest.main()
