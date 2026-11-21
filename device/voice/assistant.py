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
from contextlib import closing, contextmanager
from collections import deque
from itertools import chain, groupby

import dashscope
import sherpa_onnx

from brain import Brain
from playback import PCMPlayer, SpeechProgress, AudioStalled
from music import (
    MUSIC_ROLE,
    MusicService,
    read_volume_status,
    CommandCancelled as TurnCancelled,
)
import information
import memory
import offline
import skills
from alarms import AlarmService
from skills import Result


SAMPLE_RATE = 16000
RAW_CAPTURE_TAIL = 0.3  # Allow the speaker echo and capture buffers to drain.
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
# A shared ASR/TTS socket idle longer than this is replaced at the next wake, in case it died silently.
LINK_IDLE_SECONDS = 60
# A soft cue when a turn has been silent this long, so a slow answer does not sound like a hang.
THINKING_CUE_SECONDS = float(os.getenv("THINKSMART_THINKING_CUE_SECONDS", "2"))
ASSISTANT_ROLE = "Assistant"
# Recognition ends sentences at short pauses, sometimes before the pause. Listening stops once the
# user has been quiet this long after a sentence that sounds finished, or after one ending in a
# comma or connective.
SPEECH_PAUSE_SECONDS = 1.0
UNFINISHED_PAUSE_SECONDS = 2.0
UNFINISHED = re.compile(
    r"(?:[，、,：:；;]|然后|还有|而且|并且|或者|以及|另外|接着|因为|所以|但是|不过|如果|要是|比如|就是|还是"
    r"|那么|那个|这个|那|和|跟|与|把|给|让|帮我|我想|我要|嗯|呃|额)[。.！!]*$"
)
# Follow-up windows hear the room: a lone syllable or filler is noise unless the reply asked something.
FILLER = re.compile(r"[嗯啊呃额哦噢喔唉哎诶欸哈呵]+")
QUESTION_END = re.compile(r"(?:[？?]|[吗呢么][。！!～~]?)[\s”」』\"')）]*$")
# Time is already in the context, so time turns skip web search.
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
TOOL_RULES = (
    "你拥有以下设备本地技能（Skills）：" + skills.catalogue() + "；另外可以联网搜索。"
    "设备当前的实时状态（当前音量、是否静音、屏幕亮度、语速、深度思考开关、闹钟列表、正在播放的歌曲与伴睡定时、QQ音乐登录状态、默认城市）附在每轮用户消息前的【设备实时状态】中，是你的系统信息而不是用户说的话；"
    "当用户询问现在音量多少、亮度多少、语速多少、在放什么歌、有哪些闹钟、你有哪些本地功能时，请直接根据上下文准确回答，绝不要说无法查询。"
    "当用户表达不适或隐含调节意图（如“你说话声音好小”“太吵了”“屏幕太刺眼/太暗”“你说太快了”）时，不要反问“需要我调吗”，直接调用set_volume或control_device完成调节。"
    "用户只问现在几点时，只回答几点几分。"
    "使用工具时不要提前声称操作成功，以工具返回结果为准。"
    "新闻、比赛、行情等需要最新信息的问题可以参考联网搜索结果，用一两句话概括，不要念出网址。"
    "回答结束时不要说“还有什么需要”之类的客套追问；只有确实需要用户补充信息时才用问句结尾。"
    "【设备实时状态】里附有用户让你长期记住的事，相关时自然地用上（如称呼、口味、家人），不要逐条复述。"
)
# Spoken with the local voice when a turn fails before any reply, so the device is never silently stuck.
APOLOGIES = {"network": "网络好像断了，请稍后再试。", "auth": "语音服务的密钥失效了，请在管理页检查。",
             "config": "语音服务还没配置好，请在管理页检查。"}
# The turn a dismissed morning alarm starts; its words make the model call daily_briefing.
BRIEFING_TEXT = "（闹钟刚被我关掉）早安"
BRIEFING_WAIT_SECONDS = 4
# A wake this long after the last turn starts a new conversation; follow-up turns never clear it.
# Shorter gaps keep the history, and the model is told how long ago it was.
CONTEXT_IDLE_SECONDS = float(os.getenv("THINKSMART_CONTEXT_IDLE_MINUTES", "120")) * 60
CONTEXT_GAP_SECONDS = 120

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


SPOKEN_CONTENT = re.compile(r"[^\s，。！？、,.!?：:；;…]")


def wake_word_parts(fragments, phrase):
    """Yield (is_wake_word, text), keeping only an unfinished match between chunks."""
    prefixes = [r"\s*".join(map(re.escape, phrase[:n])) for n in range(1, len(phrase) + 1)]
    word = re.compile("(" + prefixes[-1] + ")")
    suffix = re.compile("(?:" + "|".join(prefixes[:-1]) + r")\s*\Z") if len(prefixes) > 1 else None
    pending = ""
    for fragment in fragments:
        parts = word.split(pending + fragment)
        rest = parts.pop()
        match = suffix.search(rest) if suffix else None
        end = match.start() if match else len(rest)
        pending = rest[end:]
        for index, text in enumerate(parts):
            if text:
                yield bool(index % 2), text
        if end:
            yield False, rest[:end]
    if pending:
        yield False, pending


def speech_segments(fragments, phrase):
    # Stream ordinary text in a single TTS session; drain each wake word independently.
    for guarded, group in groupby(wake_word_parts(fragments, phrase), key=lambda part: part[0]):
        if guarded:
            for _, word in group:
                yield True, (word,)
        else:
            yield False, (text for _, text in group)


class WakeStream:
    def __init__(self, spotter, keywords=None):
        self.spotter = spotter
        self.stream = spotter.create_stream(keywords)

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


class MusicWake:
    """Listen for the start of the wake word while the device plays a song."""

    def __init__(self, spotter, keywords, phrase):
        full, prefix = keywords
        self.full = WakeStream(spotter, full)
        self.prefix = WakeStream(spotter, prefix) if prefix else None
        self.phrase = phrase
        self.repeated = phrase[:2] * 2 == phrase
        self.frames = 0
        self.last_prefix = -math.inf

    def feed(self, samples, player):
        self.frames += 1
        full = self.full.feed(samples, gain=4.0)
        partial = self.prefix.feed(samples, gain=4.0) if self.prefix else ""
        if full or partial != "music-prefix":
            return full or partial
        gap = (self.frames - self.last_prefix) * FRAME_BYTES / (2 * SAMPLE_RATE)
        self.last_prefix = self.frames
        player.make_room_for_voice()
        logging.info("音乐中听到唤醒词开头，短暂降低播放音量")
        if self.repeated and 0.24 <= gap <= 2.0:
            self.last_prefix = -math.inf
            return self.phrase
        return ""

    @staticmethod
    def keywords(phrase):
        if not re.fullmatch(r"[\u4e00-\u9fff]{2,8}", phrase):
            return None
        # Only a complete word may wake the assistant. A two-character prefix
        # just ducks the music unless two separate hits spell the repeated name.
        words = [phrase]
        if len(phrase) >= 4:
            words.append(phrase[:2])
        tokens = sherpa_onnx.text2token(words, tokens=f"{KWS_MODEL}/tokens.txt", tokens_type="ppinyin")
        score = float(os.getenv("THINKSMART_KWS_SCORE", "1")) + 2
        threshold = float(os.getenv("THINKSMART_KWS_THRESHOLD", "0.25")) * 0.72
        params = f":{score:g} #{threshold:g}"
        full = f"{' '.join(tokens[0])} {params} @{phrase}"
        prefix = f"{' '.join(tokens[1])} {params} @music-prefix" if len(tokens) > 1 else None
        return full, prefix


class VoiceActivity:
    """Energy detector on the echo-cancelled microphone. Recognition reports only whole
    sentences, so this is what tells that the user is still, or again, talking."""

    def __init__(self):
        self.levels = deque(maxlen=50)  # 2 s: the quietest frame is the room, even mid-speech
        self.run = 0
        self.speech_at = 0.0  # last frame of speech

    def feed(self, data, now):
        samples = array.array("h", data)
        if sys.byteorder == "big":
            samples.byteswap()
        rms = math.sqrt(sum(v * v for v in samples) / max(1, len(samples)))
        self.levels.append(rms)
        if rms > max(3 * min(self.levels), 250):
            self.run += 1
            if self.run >= 3:  # 120 ms
                self.speech_at = now
        else:
            self.run = 0


class WakeDetector:
    """Keep AEC recognition and a separate, continuous raw-mic stream."""

    def __init__(self, assistant):
        self.assistant = assistant
        self.spotter = assistant.keyword_spotter
        self.stream = WakeStream(self.spotter)
        self.raw_capture = None
        self.raw_stream = None
        self.music_wake = None
        self.music_generation = None
        self.aec_buffer = bytearray()
        self.raw_buffer = bytearray()
        self.raw_retry_at = 0.0
        self.raw_started = 0.0
        self.raw_enabled = True

    def __enter__(self):
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

    def close_raw(self, retry=True):
        capture, self.raw_capture = self.raw_capture, None
        self.raw_stream = self.music_wake = None
        self.raw_buffer.clear()
        self.raw_retry_at = time.monotonic() + 5 if retry else 0
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
                self.music_wake = None
                self.raw_buffer.clear()
                if self.raw_enabled:
                    self.raw_stream = WakeStream(self.spotter)
                self.raw_enabled = False
                return ""
            self.raw_enabled = True
            limit = FRAME_BYTES * 8
            if len(self.raw_buffer) > limit:
                drop = (len(self.raw_buffer) - limit + FRAME_BYTES - 1) // FRAME_BYTES * FRAME_BYTES
                self.music_wake = None
                self.assistant.raw_dropped_frames += drop // FRAME_BYTES
                del self.raw_buffer[:drop]
                self.raw_stream = WakeStream(self.spotter)
            a = self.assistant
            player = a.music.player
            if a.phase != "idle" or player.state != "playing" or a.alarms.ringing:
                if self.music_wake is not None:
                    self.music_wake = None
                    self.raw_stream = WakeStream(self.spotter)
            elif a.music_wake_keywords and (self.music_wake is None or self.music_generation != player.generation):
                self.music_wake = MusicWake(self.spotter, a.music_wake_keywords, a.wake_phrase)
                self.music_generation = player.generation
            while len(self.raw_buffer) >= FRAME_BYTES:
                frame = bytes(self.raw_buffer[:FRAME_BYTES])
                del self.raw_buffer[:FRAME_BYTES]
                self.assistant.capture_levels["raw"] = self.assistant.frame_level(frame)
                samples = a.pcm_to_floats(frame)
                if self.music_wake is not None:
                    detected = self.music_wake.feed(samples, player)
                else:
                    detected = self.raw_stream.feed(samples, gain=4.0)
                result = result or detected
            return result
        except (OSError, RuntimeError, ValueError) as error:
            logging.warning("读取原始麦克风流失败: %s", error)
            self.close_raw()
            return ""

    def decode(self, stream, data, gain=1.0):
        samples = self.assistant.pcm_to_floats(data)
        return stream.feed(samples, gain)

    def poll(self, capture):
        assistant = self.assistant
        if assistant.mic_enabled.is_set():
            self.open_raw()
        elif self.raw_capture is not None:
            self.close_raw(retry=False)

        pipes = [capture.stdout]
        if self.raw_capture is not None:
            pipes.append(self.raw_capture.stdout)
        ready, _, _ = select.select(pipes, [], [], 0.25)
        frame = None
        if capture.stdout in ready:
            chunk = os.read(capture.stdout.fileno(), FRAME_BYTES - len(self.aec_buffer))
            if not chunk:
                raise RuntimeError("microphone stream ended")
            self.aec_buffer.extend(chunk)
            if len(self.aec_buffer) == FRAME_BYTES:
                frame = bytes(self.aec_buffer)
                self.aec_buffer.clear()
        if frame is None and time.monotonic() - assistant.capture_progress > 4:
            raise RuntimeError("microphone stream timed out")

        raw = self.read_raw(time.monotonic() >= assistant.raw_muted_until)
        aec = self.decode(self.stream, frame, gain=2.0) if frame and assistant.mic_enabled.is_set() else ""
        if aec and (not raw or assistant.music.player.state == "playing"
                    or assistant.phase == "speak" or assistant.alarms.ringing):
            return frame, aec, "AEC"
        return frame, raw, "raw"


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
                      "alarm_active": False, "alarm_text": "", "alarms": None, "alarm_items": []}
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
            if "items" in fields:
                state["alarm_items"] = fields.get("items") or []
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
            # The assistant's own Pulse streams carry no role, and WirePlumber
            # would remember a ducked volume for every later reply.
            if (
                props.get("media.class") == "Stream/Output/Audio"
                and props.get("media.role") not in (ASSISTANT_ROLE, MUSIC_ROLE)
                and "node.link-group" not in props
                and str(props.get("application.process.id")) != str(os.getpid())
                and props.get("application.name") != "ThinkSmart Assistant"
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
        self.brightness = None
        self.load_ui_prefs()
        self.thinking_budget = int(os.getenv("THINKSMART_THINKING_BUDGET", "1024"))
        self.followup_seconds = float(os.getenv("THINKSMART_FOLLOWUP_SECONDS", "4"))
        self.question_followup_seconds = float(os.getenv("THINKSMART_QUESTION_FOLLOWUP_SECONDS", "8"))
        self.turn_effects = []
        self.failed_tools = set()
        self.next_followup = 0.0
        self.ducker = Ducker(float(os.getenv("THINKSMART_DUCK_LEVEL", "0.45")))
        self.music = MusicService(ui.emit, float(os.getenv("THINKSMART_MUSIC_DUCK_LEVEL", "0.2")), self.check_cancel)
        self.alarms = AlarmService(ui.emit, on_ring=self.on_alarm_ring, music=self.music)
        self.offline_commands = offline.OfflineCommands(KWS_MODEL, f"{VOICE_ROOT}/offline-keywords.txt")
        self.memory = memory.Memory()
        self.skills = skills.Skills(self, self.music, self.alarms, information, self.memory)
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
        self.raw_dropped_frames = 0
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
        self.last_wake_mono = -math.inf
        self.link = None
        self.link_used = 0.0
        self.link_lock = threading.Lock()
        self.speech_player = None
        self.heard_reply = ""
        self.wake_phrase = self.read_wake_phrase()
        self.keyword_spotter = self.create_keyword_spotter()
        self.music_wake_keywords = MusicWake.keywords(self.wake_phrase)
        self.phase = "idle"
        self.phase_lock = threading.Lock()
        self.cue_timer = None
        self.followup_question = False
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
        self.raw_muted_until = 0.0
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
            "tools": skills.tools(),
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
            tmp_path = path + '.tmp'
            with open(tmp_path, 'wb') as target:
                target.write(output.getvalue())
            os.replace(tmp_path, path)

            def _remove_preview(p=path):
                try:
                    os.unlink(p)
                except OSError:
                    pass

            cleanup = threading.Timer(60, _remove_preview)
            cleanup.daemon = True
            cleanup.start()
            self.control_reply(sock, sender, message, True, 'ready')
        except Exception:
            logging.exception('试听生成失败')
            self.control_reply(sock, sender, message, False, '试听生成失败，请检查语音服务或稍后重试')
        finally:
            self.preview_lock.release()

    @staticmethod
    def clamp_rate(value):
        try:
            rate = float(value)
        except (TypeError, ValueError):
            return 1.5
        return round(min(2.0, max(0.5, rate)), 1) if math.isfinite(rate) else 1.5

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

    def set_phase(self, phase):
        with self.phase_lock:
            self.phase = phase
            if phase in ("idle", "think") and not self.barge_in_requested.is_set():
                self.clear_audio()
            if self.cue_timer is not None:
                self.cue_timer.cancel()
                self.cue_timer = None
            if phase == "think" and THINKING_CUE_SECONDS > 0:
                self.cue_timer = threading.Timer(THINKING_CUE_SECONDS, self.thinking_cue)
                self.cue_timer.daemon = True
                self.cue_timer.start()
        self.ui.emit(phase)

    def thinking_cue(self):
        if self.phase == "think" and not self.cancel_requested.is_set():
            self.play_cue(self._THINKING_TONE)

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
        # Alarms and reminders replace what was playing with their own music; timers pause it.
        if ringing:
            self.music.hold()
        elif self.phase == "idle":
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
                elif command == "alarm_snooze":
                    self.alarms.snooze(message.get("minutes") or 5)
                elif command == "alarm_cmd":
                    self.alarms.ui_command(
                        message.get("a"),
                        id=message.get("id"),
                        minutes=message.get("minutes"),
                        label=message.get("label"),
                    )
                elif command == "mic":
                    self.set_mic(bool(message.get("on")))
                elif command == "hardware_mic":
                    self.refresh_hardware_mic()
                elif command == "hello":
                    self.announce()
                elif command == "snapshot":
                    music_snap = self.music.player.snapshot()
                    alarms_snap = {"next": self.alarms.next_summary(), "items": self.alarms.list_items()}
                    with self.ui.lock:
                        self.ui._record("music", music_snap)
                        self.ui._record("alarms", alarms_snap)
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
                              "raw_dropped_frames": self.raw_dropped_frames,
                              "timing": dict(self.turn_timing),
                              "wake_detection": {"count": self.wake_hits, "last": self.last_wake},
                              "preferences": {"speech_rate": self.speech_rate, "thinking": self.thinking,
                                              "mic": self.mic_requested},
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
                    if type(message.get("brightness")) is int:
                        self.brightness = message["brightness"]
                    logging.info("语速 %.1f，思考模式%s", self.speech_rate, "开启" if self.thinking else "关闭")
                elif command == "music":
                    self.music.ui_command(message.get("a"), minutes=message.get("minutes"))
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

    def apologize(self, error):
        code = classify_error(error)
        if self.phase not in ("think", "speak") or self.heard_reply or code in ("mic", "speaker"):
            return
        try:
            offline.speak(APOLOGIES.get(code, "抱歉，刚才没处理好，请再说一次。"),
                          lambda: self.stop_requested or self.barge_in_requested.is_set())
        except Exception as warning:
            logging.warning("错误提示播报失败：%s", warning)

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
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass

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
        capture = self.create_capture(ECHO_CANCEL_SOURCE)
        self.capture = capture
        self.capture_progress = time.monotonic()
        self.capture_clock_start = self.capture_progress
        self.capture_clock_bytes = 0
        self.capture_clock = None
        self.bad_capture_windows = 0
        self.capture_thread = threading.Thread(target=self.capture_loop, args=(capture,), name="microphone", daemon=True)
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
            stderr=subprocess.DEVNULL,
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

    def track_capture(self, frame):
        self.capture_levels["aec"] = self.frame_level(frame)
        self.capture_progress = time.monotonic()
        self.capture_clock_bytes += len(frame)
        elapsed = self.capture_progress - self.capture_clock_start
        if elapsed >= 5:
            seconds = self.capture_clock_bytes / (SAMPLE_RATE * 2)
            ratio = seconds / elapsed
            self.capture_clock = {"audio_seconds": round(seconds, 2), "wall_seconds": round(elapsed, 2),
                                  "ratio": round(ratio, 3)}
            self.bad_capture_windows = self.bad_capture_windows + 1 if ratio < .9 else 0
            if ratio < .9:
                logging.warning("收音数据不连续：经过%.2f秒，收到%.2f秒音频", elapsed, seconds)
            self.capture_clock_start = self.capture_progress
            self.capture_clock_bytes = 0

    def accept_wake(self, result, source, tail):
        now = time.monotonic()
        if (self.phase in ("wake", "listen") or self.barge_in_requested.is_set()
                or now - self.last_wake_mono < 2):
            return
        self.last_wake_mono = now
        if self.alarms.ringing:
            self.alarms.dismiss()
        self.wake_hits += 1
        self.last_wake = {"at": time.time(), "source": source}
        self.wake_started = now
        self.turn_timing = {"wake_at": self.last_wake["at"], "source": source}
        self.clear_audio()
        for frame in tail:
            self.queue_audio(frame)
        self.screen_off_pending = False
        if self.phase == "idle":
            self.set_phase("wake")
            self.talk_requested.set()
        else:
            self.barge_in_requested.set()
            self.cancel_requested.set()
        logging.info("%s检测到唤醒词：%s (%s)", self.phase, result, source)

    def capture_loop(self, capture=None):
        capture = capture or self.capture
        if capture is None:
            return
        tail = deque(maxlen=8)
        try:
            with WakeDetector(self) as detector:
                while not self.capture_stop.is_set() and not self.stop_requested:
                    frame, result, source = detector.poll(capture)
                    if frame:
                        self.track_capture(frame)
                    if not self.mic_enabled.is_set():
                        tail.clear()
                        continue
                    if result:
                        self.accept_wake(result, source, tail)
                    if frame:
                        tail.append(frame)
                        if self.phase in ("wake", "listen") or self.barge_in_requested.is_set():
                            self.queue_audio(frame)
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
            if not self.alarms.ringing and self.alarms.take_briefing():
                self.wake_started = time.monotonic()
                self.turn_timing = {"wake_at": time.time(), "source": "briefing"}
                self.set_phase("wake")
                return "briefing"
            self.check_cancel()
            if not self.capture_thread.is_alive():
                raise RuntimeError("microphone stream ended")
        return False

    def start_listening(self):
        """Every entry into listening: the cue means "you can speak now"; speech over it is kept.

        The cue plays while recognition connects; audio has been queued since the wake word."""
        self.set_phase("listen")
        threading.Thread(target=self.play_listen_tone, name="listen-tone", daemon=True).start()

    @staticmethod
    def _build_listen_tone(level, pitch=660, length=0.32):
        attack, decay = 0.015, 0.085
        samples = array.array("h")
        for index in range(int(PLAYBACK_RATE * length)):
            t = index / PLAYBACK_RATE
            if t < attack:
                envelope = 0.5 - 0.5 * math.cos(math.pi * t / attack)
            else:
                envelope = math.exp(-(t - attack) / decay) * min(1.0, (length - t) / 0.02)
            tone = math.sin(2 * math.pi * pitch * t) + 0.25 * math.sin(2 * math.pi * 2 * pitch * t)
            samples.append(int(32767 * level * envelope * tone / 1.25))
        if sys.byteorder == "big":
            samples.byteswap()
        return samples.tobytes()

    _LISTEN_TONE = _build_listen_tone.__func__(0.13)
    _THINKING_TONE = _build_listen_tone.__func__(0.06, 440, 0.22)
    _OK_TONE = _build_listen_tone.__func__(0.10, 880, 0.18)

    def play_listen_tone(self):
        # Pre-rendered soft bell: smooth 15 ms attack, natural decay, zero per-wake math overhead.
        self.play_cue(self._LISTEN_TONE)

    @staticmethod
    def play_cue(pcm):
        try:
            player = PCMPlayer(PLAYBACK_RATE)
            try:
                player.write(pcm)
                player.finish(lambda: None)
            finally:
                player.close()
        except Exception as error:
            logging.warning("提示音播放失败：%s", error)

    def audio_link(self, refresh=False):
        """The ASR/TTS websocket shared across turns, so a turn skips the TLS and websocket handshake."""
        with self.link_lock:
            link = self.link
            if link is None or link.closed or (refresh and time.monotonic() - self.link_used > LINK_IDLE_SECONDS):
                self.link = self.client.ws_connection(module="audio", timeout=8)
                if link is not None:
                    threading.Thread(target=link.close, name="link-close", daemon=True).start()
            self.link_used = time.monotonic()
            return self.link

    def drop_link(self):
        with self.link_lock:
            link, self.link = self.link, None
        if link is not None:
            threading.Thread(target=link.close, name="link-close", daemon=True).start()

    def prepare_link(self):
        try:
            self.audio_link(refresh=True)
        except Exception as error:
            logging.warning("语音连接预建失败：%s", error)

    def open_audio_task(self, factory):
        """Starts a recognition or synthesis task on the shared socket, else on a socket of its own."""
        try:
            return factory(self.audio_link()).start()
        except TurnCancelled:
            raise
        except Exception as error:
            logging.warning("共享语音连接不可用，改用独立连接：%s", error)
            self.drop_link()
        return factory(None).start()

    def transcribe_live(self, wait_seconds=None):
        """Recognizes one utterance, which may span several sentences when the user pauses mid-thought."""
        state = {"sentences": [], "partial": ""}

        def heard():
            # A sentence the user went on from ends in a comma, not a full stop.
            parts = [text.rstrip("。.") + ("" if text[-1] in "？?！!，," else "，") for text in state["sentences"][:-1]]
            return "".join(parts + state["sentences"][-1:]) + state["partial"]

        def receive_events(recognition):
            for event in recognition.events():
                payload = event.get("payload") or {}
                sentence = (payload.get("output") or {}).get("sentence") or {}
                text = (sentence.get("text") or "").strip()
                if text and not sentence.get("heartbeat") and not self.cancel_requested.is_set():
                    if not state["sentences"] and self.wake_started is not None:
                        self.turn_timing.setdefault("first_text_ms", round((time.monotonic() - self.wake_started) * 1000))
                    if sentence.get("sentence_end"):
                        state["sentences"].append(text)
                        state["partial"] = ""
                    else:
                        state["partial"] = text
                    self.ui.emit("partial", text=heard())

        if wait_seconds is None:
            wait_seconds = float(os.getenv("THINKSMART_SPEECH_WAIT_SECONDS", "8"))
        max_seconds = float(os.getenv("THINKSMART_MAX_SPEECH_SECONDS", "25"))
        with closing(self.open_audio_task(lambda link: self.client.audio.asr.recognize(
            model=self.asr_model,
            format="pcm",
            sample_rate=SAMPLE_RATE,
            timeout=max(10.0, wait_seconds + 5),
            connection=link,
        ))) as recognition:
            started = time.monotonic()
            if self.wake_started is not None:
                self.turn_timing["asr_ready_ms"] = round((started - self.wake_started) * 1000)
            voice = VoiceActivity()
            judged, pause, ended_at = 0, None, 0.0
            with ThreadPoolExecutor(max_workers=1) as pool:
                receiver = pool.submit(receive_events, recognition)
                stopped = False
                try:
                    self.check_cancel()
                    logging.info("开始接收语音")
                    while not self.stop_requested:
                        if self.cancel_requested.is_set():
                            raise TurnCancelled()
                        if receiver.done():
                            receiver.result()
                        frame = self.read_audio_bytes()
                        recognition.send_audio_frame(frame)
                        self.ui.emit("level", v=self.frame_level(frame))
                        now = time.monotonic()
                        voice.feed(frame, now)
                        if len(state["sentences"]) > judged:
                            judged, ended_at = len(state["sentences"]), now
                            pause = UNFINISHED_PAUSE_SECONDS if UNFINISHED.search(heard()) else SPEECH_PAUSE_SECONDS
                        # Talking on resets the pause; noise taken for speech brings no new sentence.
                        if judged and (now - voice.speech_at >= pause or now - ended_at > 10):
                            break
                        # Text comes only with a finished sentence, so once the user has been heard
                        # the wait for it lasts until they have been quiet long enough for it to arrive.
                        # The first half second is the wake word's tail, queued before listening.
                        talked = voice.speech_at > started + 0.5
                        if (not judged and now - started >= wait_seconds
                                and now - voice.speech_at > (UNFINISHED_PAUSE_SECONDS if talked else 0)):
                            break
                        if now - started >= wait_seconds + max_seconds:
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
        if len(state["sentences"]) > 1:
            logging.info("合并了%d句：%s", len(state["sentences"]), heard())
        return heard().strip()

    def device_status_summary(self):
        volume, muted = read_volume_status()
        volume_text = f"{volume}%（{'已静音' if muted else '未静音'}）" if volume is not None else "未知"
        brightness = f"{self.brightness}%" if self.brightness else "未知"
        return (
            f"设备状态：当前音量{volume_text}，屏幕亮度{brightness}，"
            f"播报语速{self.speech_rate:g}倍，深度思考{'开启' if self.thinking else '关闭'}。"
        )

    def control_device(self, args):
        """Works out the new values; the display process owns the settings file and backlight."""
        def number(key, default):
            value = args.get(key)
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default

        update, results = {}, []
        level, change = number("brightness", -1), number("brightness_change", 0)
        if 1 <= level <= 100 or change:
            value = round(max(1, min(100, level if 1 <= level <= 100 else (self.brightness or 80) + change)))
            self.brightness = update["brightness"] = value
            results.append(f"屏幕亮度已调到百分之{value}")
        rate, rate_change = number("speech_rate", 0), number("speech_rate_change", 0)
        if 0.5 <= rate <= 2.0 or abs(rate_change) >= 0.05:
            self.speech_rate = update["speech_rate"] = self.clamp_rate(
                rate if 0.5 <= rate <= 2.0 else self.speech_rate + rate_change)
            results.append(f"播报语速已调到{self.speech_rate:g}倍")
        thinking_mode = str(args.get("thinking_mode") or "").strip()
        if thinking_mode in ("on", "off"):
            self.thinking = update["thinking"] = thinking_mode == "on"
            results.append(f"深度思考模式已{'开启' if self.thinking else '关闭'}")
        city = str(args.get("weather_city") or "").strip()[:40]
        if city:
            try:
                places = information.ui_system.search_places(city)
            except Exception as error:
                logging.warning("城市查询失败：%s", error)
                places = None
            if places:
                place = places[0]
                update["location"] = {key: place[key] for key in ("name", "lat", "lon")}
                results.append(f"默认天气城市已设为{place['name']}")
            else:
                results.append(f"没有找到城市{city}" if places == [] else "城市查询暂时不可用")
        screen = str(args.get("screen") or "").strip()
        screens = {"home": "主页", "weather": "天气详情页", "alarms": "闹钟与计时器页面",
                   "settings": "设置页面", "music": "音乐设置页面"}
        if screen == "off":
            self.screen_off_pending = True
            results.append("好的，即将关闭屏幕")
        elif screen in screens:
            update["screen"] = screen
            results.append(f"已在屏幕上打开{screens[screen]}")
        if update:
            self.ui.emit("ui_set", **update)
        if not results:
            return Result("没有听懂要调整哪项设备设置", False)
        return Result("，".join(results), bool(update) or screen == "off")

    def end_conversation(self, args):
        self.finish_conversation(bool(args.get("screen_off")), bool(args.get("stop_music")))
        return Result("好的", True)

    def chat_context(self):
        scheduled = self.alarms.manage_alarm({"action": "list"}).message
        return (
            f"用户会用“{self.wake_phrase}”称呼你，这就是你的名字。"
            f"设备当前本地时间是{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}。"
            f"{information.current_time()}"
            f"设备默认天气城市：{information.home_city()}。"
            f"{self.device_status_summary()}"
            f"当前闹钟与计时计划：{scheduled}。"
            + self.music.describe()
            + self.memory.describe()
            + self.conversation_gap()
        )

    def conversation_gap(self):
        gap = time.monotonic() - self.brain.last_active
        if not self.brain.messages or gap < CONTEXT_GAP_SECONDS:
            return ""
        return (f"上一次对话结束于约{round(gap / 60)}分钟前：用户提到之前聊过的内容（如“刚才”“那首歌”“那家店”）时接着之前的对话回答，"
                "否则当作新话题，不要牵扯之前的内容。")

    def daily_briefing(self, args):
        weather = information.get_weather({}).message
        plan = self.alarms.today_plan()
        return Result(f"{information.current_time()}{weather}"
                      + (f"今天接下来的安排：{plan}。" if plan else "今天接下来没有闹钟或提醒。"), True)

    def check_cancel(self):
        self.last_progress = time.monotonic()
        if (self.cancel_requested.is_set() or self.stop_requested
                or (self.recovery_requested.is_set() and not self.recovery_in_progress and not self.alarms.ringing)):
            raise TurnCancelled()

    def run_tool(self, name, args, has_reply):
        """Returns (message, ok, final); final results are spoken as-is instead of going back to the model.

        A tool that fails twice in one turn ends it with its error rather than letting the model retry."""
        logging.info("工具调用：%s %s", name, json.dumps(args, ensure_ascii=False))
        message, ok, final = self.call_tool(name, args, has_reply)
        if not ok:
            final = final or name in self.failed_tools
            self.failed_tools.add(name)
        return message, ok, final

    def call_tool(self, name, args, has_reply):
        self.check_cancel()
        result, effect, final = self.skills.run(name, args, has_reply)
        if effect:
            self.turn_effects.append(effect)
        self.music_started = self.music_started or result.started
        return result.message, result.ok, final

    def speak_stream(self, fragments):
        self.heard_reply = ""
        for guarded, segment in speech_segments(fragments, self.wake_phrase):
            with self.guard_wake_word(guarded) as on_play:
                self.speak_segment(segment, on_play)
        return self.heard_reply.strip()

    def speak_segment(self, fragments, on_play=None):
        # A trailing punctuation-only segment needs no TTS request.
        fragments = iter(fragments)
        leading = ""
        for fragment in fragments:
            leading += fragment
            if SPOKEN_CONTENT.search(fragment):
                break
        else:
            self.ui.emit("delta", text=leading)
            self.heard_reply += leading
            return
        fragments = chain((leading,), fragments)
        player = PCMPlayer(PLAYBACK_RATE)
        progress = SpeechProgress(PLAYBACK_RATE)
        self.speech_player = player
        reply_parts = []
        completed = False
        try:
            with closing(self.open_audio_task(lambda link: self.client.audio.tts.connect(
                model=self.tts_model,
                voice=self.tts_voice,
                format="pcm",
                sample_rate=PLAYBACK_RATE,
                rate=self.speech_rate,
                parameters={"word_timestamp_enabled": True},
                connection=link,
            ))) as session:

                speaking_set = False

                def receive_audio():
                    nonlocal speaking_set
                    for event in session.events():
                        self.check_cancel()
                        if isinstance(event, bytes):
                            if not speaking_set:
                                if on_play:
                                    on_play()
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
            self.heard_reply += "".join(reply_parts) if completed else progress.heard(player.played_seconds)
            if not completed:
                # Binary audio has no task id: late audio of this synthesis must not reach the next one.
                self.drop_link()
                logging.info("播报停止在 %.2f 秒，已听到：%s", player.played_seconds, self.heard_reply)
        self.check_cancel()

    def wait_receiver(self, receiver, timeout):
        deadline = time.monotonic() + timeout
        while True:
            try:
                return receiver.result(timeout=0.25)
            except FutureTimeout:
                self.check_cancel()
                if time.monotonic() >= deadline:
                    raise

    def handle_turn(self, user_text, shown=True):
        self.turn_models = {'started_at': time.time(), 'chat_model': None, 'speech': None}
        self.heard_reply = ""
        self.local_result = ""
        self.model_turn = False
        try:
            self.answer_turn(user_text, shown)
        finally:
            if not self.model_turn:
                reply = self.heard_reply
                if self.cancel_requested.is_set() and reply:
                    reply += "（播报被打断，只听到了以上内容。）"
                if self.local_result and "action" in self.turn_effects:
                    reply += f"（设备已执行：{self.local_result}）"
                self.brain.note(user_text, reply)

    def answer_turn(self, user_text, shown=True):
        logging.info("用户：%s", user_text)
        compact = re.sub(r"[\s，。！？、,.!?~～…]+", "", user_text or "")
        if self.wake_phrase and compact == self.wake_phrase:
            if self.alarms.ringing:
                self.alarms.dismiss()
            self.play_listen_tone()
            self.followup_question = False
            self.next_followup = (BRIEFING_WAIT_SECONDS if self.alarms.briefing_at is not None
                                  else float(os.getenv("THINKSMART_SPEECH_WAIT_SECONDS", "8")))
            return
        stripped_text = user_text
        if self.wake_phrase and compact.startswith(self.wake_phrase):
            stripped_text = re.sub(
                r"^[\s，。！？、,.!?~～`]*" + re.escape(self.wake_phrase) + r"[\s，。！？、,.!?~～`]*",
                "",
                user_text,
            ).strip() or user_text
        if shown:
            self.ui.emit("user", text=user_text)
        self.set_phase("think")
        self.music_started = False
        self.conversation_over = False
        self.turn_effects = []
        self.failed_tools = set()
        self.next_followup = 0.0
        local = skills.match(stripped_text, self.music.active(), self.wake_phrase, self.offline_mode)
        if local:
            logging.info("本地指令：%s %s", local[0], json.dumps(local[1], ensure_ascii=False))
            reply, ok, _ = self.call_tool(*local, False)
            action = local[1].get("action")
            quiet = ("mute" not in local[1] if local[0] == "set_volume"
                     else local[0] == "control_music" and (action in ("pause", "next", "previous")
                                                           or action == "resume" and self.music_started))
            if ok and quiet:
                self.local_result = self.heard_reply = reply
                self.ui.emit("reply", text=reply)
                self.play_cue(self._OK_TONE)
                logging.info("助手（提示音）：%s", reply)
                return
        elif self.offline_mode:
            reply = skills.OFFLINE_HINT
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
        self.followup_question = bool(QUESTION_END.search(reply or ""))
        if self.followup_question:
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
        for guarded, segment in speech_segments((remaining,), self.wake_phrase):
            text = "".join(segment)
            if not SPOKEN_CONTENT.search(text):
                continue
            with self.guard_wake_word(guarded) as on_play:
                succeeded = offline.speak(
                    text, lambda: self.cancel_requested.is_set() or self.stop_requested,
                    on_play=on_play)
                self.check_cancel()
                if not succeeded:
                    raise RuntimeError("speaker local playback failed")
        self.heard_reply = reply

    @contextmanager
    def guard_wake_word(self, guarded):
        def on_play():
            self.raw_muted_until = math.inf
        try:
            yield on_play if guarded else None
        finally:
            if guarded and self.raw_muted_until == math.inf:
                self.raw_muted_until = time.monotonic() + RAW_CAPTURE_TAIL

    def cloud_available(self):
        try:
            with socket.create_connection((f"{self.workspace}.{self.region}.maas.aliyuncs.com", 443), timeout=2):
                return True
        except OSError:
            return False

    def chat_turn(self, user_text):
        self.heard_reply = ""
        spoken = []
        force_tool = skills.forced(user_text)
        search = not force_tool and not TIME_WORDS.search(user_text)
        expert = bool(self.expert_model and (
            EXPERT_REQUEST.search(user_text) or (self.thinking and HARD_TASK_WORDS.search(user_text))
        ))
        self.turn_models.update(chat_model=self.expert_model if expert else self.chat_model,
                                chat_provider=self.expert_provider if expert else self.chat_provider,
                                role='expert' if expert else 'chat')
        # Qwen rejects a forced tool choice while thinking.
        thinking = self.thinking and not force_tool and bool(THINK_WORDS.search(user_text))
        if expert:
            logging.info("本轮使用 %s，推理档位 high", self.expert_model)
        elif thinking:
            logging.info("本轮开启思考")

        model_failed = False

        def model_events():
            nonlocal model_failed
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
            compact = re.sub(r"[\s，。！？、,.!?~～…]+", "", text)
            if not text or not self.followup_question and (len(compact) < 2 or FILLER.fullmatch(compact)):
                logging.info("追问窗口结束%s", f"，忽略：{text}" if text else "")
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
        try:
            while not self.stop_requested:
                try:
                    if self.recovery_requested.is_set() and not self.alarms.ringing:
                        self.recover_audio()
                    self.cancel_requested.clear()
                    woke = True
                    if self.barge_in_requested.is_set():
                        self.set_phase("wake")
                        self.barge_in_requested.clear()
                    else:
                        self.set_phase("idle")
                        self.open_capture()
                        if self.screen_off_pending:
                            self.screen_off_pending = False
                            self.ui.emit("sleep")
                        woke = self.wait_for_wake()
                        if not woke:
                            break
                        self.brain.forget_if_idle(CONTEXT_IDLE_SECONDS)
                    threading.Thread(target=self.prepare_link, name="link-prepare", daemon=True).start()
                    self.duck()
                    self.begin_turn()
                    self.offline_mode = False
                    self.conversation_over = False
                    if woke == "briefing":
                        self.set_cloud("busy")
                        self.brief()
                    else:
                        self.converse()
                        if self.alarms.take_briefing():
                            self.brief()
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
                        self.apologize(error)
                        time.sleep(2)
                finally:
                    if not self.barge_in_requested.is_set():
                        self.unduck()
        finally:
            self.shutdown()

    def converse(self):
        # Keep the wake stream running through the tone so an immediate
        # follow-on command is still captured.
        self.start_listening()
        if self.stop_requested:
            return
        self.check_cancel()
        self.set_cloud("busy")
        try:
            # Waking only to turn off an alarm should not keep the briefing waiting long.
            text = self.transcribe_live(BRIEFING_WAIT_SECONDS if self.alarms.briefing_at is not None else None)
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

    def brief(self):
        """The day's briefing once a morning alarm is turned off, unless the user moved on to something else."""
        if self.offline_mode or self.conversation_over or self.music.active():
            return
        logging.info("闹钟已关闭，播报今日简报")
        self.handle_turn(BRIEFING_TEXT, shown=False)
        self.follow_up()

    def stop(self, *_args):
        # Signal handlers must stay async-signal-safe; cleanup runs in shutdown() from run().
        self.stop_requested = True

    def shutdown(self):
        self.stop_requested = True
        with self.phase_lock:
            if self.cue_timer is not None:
                self.cue_timer.cancel()
                self.cue_timer = None
        for action in (
            self.close_capture,
            self.drop_link,
            lambda: self.speech_player.close() if self.speech_player is not None else None,
            self.alarms.shutdown,
            self.music.shutdown,
            self.brain.close,
        ):
            try:
                action()
            except Exception:
                logging.exception("退出清理失败")


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
