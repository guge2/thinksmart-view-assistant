"""Small, explicit offline command vocabulary using the existing KWS model."""

import logging
import os
import subprocess
import tempfile
import time

import sherpa_onnx

from audio import frame_level, pcm_to_floats


def model_paths(model_dir):
    prefix = "epoch-13-avg-2-chunk-16-left-64"
    if not os.path.isfile(os.path.join(model_dir, f"encoder-{prefix}.int8.onnx")):
        prefix = "epoch-12-avg-2-chunk-16-left-64"
    return {
        "tokens": os.path.join(model_dir, "tokens.txt"),
        "encoder": os.path.join(model_dir, f"encoder-{prefix}.int8.onnx"),
        "decoder": os.path.join(model_dir, f"decoder-{prefix}.onnx" if prefix.startswith("epoch-13-") else f"decoder-{prefix}.int8.onnx"),
        "joiner": os.path.join(model_dir, f"joiner-{prefix}.int8.onnx"),
    }


class OfflineCommands:
    def __init__(self, model_dir, keywords_file, threads=1):
        self.spotter = sherpa_onnx.KeywordSpotter(
            **model_paths(model_dir),
            keywords_file=keywords_file,
            num_threads=threads,
            sample_rate=16000,
            feature_dim=80,
            max_active_paths=4,
            keywords_score=3.0,
            keywords_threshold=0.1,
            provider="cpu",
        )

    def listen(self, session, mic, ui, wait_seconds=8):
        """Capture one command; caller owns the already-open microphone."""
        stream = self.spotter.create_stream()
        deadline = time.monotonic() + wait_seconds
        while not session.stop_requested and time.monotonic() < deadline:
            if session.cancel_requested.is_set() or not session.mic_enabled.is_set():
                return ""
            frame = mic.read()
            ui.emit("level", v=frame_level(frame))
            stream.accept_waveform(16000, pcm_to_floats(frame))
            while self.spotter.is_ready(stream):
                self.spotter.decode_stream(stream)
                result = self.spotter.get_result(stream)
                if result:
                    return result
        return ""


def speak(text, cancelled=None, on_play=None):
    """Speak a short response without network; return whether playback succeeded."""
    try:
        # pw-play reads stdin as raw 48 kHz stereo; a file keeps espeak's WAV header and rate.
        with tempfile.NamedTemporaryFile(suffix=".wav") as sound:
            subprocess.run(
                ["/usr/bin/espeak-ng", "-v", "cmn", "-s", "160", "-a", "100", "-w", sound.name, "--", text],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=True,
            )
            if cancelled is not None and cancelled():
                return False
            if on_play is not None:
                on_play()
            playback = subprocess.Popen(
                ["/usr/bin/pw-play", "--media-role", "Assistant", sound.name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if cancelled is not None and cancelled():
                        return False
                    if playback.poll() is not None:
                        return playback.returncode == 0
                    time.sleep(0.05)
                return False
            finally:
                if playback.poll() is None:
                    playback.terminate()
                try:
                    playback.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    playback.kill()
                    playback.wait()
    except (OSError, subprocess.SubprocessError) as error:
        logging.warning("离线语音播放失败：%s", error)
        return False
