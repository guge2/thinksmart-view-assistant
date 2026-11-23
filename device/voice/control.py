"""The local control channel: JSON datagrams from the display and the management gateway."""

import json
import logging
import os
import socket
import threading

from config import CONTROL_SOCKET

# A handler returning this sends no acknowledgement (it replies itself, or the sender wants none).
NO_ACK = None


class ControlServer:
    """Each request is {"c": command, "id": ..., ...}. handlers maps a command to
    handler(message, sender), which returns the acknowledgement status ("accepted", "applied")
    or NO_ACK. A ValueError's message goes back to the sender; other errors report "操作失败"."""

    def __init__(self, handlers, stopped, path=CONTROL_SOCKET):
        self.handlers = handlers
        self.stopped = stopped
        self.path = path
        self.sock = None

    def start(self):
        try:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.bind(self.path)
        except OSError as error:
            logging.warning("界面控制通道不可用：%s", error)
            return
        self.sock = sock
        threading.Thread(target=self.serve, name="ui-control", daemon=True).start()

    def serve(self):
        while not self.stopped():
            try:
                data, sender = self.sock.recvfrom(2048)
            except OSError:
                return
            try:
                message = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(message, dict):
                self.dispatch(message, sender)

    def dispatch(self, message, sender):
        try:
            handler = self.handlers.get(message.get("c"))
            if handler is None:
                raise ValueError("不支持的设备操作")
            status = handler(message, sender)
            if sender and status is not NO_ACK:
                self.reply(sender, message, True, status)
        except Exception as error:
            logging.warning("控制请求失败：%s", type(error).__name__)
            if sender:
                self.reply(sender, message, False, str(error) if isinstance(error, ValueError) else "操作失败")

    def send(self, sender, payload):
        try:
            self.sock.sendto(json.dumps(payload).encode(), socket.MSG_DONTWAIT, sender)
        except OSError:
            pass

    def reply(self, sender, request, ok, status):
        self.send(sender, {"e": "ack", "id": request.get("id"), "ok": ok, "status": status})
