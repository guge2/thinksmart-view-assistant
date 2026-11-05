import array
import calendar
import datetime as dt
import json
import logging
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid


STATE_FILE = "/data/thinksmart/music/alarms.json"
MAX_ITEMS = 64
NAMES = {"alarm": "闹钟", "timer": "计时器", "reminder": "提醒"}

TOOLS = [{
    "type": "function",
    "function": {
        "name": "manage_alarm",
        "description": "设置、查看、取消闹钟、倒计时和提醒，或停止正在响的提示音。设置成功前必须调用此工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "list", "cancel", "dismiss"]},
                "kind": {"type": "string", "enum": ["alarm", "timer", "reminder"]},
                "time": {"type": "string", "description": "当地钟点 HH:MM 或 HH:MM:SS，如早上七点传07:00。未指定日期时由设备选择下一次该钟点。"},
                "days_from_today": {"type": "integer", "minimum": 0, "maximum": 366, "description": "今天0、明天1、后天2；由设备计算具体日期，不要自行换算。"},
                "date": {"type": "string", "description": "仅当用户明确指定日历日期时传 YYYY-MM-DD；不能与days_from_today同时使用。"},
                "hours": {"type": "integer", "minimum": 0, "description": "多久以后：小时部分，可与minutes和seconds相加。"},
                "minutes": {"type": "integer", "minimum": 0, "description": "多久以后：分钟部分，可与hours和seconds相加。"},
                "repeat": {"type": "string", "enum": ["once", "daily", "workdays", "weekly", "monthly"], "description": "闹钟重复规则；weekly同时传weekdays，monthly同时传month_day。"},
                "month_day": {"type": "integer", "minimum": 1, "maximum": 31, "description": "每月几号；当月没有这一天则在月末响铃。"},
                "seconds": {"type": "integer", "minimum": 0, "description": "多久以后：秒部分。相对时长总计1秒至7天；不能与钟点、日期或重复规则混用。"},
                "label": {"type": "string", "description": "提醒内容或闹钟名称，最多60字。"},
                "weekdays": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 6}, "description": "重复星期，0代表周一，6代表周日；空或省略表示一次。仅闹钟可重复。"},
                "id": {"type": "string", "description": "取消时指定列表中的编号；多项时不得猜测。"}
            },
            "required": ["action"]
        }
    }
}]


def _describe(item, with_id=True):
    """with_id only for listings the model reads; create/cancel results are spoken verbatim."""
    when = dt.datetime.fromtimestamp(item["due"]).strftime("%m月%d日 %H:%M")
    label = f"，{item['label']}" if item.get("label") else ""
    repeat = ""
    if item.get("weekdays"):
        repeat = "，每周" + "、".join("一二三四五六日"[day] for day in item["weekdays"])
    if item.get("month_day"):
        repeat = f"，每月{item['month_day']}号（当月不足时在月末）"
    ident = f"（编号{item['id']}）" if with_id else ""
    return f"{NAMES[item['kind']]}{ident}，{when}{repeat}{label}"


def _next_weekday(due, weekdays, after):
    local = dt.datetime.fromtimestamp(due)
    # Construct each occurrence in local time so daylight-saving changes are respected.
    for offset in range(0, 15):
        date = dt.datetime.fromtimestamp(after).date() + dt.timedelta(days=offset)
        if date.weekday() in weekdays:
            candidate = dt.datetime.combine(date, local.time()).timestamp()
            if candidate > after:
                return candidate
    raise ValueError("invalid repeat schedule")


def _next_monthday(due, day, after):
    local = dt.datetime.fromtimestamp(due)
    date = dt.datetime.fromtimestamp(after)
    year, month = date.year, date.month
    for _ in range(14):
        candidate = dt.datetime(year, month, min(day, calendar.monthrange(year, month)[1]),
                                local.hour, local.minute, local.second).timestamp()
        if candidate > after:
            return candidate
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    raise ValueError("无法计算下次闹钟")


def plan_time(args, kind, now):
    rule = args.get("repeat") or "once"
    if rule not in ("once", "daily", "workdays", "weekly", "monthly"):
        raise ValueError("重复规则无效")
    weekdays = args.get("weekdays") or []
    if not isinstance(weekdays, list) or any(type(d) is not int or not 0 <= d <= 6 for d in weekdays):
        raise ValueError("重复星期无效")
    weekdays = sorted(set(weekdays))
    if rule in ("daily", "workdays"):
        if weekdays:
            raise ValueError("不能同时指定每日或工作日与重复星期")
        weekdays = list(range(7 if rule == "daily" else 5))
    if rule == "weekly" and not weekdays:
        raise ValueError("请指定每周几")
    month_day = args.get("month_day")
    if rule == "monthly":
        if type(month_day) is not int or not 1 <= month_day <= 31 or weekdays:
            raise ValueError("每月闹钟需指定1到31号，不能同时指定星期")
    elif month_day is not None:
        raise ValueError("只有每月闹钟可以指定几号")
    repeating = bool(weekdays or month_day)
    if repeating and kind != "alarm":
        raise ValueError("只有闹钟支持重复")
    duration = [args.get(k) for k in ("hours", "minutes", "seconds")]
    if any(v is not None for v in duration):
        if repeating or any(args.get(k) is not None for k in ("when", "time", "date", "days_from_today")):
            raise ValueError("多久以后不能与钟点、日期或重复规则同时使用")
        if any(v is not None and (type(v) is not int or v < 0) for v in duration):
            raise ValueError("相对时长必须是非负整数")
        seconds = sum((v or 0) * scale for v, scale in zip(duration, (3600, 60, 1)))
        if not 1 <= seconds <= 604800:
            raise ValueError("相对时长应为1秒至7天")
        return now + seconds, [], None, seconds
    if kind == "timer":
        raise ValueError("倒计时需要指定多久以后")
    if now < 1704067200:
        raise ValueError("设备时间尚未校准，请先联网同步时间")
    legacy = args.get("when")
    if legacy is not None:
        if any(args.get(k) is not None for k in ("time", "date", "days_from_today")):
            raise ValueError("完整日期时间不能与其他日期参数混用")
        due = dt.datetime.strptime(legacy, "%Y-%m-%dT%H:%M:%S").timestamp()
    else:
        clock = args.get("time")
        if not isinstance(clock, str):
            raise ValueError("请指定钟点")
        try:
            clock = dt.datetime.strptime(clock, "%H:%M:%S" if clock.count(":") == 2 else "%H:%M").time()
        except ValueError:
            raise ValueError("钟点应为HH:MM或HH:MM:SS") from None
        date, offset = args.get("date"), args.get("days_from_today")
        if date is not None and offset is not None:
            raise ValueError("日期和相对天数不能同时指定")
        if repeating and (date is not None or offset is not None):
            raise ValueError("重复闹钟只需钟点和重复规则")
        today = dt.datetime.fromtimestamp(now).date()
        if date is not None:
            day = dt.date.fromisoformat(date)
        elif offset is not None:
            if type(offset) is not int or not 0 <= offset <= 366:
                raise ValueError("相对天数应为0到366")
            day = today + dt.timedelta(days=offset)
        else:
            day = today
        due = dt.datetime.combine(day, clock).timestamp()
        if not repeating and date is None and offset is None and due <= now:
            due = dt.datetime.combine(day + dt.timedelta(days=1), clock).timestamp()
    after = max(now, due - 1) if legacy is not None else now
    if month_day:
        due = _next_monthday(due, month_day, after)
    elif weekdays:
        due = _next_weekday(due, weekdays, after)
    elif due <= now:
        raise ValueError("时间已过去，请重新指定")
    if due - now > 366 * 86400:
        raise ValueError("最多只能设置一年以内的时间")
    return due, weekdays, month_day, None


class AlarmService:
    tools = TOOLS

    def __init__(self, emit=None, state_file=STATE_FILE):
        self.emit = emit
        self.state_file = state_file
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.silence = threading.Event()
        self.thread = None
        self.ringing = False
        self.ringing_id = None
        self.monotonic_due = {}
        self.retry_after = {}
        self.items = self._load()

    def _load(self):
        try:
            with open(self.state_file, encoding="utf-8") as source:
                data = json.load(source)
            if not isinstance(data, list):
                raise ValueError("invalid alarm state")
            valid = []
            for item in data[:MAX_ITEMS]:
                if (isinstance(item, dict) and item.get("kind") in NAMES
                        and isinstance(item.get("id"), str)
                        and isinstance(item.get("due"), (int, float))
                        and math.isfinite(item["due"])
                        and (item.get("month_day") is None or (type(item["month_day"]) is int and 1 <= item["month_day"] <= 31))
                        and isinstance(item.get("weekdays", []), list)
                        and all(type(d) is int and 0 <= d <= 6 for d in item.get("weekdays", []))):
                    valid.append(item)
            return valid
        except FileNotFoundError:
            return []
        except (OSError, ValueError, TypeError) as error:
            logging.error("无法读取闹钟状态：%s", error)
            return []

    def _save(self):
        parent = os.path.dirname(self.state_file)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        temporary = self.state_file + ".tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                json.dump(self.items, target, ensure_ascii=False)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.state_file)
            try:
                directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError:
                logging.warning("闹钟文件已替换，目录同步未完成")
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="alarms", daemon=True)
            self.thread.start()

    def shutdown(self):
        self.stopped.set()
        self.silence.set()
        if self.thread:
            self.thread.join(timeout=5)

    def dismiss(self):
        with self.lock:
            if not self.ringing:
                return False
            self.silence.set()
            return True

    def call_tool(self, name, arguments):
        if name != "manage_alarm":
            return "不支持这个操作", False, False
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
            if not isinstance(args, dict):
                raise ValueError("参数格式不正确")
            with self.lock:
                message = self._command(args)
            return message, True, False
        except (ValueError, TypeError, OverflowError, OSError) as error:
            logging.warning("闹钟操作失败：%s", error)
            return f"操作失败：{error}", False, False

    def _command(self, args):
        action = args.get("action")
        if action == "dismiss":
            return "已停止提示音" if self.dismiss() else "现在没有正在响的闹钟或提醒"
        if action == "list":
            if not self.items:
                return "目前没有待响的闹钟、计时器或提醒"
            return "当前有：" + "；".join(_describe(i) for i in sorted(self.items, key=lambda i: i["due"]))
        if action == "cancel":
            ident = args.get("id")
            matches = [item for item in self.items if item["id"] == ident]
            if not matches and not ident and len(self.items) == 1:
                matches = self.items[:]
            if len(matches) != 1:
                raise ValueError("请先查看列表并指定要取消的编号")
            item = matches[0]
            self.items.remove(item)
            try:
                self._save()
            except Exception:
                self.items.append(item)
                raise
            self.monotonic_due.pop(item["id"], None)
            self.retry_after.pop(item["id"], None)
            if self.ringing_id == item["id"]:
                self.silence.set()
            return f"已取消{_describe(item, with_id=False)}"
        if action != "create":
            raise ValueError("不支持这个操作")
        if len(self.items) >= MAX_ITEMS:
            raise ValueError("已达到数量上限，请先取消一些")
        kind = args.get("kind")
        if kind not in NAMES:
            raise ValueError("请指定闹钟、计时器或提醒")
        now = time.time()
        due, weekdays, month_day, seconds = plan_time(args, kind, now)
        label = args.get("label") or ""
        if not isinstance(label, str) or len(label) > 60:
            raise ValueError("名称或提醒内容不能超过60字")
        if kind == "reminder" and not label.strip():
            raise ValueError("请说出需要提醒的内容")
        ident = uuid.uuid4().hex[:6]
        while any(item["id"] == ident for item in self.items):
            ident = uuid.uuid4().hex[:6]
        item = {"id": ident, "kind": kind, "due": due, "label": label.strip(), "weekdays": weekdays}
        if month_day:
            item["month_day"] = month_day
        self.items.append(item)
        try:
            self._save()
        except Exception:
            self.items.remove(item)
            raise
        if seconds is not None:
            self.monotonic_due[ident] = time.monotonic() + seconds
        result = f"已设置{_describe(item, with_id=False)}"
        if seconds is None:
            try:
                status = subprocess.run(
                    ["/usr/bin/timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                    capture_output=True, text=True, timeout=3, check=True,
                )
                synchronized = status.stdout.strip() == "yes"
            except (OSError, subprocess.SubprocessError):
                synchronized = False
            if not synchronized:
                result += "。设备时间尚未与网络同步，到时可能不准确"
        return result

    def _run(self):
        while not self.stopped.wait(0.5):
            with self.lock:
                now = time.time()
                mono = time.monotonic()
                due = next((item for item in sorted(self.items, key=lambda item: item["due"]) if
                            mono >= self.retry_after.get(item["id"], 0) and
                            (mono >= self.monotonic_due[item["id"]] if item["id"] in self.monotonic_due
                             else now >= item["due"])), None)
                if due is None:
                    continue
                self.ringing = True
                self.ringing_id = due["id"]
                self.silence.clear()
            delivered = False
            try:
                if self.emit:
                    self.emit("alarm", text=(due.get("label") or NAMES[due["kind"]]) + "时间到了")
                if due.get("label"):
                    self._announce(due["label"] + "时间到了")
                self._ring()
                delivered = True
            except Exception:
                logging.exception("闹钟提示失败")
            finally:
                with self.lock:
                    self.ringing = False
                    self.ringing_id = None
                    complete = delivered or self.silence.is_set()
                    if due in self.items and not self.stopped.is_set() and not complete:
                        self.retry_after[due["id"]] = time.monotonic() + 30
                        logging.warning("闹钟未完成，将在30秒后重试：%s", due["id"])
                    if due in self.items and not self.stopped.is_set() and complete:
                        old_due = due["due"]
                        if due.get("month_day"):
                            due["due"] = _next_monthday(old_due, due["month_day"], time.time())
                        elif due.get("weekdays"):
                            due["due"] = _next_weekday(old_due, due["weekdays"], time.time())
                        else:
                            self.items.remove(due)
                        try:
                            self._save()
                        except OSError:
                            logging.exception("保存闹钟状态失败")
                            if due not in self.items:
                                self.items.append(due)
                            due["due"] = old_due
                            self.retry_after[due["id"]] = time.monotonic() + 30
                        else:
                            self.monotonic_due.pop(due["id"], None)
                            self.retry_after.pop(due["id"], None)
                if self.emit:
                    self.emit("alarm_end")

    def _ring(self):
        if self.silence.is_set() or self.stopped.is_set():
            return
        rate = 24000
        samples = array.array("h")
        for i in range(int(rate * 0.6)):
            t = i / rate
            envelope = min(1.0, t / 0.015, max(0.0, (0.3 - t) / 0.025)) if t < 0.3 else 0
            samples.append(int(7500 * envelope * math.sin(2 * math.pi * 880 * t)))
        if sys.byteorder == "big":
            samples.byteswap()
        command = ["/usr/bin/pw-cat", "--playback", "--media-role", "Notification",
                   "--rate", str(rate), "--channels", "1", "--format", "s16", "--latency", "100ms", "-"]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tone = samples.tobytes()
        step = rate * 2 // 10
        chunks = [tone[offset:offset + step] for offset in range(0, len(tone), step)]
        try:
            for chunk in chunks * 100:
                if self.silence.is_set() or self.stopped.is_set():
                    break
                process.stdin.write(chunk)
                process.stdin.flush()
        finally:
            # The pipe holds over a second of tone; drain it only when not dismissed.
            if (self.silence.is_set() or self.stopped.is_set()) and process.poll() is None:
                process.terminate()
            try:
                process.stdin.close()
            except (OSError, BrokenPipeError):
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        if not (self.silence.is_set() or self.stopped.is_set()) and process.returncode != 0:
            raise RuntimeError("alarm speaker playback failed")

    def _announce(self, text):
        try:
            with tempfile.TemporaryFile() as sound:
                subprocess.run(
                    ["/usr/bin/espeak-ng", "-v", "cmn", "-s", "160", "--stdout", text],
                    stdout=sound, stderr=subprocess.DEVNULL, timeout=15, check=True,
                )
                if self.silence.is_set() or self.stopped.is_set():
                    return
                sound.seek(0)
                process = subprocess.Popen(
                    ["/usr/bin/pw-play", "--media-role", "Notification", "-"],
                    stdin=sound, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                deadline = time.monotonic() + 30
                try:
                    while process.poll() is None and time.monotonic() < deadline:
                        if self.silence.wait(0.2) or self.stopped.is_set():
                            break
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
        except (OSError, subprocess.SubprocessError):
            logging.exception("提醒内容播报失败")
