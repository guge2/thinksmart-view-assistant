import unittest

import schema as S

CURRENT = {"chat_model": "qwen3.8-flash", "chat_provider": "bailian", "chat_services": ["bailian", "openrouter"],
           "expert_model": "", "expert_provider": "openai", "expert_configured": False,
           "tts_model": "qwen-audio-3.1-tts-flash", "tts_voice": "longxiaocheng_v3.1"}


class NumberTests(unittest.TestCase):
    def test_number(self):
        self.assertEqual(S.number(5, 0, 10), 5)
        for bad in (True, "5", float("nan"), float("inf"), -1, 11, None):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                S.number(bad, 0, 10)


class VoiceValuesTests(unittest.TestCase):
    def test_accepts_known_fields_in_range(self):
        values = S.voice_values({"kws_threshold": 0.3, "tts_voice": "longze_v3.1"})
        self.assertEqual(values, {"kws_threshold": 0.3, "tts_voice": "longze_v3.1"})

    def test_rejects(self):
        cases = [None, {}, {"unknown": 1}, {"kws_threshold": 0.95}, {"kws_score": "2"},
                 {"tts_voice": "a b"}, {"tts_voice": "-leading"}, {"chat_model": "x" * 200},
                 {"asr_model": "../../etc/passwd\n"}]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                S.voice_values(values)


class PreferenceTests(unittest.TestCase):
    def test_normalises(self):
        self.assertEqual(S.preferences({"speech_rate": 1.26, "brightness": 40.4, "mic": False, "dim_after": 300}),
                         {"speech_rate": 1.3, "brightness": 40, "mic": False, "dim_after": 300})

    def test_rejects(self):
        cases = [{}, [], {"volume": 3}, {"thinking": 1}, {"mic": "yes"}, {"speech_rate": 3},
                 {"brightness": 0}, {"dim_after": 61}, {"dim_after": True}]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                S.preferences(values)


class ModelTests(unittest.TestCase):
    def test_tts_pair(self):
        self.assertEqual(S.tts_pair("qwen-audio-3.1-tts-plus", "longanhuan_v3.1"),
                         ("qwen-audio-3.1-tts-plus", "longanhuan_v3.1"))
        with self.assertRaises(ValueError):
            S.tts_pair("qwen-audio-3.1-tts-plus", "longxiaocheng_v3.1")
        with self.assertRaises(ValueError):
            S.tts_pair("unknown", "longanhuan_v3.1")

    def test_catalog_keeps_the_current_model(self):
        catalog = S.model_catalog(dict(CURRENT, chat_model="custom-model"))
        self.assertEqual(catalog["chat"][0]["id"], "custom-model")
        self.assertEqual(catalog["chat"][0]["group"], "当前配置")
        services = {choice["provider"] for choice in catalog["chat"]}
        self.assertEqual(services, {"bailian", "openrouter"})
        self.assertEqual({m["id"] for m in catalog["tts"]}, set(S.TTS_MODELS))

    def test_model_values_infers_the_provider(self):
        values = S.model_values({"chat_model": "moonshotai/kimi-k2.6"}, dict(CURRENT))
        self.assertEqual(values["chat_provider"], "openrouter")

    def test_model_values_rejects(self):
        cases = [{"chat_model": "gpt-6.2-sol"},  # OpenAI is not configured for chat
                 {"expert_model": "gpt-6.2-sol"},  # no expert credential
                 {"tts_voice": "longanhuan_v3.1", "tts_model": "unknown"}]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                S.model_values(values, dict(CURRENT))


if __name__ == "__main__":
    unittest.main()
