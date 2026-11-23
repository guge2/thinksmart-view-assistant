#!/usr/bin/python3
"""ThinkSmart voice assistant: wakes on its name, listens, answers through the chat model and its
skills, and keeps listening briefly for a follow-up.

This module wires the parts together and runs the wake-converse loop:
  session       phase and cancel signals shared by all threads
  capture/wake  microphone, wake word spotting, audio recovery
  listen/speak  cloud recognition and speech, over one shared websocket (speech_link)
  conversation  one turn: local command or chat model, tools, reply
  control       requests from the display and the management gateway"""

import logging
import os
import re
import signal
import threading
import time

import dashscope

import memory
import offline
from alarms import AlarmService
from audio import LISTEN_TONE, THINKING_TONE, Ducker, play_cue
from capture import AudioRecovery, Microphone
from config import (KWS_MODEL, MODEL_NAME, OFFLINE_KEYWORDS_FILE, RUNTIME_DIR, Credentials, Models, Preferences,
                    Settings, clamp_rate, load_chat_config, read_hardware_mic, read_model_overrides)
from control import NO_ACK, ControlServer
from conversation import BRIEFING_TEXT, BRIEFING_WAIT_SECONDS, Conversation, create_brain
from device_skills import DeviceSkills
from errors import apology, classify_error
from listen import Listener
from music import MusicService
from playback import AudioStalled
from recipe import RecipeService
from session import Session, TurnCancelled
from shared import system
from speak import Speaker
from speech_link import SpeechLink
from speech_text import FILLER, compact
from ui_events import UiPublisher
from wake import WakeWord, create_keyword_spotter, music_keywords

PREVIEW_ID = re.compile(r"[a-f0-9]{24}")
PREVIEW_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
PREVIEW_KEEP_SECONDS = 60
# The control channel reports the assistant unhealthy when its loop has not progressed this long.
PROGRESS_TIMEOUT = 20


class ThinkSmartAssistant:
    def __init__(self, ui, settings=None, environ=None):
        settings = settings or Settings.from_env(environ)
        credentials = Credentials.load(environ)
        chat = load_chat_config("chat.json", environ)
        expert = load_chat_config("expert.json", environ)
        self.ui = ui
        self.settings = settings
        self.client = dashscope.Client(api_key=credentials.api_key, workspace=credentials.workspace,
                                       region=credentials.region, config_path="/dev/null")
        self.models = Models.initial(settings, chat, expert)
        self.model_defaults = self.models.overridable()
        self.models.update(read_model_overrides(self.model_defaults))
        self.prefs = Preferences.load(settings)
        self.phase_lock = threading.Lock()
        self.preview_lock = threading.Lock()
        self.cue_timer = None
        self.cloud_state = "ready"

        session = self.session = Session()
        self.ducker = Ducker(settings.duck_level)
        self.music = MusicService(ui.emit, settings.music_duck_level, session.check_cancel)
        self.alarms = AlarmService(ui.emit, on_ring=self.on_alarm_ring, music=self.music)
        self.recipe = RecipeService(ui.emit, self.alarms)
        self.offline_commands = offline.OfflineCommands(KWS_MODEL, OFFLINE_KEYWORDS_FILE, settings.kws_threads)
        phrase = system.read_wake_phrase()
        self.wake_word = WakeWord(phrase, create_keyword_spotter(settings), music_keywords(phrase, settings))
        self.mic = Microphone(session, self.wake_word, self.music, self.alarms, self.on_wake, self.on_capture_failure)
        self.recovery = AudioRecovery(self.mic, session, self.music, self.alarms)
        session.interrupted = self.recovery.pending
        self.hardware_mic = read_hardware_mic()
        if self.prefs.mic and self.hardware_mic is not False:
            session.mic_enabled.set()

        self.link = SpeechLink(self.client)
        self.listener = Listener(self.client, self.link, self.mic, session, ui, self.wake_word, self.models, settings)
        self.speaker = Speaker(self.client, self.link, session, ui, self.wake_word, self.models, self.prefs,
                               self.set_phase)
        self.brain = create_brain(credentials, settings, self.models, chat, expert)
        self.conversation = Conversation(
            session, ui, self.brain, self.speaker, self.models, self.prefs, settings, self.wake_word,
            self.music, self.alarms, memory.Memory(), DeviceSkills(self.prefs, ui, self.alarms, session),
            self.recipe, self.set_phase)
        self.control = ControlServer(self.control_handlers(), lambda: session.stop_requested)

    # State shown on the display

    def set_phase(self, phase):
        session = self.session
        with self.phase_lock:
            session.phase = phase
            if phase in ("idle", "think") and not session.barge_in_requested.is_set():
                self.mic.clear()
            if self.cue_timer is not None:
                self.cue_timer.cancel()
                self.cue_timer = None
            if phase == "think" and self.settings.thinking_cue_seconds > 0:
                self.cue_timer = threading.Timer(self.settings.thinking_cue_seconds, self.thinking_cue)
                self.cue_timer.daemon = True
                self.cue_timer.start()
        self.ui.emit(phase)

    def thinking_cue(self):
        if self.session.phase == "think" and not self.session.cancel_requested.is_set():
            self.conversation.cue(THINKING_TONE)

    def set_cloud(self, state):
        self.cloud_state = state
        self.ui.emit("cloud", s=state)

    def announce(self):
        self.ui.emit("ready", wake=self.wake_word.phrase, mic=int(self.session.mic_enabled.is_set()),
                     requested_mic=self.prefs.mic, hardware_mic=self.hardware_mic, phase=self.session.phase)
        self.ui.emit("cloud", s=self.cloud_state)
        self.music.announce()
        self.alarms.announce()
        self.recipe.announce()
        self.ui.snapshot()

    def report_error(self, error):
        code = classify_error(error)
        self.session.turn_models["error"] = code
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

    def apologize(self, error):
        """Says why a turn failed, unless the user already heard part of the reply."""
        code = classify_error(error)
        if self.session.phase not in ("think", "speak") or self.speaker.heard or code in ("mic", "speaker"):
            return
        try:
            offline.speak(apology(code),
                          lambda: self.session.stop_requested or self.session.barge_in_requested.is_set())
        except Exception as warning:
            logging.warning("错误提示播报失败：%s", warning)

    # Other playback while the assistant talks

    def on_alarm_ring(self, ringing):
        # Alarms and reminders replace what was playing with their own music; timers pause it.
        if ringing:
            self.music.hold()
        elif self.session.phase == "idle":
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

    # Microphone

    def set_mic(self, enabled):
        session = self.session
        self.prefs.mic = enabled
        active = enabled and self.hardware_mic is not False
        if active:
            session.mic_enabled.set()
        else:
            session.mic_enabled.clear()
            session.talk_requested.clear()
            self.mic.clear()
            session.barge_in_requested.clear()
            if session.phase != "idle":
                session.cancel_requested.set()
        logging.info("麦克风已%s", "开启" if active else "关闭")
        self.ui.emit("mic", on=int(active), requested_mic=enabled, hardware_mic=self.hardware_mic)

    def refresh_hardware_mic(self):
        state = read_hardware_mic()
        if state is not None and state != self.hardware_mic:
            self.hardware_mic = state
            self.set_mic(self.prefs.mic)

    def on_capture_failure(self, reason):
        self.recovery.request(reason)

    def audio_ready(self):
        return self.mic.ready() and not self.recovery.in_progress

    def on_wake(self, result, source, tail):
        """Runs on the capture thread for each wake word heard."""
        decision = self.session.accept_wake(source)
        if decision is None:
            return
        if self.alarms.ringing:
            self.alarms.dismiss()
        self.mic.restart_queue(tail)
        if decision == "start":
            self.set_phase("wake")
            self.session.talk_requested.set()
        logging.info("%s检测到唤醒词：%s (%s)", self.session.phase, result, source)

    def wait_for_wake(self):
        """True on a wake word or the talk button, "briefing" when a dismissed alarm asks for the
        morning briefing, False when the service stops."""
        session = self.session
        logging.info("等待唤醒词：%s", self.wake_word.phrase)
        while not session.stop_requested:
            if session.talk_requested.wait(0.1):
                session.talk_requested.clear()
                if not session.mic_enabled.is_set():
                    continue
                if session.phase != "wake":
                    session.start_timing("button")
                self.set_phase("wake")
                return True
            if not self.alarms.ringing and self.alarms.take_briefing():
                session.start_timing("briefing")
                self.set_phase("wake")
                return "briefing"
            session.check_cancel()
            if not self.mic.alive():
                raise RuntimeError("microphone stream ended")
        return False

    def start_listening(self):
        """Every entry into listening: the cue means "you can speak now"; speech over it is kept.

        The cue plays while recognition connects; audio has been queued since the wake word."""
        self.set_phase("listen")
        threading.Thread(target=play_cue, args=(LISTEN_TONE,), name="listen-tone", daemon=True).start()

    # Control channel

    def control_handlers(self):
        session, alarms, music = self.session, self.alarms, self.music

        def talk(message, sender):
            if not session.mic_enabled.is_set():
                raise ValueError("麦克风已关闭，请先打开麦克风")
            if session.phase != "idle":
                raise ValueError("正在对话中，可先结束当前对话")
            if alarms.ringing:
                alarms.dismiss()
            session.talk_requested.set()
            return "accepted"

        def stop(message, sender):
            alarms.dismiss()
            if session.phase != "idle":
                session.cancel_requested.set()
            return "accepted"

        def snapshot(message, sender):
            self.ui.record("music", **music.player.snapshot())
            self.ui.record("alarms", next=alarms.next_summary(), items=alarms.list_items())
            self.ui.snapshot(sender)
            return NO_ACK

        def ping(message, sender):
            health = dict(self.health(), seq=message.get("seq"))
            if sender:
                self.control.send(sender, dict(health, e="pong"))
            else:
                self.ui.emit("pong", **health)
            return NO_ACK

        def prefs(message, sender):
            if "rate" in message:
                self.prefs.speech_rate = clamp_rate(message.get("rate"))
            if "think" in message:
                self.prefs.thinking = bool(message.get("think"))
            if type(message.get("brightness")) is int:
                self.prefs.brightness = message["brightness"]
            logging.info("语速 %.1f，思考模式%s", self.prefs.speech_rate, "开启" if self.prefs.thinking else "关闭")
            return "applied"

        def accepted(action):
            def handler(message, sender):
                action(message)
                return "accepted"
            return handler

        return {
            "talk": talk,
            "stop": stop,
            "alarm_stop": accepted(lambda m: alarms.dismiss()),
            "alarm_snooze": accepted(lambda m: alarms.snooze(m.get("minutes") or 5)),
            "alarm_cmd": accepted(lambda m: alarms.ui_command(m.get("a"), id=m.get("id"), minutes=m.get("minutes"),
                                                              label=m.get("label"))),
            "mic": lambda m, sender: self.set_mic(bool(m.get("on"))) or "applied",
            "hardware_mic": accepted(lambda m: self.refresh_hardware_mic()),
            "hello": lambda m, sender: self.announce() or NO_ACK,
            "snapshot": snapshot,
            "preview": self.start_preview,
            "ping": ping,
            "prefs": prefs,
            "music": accepted(lambda m: music.ui_command(m.get("a"), minutes=m.get("minutes"))),
            "login": accepted(lambda m: music.login_command(m.get("t"))),
            "recipe": accepted(lambda m: self.recipe.ui_command(m.get("a"), step=m.get("step"))),
        }

    def health(self):
        session, mic, now = self.session, self.mic, time.monotonic()
        settings, models = self.settings, self.models
        return {
            "healthy": now - session.last_progress < PROGRESS_TIMEOUT and self.audio_ready(),
            "control_healthy": now - session.last_progress < PROGRESS_TIMEOUT,
            "audio_ready": self.audio_ready(),
            "audio_recovery": self.recovery.summary(),
            "phase": session.phase, "cloud": self.cloud_state,
            "mic": session.mic_enabled.is_set(), "hardware_mic": self.hardware_mic,
            "capture_age": round(now - mic.progress, 2),
            "queued_audio_ms": mic.frames.qsize() * 40,
            "capture_clock": mic.clock,
            "raw_age": round(now - mic.raw_progress, 2) if mic.raw_progress else None,
            "levels": dict(mic.levels), "dropped_frames": mic.dropped_frames,
            "raw_dropped_frames": mic.raw_dropped_frames,
            "timing": dict(session.timing),
            "wake_detection": {"count": session.wake_hits, "last": session.last_wake},
            "preferences": {"speech_rate": self.prefs.speech_rate, "thinking": self.prefs.thinking,
                            "mic": self.prefs.mic},
            "configuration": {
                "kws_score": settings.kws_score,
                "kws_threshold": settings.kws_threshold,
                "speech_wait_seconds": settings.speech_wait_seconds,
                "max_speech_seconds": settings.max_speech_seconds,
                "followup_seconds": settings.followup_seconds,
                "question_followup_seconds": settings.question_followup_seconds},
            "models": {"chat_model": models.chat_model, "chat_provider": models.chat_provider,
                       "wake_model": MODEL_NAME,
                       "expert_model": models.expert_model, "expert_provider": models.expert_provider,
                       "asr_model": models.asr_model, "tts_model": models.tts_model,
                       "tts_voice": models.tts_voice},
            "turn_models": dict(session.turn_models),
        }

    def start_preview(self, message, sender):
        """A voice sample for the management page; the acknowledgement follows when it is ready."""
        if not sender or not PREVIEW_ID.fullmatch(str(message.get("id", ""))):
            raise ValueError("试听请求无效")
        if not all(PREVIEW_NAME.fullmatch(str(message.get(k, ""))) for k in ("model", "voice")):
            raise ValueError("试听模型或音色无效")
        if not self.preview_lock.acquire(blocking=False):
            raise ValueError("上一段试听仍在生成，请稍后再试")
        threading.Thread(target=self.preview, args=(message, sender), name="voice-preview", daemon=True).start()
        return NO_ACK

    def preview(self, message, sender):
        try:
            audio = self.speaker.preview(message["model"], message["voice"])
            path = f"{RUNTIME_DIR}/preview-{message['id']}.wav"
            with open(path + ".tmp", "wb") as target:
                target.write(audio)
            os.replace(path + ".tmp", path)

            def remove():
                try:
                    os.unlink(path)
                except OSError:
                    pass

            cleanup = threading.Timer(PREVIEW_KEEP_SECONDS, remove)
            cleanup.daemon = True
            cleanup.start()
            self.control.reply(sender, message, True, "ready")
        except Exception:
            logging.exception("试听生成失败")
            self.control.reply(sender, message, False, "试听生成失败，请检查语音服务或稍后重试")
        finally:
            self.preview_lock.release()

    # The wake-converse loop

    def begin_turn(self):
        """Loads the models and voice the management page saved, between turns only."""
        if self.models.update(read_model_overrides(self.model_defaults)):
            self.brain.configure_models(self.models.chat_model, self.models.expert_model, self.models.chat_provider)
            logging.info("下一轮模型与音色配置已加载")

    def run(self):
        session = self.session
        self.control.start()
        threading.Thread(target=self.recovery.watchdog, args=(lambda: self.speaker.player,),
                         name="audio-watchdog", daemon=True).start()
        self.alarms.start()
        try:
            self.brain.start()
        except (OSError, RuntimeError) as error:
            logging.error("智能体启动失败，将在对话时重试：%s", error)
        self.announce()
        try:
            while not session.stop_requested:
                try:
                    if self.recovery.requested.is_set() and not self.alarms.ringing:
                        self.recovery.recover()
                    session.cancel_requested.clear()
                    woke = True
                    if session.barge_in_requested.is_set():
                        self.set_phase("wake")
                        session.barge_in_requested.clear()
                    else:
                        self.set_phase("idle")
                        self.mic.open()
                        if session.screen_off_pending:
                            session.screen_off_pending = False
                            self.ui.emit("sleep")
                        woke = self.wait_for_wake()
                        if not woke:
                            break
                        self.brain.forget_if_idle(self.settings.context_idle_seconds)
                    threading.Thread(target=self.link.prepare, name="link-prepare", daemon=True).start()
                    self.duck()
                    self.begin_turn()
                    self.conversation.begin()
                    if woke == "briefing":
                        self.set_cloud("busy")
                        self.brief()
                    else:
                        self.converse()
                        if self.alarms.take_briefing():
                            self.brief()
                    self.set_cloud("offline" if self.conversation.offline_mode else "ready")
                except TurnCancelled:
                    logging.info("本轮对话已取消")
                    self.set_cloud("ready")
                except AudioStalled:
                    logging.exception("音频调用无法恢复，退出并由systemd重启")
                    os._exit(1)
                except Exception as error:
                    if classify_error(error) == "mic" or not self.mic.alive():
                        self.mic.close()
                    if not session.stop_requested:
                        logging.exception("语音处理失败：%s", error)
                        self.report_error(error)
                        self.apologize(error)
                        time.sleep(2)
                finally:
                    if not session.barge_in_requested.is_set():
                        self.unduck()
        finally:
            self.shutdown()

    def converse(self):
        conversation = self.conversation
        # Keep the wake stream running through the tone so an immediate
        # follow-on command is still captured.
        self.start_listening()
        if self.session.stop_requested:
            return
        self.session.check_cancel()
        self.set_cloud("busy")
        try:
            # Waking only to turn off an alarm should not keep the briefing waiting long.
            briefing = self.alarms.briefing_at is not None
            text = self.listener.transcribe(BRIEFING_WAIT_SECONDS if briefing else None,
                                            playback_active=self.music.active() and not briefing)
        except TurnCancelled:
            raise
        except Exception as error:
            if classify_error(error) == "mic":
                raise
            logging.warning("云端识别不可用，改用离线指令：%s", error)
            conversation.offline_mode = True
            self.set_cloud("offline")
            self.set_phase("think")
            self.speaker.say("网络不可用，请再说一次简单指令。", offline_mode=True)
            self.start_listening()
            text = self.offline_commands.listen(self.session, self.mic, self.ui)
        if text:
            conversation.handle_turn(text)
            self.follow_up()
        else:
            logging.info("唤醒后未识别到有效语音")
            self.ui.emit("nospeech")

    def follow_up(self):
        """Keep listening briefly after a reply so the user can continue without the wake word."""
        session, conversation = self.session, self.conversation
        while conversation.next_followup > 0 and not session.stop_requested and session.mic_enabled.is_set():
            self.begin_turn()
            session.start_timing("followup")
            self.mic.open()
            self.set_phase("listen")
            try:
                text = self.listener.transcribe(conversation.next_followup)
            except (TurnCancelled, AudioStalled):
                raise
            except Exception as error:
                if classify_error(error) == "mic":
                    raise
                logging.warning("追问识别不可用，结束追问窗口：%s", error)
                return
            short = compact(text)
            if not text or not conversation.followup_question and (len(short) < 2 or FILLER.fullmatch(short)):
                logging.info("追问窗口结束%s", f"，忽略：{text}" if text else "")
                return
            self.duck()
            conversation.handle_turn(text)

    def brief(self):
        """The day's briefing once a morning alarm is turned off, unless the user moved on to something else."""
        conversation = self.conversation
        if conversation.offline_mode or conversation.over or self.music.active():
            return
        logging.info("闹钟已关闭，播报今日简报")
        conversation.handle_turn(BRIEFING_TEXT, shown=False)
        self.follow_up()

    def stop(self, *_args):
        # Signal handlers must stay async-signal-safe; cleanup runs in shutdown() from run().
        self.session.stop_requested = True

    def shutdown(self):
        self.session.stop_requested = True
        with self.phase_lock:
            if self.cue_timer is not None:
                self.cue_timer.cancel()
                self.cue_timer = None
        for action in (
            self.mic.close,
            self.link.drop,
            lambda: self.speaker.player.close() if self.speaker.player is not None else None,
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
