"""PCM helpers, PipeWire recording, the short cue tones, and ducking other playback."""

import array
import json
import logging
import math
import os
import subprocess
import sys
import threading
import time

from config import ASSISTANT_ROLE, PLAYBACK_RATE, SAMPLE_RATE
from music import MUSIC_ROLE
from playback import PCMPlayer


def _samples(data):
    samples = array.array("h")
    samples.frombytes(data)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def frame_level(data):
    """Peak of an s16 frame, 0-100."""
    samples = _samples(data)
    if not samples:
        return 0.0
    peak = max(max(samples), -min(samples))
    return round(min(100.0, peak * 100 / 32768), 2)


def frame_rms(data):
    samples = _samples(data)
    return math.sqrt(sum(v * v for v in samples) / max(1, len(samples)))


def pcm_to_floats(data, gain=1.0):
    """Normalize one s16 frame for recognition, clipping amplified samples to [-1, 1]."""
    scale = gain / 32768.0
    samples = [value * scale for value in _samples(data)]
    if gain > 1.0:
        for i, value in enumerate(samples):
            if value > 1.0:
                samples[i] = 1.0
            elif value < -1.0:
                samples[i] = -1.0
    return samples


def open_recorder(source):
    """pw-cat recording 16 kHz mono s16 from one PipeWire source onto stdout."""
    command = ["/usr/bin/pw-cat", "--record", "--media-role", ASSISTANT_ROLE, "--rate", str(SAMPLE_RATE),
               "--channels", "1", "--format", "s16", "--latency", "100ms",
               # WirePlumber 0.4 otherwise falls back to the default (raw) microphone.
               "--target", source, "--properties", "node.dont-reconnect=true", "-"]
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
        # Keep recording through the wake tone and the 2-second network probe.
        pipesize=128 * 1024,
    )


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


def build_tone(level, pitch=660, length=0.32):
    """A soft bell: smooth 15 ms attack and a natural decay, rendered once at import."""
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


LISTEN_TONE = build_tone(0.13)  # "you can speak now"
THINKING_TONE = build_tone(0.06, 440, 0.22)  # a slow answer is still coming
OK_TONE = build_tone(0.10, 880, 0.18)  # a quiet command was carried out


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
            result = subprocess.run(["/usr/bin/pw-dump"], capture_output=True, timeout=5, check=True)
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
