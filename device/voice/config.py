"""Paths, tunables from /etc/thinksmart-voice.conf (the service environment), credentials, the
models in use and the user's preferences. Everything the assistant reads from outside lives here."""

import json
import logging
import math
import os
import re
from dataclasses import dataclass, fields

SAMPLE_RATE = 16000
FRAME_BYTES = 1280  # 40 ms of 16 kHz s16 mono
PLAYBACK_RATE = 22050
VOICE_ROOT = "/data/thinksmart/voice"
MODEL_NAME = "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
KWS_MODEL = f"{VOICE_ROOT}/models/{MODEL_NAME}"
KEYWORDS_FILE = f"{VOICE_ROOT}/keywords.txt"
OFFLINE_KEYWORDS_FILE = f"{VOICE_ROOT}/offline-keywords.txt"
RUNTIME_DIR = "/run/thinksmart-voice"
CONTROL_SOCKET = f"{RUNTIME_DIR}/control.sock"
UI_EVENT_SOCKET = "/run/thinksmart-ui/events.sock"
ECHO_CANCEL_SOURCE = "thinksmart.echo-cancel-source"
RAW_CAPTURE_SOURCE = "alsa_input.platform-c051000.sound-card.HiFi__hw_cd18781y_1__source"
UI_SETTINGS_FILE = "/data/thinksmart/ui/settings.json"
MIC_STATE_FILE = "/run/thinksmart-keys/mic-state"
MODEL_OVERRIDES = "/etc/thinksmart-voice/gateway.conf"
RECOVERY_STATE_FILE = "/data/thinksmart/music/audio-recovery.json"
ASSISTANT_ROLE = "Assistant"
REGION = "cn-beijing"

DEFAULT_SYSTEM_PROMPT = (
    "你是运行在联想ThinkSmart独立设备上的语音助手。"
    "请用简洁、自然的中文回答，通常不超过两句话；不要使用Markdown或表情符号。"
    "不知道的事情就直接说明，不要编造。"
)


def _env(environ, name, default, cast=float):
    raw = environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = cast(raw)
    except ValueError:
        logging.warning("配置 %s=%r 无效，改用默认值 %r", name, raw, default)
        return default
    if isinstance(value, float) and not math.isfinite(value):
        logging.warning("配置 %s=%r 无效，改用默认值 %r", name, raw, default)
        return default
    return value


def _flag(environ, name, default=True):
    return environ.get(name, "1" if default else "0") != "0"


def clamp_rate(value):
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return 1.5
    return round(min(2.0, max(0.5, rate)), 1) if math.isfinite(rate) else 1.5


@dataclass(frozen=True)
class Settings:
    """Tunables fixed for the life of the service."""

    kws_score: float = 1.0
    kws_threshold: float = 0.25
    kws_threads: int = 1
    speech_wait_seconds: float = 8.0
    max_speech_seconds: float = 25.0
    followup_seconds: float = 4.0
    question_followup_seconds: float = 8.0
    # A soft cue when a turn has been silent this long, so a slow answer does not sound like a hang.
    thinking_cue_seconds: float = 2.0
    # A wake this long after the last turn starts a new conversation; follow-up turns never clear it.
    context_idle_seconds: float = 120 * 60
    duck_level: float = 0.45
    music_duck_level: float = 0.2
    max_reply_tokens: int = 256
    thinking_budget: int = 1024
    web_search: bool = True
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    speech_rate: float = 1.5
    thinking: bool = True
    asr_model: str = "qwen-audio-3.1-asr-flash-message"
    chat_model: str = "qwen3.8-flash"
    tts_model: str = "qwen-audio-3.1-tts-flash"
    tts_voice: str = "longxiaocheng_v3.1"

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        d = cls()
        return cls(
            kws_score=_env(env, "THINKSMART_KWS_SCORE", d.kws_score),
            kws_threshold=_env(env, "THINKSMART_KWS_THRESHOLD", d.kws_threshold),
            kws_threads=max(1, _env(env, "THINKSMART_KWS_THREADS", d.kws_threads, int)),
            speech_wait_seconds=_env(env, "THINKSMART_SPEECH_WAIT_SECONDS", d.speech_wait_seconds),
            max_speech_seconds=_env(env, "THINKSMART_MAX_SPEECH_SECONDS", d.max_speech_seconds),
            followup_seconds=_env(env, "THINKSMART_FOLLOWUP_SECONDS", d.followup_seconds),
            question_followup_seconds=_env(env, "THINKSMART_QUESTION_FOLLOWUP_SECONDS", d.question_followup_seconds),
            thinking_cue_seconds=_env(env, "THINKSMART_THINKING_CUE_SECONDS", d.thinking_cue_seconds),
            context_idle_seconds=_env(env, "THINKSMART_CONTEXT_IDLE_MINUTES", d.context_idle_seconds / 60) * 60,
            duck_level=_env(env, "THINKSMART_DUCK_LEVEL", d.duck_level),
            music_duck_level=_env(env, "THINKSMART_MUSIC_DUCK_LEVEL", d.music_duck_level),
            max_reply_tokens=_env(env, "THINKSMART_MAX_REPLY_TOKENS", d.max_reply_tokens, int),
            thinking_budget=_env(env, "THINKSMART_THINKING_BUDGET", d.thinking_budget, int),
            web_search=_flag(env, "THINKSMART_WEB_SEARCH"),
            system_prompt=env.get("THINKSMART_SYSTEM_PROMPT") or d.system_prompt,
            speech_rate=clamp_rate(env.get("THINKSMART_TTS_RATE", d.speech_rate)),
            thinking=_flag(env, "THINKSMART_THINKING"),
            asr_model=env.get("THINKSMART_ASR_MODEL") or d.asr_model,
            chat_model=env.get("THINKSMART_CHAT_MODEL") or d.chat_model,
            tts_model=env.get("THINKSMART_TTS_MODEL") or d.tts_model,
            tts_voice=env.get("THINKSMART_TTS_VOICE") or d.tts_voice,
        )


def _credential_path(filename, environ=None):
    directory = (os.environ if environ is None else environ).get("CREDENTIALS_DIRECTORY", "")
    return os.path.join(directory, filename) if directory else None


@dataclass(frozen=True)
class Credentials:
    """The Aliyun Bailian key, passed in by systemd as the aliyun.json credential."""

    api_key: str
    workspace: str
    region: str = REGION

    @property
    def endpoint(self):
        return f"https://{self.workspace}.{self.region}.maas.aliyuncs.com/compatible-mode/v1"

    @classmethod
    def load(cls, environ=None):
        path = _credential_path("aliyun.json", environ)
        if path is None:
            raise RuntimeError("systemd credential directory is unavailable")
        with open(path, encoding="utf-8") as source:
            data = json.load(source)
        if not data.get("api_key") or not data.get("workspace_id"):
            raise RuntimeError("Aliyun credentials are incomplete")
        credentials = cls(data["api_key"], data["workspace_id"], data.get("region", REGION))
        if credentials.region != REGION:
            raise RuntimeError("only cn-beijing is configured")
        if not re.fullmatch(r"[A-Za-z0-9-]{3,128}", credentials.workspace):
            raise RuntimeError("invalid workspace ID")
        return credentials


def load_chat_config(filename="chat.json", environ=None):
    """An optional chat or expert model credential (chat.json, expert.json); empty when not configured."""
    path = _credential_path(filename, environ)
    if path is None or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as source:
        return json.load(source)


# Model fields the management page may change between turns, and their keys in gateway.conf.
OVERRIDABLE = {"chat_model": "THINKSMART_CHAT_MODEL", "chat_provider": "THINKSMART_CHAT_PROVIDER",
               "expert_model": "THINKSMART_EXPERT_MODEL", "tts_model": "THINKSMART_TTS_MODEL",
               "tts_voice": "THINKSMART_TTS_VOICE"}


@dataclass
class Models:
    chat_provider: str
    chat_model: str
    expert_model: str | None
    expert_provider: str | None
    asr_model: str
    tts_model: str
    tts_voice: str

    @classmethod
    def initial(cls, settings, chat, expert):
        expert_model = expert.get("model") if expert.get("api_key") else None
        return cls(
            chat_provider=chat.get("provider", "bailian"),
            chat_model=chat.get("model") or settings.chat_model,
            expert_model=expert_model,
            expert_provider=expert.get("provider", "openai") if expert_model else None,
            asr_model=settings.asr_model,
            tts_model=settings.tts_model,
            tts_voice=settings.tts_voice,
        )

    def overridable(self):
        return {key: getattr(self, key) for key in OVERRIDABLE}

    def update(self, values):
        """Applies the values; returns whether any changed."""
        changed = any(getattr(self, key) != value for key, value in values.items())
        for key, value in values.items():
            setattr(self, key, value)
        return changed

    def as_dict(self):
        return {f.name: getattr(self, f.name) for f in fields(self)}


def read_model_overrides(defaults, path=MODEL_OVERRIDES):
    """The defaults with what the management page saved in gateway.conf on top."""
    values = dict(defaults)
    try:
        with open(path, encoding="utf-8") as source:
            env = dict(line.strip().split("=", 1) for line in source if "=" in line and not line.lstrip().startswith("#"))
    except OSError:
        return values
    for key, name in OVERRIDABLE.items():
        if name in env:
            values[key] = env[name]
    return values


@dataclass
class Preferences:
    """What the user changes on the screen or by voice; the display owns the settings file."""

    speech_rate: float
    thinking: bool
    mic: bool = True
    brightness: int | None = None

    @classmethod
    def load(cls, settings, path=UI_SETTINGS_FILE):
        prefs = cls(settings.speech_rate, settings.thinking)
        try:
            with open(path, encoding="utf-8") as source:
                saved = json.load(source)
        except (OSError, ValueError):
            return prefs
        if not isinstance(saved, dict):
            return prefs
        if "speech_rate" in saved:
            prefs.speech_rate = clamp_rate(saved["speech_rate"])
        if isinstance(saved.get("thinking"), bool):
            prefs.thinking = saved["thinking"]
        prefs.mic = saved.get("mic", True) is not False
        return prefs


def read_hardware_mic(path=MIC_STATE_FILE):
    """The privacy switch: True or False, or None when the key service has not reported it."""
    try:
        with open(path, encoding="ascii") as source:
            value = source.read(4).strip()
        return {"1": True, "0": False}.get(value)
    except OSError:
        return None
