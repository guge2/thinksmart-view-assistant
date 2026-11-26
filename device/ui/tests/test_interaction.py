import unittest
from threading import Event
from unittest.mock import Mock, patch

from app import BAD, GOOD, App
from ime import PinyinInput
from system import SystemStatus


class TouchControls(unittest.TestCase):
    def setUp(self):
        self.app = App.__new__(App)
        self.app.link = Mock()
        self.app.dirty = set()

    def test_timer_feedback_waits_for_its_own_ack(self):
        app = self.app
        app.start_quick_timer(3)
        request = app.link.send.call_args.kwargs["id"]
        app.on_voice({"e": "ack", "id": "older", "ok": True}, 0)
        self.assertNotIn("已开启", app.notice[0])
        app.on_voice({"e": "ack", "id": request, "ok": True}, 0)
        self.assertEqual(app.notice[:2], ("已开启 3 分钟倒计时", GOOD))
        app.cancel_alarm_item("abc123")
        fields = app.link.send.call_args.kwargs
        self.assertEqual(fields["alarm_id"], "abc123")
        app.on_voice({"e": "ack", "id": fields["id"], "ok": False, "status": "提醒不存在"}, 0)
        self.assertEqual(app.notice[:2], ("提醒不存在", BAD))
        app.link.send.return_value = False
        app.start_quick_timer(5)
        self.assertIn("未送达", app.notice[0])

    def test_replacing_an_alarm_rebuilds_its_cancel_button(self):
        app = self.app
        app.screen, app.music = "alarms", {}
        app.alarm_items = [{"id": "first"}]
        before = app.wanted_layout(0)
        app.alarm_items = [{"id": "second"}]
        self.assertNotEqual(app.wanted_layout(0), before)

    def test_typing_while_dictionary_loads(self):
        for table in ({ord("你"): "nǐ"}, None):
            app = self.app
            app.ime = PinyinInput()
            release = Event()
            def load_dictionary(result=table, gate=release):
                gate.wait(3)
                return result

            with patch("ime.load_pinyin_dict", side_effect=load_dictionary):
                app.open_keyboard("city")
                app.press_key("n")
                app.press_key("i")
                self.assertTrue(app.kb_loading)
                release.set()
                app.ime.loader.join(2)
                app.keyboard_ready()
            if table:
                self.assertEqual(app.candidates, ["你"])
                self.assertEqual(app.kb_buffer, "ni")
            else:
                self.assertTrue(app.kb_latin)
                self.assertEqual((app.kb_text, app.kb_buffer), ("ni", ""))


class StatusTests(unittest.TestCase):
    def setUp(self):
        with patch("threading.Thread.start"), patch.object(SystemStatus, "load_weather_cache", return_value=None):
            self.status = SystemStatus({"location": {"name": "上海"}})

    def test_controls_coalesce_and_do_not_queue_network_jobs(self):
        status = self.status
        status.request_controls(volume=10)
        status.request_controls(brightness=20)
        status.request_controls(volume=30)
        self.assertEqual(status.pending_controls, {"volume": 30, "brightness": 20})
        self.assertTrue(status.jobs.empty())
        self.assertEqual(status.volume, 30)

    def test_late_weather_does_not_replace_the_new_city(self):
        status = self.status
        status.network = {"kind": "wifi"}

        def delayed(location):
            status.settings["location"] = {"name": "北京"}
            status.refresh_weather()
            return location

        with patch("system.fetch_weather", side_effect=delayed), patch("system.write_atomic") as save:
            status.update_weather(10)
        self.assertIsNone(status.weather)
        self.assertEqual(status.next_weather, 0)
        save.assert_not_called()
