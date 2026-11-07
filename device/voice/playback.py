"""PCM playback through PipeWire's Pulse server, with the audible position."""
import array
import ctypes as C
import queue
import subprocess
import threading
import time


class AudioStalled(RuntimeError):
    pass


class SampleSpec(C.Structure):
    _fields_ = [("format", C.c_int), ("rate", C.c_uint32), ("channels", C.c_uint8)]


class BufferAttr(C.Structure):
    _fields_ = [(name, C.c_uint32) for name in ("maxlength", "tlength", "prebuf", "minreq", "fragsize")]


class PCMPlayer:
    _lib = None

    @classmethod
    def library(cls):
        if cls._lib is None:
            lib = C.CDLL("libpulse-simple.so.0")
            lib.pa_simple_new.restype = C.c_void_p
            lib.pa_simple_new.argtypes = [C.c_char_p, C.c_char_p, C.c_int, C.c_char_p, C.c_char_p,
                                          C.POINTER(SampleSpec), C.c_void_p, C.POINTER(BufferAttr), C.c_void_p]
            lib.pa_simple_write.argtypes = [C.c_void_p, C.c_char_p, C.c_size_t, C.c_void_p]
            lib.pa_simple_get_latency.argtypes = [C.c_void_p, C.c_void_p]
            lib.pa_simple_get_latency.restype = C.c_uint64
            lib.pa_simple_flush.argtypes = [C.c_void_p, C.c_void_p]
            lib.pa_simple_drain.argtypes = [C.c_void_p, C.c_void_p]
            lib.pa_simple_free.argtypes = [C.c_void_p]
            cls._lib = lib
        return cls._lib

    def __init__(self, rate, gain=1.0):
        self.rate = rate
        self.gain = gain
        self.played_seconds = 0.0
        self.frames = queue.Queue(maxsize=50)
        self.io_started = None
        self.stopped = threading.Event()
        self.done = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._run, name="speech-output", daemon=True)
        self.thread.start()

    def write(self, pcm):
        step = self.rate * 2 // 25
        for offset in range(0, len(pcm), step):
            self._put(pcm[offset:offset + step])

    def stalled(self):
        started = self.io_started
        return started is not None and time.monotonic() - started > 10

    def _put(self, pcm):
        while not self.stopped.is_set():
            if self.error:
                raise self.error
            if self.stalled():
                raise AudioStalled("speaker operation stalled")
            try:
                self.frames.put(pcm, timeout=0.1)
                return
            except queue.Full:
                continue
        raise RuntimeError("speaker playback closed")

    def finish(self, check_cancel):
        self._put(None)
        while not self.done.wait(0.04):
            check_cancel()
            if self.stalled():
                raise AudioStalled("speaker operation stalled")
        check_cancel()
        if self.error:
            raise self.error

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise AudioStalled("speaker shutdown stalled")

    def _call(self, function, *args):
        self.io_started = time.monotonic()
        try:
            return function(*args)
        finally:
            self.io_started = None

    def _run(self):
        stream = None
        try:
            lib = self.library()
            spec = SampleSpec(3, self.rate, 1)  # PA_SAMPLE_S16LE
            attrs = BufferAttr(0xffffffff, self.rate * 2 // 5, 0xffffffff, 0xffffffff, 0xffffffff)
            stream = self._call(lib.pa_simple_new, None, b"ThinkSmart Assistant", 1, None, b"Assistant",
                                C.byref(spec), None, C.byref(attrs), None)
            if not stream:
                subprocess.run(
                    ["/usr/bin/systemctl", "--user", "start", "pipewire-pulse.socket", "pipewire-pulse.service"],
                    check=True, capture_output=True, timeout=8,
                )
                stream = self._call(lib.pa_simple_new, None, b"ThinkSmart Assistant", 1, None, b"Assistant",
                                    C.byref(spec), None, C.byref(attrs), None)
            if not stream:
                raise RuntimeError("speaker stream could not open")
            written = 0

            def position():
                latency = self._call(lib.pa_simple_get_latency, stream, None)
                if latency != 0xffffffffffffffff:
                    self.played_seconds = max(self.played_seconds, written / (self.rate * 2) - latency / 1e6)

            while not self.stopped.is_set():
                position()
                try:
                    pcm = self.frames.get(timeout=0.04)
                except queue.Empty:
                    continue
                if pcm is None:
                    if not self.stopped.is_set():
                        # The hardware latency can retain a nonzero floor after
                        # the last sample. Drain is the server's completion signal.
                        if self._call(lib.pa_simple_drain, stream, None) < 0:
                            raise RuntimeError("speaker drain failed")
                        self.played_seconds = written / (self.rate * 2)
                    break
                if self.gain != 1.0:
                    samples = array.array("h", pcm)
                    pcm = array.array("h", (int(v * self.gain) for v in samples)).tobytes()
                step = self.rate * 2 // 25
                for offset in range(0, len(pcm), step):
                    if self.stopped.is_set():
                        break
                    block = pcm[offset:offset + step]
                    if self._call(lib.pa_simple_write, stream, block, len(block), None) < 0:
                        raise RuntimeError("speaker write failed")
                    written += len(block)
                    position()
            position()
        except Exception as error:
            self.error = error
        finally:
            if stream:
                self._call(lib.pa_simple_flush, stream, None)
                self._call(lib.pa_simple_free, stream)
            self.done.set()


class SpeechProgress:
    def __init__(self, rate):
        self.rate = rate
        self.received = 0
        self.sentences = {}

    def audio(self, pcm):
        self.received += len(pcm)

    def event(self, event):
        output = (event.get("payload") or {}).get("output") or {}
        sentence = output.get("sentence") or {}
        index = sentence.get("index")
        if index is None:
            return
        item = self.sentences.setdefault(index, {"start": self.received / (self.rate * 2), "text": "", "words": []})
        if output.get("original_text"):
            item["text"] = output["original_text"]
        if sentence.get("words"):
            item["words"] = sentence["words"]
        if output.get("type") == "sentence-end":
            item["end"] = self.received / (self.rate * 2)

    def heard(self, seconds):
        parts = []
        for _, item in sorted(self.sentences.items()):
            elapsed = seconds - item["start"]
            if elapsed <= 0:
                break
            if "end" in item and seconds >= item["end"]:
                parts.append(item["text"])
                continue
            end = max((w["end_index"] for w in item["words"] if w["end_time"] <= elapsed * 1000), default=0)
            parts.append(item["text"][:end])
            break
        return "".join(parts).strip()
