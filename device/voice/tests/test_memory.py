import json
import os
import tempfile
import unittest

from memory import MAX_ITEMS, MAX_TEXT, Memory


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "memory.json")
        self.memory = Memory(self.path)

    def tearDown(self):
        self.dir.cleanup()

    def run_action(self, **args):
        return self.memory.manage_memory(args)

    def test_add_persists_and_echoes_in_second_person(self):
        result = self.run_action(action="add", text="我女儿叫小米")
        self.assertEqual(result, ("好的，记住了：你女儿叫小米", True, False))
        self.assertEqual([item["text"] for item in Memory(self.path).items], ["我女儿叫小米"])
        self.assertEqual(self.run_action(action="add", text="我女儿叫小米").message, "我记着呢：你女儿叫小米")
        self.assertEqual(len(self.memory.items), 1)

    def test_update_forget_clear(self):
        self.run_action(action="add", text="我不吃香菜")
        ident = self.memory.items[0]["id"]
        self.assertTrue(self.run_action(action="update", id=ident, text="我不吃葱").ok)
        self.assertEqual(Memory(self.path).items[0], dict(self.memory.items[0], text="我不吃葱"))
        self.assertFalse(self.run_action(action="update", id="zzzz", text="x").ok)
        self.assertFalse(self.run_action(action="forget", id="zzzz").ok)
        self.assertEqual(self.run_action(action="forget", id=ident).message, "好的，已经忘掉：你不吃葱")
        self.run_action(action="add", text="我住在西湖边")
        self.assertTrue(self.run_action(action="clear").ok)
        self.assertEqual(Memory(self.path).items, [])

    def test_list_and_describe(self):
        self.assertEqual(self.memory.describe(), "")
        self.assertEqual(self.run_action(action="list").message, "目前还没有记住任何事")
        self.run_action(action="add", text="我女儿叫小米")
        ident = self.memory.items[0]["id"]
        self.assertIn(f"[{ident}]我女儿叫小米", self.memory.describe())
        self.assertNotIn(ident, self.run_action(action="list").message)

    def test_rejects_bad_input(self):
        self.assertFalse(self.run_action(action="add", text="  ").ok)
        self.assertFalse(self.run_action(action="rename").ok)
        self.run_action(action="add", text="我" + "很" * 100 + "  高")
        self.assertEqual(len(self.memory.items[0]["text"]), MAX_TEXT)
        self.memory.items = [{"id": f"{i:04x}", "text": str(i)} for i in range(MAX_ITEMS)]
        self.assertFalse(self.run_action(action="add", text="再多一条").ok)

    def test_failed_save_rolls_back(self):
        broken = Memory(os.path.join(self.dir.name, "missing", "memory.json"))
        self.assertFalse(broken.manage_memory({"action": "add", "text": "我不吃香菜"}).ok)
        self.assertEqual(broken.items, [])

    def test_corrupt_file_loads_empty(self):
        with open(self.path, "w", encoding="utf-8") as target:
            target.write("{broken")
        self.assertEqual(Memory(self.path).items, [])
        with open(self.path, "w", encoding="utf-8") as target:
            json.dump([{"id": "a1", "text": "ok"}, {"text": "no id"}, "junk"], target)
        self.assertEqual(Memory(self.path).items, [{"id": "a1", "text": "ok"}])


if __name__ == "__main__":
    unittest.main()
