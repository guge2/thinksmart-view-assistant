"""Weather worth preparing for, shown on the home screen."""

import datetime as dt

from system import WEATHER_CODES

STORMY = frozenset((95, 96, 99))
SNOWY = frozenset((71, 73, 75, 77, 85, 86))
RAINY = frozenset((51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82)) | STORMY
RAIN_LIKELY = 60  # percent
EVENING_HOUR, MORNING_HOURS = 18, range(5, 11)


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
