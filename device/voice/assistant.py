#!/usr/bin/python3
import array
import datetime
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
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from collections import deque
from itertools import chain

import dashscope
import sherpa_onnx

from brain import Brain
from playback import PCMPlayer, SpeechProgress
from music import MUSIC_ROLE, MusicService, match_command
import information
import offline
from alarms import AlarmService


SAMPLE_RATE = 16000
FRAME_BYTES = 1280  # 40 ms of 16 kHz s16 mono
PLAYBACK_RATE = 22050
VOICE_ROOT = "/data/thinksmart/voice"
MODEL_NAME = "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
KWS_MODEL = f"{VOICE_ROOT}/models/{MODEL_NAME}"
KEYWORDS_FILE = f"{VOICE_ROOT}/keywords.txt"
WAKE_PHRASE_FILE = "/etc/thinksmart-voice/wake-phrase"
UI_EVENT_SOCKET = "/run/thinksmart-ui/events.sock"
CONTROL_SOCKET = "/run/thinksmart-voice/control.sock"
ECHO_CANCEL_SOURCE = "thinksmart.echo-cancel-source"
RAW_CAPTURE_SOURCE = "alsa_input.platform-c051000.sound-card.HiFi__hw_cd18781y_1__source"
UI_SETTINGS_FILE = "/data/thinksmart/ui/settings.json"
UI_TEXT_LIMIT = 2000
ASSISTANT_ROLE = "Assistant"
FAREWELL_WORDS = re.compile(r"退下|没事|不用|再见|拜拜|休息|去休息|别说|不聊|结束对话|就这样")
SLEEP_WORDS = re.compile(r"关机|关屏|关闭屏幕|关掉屏幕|息屏|熄屏|睡觉|去睡觉|晚安")
QUESTION_END = re.compile(r"(?:[？?]|[吗呢么][。！!～~]?)[\s”」』\"')）]*$")
# Without thinking the model often answers weather from web search or memory, so weather turns force
# the tool and skip search; time is already in the context, so time turns only skip search.
WEATHER_WORDS = re.compile(r"天气|气温|温度|几度|下雨|下雪|降雨|带伞|刮风|冷不冷|热不热|穿什么")
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
            },
        },
    },
}
TOOL_RULES = (
    "你可以用工具播放QQ音乐、控制播放和调节音量，只在用户明确提出时调用。"
    "询问当前时间、日期或天气时必须使用对应工具，不得猜测实时信息。"
    "设置、查看、取消闹钟、计时器、提醒时必须调用manage_alarm。"
    "根据当前设备本地时间计算具体日期，不得假装已设置。"
    "取消时根据用户的描述匹配编号；不明确时先询问。"
    "使用工具时不要提前声称操作成功，以工具返回结果为准。"
    "用户想结束对话时调用end_conversation；说关机、关屏或睡觉时同时把screen_off设为true，你无法关闭设备电源。"
    "新闻、比赛、行情等需要最新信息的问题可以参考联网搜索结果，用一两句话概括，不要念出网址。"
    "回答结束时不要说“还有什么需要”之类的客套追问；只有确实需要用户补充信息时才用问句结尾。"
)


class TurnCancelled(Exception):
    pass


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

    def __enter__(self):
        self.open_raw()
        return self

    def __exit__(self, *_):
        self.close_raw()

    def open_raw(self):
        if self.raw_capture is None:
            try:
                self.raw_capture = self.assistant.create_capture(RAW_CAPTURE_SOURCE)
                self.raw_stream = WakeStream(self.spotter)
            except Exception as error:
                logging.warning("启动原始麦克风流失败: %s", error)

    def close_raw(self):
        self.assistant.stop_process(self.raw_capture)
        self.raw_capture = None
        self.raw_stream = None

    def decode(self, stream, data, gain=1.0):
        samples = self.assistant.pcm_to_floats(data)
        return stream.feed(samples, gain)

    def feed(self, data, is_playing=False, raw_wake_allowed=True):
        if self.raw_capture is None:
            self.open_raw()

        raw_result = ""
        if self.raw_capture is not None:
            try:
                raw_data = self.assistant.read_capture(self.raw_capture)
                if not is_playing or raw_wake_allowed:
                    raw_result = self.decode(self.raw_stream, raw_data, gain=4.0)
            except Exception as error:
                logging.warning("读取原始麦克风流失败: %s", error)
                self.close_raw()

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
        try:
            self.sock = socket.socket(
                socket.AF_UNIX,
                socket.SOCK_DGRAM | socket.SOCK_CLOEXEC,
            )
            self.sock.setblocking(False)
        except OSError:
            self.sock = None

    def emit(self, event, **fields):
        if self.sock is None:
            return
        text = fields.get("text")
        if isinstance(text, str) and len(text) > UI_TEXT_LIMIT:
            fields["text"] = text[-UI_TEXT_LIMIT:]
        fields["e"] = event
        try:
            payload = json.dumps(
                fields,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.sock.sendto(payload, self.path)
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
        self.chat_provider = chat.get("provider", "bailian")
        self.chat_model = chat.get("model") or os.getenv("THINKSMART_CHAT_MODEL", "qwen3.8-flash")
        self.tts_model = os.getenv(
            "THINKSMART_TTS_MODEL",
            "qwen-audio-3.1-tts-flash",
        )
        self.tts_voice = os.getenv("THINKSMART_TTS_VOICE", "longxiaocheng_v3.1")
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
        self.alarms = AlarmService(ui.emit)
        self.offline_commands = offline.OfflineCommands(KWS_MODEL, f"{VOICE_ROOT}/offline-keywords.txt")
        self.offline_mode = False
        self.music_started = False
        self.conversation_over = False
        self.screen_off_pending = False
        self.stop_requested = False
        self.capture = None
        self.capture_thread = None
        self.capture_stop = threading.Event()
        self.audio_frames = queue.Queue()
        self.playback = None
        self.speech_player = None
        self.heard_reply = ""
        self.wake_phrase = self.read_wake_phrase()
        self.keyword_spotter = self.create_keyword_spotter()
        self.phase = "idle"
        self.cloud_state = "ready"
        self.mic_enabled = threading.Event()
        self.mic_enabled.set()
        self.talk_requested = threading.Event()
        self.cancel_requested = threading.Event()
        self.barge_in_requested = threading.Event()
        self.raw_wake_allowed = True
        self.brain = Brain({
            "apiKey": chat.get("api_key") or self.api_key,
            "baseUrl": chat.get("base_url") or f"https://{self.workspace}.{self.region}.maas.aliyuncs.com/compatible-mode/v1",
            "provider": self.chat_provider,
            "providerOrder": chat.get("provider_order", []),
            "model": self.chat_model,
            "maxTokens": chat.get("max_tokens", int(os.getenv("THINKSMART_MAX_REPLY_TOKENS", "256"))),
            "contextWindow": chat.get("context_window", 131072),
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
                "不要把记忆当成实时信息；普通聊天不必搜索。搜索后用自然中文概括，通常两三句话，"
                "不要念网址、引用编号或Markdown标记。" if self.chat_provider == "openrouter" else ""
            ),
            "tools": [END_TOOL] + self.music.tools + information.TOOLS + self.alarms.tools,
        })
        logging.info("对话模型：%s / %s", self.chat_provider, self.chat_model)
        if self.expert_model:
            logging.info("困难任务模型：openai / %s / high", self.expert_model)

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
            phase=self.phase,
        )
        self.ui.emit("cloud", s=self.cloud_state)
        self.music.announce()

    def duck(self):
        self.music.hold()
        self.music.duck(True)
        self.ducker.duck()

    def unduck(self):
        self.music.duck(False)
        self.music.release()
        self.ducker.restore()

    def set_mic(self, enabled):
        if enabled:
            self.mic_enabled.set()
        else:
            self.mic_enabled.clear()
            self.barge_in_requested.clear()
            if self.phase != "idle":
                self.cancel_requested.set()
        logging.info("麦克风已%s", "开启" if enabled else "关闭")
        self.ui.emit("mic", on=int(enabled))

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
                data = sock.recv(512)
            except OSError:
                return
            try:
                message = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(message, dict):
                continue
            command = message.get("c")
            if command == "talk":
                if self.mic_enabled.is_set() and self.phase == "idle":
                    self.talk_requested.set()
            elif command == "stop":
                self.alarms.dismiss()
                if self.phase != "idle":
                    self.cancel_requested.set()
            elif command == "alarm_stop":
                self.alarms.dismiss()
            elif command == "mic":
                self.set_mic(bool(message.get("on")))
            elif command == "hello":
                self.announce()
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

    def report_error(self, error):
        code = classify_error(error)
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
        return min(100, peak * 100 // 32768)

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
            tokens=os.path.join(KWS_MODEL, "tokens.txt"),
            encoder=os.path.join(
                KWS_MODEL,
                "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            ),
            decoder=os.path.join(
                KWS_MODEL,
                "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            ),
            joiner=os.path.join(
                KWS_MODEL,
                "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            ),
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
        self.capture_thread = None
        self.capture = None
        self.clear_audio()

    def clear_audio(self):
        while not self.audio_frames.empty():
            try:
                self.audio_frames.get_nowait()
            except queue.Empty:
                break

    def read_audio_bytes(self):
        while True:
            self.check_cancel()
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
                    if not self.mic_enabled.is_set():
                        detector.close_raw()
                        tail.clear()
                        continue
                    tail.append(data)
                    is_playing = (
                        self.music.player.state == "playing" or self.speech_player is not None
                    )
                    result, source = detector.feed(data, is_playing, self.raw_wake_allowed)
                    hit = result and self.phase != "wake" and not self.barge_in_requested.is_set()
                    if hit:
                        self.clear_audio()
                        for frame in list(tail)[:-1]:
                            self.audio_frames.put_nowait(frame)
                        self.screen_off_pending = False
                        if self.phase == "idle":
                            self.set_phase("wake")
                            self.talk_requested.set()
                        else:
                            self.barge_in_requested.set()
                            self.cancel_requested.set()
                        logging.info("%s检测到唤醒词：%s (%s)", self.phase, result, source)
                    if self.phase in ("wake", "listen") or self.barge_in_requested.is_set():
                        self.audio_frames.put_nowait(data)
        except (OSError, RuntimeError, ValueError) as error:
            if not self.capture_stop.is_set() and not self.stop_requested:
                logging.error("麦克风监听失败：%s", error)

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
                self.set_phase("wake")
                return True
            if not self.capture_thread.is_alive():
                raise RuntimeError("microphone stream ended")
        return False

    def play_wake_tone(self):
        duration_seconds = 0.12
        sample_count = int(PLAYBACK_RATE * duration_seconds)
        fade_in = int(PLAYBACK_RATE * 0.01)
        fade_out = int(PLAYBACK_RATE * 0.02)
        samples = array.array("h")
        for index in range(sample_count):
            envelope = min(
                1.0,
                index / max(1, fade_in),
                (sample_count - index - 1) / max(1, fade_out),
            )
            value = (0.09 if self.night_active() else 0.18) * envelope * math.sin(
                2 * math.pi * 880 * index / PLAYBACK_RATE
            )
            samples.append(int(32767 * value))
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
                logging.warning("唤醒提示音播放失败")
        except (OSError, subprocess.TimeoutExpired) as error:
            logging.warning("唤醒提示音播放失败：%s", error)
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
                    speech_seen.set()
                    if sentence.get("sentence_end"):
                        sentence_ended.set()

        if wait_seconds is None:
            wait_seconds = float(os.getenv("THINKSMART_SPEECH_WAIT_SECONDS", "8"))
        max_seconds = float(os.getenv("THINKSMART_MAX_SPEECH_SECONDS", "25"))
        wait_deadline = time.monotonic() + wait_seconds

        with self.client.audio.asr.recognize(
            model=self.asr_model,
            format="pcm",
            sample_rate=SAMPLE_RATE,
        ) as recognition:
            with ThreadPoolExecutor(max_workers=1) as pool:
                receiver = pool.submit(receive_events, recognition)
                stopped = False
                try:
                    logging.info("开始接收语音")
                    while not self.stop_requested and not sentence_ended.is_set():
                        if self.cancel_requested.is_set():
                            raise TurnCancelled()
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
        if self.cancel_requested.is_set() or self.stop_requested:
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
            self.finish_conversation(bool(args.get("screen_off")))
            return ("" if has_reply else "好的"), True, True
        message, ok, started = self.music.call_tool(name, arguments)
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

                def receive_audio():
                    for event in session.events():
                        self.check_cancel()
                        if isinstance(event, bytes):
                            progress.audio(event)
                            player.write(event)
                        else:
                            progress.event(event)

                with ThreadPoolExecutor(max_workers=1) as pool:
                    receiver = pool.submit(receive_audio)
                    try:
                        for fragment in fragments:
                            self.check_cancel()
                            if fragment:
                                reply_parts.append(fragment)
                                if self.wake_phrase in re.sub(r"\s", "", "".join(reply_parts)):
                                    self.raw_wake_allowed = False
                                self.ui.emit("delta", text=fragment)
                                session.send_text(fragment)
                        session.stop()
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
            player.close()
            self.speech_player = None
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
        self.ui.emit("user", text=user_text)
        self.set_phase("think")
        self.music_started = False
        self.conversation_over = False
        self.turn_effects = []
        self.next_followup = 0.0
        command = match_command(user_text, self.music.active())
        farewell = match_farewell(user_text, self.wake_phrase)
        if re.sub(r"[\s，。！？,.!?]", "", user_text) in ("停止闹钟", "关闭闹钟", "停止提醒"):
            dismissed = self.alarms.dismiss()
            self.turn_effects.append("action" if dismissed else "failed")
            reply = "已停止提示音" if dismissed else "现在没有正在响的闹钟或提醒"
        elif farewell:
            self.finish_conversation(farewell == "sleep")
            reply = "好的"
        elif self.offline_mode and any(word in user_text for word in ("几点", "几号", "星期")):
            reply = information.current_time()
        elif command:
            logging.info("本地指令：%s %s", *command)
            reply, ok, self.music_started = self.music.run_command(*command)
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

    def finish_conversation(self, screen_off):
        self.conversation_over = True
        self.screen_off_pending = self.screen_off_pending or screen_off
        logging.info("用户结束对话%s", "，关闭屏幕" if screen_off else "")

    def speak_reply(self, reply):
        if not self.offline_mode:
            try:
                self.speak_stream(iter((reply,)))
                return
            except TurnCancelled:
                raise
            except Exception as error:
                logging.warning("云端播报失败，改用本地播报：%s", error)
        self.set_phase("speak")
        self.ui.emit("delta", text=reply)
        self.raw_wake_allowed = self.wake_phrase not in re.sub(r"\s", "", reply)
        offline.speak(reply, lambda: self.cancel_requested.is_set() or self.stop_requested,
                      gain=0.5 if self.night_active() else 1.0)
        self.heard_reply = "" if self.cancel_requested.is_set() else reply
        self.check_cancel()

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
        # Qwen rejects a forced tool choice while thinking.
        thinking = self.thinking and not weather and bool(THINK_WORDS.search(user_text))
        if expert:
            logging.info("本轮使用 %s，推理档位 high", self.expert_model)
        elif thinking:
            logging.info("本轮开启思考")

        def fragments():
            force_tool = "get_weather" if weather else None
            for event in self.brain.chat(user_text, self.chat_context(), thinking, search, force_tool, self.check_cancel, expert=expert):
                if event[0] == "delta":
                    spoken.append(event[1])
                    yield event[1]
                    continue
                _, call, name, args = event
                message, ok, final = self.run_tool(name, args, bool("".join(spoken).strip()))
                self.brain.tool_result(call, message, ok, final)
                if final and message:
                    spoken.append(message)
                    yield message

        stream = fragments()
        reply = ""
        try:
            first = next(stream, None)
            if first is not None:
                reply = self.speak_stream(chain((first,), stream))
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
            self.set_phase("listen")
            self.open_capture()
            text = self.transcribe_live(self.next_followup)
            if not text:
                logging.info("追问窗口结束")
                return
            self.duck()
            self.handle_turn(text)

    def run(self):
        self.start_control()
        self.alarms.start()
        try:
            self.brain.start()
        except (OSError, RuntimeError) as error:
            logging.error("智能体启动失败，将在对话时重试：%s", error)
        self.announce()
        while not self.stop_requested:
            try:
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
                # Keep the wake stream running through the tone and network
                # check, so an immediate follow-on command is still captured.
                self.play_wake_tone()
                if self.stop_requested:
                    break
                self.set_phase("listen")
                self.offline_mode = not self.cloud_available()
                self.check_cancel()
                self.set_cloud("offline" if self.offline_mode else "busy")
                if self.offline_mode:
                    text = self.offline_commands.listen(self)
                else:
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
                        self.set_phase("listen")
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
