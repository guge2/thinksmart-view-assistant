import array
import calendar
import datetime as dt
import json
import logging
import math
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
import uuid


STATE_FILE = "/data/thinksmart/music/alarms.json"
MAX_ITEMS = 64
NAMES = {"alarm": "闹钟", "timer": "计时器", "reminder": "提醒"}
# Alarms wake with music, which plays this long unless stopped; timers and reminders beep.
MUSIC_SECONDS = 300

TOOLS = [{
    "type": "function",
    "function": {
        "name": "manage_alarm",
        "description": "设置、查看、取消闹钟、倒计时和提醒，或停止、延后正在响的提示音。设置成功前必须调用此工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "list", "cancel", "dismiss", "snooze"]},
                "kind": {"type": "string", "enum": ["alarm", "timer", "reminder"]},
                "time": {"type": "string", "description": "当地钟点 HH:MM 或 HH:MM:SS，如早上七点传07:00。未指定日期时由设备选择下一次该钟点。"},
                "days_from_today": {"type": "integer", "minimum": 0, "maximum": 366, "description": "今天0、明天1、后天2；由设备计算具体日期，不要自行换算。"},
                "date": {"type": "string", "description": "仅当用户明确指定日历日期时传 YYYY-MM-DD；不能与days_from_today同时使用。"},
                "hours": {"type": "integer", "minimum": 0, "description": "多久以后：小时部分，可与minutes和seconds相加。"},
                "minutes": {"type": "integer", "minimum": 0, "description": "多久以后：分钟部分，可与hours和seconds相加。"},
                "repeat": {"type": "string", "enum": ["once", "daily", "workdays", "weekly", "monthly"], "description": "闹钟重复规则；weekly同时传weekdays，monthly同时传month_day。"},
                "month_day": {"type": "integer", "minimum": 0, "maximum": 31, "description": "仅当repeat为monthly时传1到31号；其他情况填0。"},
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
    month_day = args.get("month_day") or None
    if rule == "monthly":
        if type(month_day) is not int or not 1 <= month_day <= 31 or weekdays:
            raise ValueError("每月闹钟需指定1到31号，不能同时指定星期")
    elif args.get("repeat") in ("once", "daily", "workdays", "weekly"):
        month_day = None
    elif month_day is not None:
        raise ValueError("只有每月闹钟可以指定几号")
    repeating = bool(weekdays or month_day)
    if repeating and kind != "alarm":
        raise ValueError("只有闹钟支持重复")
    legacy = (args.get("when").strip() or None) if isinstance(args.get("when"), str) else args.get("when")
    clock_str = (args.get("time").strip() or None) if isinstance(args.get("time"), str) else args.get("time")
    date = (args.get("date").strip() or None) if isinstance(args.get("date"), str) else args.get("date")
    duration = [args.get(k) for k in ("hours", "minutes", "seconds")]
    if any(v is not None and (type(v) is not int or v < 0) for v in duration):
        raise ValueError("相对时长必须是非负整数")
    seconds = sum((v or 0) * scale for v, scale in zip(duration, (3600, 60, 1)))
    if seconds > 0 or (clock_str is None and legacy is None and any(v is not None for v in duration)):
        if repeating or any(v is not None for v in (legacy, clock_str, date)) or (args.get("days_from_today") not in (None, 0)):
            raise ValueError("多久以后不能与钟点、日期或重复规则同时使用")
        if not 1 <= seconds <= 604800:
            raise ValueError("相对时长应为1秒至7天")
        return now + seconds, [], None, seconds
    if kind == "timer":
        raise ValueError("倒计时需要指定多久以后")
    if now < 1704067200:
        raise ValueError("设备时间尚未校准，请先联网同步时间")
    if legacy is not None:
        if any(v is not None for v in (clock_str, date)) or (args.get("days_from_today") not in (None, 0)):
            raise ValueError("完整日期时间不能与其他日期参数混用")
        due = dt.datetime.strptime(legacy, "%Y-%m-%dT%H:%M:%S").timestamp()
    else:
        clock = clock_str
        if not isinstance(clock, str):
            raise ValueError("请指定钟点")
        try:
            clock = dt.datetime.strptime(clock, "%H:%M:%S" if clock.count(":") == 2 else "%H:%M").time()
        except ValueError:
            raise ValueError("钟点应为HH:MM或HH:MM:SS") from None
        offset = args.get("days_from_today")
        if date is not None and offset == 0:
            offset = None
        if repeating and offset == 0 and date is None:
            offset = None
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
        if not repeating and date is None and offset in (None, 0) and due <= now:
            due = dt.datetime.combine(today + dt.timedelta(days=1), clock).timestamp()
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

    def __init__(self, emit=None, state_file=STATE_FILE, on_ring=None, music=None):
        self.emit = emit
        self.state_file = state_file
        self.on_ring = on_ring
        self.music = music
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.silence = threading.Event()
        self.thread = None
        self.ringing = False
        self.ringing_id = None
        self.last_rang = None
        self.monotonic_due = {}
        self.retry_after = {}
        self.items = self._load()
        self._prune_stale(time.time())

    def next_summary(self):
        with self.lock:
            if not self.items:
                return None
            earliest = min(self.items, key=lambda item: item["due"])
            when = dt.datetime.fromtimestamp(earliest["due"]).strftime("%H:%M")
            return {"kind": earliest["kind"], "due": round(earliest["due"], 1),
                    "time": when, "label": earliest.get("label") or ""}

    def list_items(self):
        with self.lock:
            result = []
            for item in sorted(self.items, key=lambda i: i["due"])[:16]:
                when = dt.datetime.fromtimestamp(item["due"]).strftime("%H:%M")
                repeat = ""
                if item.get("weekdays"):
                    days = item["weekdays"]
                    if days == [0, 1, 2, 3, 4, 5, 6]:
                        repeat = "每天"
                    elif days == [0, 1, 2, 3, 4]:
                        repeat = "工作日"
                    else:
                        repeat = "周" + "、".join("一二三四五六日"[d] for d in days)
                elif item.get("month_day"):
                    repeat = f"每月{item['month_day']}号"
                result.append({
                    "id": item["id"],
                    "kind": item["kind"],
                    "due": round(item["due"], 1),
                    "time": when,
                    "label": item.get("label") or "",
                    "repeat": repeat,
                })
            return result

    def announce(self):
        if self.emit:
            self.emit("alarms", next=self.next_summary(), items=self.list_items())

    def _prune_stale(self, now):
        if now < 1704067200:
            return
        changed = False
        for item in list(self.items):
            if item["id"] in self.monotonic_due or item["due"] >= now - 600:
                continue
            if item.get("month_day"):
                item["due"] = _next_monthday(item["due"], item["month_day"], now)
                changed = True
            elif item.get("weekdays"):
                item["due"] = _next_weekday(item["due"], item["weekdays"], now)
                changed = True
            else:
                logging.info("跳过断电期间过期的单次闹钟或提醒：%s", item["id"])
                self.items.remove(item)
                changed = True
        if changed:
            try:
                self._save()
            except OSError:
                logging.warning("清理过期闹钟后保存失败")

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

    def snooze(self, minutes=5):
        minutes = max(1, min(60, int(minutes or 5)))
        with self.lock:
            now_mono = time.monotonic()
            source = None
            if self.ringing and self.ringing_id:
                source = next((i for i in self.items if i["id"] == self.ringing_id), None)
            if source is None and self.last_rang and now_mono - self.last_rang[1] < 60:
                source = self.last_rang[0]
            if source is None and not self.ringing:
                return None
            seconds = minutes * 60
            now = time.time()
            ident = uuid.uuid4().hex[:6]
            while any(item["id"] == ident for item in self.items):
                ident = uuid.uuid4().hex[:6]
            kind = source["kind"] if source else "alarm"
            label = (source.get("label") or "") if source else ""
            item = {"id": ident, "kind": kind, "due": now + seconds, "label": label, "weekdays": []}
            self.items.append(item)
            self.monotonic_due[ident] = now_mono + seconds
            self.last_rang = None
            if self.ringing:
                self.silence.set()
            try:
                self._save()
            except OSError:
                logging.warning("保存贪睡闹钟失败")
        self.announce()
        return f"好的，{minutes}分钟后再提醒"

    def ui_command(self, action, **kwargs):
        with self.lock:
            if action == "snooze":
                return self.snooze(kwargs.get("minutes") or 5)
            if action == "cancel":
                ident = kwargs.get("id")
                if not ident:
                    return None
                return self._command({"action": "cancel", "id": str(ident)})
            if action == "timer":
                minutes = int(kwargs.get("minutes") or 5)
                label = str(kwargs.get("label") or "").strip()[:60]
                return self._command({"action": "create", "kind": "timer", "minutes": minutes, "label": label})
        return None

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
        if action == "snooze":
            minutes = args.get("minutes")
            reply = self.snooze(minutes if type(minutes) is int and 1 <= minutes <= 60 else 5)
            return reply or "现在没有正在响的闹钟或提醒"
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
            self.announce()
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
        self.announce()
        result = f"已设置{_describe(item, with_id=False)}"
        if seconds is None:
            synchronized = os.path.exists("/run/systemd/timesync/synchronized")
            if not synchronized:
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
                self._prune_stale(now)
                mono = time.monotonic()
                due = next((item for item in sorted(self.items, key=lambda item: item["due"]) if
                            mono >= self.retry_after.get(item["id"], 0) and
                            (mono >= self.monotonic_due[item["id"]] if item["id"] in self.monotonic_due
                             else now >= item["due"])), None)
                if due is None:
                    continue
                self.ringing = True
                self.ringing_id = due["id"]
                self.last_rang = (dict(due), mono)
                self.silence.clear()
            delivered = False
            logging.info("%s响铃：%s", NAMES[due["kind"]], due.get("label") or due["id"])
            if self.on_ring:
                try:
                    self.on_ring(True)
                except Exception:
                    pass
            try:
                if self.emit:
                    self.emit("alarm", text=(due.get("label") or NAMES[due["kind"]]) + "时间到了")
                if due["kind"] == "alarm" and self.music is not None:
                    if due.get("label"):
                        self._announce(due["label"] + "时间到了")
                    if not self._ring_music():
                        self._ring(cycles=100)
                elif due.get("label"):
                    for _ in range(3):
                        if self.silence.is_set() or self.stopped.is_set():
                            break
                        self._announce(due["label"] + "时间到了")
                        self._ring(cycles=30)
                else:
                    self._ring(cycles=100)
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
                self.announce()
                if self.emit:
                    self.emit("alarm_end")
                if self.on_ring:
                    try:
                        self.on_ring(False)
                    except Exception:
                        pass

    def _ring_music(self):
        """False when no music could start; the caller beeps instead."""
        def cancelled():
            return self.silence.is_set() or self.stopped.is_set()

        if cancelled() or not self.music.play_alarm(cancelled):
            return False
        player = self.music.player
        deadline = time.monotonic() + MUSIC_SECONDS
        position, moved = player.position, time.monotonic()
        try:
            while not self.silence.wait(0.5) and not self.stopped.is_set() and time.monotonic() < deadline:
                if player.state not in ("loading", "playing"):
                    break
                # Between songs the player loads the next one; its own retries bound that.
                if player.state == "loading" or player.position != position:
                    position, moved = player.position, time.monotonic()
                elif time.monotonic() - moved > 5:
                    # A stalled audio graph: give up, so audio can be rebuilt and the alarm retried.
                    raise RuntimeError("alarm music stalled")
        finally:
            player.stop()
        return True

    def _ring(self, cycles=100):
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
        # Chunks under PIPE_BUF never block once select reports the pipe writable.
        step = rate * 2 // 20
        chunks = [tone[offset:offset + step] for offset in range(0, len(tone), step)]
        try:
            for chunk in chunks * cycles:
                if self.silence.is_set() or self.stopped.is_set():
                    break
                # A stalled audio graph stops draining the pipe. Give up, so the assistant
                # can rebuild the audio chain, and ring again on the retry.
                if not select.select([], [process.stdin], [], 3)[1]:
                    raise RuntimeError("alarm speaker stalled")
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
