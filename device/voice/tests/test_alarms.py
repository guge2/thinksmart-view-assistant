import datetime as dt
import os
import tempfile
import time
import unittest
from unittest import mock

os.environ["TZ"] = "Asia/Shanghai"
time.tzset()

import alarms
from alarms import NOTHING_RINGING, AlarmService, plan_time


def at(*args):
    return dt.datetime(*args).timestamp()


# Monday 2026-11-09 08:00.
NOW = at(2026, 11, 9, 8, 0)


class PlanTime(unittest.TestCase):
    def due(self, kind="alarm", **args):
        return plan_time(args, kind, NOW)

    def test_clock_times(self):
        self.assertEqual(self.due(time="09:30")[0], at(2026, 11, 9, 9, 30))
        self.assertEqual(self.due(time="07:00")[0], at(2026, 11, 10, 7, 0))
        self.assertEqual(self.due(time="07:00", days_from_today=2)[0], at(2026, 11, 11, 7, 0))
        self.assertEqual(self.due(time="07:00:30", date="2026-12-01")[0], at(2026, 12, 1, 7, 0, 30))

    def test_relative(self):
        self.assertEqual(self.due("timer", minutes=5), (NOW + 300, [], None, 300))
        self.assertEqual(self.due("reminder", hours=1, minutes=30)[0], NOW + 5400)

    def test_repeats(self):
        due, weekdays, _, _ = self.due(time="07:00", repeat="workdays")
        self.assertEqual((due, weekdays), (at(2026, 11, 10, 7, 0), [0, 1, 2, 3, 4]))
        self.assertEqual(self.due(time="09:00", repeat="weekly", weekdays=[5, 6])[0], at(2026, 11, 14, 9, 0))
        due, _, month_day, _ = self.due(time="07:00", repeat="monthly", month_day=31)
        self.assertEqual((due, month_day), (at(2026, 11, 30, 7, 0), 31))

    def test_invalid(self):
        cases = [
            ("timer", {}),
            ("timer", {"time": "07:00"}),
            ("alarm", {"time": "07:00", "minutes": 5}),
            ("alarm", {"time": "7点"}),
            ("alarm", {"time": "07:00", "date": "2026-11-01"}),
            ("alarm", {"time": "07:00", "date": "2028-01-01"}),
            ("alarm", {"time": "07:00", "repeat": "weekly"}),
            ("reminder", {"time": "07:00", "repeat": "daily"}),
            ("timer", {"seconds": 8 * 86400}),
        ]
        for kind, args in cases:
            with self.subTest(kind=kind, args=args), self.assertRaises(ValueError):
                plan_time(args, kind, NOW)


def clock(hour):
    class Fixed(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 11, 9, hour, 0, tzinfo=tz)
    return mock.patch.object(alarms.dt, "datetime", Fixed)


class Service(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "alarms.json")
        self.service = AlarmService(state_file=self.path)

    def tearDown(self):
        self.dir.cleanup()

    def command(self, **args):
        return self.service.manage_alarm(args)

    def ring(self, kind="alarm", label=""):
        self.service.ringing = True
        self.service.last_rang = ({"id": "r1", "kind": kind, "label": label}, time.monotonic())

    def test_create_list_cancel_persist(self):
        self.assertTrue(self.command(action="create", kind="timer", minutes=10, label="煮蛋").ok)
        self.assertFalse(self.command(action="create", kind="reminder", minutes=5).ok)
        self.assertIn("煮蛋", self.command(action="list").message)
        self.assertEqual(len(AlarmService(state_file=self.path).items), 1)
        self.assertTrue(self.command(action="cancel").message.startswith("已取消计时器"))
        self.assertEqual(AlarmService(state_file=self.path).items, [])
        self.assertFalse(self.command(action="cancel", id="nope").ok)

    def test_alarms_and_reminders_ring_with_music(self):
        for kind, expected in (("alarm", "music"), ("reminder", "music"), ("timer", "beep")):
            with self.subTest(kind=kind):
                rang = []
                service = AlarmService(state_file=self.path, music=object())
                service.items = [{"id": kind, "kind": kind, "due": time.time(), "label": "", "weekdays": []}]
                service._ring_music = lambda: rang.append("music") or service.stopped.set() or True
                service._ring = lambda cycles=100: rang.append("beep") or service.stopped.set()
                with mock.patch.object(service, "_save"):
                    service._run()
                self.assertEqual(rang, [expected])

    def test_dismiss(self):
        self.assertEqual(self.command(action="dismiss"), (NOTHING_RINGING, False, False))
        self.ring()
        self.assertEqual(self.command(action="dismiss").message, "已停止提示音")
        self.assertTrue(self.service.silence.is_set())
        # The ring ended moments ago, e.g. silenced by the wake word.
        self.service.ringing = False
        self.assertEqual(self.command(action="dismiss").message, "闹钟已经关了")
        self.service.last_rang = (self.service.last_rang[0], time.monotonic() - 61)
        self.assertFalse(self.command(action="dismiss").ok)

    def test_briefing_after_morning_alarm(self):
        self.ring()
        self.service.dismiss()
        with clock(7):
            self.assertTrue(self.service.take_briefing())
            self.assertFalse(self.service.take_briefing())
        self.ring()
        self.service.dismiss()
        with clock(14):
            self.assertFalse(self.service.take_briefing())
        self.ring("timer")
        self.service.dismiss()
        with clock(7):
            self.assertFalse(self.service.take_briefing())

    def test_snooze(self):
        self.assertFalse(self.command(action="snooze").ok)
        self.ring(label="起床")
        self.service.dismiss()
        self.service.ringing = False
        self.assertEqual(self.command(action="snooze", minutes=10).message, "好的，10分钟后再提醒")
        self.assertIsNone(self.service.briefing_at)
        [item] = self.service.items
        self.assertEqual((item["kind"], item["label"]), ("alarm", "起床"))
        self.assertAlmostEqual(item["due"], time.time() + 600, delta=5)
        self.assertFalse(self.command(action="snooze").ok)

    def test_today_plan(self):
        midnight = dt.datetime.combine(dt.date.today(), dt.time()).timestamp()
        self.service.items = [
            {"id": "b", "kind": "reminder", "due": midnight + 23 * 3600, "label": "吃药"},
            {"id": "a", "kind": "alarm", "due": midnight + 7 * 3600, "label": ""},
            {"id": "c", "kind": "alarm", "due": midnight + 31 * 3600, "label": ""},
        ]
        self.assertEqual(self.service.today_plan(), "07:00闹钟；23:00提醒：吃药")

    def test_power_loss_drops_missed_once_and_moves_repeats(self):
        now = time.time()
        self.service.items = [
            {"id": "once", "kind": "alarm", "due": now - 3600, "label": "", "weekdays": []},
            {"id": "daily", "kind": "alarm", "due": now - 3600, "label": "", "weekdays": list(range(7))},
        ]
        self.service._save()
        [item] = AlarmService(state_file=self.path).items
        self.assertEqual(item["id"], "daily")
        self.assertAlmostEqual(item["due"], now - 3600 + 86400, delta=3700)

    def test_announce_runs_outside_lock(self):
        import threading

        locked_during_emit = []

        def emit(event, **_fields):
            if event != "alarms":
                return
            result = []

            def probe():
                acquired = service.lock.acquire(blocking=False)
                if acquired:
                    service.lock.release()
                result.append(not acquired)

            t = threading.Thread(target=probe)
            t.start()
            t.join()
            locked_during_emit.append(result[0])

        service = AlarmService(emit=emit, state_file=self.path)
        self.assertTrue(service.manage_alarm({"action": "create", "kind": "timer", "minutes": 5, "label": "茶"}).ok)
        self.assertTrue(service.manage_alarm({"action": "cancel"}).ok)
        self.assertEqual(locked_during_emit, [False, False])


if __name__ == "__main__":
    unittest.main()
