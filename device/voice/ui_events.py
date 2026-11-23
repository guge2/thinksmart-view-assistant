"""Events for the display process, which may be absent or restarted at any time."""

import json
import socket
import threading
import time

from config import UI_EVENT_SOCKET

UI_TEXT_LIMIT = 2000
# A datagram larger than this would not reach the display; the snapshot drops what it can spare.
SNAPSHOT_LIMIT = 58000


class UiPublisher:
    """Fire-and-forget datagrams to the display. It also keeps the state the events add up to, so a
    display that (re)starts can ask for a snapshot."""

    def __init__(self, path=UI_EVENT_SOCKET):
        self.path = path
        self.lock = threading.RLock()
        self.generation = str(time.monotonic_ns())
        self.sequence = 0
        self.state = {"phase": "idle", "cloud": "unknown", "mic": True,
                      "hardware_mic": None, "requested_mic": True,
                      "partial": "", "user_text": "", "answer": "",
                      "alarm_active": False, "alarm_text": "", "alarms": None, "alarm_items": []}
        try:
            # Python sockets are close-on-exec already (PEP 446).
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.sock.setblocking(False)
        except OSError:
            self.sock = None

    def emit(self, event, **fields):
        with self.lock:
            self._record(event, fields)
            self._send(event, fields, self.path)

    def record(self, event, **fields):
        """Updates the state without telling the display."""
        with self.lock:
            self._record(event, fields)

    def _record(self, event, fields):
        state = self.state
        if event in ("idle", "wake", "listen", "think", "speak"):
            state["phase"] = event
        if event == "wake":
            state.update(partial="", user_text="", answer="", error_code=None)
        elif event == "user":
            state.update(user_text=str(fields.get("text", ""))[-2000:], partial="", answer="")
        elif event == "partial":
            state["partial"] = str(fields.get("text", ""))[-2000:]
        elif event == "delta":
            state["answer"] = (state["answer"] + str(fields.get("text", "")))[-4000:]
            state["phase"] = "answer"
        elif event == "reply":
            state["answer"] = str(fields.get("text", ""))[-4000:]
        elif event == "cloud":
            state["cloud"] = fields.get("s")
        elif event == "ready":
            state.update({k: fields[k] for k in ("wake", "mic", "requested_mic", "hardware_mic") if k in fields})
        elif event == "mic":
            state.update(mic=bool(fields.get("on")), requested_mic=fields.get("requested_mic", True),
                         hardware_mic=fields.get("hardware_mic"))
        elif event in ("music", "lyric", "recipe"):
            state[event] = dict(fields)
        elif event == "mlogin":
            if fields.get("s") in ("ok", "none", "expired"):
                state["account"], state["login"] = dict(fields), None
            else:
                state["login"] = dict(fields)
        elif event == "alarm":
            state.update(alarm_active=True, alarm_text=str(fields.get("text", ""))[-2000:])
        elif event == "alarm_end":
            state.update(alarm_active=False, alarm_text="")
        elif event == "alarms":
            state["alarms"] = fields.get("next")
            if "items" in fields:
                state["alarm_items"] = fields.get("items") or []
        elif event == "error":
            state.update(error_code=fields.get("code"), error_until=time.monotonic() + 10)

    def snapshot(self, target=None):
        with self.lock:
            fields = dict(self.state)
            if len(json.dumps(fields, ensure_ascii=False).encode()) > SNAPSHOT_LIMIT:
                fields.pop("lyric", None)
                fields.pop("login", None)
            self._send("snapshot", fields, target or self.path)

    def _send(self, event, fields, target):
        self.sequence += 1
        if self.sock is None:
            return
        text = fields.get("text")
        if isinstance(text, str) and len(text) > UI_TEXT_LIMIT:
            fields["text"] = text[-UI_TEXT_LIMIT:]
        fields["e"] = event
        fields["generation"] = self.generation
        fields["event_seq"] = self.sequence
        try:
            payload = json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.sock.sendto(payload, target)
        except (OSError, TypeError, ValueError):
            pass
