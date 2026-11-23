import json
import os
import socket
import tempfile
import time
import unittest
from unittest.mock import Mock

from control import NO_ACK, ControlServer
from ui_events import UiPublisher


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.server = ControlServer({
            "ok": lambda message, sender: "accepted",
            "quiet": lambda message, sender: NO_ACK,
            "bad": Mock(side_effect=ValueError("麦克风已关闭")),
            "broken": Mock(side_effect=KeyError("secret")),
        }, lambda: False)
        self.server.send = Mock()

    def acks(self):
        return [call.args[1] for call in self.server.send.call_args_list]

    def test_acknowledgements(self):
        with self.assertLogs(level="WARNING"):
            for command in ("ok", "quiet", "bad", "broken", "missing"):
                self.server.dispatch({"c": command, "id": command}, "client")
        self.assertEqual(self.acks(), [
            {"e": "ack", "id": "ok", "ok": True, "status": "accepted"},
            {"e": "ack", "id": "bad", "ok": False, "status": "麦克风已关闭"},
            {"e": "ack", "id": "broken", "ok": False, "status": "操作失败"},
            {"e": "ack", "id": "missing", "ok": False, "status": "不支持的设备操作"},
        ])

    def test_no_sender_no_reply(self):
        self.server.dispatch({"c": "ok"}, None)
        self.server.send.assert_not_called()


class SocketTests(unittest.TestCase):
    """The real datagram round trip, as the gateway and display use it."""

    def test_round_trip(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = temporary.name
        stopped = []
        server = ControlServer({"ok": lambda m, s: "applied"}, lambda: bool(stopped), os.path.join(directory, "c.sock"))
        server.start()
        self.addCleanup(server.sock.close)
        self.addCleanup(stopped.append, True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.bind(os.path.join(directory, "client.sock"))
            client.settimeout(2)
            client.sendto(b"not json", server.path)
            client.sendto(json.dumps({"c": "ok", "id": "1"}).encode(), server.path)
            reply = json.loads(client.recv(1024))
        self.assertEqual(reply, {"e": "ack", "id": "1", "ok": True, "status": "applied"})


class UiPublisherTests(unittest.TestCase):
    def test_state_follows_events(self):
        ui = UiPublisher(path="/nonexistent/events.sock")
        self.addCleanup(ui.sock.close)
        ui.emit("wake")
        ui.emit("user", text="你好")
        ui.emit("delta", text="你")
        ui.emit("delta", text="好")
        self.assertEqual((ui.state["phase"], ui.state["user_text"], ui.state["answer"]), ("answer", "你好", "你好"))
        ui.record("mic", on=0, requested_mic=False, hardware_mic=None)
        self.assertFalse(ui.state["mic"])
        ui.emit("error", code="network")
        self.assertGreater(ui.state["error_until"], time.monotonic())
        self.assertEqual(ui.sequence, 5)

    def test_long_text_is_trimmed(self):
        ui = UiPublisher(path="/nonexistent/events.sock")
        sent = []
        ui.sock.close()
        ui.sock = Mock(sendto=lambda payload, target: sent.append(json.loads(payload)))
        ui.emit("partial", text="字" * 5000)
        self.assertEqual(len(sent[0]["text"]), 2000)
        ui.state["lyric"] = {"lines": "词" * 60000}
        ui.snapshot()
        self.assertNotIn("lyric", sent[-1])


if __name__ == "__main__":
    unittest.main()
