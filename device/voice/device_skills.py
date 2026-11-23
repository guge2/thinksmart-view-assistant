"""Skills that change the device itself: screen, speech rate, thinking mode, weather city, and the
morning briefing. The display process owns the settings file and the backlight; these only work out
the new values and send them to it."""

import logging

import information
from config import clamp_rate
from shared import system
from skills import Result

SCREENS = {"home": "主页", "weather": "天气详情页", "alarms": "闹钟与计时器页面",
           "settings": "设置页面", "music": "音乐设置页面"}


def _number(args, key, default):
    value = args.get(key)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


class DeviceSkills:
    def __init__(self, prefs, ui, alarms, session):
        self.prefs = prefs
        self.ui = ui
        self.alarms = alarms
        self.session = session

    def control_device(self, args):
        prefs = self.prefs
        update, results = {}, []
        level, change = _number(args, "brightness", -1), _number(args, "brightness_change", 0)
        if 1 <= level <= 100 or change:
            value = round(max(1, min(100, level if 1 <= level <= 100 else (prefs.brightness or 80) + change)))
            prefs.brightness = update["brightness"] = value
            results.append(f"屏幕亮度已调到百分之{value}")
        rate, rate_change = _number(args, "speech_rate", 0), _number(args, "speech_rate_change", 0)
        if 0.5 <= rate <= 2.0 or abs(rate_change) >= 0.05:
            prefs.speech_rate = update["speech_rate"] = clamp_rate(
                rate if 0.5 <= rate <= 2.0 else prefs.speech_rate + rate_change)
            results.append(f"播报语速已调到{prefs.speech_rate:g}倍")
        thinking_mode = str(args.get("thinking_mode") or "").strip()
        if thinking_mode in ("on", "off"):
            prefs.thinking = update["thinking"] = thinking_mode == "on"
            results.append(f"深度思考模式已{'开启' if prefs.thinking else '关闭'}")
        city = str(args.get("weather_city") or "").strip()[:40]
        if city:
            try:
                places = system.search_places(city)
            except Exception as error:
                logging.warning("城市查询失败：%s", error)
                places = None
            if places:
                place = places[0]
                update["location"] = {key: place[key] for key in ("name", "lat", "lon")}
                results.append(f"默认天气城市已设为{place['name']}")
            else:
                results.append(f"没有找到城市{city}" if places == [] else "城市查询暂时不可用")
        screen = str(args.get("screen") or "").strip()
        if screen == "off":
            self.session.screen_off_pending = True
            results.append("好的，即将关闭屏幕")
        elif screen in SCREENS:
            update["screen"] = screen
            results.append(f"已在屏幕上打开{SCREENS[screen]}")
        if update:
            self.ui.emit("ui_set", **update)
        if not results:
            return Result("没有听懂要调整哪项设备设置", False)
        return Result("，".join(results), bool(update) or screen == "off")

    def daily_briefing(self, args):
        weather = information.get_weather({}).message
        plan = self.alarms.today_plan()
        return Result(f"{information.current_time()}{weather}"
                      + (f"今天接下来的安排：{plan}。" if plan else "今天接下来没有闹钟或提醒。"), True)

    def status_summary(self):
        volume, muted = system.read_volume()
        volume_text = f"{volume}%（{'已静音' if muted else '未静音'}）" if volume is not None else "未知"
        brightness = f"{self.prefs.brightness}%" if self.prefs.brightness else "未知"
        return (
            f"设备状态：当前音量{volume_text}，屏幕亮度{brightness}，"
            f"播报语速{self.prefs.speech_rate:g}倍，深度思考{'开启' if self.prefs.thinking else '关闭'}。"
        )
