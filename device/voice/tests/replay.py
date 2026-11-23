"""Replays conversations through the assistant's real turn handling and chat model on the device,
without the microphone or speaker. Tools that would change the device are recorded, not run;
memory and alarms run in memory only. Start it with replay.sh.

Each step either says something and checks the outcome, or moves time forward:
  tool: a skill this turn must call; None means no skill at all
  args: arguments that call must include; a compiled pattern matches the value as text
  local: True when the phrase must be handled without the model, False when it must reach it
  not_tool / reply / not_reply: a skill it must not call, patterns the reply must or must not match
  gap: minutes since the last turn ended, as if the user woke the device again"""

import dataclasses
import logging
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import assistant as A  # noqa: E402
from config import Settings  # noqa: E402
from conversation import BRIEFING_TEXT  # noqa: E402
from skills import Result  # noqa: E402

DEVICE_SKILLS = ("control_device", "play_music", "control_music", "set_volume", "end_conversation")
DAUGHTER = [{"id": "a1b2", "text": "我女儿叫小米"}]

CASES = [
    ("记住一件事", [], [
        {"say": "记住我女儿叫小米", "tool": "manage_memory", "args": {"action": "add", "text": re.compile("小米")},
         "reply": "小米"},
    ]),
    ("闲聊不擅自记录", [], [
        {"say": "我今天上班好累啊", "not_tool": "manage_memory"},
    ]),
    ("用上记忆", DAUGHTER, [
        {"say": "我女儿叫什么来着", "tool": None, "reply": "小米"},
        {"say": "你都记得我什么", "tool": "manage_memory", "args": {"action": "list"}, "reply": "小米"},
        {"say": "忘掉我女儿的名字", "tool": "manage_memory", "args": {"action": "forget", "id": "a1b2"}},
    ]),
    ("早安简报", [], [
        {"say": "早上好", "tool": "daily_briefing", "reply": "度|℃|雨|晴|阴|云"},
        {"say": BRIEFING_TEXT, "tool": "daily_briefing"},
    ]),
    ("天气", [], [
        {"say": "明天杭州会下雨吗", "tool": "get_weather", "args": {"day": 1}},
    ]),
    ("闹钟", [], [
        {"say": "明天早上七点叫我起床", "tool": "manage_alarm",
         "args": {"action": "create", "time": re.compile(r"^07:00"), "days_from_today": 1}},
        {"say": "刚才那个闹钟取消吧", "tool": "manage_alarm", "args": {"action": "cancel"}},
    ]),
    ("点歌", [], [
        {"say": "我想听周杰伦", "local": True, "tool": "play_music"},
        {"say": "放点适合写作业听的歌", "local": False, "tool": "play_music"},
    ]),
    ("菜谱", [], [
        {"say": "教我做番茄炒蛋", "tool": "manage_recipe", "args": {"action": "start"}},
        {"say": "下一步", "local": True, "tool": "manage_recipe", "reply": "第1步"},
        {"say": "这一步火要开多大", "local": False, "not_tool": "end_conversation"},
        {"say": "不做了", "local": True, "tool": "manage_recipe"},
    ]),
    ("设备设置", [], [
        {"say": "屏幕太亮了", "tool": "control_device"},
    ]),
    ("结束对话", [], [
        {"say": "好了没事了", "local": True, "tool": "end_conversation"},
        {"say": "我先去忙了你也歇会儿", "tool": "end_conversation"},
    ]),
    ("隔一段时间再问", [], [
        {"say": "苹果和香蕉哪个热量更高"},
        {"gap": 30},
        {"say": "那刚才那个低的一根大概多少卡", "reply": "苹果"},
        {"gap": 40},
        {"say": "帮我想一个周末带孩子去的地方", "not_reply": "苹果|香蕉"},
    ]),
]


def build():
    settings = dataclasses.replace(Settings.from_env(), thinking_cue_seconds=0)
    a = A.ThinkSmartAssistant(A.UiPublisher("/run/thinksmart-replay.sock"), settings)
    conversation = a.conversation
    conversation.cue = lambda pcm: None
    conversation.memory._save = a.alarms._save = lambda: None
    a.recipe.state_file = os.path.join(tempfile.mkdtemp(prefix="thinksmart-replay-"), "recipe.json")

    def speak(fragments):
        a.speaker.heard = "".join(fragments).strip()
        return a.speaker.heard
    a.speaker.stream = speak
    registry = conversation.skills
    for name in DEVICE_SKILLS:
        registry.handlers[name] = lambda args: Result("好的", True)
    calls = []
    run = registry.run

    def recording(name, args, has_reply=False):
        calls.append((name, args if isinstance(args, dict) else {}))
        return run(name, args, has_reply)
    registry.run = recording
    return a, calls


def check(step, calls, reply, local):
    names = [name for name, _ in calls]
    problems = []
    if "tool" in step:
        if step["tool"] is None and names:
            problems.append("不该调用工具")
        elif step["tool"] and step["tool"] not in names:
            problems.append(f"没有调用 {step['tool']}")
    if step.get("not_tool") in names:
        problems.append(f"不该调用 {step['not_tool']}")
    if "local" in step and step["local"] != local:
        problems.append("应由本地处理" if step["local"] else "应交给模型")
    for key, expected in step.get("args", {}).items():
        values = [args.get(key) for name, args in calls if name == step.get("tool")]
        if values and not any(expected.search(str(v)) if isinstance(expected, re.Pattern) else v == expected
                              for v in values):
            problems.append(f"{key}={values} 不符合 {getattr(expected, 'pattern', expected)}")
    if step.get("reply") and not re.search(step["reply"], reply):
        problems.append(f"回复里没有 /{step['reply']}/")
    if step.get("not_reply") and re.search(step["not_reply"], reply):
        problems.append(f"回复里不该有 /{step['not_reply']}/")
    return problems


def main(filters):
    a, calls = build()
    failed = total = 0
    for title, memory, steps in CASES:
        if filters and not any(word in title for word in filters):
            continue
        print(f"== {title}")
        a.conversation.memory.items = [dict(item) for item in memory]
        a.alarms.items = []
        a.recipe.recipe = None
        a.brain.messages = []
        for step in steps:
            if "gap" in step:
                a.brain.last_active -= step["gap"] * 60
                print(f"   （{step['gap']}分钟后再次唤醒）")
                continue
            a.brain.forget_if_idle(a.settings.context_idle_seconds)
            calls.clear()
            started = time.monotonic()
            try:
                a.conversation.handle_turn(step["say"])
                problems = check(step, calls, a.conversation.heard_reply, not a.conversation.model_turn)
            except Exception as error:
                problems = [f"出错：{error}"]
            total += 1
            failed += bool(problems)
            print(f"{'✗' if problems else '✓'} 用户：{step['say']}  （{time.monotonic() - started:.1f}秒）")
            for name, args in calls:
                print(f"   工具：{name} {args}")
            print(f"   助手：{a.conversation.heard_reply}")
            for problem in problems:
                print(f"   ！{problem}")
    print(f"\n{total - failed}/{total} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    code = main(sys.argv[1:])
    sys.stdout.flush()
    os._exit(code)  # the assistant's background threads would keep the process alive
