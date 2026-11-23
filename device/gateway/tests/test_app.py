import json
import os
import sys
import tempfile
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import AioHTTPTestCase

# The gateway imports the display's system module, installed next to it on the device; appended so
# that the display's own app.py does not shadow the gateway's.
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "ui"))
import app as G  # noqa: E402
import system  # noqa: E402


class GatewayTestCase(AioHTTPTestCase):
    async def get_application(self):
        return G.make_app()

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.voice = AsyncMock(return_value={"e": "ack", "ok": True})
        patcher = patch.object(G, "voice", self.voice)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def post(self, path, body, **headers):
        headers = {"X-CSRF-Token": G.CSRF, "Content-Type": "application/json", **headers}
        return await self.client.post(path, data=json.dumps(body), headers=headers)


class GuardTests(GatewayTestCase):
    async def test_session_and_security_headers(self):
        response = await self.client.get("/api/session")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["csrf"], G.CSRF)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_other_hosts_and_origins_are_refused(self):
        cases = [{"Host": "thinksmart.local:8787"},
                 {"Origin": "http://evil.example"},
                 {"Sec-Fetch-Site": "cross-site"}]
        for headers in cases:
            with self.subTest(headers=headers):
                response = await self.client.get("/api/session", headers=headers)
                self.assertEqual(response.status, 403)
                self.assertFalse((await response.json())["ok"])

    async def test_writes_need_the_token_and_json(self):
        response = await self.post("/api/action", {"action": "talk"}, **{"X-CSRF-Token": "wrong"})
        self.assertEqual(response.status, 403)
        response = await self.client.post("/api/action", data="action=talk",
                                          headers={"X-CSRF-Token": G.CSRF,
                                                   "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(response.status, 400)
        self.voice.assert_not_called()

    async def test_only_the_page_files_are_served(self):
        self.assertEqual((await self.client.get("/")).status, 200)
        self.assertEqual((await self.client.get("/app.js")).status, 200)
        self.assertEqual((await self.client.get("/schema.py")).status, 404)
        self.assertEqual((await self.client.get("/..%2fapp.py")).status, 404)

    async def test_oversized_body_is_refused(self):
        response = await self.post("/api/action", {"action": "talk", "pad": "x" * 9000})
        self.assertEqual(response.status, 413)


class ActionTests(GatewayTestCase):
    async def test_device_actions_reach_the_voice_service(self):
        cases = [({"action": "talk"}, ("talk",), {}),
                 ({"action": "music_stop"}, ("music",), {"a": "stop"}),
                 ({"action": "sleep_timer", "minutes": 30.4}, ("music",), {"a": "sleep", "minutes": 30}),
                 ({"action": "alarm_cancel", "id": "a1b2"}, ("alarm_cmd",), {"a": "cancel", "id": "a1b2"})]
        for body, args, kwargs in cases:
            with self.subTest(body=body):
                self.voice.reset_mock()
                response = await self.post("/api/action", body)
                self.assertEqual(response.status, 200)
                self.voice.assert_awaited_once_with(*args, **kwargs)

    async def test_invalid_actions(self):
        cases = [{"action": "reboot"}, {"action": "alarm_cancel", "id": "../x"}, {"action": "timer", "minutes": 0},
                 {"action": "volume", "value": 101}, ["talk"]]
        for body in cases:
            with self.subTest(body=body):
                response = await self.post("/api/action", body)
                self.assertEqual(response.status, 400)
        self.voice.assert_not_called()

    async def test_voice_service_down(self):
        self.voice.side_effect = TimeoutError()
        response = await self.post("/api/action", {"action": "talk"})
        self.assertEqual(response.status, 503)
        self.assertIn("没有响应", (await response.json())["error"])

    async def test_refusal_from_the_voice_service_is_shown(self):
        self.voice.side_effect = ValueError("麦克风已关闭，请先打开麦克风")
        response = await self.post("/api/action", {"action": "talk"})
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"], "麦克风已关闭，请先打开麦克风")

    async def test_wake_word_is_checked_before_the_helper_runs(self):
        with patch.object(G.Gateway, "root_action", AsyncMock()) as root_action:
            for phrase in ("hi", "小", "小度小度小度小度小", 3):
                response = await self.post("/api/wake-word", {"phrase": phrase})
                self.assertEqual(response.status, 400)
            root_action.assert_not_called()
            response = await self.post("/api/wake-word", {"phrase": "小度小度"})
        self.assertEqual(response.status, 200)
        root_action.assert_awaited_once_with("wake_word", phrase="小度小度")

    async def test_restart_only_known_services(self):
        with patch.object(G.Gateway, "root_action", AsyncMock()) as root_action:
            self.assertEqual((await self.post("/api/restart", {"target": "sshd"})).status, 400)
            self.assertEqual((await self.post("/api/restart", {"target": "voice"})).status, 200)
        root_action.assert_awaited_once_with("restart", target="voice")


class PreferenceTests(GatewayTestCase):
    async def test_saved_and_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(system, "SETTINGS_FILE", os.path.join(directory, "settings.json")):
                response = await self.post("/api/preferences", {"speech_rate": 1.26, "mic": False})
                body = await response.json()
                with open(system.SETTINGS_FILE) as source:
                    saved = json.load(source)
        self.assertEqual(response.status, 200)
        self.assertEqual(body["status"], "applied")
        self.assertEqual((saved["speech_rate"], saved["mic"]), (1.3, False))
        self.voice.assert_any_await("mic", on=0)

    async def test_saved_while_voice_is_down(self):
        self.voice.side_effect = OSError()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(system, "SETTINGS_FILE", os.path.join(directory, "settings.json")):
                body = await (await self.post("/api/preferences", {"thinking": False})).json()
        self.assertEqual(body["status"], "saved")


class LogTests(GatewayTestCase):
    async def test_logs_hide_secrets_and_conversation(self):
        lines = [
            {"MESSAGE": "INFO 用户：我的银行卡密码是多少", "__REALTIME_TIMESTAMP": "1000000"},
            {"MESSAGE": "WARNING Authorization: Bearer abc", "__REALTIME_TIMESTAMP": "2000000"},
            {"MESSAGE": "ERROR key sk-abcdef123 rejected", "__REALTIME_TIMESTAMP": "3000000"},
            {"MESSAGE": list("INFO 字节".encode()), "__REALTIME_TIMESTAMP": "4000000"},
        ]
        output = "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\nnot json"
        with patch.object(G, "command", AsyncMock(return_value=output)) as command:
            response = await self.client.get("/api/logs?target=gateway")
            rows = (await response.json())["rows"]
        self.assertIn("thinksmart-gateway.service", command.call_args.args)
        self.assertEqual([row["message"] for row in rows], [
            "INFO 用户：[内容已省略]", "[已隐藏包含敏感字段的日志]", "ERROR key [已隐藏密钥] rejected", "INFO 字节"])
        self.assertEqual([row["level"] for row in rows], ["info", "info", "error", "info"])
        self.assertEqual((await self.client.get("/api/logs?target=sshd")).status, 400)


if __name__ == "__main__":
    import unittest
    unittest.main()
