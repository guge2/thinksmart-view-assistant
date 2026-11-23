"""Speaking replies: streamed cloud TTS, with the local espeak voice as the fallback."""

import io
import logging
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from itertools import chain

import offline
from config import PLAYBACK_RATE
from playback import AudioStalled, PCMPlayer, SpeechProgress
from session import TurnCancelled
from speech_text import SPOKEN_CONTENT, speech_segments

PREVIEW_TEXT = "你好，我是你的语音助手。很高兴为你服务。"
PREVIEW_MAX_SECONDS = 20


class Speaker:
    """heard holds what the user has heard of the reply so far; player is the playback in progress,
    which the audio watchdog checks for stalls. set_phase("speak") runs when audio starts."""

    def __init__(self, client, link, session, ui, wake_word, models, prefs, set_phase):
        self.client = client
        self.link = link
        self.session = session
        self.ui = ui
        self.wake_word = wake_word
        self.models = models
        self.prefs = prefs
        self.set_phase = set_phase
        self.heard = ""
        self.player = None

    def say(self, reply, offline_mode=False):
        """Speaks a whole reply, in the cloud voice unless offline or the cloud fails."""
        if not offline_mode:
            try:
                self.stream(iter((reply,)))
                return
            except (TurnCancelled, AudioStalled):
                raise
            except Exception as error:
                logging.warning("云端播报失败，改用本地播报：%s", error)
        self.local(reply)

    def stream(self, fragments):
        """Speaks text as it arrives; returns what was heard."""
        self.heard = ""
        for guarded, segment in speech_segments(fragments, self.wake_word.phrase):
            with self.wake_word.guard(guarded) as on_play:
                self.segment(segment, on_play)
        return self.heard.strip()

    def segment(self, fragments, on_play=None):
        """One TTS session; on_play runs when its first audio plays."""
        session = self.session
        # A trailing punctuation-only segment needs no TTS request.
        fragments = iter(fragments)
        leading = ""
        for fragment in fragments:
            leading += fragment
            if SPOKEN_CONTENT.search(fragment):
                break
        else:
            self.ui.emit("delta", text=leading)
            self.heard += leading
            return
        fragments = chain((leading,), fragments)
        models = self.models
        player = PCMPlayer(PLAYBACK_RATE)
        progress = SpeechProgress(PLAYBACK_RATE)
        self.player = player
        reply_parts = []
        completed = False
        try:
            with closing(self.link.start(lambda link: self.client.audio.tts.connect(
                model=models.tts_model,
                voice=models.tts_voice,
                format="pcm",
                sample_rate=PLAYBACK_RATE,
                rate=self.prefs.speech_rate,
                parameters={"word_timestamp_enabled": True},
                connection=link,
            ))) as tts:
                speaking = False

                def receive_audio():
                    nonlocal speaking
                    for event in tts.events():
                        session.check_cancel()
                        if isinstance(event, bytes):
                            if not speaking:
                                if on_play:
                                    on_play()
                                speaking = True
                                self.set_phase("speak")
                            session.turn_models.update(speech="cloud", tts_model=models.tts_model, tts_voice=models.tts_voice)
                            progress.audio(event)
                            player.write(event)
                        else:
                            progress.event(event)

                with ThreadPoolExecutor(max_workers=1) as pool:
                    receiver = pool.submit(receive_audio)
                    try:
                        for fragment in fragments:
                            session.check_cancel()
                            if receiver.done():
                                receiver.result()
                            if fragment:
                                reply_parts.append(fragment)
                                self.ui.emit("delta", text=fragment)
                                tts.send_text(fragment)
                        tts.stop()
                        if not speaking:
                            self.set_phase("speak")
                        session.wait(receiver, 180)
                        player.finish(session.check_cancel)
                    except Exception:
                        player.close()
                        tts.close()
                        raise
            completed = True
        except Exception:
            session.check_cancel()
            raise
        finally:
            try:
                player.close()
            finally:
                self.player = None
            self.heard += "".join(reply_parts) if completed else progress.heard(player.played_seconds)
            if not completed:
                # Binary audio has no task id: late audio of this synthesis must not reach the next one.
                self.link.drop()
                logging.info("播报停止在 %.2f 秒，已听到：%s", player.played_seconds, self.heard)
        session.check_cancel()

    def local(self, reply):
        """Speaks the part of the reply not heard yet with espeak."""
        session = self.session
        session.turn_models.update(speech="local", tts_model="espeak-ng", tts_voice="cmn")
        session.check_cancel()
        heard = self.heard
        remaining = reply[len(heard):] if heard and reply.startswith(heard) else reply
        self.set_phase("speak")
        self.ui.emit("reply", text=reply)
        for guarded, segment in speech_segments((remaining,), self.wake_word.phrase):
            text = "".join(segment)
            if not SPOKEN_CONTENT.search(text):
                continue
            with self.wake_word.guard(guarded) as on_play:
                succeeded = offline.speak(text, session.cancelled, on_play=on_play)
                session.check_cancel()
                if not succeeded:
                    raise RuntimeError("speaker local playback failed")
        self.heard = reply

    def preview(self, model, voice):
        """A short sample of a model and voice as WAV bytes, for the management page."""
        output = io.BytesIO()
        with wave.open(output, "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(PLAYBACK_RATE)
            with self.client.audio.tts.connect(model=model, voice=voice, format="pcm", sample_rate=PLAYBACK_RATE,
                                               rate=self.prefs.speech_rate, timeout=15) as tts:
                tts.send_text(PREVIEW_TEXT)
                tts.stop()
                total = 0
                for event in tts.events():
                    if isinstance(event, bytes):
                        total += len(event)
                        if total > PLAYBACK_RATE * 2 * PREVIEW_MAX_SECONDS:
                            raise ValueError("试听音频过长")
                        target.writeframesraw(event)
                if not total:
                    raise ValueError("没有收到试听音频，请稍后重试")
        return output.getvalue()
