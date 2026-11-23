"""What the assistant is doing right now, shared by the conversation, capture, control and watchdog
threads: the phase, the stop and cancel signals, and the timing of the current wake."""

import math
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout

from music import CommandCancelled as TurnCancelled

PHASES = ("idle", "wake", "listen", "think", "speak")
# Two wake hits closer than this are one wake word.
WAKE_COOLDOWN_SECONDS = 2

__all__ = ["Session", "TurnCancelled"]


class Session:
    def __init__(self):
        self.phase = "idle"
        self.stop_requested = False
        self.cancel_requested = threading.Event()
        self.barge_in_requested = threading.Event()
        self.talk_requested = threading.Event()
        self.mic_enabled = threading.Event()
        self.screen_off_pending = False
        # Liveness for the control channel's health check: every check_cancel() is progress.
        self.last_progress = time.monotonic()
        self.wake_started = None
        self.timing = {}
        self.turn_models = {}
        self.wake_hits = 0
        self.last_wake = None
        self.last_wake_mono = -math.inf
        # Set by the owner: another reason to abandon the turn (a pending audio recovery).
        self.interrupted = lambda: False

    def check_cancel(self):
        self.last_progress = time.monotonic()
        if self.cancel_requested.is_set() or self.stop_requested or self.interrupted():
            raise TurnCancelled()

    def cancelled(self):
        return self.cancel_requested.is_set() or self.stop_requested

    def wait(self, future, timeout):
        """A future's result, checking for cancellation while it runs."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                return future.result(timeout=0.25)
            except FutureTimeout:
                self.check_cancel()
                if time.monotonic() >= deadline:
                    raise

    def start_timing(self, source):
        self.wake_started = time.monotonic()
        self.timing = {"wake_at": time.time(), "source": source}

    def mark(self, name, first=True):
        """Records how long after the wake this point of the turn came; with first, only the first time."""
        if self.wake_started is not None and not (first and name in self.timing):
            self.timing[name] = round((time.monotonic() - self.wake_started) * 1000)

    def accept_wake(self, source):
        """Counts a wake word heard by the microphone. Returns "start" when it begins a turn,
        "barge" when it interrupts one, or None when it is ignored."""
        now = time.monotonic()
        if (self.phase in ("wake", "listen") or self.barge_in_requested.is_set()
                or now - self.last_wake_mono < WAKE_COOLDOWN_SECONDS):
            return None
        self.last_wake_mono = now
        self.wake_hits += 1
        self.last_wake = {"at": time.time(), "source": source}
        self.wake_started = now
        self.timing = {"wake_at": self.last_wake["at"], "source": source}
        self.screen_off_pending = False
        if self.phase == "idle":
            return "start"
        self.barge_in_requested.set()
        self.cancel_requested.set()
        return "barge"
