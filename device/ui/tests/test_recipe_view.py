import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import recipe_view as V

RECIPE = {"active": True, "title": "红烧肉", "view": "steps", "index": 3, "ingredients": [],
          "steps": [{"text": "a", "minutes": 0}, {"text": "b", "minutes": 0}, {"text": "c", "minutes": 0},
                    {"text": "炖", "minutes": 40}, {"text": "收汁", "minutes": 0}]}


def make_view(alarm_items=()):
    app = SimpleNamespace(recipe=dict(RECIPE), alarm_items=list(alarm_items), link=Mock(), dirty=set())
    return V.RecipeView(app), app


def timer(step, ident, seconds=600):
    return {"kind": "timer", "label": f"红烧肉·第{step}步", "due": time.time() + seconds, "id": ident}


class TimerButtonTests(unittest.TestCase):
    def test_one_tap_starts(self):
        view, app = make_view()
        self.assertEqual(view.timers(), (None, []))
        view.tap_step_timer()
        app.link.send.assert_called_once_with("recipe", a="timer")

    def test_one_tap_stops(self):
        view, app = make_view([timer(4, "t4"), timer(1, "t1", 30)])
        mine, others = view.timers()
        self.assertEqual((mine["id"], [t["id"] for t in others]), ("t4", ["t1"]))
        view.tap_step_timer()
        app.link.send.assert_called_once_with("alarm_cmd", a="cancel", id="t4")
        view.cancel(others[0]["id"])
        app.link.send.assert_called_with("alarm_cmd", a="cancel", id="t1")

    def test_exit_needs_two_taps(self):
        view, app = make_view()
        view.tap_exit()
        app.link.send.assert_not_called()
        view.tap_exit()
        app.link.send.assert_called_once_with("recipe", a="stop")

    def test_exit_confirmation_expires(self):
        view, app = make_view()
        view.tap_exit()
        self.assertIn("recipe_exit", view.tick(time.monotonic() + V.CONFIRM_SECONDS + 1))
        view.tap_exit()
        app.link.send.assert_not_called()

    def test_no_step_timer_on_the_ingredient_list(self):
        view, app = make_view([timer(4, "t4")])
        app.recipe["view"] = "ingredients"
        self.assertIsNone(view.current_step())
        self.assertEqual([t["id"] for t in view.timers()[1]], ["t4"])
        self.assertIn("t4", view.layout(False))


if __name__ == "__main__":
    unittest.main()
