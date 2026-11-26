"""Gold reference price in CNY per gram, ported from goldShow: XAU/USD × USD/CNY ÷ 31.1034768.

A background thread polls the quote, keeps 48 hours of samples for the 24-hour change and caches
both so the home screen still has a price after a restart or while offline.
"""
import json
import logging
import os
import threading
import time

from system import DATA_DIR, env_float, http_json, write_json

GOLD_FILE = f"{DATA_DIR}/gold.json"
TROY_OUNCE_GRAMS = 31.1034768
PRIMARY = "https://api.gold-api.com/price/XAU"
FALLBACK = "https://www.vang.today/api/prices?type=XAUUSD"
FX = "https://api.frankfurter.dev/v2/rate/USD/CNY"
DAY = 24 * 3600
TOLERANCE = 2 * 3600
RETENTION = 48 * 3600
STALE = 30 * 60


def positive(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 and number != float("inf") else None


def parse_primary(data):
    price = positive((data or {}).get("price"))
    if price is None:
        raise ValueError("Gold API 没有给出价格")
    return price, "Gold API"


def parse_fallback(data):
    # vang.today only gives a buy price, so it is labelled as such rather than passed off as spot.
    if not isinstance(data, dict) or data.get("success") is not True or positive(data.get("buy")) is None:
        raise ValueError("vang.today 没有给出价格")
    return positive(data["buy"]), "vang.today 买入参考"


def parse_fx(data):
    rate = positive((data or {}).get("rate"))
    if rate is None:
        raise ValueError("汇率接口没有给出 USD/CNY")
    return rate


def cny_per_gram(usd_per_oz, usd_cny):
    return round(usd_per_oz * usd_cny / TROY_OUNCE_GRAMS, 2)


def prune(samples, now):
    kept = []
    for sample in samples if isinstance(samples, list) else []:
        try:
            at, price = float(sample[0]), positive(sample[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price is not None and now - RETENTION <= at <= now + 300:
            kept.append([at, price])
    kept.sort()
    return kept


def change_24h(samples, price, now):
    """Percent change against the sample closest to 24 hours ago, or None while history is short."""
    target = now - DAY
    best = min(samples, key=lambda sample: abs(sample[0] - target), default=None)
    if best is None or abs(best[0] - target) > TOLERANCE:
        return None
    return round((price - best[1]) / best[1] * 100, 2)


def load_cache(path=GOLD_FILE):
    try:
        with open(path, encoding="utf-8") as source:
            cached = json.load(source)
        quote = cached["quote"]
        if positive(quote["price"]) is None or not isinstance(quote["at"], (int, float)):
            return None, []
        return quote, prune(cached.get("samples"), time.time())
    except (OSError, ValueError, KeyError, TypeError):
        return None, []


class GoldPrice:
    """Polls in its own thread so slow quote servers never hold up volume or weather updates."""

    def __init__(self, network, path=GOLD_FILE, fetch=http_json):
        self.network = network
        self.path = path
        self.fetch = fetch
        self.interval = max(5, env_float("THINKSMART_UI_GOLD_MINUTES", 10)) * 60
        self.quote, self.samples = load_cache(path)
        self.failed = False
        self.version = 0
        self.failures = 0

    def start(self):
        threading.Thread(target=self.run, name="gold", daemon=True).start()

    def age(self, now=None):
        return None if self.quote is None else max(0, (now or time.time()) - self.quote["at"])

    def run(self):
        if self.quote and self.age() < self.interval:
            time.sleep(self.interval - self.age())
        while True:
            delay = self.interval
            if self.network()["kind"] == "none":
                delay = 15
            elif not self.refresh():
                self.failures += 1
                delay = min(self.interval, 60 * 2 ** min(self.failures, 4))
            time.sleep(delay)

    def quote_once(self):
        usd_cny = parse_fx(self.fetch(FX))
        errors = []
        for url, parse in ((PRIMARY, parse_primary), (FALLBACK, parse_fallback)):
            try:
                usd_per_oz, source = parse(self.fetch(url))
                return usd_per_oz, usd_cny, source
            except Exception as error:
                errors.append(str(error))
        raise RuntimeError("；".join(errors))

    def refresh(self):
        try:
            usd_per_oz, usd_cny, source = self.quote_once()
        except Exception as error:
            logging.warning("金价获取失败：%s", error)
            if not self.failed:
                self.failed = True
                self.version += 1
            return False
        now = time.time()
        price = cny_per_gram(usd_per_oz, usd_cny)
        samples = prune(self.samples + [[now, price]], now)
        self.quote = {
            "price": price, "usd": usd_per_oz, "fx": usd_cny, "source": source, "at": now,
            "change": change_24h(self.samples, price, now),
        }
        self.samples = samples
        self.failed = False
        self.failures = 0
        self.version += 1
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            payload = {"quote": self.quote, "samples": samples}
            write_json(self.path, payload)
        except OSError as error:
            logging.warning("金价缓存保存失败：%s", error)
        return True
