import glob
import fcntl
import json
import logging
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request

DATA_DIR = "/data/thinksmart/ui"
SETTINGS_FILE = f"{DATA_DIR}/settings.json"
WEATHER_FILE = f"{DATA_DIR}/weather.json"
RUNTIME_DIR = "/run/thinksmart-ui"
EVENT_SOCKET = f"{RUNTIME_DIR}/events.sock"
WAKE_REQUEST = f"{RUNTIME_DIR}/wake-request"
CONTROL_SOCKET = "/run/thinksmart-voice/control.sock"
WAKE_PHRASE_FILE = "/etc/thinksmart-voice/wake-phrase"
CREDENTIAL_FILE = "/etc/thinksmart-voice/aliyun.json"
WAKE_PATTERN = re.compile(r"[\u4e00-\u9fff]{2,8}")
DIM_CHOICES = (0, 60, 300, 900, 1800)
SINK = "@DEFAULT_AUDIO_SINK@"
HEADERS = {"User-Agent": "ThinkSmart-Display/1.0", "Accept": "application/json"}

WEATHER_CODES = {
    0: ("晴", "sun"), 1: ("晴间少云", "sun"), 2: ("多云", "partly"), 3: ("阴", "cloud"),
    45: ("雾", "fog"), 48: ("雾凇", "fog"),
    51: ("小毛毛雨", "rain"), 53: ("毛毛雨", "rain"), 55: ("强毛毛雨", "rain"),
    56: ("冻毛毛雨", "rain"), 57: ("强冻毛毛雨", "rain"),
    61: ("小雨", "rain"), 63: ("中雨", "rain"), 65: ("大雨", "rain"),
    66: ("冻雨", "rain"), 67: ("强冻雨", "rain"),
    71: ("小雪", "snow"), 73: ("中雪", "snow"), 75: ("大雪", "snow"), 77: ("米雪", "snow"),
    80: ("阵雨", "rain"), 81: ("中阵雨", "rain"), 82: ("强阵雨", "rain"),
    85: ("阵雪", "snow"), 86: ("强阵雪", "snow"),
    95: ("雷阵雨", "storm"), 96: ("雷阵雨伴冰雹", "storm"), 99: ("强雷暴伴冰雹", "storm"),
}


def env_float(name, default):
    try:
        return float(os.getenv(name, ""))
    except ValueError:
        return default


def write_atomic(path, data):
    temporary = f"{path}.tmp"
    with open(temporary, "wb") as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)


def read_wake_phrase():
    try:
        with open(WAKE_PHRASE_FILE, "r", encoding="utf-8") as source:
            phrase = source.read().strip()
            if phrase:
                return phrase
    except OSError:
        pass
    return "小爱同学"


def request_wake_word(phrase):
    if not WAKE_PATTERN.fullmatch(phrase):
        return False
    try:
        write_atomic(WAKE_REQUEST, phrase.encode("utf-8"))
        return True
    except OSError as error:
        logging.warning("无法提交唤醒词请求：%s", error)
        return False


def credentials_configured():
    return os.path.exists(CREDENTIAL_FILE)


def valid_location(value):
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    lat = value.get("lat")
    lon = value.get("lon")
    if not isinstance(name, str) or not 0 < len(name) <= 40:
        return None
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None
    return {"name": name, "lat": float(lat), "lon": float(lon)}


class Settings:
    def __init__(self):
        dim = int(env_float("THINKSMART_UI_DIM_SECONDS", 300))
        self.data = {
            "brightness": None,
            "dim_after": dim if dim in DIM_CHOICES else 300,
            "mic": True,
            "speech_rate": 1.5,
            "thinking": True,
            "night": {"enabled": False, "start": 22 * 60, "end": 7 * 60},
            "location": {
                "name": os.getenv("THINKSMART_UI_CITY", "上海")[:40] or "上海",
                "lat": env_float("THINKSMART_UI_LATITUDE", 31.2304),
                "lon": env_float("THINKSMART_UI_LONGITUDE", 121.4737),
            },
        }
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as source:
                saved = json.load(source)
        except (OSError, ValueError):
            saved = {}
        if isinstance(saved, dict):
            brightness = saved.get("brightness")
            if isinstance(brightness, int) and 1 <= brightness <= 100:
                self.data["brightness"] = brightness
            if saved.get("dim_after") in DIM_CHOICES:
                self.data["dim_after"] = saved["dim_after"]
            if isinstance(saved.get("mic"), bool):
                self.data["mic"] = saved["mic"]
            rate = saved.get("speech_rate")
            if isinstance(rate, (int, float)) and 0.5 <= rate <= 2.0:
                self.data["speech_rate"] = round(float(rate), 1)
            if isinstance(saved.get("thinking"), bool):
                self.data["thinking"] = saved["thinking"]
            night = saved.get("night")
            if isinstance(night, dict):
                self.data["night"] = {
                    "enabled": bool(night.get("enabled", False)),
                    "start": int(night.get("start", 1320)) % 1440,
                    "end": int(night.get("end", 420)) % 1440,
                }
            location = valid_location(saved.get("location"))
            if location:
                self.data["location"] = location

    def __getitem__(self, key):
        return self.data[key]

    def update(self, strict=False, **values):
        try:
            with open(SETTINGS_FILE + ".lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                current = Settings().data
                current.update(values)
                write_atomic(SETTINGS_FILE, json.dumps(current, ensure_ascii=False).encode("utf-8"))
                self.data = current
            return True
        except OSError as error:
            logging.warning("设置保存失败：%s", error)
            if strict:
                raise
            return False


class Backlight:
    def __init__(self):
        self.path = None
        self.max = 0
        preferred = os.getenv("THINKSMART_UI_BACKLIGHT", "").strip()
        candidates = [preferred] if preferred else sorted(glob.glob("/sys/class/backlight/*"))
        for candidate in candidates:
            try:
                with open(os.path.join(candidate, "max_brightness"), "r") as source:
                    maximum = int(source.read().strip())
            except (OSError, ValueError):
                continue
            if maximum > 0:
                self.path = candidate
                self.max = maximum
                break

    def writable(self):
        return bool(self.path) and os.access(os.path.join(self.path, "brightness"), os.W_OK)

    def read_percent(self):
        if not self.path:
            return None
        try:
            with open(os.path.join(self.path, "brightness"), "r") as source:
                return max(1, min(100, round(int(source.read().strip()) * 100 / self.max)))
        except (OSError, ValueError):
            return None

    def write_percent(self, percent):
        if not self.path:
            return
        raw = 0 if percent <= 0 else max(1, min(self.max, round(self.max * percent / 100)))
        try:
            with open(os.path.join(self.path, "brightness"), "w") as target:
                target.write(str(raw))
        except OSError as error:
            logging.warning("亮度设置失败：%s", error)


def run_wpctl(*args):
    executable = shutil.which("wpctl") or "/usr/bin/wpctl"
    return subprocess.run(
        [executable, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=4,
    )


def read_volume():
    result = run_wpctl("get-volume", SINK)
    match = re.search(r"Volume:\s*([0-9.]+)", result.stdout)
    if result.returncode != 0 or not match:
        return None, False
    return min(100, round(float(match.group(1)) * 100)), "MUTED" in result.stdout


def write_volume(percent):
    run_wpctl("set-volume", "-l", "1.0", SINK, f"{percent / 100:.2f}")
    run_wpctl("set-mute", SINK, "0" if percent > 0 else "1")


def default_interface():
    try:
        with open("/proc/net/route", "r") as source:
            for line in source.readlines()[1:]:
                fields = line.split()
                if len(fields) > 3 and fields[1] == "00000000" and int(fields[3], 16) & 2:
                    return fields[0]
    except (OSError, ValueError):
        pass
    return None


def local_address():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("223.5.5.5", 53))
            return probe.getsockname()[0]
    except OSError:
        return ""


def wifi_signal(interface):
    try:
        with open("/proc/net/wireless", "r") as source:
            for line in source:
                if line.strip().startswith(interface + ":"):
                    quality = float(line.split()[2].rstrip("."))
                    return max(0, min(100, round(quality * 100 / 70)))
    except (OSError, ValueError, IndexError):
        pass
    return None


def wifi_ssid(interface):
    executable = shutil.which("iw")
    if not executable:
        return ""
    try:
        result = subprocess.run(
            [executable, "dev", interface, "link"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r"SSID:\s*(.+)", result.stdout)
    return match.group(1).strip()[:32] if match else ""


def http_json(url, timeout=10):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(262144).decode("utf-8"))


def fetch_weather(location):
    query = urllib.parse.urlencode({
        "latitude": f"{location['lat']:.4f}",
        "longitude": f"{location['lon']:.4f}",
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,is_day",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min",
        "timezone": "auto",
        "forecast_days": 3,
    })
    payload = http_json(f"https://api.open-meteo.com/v1/forecast?{query}")
    current = payload["current"]
    daily = payload.get("daily") or {}
    days = []
    for index, day in enumerate(daily.get("time") or []):
        try:
            days.append({
                "date": str(day),
                "code": int(daily["weather_code"][index]),
                "max": float(daily["temperature_2m_max"][index]),
                "min": float(daily["temperature_2m_min"][index]),
            })
        except (KeyError, IndexError, TypeError, ValueError):
            break
    return {
        "name": location["name"],
        "lat": location["lat"],
        "lon": location["lon"],
        "temp": float(current["temperature_2m"]),
        "feels": float(current.get("apparent_temperature", current["temperature_2m"])),
        "humidity": int(current.get("relative_humidity_2m") or 0),
        "wind": float(current.get("wind_speed_10m") or 0),
        "code": int(current.get("weather_code") or 0),
        "day": bool(current.get("is_day", 1)),
        "days": days[:3],
        "fetched": time.time(),
    }


def search_places(name):
    query = urllib.parse.urlencode({
        "name": name,
        "count": 6,
        "language": "zh",
        "format": "json",
    })
    payload = http_json(f"https://geocoding-api.open-meteo.com/v1/search?{query}")
    places = []
    for item in payload.get("results") or []:
        try:
            places.append({
                "name": str(item["name"])[:40],
                "detail": " · ".join(
                    str(part) for part in (item.get("admin1"), item.get("country")) if part
                )[:60],
                "lat": float(item["latitude"]),
                "lon": float(item["longitude"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return places


class SystemStatus:
    """Background poller; the render thread only reads attributes it swaps."""

    def __init__(self, settings):
        self.settings = settings
        self.version = 0
        self.network = {"kind": "none", "iface": "", "ip": "", "signal": None, "ssid": ""}
        self.volume = None
        self.muted = False
        self.backlight = Backlight()
        self.weather = self.load_weather_cache()
        self.weather_failed = False
        self.places = []
        self.places_state = "idle"
        self.pending_volume = None
        self.pending_brightness = None
        self.jobs = queue.Queue()
        self.interval = max(10, env_float("THINKSMART_UI_WEATHER_MINUTES", 30)) * 60
        self.failures = 0
        self.next_weather = 0.0
        if self.weather and time.time() - self.weather["fetched"] < self.interval:
            self.next_weather = time.monotonic() + self.interval - (time.time() - self.weather["fetched"])
        self.next_network = 0.0
        self.next_volume = 0.0
        self.next_ssid = 0.0
        self.ssid = ""
        threading.Thread(target=self.run, name="status", daemon=True).start()

    def load_weather_cache(self):
        try:
            with open(WEATHER_FILE, "r", encoding="utf-8") as source:
                cached = json.load(source)
        except (OSError, ValueError):
            return None
        location = self.settings["location"]
        try:
            if (
                not isinstance(cached.get("fetched"), (int, float))
                or abs(cached["lat"] - location["lat"]) > 0.01
                or abs(cached["lon"] - location["lon"]) > 0.01
                or not isinstance(cached["temp"], (int, float))
            ):
                return None
        except (AttributeError, KeyError, TypeError):
            return None
        return cached

    def changed(self):
        self.version += 1

    def set_volume(self, percent):
        self.pending_volume = percent
        self.jobs.put(None)

    def set_brightness(self, percent):
        self.pending_brightness = percent
        self.jobs.put(None)

    def refresh_weather(self):
        self.weather = None
        self.weather_failed = False
        self.failures = 0
        self.next_weather = 0.0
        self.changed()
        self.jobs.put(None)

    def find_places(self, name):
        self.places = []
        self.places_state = "searching"
        self.changed()
        self.jobs.put(lambda: self.search(name))

    def search(self, name):
        try:
            places = search_places(name)
            self.places = places
            self.places_state = "done" if places else "empty"
        except Exception as error:
            logging.warning("城市搜索失败：%s", error)
            self.places_state = "error"
        self.changed()

    def run(self):
        while True:
            try:
                job = self.jobs.get(timeout=1.0)
            except queue.Empty:
                job = None
            try:
                if job:
                    job()
                self.apply_pending()
                self.poll()
            except Exception:
                logging.exception("状态更新失败")
                time.sleep(1)

    def apply_pending(self):
        brightness = self.pending_brightness
        if brightness is not None:
            self.pending_brightness = None
            self.backlight.write_percent(brightness)
        volume = self.pending_volume
        if volume is not None:
            self.pending_volume = None
            try:
                write_volume(volume)
                self.volume = volume
                self.muted = volume == 0
            except (OSError, subprocess.SubprocessError) as error:
                logging.warning("音量设置失败：%s", error)
            self.next_volume = time.monotonic() + 3
            self.changed()

    def poll(self):
        now = time.monotonic()
        if now >= self.next_network:
            self.next_network = now + 5
            network = self.read_network(now)
            if network != self.network:
                self.network = network
                self.changed()
        if now >= self.next_volume and self.pending_volume is None:
            self.next_volume = now + 5
            try:
                volume, muted = read_volume()
            except (OSError, subprocess.SubprocessError):
                volume, muted = None, False
            if (volume, muted) != (self.volume, self.muted):
                self.volume, self.muted = volume, muted
                self.changed()
        if now >= self.next_weather:
            self.update_weather(now)

    def read_network(self, now):
        interface = default_interface()
        if not interface:
            return {"kind": "none", "iface": "", "ip": "", "signal": None, "ssid": ""}
        wireless = os.path.isdir(f"/sys/class/net/{interface}/wireless") or interface.startswith("wl")
        if wireless:
            kind = "wifi"
        elif interface.startswith(("usb", "rndis", "ncm")):
            kind = "usb"
        else:
            kind = "ethernet"
        if wireless and now >= self.next_ssid:
            self.next_ssid = now + 30
            self.ssid = wifi_ssid(interface)
        return {
            "kind": kind,
            "iface": interface,
            "ip": local_address(),
            "signal": wifi_signal(interface) if wireless else None,
            "ssid": self.ssid if wireless else "",
        }

    def update_weather(self, now):
        location = self.settings["location"]
        if self.network["kind"] == "none":
            self.next_weather = now + 60
            if not self.weather_failed:
                self.weather_failed = True
                self.changed()
            return
        try:
            weather = fetch_weather(location)
        except Exception as error:
            logging.warning("天气获取失败：%s", error)
            self.failures += 1
            self.next_weather = now + min(1800, 120 * 2 ** min(self.failures, 4))
            self.weather_failed = True
            self.changed()
            return
        self.failures = 0
        self.weather = weather
        self.weather_failed = False
        self.next_weather = now + self.interval
        self.changed()
        try:
            write_atomic(WEATHER_FILE, json.dumps(weather, ensure_ascii=False).encode("utf-8"))
        except OSError as error:
            logging.warning("天气缓存保存失败：%s", error)


class VoiceLink:
    def __init__(self):
        runtime = os.environ.get("RUNTIME_DIRECTORY", RUNTIME_DIR).split(":")[0]
        self.path = os.path.join(runtime, "events.sock")
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self.sock = socket.socket(
            socket.AF_UNIX,
            socket.SOCK_DGRAM | socket.SOCK_NONBLOCK | socket.SOCK_CLOEXEC,
        )
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
        self.sock.bind(self.path)
        self.out = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)
        self.out.setblocking(False)

    def fileno(self):
        return self.sock.fileno()

    def receive(self):
        messages = []
        for _ in range(256):
            try:
                data = self.sock.recv(65536)
            except (BlockingIOError, InterruptedError):
                break
            try:
                message = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(message, dict) and isinstance(message.get("e"), str):
                messages.append(message)
        return messages

    def send(self, command, **fields):
        fields["c"] = command
        try:
            self.out.sendto(json.dumps(fields).encode("utf-8"), CONTROL_SOCKET)
            return True
        except OSError:
            return False

    def close(self):
        self.sock.close()
        self.out.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass
