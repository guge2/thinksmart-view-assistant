import json
import logging
import os
import select
import subprocess
import time

NODE = os.getenv("THINKSMART_NODE", "/opt/node/bin/node")
SCRIPT = os.getenv("THINKSMART_AGENT_SCRIPT", "/usr/local/lib/thinksmart-agent/agent.mjs")
IDLE_TIMEOUT = 90


class Brain:
    """Pi agent child process exchanging JSON lines over stdin/stdout."""

    def __init__(self, config):
        self.config = config
        self.process = None
        self.buffer = b""
        self.next_id = 0

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def start(self):
        if self.alive():
            return
        if self.process is not None:
            logging.warning("智能体进程已退出（%s），正在重启", self.process.returncode)
        self.buffer = b""
        self.process = subprocess.Popen(
            [NODE, "--max-old-space-size=96", SCRIPT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
        )
        self.send(dict(self.config, t="config"))

    def send(self, message):
        try:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
        except (OSError, AttributeError, ValueError) as error:
            raise RuntimeError(f"agent process unavailable: {error}") from error

    def receive(self, timeout):
        while b"\n" not in self.buffer:
            ready, _, _ = select.select([self.process.stdout], [], [], timeout)
            if not ready:
                return None
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("agent process exited")
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        try:
            message = json.loads(line)
        except ValueError:
            return {}
        return message if isinstance(message, dict) else {}

    def chat(self, text, context, thinking, search, force_tool, check_cancel):
        """Yields ("delta", text) and ("tool", call, name, args); answer each tool with tool_result()."""
        self.start()
        self.next_id += 1
        request = self.next_id
        self.send({"t": "chat", "id": request, "text": text, "context": context, "thinking": thinking, "search": search, "tool": force_tool})
        finished = False
        deadline = time.monotonic() + IDLE_TIMEOUT
        try:
            while True:
                check_cancel()
                message = self.receive(0.25)
                if message is None:
                    if time.monotonic() > deadline:
                        raise RuntimeError("chat request timed out")
                    continue
                if message.get("id") != request:
                    continue
                kind = message.get("t")
                if kind == "delta":
                    yield "delta", message.get("text") or ""
                elif kind == "tool":
                    args = message.get("args")
                    yield "tool", message.get("call"), message.get("name") or "", args if isinstance(args, dict) else {}
                elif kind == "done":
                    finished = True
                    return
                elif kind == "error":
                    finished = True
                    raise RuntimeError(f"chat request failed: {message.get('message')}")
                deadline = time.monotonic() + IDLE_TIMEOUT
        finally:
            if not finished and self.alive():
                try:
                    self.send({"t": "abort"})
                except RuntimeError:
                    pass

    def tool_result(self, call, text, ok, final):
        self.send({"t": "tool_result", "call": call, "text": text, "ok": ok, "final": final})

    def note(self, user, reply):
        """Records a turn answered locally so the model keeps the conversation context."""
        if self.alive() and user and reply:
            try:
                self.send({"t": "note", "user": user, "reply": reply})
            except RuntimeError:
                pass

    def close(self):
        if self.process is None:
            return
        try:
            self.process.stdin.close()
            self.process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()
