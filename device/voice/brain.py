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
        profile_fields = ('apiKey', 'api', 'provider', 'baseUrl', 'maxTokens', 'contextWindow')
        self.profiles = {config['provider']: {key: config.get(key) for key in profile_fields}}
        expert = config.get('expert', {})
        if expert.get('provider') == 'openai' and expert.get('apiKey') and 'openai' not in self.profiles:
            self.profiles['openai'] = {key: expert.get(key) for key in profile_fields}
            self.profiles['openai'].update(maxTokens=4096, api='openai-responses')
        self.provider_order = config.get('providerOrder', [])
        self.provider_order_model = config.get('providerOrderModel', config['model'])
        if config['model'] != self.provider_order_model:
            self.config['providerOrder'] = []
        self.process = None
        self.buffer = b""
        self.next_id = 0
        self.request = None

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

    def configure_models(self, model, expert_model, provider):
        if provider not in self.profiles:
            raise ValueError('所选服务尚未配置日常对话凭据')
        self.config.update(self.profiles[provider])
        self.config['model'] = model
        self.config['expert']['model'] = expert_model
        # Provider preferences belong to the original model; other models choose their own route.
        self.config['providerOrder'] = self.provider_order if model == self.provider_order_model else []
        if self.alive():
            self.send(dict(self.config, t='models'))

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

    def chat(self, text, context, thinking, search, force_tool, check_cancel, expert=False):
        """Yields ("delta", text) and ("tool", call, name, args); answer each tool with tool_result()."""
        self.start()
        self.next_id += 1
        request = self.next_id
        self.request = request
        self.send({"t": "chat", "id": request, "text": text, "context": context, "thinking": thinking, "search": search, "tool": force_tool, "expert": expert})
        finished = False
        timeout = 270 if expert else IDLE_TIMEOUT
        deadline = time.monotonic() + timeout
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
                deadline = time.monotonic() + timeout
        finally:
            if not finished and self.alive():
                try:
                    self.send({"t": "abort", "id": request})
                except RuntimeError:
                    pass

    def tool_result(self, call, text, ok, final):
        self.send({"t": "tool_result", "call": call, "text": text, "ok": ok, "final": final})

    def heard(self, text, interrupted=False):
        if self.request is not None and self.alive():
            self.send({"t": "heard", "id": self.request, "text": text, "interrupted": interrupted})
        self.request = None

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
