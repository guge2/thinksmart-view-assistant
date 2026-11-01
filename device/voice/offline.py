"""Small, explicit offline command vocabulary using the existing KWS model."""

import logging
import os
import subprocess
import time

import sherpa_onnx


class OfflineCommands:
    def __init__(self, model_dir, keywords_file):
        self.spotter = sherpa_onnx.KeywordSpotter(
            tokens=os.path.join(model_dir, "tokens.txt"),
            encoder=os.path.join(model_dir, "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            decoder=os.path.join(model_dir, "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            joiner=os.path.join(model_dir, "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            keywords_file=keywords_file,
            num_threads=int(os.getenv("THINKSMART_KWS_THREADS", "1")),
            sample_rate=16000,
            feature_dim=80,
            max_active_paths=4,
            keywords_score=3.0,
            keywords_threshold=0.1,
            provider="cpu",
        )

    def listen(self, assistant, wait_seconds=8):
        """Capture one command; caller owns the already-open microphone."""
        stream = self.spotter.create_stream()
        deadline = time.monotonic() + wait_seconds
        while not assistant.stop_requested and time.monotonic() < deadline:
            if assistant.cancel_requested.is_set() or not assistant.mic_enabled.is_set():
                return ""
            frame = assistant.read_audio_bytes()
            assistant.ui.emit("level", v=assistant.frame_level(frame))
            stream.accept_waveform(16000, assistant.pcm_to_floats(frame))
            while self.spotter.is_ready(stream):
                self.spotter.decode_stream(stream)
            result = self.spotter.get_result(stream)
            if result:
                return result
        return ""


def speak(text, cancelled=None):
    """Speak a short response without network; return whether playback succeeded."""
    try:
        synthesis = subprocess.run(
            ["/usr/bin/espeak-ng", "-v", "cmn", "-s", "160", "--stdout", text],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15, check=True,
        )
        if cancelled is not None and cancelled():
            return False
        playback = subprocess.Popen(
            ["/usr/bin/pw-play", "--media-role", "Assistant", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            data = synthesis.stdout
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if cancelled is not None and cancelled():
                    return False
                try:
                    playback.communicate(data, timeout=0.1)
                    return playback.returncode == 0
                except subprocess.TimeoutExpired:
                    data = None
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
