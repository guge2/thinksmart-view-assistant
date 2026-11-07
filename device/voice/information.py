import datetime
import json
import logging
import os
import subprocess
import sys
import time

sys.path.insert(0, "/usr/local/lib/thinksmart-ui")
import system as ui_system

TOOLS = [
    {"type": "function", "function": {
        "name": "get_current_time",
        "description": "查询设备所在时区的当前日期、星期和时间。用户询问现在几点或今天日期时调用。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "查询实时天气或未来两天天气。用户询问天气时必须调用，不能自行编造。",
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string", "description": "用户明确指定的城市；未指定则留空，使用屏幕设置的城市"},
            "day": {"type": "integer", "description": "今天为0，明天为1，后天为2"}}}}},
]


def current_time():
    now = datetime.datetime.now().astimezone()
    weekdays = "一二三四五六日"
    result = f"现在是{now.year}年{now.month}月{now.day}日，星期{weekdays[now.weekday()]}，{now.hour}点{now.minute:02d}分。"
    if os.path.exists("/run/systemd/timesync/synchronized"):
        return result
    try:
        result_code = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                                     capture_output=True, text=True, timeout=3, check=True)
        if result_code.stdout.strip() != "yes":
            result += "设备时间尚未与网络同步，可能不准确。"
    except (OSError, subprocess.SubprocessError):
        result += "设备时间是否准确尚未确认。"
    return result


def home_city():
    return ui_system.Settings()["location"]["name"]


def weather(city="", day=0):
    location = ui_system.Settings()["location"]
    city = str(city or "").strip()[:40]
    if city and city not in (location["name"], "本地", "这里", "当地"):
        places = ui_system.search_places(city)
        if not places:
            return f"没有找到{city}的天气地点，请换个城市名称。", False
        location = places[0]
    try:
        data = ui_system.fetch_weather(location)
    except Exception as error:
        logging.warning("天气查询失败：%s", error)
        return "天气服务暂时不可用，请联网后再试。", False
    try:
        day = int(day)
    except (TypeError, ValueError):
        day = 0
    if day not in (0, 1, 2) or day >= len(data["days"]):
        return "目前只能查询今天、明天和后天的天气。", False
    forecast = data["days"][day]
    code = data["code"] if day == 0 else forecast["code"]
    description = ui_system.WEATHER_CODES.get(code, ("天气状况未知",))[0]
    label = ("现在", "明天", "后天")[day]
    result = f"{data['name']}{label}{description}"
    if day == 0:
        result += f"，气温{data['temp']:.0f}度，体感{data['feels']:.0f}度"
    result += f"，{'今天' if day == 0 else '当天'}最高{forecast['max']:.0f}度，最低{forecast['min']:.0f}度。"
    return result, True


def call_tool(name, arguments):
    try:
        args = json.loads(arguments or "{}")
    except (TypeError, ValueError):
        args = {}
    if not isinstance(args, dict):
        args = {}
    if name == "get_current_time":
        return current_time(), True, False
    if name == "get_weather":
        try:
            result, ok = weather(args.get("city"), args.get("day", 0))
        except Exception as error:
            logging.warning("地点查询失败：%s", error)
            return "天气服务暂时不可用，请联网后再试。", False, False
        return result, ok, False
    return "不支持这个操作", False, False
