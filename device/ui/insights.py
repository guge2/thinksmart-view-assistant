"""Quiet tips for the home screen, shown and never spoken: the weather worth preparing for (tomorrow's
in the evening, today's in the morning) and bedtime, learned from when the device stops being used at
night and measured against tomorrow morning's alarm."""

import datetime as dt
import json
import logging
import statistics

from system import WEATHER_CODES, write_atomic

STORMY = frozenset((95, 96, 99))
SNOWY = frozenset((71, 73, 75, 77, 85, 86))
RAINY = frozenset((51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82)) | STORMY
RAIN_LIKELY = 60  # percent
EVENING_HOUR, MORNING_HOURS = 18, range(5, 11)

# A night runs from 18:00 to 05:00; its times are minutes after 18:00, so midnight does not split it.
NIGHT_START, NIGHT_END = 18, 5
NIGHTS_KEPT, NIGHTS_USED, NIGHTS_NEEDED = 30, 14, 3
DEFAULT_BEDTIME = (22 - NIGHT_START) * 60 + 30  # 22:30
BEDTIME_LEAD = 30  # minutes before the usual bedtime the tip appears
BEDTIME_LAST = (4 + 24 - NIGHT_START) * 60  # no bedtime tip after 04:00
SHORT_SLEEP_HOURS = 7
LATE_NIGHT = 60  # minutes past the usual bedtime that the next morning mentions


def _day(weather, date):
    return next((day for day in (weather or {}).get("days") or [] if day.get("date") == date.isoformat()), None)


def _describe(code):
    return WEATHER_CODES.get(code, ("",))[0]


def weather_tip(weather, now):
    """One line about the weather to prepare for, or None."""
    if now.hour >= EVENING_HOUR:
        today, tomorrow = _day(weather, now.date()), _day(weather, now.date() + dt.timedelta(days=1))
        if not tomorrow:
            return None
        code, rain = tomorrow.get("code"), tomorrow.get("rain")
        if code in STORMY:
            return f"明天有{_describe(code)}，出门注意安全"
        if code in SNOWY:
            return f"明天有{_describe(code)}，出门注意路滑"
        if code in RAINY or (rain or 0) >= RAIN_LIKELY:
            chance = f"（降水概率 {rain}%）" if rain else ""
            return f"明天{_describe(code) if code in RAINY else '可能下雨'}{chance}，出门记得带伞"
        if today:
            drop = round(today["max"] - tomorrow["max"])
            if drop >= 5:
                return f"明天降温 {drop}°，最高 {round(tomorrow['max'])}°，注意添衣"
            if tomorrow["min"] <= 0 < today["min"]:
                return f"明早最低 {round(tomorrow['min'])}°，注意防冻"
            rise = -drop
            if rise >= 6:
                return f"明天升温 {rise}°，最高 {round(tomorrow['max'])}°"
        return None
    if now.hour in MORNING_HOURS:
        today = _day(weather, now.date())
        if not today:
            return None
        code, rain = today.get("code"), today.get("rain")
        if code in RAINY or code in SNOWY:
            return f"今天有{_describe(code)}，出门带伞"
        if (rain or 0) >= RAIN_LIKELY:
            return f"今天降水概率 {rain}%，出门带伞"
        spread = round(today["max"] - today["min"])
        if spread >= 10:
            return f"今天温差 {spread}°（{round(today['min'])}–{round(today['max'])}°），早晚加件外套"
    return None


def night_of(now):
    """(night key, minutes after 18:00) when now falls in a night, else None."""
    if now.hour >= NIGHT_START:
        return now.date().isoformat(), (now.hour - NIGHT_START) * 60 + now.minute
    if now.hour < NIGHT_END:
        evening = now.date() - dt.timedelta(days=1)
        return evening.isoformat(), (now.hour + 24 - NIGHT_START) * 60 + now.minute
    return None


def clock(minutes):
    hours, minutes = divmod(minutes + NIGHT_START * 60, 60)
    return f"{hours % 24}:{minutes:02d}"


class Routine:
    """The last time the device was used each night, kept for a month."""

    def __init__(self, path):
        self.path = path
        self.nights = {}
        self.dirty = False
        try:
            with open(path, encoding="utf-8") as source:
                saved = json.load(source)
            self.nights = {k: v for k, v in saved.items() if isinstance(k, str) and type(v) is int}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as error:
            logging.warning("作息记录读取失败：%s", error)

    def record(self, now):
        night = night_of(now)
        if night is None:
            return
        key, minutes = night
        if self.nights.get(key, -1) < minutes:
            self.nights[key] = minutes
            self.dirty = True

    def save(self):
        if not self.dirty:
            return
        self.nights = dict(sorted(self.nights.items())[-NIGHTS_KEPT:])
        try:
            write_atomic(self.path, json.dumps(self.nights).encode("utf-8"))
            self.dirty = False
        except OSError as error:
            logging.warning("作息记录保存失败：%s", error)

    def last_night_tip(self, now):
        """In the morning, a word when last night ran well past the usual bedtime."""
        if now.hour not in MORNING_HOURS:
            return None
        key = (now.date() - dt.timedelta(days=1)).isoformat()
        last_night = self.nights.get(key)
        usual = self.usual(now, exclude=key)
        if last_night is None or usual is None or last_night - usual < LATE_NIGHT:
            return None
        return f"昨晚 {clock(last_night)} 才休息，比平时晚 {last_night - usual} 分钟，今天别太累"

    def usual(self, now, exclude=None):
        """The usual bedtime in minutes after 18:00, from recent nights before this one; None until known."""
        current = exclude or (night_of(now) or (None,))[0]
        recent = [minutes for key, minutes in sorted(self.nights.items()) if key != current][-NIGHTS_USED:]
        # A last use before 21:00 says more about an evening out than about bedtime.
        recent = [minutes for minutes in recent if minutes >= 180]
        return round(statistics.median(recent)) if len(recent) >= NIGHTS_NEEDED else None


def bedtime_tip(alarm_items, usual, now):
    """(text, urgent) about going to sleep, or None outside the bedtime window."""
    night = night_of(now)
    if night is None:
        return None
    minutes = night[1]
    start = (usual if usual is not None else DEFAULT_BEDTIME) - BEDTIME_LEAD
    if not start <= minutes <= BEDTIME_LAST:
        return None
    timestamp = now.timestamp()
    morning = [item for item in alarm_items if item.get("kind") == "alarm" and isinstance(item.get("due"), (int, float))
               and 3 * 3600 < item["due"] - timestamp < 14 * 3600]
    if morning:
        alarm = min(morning, key=lambda item: item["due"])
        left = int(alarm["due"] - timestamp) // 60
        when = "今早" if dt.datetime.fromtimestamp(alarm["due"]).date() == now.date() else "明早"
        hours, rest = divmod(left, 60)
        urgent = left < SHORT_SLEEP_HOURS * 60
        # Urgency shows as colour; the line has to fit the tip card.
        return f"{when} {alarm.get('time') or ''} 有闹钟，现在睡还能睡 {hours} 小时 {rest:02d} 分", urgent
    if usual is None:
        return None
    late = minutes - usual
    if late >= BEDTIME_LEAD:
        return f"比平时（{clock(usual)}）晚了 {late} 分钟，早点休息吧", True
    if late < 0:
        return f"快到平时休息的时间了（约 {clock(usual)}）", False
    return None
