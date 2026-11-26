"""The microphone: a capture thread that spots the wake word and queues audio for recognition, its
health statistics, and recovery of the PipeWire audio chain when capture stalls."""

import json
import logging
import os
import queue
import subprocess
import threading
import time
from collections import deque

from audio import frame_level, open_recorder, stop_process
from config import ECHO_CANCEL_SOURCE, RECOVERY_STATE_FILE, SAMPLE_RATE
from playback import AudioStalled
from shared import system
from wake import WakeDetector

QUEUE_FRAMES = 200  # 8 s of 40 ms frames
WAKE_TAIL_FRAMES = 8  # audio kept from just before a wake, so a command said right after it is heard
CLOCK_WINDOW_SECONDS = 5
# Capture delivering less than this share of real time is broken even while it still flows.
MIN_CLOCK_RATIO, MAX_CLOCK_RATIO = .9, 1.15


def boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as source:
            return source.read().strip()
    except OSError:
        return ""


def clock_ok(clock):
    return clock is not None and MIN_CLOCK_RATIO <= clock["ratio"] <= MAX_CLOCK_RATIO


class Microphone:
    """Owns the echo-cancelled capture process and the thread reading it.

    on_wake(result, source, tail) runs on the capture thread for each wake word heard; on_failure(reason)
    when capture keeps failing. While the session listens, frames go to a queue for read()."""

    def __init__(self, session, wake_word, music, alarms, on_wake, on_failure):
        self.session = session
        self.wake_word = wake_word
        self.music = music
        self.alarms = alarms
        self.on_wake = on_wake
        self.on_failure = on_failure
        self.process = None
        self.thread = None
        self.stop = threading.Event()
        self.frames = queue.Queue(maxsize=QUEUE_FRAMES)
        self.overflow = threading.Event()
        self.progress = time.monotonic()
        self.clock_start = self.progress
        self.clock_bytes = 0
        self.clock = None
        self.raw_progress = 0.0
        self.levels = {"raw": 0.0, "aec": 0.0}
        self.raw_dropped_frames = 0
        self.dropped_frames = 0
        self.bad_windows = 0
        self.failures = deque()

    def alive(self):
        return self.thread is not None and self.thread.is_alive()

    def open(self):
        if self.process is not None and self.alive():
            return
        self.close()
        self.stop.clear()
        process = open_recorder(ECHO_CANCEL_SOURCE)
        self.process = process
        self.progress = time.monotonic()
        self.clock_start = self.progress
        self.clock_bytes = 0
        self.clock = None
        self.bad_windows = 0
        self.thread = threading.Thread(target=self._loop, args=(process,), name="microphone", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        stop_process(self.process)
        if self.thread is not None:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise AudioStalled("microphone shutdown stalled")
        self.thread = None
        self.process = None
        self.clear()

    def clear(self):
        self.overflow.clear()
        while not self.frames.empty():
            try:
                self.frames.get_nowait()
            except queue.Empty:
                break

    def put(self, data):
        try:
            self.frames.put_nowait(data)
        except queue.Full:
            self.overflow.set()
            self.dropped_frames += 1
            try:
                self.frames.get_nowait()
            except queue.Empty:
                pass
            self.frames.put_nowait(data)

    def restart_queue(self, frames):
        """Drops what was queued and starts again from these frames."""
        self.clear()
        for frame in frames:
            self.put(frame)

    def read(self):
        """The next queued frame, while the turn is not cancelled."""
        while True:
            self.session.check_cancel()
            if self.overflow.is_set():
                raise RuntimeError("microphone command buffer overflow")
            try:
                return self.frames.get(timeout=0.1)
            except queue.Empty:
                if not self.alive():
                    raise RuntimeError("microphone stream ended")

    def ready(self):
        return bool(self.alive() and not self.stop.is_set()
                    and time.monotonic() - self.progress < 1 and clock_ok(self.clock))

    def stalled(self, seconds):
        return self.alive() and not self.stop.is_set() and time.monotonic() - self.progress > seconds

    def _track(self, frame):
        self.levels["aec"] = frame_level(frame)
        self.progress = time.monotonic()
        self.clock_bytes += len(frame)
        elapsed = self.progress - self.clock_start
        if elapsed >= CLOCK_WINDOW_SECONDS:
            seconds = self.clock_bytes / (SAMPLE_RATE * 2)
            ratio = seconds / elapsed
            self.clock = {"audio_seconds": round(seconds, 2), "wall_seconds": round(elapsed, 2),
                          "ratio": round(ratio, 3)}
            self.bad_windows = self.bad_windows + 1 if ratio < MIN_CLOCK_RATIO else 0
            if ratio < MIN_CLOCK_RATIO:
                logging.warning("收音数据不连续：经过%.2f秒，收到%.2f秒音频", elapsed, seconds)
            self.clock_start = self.progress
            self.clock_bytes = 0

    def _loop(self, process):
        session = self.session
        tail = deque(maxlen=WAKE_TAIL_FRAMES)
        try:
            with WakeDetector(self, session, self.wake_word, self.music, self.alarms) as detector:
                while not self.stop.is_set() and not session.stop_requested:
                    frame, result, source = detector.poll(process)
                    if frame:
                        self._track(frame)
                    if not session.mic_enabled.is_set():
                        tail.clear()
                        continue
                    if result:
                        self.on_wake(result, source, tail)
                    if frame:
                        tail.append(frame)
                        if session.phase in ("wake", "listen") or session.barge_in_requested.is_set():
                            self.put(frame)
        except (OSError, RuntimeError, ValueError) as error:
            if not self.stop.is_set() and not session.stop_requested:
                logging.error("麦克风监听失败：%s", error)
                now = time.monotonic()
                self.failures.append(now)
                while self.failures and now - self.failures[0] > 60:
                    self.failures.popleft()
                if len(self.failures) >= 3:
                    self.on_failure("一分钟内采集流连续三次失败")


class AudioRecovery:
    """Restarts PipeWire when capture stalls, at most twice in ten minutes across service restarts."""

    LIMIT, WINDOW = 2, 600

    def __init__(self, mic, session, music, alarms, state_file=RECOVERY_STATE_FILE):
        self.mic = mic
        self.session = session
        self.music = music
        self.alarms = alarms
        self.state_file = state_file
        self.requested = threading.Event()
        self.in_progress = False
        self.attempts = deque()
        self.retry_at = 0.0
        self.status = "idle"
        self.reason = ""
        # Attempts are kept in monotonic time, which only means something within one boot.
        self.boot = boot_id()
        if self.boot:
            self._load()

    def _load(self):
        try:
            with open(self.state_file, encoding="utf-8") as source:
                saved = json.load(source)
            if saved.get("boot") == self.boot:
                now = time.monotonic()
                self.attempts.extend(t for t in saved.get("attempts", [])
                                     if type(t) in (int, float) and 0 <= now - t < self.WINDOW)
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def pending(self):
        """A recovery waits for the current turn to end; a ringing alarm keeps it waiting."""
        return self.requested.is_set() and not self.in_progress and not self.alarms.ringing

    def request(self, reason):
        now = time.monotonic()
        if self.requested.is_set() or self.in_progress or now < self.retry_at:
            return
        while self.attempts and now - self.attempts[0] >= self.WINDOW:
            self.attempts.popleft()
        self.reason = reason
        if len(self.attempts) >= self.LIMIT:
            self.status = "limited"
            self.retry_at = now + 60
            logging.error("音频恢复已达每十分钟两次上限：%s", reason)
            return
        self.status = "pending"
        self.requested.set()
        if not self.alarms.ringing:
            self.session.cancel_requested.set()

    def _save(self):
        try:
            system.write_json(self.state_file, {"boot": self.boot, "attempts": list(self.attempts)})
        except OSError:
            logging.warning("未能保存音频恢复次数")

    def recover(self):
        if self.alarms.ringing:
            return
        mic, session = self.mic, self.session
        self.in_progress = True
        self.status = "recovering"
        self.attempts.append(time.monotonic())
        self._save()
        self.retry_at = time.monotonic() + 60
        self.music.hold()
        try:
            logging.warning("正在重建音频链路：%s", self.reason)
            mic.close()
            subprocess.run(["/usr/bin/systemctl", "--user", "restart", "pipewire.service",
                            "wireplumber.service", "pipewire-pulse.service"],
                           check=True, capture_output=True, timeout=20)
            mic.open()
            deadline = time.monotonic() + 30
            while not session.stop_requested and time.monotonic() < deadline:
                session.last_progress = time.monotonic()
                if not mic.alive():
                    mic.open()
                if clock_ok(mic.clock) and time.monotonic() - mic.progress < 1:
                    mic.failures.clear()
                    self.status = "recovered"
                    logging.info("音频链路已恢复连续采集")
                    return
                time.sleep(.1)
            self.status = "failed"
            logging.error("音频链路恢复后仍未就绪")
        except (OSError, subprocess.SubprocessError) as error:
            self.status = "failed"
            logging.error("音频链路恢复失败：%s", type(error).__name__)
        finally:
            self.requested.clear()
            self.in_progress = False
            session.cancel_requested.clear()
            self.music.release()

    def watchdog(self, speech_player):
        """Runs on its own thread. speech_player() is the reply being played, if any."""
        while not self.session.stop_requested:
            time.sleep(1)
            if self.in_progress:
                continue
            player = speech_player()
            if player is not None and player.stalled():
                logging.critical("播放调用已停滞，退出并由systemd恢复服务")
                os._exit(1)
            if self.mic.stalled(15):
                self.request("采集流超过15秒未推进")
            elif self.mic.bad_windows >= 3:
                self.request("连续三个观察窗口收音不足")

    def summary(self):
        return {"status": self.status, "reason": self.reason, "attempts": len(self.attempts)}
