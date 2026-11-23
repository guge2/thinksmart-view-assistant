import json
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import recipe as R
import skills
from skills import Result

ARGS = {"action": "start", "title": "番茄炒蛋", "ingredients": ["番茄 2个", "鸡蛋 3个", ""],
        "steps": [{"text": "番茄切块，鸡蛋打散", "minutes": 0},
                  {"text": "炒蛋盛出", "minutes": 0},
                  {"text": "番茄炒软后焖一会儿。", "minutes": 3},
                  {"text": "倒回鸡蛋，调味出锅", "minutes": 0}]}


class RecipeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "recipe.json")
        self.emit = Mock()
        self.alarms = Mock()
        self.alarms.manage_alarm.return_value = Result("已设置计时器", True)
        self.service = R.RecipeService(self.emit, self.alarms, self.path)

    def say(self, action, **args):
        return self.service.manage_recipe(dict(args, action=action))

    def test_start_shows_ingredients_first(self):
        result = self.service.manage_recipe(ARGS)
        self.assertTrue(result.ok)
        self.assertIn("一共4步", result.message)
        snapshot = self.service.snapshot()
        self.assertEqual((snapshot["index"], snapshot["view"]), (-1, "ingredients"))
        self.assertEqual(snapshot["ingredients"], ["番茄 2个", "鸡蛋 3个"])
        self.emit.assert_called_with("recipe", **snapshot)
        self.assertIn("还没开始", self.service.describe())

    def test_invalid_recipes(self):
        for args in ({"action": "start"}, {"action": "start", "title": "汤", "steps": []},
                     {"action": "start", "title": "汤", "steps": [{"text": " "}]},
                     dict(ARGS, steps=[{"text": "x"}] * 21)):
            with self.subTest(args=args):
                self.assertFalse(self.service.manage_recipe(args).ok)
        self.assertFalse(self.service.active())
        self.assertFalse(self.say("next").ok)

    def test_walk_through_with_one_timer(self):
        self.service.manage_recipe(ARGS)
        self.assertEqual(self.say("next").message, "第1步，番茄切块，鸡蛋打散。")
        self.assertEqual(self.say("previous").message, "已经是第一步了")
        self.say("next")
        third = self.say("next").message
        self.assertEqual(third, "第3步，番茄炒软后焖一会儿。已开始3分钟计时。")
        self.alarms.manage_alarm.assert_called_once_with(
            {"action": "create", "kind": "timer", "minutes": 3, "label": "番茄炒蛋·第3步"})
        self.say("previous")
        self.say("next")
        self.assertEqual(self.alarms.manage_alarm.call_count, 1)  # coming back does not start it again
        self.assertTrue(self.say("timer").ok)
        self.assertEqual(self.alarms.manage_alarm.call_count, 2)  # unless asked
        self.assertIn("这是最后一步", self.say("next").message)
        self.assertIn("全部完成", self.say("next").message)
        self.assertFalse(self.service.active())
        self.emit.assert_called_with("recipe", active=False)

    def test_goto_repeat_ingredients(self):
        self.service.manage_recipe(ARGS)
        self.assertFalse(self.say("repeat").ok)
        self.assertFalse(self.say("goto", step=9).ok)
        self.assertFalse(self.say("goto", step=True).ok)
        self.assertTrue(self.say("goto", step=2).message.startswith("第2步"))
        self.assertEqual(self.say("repeat").message, "第2步，炒蛋盛出。")
        self.assertFalse(self.say("timer").ok)  # step 2 has no wait
        self.assertEqual(self.say("ingredients").message, "需要准备：番茄 2个、鸡蛋 3个")
        self.assertEqual(self.service.snapshot()["view"], "ingredients")
        self.assertEqual(self.say("stop").message, "好的，已退出菜谱")

    def test_failed_timer_is_said(self):
        self.alarms.manage_alarm.return_value = Result("已达到数量上限，请先取消一些", False)
        self.service.manage_recipe(ARGS)
        self.assertIn("计时器没能启动", self.say("goto", step=3).message)
        self.assertFalse(self.say("timer").ok)

    def test_screen_taps_are_silent(self):
        self.service.manage_recipe(ARGS)
        self.service.ui_command("next")
        self.assertEqual(self.service.snapshot()["index"], 0)
        self.service.ui_command("goto", step=4)
        with self.assertRaises(ValueError):
            self.service.ui_command("goto", step=5)
        with self.assertRaises(ValueError):
            self.service.ui_command("start")
        self.service.ui_command("stop")
        with self.assertRaises(ValueError):
            self.service.ui_command("next")

    def test_survives_a_restart_but_not_a_day(self):
        self.service.manage_recipe(ARGS)
        self.say("goto", step=3)
        again = R.RecipeService(None, self.alarms, self.path)
        self.assertEqual(again.snapshot()["index"], 2)
        self.assertEqual(again.recipe["timers"], [2])
        with open(self.path) as source:
            saved = json.load(source)
        saved["at"] = time.time() - R.EXPIRE_SECONDS - 1
        with open(self.path, "w") as target:
            json.dump(saved, target)
        self.assertFalse(R.RecipeService(None, self.alarms, self.path).active())
        self.say("stop")
        self.assertFalse(R.RecipeService(None, self.alarms, self.path).active())

    def test_corrupt_state_is_ignored(self):
        with open(self.path, "w") as target:
            target.write('{"title": 1}')
        with self.assertLogs(level="WARNING"):
            self.assertFalse(R.RecipeService(None, self.alarms, self.path).active())

    def test_unsaved_state_still_works(self):
        with patch.object(R.system, "write_atomic", side_effect=OSError("read-only")), self.assertLogs(level="WARNING"):
            self.assertTrue(self.service.manage_recipe(ARGS).ok)
        self.assertTrue(self.service.active())


class PhraseTests(unittest.TestCase):
    def test_navigation_only_while_cooking(self):
        cases = [("下一步", {"action": "next"}), ("好了下一步吧", {"action": "next"}),
                 ("上一步", {"action": "previous"}), ("再说一遍", {"action": "repeat"}),
                 ("跳到第三步", {"action": "goto", "step": 3}), ("第12步", {"action": "goto", "step": 12}),
                 ("要准备什么", {"action": "ingredients"}), ("需要哪些食材", {"action": "ingredients"}),
                 ("帮我计时", {"action": "timer"}), ("不做了", {"action": "stop"})]
        for text, args in cases:
            with self.subTest(text=text):
                self.assertEqual(skills.match(text, False, "小度小度", recipe_active=True), ("manage_recipe", args))
                self.assertIsNone(skills.match(text, False, "小度小度"))

    def test_music_commands_still_win(self):
        self.assertEqual(skills.match("下一首", True, recipe_active=True), ("control_music", {"action": "next"}))
        self.assertEqual(skills.match("暂停", True, recipe_active=True), ("control_music", {"action": "pause"}))

    def test_teach_me_forces_the_recipe(self):
        self.assertEqual(skills.forced("教我做红烧肉"), "manage_recipe")
        self.assertEqual(skills.forced("带我炒个青菜"), "manage_recipe")
        self.assertIsNone(skills.forced("红烧肉怎么做"))


if __name__ == "__main__":
    unittest.main()
