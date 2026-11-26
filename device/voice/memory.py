import json
import logging
import threading
import time
import uuid

from shared import system
from skills import Result


STATE_FILE = "/data/thinksmart/music/memory.json"
MAX_ITEMS = 60
MAX_TEXT = 80


class Memory:
    """Facts about the household the user asked to keep; every turn's context carries them."""

    def __init__(self, state_file=STATE_FILE):
        self.state_file = state_file
        self.lock = threading.Lock()
        self.items = self._load()

    def _load(self):
        try:
            with open(self.state_file, encoding="utf-8") as source:
                data = json.load(source)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as error:
            logging.error("无法读取长期记忆：%s", error)
            return []
        return [item for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)
                and isinstance(item.get("text"), str)][:MAX_ITEMS] if isinstance(data, list) else []

    def _save(self):
        system.write_json(self.state_file, self.items)

    def describe(self):
        with self.lock:
            if not self.items:
                return ""
            return ("用户让你长期记住的事（以用户口吻记录，“我”指用户；编号只用于manage_memory传参，不要读出）："
                    + "；".join(f"[{item['id']}]{item['text']}" for item in self.items) + "。")

    def manage_memory(self, args):
        action = args.get("action")
        text = " ".join(str(args.get("text") or "").split())[:MAX_TEXT]
        ident = str(args.get("id") or "").strip()
        with self.lock:
            if action == "list":
                if not self.items:
                    return Result("目前还没有记住任何事", True)
                return Result("已记住（以用户口吻记录）：" + "；".join(item["text"] for item in self.items), True)
            before = list(self.items)
            if action == "clear":
                self.items = []
                reply = "好的，已经全部忘掉了"
            elif action == "forget":
                matches = [item for item in self.items if item["id"] == ident]
                if not matches:
                    return Result("没有找到这条记忆，请先查看记住的内容", False)
                self.items.remove(matches[0])
                reply = f"好的，已经忘掉：{matches[0]['text'].replace('我', '你')}"
            elif action in ("add", "update"):
                if not text:
                    return Result("请说出要记住的内容", False)
                index = next((i for i, item in enumerate(self.items) if item["id"] == ident), None) if ident else None
                if action == "update" and index is None:
                    return Result("没有找到要更新的记忆", False)
                # Kept in the user's own words; the confirmation speaks them back to the user.
                echo = text.replace("我", "你")
                if any(item["text"] == text for item in self.items):
                    return Result(f"我记着呢：{echo}", True)
                if index is not None:
                    self.items[index] = dict(self.items[index], text=text, at=round(time.time()))
                else:
                    if len(self.items) >= MAX_ITEMS:
                        return Result("记住的内容已经太多，请先让我忘掉一些", False)
                    ident = uuid.uuid4().hex[:4]
                    while any(item["id"] == ident for item in self.items):
                        ident = uuid.uuid4().hex[:4]
                    self.items.append({"id": ident, "text": text, "at": round(time.time())})
                reply = f"好的，记住了：{echo}"
            else:
                return Result("不支持这个操作", False)
            try:
                self._save()
            except OSError as error:
                self.items = before
                logging.warning("保存长期记忆失败：%s", error)
                return Result("记忆保存失败，请稍后再试", False)
        logging.info("长期记忆：%s %s", action, text or ident)
        return Result(reply, True)
