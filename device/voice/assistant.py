#!/usr/bin/python3
import array
import datetime
import io
import json
import logging
import math
import os
import queue
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from collections import deque
from itertools import chain

import dashscope
import sherpa_onnx

from brain import Brain
from playback import PCMPlayer, SpeechProgress, AudioStalled
from music import MUSIC_ROLE, MusicService, match_command, CommandCancelled as TurnCancelled
import information
import offline
from alarms import AlarmService


SAMPLE_RATE = 16000
FRAME_BYTES = 1280  # 40 ms of 16 kHz s16 mono
PLAYBACK_RATE = 22050
VOICE_ROOT = "/data/thinksmart/voice"
MODEL_NAME = "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
KWS_MODEL = f"{VOICE_ROOT}/models/{MODEL_NAME}"
KEYWORDS_FILE = f"{VOICE_ROOT}/keywords.txt"
WAKE_PHRASE_FILE = "/etc/thinksmart-voice/wake-phrase"
UI_EVENT_SOCKET = "/run/thinksmart-ui/events.sock"
CONTROL_SOCKET = "/run/thinksmart-voice/control.sock"
ECHO_CANCEL_SOURCE = "thinksmart.echo-cancel-source"
RAW_CAPTURE_SOURCE = "alsa_input.platform-c051000.sound-card.HiFi__hw_cd18781y_1__source"
UI_SETTINGS_FILE = "/data/thinksmart/ui/settings.json"
MIC_STATE_FILE = "/run/thinksmart-keys/mic-state"
MODEL_OVERRIDES = "/etc/thinksmart-voice/gateway.conf"
MODEL_ENV = {"chat_model": "THINKSMART_CHAT_MODEL", "chat_provider": "THINKSMART_CHAT_PROVIDER", "expert_model": "THINKSMART_EXPERT_MODEL",
             "tts_model": "THINKSMART_TTS_MODEL", "tts_voice": "THINKSMART_TTS_VOICE"}
UI_TEXT_LIMIT = 2000
ASSISTANT_ROLE = "Assistant"
FAREWELL_WORDS = re.compile(r"退下|没事|不用|再见|拜拜|休息|去休息|别说|不聊|结束对话|就这样")
SLEEP_WORDS = re.compile(r"关机|关屏|关闭屏幕|关掉屏幕|息屏|熄屏|睡觉|去睡觉|晚安")
# Sleep words that also stop music; screen-only words leave playback running.
POWER_OFF_WORDS = re.compile(r"关机|睡觉|晚安")
QUESTION_END = re.compile(r"(?:[？?]|[吗呢么][。！!～~]?)[\s”」』\"')）]*$")
# Without thinking the model often answers weather from web search or memory, so weather turns force
# the tool and skip search; time is already in the context, so time turns only skip search.
WEATHER_WORDS = re.compile(
    r"天气|气温|天气预报|下雪|降雨|带伞|刮风|穿什么"
    r"|(?:今[天日]|明[天日]|后天|外面|室外|出门|[早晚]上|这[里边]|本地|当地).{0,6}(?:几度|多少度|温度|下雨|冷不冷|热不热)"
    r"|^(?:现在|今天|明天|外面)?(?:多少度|几度|冷不冷|热不热)[？?吗呢吧呀啊]*$"
)
TIME_WORDS = re.compile(r"几点|几号|星期几|礼拜几")
# Thinking delays the first spoken word, so it is reserved for calculation, comparison and advice.
THINK_WORDS = re.compile(
    r"计算|算一下|算算|算出|等于多少|换算|折合|平均|百分之|几倍|多少倍|乘以|除以|加上|减去|\d\s*[+\-*/×÷^]\s*\d"
    r"|比较|对比|相比|区别|差别|差异|优缺点|利弊|哪个(?:好|更|划算|值)|哪种|还是.{0,12}(?:好|划算|合适)"
    r"|建议|推荐(?!.{0,6}(?:歌|曲|音乐))|怎么办|该不该|应不应该|值不值|值得|规划|计划|方案|攻略|分析"
    r"|帮我(?:想|选|安排)|怎么选|如何选"
)
EXPERT_REQUEST = re.compile(r"(?:用|请|让|交给|切换到?)\s*(?:openai|gpt|sol|6[.点]1|六点一)", re.I)
HARD_TASK_WORDS = re.compile(
    r"深入分析|详细分析|仔细分析|帮我分析|推导|证明|算法|调试|排查|代码|编程|架构"
    r"|数学题|逻辑题|复杂|多步骤|详细规划|可行性|成本收益"
)
END_TOOL = {
    "type": "function",
    "function": {
        "name": "end_conversation",
        "description": "用户想结束对话时调用，例如让你退下、说没事了、不用了、你去忙吧。调用后不再继续聆听。",
        "parameters": {
            "type": "object",
            "properties": {
                "screen_off": {"type": "boolean", "description": "用户要求关机、关屏或睡觉时为true"},
                "stop_music": {"type": "boolean", "description": "用户要求关机、睡觉或说晚安时为true，同时停止播放音乐；只关屏时为false"},
            },
        },
    },
}
TOOL_RULES = (
    "你可以用工具播放QQ音乐、控制播放和调节音量，只在用户明确提出时调用。"
    "询问当前时间、日期或天气时必须使用对应工具，不得猜测实时信息。"
    "设置、查看、取消闹钟、计时器、提醒时必须调用manage_alarm。"
    "设置闹钟时传钟点time和相对天数days_from_today（明天1、后天2），由设备计算日期。"
    "多久以后传hours/minutes/seconds；重复传repeat规则；不要自行计算完整日期，不得假装已设置。"
    "取消时根据用户的描述匹配编号；不明确时先询问。编号仅供取消时传参，不要读给用户。"
    "使用工具时不要提前声称操作成功，以工具返回结果为准。"
    "用户想结束对话时调用end_conversation；说关机、关屏或睡觉时同时把screen_off设为true，说关机、睡觉或晚安时还要把stop_music设为true，你无法关闭设备电源。"
    "新闻、比赛、行情等需要最新信息的问题可以参考联网搜索结果，用一两句话概括，不要念出网址。"
    "回答结束时不要说“还有什么需要”之类的客套追问；只有确实需要用户补充信息时才用问句结尾。"
)


LINK = re.compile(r"\[([^\]\n]*)\]\((?:[^()\s]|\([^()\s]*\))*\)")
# A parenthesised aside holding a link is a source note, e.g. "([site.com](https://...))".
LINK_ASIDE = re.compile(r"[（(][^（()）\n]*?" + LINK.pattern + r"[^（()）\n]*?[)）]")
URL = re.compile(r"(?:https?://|www\.)[^\s，。！？；、）)】\]]*")
DOMAIN = re.compile(r"[\w.-]+\.[A-Za-z]{2,}(?:/\S*)?")
CITATION = re.compile(r"\ue200.*?\ue201|[\ue000-\uf8ff]|\[\^?\d+\]|【\d+[^】]*】")
EMPTY_ASIDE = re.compile(r"[（(]\s*(?:(?:来源|参考|出处|引自)[:：]?)?[\s,，、;；]*[)）]")
MARKDOWN = re.compile(r"\*\*|__|`|(?<=\n)[ \t]*(?:#+|>)[ \t]*")
URL_START = re.compile(r"(?:https?:/{0,2}|https?|htt|ht|h|www\.?|ww|w)$")
MARKDOWN_START = re.compile(r"(?:\n[ \t]*[#>]*[ \t]*|[*_]+)$")


def spoken_text(text, line_start=True):
    """Removes links, URLs and citation marks that web search adds to replies."""
    if line_start:
        return spoken_text("\n" + text, False)[1:]
    text = LINK_ASIDE.sub("", text)
    text = LINK.sub(lambda m: "" if not m.group(1).strip() or DOMAIN.fullmatch(m.group(1).strip()) else m.group(1), text)
    text = CITATION.sub("", URL.sub("", text))
    text = re.sub(r"[（(]\s*" + DOMAIN.pattern + r"\s*[)）]", "", text)
    return MARKDOWN.sub("", EMPTY_ASIDE.sub("", text))


class SpokenStream:
    """Holds back a possibly unfinished link or URL until it can be removed whole."""
    HOLD = 400

    def __init__(self):
        self.pending = ""
        self.line_start = True

    def feed(self, text):
        self.pending += text
        cut = self._safe_end(self.pending)
        out, self.pending = self.pending[:cut], self.pending[cut:]
        return self._emit(out)

    def flush(self):
        out, self.pending = self.pending, ""
        return self._emit(out)

    def _emit(self, text):
        if not text:
            return ""
        cleaned = spoken_text(text, self.line_start)
        self.line_start = text.endswith("\n")
        return cleaned

    def _safe_end(self, text):
        if len(text) > self.HOLD:
            return len(text)
        stack, hold = [], len(text)
        pairs = {"]": "[", ")": "(", "）": "(", "】": "【", "\ue201": "\ue200"}
        label = None  # (start, end) of the last "[text]", which a "(url)" may follow
        for index, char in enumerate(text):
            opener = "(" if char == "（" else char
            if opener in ("[", "(", "【", "\ue200"):
                linked = opener == "(" and label is not None and label[1] == index - 1
                stack.append((opener, label[0] if linked else index))
            elif char in pairs and stack and stack[-1][0] == pairs[char]:
                start = stack.pop()[1]
                if char == "]":
                    label = (start, index)
                    if index == len(text) - 1:
                        hold = min(hold, start)
        if stack:
            hold = min(hold, stack[0][1])
        url = URL.search(text)
        while url and url.end() != len(text):
            url = URL.search(text, url.end())
        if url:
            hold = min(hold, url.start())
        partial = URL_START.search(text)
        if partial:
            hold = min(hold, partial.start())
        prefix = "\n" if self.line_start else ""
        partial = MARKDOWN_START.search(prefix + text)
        if partial:
            hold = min(hold, max(0, partial.start() - len(prefix)))
        return hold


class WakeStream:
    def __init__(self, spotter):
        self.spotter = spotter
        self.stream = spotter.create_stream()

    def feed(self, samples, gain=1.0):
        # Feed silence too: dropping quiet blocks reduced detections on the
        # device's own known wake-word recording.
        if gain != 1.0:
            samples = [max(-1.0, min(1.0, v * gain)) for v in samples]
        self.stream.accept_waveform(SAMPLE_RATE, samples)
        while self.spotter.is_ready(self.stream):
            self.spotter.decode_stream(self.stream)
            result = self.spotter.get_result(self.stream)
            if result:
                self.spotter.reset_stream(self.stream)
                return result
        return ""


class WakeDetector:
    """Keep AEC recognition and a separate, continuous raw-mic stream."""

    def __init__(self, assistant):
        self.assistant = assistant
        self.spotter = assistant.keyword_spotter
        self.stream = WakeStream(self.spotter)
        self.raw_capture = None
        self.raw_stream = None
        self.last_hit = 0.0
        self.raw_buffer = bytearray()
        self.raw_retry_at = 0.0
        self.raw_started = 0.0
        self.raw_enabled = True

    def __enter__(self):
        self.open_raw()
        return self

    def __exit__(self, *_):
        self.close_raw()

    def open_raw(self):
        if self.raw_capture is None and time.monotonic() >= self.raw_retry_at:
            try:
                self.raw_capture = self.assistant.create_capture(RAW_CAPTURE_SOURCE)
                os.set_blocking(self.raw_capture.stdout.fileno(), False)
                self.raw_stream = WakeStream(self.spotter)
                self.raw_started = time.monotonic()
            except Exception as error:
                logging.warning("启动原始麦克风流失败: %s", error)
                self.close_raw()

    def close_raw(self):
        capture, self.raw_capture = self.raw_capture, None
        self.raw_stream = None
        self.raw_buffer.clear()
        self.raw_retry_at = time.monotonic() + 5
        if capture is not None:
            threading.Thread(target=self.assistant.stop_process, args=(capture,),
                             name="raw-capture-close", daemon=True).start()

    def read_raw(self, allowed):
        capture = self.raw_capture
        if capture is None:
            return ""
        result = ""
        try:
            received = 0
            while received < 128 * 1024:
                try:
                    data = os.read(capture.stdout.fileno(), 16384)
                except BlockingIOError:
                    break
                if not data:
                    raise RuntimeError("raw microphone stream ended")
                self.raw_buffer.extend(data)
                received += len(data)
            if received:
                self.assistant.raw_progress = time.monotonic()
            elif time.monotonic() - max(self.raw_started, self.assistant.raw_progress) > 2:
                raise RuntimeError("raw microphone stream timed out")
            if not allowed:
                self.raw_buffer.clear()
                if self.raw_enabled:
                    self.raw_stream = WakeStream(self.spotter)
                self.raw_enabled = False
                return ""
            self.raw_enabled = True
            limit = FRAME_BYTES * 8
            if len(self.raw_buffer) > limit:
                drop = (len(self.raw_buffer) - limit + FRAME_BYTES - 1) // FRAME_BYTES * FRAME_BYTES
                del self.raw_buffer[:drop]
                self.raw_stream = WakeStream(self.spotter)
            while len(self.raw_buffer) >= FRAME_BYTES:
                frame = bytes(self.raw_buffer[:FRAME_BYTES])
                del self.raw_buffer[:FRAME_BYTES]
                self.assistant.capture_levels["raw"] = self.assistant.frame_level(frame)
                detected = self.decode(self.raw_stream, frame, gain=4.0)
                result = result or detected
            return result
        except (OSError, RuntimeError, ValueError) as error:
            logging.warning("读取原始麦克风流失败: %s", error)
            self.close_raw()
            return ""

    def decode(self, stream, data, gain=1.0):
        samples = self.assistant.pcm_to_floats(data)
        return stream.feed(samples, gain)

    def feed(self, data, is_playing=False, raw_wake_allowed=True):
        if self.raw_capture is None:
            self.open_raw()

        raw_result = self.read_raw(not is_playing or raw_wake_allowed)

        result = self.decode(self.stream, data, gain=2.0)
        now = time.monotonic()
        if not (result or raw_result) or now - self.last_hit < 2.0:
            return "", ""
        self.last_hit = now
        if not is_playing:
            return (raw_result, "raw") if raw_result else (result, "AEC")
        return (result, "AEC") if result else (raw_result, "raw")


def match_farewell(text, wake_phrase):
    """Returns "sleep", "end" or None for short phrases that close the conversation."""
    normalized = re.sub(r"[\s，。！？、,.!?~～…]+", "", text or "")
    if wake_phrase:
        normalized = normalized.replace(wake_phrase, "")
    normalized = re.sub(r"^(?:好了|好的|行了|那|嗯|你|请)+", "", normalized)
    normalized = re.sub(r"(?:吧|啦|了|呀|哦|啊)+$", "", re.sub(r"(?:谢谢你?|谢了)$", "", normalized))
    if SLEEP_WORDS.fullmatch(normalized):
        return "sleep"
    if FAREWELL_WORDS.fullmatch(normalized):
        return "end"
    return None


class UiPublisher:
    """Fire-and-forget datagrams to the display; the display may be absent."""

    def __init__(self, path=UI_EVENT_SOCKET):
        self.path = path
        self.lock = threading.RLock()
        self.generation = str(time.monotonic_ns())
        self.sequence = 0
        self.state = {"phase": "idle", "cloud": "unknown", "mic": True,
                      "hardware_mic": None, "requested_mic": True,
                      "partial": "", "user_text": "", "answer": "",
                      "alarm_active": False, "alarm_text": "", "alarms": None}
        try:
            self.sock = socket.socket(
                socket.AF_UNIX,
                socket.SOCK_DGRAM | socket.SOCK_CLOEXEC,
            )
            self.sock.setblocking(False)
        except OSError:
            self.sock = None

    def emit(self, event, **fields):
        with self.lock:
            self._record(event, fields)
            self._send(event, fields, self.path)

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
        elif event in ("music", "lyric"):
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
        elif event == "error":
            state.update(error_code=fields.get("code"), error_until=time.monotonic() + 10)

    def snapshot(self, target=None):
        with self.lock:
            fields = dict(self.state)
            if len(json.dumps(fields, ensure_ascii=False).encode()) > 58000:
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
            payload = json.dumps(
                fields,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.sock.sendto(payload, target)
        except (OSError, TypeError, ValueError):
            pass


def classify_error(error):
    parts = []
    current = error
    while current is not None and len(parts) < 4:
        parts.append(f"{type(current).__name__} {current}")
        current = current.__cause__ or current.__context__
    text = " ".join(parts).lower()
    if "microphone" in text:
        return "mic"
    if "speaker" in text or "pipewire-pulse" in text:
        return "speaker"
    if "credential" in text:
        return "config"
    if any(
        marker in text
        for marker in (
            "http 401",
            "http 403",
            "unauthorized",
            "invalidapikey",
            "invalid api",
            "accessdenied",
            "access denied",
            "forbidden",
        )
    ):
        return "auth"
    if any(
        marker in text
        for marker in (
            "urlerror",
            "timed out",
            "timeout",
            "connection",
            "network",
            "name resolution",
            "temporary failure",
            "unreachable",
            "ssl",
            "gaierror",
        )
    ):
        return "network"
    if "empty reply" in text:
        return "llm"
    return "service"


class Ducker:
    """Lowers other playback streams while a conversation is active."""

    def __init__(self, level):
        self.level = level
        self.saved = {}
        self.lock = threading.Lock()

    @staticmethod
    def get_volume(node_id):
        try:
            result = subprocess.run(
                ["/usr/bin/wpctl", "get-volume", str(node_id)],
                capture_output=True, text=True, timeout=3, check=True,
            )
            return float(result.stdout.split()[1])
        except (OSError, subprocess.SubprocessError, IndexError, ValueError):
            return None

    @staticmethod
    def set_volume(node_id, volume):
        try:
            subprocess.run(
                ["/usr/bin/wpctl", "set-volume", str(node_id), f"{volume:.3f}"],
                capture_output=True, timeout=3, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def streams(self):
        try:
            result = subprocess.run(
                ["/usr/bin/pw-dump"], capture_output=True, timeout=5, check=True,
            )
            # pw-dump may append update batches after the first array.
            objects, _end = json.JSONDecoder().raw_decode(result.stdout.decode("utf-8"))
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError, ValueError) as error:
            logging.warning("无法列出播放流：%s", error)
            return []
        found = []
        for item in objects:
            if item.get("type") != "PipeWire:Interface:Node":
                continue
            props = (item.get("info") or {}).get("props") or {}
            # Loopback, filter-chain and echo-cancel internals carry a link
            # group; ducking them would also duck the assistant's own voice.
            if (
                props.get("media.class") == "Stream/Output/Audio"
                and props.get("media.role") not in (ASSISTANT_ROLE, MUSIC_ROLE)
                and "node.link-group" not in props
            ):
                found.append(item["id"])
        return found

    def duck(self):
        if self.level >= 1:
            return
        with self.lock:
            for node_id in self.streams():
                if node_id in self.saved:
                    continue
                volume = self.get_volume(node_id)
                if volume is None:
                    continue
                self.saved[node_id] = volume
                self.set_volume(node_id, volume * self.level)
            if self.saved:
                logging.info("已压低 %d 路其他播放", len(self.saved))

    def restore(self):
        with self.lock:
            if not self.saved:
                return
            saved, self.saved = self.saved, {}
            for step in (0.35, 0.7, 1.0):
                for node_id, volume in saved.items():
                    self.set_volume(node_id, volume * (self.level + (1 - self.level) * step))
                time.sleep(0.12)

class ThinkSmartAssistant:
    def __init__(self, ui):
        self.ui = ui
        credentials = self.load_credentials()
        self.api_key = credentials["api_key"]
        self.workspace = credentials["workspace_id"]
        self.region = credentials.get("region", "cn-beijing")
        if self.region != "cn-beijing":
            raise RuntimeError("only cn-beijing is configured")
        if not re.fullmatch(r"[A-Za-z0-9-]{3,128}", self.workspace):
            raise RuntimeError("invalid workspace ID")

        self.client = dashscope.Client(
            api_key=self.api_key,
            workspace=self.workspace,
            region=self.region,
            config_path="/dev/null",
        )
        self.asr_model = os.getenv(
            "THINKSMART_ASR_MODEL",
            "qwen-audio-3.1-asr-flash-message",
        )
        chat = self.load_chat_config()
        expert = self.load_chat_config("expert.json")
        self.expert_model = expert.get("model") if expert.get("api_key") else None
        self.expert_provider = expert.get("provider", "openai") if self.expert_model else None
        self.chat_provider = chat.get("provider", "bailian")
        self.chat_model = chat.get("model") or os.getenv("THINKSMART_CHAT_MODEL", "qwen3.8-flash")
        self.tts_model = os.getenv(
            "THINKSMART_TTS_MODEL",
            "qwen-audio-3.1-tts-flash",
        )
        self.tts_voice = os.getenv("THINKSMART_TTS_VOICE", "longxiaocheng_v3.1")
        self.model_defaults = {key: getattr(self, key) for key in MODEL_ENV}
        for key, value in self.model_overrides().items():
            setattr(self, key, value)
        self.turn_models = {}
        self.preview_lock = threading.Lock()
        self.system_prompt = os.getenv(
            "THINKSMART_SYSTEM_PROMPT",
            "你是运行在联想ThinkSmart独立设备上的语音助手。"
            "请用简洁、自然的中文回答，通常不超过两句话；不要使用Markdown或表情符号。"
            "不知道的事情就直接说明，不要编造。",
        )
        self.speech_rate = self.clamp_rate(os.getenv("THINKSMART_TTS_RATE", "1.5"))
        self.thinking = os.getenv("THINKSMART_THINKING", "1") != "0"
        self.night = {"enabled": False, "start": 1320, "end": 420}
        self.load_ui_prefs()
        self.thinking_budget = int(os.getenv("THINKSMART_THINKING_BUDGET", "1024"))
        self.followup_seconds = float(os.getenv("THINKSMART_FOLLOWUP_SECONDS", "4"))
        self.question_followup_seconds = float(os.getenv("THINKSMART_QUESTION_FOLLOWUP_SECONDS", "8"))
        self.turn_effects = []
        self.next_followup = 0.0
        self.ducker = Ducker(float(os.getenv("THINKSMART_DUCK_LEVEL", "0.45")))
        self.music = MusicService(ui.emit, float(os.getenv("THINKSMART_MUSIC_DUCK_LEVEL", "0.2")))
        self.alarms = AlarmService(ui.emit, on_ring=self.on_alarm_ring)
        self.offline_commands = offline.OfflineCommands(KWS_MODEL, f"{VOICE_ROOT}/offline-keywords.txt")
        self.offline_mode = False
        self.music_started = False
        self.conversation_over = False
        self.screen_off_pending = False
        self.stop_requested = False
        self.last_progress = time.monotonic()
        self.capture = None
        self.capture_thread = None
        self.capture_stop = threading.Event()
        self.audio_frames = queue.Queue(maxsize=200)
        self.capture_progress = time.monotonic()
        self.capture_clock_start = self.capture_progress
        self.capture_clock_bytes = 0
        self.capture_clock = None
        self.raw_progress = 0.0
        self.capture_levels = {"raw": 0.0, "aec": 0.0}
        self.dropped_frames = 0
        self.audio_overflow = threading.Event()
        self.bad_capture_windows = 0
        self.capture_failures = deque()
        self.recovery_requested = threading.Event()
        self.recovery_in_progress = False
        self.recovery_attempts = deque()
        self.recovery_state_file = "/data/thinksmart/music/audio-recovery.json"
        try:
            with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as source:
                self.recovery_boot = source.read().strip()
            with open(self.recovery_state_file, encoding="utf-8") as source:
                saved_recovery = json.load(source)
            if saved_recovery.get("boot") == self.recovery_boot:
                now = time.monotonic()
                self.recovery_attempts.extend(t for t in saved_recovery.get("attempts", [])
                                              if type(t) in (int, float) and 0 <= now - t < 600)
        except (OSError, ValueError, TypeError, AttributeError):
            self.recovery_boot = getattr(self, "recovery_boot", "")
        self.recovery_retry_at = 0.0
        self.recovery_status = "idle"
        self.recovery_reason = ""
        self.turn_timing = {}
        self.wake_started = None
        self.wake_hits = 0
        self.last_wake = None
        self.playback = None
        self.speech_player = None
        self.local_speaking = False
        self.heard_reply = ""
        self.wake_phrase = self.read_wake_phrase()
        self.keyword_spotter = self.create_keyword_spotter()
        self.phase = "idle"
        self.cloud_state = "ready"
        self.mic_enabled = threading.Event()
        self.mic_requested = True
        self.hardware_mic = self.read_hardware_mic()
        try:
            with open(UI_SETTINGS_FILE, encoding="utf-8") as source:
                self.mic_requested = json.load(source).get("mic", True) is not False
        except (OSError, ValueError, AttributeError):
            pass
        if self.mic_requested and self.hardware_mic is not False:
            self.mic_enabled.set()
        self.talk_requested = threading.Event()
        self.cancel_requested = threading.Event()
        self.barge_in_requested = threading.Event()
        self.raw_wake_allowed = True
        self.brain = Brain({
            "apiKey": chat.get("api_key") or self.api_key,
            "baseUrl": chat.get("base_url") or f"https://{self.workspace}.{self.region}.maas.aliyuncs.com/compatible-mode/v1",
            "provider": chat.get("provider", "bailian"),
            "api": chat.get("api", "openai-completions"),
            "providerOrder": chat.get("provider_order", []),
            "providerOrderModel": chat.get("model") or self.chat_model,
            "model": self.chat_model,
            "maxTokens": chat.get("max_tokens", int(os.getenv("THINKSMART_MAX_REPLY_TOKENS", "256"))),
            "contextWindow": chat.get("context_window", 131072),
            "bailian": {
                "apiKey": self.api_key,
                "baseUrl": f"https://{self.workspace}.{self.region}.maas.aliyuncs.com/compatible-mode/v1",
                "provider": "bailian",
                "api": "openai-completions",
                "maxTokens": int(os.getenv("THINKSMART_MAX_REPLY_TOKENS", "256")),
                "contextWindow": 131072,
            },
            "expert": {
                "apiKey": expert.get("api_key"),
                "api": expert.get("api", "openai-responses"),
                "provider": expert.get("provider", "openai"),
                "baseUrl": expert.get("base_url", "https://api.openai.com/v1"),
                "model": self.expert_model,
                "reasoningEffort": expert.get("reasoning_effort", "high"),
                "maxTokens": expert.get("max_tokens", 16384),
                "contextWindow": expert.get("context_window", 1050000),
            },
            "temperature": 0.6,
            "thinkingBudget": self.thinking_budget,
            "search": os.getenv("THINKSMART_WEB_SEARCH", "1") != "0",
            "system": self.system_prompt + TOOL_RULES + (
                "默认使用简体中文与用户交流，回复正文和语音播报均使用中文。"
                "即使用户夹带外文，或搜索结果、工具结果是外文，也要用中文解释和概括；"
                "只有用户明确要求翻译成其他语言、使用其他语言回答或进行外语练习时，才切换语言。"
                "歌名、歌手名、人名、型号、代码和接口标识符保留准确原文，不要为了中文化而误译。"
            ) + (
                "你可以按需调用联网搜索工具。涉及新闻、近期事件、比分、行情等实时信息时先搜索核实，"
                "不要把记忆当成实时信息；普通聊天不必搜索。搜索后用自然中文概括，通常两三句话。"
            ) + "回复会被直接朗读：不要输出网址、链接、来源标注、引用编号或Markdown标记。",
            "tools": [END_TOOL] + self.music.tools + information.TOOLS + self.alarms.tools,
        })
        self.brain.configure_models(self.chat_model, self.expert_model, self.chat_provider)
        logging.info("对话模型：%s / %s", self.chat_provider, self.chat_model)
        if self.expert_model:
            logging.info("困难任务模型：openai / %s / high", self.expert_model)

    def model_overrides(self):
        values = dict(self.model_defaults)
        try:
            with open(MODEL_OVERRIDES, encoding='utf-8') as source:
                env = dict(line.strip().split('=', 1) for line in source if '=' in line and not line.lstrip().startswith('#'))
            for key, name in MODEL_ENV.items():
                if name in env:
                    values[key] = env[name]
        except OSError:
            pass
        return values

    def begin_turn(self):
        # Only the conversation thread changes these values, before recognition starts.
        values = self.model_overrides()
        if any(getattr(self, key) != value for key, value in values.items()):
            self.brain.configure_models(values['chat_model'], values['expert_model'], values['chat_provider'])
            for key, value in values.items():
                setattr(self, key, value)
            logging.info('下一轮模型与音色配置已加载')

    def preview_voice(self, sock, sender, message):
        try:
            output = io.BytesIO()
            with wave.open(output, 'wb') as target:
                target.setnchannels(1)
                target.setsampwidth(2)
                target.setframerate(PLAYBACK_RATE)
                with self.client.audio.tts.connect(model=message['model'], voice=message['voice'], format='pcm',
                                                   sample_rate=PLAYBACK_RATE, rate=self.speech_rate, timeout=15) as session:
                    session.send_text('你好，我是你的语音助手。很高兴为你服务。')
                    session.stop()
                    total = 0
                    for event in session.events():
                        if isinstance(event, bytes):
                            total += len(event)
                            if total > PLAYBACK_RATE * 2 * 20:
                                raise ValueError('试听音频过长')
                            target.writeframesraw(event)
                    if not total:
                        raise ValueError('没有收到试听音频，请稍后重试')
            path = '/run/thinksmart-voice/preview-' + message['id'] + '.wav'
            with open(path, 'wb') as target:
                target.write(output.getvalue())
            cleanup = threading.Timer(60, lambda: os.path.exists(path) and os.unlink(path))
            cleanup.daemon = True
            cleanup.start()
            self.control_reply(sock, sender, message, True, 'ready')
        except Exception:
            self.control_reply(sock, sender, message, False, '试听生成失败，请检查语音服务或稍后重试')
        finally:
            self.preview_lock.release()

    @staticmethod
    def clamp_rate(value):
        try:
            rate = float(value)
        except (TypeError, ValueError):
            return 1.5
        return round(min(2.0, max(0.5, rate)), 2) if math.isfinite(rate) else 1.5

    def load_ui_prefs(self):
        try:
            with open(UI_SETTINGS_FILE, "r", encoding="utf-8") as source:
                saved = json.load(source)
        except (OSError, ValueError):
            return
        if not isinstance(saved, dict):
            return
        if "speech_rate" in saved:
            self.speech_rate = self.clamp_rate(saved["speech_rate"])
        if isinstance(saved.get("thinking"), bool):
            self.thinking = saved["thinking"]
        if isinstance(saved.get("night"), dict):
            self.night.update(saved["night"])

    def night_active(self):
        now = datetime.datetime.now()
        minute = now.hour * 60 + now.minute
        start, end = self.night["start"], self.night["end"]
        return self.night["enabled"] and start != end and (
            start <= minute < end if start < end else minute >= start or minute < end
        )

    def set_phase(self, phase):
        self.phase = phase
        if phase in ("idle", "think") and not self.barge_in_requested.is_set():
            self.clear_audio()
        self.ui.emit(phase)

    def set_cloud(self, state):
        self.cloud_state = state
        self.ui.emit("cloud", s=state)

    def announce(self):
        self.ui.emit(
            "ready",
            wake=self.wake_phrase,
            mic=int(self.mic_enabled.is_set()),
            requested_mic=self.mic_requested,
            hardware_mic=self.hardware_mic,
            phase=self.phase,
        )
        self.ui.emit("cloud", s=self.cloud_state)
        self.music.announce()
        self.alarms.announce()
        self.ui.snapshot()

    def on_alarm_ring(self, ringing):
        if ringing:
            self.music.hold()
            self.music.duck(True)
        elif self.phase == "idle":
            self.music.duck(False)
            self.music.release()

    def duck(self):
        self.music.hold()
        self.music.duck(True)
        threading.Thread(target=self.ducker.duck, name="ducker", daemon=True).start()

    def unduck(self):
        if not self.alarms.ringing:
            self.music.duck(False)
            self.music.release()
        threading.Thread(target=self.ducker.restore, name="ducker-restore", daemon=True).start()

    def set_mic(self, enabled):
        self.mic_requested = enabled
        active = enabled and self.hardware_mic is not False
        if active:
            self.mic_enabled.set()
        else:
            self.mic_enabled.clear()
            self.talk_requested.clear()
            self.clear_audio()
            self.barge_in_requested.clear()
            if self.phase != "idle":
                self.cancel_requested.set()
        logging.info("麦克风已%s", "开启" if active else "关闭")
        self.ui.emit("mic", on=int(active), requested_mic=enabled, hardware_mic=self.hardware_mic)

    @staticmethod
    def read_hardware_mic():
        try:
            with open(MIC_STATE_FILE, encoding="ascii") as source:
                value = source.read(4).strip()
            return {"1": True, "0": False}.get(value)
        except OSError:
            return None

    def refresh_hardware_mic(self):
        state = self.read_hardware_mic()
        if state is not None and state != self.hardware_mic:
            self.hardware_mic = state
            self.set_mic(self.mic_requested)

    def start_control(self):
        try:
            try:
                os.unlink(CONTROL_SOCKET)
            except FileNotFoundError:
                pass
            sock = socket.socket(
                socket.AF_UNIX,
                socket.SOCK_DGRAM | socket.SOCK_CLOEXEC,
            )
            sock.bind(CONTROL_SOCKET)
        except OSError as error:
            logging.warning("界面控制通道不可用：%s", error)
            return
        threading.Thread(
            target=self.control_loop,
            args=(sock,),
            name="ui-control",
            daemon=True,
        ).start()

    def control_loop(self, sock):
        while not self.stop_requested:
            try:
                data, sender = sock.recvfrom(2048)
            except OSError:
                return
            try:
                message = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(message, dict):
                continue
            try:
                command = message.get("c")
                if command == "talk":
                    if not self.mic_enabled.is_set():
                        raise ValueError("麦克风已关闭，请先打开麦克风")
                    if self.phase != "idle":
                        raise ValueError("正在对话中，可先结束当前对话")
                    if self.alarms.ringing:
                        self.alarms.dismiss()
                    self.talk_requested.set()
                elif command == "stop":
                    self.alarms.dismiss()
                    if self.phase != "idle":
                        self.cancel_requested.set()
                elif command == "alarm_stop":
                    self.alarms.dismiss()
                elif command == "mic":
                    self.set_mic(bool(message.get("on")))
                elif command == "hardware_mic":
                    self.refresh_hardware_mic()
                elif command == "hello":
                    self.announce()
                elif command == "snapshot":
                    with self.ui.lock:
                        self.ui._record("music", self.music.player.snapshot())
                        self.ui._record("alarms", {"next": self.alarms.next_summary()})
                    self.ui.snapshot(sender)
                elif command == 'preview':
                    if not sender or not re.fullmatch('[a-f0-9]{24}', str(message.get('id', ''))):
                        raise ValueError('试听请求无效')
                    if not all(re.fullmatch('[A-Za-z0-9_.-]{1,128}', str(message.get(k, ''))) for k in ('model', 'voice')):
                        raise ValueError('试听模型或音色无效')
                    if not self.preview_lock.acquire(blocking=False):
                        raise ValueError('上一段试听仍在生成，请稍后再试')
                    threading.Thread(target=self.preview_voice, args=(sock, sender, message), daemon=True).start()
                    continue
                elif command == "ping":
                    health = {"seq": message.get("seq"),
                              "healthy": time.monotonic() - self.last_progress < 20 and self.audio_ready(),
                              "control_healthy": time.monotonic() - self.last_progress < 20,
                              "audio_ready": self.audio_ready(),
                              "audio_recovery": {"status": self.recovery_status, "reason": self.recovery_reason,
                                                 "attempts": len(self.recovery_attempts)},
                              "phase": self.phase, "cloud": self.cloud_state,
                              "mic": self.mic_enabled.is_set(), "hardware_mic": self.hardware_mic,
                              "capture_age": round(time.monotonic() - self.capture_progress, 2),
                              "queued_audio_ms": self.audio_frames.qsize() * 40,
                              "capture_clock": self.capture_clock,
                              "raw_age": round(time.monotonic() - self.raw_progress, 2) if self.raw_progress else None,
                              "levels": dict(self.capture_levels), "dropped_frames": self.dropped_frames,
                              "timing": dict(self.turn_timing),
                              "wake_detection": {"count": self.wake_hits, "last": self.last_wake},
                              "preferences": {"speech_rate": self.speech_rate, "thinking": self.thinking,
                                              "night": self.night, "mic": self.mic_requested},
                              "configuration": {
                                  "kws_score": float(os.getenv("THINKSMART_KWS_SCORE", "1.0")),
                                  "kws_threshold": float(os.getenv("THINKSMART_KWS_THRESHOLD", "0.25")),
                                  "speech_wait_seconds": float(os.getenv("THINKSMART_SPEECH_WAIT_SECONDS", "8")),
                                  "max_speech_seconds": float(os.getenv("THINKSMART_MAX_SPEECH_SECONDS", "25")),
                                  "followup_seconds": self.followup_seconds,
                                  "question_followup_seconds": self.question_followup_seconds},
                              "models": {"chat_model": self.chat_model, "chat_provider": self.chat_provider,
                                         "wake_model": MODEL_NAME,
                                         "expert_model": self.expert_model, "expert_provider": self.expert_provider,
                                         "asr_model": self.asr_model, "tts_model": self.tts_model,
                                         "tts_voice": self.tts_voice}, "turn_models": dict(self.turn_models)}
                    if sender:
                        try:
                            sock.sendto(json.dumps(dict(health, e="pong")).encode(), socket.MSG_DONTWAIT, sender)
                        except OSError:
                            pass
                    else:
                        self.ui.emit("pong", **health)
                elif command == "prefs":
                    if "rate" in message:
                        self.speech_rate = self.clamp_rate(message.get("rate"))
                    if "think" in message:
                        self.thinking = bool(message.get("think"))
                    if "night" in message:
                        self.night = message["night"]
                    logging.info("语速 %.1f，思考模式%s", self.speech_rate, "开启" if self.thinking else "关闭")
                elif command == "music":
                    self.music.ui_command(message.get("a"))
                elif command == "login":
                    self.music.login_command(message.get("t"))
                else:
                    raise ValueError("不支持的设备操作")
                if sender and command not in ("ping", "snapshot", "hello"):
                    self.control_reply(sock, sender, message, True, "applied" if command in ("prefs", "mic") else "accepted")
            except Exception as error:
                logging.warning("控制请求失败：%s", type(error).__name__)
                if sender:
                    self.control_reply(sock, sender, message, False, str(error) if isinstance(error, ValueError) else "操作失败")

    @staticmethod
    def control_reply(sock, sender, request, ok, status):
        try:
            sock.sendto(json.dumps({"e": "ack", "id": request.get("id"), "ok": ok,
                                   "status": status}).encode(), socket.MSG_DONTWAIT, sender)
        except OSError:
            pass

    def report_error(self, error):
        code = classify_error(error)
        self.turn_models['error'] = code
        self.ui.emit("error", code=code)
        if code == "network":
            self.set_cloud("offline")
        elif code in ("auth", "config"):
            self.set_cloud("auth")
        elif code == "mic":
            if self.cloud_state == "busy":
                self.set_cloud("ready")
        else:
            self.set_cloud("error")

    @staticmethod
    def frame_level(data):
        samples = array.array("h", data)
        if sys.byteorder == "big":
            samples.byteswap()
        peak = max(max(samples), -min(samples))
        return round(min(100.0, peak * 100 / 32768), 2)

    @staticmethod
    def load_chat_config(filename="chat.json"):
        directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
        path = os.path.join(directory, filename)
        if not directory or not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as source:
            return json.load(source)

    @staticmethod
    def load_credentials():
        credential_dir = os.environ.get("CREDENTIALS_DIRECTORY")
        if not credential_dir:
            raise RuntimeError("systemd credential directory is unavailable")
        path = os.path.join(credential_dir, "aliyun.json")
        with open(path, "r", encoding="utf-8") as source:
            credentials = json.load(source)
        if not credentials.get("api_key") or not credentials.get("workspace_id"):
            raise RuntimeError("Aliyun credentials are incomplete")
        return credentials

    @staticmethod
    def read_wake_phrase():
        try:
            with open(WAKE_PHRASE_FILE, "r", encoding="utf-8") as source:
                phrase = source.read().strip()
                if phrase:
                    return phrase
        except OSError:
            pass
        return "小爱同学"

    @staticmethod
    def audio_command(mode, rate):
        return [
            "/usr/bin/pw-cat",
            mode,
            "--media-role",
            ASSISTANT_ROLE,
            "--rate",
            str(rate),
            "--channels",
            "1",
            "--format",
            "s16",
            "--latency",
            "100ms",
            "-",
        ]

    @staticmethod
    def stop_process(process):
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def create_keyword_spotter(self):
        return sherpa_onnx.KeywordSpotter(
            **offline.model_paths(KWS_MODEL),
            keywords_file=KEYWORDS_FILE,
            num_threads=int(os.getenv("THINKSMART_KWS_THREADS", "1")),
            sample_rate=SAMPLE_RATE,
            feature_dim=80,
            max_active_paths=4,
            keywords_score=float(os.getenv("THINKSMART_KWS_SCORE", "1.0")),
            keywords_threshold=float(
                os.getenv("THINKSMART_KWS_THRESHOLD", "0.25")
            ),
            provider="cpu",
        )

    def open_capture(self):
        if self.capture is not None and self.capture_thread is not None and self.capture_thread.is_alive():
            return
        self.close_capture()
        self.capture_stop.clear()
        self.capture = self.create_capture(ECHO_CANCEL_SOURCE)
        self.capture_progress = time.monotonic()
        self.capture_clock_start = self.capture_progress
        self.capture_clock_bytes = 0
        self.capture_clock = None
        self.bad_capture_windows = 0
        self.capture_thread = threading.Thread(target=self.capture_loop, name="microphone", daemon=True)
        self.capture_thread.start()

    def create_capture(self, source):
        command = self.audio_command("--record", SAMPLE_RATE)
        # WirePlumber 0.4 otherwise falls back to the default (raw) microphone.
        command[-1:-1] = [
            "--target", source,
            "--properties", "node.dont-reconnect=true",
        ]
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            # Keep recording through the wake tone and the 2-second network probe.
            pipesize=128 * 1024,
        )

    def close_capture(self):
        self.capture_stop.set()
        self.stop_process(self.capture)
        if self.capture_thread is not None:
            self.capture_thread.join(timeout=5)
            if self.capture_thread.is_alive():
                raise AudioStalled("microphone shutdown stalled")
        self.capture_thread = None
        self.capture = None
        self.clear_audio()

    def clear_audio(self):
        self.audio_overflow.clear()
        while not self.audio_frames.empty():
            try:
                self.audio_frames.get_nowait()
            except queue.Empty:
                break

    def queue_audio(self, data):
        try:
            self.audio_frames.put_nowait(data)
        except queue.Full:
            self.audio_overflow.set()
            self.dropped_frames += 1
            try:
                self.audio_frames.get_nowait()
            except queue.Empty:
                pass
            self.audio_frames.put_nowait(data)

    def audio_ready(self):
        clock = self.capture_clock
        return bool(self.capture_thread is not None and self.capture_thread.is_alive()
                    and not self.capture_stop.is_set() and not self.recovery_in_progress
                    and time.monotonic() - self.capture_progress < 1
                    and clock is not None and .9 <= clock["ratio"] <= 1.15)

    def request_audio_recovery(self, reason):
        now = time.monotonic()
        if self.recovery_requested.is_set() or self.recovery_in_progress or now < self.recovery_retry_at:
            return
        while self.recovery_attempts and now - self.recovery_attempts[0] >= 600:
            self.recovery_attempts.popleft()
        self.recovery_reason = reason
        if len(self.recovery_attempts) >= 2:
            self.recovery_status = "limited"
            self.recovery_retry_at = now + 60
            logging.error("音频恢复已达每十分钟两次上限：%s", reason)
            return
        self.recovery_status = "pending"
        self.recovery_requested.set()
        if not self.alarms.ringing:
            self.cancel_requested.set()

    def recover_audio(self):
        if self.alarms.ringing:
            return
        self.recovery_in_progress = True
        self.recovery_status = "recovering"
        self.recovery_attempts.append(time.monotonic())
        try:
            temporary = self.recovery_state_file + ".new"
            with open(temporary, "w", encoding="utf-8") as target:
                json.dump({"boot": self.recovery_boot, "attempts": list(self.recovery_attempts)}, target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.recovery_state_file)
        except OSError:
            logging.warning("未能保存音频恢复次数")
        self.recovery_retry_at = time.monotonic() + 60
        self.music.hold()
        try:
            logging.warning("正在重建音频链路：%s", self.recovery_reason)
            self.close_capture()
            subprocess.run(["/usr/bin/systemctl", "--user", "restart", "pipewire.service",
                            "wireplumber.service", "pipewire-pulse.service"],
                           check=True, capture_output=True, timeout=20)
            self.open_capture()
            deadline = time.monotonic() + 30
            while not self.stop_requested and time.monotonic() < deadline:
                self.last_progress = time.monotonic()
                if self.capture_thread is None or not self.capture_thread.is_alive():
                    self.open_capture()
                clock = self.capture_clock
                if (clock is not None and .9 <= clock["ratio"] <= 1.15
                        and time.monotonic() - self.capture_progress < 1):
                    self.capture_failures.clear()
                    self.recovery_status = "recovered"
                    logging.info("音频链路已恢复连续采集")
                    return
                time.sleep(.1)
            self.recovery_status = "failed"
            logging.error("音频链路恢复后仍未就绪")
        except (OSError, subprocess.SubprocessError) as error:
            self.recovery_status = "failed"
            logging.error("音频链路恢复失败：%s", type(error).__name__)
        finally:
            self.recovery_requested.clear()
            self.recovery_in_progress = False
            self.cancel_requested.clear()
            self.music.release()

    def audio_watchdog(self):
        while not self.stop_requested:
            time.sleep(1)
            if self.recovery_in_progress:
                continue
            player = self.speech_player
            capture_stalled = (self.capture_thread is not None and self.capture_thread.is_alive()
                               and not self.capture_stop.is_set()
                               and time.monotonic() - self.capture_progress > 15)
            if player is not None and player.stalled():
                logging.critical("播放调用已停滞，退出并由systemd恢复服务")
                os._exit(1)
            if capture_stalled:
                self.request_audio_recovery("采集流超过15秒未推进")
            elif self.bad_capture_windows >= 3:
                self.request_audio_recovery("连续三个观察窗口收音不足")

    def read_audio_bytes(self):
        while True:
            self.check_cancel()
            if self.audio_overflow.is_set():
                raise RuntimeError("microphone command buffer overflow")
            try:
                return self.audio_frames.get(timeout=0.1)
            except queue.Empty:
                if self.capture_thread is None or not self.capture_thread.is_alive():
                    raise RuntimeError("microphone stream ended")

    @staticmethod
    def read_capture(capture):
        frame = bytearray()
        while len(frame) < FRAME_BYTES:
            if not select.select([capture.stdout], [], [], 4)[0]:
                raise RuntimeError("microphone stream timed out")
            data = capture.stdout.read(FRAME_BYTES - len(frame))
            if not data:
                details = ""
                if capture.poll() is not None and capture.stderr:
                    details = capture.stderr.read(4096).decode(
                        "utf-8",
                        errors="replace",
                    ).strip()
                suffix = f": {details}" if details else ""
                raise RuntimeError(f"microphone stream ended{suffix}")
            frame.extend(data)
        return bytes(frame)

    def capture_loop(self):
        tail = deque(maxlen=8)
        try:
            with WakeDetector(self) as detector:
                while not self.capture_stop.is_set() and not self.stop_requested:
                    data = self.read_capture(self.capture)
                    self.capture_progress = time.monotonic()
                    self.capture_clock_bytes += len(data)
                    elapsed = self.capture_progress - self.capture_clock_start
                    if elapsed >= 5:
                        seconds = self.capture_clock_bytes / (SAMPLE_RATE * 2)
                        self.capture_clock = {"audio_seconds": round(seconds, 2),
                                              "wall_seconds": round(elapsed, 2),
                                              "ratio": round(seconds / elapsed, 3)}
                        self.bad_capture_windows = self.bad_capture_windows + 1 if seconds / elapsed < .9 else 0
                        if seconds / elapsed < .9:
                            logging.warning("收音数据不连续：经过%.2f秒，收到%.2f秒音频", elapsed, seconds)
                        self.capture_clock_start = self.capture_progress
                        self.capture_clock_bytes = 0
                    self.capture_levels["aec"] = self.frame_level(data)
                    if not self.mic_enabled.is_set():
                        detector.close_raw()
                        tail.clear()
                        continue
                    tail.append(data)
                    is_playing = (
                        self.music.player.state == "playing"
                        or self.speech_player is not None
                        or self.local_speaking
                        or self.alarms.ringing
                    )
                    result, source = detector.feed(data, is_playing, self.raw_wake_allowed)
                    hit = result and self.phase not in ("wake", "listen") and not self.barge_in_requested.is_set()
                    if hit:
                        if self.alarms.ringing:
                            self.alarms.dismiss()
                        self.wake_hits += 1
                        self.last_wake = {"at": time.time(), "source": source}
                        self.wake_started = time.monotonic()
                        self.turn_timing = {"wake_at": time.time(), "source": source}
                        self.clear_audio()
                        for frame in list(tail)[:-1]:
                            self.queue_audio(frame)
                        self.screen_off_pending = False
                        if self.phase == "idle":
                            self.set_phase("wake")
                            self.talk_requested.set()
                        else:
                            self.barge_in_requested.set()
                            self.cancel_requested.set()
                        logging.info("%s检测到唤醒词：%s (%s)", self.phase, result, source)
                    if self.phase in ("wake", "listen") or self.barge_in_requested.is_set():
                        self.queue_audio(data)
        except (OSError, RuntimeError, ValueError) as error:
            if not self.capture_stop.is_set() and not self.stop_requested:
                logging.error("麦克风监听失败：%s", error)
                now = time.monotonic()
                self.capture_failures.append(now)
                while self.capture_failures and now - self.capture_failures[0] > 60:
                    self.capture_failures.popleft()
                if len(self.capture_failures) >= 3:
                    self.request_audio_recovery("一分钟内采集流连续三次失败")

    @staticmethod
    def pcm_to_floats(data):
        samples = array.array("h")
        samples.frombytes(data)
        if sys.byteorder == "big":
            samples.byteswap()
        return [sample / 32768.0 for sample in samples]

    def wait_for_wake(self):
        logging.info("等待唤醒词：%s", self.wake_phrase)
        while not self.stop_requested:
            if self.talk_requested.wait(0.1):
                self.talk_requested.clear()
                if not self.mic_enabled.is_set():
                    continue
                if self.phase != "wake":
                    self.wake_started = time.monotonic()
                    self.turn_timing = {"wake_at": time.time(), "source": "button"}
                self.set_phase("wake")
                return True
            self.check_cancel()
            if not self.capture_thread.is_alive():
                raise RuntimeError("microphone stream ended")
        return False

    def start_listening(self):
        """Every entry into listening: the cue means "you can speak now"; speech over it is kept."""
        self.set_phase("listen")
        self.play_listen_tone()

    def play_listen_tone(self):
        # A soft bell: smooth 15 ms attack, then a natural decay instead of a flat beep.
        level = 0.065 if self.night_active() else 0.13
        attack, decay, length = 0.015, 0.085, 0.32
        samples = array.array("h")
        for index in range(int(PLAYBACK_RATE * length)):
            t = index / PLAYBACK_RATE
            if t < attack:
                envelope = 0.5 - 0.5 * math.cos(math.pi * t / attack)
            else:
                envelope = math.exp(-(t - attack) / decay) * min(1.0, (length - t) / 0.02)
            tone = math.sin(2 * math.pi * 660 * t) + 0.25 * math.sin(2 * math.pi * 1320 * t)
            samples.append(int(32767 * level * envelope * tone / 1.25))
        if sys.byteorder == "big":
            samples.byteswap()

        self.playback = subprocess.Popen(
            self.audio_command("--playback", PLAYBACK_RATE),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        try:
            self.playback.communicate(samples.tobytes(), timeout=5)
            if self.playback.returncode != 0:
                logging.warning("聆听提示音播放失败")
        except (OSError, subprocess.TimeoutExpired) as error:
            logging.warning("聆听提示音播放失败：%s", error)
            self.stop_process(self.playback)
        finally:
            self.playback = None

    def transcribe_live(self, wait_seconds=None):
        state = {"text": "", "speech_at": None}
        speech_seen = threading.Event()
        sentence_ended = threading.Event()

        def receive_events(recognition):
            for event in recognition.events():
                payload = event.get("payload") or {}
                sentence = (payload.get("output") or {}).get("sentence") or {}
                text = sentence.get("text")
                if text and not sentence.get("heartbeat") and not self.cancel_requested.is_set():
                    if text.strip() != state["text"]:
                        self.ui.emit("partial", text=text.strip())
                    state["text"] = text.strip()
                    if state["speech_at"] is None:
                        state["speech_at"] = time.monotonic()
                        if self.wake_started is not None:
                            self.turn_timing["first_text_ms"] = round((state["speech_at"] - self.wake_started) * 1000)
                    speech_seen.set()
                    if sentence.get("sentence_end"):
                        sentence_ended.set()

        if wait_seconds is None:
            wait_seconds = float(os.getenv("THINKSMART_SPEECH_WAIT_SECONDS", "8"))
        max_seconds = float(os.getenv("THINKSMART_MAX_SPEECH_SECONDS", "25"))
        with self.client.audio.asr.recognize(
            model=self.asr_model,
            format="pcm",
            sample_rate=SAMPLE_RATE,
            timeout=max(10.0, wait_seconds + 5),
        ) as recognition:
            wait_deadline = time.monotonic() + wait_seconds
            if self.wake_started is not None:
                self.turn_timing["asr_ready_ms"] = round((time.monotonic() - self.wake_started) * 1000)
            with ThreadPoolExecutor(max_workers=1) as pool:
                receiver = pool.submit(receive_events, recognition)
                stopped = False
                try:
                    self.check_cancel()
                    logging.info("开始接收语音")
                    while not self.stop_requested and not sentence_ended.is_set():
                        if self.cancel_requested.is_set():
                            raise TurnCancelled()
                        if receiver.done():
                            receiver.result()
                        frame = self.read_audio_bytes()
                        recognition.send_audio_frame(frame)
                        self.ui.emit("level", v=self.frame_level(frame))
                        now = time.monotonic()
                        if not speech_seen.is_set() and now >= wait_deadline:
                            break
                        if (
                            state["speech_at"] is not None
                            and now - state["speech_at"] >= max_seconds
                        ):
                            break
                    recognition.stop()
                    stopped = True
                    self.wait_receiver(receiver, 20)
                except FutureTimeout as error:
                    raise RuntimeError("speech recognition did not finish") from error
                finally:
                    if not stopped:
                        try:
                            recognition.stop()
                        except Exception:
                            pass
                    recognition.close()

        self.check_cancel()
        if self.audio_overflow.is_set():
            raise RuntimeError("microphone command buffer overflow")
        return state["text"].strip()

    def chat_context(self):
        scheduled, _, _ = self.alarms.call_tool("manage_alarm", '{"action":"list"}')
        return (
            f"用户会用“{self.wake_phrase}”称呼你，这就是你的名字。"
            f"设备当前本地时间是{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}。"
            f"{information.current_time()}"
            f"设备所在城市：{information.home_city()}。"
            f"当前计划：{scheduled}。"
            + self.music.describe()
        )

    def check_cancel(self):
        self.last_progress = time.monotonic()
        if (self.cancel_requested.is_set() or self.stop_requested
                or (self.recovery_requested.is_set() and not self.recovery_in_progress and not self.alarms.ringing)):
            raise TurnCancelled()

    def run_tool(self, name, args, has_reply):
        """Returns (message, ok, final); final results are spoken as-is instead of going back to the model."""
        self.check_cancel()
        arguments = json.dumps(args, ensure_ascii=False)
        logging.info("工具调用：%s %s", name, arguments)
        if name in ("get_current_time", "get_weather"):
            message, ok, _ = information.call_tool(name, arguments)
            self.turn_effects.append("info")
            return message, ok, False
        if name == "manage_alarm":
            message, ok, _ = self.alarms.call_tool(name, arguments)
            listing = args.get("action") == "list"
            self.turn_effects.append("info" if listing else "action" if ok else "failed")
            return message, ok, ok and not listing
        if name == "end_conversation":
            self.finish_conversation(bool(args.get("screen_off")), bool(args.get("stop_music")))
            return ("" if has_reply else "好的"), True, True
        message, ok, started = self.music.call_tool(name, arguments, self.check_cancel)
        self.turn_effects.append("action" if ok else "failed")
        self.music_started = self.music_started or started
        return message, ok, True

    def speak_stream(self, fragments):
        player = PCMPlayer(PLAYBACK_RATE, 0.5 if self.night_active() else 1.0)
        progress = SpeechProgress(PLAYBACK_RATE)
        self.speech_player = player
        self.heard_reply = ""
        reply_parts = []
        completed = False
        self.raw_wake_allowed = True
        try:
            with self.client.audio.tts.connect(
                model=self.tts_model,
                voice=self.tts_voice,
                format="pcm",
                sample_rate=PLAYBACK_RATE,
                rate=self.speech_rate,
                parameters={"word_timestamp_enabled": True},
            ) as session:

                speaking_set = False

                def receive_audio():
                    nonlocal speaking_set
                    for event in session.events():
                        self.check_cancel()
                        if isinstance(event, bytes):
                            if not speaking_set:
                                speaking_set = True
                                self.set_phase("speak")
                            self.turn_models.update(speech='cloud', tts_model=self.tts_model, tts_voice=self.tts_voice)
                            progress.audio(event)
                            player.write(event)
                        else:
                            progress.event(event)

                with ThreadPoolExecutor(max_workers=1) as pool:
                    receiver = pool.submit(receive_audio)
                    try:
                        for fragment in fragments:
                            self.check_cancel()
                            if receiver.done():
                                receiver.result()
                            if fragment:
                                reply_parts.append(fragment)
                                if self.wake_phrase in re.sub(r"\s", "", "".join(reply_parts)):
                                    self.raw_wake_allowed = False
                                self.ui.emit("delta", text=fragment)
                                session.send_text(fragment)
                        session.stop()
                        if not speaking_set:
                            self.set_phase("speak")
                        self.wait_receiver(receiver, 180)
                        player.finish(self.check_cancel)
                    except Exception:
                        player.close()
                        session.close()
                        raise
            completed = True
        except Exception:
            self.check_cancel()
            raise
        finally:
            try:
                player.close()
            finally:
                self.speech_player = None
                self.raw_wake_allowed = True
            self.heard_reply = "".join(reply_parts).strip() if completed else progress.heard(player.played_seconds)
            if not completed:
                logging.info("播报停止在 %.2f 秒，已听到：%s", player.played_seconds, self.heard_reply)
        self.check_cancel()
        return self.heard_reply

    def wait_receiver(self, receiver, timeout):
        deadline = time.monotonic() + timeout
        while True:
            try:
                return receiver.result(timeout=0.25)
            except FutureTimeout:
                self.check_cancel()
                if time.monotonic() >= deadline:
                    raise

    def handle_turn(self, user_text):
        self.turn_models = {'started_at': time.time(), 'chat_model': None, 'speech': None}
        self.heard_reply = ""
        self.local_result = ""
        self.model_turn = False
        try:
            self.answer_turn(user_text)
        finally:
            if not self.model_turn:
                reply = self.heard_reply
                if self.cancel_requested.is_set() and reply:
                    reply += "（播报被打断，只听到了以上内容。）"
                if self.local_result and "action" in self.turn_effects:
                    reply += f"（设备已执行：{self.local_result}）"
                self.brain.note(user_text, reply)

    def answer_turn(self, user_text):
        logging.info("用户：%s", user_text)
        compact = re.sub(r"[\s，。！？、,.!?~～…]+", "", user_text or "")
        if self.wake_phrase and compact == self.wake_phrase:
            if self.alarms.ringing:
                self.alarms.dismiss()
            self.play_listen_tone()
            self.next_followup = float(os.getenv("THINKSMART_SPEECH_WAIT_SECONDS", "8"))
            return
        stripped_text = user_text
        if self.wake_phrase and compact.startswith(self.wake_phrase):
            stripped_text = re.sub(
                r"^[\s，。！？、,.!?~～`]*" + re.escape(self.wake_phrase) + r"[\s，。！？、,.!?~～`]*",
                "",
                user_text,
            ).strip() or user_text
        self.ui.emit("user", text=user_text)
        self.set_phase("think")
        self.music_started = False
        self.conversation_over = False
        self.turn_effects = []
        self.next_followup = 0.0
        command = match_command(stripped_text, self.music.active())
        farewell = match_farewell(stripped_text, self.wake_phrase)
        if re.sub(r"[\s，。！？,.!?]", "", stripped_text) in ("停止闹钟", "关闭闹钟", "停止提醒", "关掉闹钟"):
            dismissed = self.alarms.dismiss()
            self.turn_effects.append("action" if dismissed else "failed")
            reply = "已停止提示音" if dismissed else "现在没有正在响的闹钟或提醒"
        elif farewell:
            self.finish_conversation(farewell == "sleep",
                                     farewell == "sleep" and bool(POWER_OFF_WORDS.search(user_text)))
            reply = "好的"
        elif self.offline_mode and any(word in user_text for word in ("几点", "几号", "星期")):
            reply = information.current_time()
        elif command:
            logging.info("本地指令：%s %s", *command)
            reply, ok, self.music_started = self.music.run_command(*command, check_cancel=self.check_cancel)
            self.turn_effects.append("action" if ok else "failed")
        elif self.offline_mode:
            reply = "离线时请说音量调大、音量调小、暂停播放、继续播放、上一首、下一首或现在几点。"
        else:
            self.model_turn = True
            reply = self.chat_turn(user_text)
            logging.info("助手：%s", reply)
            self.next_followup = self.followup_window(reply)
            return
        self.local_result = reply
        self.speak_reply(reply)
        logging.info("助手：%s", reply)
        self.next_followup = self.followup_window(reply)

    def followup_window(self, reply):
        """Seconds to keep listening without the wake word, judged by what this turn did.

        A turn that only carried out actions (music, volume, alarms) is finished; a reply
        that asks something waits longer; anything else gets a short window that closes
        silently if the user does not speak."""
        if self.conversation_over or self.music_started or self.offline_mode:
            return 0.0
        if QUESTION_END.search(reply or ""):
            return self.question_followup_seconds
        if self.turn_effects and all(effect == "action" for effect in self.turn_effects):
            logging.info("指令已完成，结束对话")
            return 0.0
        return self.followup_seconds

    def finish_conversation(self, screen_off, stop_music=False):
        self.conversation_over = True
        self.screen_off_pending = self.screen_off_pending or screen_off
        if stop_music:
            # Also clears the conversation hold, so unduck() will not resume it.
            self.music.control("stop")
        logging.info("用户结束对话%s%s", "，关闭屏幕" if screen_off else "", "，停止音乐" if stop_music else "")

    def speak_reply(self, reply):
        if not self.offline_mode:
            try:
                self.speak_stream(iter((reply,)))
                return
            except (TurnCancelled, AudioStalled):
                raise
            except Exception as error:
                logging.warning("云端播报失败，改用本地播报：%s", error)
        self.speak_local(reply)

    def speak_local(self, reply):
        self.turn_models.update(speech='local', tts_model='espeak-ng', tts_voice='cmn')
        self.check_cancel()
        heard = self.heard_reply
        remaining = reply[len(heard):] if heard and reply.startswith(heard) else reply
        self.set_phase("speak")
        self.ui.emit("reply", text=reply)
        self.raw_wake_allowed = self.wake_phrase not in re.sub(r"\s", "", reply)
        self.local_speaking = True
        try:
            succeeded = not remaining or offline.speak(
                remaining, lambda: self.cancel_requested.is_set() or self.stop_requested,
                gain=0.5 if self.night_active() else 1.0)
            self.check_cancel()
            if not succeeded:
                raise RuntimeError("speaker local playback failed")
            self.heard_reply = reply
        finally:
            self.local_speaking = False
            self.raw_wake_allowed = True

    def cloud_available(self):
        try:
            with socket.create_connection((f"{self.workspace}.{self.region}.maas.aliyuncs.com", 443), timeout=2):
                return True
        except OSError:
            return False

    def chat_turn(self, user_text):
        self.heard_reply = ""
        spoken = []
        weather = bool(WEATHER_WORDS.search(user_text))
        search = not weather and not TIME_WORDS.search(user_text)
        expert = bool(self.expert_model and (
            EXPERT_REQUEST.search(user_text) or (self.thinking and HARD_TASK_WORDS.search(user_text))
        ))
        self.turn_models.update(chat_model=self.expert_model if expert else self.chat_model,
                                chat_provider=self.expert_provider if expert else self.chat_provider,
                                role='expert' if expert else 'chat')
        # Qwen rejects a forced tool choice while thinking.
        thinking = self.thinking and not weather and bool(THINK_WORDS.search(user_text))
        if expert:
            logging.info("本轮使用 %s，推理档位 high", self.expert_model)
        elif thinking:
            logging.info("本轮开启思考")

        model_failed = False

        def model_events():
            nonlocal model_failed
            force_tool = "get_weather" if weather else None
            try:
                yield from self.brain.chat(user_text, self.chat_context(), thinking, search, force_tool, self.check_cancel, expert=expert)
            except Exception:
                model_failed = True
                raise

        def fragments():
            nonlocal model_failed
            cleaner = SpokenStream()
            for event in model_events():
                if event[0] == "delta":
                    text = cleaner.feed(event[1])
                    if text:
                        spoken.append(text)
                        yield text
                    continue
                text = cleaner.flush()
                if text:
                    spoken.append(text)
                    yield text
                _, call, name, args = event
                try:
                    message, ok, final = self.run_tool(name, args, bool("".join(spoken).strip()))
                    self.brain.tool_result(call, message, ok, final)
                except Exception:
                    model_failed = True
                    raise
                if final and message:
                    spoken.append(message)
                    yield message
            text = cleaner.flush()
            if text:
                spoken.append(text)
                yield text

        stream = fragments()
        reply = ""
        try:
            first = next(stream, None)
            if first is not None:
                try:
                    reply = self.speak_stream(chain((first,), stream))
                except (TurnCancelled, AudioStalled):
                    raise
                except Exception:
                    if model_failed:
                        raise
                    logging.warning("流式播报失败，完成当前回答后改用本地播报")
                    for _ in stream:
                        self.check_cancel()
                    self.speak_local("".join(spoken).strip())
                    reply = self.heard_reply
        finally:
            stream.close()
            self.brain.heard(self.heard_reply, interrupted=self.cancel_requested.is_set() or not reply)
        if not reply:
            raise RuntimeError("language model returned an empty reply")
        return reply

    def follow_up(self):
        """Keep listening briefly after a reply so the user can continue without the wake word."""
        while (
            self.next_followup > 0
            and not self.stop_requested
            and self.mic_enabled.is_set()
        ):
            self.begin_turn()
            self.wake_started = time.monotonic()
            self.turn_timing = {"wake_at": time.time(), "source": "followup"}
            self.open_capture()
            self.set_phase("listen")
            try:
                text = self.transcribe_live(self.next_followup)
            except (TurnCancelled, AudioStalled):
                raise
            except Exception as error:
                if classify_error(error) == "mic":
                    raise
                logging.warning("追问识别不可用，结束追问窗口：%s", error)
                return
            if not text:
                logging.info("追问窗口结束")
                return
            self.duck()
            self.handle_turn(text)

    def run(self):
        self.start_control()
        threading.Thread(target=self.audio_watchdog, name="audio-watchdog", daemon=True).start()
        self.alarms.start()
        try:
            self.brain.start()
        except (OSError, RuntimeError) as error:
            logging.error("智能体启动失败，将在对话时重试：%s", error)
        self.announce()
        while not self.stop_requested:
            try:
                if self.recovery_requested.is_set() and not self.alarms.ringing:
                    self.recover_audio()
                self.cancel_requested.clear()
                if self.barge_in_requested.is_set():
                    self.set_phase("wake")
                    self.barge_in_requested.clear()
                else:
                    self.set_phase("idle")
                    self.open_capture()
                    if self.screen_off_pending:
                        self.screen_off_pending = False
                        self.ui.emit("sleep")
                    if not self.wait_for_wake():
                        break
                self.duck()
                self.begin_turn()
                # Keep the wake stream running through the tone so an immediate
                # follow-on command is still captured.
                self.start_listening()
                if self.stop_requested:
                    break
                self.offline_mode = False
                self.check_cancel()
                self.set_cloud("busy")
                try:
                    text = self.transcribe_live()
                except TurnCancelled:
                    raise
                except Exception as error:
                    if classify_error(error) == "mic":
                        raise
                    logging.warning("云端识别不可用，改用离线指令：%s", error)
                    self.offline_mode = True
                    self.set_cloud("offline")
                    self.set_phase("think")
                    self.speak_reply("网络不可用，请再说一次简单指令。")
                    self.start_listening()
                    text = self.offline_commands.listen(self)
                if text:
                    self.handle_turn(text)
                    self.follow_up()
                else:
                    logging.info("唤醒后未识别到有效语音")
                    self.ui.emit("nospeech")
                self.set_cloud("offline" if self.offline_mode else "ready")
            except TurnCancelled:
                logging.info("本轮对话已取消")
                self.set_cloud("ready")
            except AudioStalled:
                logging.exception("音频调用无法恢复，退出并由systemd重启")
                os._exit(1)
            except Exception as error:
                if classify_error(error) == "mic" or self.capture_thread is None or not self.capture_thread.is_alive():
                    self.close_capture()
                if not self.stop_requested:
                    logging.exception("语音处理失败：%s", error)
                    self.report_error(error)
                    time.sleep(2)
            finally:
                if not self.barge_in_requested.is_set():
                    self.unduck()

    def stop(self, *_args):
        self.stop_requested = True
        self.close_capture()
        self.stop_process(self.playback)
        if self.speech_player is not None:
            self.speech_player.close()
        self.alarms.shutdown()
        self.music.shutdown()
        self.brain.close()


def main():
    logging.basicConfig(
        level=os.getenv("THINKSMART_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    ui = UiPublisher()
    try:
        assistant = ThinkSmartAssistant(ui)
    except Exception as error:
        code = classify_error(error)
        ui.emit("error", code=code)
        ui.emit("cloud", s="auth" if code in ("auth", "config") else "error")
        raise
    signal.signal(signal.SIGTERM, assistant.stop)
    signal.signal(signal.SIGINT, assistant.stop)
    assistant.run()


if __name__ == "__main__":
    main()
