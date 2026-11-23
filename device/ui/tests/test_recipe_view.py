import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import recipe_view as V

RECIPE = {"active": True, "title": "红烧肉", "view": "steps", "index": 3, "ingredients": [],
          "steps": [{"text": "a", "minutes": 0}, {"text": "b", "minutes": 0}, {"text": "c", "minutes": 0},
                    {"text": "炖", "minutes": 40}, {"text": "收汁", "minutes": 0}]}


def make_view(alarm_items=()):
    app = SimpleNamespace(recipe=dict(RECIPE), alarm_items=list(alarm_items), link=Mock(), dirty=set())
    return V.RecipeView(app), app


def timer(step, ident, seconds=600):
    return {"kind": "timer", "label": f"红烧肉·第{step}步", "due": time.time() + seconds, "id": ident}


class TimerChipTests(unittest.TestCase):
    def test_current_step_timer_starts_with_one_tap(self):
        view, app = make_view()
        self.assertEqual(view.timers(), (None, []))
        view.tap_step_timer()
        app.link.send.assert_called_once_with("recipe", a="timer")

    def test_running_timer_cancels_on_the_second_tap(self):
        view, app = make_view([timer(4, "t4"), timer(1, "t1", 30)])
        mine, others = view.timers()
        self.assertEqual((mine["id"], [t["id"] for t in others]), ("t4", ["t1"]))
        view.tap_step_timer()
        app.link.send.assert_not_called()
        self.assertTrue(view.armed_for("t4"))
        view.tap_step_timer()
        app.link.send.assert_called_once_with("alarm_cmd", a="cancel", id="t4")

    def test_confirmation_expires_and_is_per_target(self):
        view, app = make_view([timer(1, "t1"), timer(2, "t2")])
        view.confirm("t1")
        view.confirm("t2")  # a different chip re-arms instead of cancelling
        app.link.send.assert_not_called()
        with patch.object(V.time, "monotonic", return_value=time.monotonic() + V.CONFIRM_SECONDS + 1):
            self.assertIn("recipe_exit", view.tick(time.monotonic() + V.CONFIRM_SECONDS + 1))
            view.confirm("t2")
        app.link.send.assert_not_called()

    def test_exit_needs_two_taps(self):
        view, app = make_view()
        view.confirm("exit", "stop")
        view.confirm("exit", "stop")
        app.link.send.assert_called_once_with("recipe", a="stop")

    def test_no_step_timer_on_the_ingredient_list(self):
        view, app = make_view([timer(4, "t4")])
        app.recipe["view"] = "ingredients"
        self.assertIsNone(view.current_step())
        self.assertEqual([t["id"] for t in view.timers()[1]], ["t4"])
        self.assertIn("t4", view.layout(False))


if __name__ == "__main__":
    unittest.main()
