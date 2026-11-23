"""Wake word spotting on two microphone streams: the echo-cancelled one, which ignores the device's own
voice, and the raw one, which still hears the user over loud music."""

import logging
import math
import os
import re
import select
import threading
import time
from contextlib import contextmanager

import sherpa_onnx

import offline
from audio import frame_level, open_recorder, pcm_to_floats, stop_process
from config import FRAME_BYTES, KEYWORDS_FILE, KWS_MODEL, RAW_CAPTURE_SOURCE, SAMPLE_RATE

# Allow the speaker echo and capture buffers to drain after the device says its own name.
RAW_CAPTURE_TAIL = 0.3
RAW_GAIN = 4.0
AEC_GAIN = 2.0


def create_keyword_spotter(settings, keywords_file=KEYWORDS_FILE):
    return sherpa_onnx.KeywordSpotter(
        **offline.model_paths(KWS_MODEL),
        keywords_file=keywords_file,
        num_threads=settings.kws_threads,
        sample_rate=SAMPLE_RATE,
        feature_dim=80,
        max_active_paths=4,
        keywords_score=settings.kws_score,
        keywords_threshold=settings.kws_threshold,
        provider="cpu",
    )


def music_keywords(phrase, settings):
    """Keywords for listening over music: the whole name, and its first two characters as a hint.
    None when the phrase is not two to eight Chinese characters."""
    if not re.fullmatch(r"[一-鿿]{2,8}", phrase):
        return None
    # Only a complete word may wake the assistant. A two-character prefix
    # just ducks the music unless two separate hits spell the repeated name.
    words = [phrase]
    if len(phrase) >= 4:
        words.append(phrase[:2])
    tokens = sherpa_onnx.text2token(words, tokens=f"{KWS_MODEL}/tokens.txt", tokens_type="ppinyin")
    score, threshold = settings.kws_score, settings.kws_threshold
    full = f"{' '.join(tokens[0])} :{score + 1:g} #{threshold * .64:g} @{phrase}"
    prefix = (f"{' '.join(tokens[1])} :{score + 2:g} #{threshold * .72:g} @music-prefix"
              if len(tokens) > 1 else None)
    return full, prefix


class WakeWord:
    """The wake phrase, its spotter, and the guard that keeps the raw microphone from waking on the
    device saying its own name."""

    def __init__(self, phrase, spotter, music_keywords=None):
        self.phrase = phrase
        self.spotter = spotter
        self.music_keywords = music_keywords
        self.raw_muted_until = 0.0

    def raw_allowed(self):
        return time.monotonic() >= self.raw_muted_until

    @contextmanager
    def guard(self, guarded):
        """Yields the callback to run when the guarded text starts playing, or None when unguarded."""
        def on_play():
            self.raw_muted_until = math.inf
        try:
            yield on_play if guarded else None
        finally:
            if guarded and self.raw_muted_until == math.inf:
                self.raw_muted_until = time.monotonic() + RAW_CAPTURE_TAIL


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
        full = self.full.feed(samples, gain=RAW_GAIN)
        if full:
            return full
        partial = self.prefix.feed(samples, gain=RAW_GAIN) if self.prefix else ""
        if partial != "music-prefix":
            return partial
        gap = (self.frames - self.last_prefix) * FRAME_BYTES / (2 * SAMPLE_RATE)
        self.last_prefix = self.frames
        player.make_room_for_voice()
        logging.info("音乐中听到唤醒词开头，短暂降低播放音量")
        if self.repeated and 0.24 <= gap <= 2.0:
            self.last_prefix = -math.inf
            return self.phrase
        return ""


class WakeDetector:
    """Reads the echo-cancelled capture frame by frame and keeps a separate, continuous raw stream.

    mic supplies the capture statistics (progress, levels, dropped frames); session, music and
    alarms decide which stream may wake the assistant."""

    def __init__(self, mic, session, wake_word, music, alarms, open_raw=open_recorder):
        self.mic = mic
        self.session = session
        self.wake_word = wake_word
        self.music = music
        self.alarms = alarms
        self.open_recorder = open_raw
        self.spotter = wake_word.spotter
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
        self.aec_enabled = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close_raw()

    def open_raw(self):
        if self.raw_capture is None and time.monotonic() >= self.raw_retry_at:
            try:
                self.raw_capture = self.open_recorder(RAW_CAPTURE_SOURCE)
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
            threading.Thread(target=stop_process, args=(capture,), name="raw-capture-close", daemon=True).start()

    def read_raw(self, allowed):
        capture = self.raw_capture
        if capture is None:
            return ""
        mic = self.mic
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
                mic.raw_progress = time.monotonic()
            elif time.monotonic() - max(self.raw_started, mic.raw_progress) > 2:
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
                mic.raw_dropped_frames += drop // FRAME_BYTES
                del self.raw_buffer[:drop]
                self.raw_stream = WakeStream(self.spotter)
            player = self.music.player
            keywords = self.wake_word.music_keywords
            if self.session.phase != "idle" or player.state != "playing" or self.alarms.ringing:
                if self.music_wake is not None:
                    self.music_wake = None
                    self.raw_stream = WakeStream(self.spotter)
            elif keywords and (self.music_wake is None or self.music_generation != player.generation):
                self.music_wake = MusicWake(self.spotter, keywords, self.wake_word.phrase)
                self.music_generation = player.generation
            while len(self.raw_buffer) >= FRAME_BYTES:
                frame = bytes(self.raw_buffer[:FRAME_BYTES])
                del self.raw_buffer[:FRAME_BYTES]
                mic.levels["raw"] = frame_level(frame)
                samples = pcm_to_floats(frame)
                if self.music_wake is not None:
                    detected = self.music_wake.feed(samples, player)
                else:
                    detected = self.raw_stream.feed(samples, gain=RAW_GAIN)
                result = result or detected
            return result
        except (OSError, RuntimeError, ValueError) as error:
            logging.warning("读取原始麦克风流失败: %s", error)
            self.close_raw()
            return ""

    def decode(self, stream, data, gain=1.0):
        return stream.feed(pcm_to_floats(data), gain)

    def poll(self, capture):
        """Waits up to 0.25 s for audio. Returns (frame, wake word, source): the next complete
        echo-cancelled frame or None, the wake word heard or "", and which stream heard it."""
        session = self.session
        if session.mic_enabled.is_set():
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
        if frame is None and time.monotonic() - self.mic.progress > 4:
            raise RuntimeError("microphone stream timed out")

        wake_enabled = session.mic_enabled.is_set() and session.phase not in ("wake", "listen")
        if self.aec_enabled and not wake_enabled:
            self.stream = WakeStream(self.spotter)
        self.aec_enabled = wake_enabled
        aec = self.decode(self.stream, frame, gain=AEC_GAIN) if frame and wake_enabled else ""
        if aec and (self.music.player.state == "playing" or session.phase == "speak" or self.alarms.ringing):
            self.read_raw(False)
            return frame, aec, "AEC"
        raw = self.read_raw(wake_enabled and self.wake_word.raw_allowed())
        if aec and not raw:
            return frame, aec, "AEC"
        return frame, raw, "raw"
