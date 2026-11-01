import array
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
                "when": {"type": "string", "description": "闹钟或提醒的当地日期时间，格式 YYYY-MM-DDTHH:MM:SS；必须由当前日期时间计算，不能猜测。"},
                "seconds": {"type": "integer", "description": "倒计时秒数，1 到 604800。"},
                "label": {"type": "string", "description": "提醒内容或闹钟名称，最多60字。"},
                "weekdays": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 6}, "description": "重复星期，0代表周一，6代表周日；空或省略表示一次。仅闹钟可重复。"},
                "id": {"type": "string", "description": "取消时指定列表中的编号；多项时不得猜测。"}
            },
            "required": ["action"]
        }
    }
}]


def _describe(item):
    when = dt.datetime.fromtimestamp(item["due"]).strftime("%m月%d日 %H:%M")
    label = f"，{item['label']}" if item.get("label") else ""
    repeat = ""
    if item.get("weekdays"):
        repeat = "，每周" + "、".join("一二三四五六日"[day] for day in item["weekdays"])
    return f"{NAMES[item['kind']]} {item['id']}，{when}{repeat}{label}"


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
            if self.ringing_id == item["id"]:
                self.silence.set()
            return f"已取消{_describe(item)}"
        if action != "create":
            raise ValueError("不支持这个操作")
        if len(self.items) >= MAX_ITEMS:
            raise ValueError("已达到数量上限，请先取消一些")
        kind = args.get("kind")
        if kind not in NAMES:
            raise ValueError("请指定闹钟、计时器或提醒")
        now = time.time()
        if now < 1704067200:
            raise ValueError("设备时间尚未校准，请先联网同步时间")
        weekdays = args.get("weekdays") or []
        if not isinstance(weekdays, list) or any(type(d) is not int or d < 0 or d > 6 for d in weekdays):
            raise ValueError("重复星期无效")
        weekdays = sorted(set(weekdays))
        if weekdays and kind != "alarm":
            raise ValueError("只有闹钟支持按星期重复")
        if kind == "timer":
            seconds = args.get("seconds")
            if type(seconds) is not int or not 1 <= seconds <= 604800:
                raise ValueError("倒计时应为1秒至7天")
            due = now + seconds
        else:
            when = args.get("when")
            if not isinstance(when, str):
                raise ValueError("请指定完整日期和时间")
            try:
                due = dt.datetime.strptime(when, "%Y-%m-%dT%H:%M:%S").timestamp()
            except ValueError:
                raise ValueError("日期时间格式无效") from None
            if weekdays:
                due = _next_weekday(due, weekdays, max(now, due - 1))
            elif due <= now:
                raise ValueError("时间已过去，请重新指定")
        label = args.get("label") or ""
        if not isinstance(label, str) or len(label) > 60:
            raise ValueError("名称或提醒内容不能超过60字")
        if kind == "reminder" and not label.strip():
            raise ValueError("请说出需要提醒的内容")
        ident = uuid.uuid4().hex[:6]
        while any(item["id"] == ident for item in self.items):
            ident = uuid.uuid4().hex[:6]
        item = {"id": ident, "kind": kind, "due": due, "label": label.strip(), "weekdays": weekdays}
        self.items.append(item)
        try:
            self._save()
        except Exception:
            self.items.remove(item)
            raise
        if kind == "timer":
            self.monotonic_due[ident] = time.monotonic() + seconds
        result = f"已设置{_describe(item)}"
        if kind != "timer":
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
                due = next((item for item in self.items if
                            (mono >= self.monotonic_due[item["id"]] if item["id"] in self.monotonic_due
                             else now >= item["due"])), None)
                if due is None:
                    continue
                self.ringing = True
                self.ringing_id = due["id"]
                self.silence.clear()
            try:
                if self.emit:
                    self.emit("alarm", text=(due.get("label") or NAMES[due["kind"]]) + "时间到了")
                if due.get("label"):
                    self._announce(due["label"] + "时间到了")
                self._ring()
            except Exception:
                logging.exception("闹钟提示失败")
            finally:
                with self.lock:
                    self.ringing = False
                    self.ringing_id = None
                    if due in self.items and not self.stopped.is_set():
                        old_due = due["due"]
                        if due.get("weekdays"):
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
                        else:
                            self.monotonic_due.pop(due["id"], None)
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
        try:
            for _ in range(100):
                if self.silence.is_set() or self.stopped.is_set():
                    break
                process.stdin.write(samples.tobytes())
                process.stdin.flush()
        finally:
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
