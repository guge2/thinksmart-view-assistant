import time
import unittest

from brain import HISTORY_TURNS, MAX_STEPS, Brain


def text(reply):
    return {"role": "assistant", "content": [{"type": "text", "text": reply}]}


def call(ident, name, args=None):
    return {"role": "assistant", "content": [{"type": "toolCall", "id": ident, "name": name, "arguments": args or {}}]}


class Scripted:
    """Stands in for one provider stream per model request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.tool_choices = []

    def __call__(self, selected, context, thinking, search, tool_choice, timeout, check_cancel):
        self.tool_choices.append(tool_choice)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        for block in reply["content"]:
            if block["type"] == "text":
                yield "delta", block["text"]
        yield "_assistant", reply


class BrainTest(unittest.TestCase):
    def setUp(self):
        self.brain = Brain({"provider": "bailian", "model": "m", "api": "openai-completions", "apiKey": "sk-secret"})

    def turn(self, user, stream, results=()):
        """Runs one turn; results answers each tool call as (text, ok, final)."""
        self.brain._stream_completions = stream
        results, events = list(results), []
        for event in self.brain.chat(user, "ctx", False, True, None, lambda: None):
            if event[0] == "delta":
                events.append(event)
            else:
                events.append(("tool",) + event[2:])
                self.brain.tool_result(event[1], *results.pop(0))
        return events

    def test_tool_round_trip(self):
        stream = Scripted(call("c1", "get_weather", {"day": 1}), text("明天小雨"))
        events = self.turn("明天天气", stream, [("小雨，15度", True, False)])
        self.assertEqual(events, [("tool", "get_weather", {"day": 1}), ("delta", "明天小雨")])
        self.brain.heard("明天小雨")
        roles = [m["role"] for m in self.brain.messages]
        self.assertEqual(roles, ["user", "assistant", "toolResult", "assistant"])
        self.assertEqual(self.brain.messages[2]["text"], "小雨，15度")

    def test_final_tool_result_ends_turn(self):
        stream = Scripted(call("c1", "set_volume"), text("不该再请求"))
        self.turn("大声点", stream, [("音量60%", True, True)])
        self.assertEqual(len(stream.replies), 1)

    def test_last_step_forbids_tools(self):
        stream = Scripted(*[call(f"c{i}", "get_weather") for i in range(MAX_STEPS - 1)], text("算了"))
        self.turn("天气", stream, [("失败", False, False)] * (MAX_STEPS - 1))
        self.assertEqual(stream.tool_choices, [None] * (MAX_STEPS - 1) + ["none"])

    def test_interrupted_reply(self):
        self.turn("讲个故事", Scripted(text("从前有座山，山里有座庙")))
        self.brain.heard("从前有座山", interrupted=True)
        self.assertIn("从前有座山（播报被用户打断", self.brain.messages[-1]["content"][0]["text"])
        self.turn("你好", Scripted(text("你好")))
        self.brain.heard("")
        self.assertEqual(len(self.brain.messages), 2)

    def test_errors_hide_the_key(self):
        with self.assertRaises(RuntimeError) as caught:
            self.turn("你好", Scripted(OSError("bad key sk-secret")))
        self.assertNotIn("sk-secret", str(caught.exception))

    def test_history_window(self):
        for i in range(HISTORY_TURNS + 2):
            self.brain.note(f"问{i}", f"答{i}")
        users = [m["content"] for m in self.brain.messages if m["role"] == "user"]
        self.assertEqual(users, [f"问{i}" for i in range(2, HISTORY_TURNS + 2)])

    def test_context_only_on_newest_user_message(self):
        self.brain.note("问0", "答0")
        self.brain.messages.append({"role": "user", "content": "问1"})
        self.assertEqual(self.brain._user_texts("状态"), {0: "问0", 2: "【设备实时状态】状态\n【用户说】问1"})

    def test_forget_if_idle(self):
        self.brain.note("问", "答")
        self.brain.forget_if_idle(0)
        self.brain.forget_if_idle(7200)
        self.assertEqual(len(self.brain.messages), 2)
        self.brain.last_active = time.monotonic() - 7201
        self.brain.forget_if_idle(7200)
        self.assertEqual(self.brain.messages, [])


if __name__ == "__main__":
    unittest.main()
