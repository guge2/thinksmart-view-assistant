import json
import os
import tempfile
import unittest

import gold as G

NOW = 1_800_000_000.0


class Network:
    kind = "wifi"

    def __call__(self):
        return {"kind": self.kind}


def fake_fetch(responses):
    def fetch(url):
        value = responses[url]
        if isinstance(value, Exception):
            raise value
        return value
    return fetch


class QuoteTests(unittest.TestCase):
    def test_converts_ounce_dollars_to_yuan_per_gram(self):
        self.assertEqual(G.cny_per_gram(4419.70, 6.7378), 957.42)

    def test_parsers_reject_missing_prices(self):
        for parse, data in ((G.parse_primary, {}), (G.parse_fallback, {"success": False, "buy": 1}),
                            (G.parse_fx, {"rate": "x"}), (G.parse_primary, {"price": -1})):
            with self.assertRaises(ValueError):
                parse(data)

    def test_change_uses_sample_nearest_a_day_ago_within_tolerance(self):
        samples = [[NOW - G.DAY - 3000, 900.0], [NOW - G.DAY + 600, 950.0], [NOW - 600, 990.0]]
        self.assertEqual(G.change_24h(samples, 959.5, NOW), 1.0)
        self.assertIsNone(G.change_24h([[NOW - 3600, 900.0]], 959.5, NOW))

    def test_prune_drops_old_and_malformed_samples(self):
        samples = [[NOW - G.RETENTION - 1, 1.0], [NOW - 10, 2.0], ["x", 3.0], [NOW, -1], [NOW - 20, 4.0]]
        self.assertEqual(G.prune(samples, NOW), [[NOW - 20, 4.0], [NOW - 10, 2.0]])


class GoldPriceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "gold.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_fallback_source_takes_over_and_result_is_cached(self):
        fetch = fake_fetch({G.FX: {"rate": 6.7378}, G.PRIMARY: OSError("down"),
                            G.FALLBACK: {"success": True, "buy": 4419.70}})
        gold = G.GoldPrice(Network(), self.path, fetch)
        self.assertTrue(gold.refresh())
        self.assertEqual((gold.quote["price"], gold.quote["source"]), (957.42, "vang.today 买入参考"))
        with open(self.path, encoding="utf-8") as source:
            self.assertEqual(json.load(source)["quote"]["price"], 957.42)
        quote, samples = G.load_cache(self.path)
        self.assertEqual((quote["price"], len(samples)), (957.42, 1))

    def test_failure_keeps_last_quote(self):
        fetch = fake_fetch({G.FX: {"rate": 6.7}, G.PRIMARY: {"price": 4000}, G.FALLBACK: {}})
        gold = G.GoldPrice(Network(), self.path, fetch)
        gold.refresh()
        version = gold.version
        gold.fetch = fake_fetch({G.FX: OSError("offline")})
        self.assertFalse(gold.refresh())
        self.assertTrue(gold.failed)
        self.assertGreater(gold.version, version)
        self.assertEqual(gold.quote["usd"], 4000)

    def test_corrupt_cache_is_ignored(self):
        with open(self.path, "w", encoding="utf-8") as target:
            target.write('{"quote": {"price": "oops"}}')
        self.assertEqual(G.load_cache(self.path), (None, []))


if __name__ == "__main__":
    unittest.main()
