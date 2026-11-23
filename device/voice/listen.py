"""Cloud speech recognition of one utterance from the queued microphone audio."""

import logging
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import closing

import skills
from audio import frame_level, frame_rms
from config import SAMPLE_RATE
from session import TurnCancelled
from speech_text import UNFINISHED, strip_wake_prefix

SPEECH_PAUSE_SECONDS = 1.0
UNFINISHED_PAUSE_SECONDS = 2.0
# While music plays, a quick command ("下一首") ends after a shorter pause, segmented sooner by the server.
PLAYBACK_COMMAND_PAUSE_SECONDS = 0.45
PLAYBACK_ASR_SILENCE_MS = 380
# Text comes only with a finished sentence; one that ended this long ago ends listening regardless.
SENTENCE_GRACE_SECONDS = 10
# The first half second of listening is the wake word's tail, queued before listening.
WAKE_TAIL_SECONDS = 0.5


class VoiceActivity:
    """Energy detector on the echo-cancelled microphone. Recognition reports only whole
    sentences, so this is what tells that the user is still, or again, talking."""

    def __init__(self):
        self.levels = deque(maxlen=50)  # 2 s: the quietest frame is the room, even mid-speech
        self.run = 0
        self.speech_at = 0.0  # last frame of speech

    def feed(self, data, now):
        rms = frame_rms(data)
        self.levels.append(rms)
        if rms > max(3 * min(self.levels), 250):
            self.run += 1
            if self.run >= 3:  # 120 ms
                self.speech_at = now
        else:
            self.run = 0


class Listener:
    def __init__(self, client, link, mic, session, ui, wake_word, models, settings):
        self.client = client
        self.link = link
        self.mic = mic
        self.session = session
        self.ui = ui
        self.wake_word = wake_word
        self.models = models
        self.settings = settings

    def transcribe(self, wait_seconds=None, *, playback_active=False):
        """Recognizes one utterance, which may span several sentences when the user pauses mid-thought.

        wait_seconds is how long to wait for the user to start; playback_active marks a command said
        over music, which starts with the wake word and may be a short playback command."""
        session, phrase = self.session, self.wake_word.phrase
        state = {"sentences": [], "partial": ""}

        def heard(include_partial=True):
            # A sentence the user went on from ends in a comma, not a full stop.
            sentences = state["sentences"][:]
            parts = [text.rstrip("。.") + ("" if text[-1] in "？?！!，," else "，") for text in sentences[:-1]]
            return "".join(parts + sentences[-1:]) + (state["partial"] if include_partial else "")

        def receive_events(recognition):
            for event in recognition.events():
                payload = event.get("payload") or {}
                sentence = (payload.get("output") or {}).get("sentence") or {}
                text = (sentence.get("text") or "").strip()
                if text and not sentence.get("heartbeat") and not session.cancel_requested.is_set():
                    if not state["sentences"]:
                        session.mark("first_text_ms")
                    if sentence.get("sentence_end"):
                        state["sentences"].append(text)
                        state["partial"] = ""
                    else:
                        state["partial"] = text
                    self.ui.emit("partial", text=heard())

        if wait_seconds is None:
            wait_seconds = self.settings.speech_wait_seconds
        max_seconds = self.settings.max_speech_seconds
        asr_model = self.models.asr_model
        # The server can segment early while the local silence check still allows longer utterances.
        parameters = ({"max_sentence_silence": PLAYBACK_ASR_SILENCE_MS}
                      if playback_active and asr_model.startswith("qwen-audio-3.1-asr-") else {})
        with closing(self.link.start(lambda link: self.client.audio.asr.recognize(
            model=asr_model,
            format="pcm",
            sample_rate=SAMPLE_RATE,
            parameters=parameters,
            timeout=max(10.0, wait_seconds + 5),
            connection=link,
        ))) as recognition:
            started = time.monotonic()
            session.mark("asr_ready_ms", first=False)
            voice = VoiceActivity()
            judged, pause, ended_at = 0, None, 0.0
            quick_command = False
            with ThreadPoolExecutor(max_workers=1) as pool:
                receiver = pool.submit(receive_events, recognition)
                stopped = False
                try:
                    session.check_cancel()
                    logging.info("开始接收语音")
                    while not session.stop_requested:
                        if session.cancel_requested.is_set():
                            raise TurnCancelled()
                        if receiver.done():
                            receiver.result()
                        frame = self.mic.read()
                        recognition.send_audio_frame(frame)
                        self.ui.emit("level", v=frame_level(frame))
                        now = time.monotonic()
                        voice.feed(frame, now)
                        if len(state["sentences"]) > judged:
                            judged, ended_at = len(state["sentences"]), now
                            final_text = heard(include_partial=False)
                            command = strip_wake_prefix(final_text, phrase) if playback_active else final_text
                            pause = None
                            quick_command = False
                            if command:
                                pause = UNFINISHED_PAUSE_SECONDS if UNFINISHED.search(command) else SPEECH_PAUSE_SECONDS
                                quick_command = playback_active and skills.is_quick_playback_command(
                                    skills.match(command, True, phrase))
                        # Talking on resets the pause; noise taken for speech brings no new sentence.
                        if pause is not None:
                            silence = PLAYBACK_COMMAND_PAUSE_SECONDS if quick_command and not state["partial"] else pause
                            if now - voice.speech_at >= silence or now - ended_at > SENTENCE_GRACE_SECONDS:
                                break
                        # Once the user has been heard, the wait for their sentence lasts until they
                        # have been quiet long enough for it to arrive.
                        talked = voice.speech_at > started + WAKE_TAIL_SECONDS
                        if (pause is None and now - started >= wait_seconds
                                and now - voice.speech_at > (UNFINISHED_PAUSE_SECONDS if talked else 0)):
                            break
                        if now - started >= wait_seconds + max_seconds:
                            break
                    recognition.stop()
                    stopped = True
                    session.wait(receiver, 20)
                except FutureTimeout as error:
                    raise RuntimeError("speech recognition did not finish") from error
                finally:
                    if not stopped:
                        try:
                            recognition.stop()
                        except Exception:
                            pass
                    recognition.close()

        session.check_cancel()
        if self.mic.overflow.is_set():
            raise RuntimeError("microphone command buffer overflow")
        if len(state["sentences"]) > 1:
            logging.info("合并了%d句：%s", len(state["sentences"]), heard())
        text = heard().strip()
        return strip_wake_prefix(text, phrase) if playback_active else text
