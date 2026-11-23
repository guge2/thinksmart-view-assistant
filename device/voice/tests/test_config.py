import json
import os
import tempfile
import unittest

import config as C
from errors import apology, classify_error


class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(C.Settings.from_env({}), C.Settings())

    def test_values_and_invalid_values(self):
        with self.assertLogs(level="WARNING"):
            s = C.Settings.from_env({
                "THINKSMART_KWS_SCORE": "1.5", "THINKSMART_KWS_THRESHOLD": "abc",
                "THINKSMART_KWS_THREADS": "0", "THINKSMART_CONTEXT_IDLE_MINUTES": "30",
                "THINKSMART_WEB_SEARCH": "0", "THINKSMART_TTS_RATE": "9", "THINKSMART_MAX_REPLY_TOKENS": "nan",
                "THINKSMART_SPEECH_WAIT_SECONDS": "inf", "THINKSMART_CHAT_MODEL": "",
            })
        self.assertEqual(s.kws_score, 1.5)
        self.assertEqual(s.kws_threshold, 0.25)
        self.assertEqual(s.kws_threads, 1)
        self.assertEqual(s.context_idle_seconds, 1800)
        self.assertFalse(s.web_search)
        self.assertEqual(s.speech_rate, 2.0)
        self.assertEqual(s.max_reply_tokens, 256)
        self.assertEqual(s.speech_wait_seconds, 8.0)
        self.assertEqual(s.chat_model, C.Settings.chat_model)

    def test_clamp_rate(self):
        self.assertEqual([C.clamp_rate(v) for v in ("0.1", 1.26, "x", None, float("nan"))], [0.5, 1.3, 1.5, 1.5, 1.5])


class FileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = {"CREDENTIALS_DIRECTORY": self.dir.name}

    def write(self, name, text):
        path = os.path.join(self.dir.name, name)
        with open(path, "w", encoding="utf-8") as target:
            target.write(text)
        return path

    def test_credentials(self):
        with self.assertRaisesRegex(RuntimeError, "credential directory"):
            C.Credentials.load({})
        self.write("aliyun.json", json.dumps({"api_key": "k"}))
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            C.Credentials.load(self.env)
        self.write("aliyun.json", json.dumps({"api_key": "k", "workspace_id": "ws/../x"}))
        with self.assertRaisesRegex(RuntimeError, "workspace"):
            C.Credentials.load(self.env)
        self.write("aliyun.json", json.dumps({"api_key": "k", "workspace_id": "ws-1", "region": "cn-shanghai"}))
        with self.assertRaisesRegex(RuntimeError, "cn-beijing"):
            C.Credentials.load(self.env)
        self.write("aliyun.json", json.dumps({"api_key": "k", "workspace_id": "ws-1"}))
        credentials = C.Credentials.load(self.env)
        self.assertEqual(credentials.endpoint, "https://ws-1.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")

    def test_optional_chat_config(self):
        self.assertEqual(C.load_chat_config("chat.json", {}), {})
        self.assertEqual(C.load_chat_config("chat.json", self.env), {})
        self.write("chat.json", '{"model": "m"}')
        self.assertEqual(C.load_chat_config("chat.json", self.env), {"model": "m"})

    def test_models_and_overrides(self):
        settings = C.Settings()
        models = C.Models.initial(settings, {"model": "chat-x"}, {"model": "gpt", "api_key": ""})
        self.assertEqual((models.chat_model, models.chat_provider, models.expert_model), ("chat-x", "bailian", None))
        defaults = models.overridable()
        path = self.write("gateway.conf", "# saved\nTHINKSMART_TTS_VOICE=v2\nOTHER=1\n")
        values = C.read_model_overrides(defaults, path)
        self.assertEqual(values["tts_voice"], "v2")
        self.assertTrue(models.update(values))
        self.assertFalse(models.update(values))
        self.assertEqual(C.read_model_overrides(defaults, path + ".missing"), defaults)

    def test_preferences(self):
        settings = C.Settings(speech_rate=1.2, thinking=False)
        missing = C.Preferences.load(settings, os.path.join(self.dir.name, "none.json"))
        self.assertEqual((missing.speech_rate, missing.thinking, missing.mic), (1.2, False, True))
        path = self.write("settings.json", json.dumps({"speech_rate": 3, "thinking": True, "mic": False}))
        prefs = C.Preferences.load(settings, path)
        self.assertEqual((prefs.speech_rate, prefs.thinking, prefs.mic), (2.0, True, False))
        self.write("settings.json", "[]")
        self.assertTrue(C.Preferences.load(settings, path).mic)

    def test_hardware_mic(self):
        path = self.write("mic-state", "0\n")
        self.assertFalse(C.read_hardware_mic(path))
        self.write("mic-state", "1")
        self.assertTrue(C.read_hardware_mic(path))
        self.write("mic-state", "?")
        self.assertIsNone(C.read_hardware_mic(path))
        self.assertIsNone(C.read_hardware_mic(path + ".missing"))


class ErrorTests(unittest.TestCase):
    def test_classify(self):
        def chained(outer, inner):
            try:
                try:
                    raise inner
                except Exception as cause:
                    raise outer from cause
            except Exception as error:
                return error

        cases = [
            (RuntimeError("microphone stream ended"), "mic"),
            (RuntimeError("speaker local playback failed"), "speaker"),
            (RuntimeError("systemd credential directory is unavailable"), "config"),
            (RuntimeError("HTTP 401 Unauthorized"), "auth"),
            (chained(RuntimeError("request failed"), TimeoutError("timed out")), "network"),
            (RuntimeError("language model returned an empty reply"), "llm"),
            (ValueError("x"), "service"),
        ]
        for error, code in cases:
            with self.subTest(error=error):
                self.assertEqual(classify_error(error), code)
        self.assertIn("网络", apology("network"))
        self.assertIn("再说一次", apology("service"))


if __name__ == "__main__":
    unittest.main()
