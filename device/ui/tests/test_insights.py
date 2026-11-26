import datetime as dt
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
