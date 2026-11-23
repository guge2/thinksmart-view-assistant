"""One turn of conversation: a local command or the chat model, its tools, and the spoken reply."""

import datetime
import json
import logging
import re
import time
from itertools import chain

import information
import skills
from audio import LISTEN_TONE, OK_TONE, play_cue
from brain import Brain
from playback import AudioStalled
from session import TurnCancelled
from skills import Result
from speech_text import QUESTION_END, SpokenStream, strip_wake_prefix

# Time is already in the context, so time turns skip web search.
TIME_WORDS = re.compile(r"几点|几号|星期几|礼拜几")
# Thinking delays the first spoken word, so it is reserved for calculation, comparison and advice.
THINK_WORDS = re.compile(
    r"计算|算一下|算算|算出|等于多少|换算|折合|平均|百分之|几倍|多少倍|乘以|除以|加上|减去|\d\s*[+\-*/×÷^]\s*\d"
    r"|比较|对比|相比|区别|差别|差异|优缺点|利弊|哪个(?:好|更|划算|值)|哪种|还是.{0,12}(?:好|划算|合适)"
    r"|建议|推荐(?!.{0,6}(?:歌|曲|音乐))|怎么办|该不该|应不应该|值不值|值得|规划|计划|方案|攻略|分析"
    r"|帮我(?:想|选|安排)|怎么选|如何选"
)
EXPERT_REQUEST = re.compile(r"(?:用|请|让|交给|切换到?)\s*(?:openai|gpt|sol|6[.点]1|六点一)", re.I)
HARD_TASK_WORDS = re.compile(
    r"深入分析|详细分析|仔细分析|帮我分析|推导|证明|算法|调试|排查|代码|编程|架构"
    r"|数学题|逻辑题|复杂|多步骤|详细规划|可行性|成本收益"
)
# Local commands whose result is a soft tone rather than a spoken sentence.
QUIET_MUSIC_ACTIONS = ("pause", "next", "previous", "stop")

TOOL_RULES = (
    "你拥有以下设备本地技能（Skills）：" + skills.catalogue() + "；另外可以联网搜索。"
    "设备当前的实时状态（当前音量、是否静音、屏幕亮度、语速、深度思考开关、闹钟列表、正在播放的歌曲与伴睡定时、QQ音乐登录状态、默认城市）附在每轮用户消息前的【设备实时状态】中，是你的系统信息而不是用户说的话；"
    "当用户询问现在音量多少、亮度多少、语速多少、在放什么歌、有哪些闹钟、你有哪些本地功能时，请直接根据上下文准确回答，绝不要说无法查询。"
    "当用户表达不适或隐含调节意图（如“你说话声音好小”“太吵了”“屏幕太刺眼/太暗”“你说太快了”）时，不要反问“需要我调吗”，直接调用set_volume或control_device完成调节。"
    "用户只问现在几点时，只回答几点几分。"
    "使用工具时不要提前声称操作成功，以工具返回结果为准。"
    "新闻、比赛、行情等需要最新信息的问题可以参考联网搜索结果，用一两句话概括，不要念出网址。"
    "回答结束时不要说“还有什么需要”之类的客套追问；只有确实需要用户补充信息时才用问句结尾。"
    "【设备实时状态】里附有用户让你长期记住的事，相关时自然地用上（如称呼、口味、家人），不要逐条复述。"
)
LANGUAGE_RULES = (
    "默认使用简体中文与用户交流，回复正文和语音播报均使用中文。"
    "即使用户夹带外文，或搜索结果、工具结果是外文，也要用中文解释和概括；"
    "只有用户明确要求翻译成其他语言、使用其他语言回答或进行外语练习时，才切换语言。"
    "歌名、歌手名、人名、型号、代码和接口标识符保留准确原文，不要为了中文化而误译。"
)
SEARCH_RULES = (
    "你可以按需调用联网搜索工具。涉及新闻、近期事件、比分、行情等实时信息时先搜索核实，"
    "不要把记忆当成实时信息；普通聊天不必搜索。搜索后用自然中文概括，通常两三句话。"
)
SPOKEN_RULES = "回复会被直接朗读：不要输出网址、链接、来源标注、引用编号或Markdown标记。"

# The turn a dismissed morning alarm starts; its words make the model call daily_briefing.
BRIEFING_TEXT = "（闹钟刚被我关掉）早安"
BRIEFING_WAIT_SECONDS = 4
# Shorter gaps than this keep the history without comment; longer ones tell the model how long ago it was.
CONTEXT_GAP_SECONDS = 120


def create_brain(credentials, settings, models, chat, expert):
    """The chat agent: the configured chat model (Bailian unless chat.json says otherwise), Bailian as
    the fallback, and the optional expert model for hard tasks."""
    brain = Brain({
        "apiKey": chat.get("api_key") or credentials.api_key,
        "baseUrl": chat.get("base_url") or credentials.endpoint,
        "provider": chat.get("provider", "bailian"),
        "api": chat.get("api", "openai-completions"),
        "providerOrder": chat.get("provider_order", []),
        "providerOrderModel": chat.get("model") or models.chat_model,
        "model": models.chat_model,
        "maxTokens": chat.get("max_tokens", settings.max_reply_tokens),
        "contextWindow": chat.get("context_window", 131072),
        "bailian": {
            "apiKey": credentials.api_key,
            "baseUrl": credentials.endpoint,
            "provider": "bailian",
            "api": "openai-completions",
            "maxTokens": settings.max_reply_tokens,
            "contextWindow": 131072,
        },
        "expert": {
            "apiKey": expert.get("api_key"),
            "api": expert.get("api", "openai-responses"),
            "provider": expert.get("provider", "openai"),
            "baseUrl": expert.get("base_url", "https://api.openai.com/v1"),
            "model": models.expert_model,
            "reasoningEffort": expert.get("reasoning_effort", "high"),
            "maxTokens": expert.get("max_tokens", 16384),
            "contextWindow": expert.get("context_window", 1050000),
        },
        "temperature": 0.6,
        "thinkingBudget": settings.thinking_budget,
        "search": settings.web_search,
        "system": settings.system_prompt + TOOL_RULES + LANGUAGE_RULES + SEARCH_RULES + SPOKEN_RULES,
        "tools": skills.tools(),
    })
    brain.configure_models(models.chat_model, models.expert_model, models.chat_provider)
    logging.info("对话模型：%s / %s", models.chat_provider, models.chat_model)
    if models.expert_model:
        logging.info("困难任务模型：openai / %s / high", models.expert_model)
    return brain


class Conversation:
    """Answers what the user said. Turn state (what the turn did, how long to keep listening after it)
    lives here; it is also the provider of the end_conversation skill."""

    def __init__(self, session, ui, brain, speaker, models, prefs, settings, wake_word,
                 music, alarms, memory, device, set_phase):
        self.session = session
        self.ui = ui
        self.brain = brain
        self.speaker = speaker
        self.models = models
        self.prefs = prefs
        self.settings = settings
        self.wake_word = wake_word
        self.music = music
        self.alarms = alarms
        self.memory = memory
        self.device = device
        self.set_phase = set_phase
        self.skills = skills.Skills(device, self, music, alarms, information, memory)
        self.cue = play_cue
        self.offline_mode = False
        self.over = False
        self.music_started = False
        self.effects = []
        self.failed_tools = set()
        self.next_followup = 0.0
        self.followup_question = False
        self.local_result = ""
        self.model_turn = False

    @property
    def heard_reply(self):
        return self.speaker.heard

    def begin(self):
        """A new wake: online again, and not over yet."""
        self.offline_mode = False
        self.over = False

    def handle_turn(self, user_text, shown=True):
        self.session.turn_models = {"started_at": time.time(), "chat_model": None, "speech": None}
        self.speaker.heard = ""
        self.local_result = ""
        self.model_turn = False
        try:
            self._answer(user_text, shown)
        finally:
            # The model records its own turns; local ones are noted so it knows what happened.
            if not self.model_turn:
                reply = self.speaker.heard
                if self.session.cancel_requested.is_set() and reply:
                    reply += "（播报被打断，只听到了以上内容。）"
                if self.local_result and "action" in self.effects:
                    reply += f"（设备已执行：{self.local_result}）"
                self.brain.note(user_text, reply)

    def _answer(self, user_text, shown):
        logging.info("用户：%s", user_text)
        phrase = self.wake_word.phrase
        stripped_text = strip_wake_prefix(user_text, phrase)
        if phrase and user_text and not stripped_text:
            # Only the wake word again: the user is still to say what they want.
            if self.alarms.ringing:
                self.alarms.dismiss()
            self.cue(LISTEN_TONE)
            self.followup_question = False
            self.next_followup = (BRIEFING_WAIT_SECONDS if self.alarms.briefing_at is not None
                                  else self.settings.speech_wait_seconds)
            return
        if shown:
            self.ui.emit("user", text=user_text)
        self.set_phase("think")
        self.music_started = False
        self.over = False
        self.effects = []
        self.failed_tools = set()
        self.next_followup = 0.0
        local = skills.match(stripped_text, self.music.active(), phrase, self.offline_mode)
        if local:
            name, args = local
            logging.info("本地指令：%s %s", name, json.dumps(args, ensure_ascii=False))
            reply, ok, _ = self.call_tool(name, args, False)
            if ok and self._quiet(name, args):
                self.local_result = self.speaker.heard = reply
                self.ui.emit("reply", text=reply)
                self.cue(OK_TONE)
                logging.info("助手（提示音）：%s", reply)
                return
        elif self.offline_mode:
            reply = skills.OFFLINE_HINT
        else:
            self.model_turn = True
            reply = self.chat(user_text)
            logging.info("助手：%s", reply)
            self.next_followup = self.followup_window(reply)
            return
        self.local_result = reply
        self.speaker.say(reply, self.offline_mode)
        logging.info("助手：%s", reply)
        self.next_followup = self.followup_window(reply)

    def _quiet(self, name, args):
        if name == "set_volume":
            return "mute" not in args
        action = args.get("action")
        return name == "control_music" and (action in QUIET_MUSIC_ACTIONS or action == "resume" and self.music_started)

    def followup_window(self, reply):
        """Seconds to keep listening without the wake word, judged by what this turn did.

        A turn that only carried out actions (music, volume, alarms) is finished; a reply
        that asks something waits longer; anything else gets a short window that closes
        silently if the user does not speak."""
        if self.over or self.music_started or self.offline_mode:
            return 0.0
        self.followup_question = bool(QUESTION_END.search(reply or ""))
        if self.followup_question:
            return self.settings.question_followup_seconds
        if self.effects and all(effect == "action" for effect in self.effects):
            logging.info("指令已完成，结束对话")
            return 0.0
        return self.settings.followup_seconds

    def finish(self, screen_off, stop_music=False):
        self.over = True
        self.session.screen_off_pending = self.session.screen_off_pending or screen_off
        if stop_music:
            # Also clears the conversation hold, so unducking will not resume it.
            self.music.control("stop")
        logging.info("用户结束对话%s%s", "，关闭屏幕" if screen_off else "", "，停止音乐" if stop_music else "")

    def end_conversation(self, args):
        self.finish(bool(args.get("screen_off")), bool(args.get("stop_music")))
        return Result("好的", True)

    def run_tool(self, name, args, has_reply):
        """Returns (message, ok, final); final results are spoken as-is instead of going back to the model.

        A tool that fails twice in one turn ends it with its error rather than letting the model retry."""
        logging.info("工具调用：%s %s", name, json.dumps(args, ensure_ascii=False))
        message, ok, final = self.call_tool(name, args, has_reply)
        if not ok:
            final = final or name in self.failed_tools
            self.failed_tools.add(name)
        return message, ok, final

    def call_tool(self, name, args, has_reply):
        self.session.check_cancel()
        result, effect, final = self.skills.run(name, args, has_reply)
        if effect:
            self.effects.append(effect)
        self.music_started = self.music_started or result.started
        return result.message, result.ok, final

    def context(self):
        """The device state sent ahead of each user message."""
        scheduled = self.alarms.manage_alarm({"action": "list"}).message
        return (
            f"用户会用“{self.wake_word.phrase}”称呼你，这就是你的名字。"
            f"设备当前本地时间是{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}。"
            f"{information.current_time()}"
            f"设备默认天气城市：{information.home_city()}。"
            f"{self.device.status_summary()}"
            f"当前闹钟与计时计划：{scheduled}。"
            + self.music.describe()
            + self.memory.describe()
            + self.gap()
        )

    def gap(self):
        gap = time.monotonic() - self.brain.last_active
        if not self.brain.messages or gap < CONTEXT_GAP_SECONDS:
            return ""
        return (f"上一次对话结束于约{round(gap / 60)}分钟前：用户提到之前聊过的内容（如“刚才”“那首歌”“那家店”）时接着之前的对话回答，"
                "否则当作新话题，不要牵扯之前的内容。")

    def chat(self, user_text):
        """Asks the model and speaks its reply as it streams; returns what was heard."""
        session, models, speaker = self.session, self.models, self.speaker
        speaker.heard = ""
        spoken = []
        force_tool = skills.forced(user_text)
        search = not force_tool and not TIME_WORDS.search(user_text)
        expert = bool(models.expert_model and (
            EXPERT_REQUEST.search(user_text) or (self.prefs.thinking and HARD_TASK_WORDS.search(user_text))
        ))
        session.turn_models.update(chat_model=models.expert_model if expert else models.chat_model,
                                   chat_provider=models.expert_provider if expert else models.chat_provider,
                                   role="expert" if expert else "chat")
        # Qwen rejects a forced tool choice while thinking.
        thinking = self.prefs.thinking and not force_tool and bool(THINK_WORDS.search(user_text))
        if expert:
            logging.info("本轮使用 %s，推理档位 high", models.expert_model)
        elif thinking:
            logging.info("本轮开启思考")

        model_failed = False

        def model_events():
            nonlocal model_failed
            try:
                yield from self.brain.chat(user_text, self.context(), thinking, search, force_tool,
                                           session.check_cancel, expert=expert)
            except Exception:
                model_failed = True
                raise

        def fragments():
            nonlocal model_failed
            cleaner = SpokenStream()
            for event in model_events():
                if event[0] == "delta":
                    text = cleaner.feed(event[1])
                    if text:
                        spoken.append(text)
                        yield text
                    continue
                text = cleaner.flush()
                if text:
                    spoken.append(text)
                    yield text
                _, call, name, args = event
                try:
                    message, ok, final = self.run_tool(name, args, bool("".join(spoken).strip()))
                    self.brain.tool_result(call, message, ok, final)
                except Exception:
                    model_failed = True
                    raise
                if final and message:
                    spoken.append(message)
                    yield message
            text = cleaner.flush()
            if text:
                spoken.append(text)
                yield text

        stream = fragments()
        reply = ""
        try:
            first = next(stream, None)
            if first is not None:
                try:
                    reply = speaker.stream(chain((first,), stream))
                except (TurnCancelled, AudioStalled):
                    raise
                except Exception:
                    if model_failed:
                        raise
                    logging.warning("流式播报失败，完成当前回答后改用本地播报")
                    for _ in stream:
                        session.check_cancel()
                    speaker.local("".join(spoken).strip())
                    reply = speaker.heard
        finally:
            stream.close()
            self.brain.heard(speaker.heard, interrupted=session.cancel_requested.is_set() or not reply)
        if not reply:
            raise RuntimeError("language model returned an empty reply")
        return reply
