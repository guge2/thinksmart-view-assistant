"""Kitchen recipe mode. The model writes a recipe as an ingredient list and short steps; the user then
goes through it by voice ("下一步", "上一步", "再说一遍", "第三步") or on the screen. A step that waits
("小火炖40分钟") offers a timer named after the dish and step; it starts only when asked ("开始计时" or a
tap), since the user knows when the pot is actually on."""

import json
import logging
import threading
import time

from shared import system
from skills import Result

STATE_FILE = "/data/thinksmart/music/recipe.json"
MAX_TITLE = 20
MAX_INGREDIENTS = 30
MAX_INGREDIENT = 40
MAX_STEPS = 20
MAX_STEP = 120
MAX_MINUTES = 600
# A recipe left this long is forgotten, so the screen does not reopen it days later.
EXPIRE_SECONDS = 8 * 3600
ACTIONS = ("next", "previous", "repeat", "goto", "ingredients", "timer", "stop")


def _text(value, limit):
    return " ".join(str(value or "").split())[:limit]


def _minutes(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, min(MAX_MINUTES, round(value)))


def timer_label(title, index):
    return f"{title}·第{index + 1}步"


def parse_recipe(args):
    """The recipe from the start action's arguments, cleaned; raises ValueError when unusable."""
    title = _text(args.get("title"), MAX_TITLE)
    raw_steps = args.get("steps")
    steps = []
    for step in raw_steps if isinstance(raw_steps, list) else []:
        if isinstance(step, str):
            step = {"text": step}
        if isinstance(step, dict) and _text(step.get("text"), MAX_STEP):
            steps.append({"text": _text(step["text"], MAX_STEP), "minutes": _minutes(step.get("minutes"))})
    raw_ingredients = args.get("ingredients")
    ingredients = [_text(item, MAX_INGREDIENT) for item in raw_ingredients if _text(item, MAX_INGREDIENT)] \
        if isinstance(raw_ingredients, list) else []
    if not title or not steps:
        raise ValueError("菜谱需要菜名和至少一个步骤")
    if len(steps) > MAX_STEPS:
        raise ValueError(f"步骤太多，请合并到{MAX_STEPS}步以内")
    return {"title": title, "ingredients": ingredients[:MAX_INGREDIENTS], "steps": steps}


class RecipeService:
    """The recipe being cooked: its steps, where the user is (index -1 is the ingredient overview
    before the first step), and what the screen shows."""

    def __init__(self, emit=None, alarms=None, state_file=STATE_FILE):
        self.emit = emit
        self.alarms = alarms
        self.state_file = state_file
        self.lock = threading.RLock()
        self.recipe = self._load()

    def _load(self):
        try:
            with open(self.state_file, encoding="utf-8") as source:
                saved = json.load(source)
            if saved is None:
                return None
            recipe = dict(parse_recipe(saved), index=int(saved["index"]), view=saved.get("view", "steps"),
                          at=float(saved["at"]))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            logging.warning("无法读取菜谱：%s", error)
            return None
        if time.time() - recipe["at"] > EXPIRE_SECONDS or not -1 <= recipe["index"] < len(recipe["steps"]):
            return None
        return recipe

    def _changed(self):
        recipe = self.recipe
        try:
            if recipe is not None:
                recipe["at"] = time.time()
            system.write_json(self.state_file, recipe)
        except OSError as error:
            logging.warning("菜谱保存失败：%s", error)
        self.announce()

    def active(self):
        return self.recipe is not None

    def snapshot(self):
        with self.lock:
            recipe = self.recipe
            if recipe is None:
                return {"active": False}
            return {"active": True, "title": recipe["title"], "ingredients": recipe["ingredients"],
                    "steps": recipe["steps"], "index": recipe["index"], "view": recipe["view"]}

    def announce(self):
        if self.emit:
            self.emit("recipe", **self.snapshot())

    def describe(self):
        """The recipe in the model's context, so it can answer questions about the dish being cooked."""
        with self.lock:
            recipe = self.recipe
            if recipe is None:
                return ""
            steps = recipe["steps"]
            index = recipe["index"]
            where = ("还没开始，屏幕上显示食材" if index < 0
                     else f"正在第{index + 1}步：{steps[index]['text']}")
            return (f"用户正在跟着菜谱做“{recipe['title']}”，共{len(steps)}步，{where}。"
                    f"食材：{'、'.join(recipe['ingredients']) or '未列出'}。"
                    "用户问这道菜的细节（火候、替代食材等）时结合菜谱简短回答；"
                    "说下一步、上一步、第几步、开始计时或不做了时调用manage_recipe。")

    # The manage_recipe skill: every result is spoken as is.

    def manage_recipe(self, args):
        action = args.get("action")
        with self.lock:
            if action == "start":
                try:
                    recipe = parse_recipe(args)
                except ValueError as error:
                    return Result(str(error), False)
                self.recipe = dict(recipe, index=-1, view="ingredients", at=time.time())
                self._changed()
                logging.info("开始菜谱：%s，%d步", recipe["title"], len(recipe["steps"]))
                return Result(f"好的，{recipe['title']}一共{len(recipe['steps'])}步，食材已经显示在屏幕上。"
                              "准备好了就说“下一步”。", True)
            if action not in ACTIONS:
                return Result("不支持这个操作", False)
            if self.recipe is None:
                return Result("现在没有在做的菜，可以说“教我做红烧肉”", False)
            return self._navigate(action, args.get("step"), speak=True)

    def ui_command(self, action, step=None):
        """A tap on the screen: the same moves without the spoken text."""
        with self.lock:
            if action not in ACTIONS or self.recipe is None:
                raise ValueError("菜谱操作无效")
            result = self._navigate(action, step, speak=False)
            if not result.ok:
                raise ValueError(result.message)

    def _navigate(self, action, step, speak):
        recipe = self.recipe
        steps, index = recipe["steps"], recipe["index"]
        if action == "stop":
            self.recipe = None
            self._changed()
            return Result("好的，已退出菜谱", True)
        if action == "ingredients":
            recipe["view"] = "ingredients"
            self._changed()
            items = recipe["ingredients"]
            return Result(f"需要准备：{'、'.join(items)}" if items else "这道菜没有列出食材", True)
        if action == "next":
            if index + 1 >= len(steps):
                title = recipe["title"]
                self.recipe = None
                self._changed()
                return Result(f"{title}全部完成，慢慢享用吧。", True)
            return self._go(index + 1, speak)
        if action == "previous":
            if index <= 0:
                return Result("已经是第一步了" if index == 0 else "还没开始，说“下一步”开始第一步", index == 0)
            return self._go(index - 1, speak)
        if action == "goto":
            if isinstance(step, bool) or not isinstance(step, int) or not 1 <= step <= len(steps):
                return Result(f"这道菜只有{len(steps)}步", False)
            return self._go(step - 1, speak)
        if index < 0:
            return Result("还没开始，说“下一步”开始第一步", False)
        if action == "repeat":
            return self._go(index, speak)
        # "timer": start a timer for the current step.
        minutes = steps[index]["minutes"]
        if not minutes:
            return Result("这一步不需要计时", False)
        if self.alarms is None:
            return Result("计时器暂时不可用", False)
        result = self.alarms.manage_alarm({"action": "create", "kind": "timer", "minutes": minutes,
                                           "label": timer_label(recipe["title"], index)})
        if not result.ok:
            return Result(f"计时器没能启动：{result.message}", False)
        return Result(f"已开始{minutes}分钟计时", True)

    def _go(self, index, speak):
        recipe = self.recipe
        recipe["index"] = index
        recipe["view"] = "steps"
        step = recipe["steps"][index]
        self._changed()
        last = "这是最后一步。" if index == len(recipe["steps"]) - 1 else ""
        wait = f"这一步要等{step['minutes']}分钟，下锅后说“开始计时”。" if step["minutes"] else ""
        text = step["text"].rstrip("。.") + "。"
        return Result(f"第{index + 1}步，{text}{last}{wait}" if speak else text, True)
