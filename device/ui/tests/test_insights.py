import datetime as dt
import json
import os
import tempfile
import time
import unittest

import insights as I
from recipe_view import recipe_timers

TODAY = dt.date(2026, 11, 23)


def weather(*days):
    return {"days": [dict(day, date=(TODAY + dt.timedelta(days=n)).isoformat()) for n, day in enumerate(days)]}


def at(hour, minute=0, day=0):
    return dt.datetime.combine(TODAY + dt.timedelta(days=day), dt.time(hour, minute))


MILD = {"code": 1, "max": 15, "min": 8, "rain": 10}


class WeatherTipTests(unittest.TestCase):
    def test_evening_looks_at_tomorrow(self):
        cases = [
            ({"code": 63, "max": 12, "min": 8, "rain": 80}, "明天中雨（降水概率 80%），出门记得带伞"),
            ({"code": 3, "max": 14, "min": 8, "rain": 70}, "明天可能下雨（降水概率 70%），出门记得带伞"),
            ({"code": 95, "max": 20, "min": 15}, "明天有雷阵雨，出门注意安全"),
            ({"code": 73, "max": 1, "min": -3}, "明天有中雪，出门注意路滑"),
            ({"code": 1, "max": 8, "min": 4, "rain": 0}, "明天降温 7°，最高 8°，注意添衣"),
            ({"code": 1, "max": 13, "min": -1, "rain": None}, "明早最低 -1°，注意防冻"),
            ({"code": 1, "max": 22, "min": 10}, "明天升温 7°，最高 22°"),
            ({"code": 1, "max": 16, "min": 9}, None),
        ]
        for tomorrow, expected in cases:
            with self.subTest(tomorrow=tomorrow):
                self.assertEqual(I.weather_tip(weather(MILD, tomorrow), at(20)), expected)

    def test_morning_looks_at_today(self):
        self.assertEqual(I.weather_tip(weather({"code": 61, "max": 10, "min": 5}), at(7)), "今天有小雨，出门带伞")
        self.assertEqual(I.weather_tip(weather({"code": 2, "max": 10, "min": 5, "rain": 65}), at(7)),
                         "今天降水概率 65%，出门带伞")
        self.assertEqual(I.weather_tip(weather({"code": 0, "max": 20, "min": 6}), at(7)),
                         "今天温差 14°（6–20°），早晚加件外套")
        self.assertIsNone(I.weather_tip(weather(MILD), at(7)))

    def test_quiet_hours_and_stale_or_missing_forecasts(self):
        rainy = weather({"code": 61, "max": 10, "min": 5}, {"code": 61, "max": 10, "min": 5})
        self.assertIsNone(I.weather_tip(rainy, at(14)))
        self.assertIsNone(I.weather_tip(rainy, at(20, day=5)))
        self.assertIsNone(I.weather_tip(None, at(20)))
        self.assertIsNone(I.weather_tip({"days": []}, at(7)))


class RoutineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "routine.json")

    def test_night_spans_midnight(self):
        self.assertEqual(I.night_of(at(23, 10)), ("2026-11-23", 310))
        self.assertEqual(I.night_of(at(0, 40, day=1)), ("2026-11-23", 400))
        self.assertIsNone(I.night_of(at(12)))
        self.assertEqual(I.clock(400), "0:40")

    def test_learns_the_usual_bedtime(self):
        routine = I.Routine(self.path)
        routine.record(at(20))
        routine.record(at(23, 0))  # the latest use of a night counts
        routine.record(at(21, 0))
        routine.record(at(0, 10, day=2))  # the night of the 24th, past midnight
        routine.record(at(19, 0, day=2))  # an evening out says nothing about bedtime
        self.assertEqual(routine.nights, {"2026-11-23": 300, "2026-11-24": 370, "2026-11-25": 60})
        self.assertIsNone(routine.usual(at(22, day=3)))
        routine.record(at(23, 30, day=3))
        self.assertIsNone(routine.usual(at(22, day=3)))  # tonight is not history yet
        self.assertEqual(I.clock(routine.usual(at(22, day=4))), "23:30")
        routine.save()
        with open(self.path) as source:
            self.assertEqual(len(json.load(source)), 4)
        self.assertEqual(I.Routine(self.path).nights, routine.nights)

    def test_morning_after_a_late_night(self):
        routine = I.Routine(self.path)
        routine.nights = {f"2026-11-{d}": 300 for d in (18, 19, 20, 21)}
        routine.nights["2026-11-22"] = 420  # 01:00
        self.assertEqual(routine.last_night_tip(at(8)), "昨晚 1:00 才休息，比平时晚 120 分钟，今天别太累")
        self.assertIsNone(routine.last_night_tip(at(14)))
        routine.nights["2026-11-22"] = 330
        self.assertIsNone(routine.last_night_tip(at(8)))

    def test_bad_file_starts_empty(self):
        with open(self.path, "w") as target:
            target.write("[1, 2]")
        with self.assertLogs(level="WARNING"):
            self.assertEqual(I.Routine(self.path).nights, {})


class BedtimeTipTests(unittest.TestCase):
    def alarm(self, when, kind="alarm"):
        return {"kind": kind, "due": when.timestamp(), "time": when.strftime("%H:%M")}

    def test_counts_down_to_the_morning_alarm(self):
        alarms = [self.alarm(at(7, 0, day=1)), self.alarm(at(23, 50), "timer")]
        self.assertEqual(I.bedtime_tip(alarms, None, at(22, 40)), ("明早 07:00 有闹钟，现在睡还能睡 8 小时 20 分", False))
        text, urgent = I.bedtime_tip(alarms, None, at(1, 30, day=1))
        self.assertEqual(text, "今早 07:00 有闹钟，现在睡还能睡 5 小时 30 分")
        self.assertTrue(urgent)

    def test_window_follows_the_usual_bedtime(self):
        usual = 330  # 23:30
        self.assertIsNone(I.bedtime_tip([], usual, at(22, 30)))
        self.assertEqual(I.bedtime_tip([], usual, at(23, 10)), ("快到平时休息的时间了（约 23:30）", False))
        self.assertIsNone(I.bedtime_tip([], usual, at(23, 45)))
        self.assertEqual(I.bedtime_tip([], usual, at(0, 30, day=1)), ("比平时（23:30）晚了 60 分钟，早点休息吧", True))
        self.assertIsNone(I.bedtime_tip([], usual, at(4, 30, day=1)))
        self.assertIsNone(I.bedtime_tip([], None, at(23, 0)))
        self.assertIsNone(I.bedtime_tip([self.alarm(at(7, 0, day=1))], None, at(21, 0)))


class RecipeTimerTests(unittest.TestCase):
    def test_only_this_dishes_timers(self):
        now = time.time()
        items = [{"kind": "timer", "label": "红烧肉·第5步", "due": now + 600},
                 {"kind": "timer", "label": "红烧肉·第2步", "due": now + 30},
                 {"kind": "timer", "label": "番茄炒蛋·第1步", "due": now + 60},
                 {"kind": "alarm", "label": "红烧肉·第1步", "due": now + 60},
                 {"kind": "timer", "label": "", "due": now + 10}]
        for number, item in enumerate(items):
            item["id"] = str(number)
        timers = recipe_timers({"title": "红烧肉"}, items)
        self.assertEqual([(t["step"], t["id"]) for t in timers], [(2, "1"), (5, "0")])
        self.assertAlmostEqual(timers[0]["seconds"], 30, delta=1)


if __name__ == "__main__":
    unittest.main()
