import asyncio
import base64
import ctypes
import fcntl
import json
import logging
import os
import random
import re
import struct
import subprocess
import termios
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import TimeoutError as FutureTimeout


MUSIC_ROOT = "/data/thinksmart/music"
CREDENTIAL_FILE = f"{MUSIC_ROOT}/qq-credential.json"
DEVICE_FILE = f"{MUSIC_ROOT}/qq-device.json"
SETTINGS_FILE = f"{MUSIC_ROOT}/settings.json"
MUSIC_ROLE = "Music"
# Audio pw-cat has taken from the pipe but not yet played: the PipeWire quantum
# plus the speaker's 4096-frame ALSA headroom at 48 kHz, with some margin.
RESUME_REWIND = 0.15
SINK = "@DEFAULT_AUDIO_SINK@"
QQ_STREAM = "https://isure.stream.qqmusic.qq.com/"
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
QQ_HEADERS = {"User-Agent": BROWSER_UA}
NETEASE_HEADERS = {"User-Agent": BROWSER_UA, "Referer": "https://music.163.com/"}
NETEASE_FREE = (0, 8)
CHUNK = 65536
URL_LIFETIME = 600
LYRIC_LIMIT = 15000
FAVORITE_WORDS = ("我喜欢", "我喜欢的", "我的收藏", "收藏", "我收藏的", "红心", "我的红心", "我的歌单")
GENERIC_WORDS = ("", "音乐", "歌", "歌曲", "首歌", "一首歌", "点音乐", "点歌", "些歌", "推荐", "推荐歌曲", "随便", "随机")
MODES = ("shuffle", "order", "repeat_one")
MODE_NAMES = {"shuffle": "随机播放", "order": "顺序播放", "repeat_one": "单曲循环"}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "play_music",
            "description": "搜索并播放歌曲。用户想听歌、点歌、听某位歌手或某种风格的音乐时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，如“周杰伦 晴天”“陈奕迅”“轻音乐”；想听推荐时留空，想听收藏时填“我喜欢”",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "control_music",
            "description": "控制当前的音乐播放。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["pause", "resume", "next", "previous", "stop", *MODES],
                        "description": "暂停、继续、下一首、上一首、停止、切换为随机播放、顺序播放、单曲循环",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_volume",
            "description": "调节设备的播放音量。",
            "parameters": {
                "type": "object",
                "properties": {
                    "level": {"type": "integer", "description": "目标音量，0 到 100"},
                    "change": {"type": "integer", "description": "相对调整量，如 10 或 -10"},
                },
            },
        },
    },
]

_PUNCT = re.compile(r"[\s，。！？、；：,.!?;:~～…“”\"'‘’]+")
_POLITE = re.compile(r"^(?:请你?|麻烦你?|帮我|给我|你|再)+")
_PLAY = re.compile(r"^(?:播放|我想听听?|我要听|想听|来一首|来首|放一首|放首|唱一首|唱首|搜索)(.*)$")
_COMMANDS = (
    ("stop", re.compile(r"^(?:停止|停止播放|停止音乐|关闭音乐|关掉音乐|关闭播放|别放了|不听了|不要放了)$")),
    ("pause", re.compile(r"^(?:暂停|暂停播放|暂停音乐|暂停一下|停一下|先停一下|停)$")),
    ("resume", re.compile(r"^(?:继续|继续播放|继续放|接着放|接着播放|恢复播放|继续音乐)$")),
    ("next", re.compile(r"^(?:下一首|下一曲|下一首歌|换一首|换首歌|换一首歌|切歌|换一个|下首)$")),
    ("previous", re.compile(r"^(?:上一首|上一曲|上一首歌|上首)$")),
    ("louder", re.compile(r"^(?:把)?(?:声音|音量)(?:调|开)?(?:大|高)(?:一)?[点些]?$|^大声(?:一)?点$|^(?:声音|音量)?再?大(?:一)?点$")),
    ("quieter", re.compile(r"^(?:把)?(?:声音|音量)(?:调|开)?(?:小|低)(?:一)?[点些]?$|^小声(?:一)?点$|^(?:声音|音量)?再?小(?:一)?点$")),
    ("shuffle", re.compile(r"^(?:切换到?|切到|改成|换成|改为|设为|开启)?(?:随机播放|乱序播放|随机模式|乱序模式|打乱顺序|随机放歌?|随机听歌?)(?:模式)?$")),
    ("order", re.compile(r"^(?:切换到?|切到|改成|换成|改为|设为|开启)?(?:顺序播放|按顺序播放|顺序模式|列表循环|按顺序放|按顺序听)(?:模式)?$")),
    ("repeat_one", re.compile(r"^(?:切换到?|切到|改成|换成|改为|设为|开启)?(?:单曲循环|单曲重复|(?:循环|重复|一直)(?:播放|放)?这首歌?)(?:模式)?$")),
)
_ANYTIME = ("louder", "quieter", *MODES)


def plain(text):
    text = re.sub(r"[（(\[【].*?[）)\]】]", "", text or "")
    return re.sub(r"[\W_]+", "", text).lower()


def clean_query(text):
    query = re.sub(r"^(?:一首|一下|首|点)", "", text)
    query = re.sub(r"(?:吧|呗|好吗|可以吗|好不好)$", "", query)
    if query in FAVORITE_WORDS or re.sub(r"的?(?:歌曲?|音乐)$", "", query) in FAVORITE_WORDS:
        return "我喜欢"
    return "" if query in GENERIC_WORDS else query


def match_command(text, music_active):
    normalized = _POLITE.sub("", _PUNCT.sub("", text or ""))
    if not normalized or len(normalized) > 30:
        return None
    played = _PLAY.match(normalized)
    if played:
        return "play", clean_query(played.group(1))
    for name, pattern in _COMMANDS:
        if pattern.match(normalized):
            if name in _ANYTIME or music_active:
                return name, None
            return None
    return None


def write_private(path, data):
    temporary = f"{path}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)


def read_volume():
    result = subprocess.run(
        ["/usr/bin/wpctl", "get-volume", SINK],
        capture_output=True, text=True, timeout=3, check=False,
    )
    match = re.search(r"Volume:\s*([0-9.]+)", result.stdout)
    return round(float(match.group(1)) * 100) if match else None


def set_volume(level=None, change=None):
    if level is not None:
        value = f"{max(0, min(100, int(level))) / 100:.2f}"
    else:
        step = int(change or 0)
        if step == 0:
            return read_volume()
        value = f"{min(100, abs(step))}%{'+' if step > 0 else '-'}"
    subprocess.run(["/usr/bin/wpctl", "set-volume", "-l", "1.0", SINK, value],
                   capture_output=True, timeout=3, check=False)
    subprocess.run(["/usr/bin/wpctl", "set-mute", SINK, "0"],
                   capture_output=True, timeout=3, check=False)
    return read_volume()


def track_key(track):
    return f"{track['src']}:{track['id']}"


def unique_titles(tracks):
    seen = set()
    result = []
    for track in tracks:
        key = plain(track["title"])
        if key and key not in seen:
            seen.add(key)
            result.append(track)
    return result


class Mpg123:
    OK, NEED_MORE, NEW_FORMAT, DONE = 0, -10, -11, -12
    _lib = None

    @classmethod
    def library(cls):
        if cls._lib is None:
            lib = ctypes.CDLL("libmpg123.so.0")
            lib.mpg123_new.restype = ctypes.c_void_p
            lib.mpg123_new.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
            lib.mpg123_open_feed.argtypes = [ctypes.c_void_p]
            lib.mpg123_feed.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
            lib.mpg123_read.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
            ]
            lib.mpg123_getformat.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_long),
                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ]
            lib.mpg123_volume.argtypes = [ctypes.c_void_p, ctypes.c_double]
            lib.mpg123_delete.argtypes = [ctypes.c_void_p]
            lib.mpg123_delete.restype = None
            lib.mpg123_init()
            cls._lib = lib
        return cls._lib

    def __init__(self):
        self.lib = self.library()
        error = ctypes.c_int()
        self.handle = self.lib.mpg123_new(None, ctypes.byref(error))
        if not self.handle or self.lib.mpg123_open_feed(self.handle) != 0:
            self.close()
            raise RuntimeError(f"mpg123 init failed ({error.value})")
        self.buffer = ctypes.create_string_buffer(16384)
        self.done = ctypes.c_size_t()

    def feed(self, data):
        self.lib.mpg123_feed(self.handle, data, len(data))

    def read(self):
        code = self.lib.mpg123_read(self.handle, self.buffer, len(self.buffer), ctypes.byref(self.done))
        return code, ctypes.string_at(self.buffer, self.done.value)

    def format(self):
        rate, channels, encoding = ctypes.c_long(), ctypes.c_int(), ctypes.c_int()
        self.lib.mpg123_getformat(self.handle, ctypes.byref(rate), ctypes.byref(channels), ctypes.byref(encoding))
        return rate.value or 44100, channels.value or 2

    def volume(self, value):
        self.lib.mpg123_volume(self.handle, value)

    def close(self):
        if self.handle:
            self.lib.mpg123_delete(self.handle)
            self.handle = None


class Download:
    """Keeps the whole file in memory so pausing never stalls on the network."""

    def __init__(self, url, headers):
        self.chunks = []
        self.finished = False
        self.error = None
        self.cancelled = False
        self.cond = threading.Condition()
        threading.Thread(target=self.run, args=(url, headers), name="music-download", daemon=True).start()

    def run(self, url, headers):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=15) as response:
                while not self.cancelled:
                    data = response.read(CHUNK)
                    if not data:
                        break
                    with self.cond:
                        self.chunks.append(data)
                        self.cond.notify_all()
        except Exception as error:
            self.error = error
        finally:
            with self.cond:
                self.finished = True
                self.cond.notify_all()

    def get(self, index, alive):
        with self.cond:
            while index >= len(self.chunks) and not self.finished:
                if not alive():
                    return None
                self.cond.wait(0.5)
            return self.chunks[index] if index < len(self.chunks) else None


class Player:
    def __init__(self, resolve, extend, notify, duck_level, mode):
        self.resolve = resolve
        self.extend = extend
        self.notify = notify
        self.duck_level = duck_level
        self.ducked = False
        self.lock = threading.Condition()
        self.mode = mode
        self.queue = []
        self.index = 0
        self.added = 0
        self.generation = 0
        self.state = "stopped"
        self.paused = False
        self.output = None
        self.history = bytearray()
        self.position = 0.0
        threading.Thread(target=self.run, name="music-player", daemon=True).start()

    def current(self):
        with self.lock:
            if 0 <= self.index < len(self.queue):
                return self.queue[self.index]
            return None

    def active(self):
        return self.state != "stopped"

    def snapshot(self):
        with self.lock:
            track = self.queue[self.index] if 0 <= self.index < len(self.queue) else None
            state = self.state
            position = self.position
            mode = self.mode
        if track is None or state == "stopped":
            return {"s": "stopped", "m": mode}
        return {
            "s": state,
            "m": mode,
            "k": track_key(track),
            "t": track["title"][:60],
            "a": track["artist"][:60],
            "src": track.get("via") or track["src"],
            "pos": round(position, 1),
            "dur": track.get("dur") or 0,
        }

    def changed(self):
        try:
            self.notify(self.snapshot())
        except Exception:
            logging.exception("音乐状态通知失败")

    def interrupt_locked(self, state):
        self.generation += 1
        self.state = state
        self.position = 0.0
        if self.output is not None and self.output.poll() is None:
            self.output.terminate()
        self.lock.notify_all()

    def number_locked(self, tracks):
        for track in tracks:
            track["seq"] = self.added
            self.added += 1

    def arrange_locked(self, shuffle):
        """Reorders only the tracks after the current one, so history and previous() stay intact."""
        rest = self.queue[self.index + 1:]
        if shuffle:
            random.shuffle(rest)
        else:
            pivot = self.queue[self.index]["seq"] if self.index < len(self.queue) else -1
            rest.sort(key=lambda track: (track["seq"] <= pivot, track["seq"]))
        self.queue[self.index + 1:] = rest

    def play(self, tracks, first=None):
        """tracks is in source order; first (default tracks[0]) plays now, the rest follow the mode."""
        with self.lock:
            tracks = list(tracks)
            first = tracks[0] if first is None else first
            self.number_locked(tracks)
            self.queue = [first] + [track for track in tracks if track is not first]
            self.index = 0
            self.arrange_locked(self.mode == "shuffle")
            self.paused = False
            self.interrupt_locked("loading")
        self.changed()

    def set_mode(self, mode):
        with self.lock:
            self.mode = mode
            if mode != "repeat_one":
                self.arrange_locked(mode == "shuffle")
        self.changed()

    def pause(self):
        with self.lock:
            if self.state not in ("loading", "playing"):
                return False
            self.paused = True
            self.state = "paused"
            if self.output is not None and self.output.poll() is None:
                self.output.terminate()
            self.lock.notify_all()
        self.changed()
        return True

    def resume(self):
        with self.lock:
            if self.state == "paused":
                self.paused = False
                self.state = "playing"
            elif self.state == "stopped" and 0 <= self.index < len(self.queue):
                self.paused = False
                self.interrupt_locked("loading")
            else:
                return False
            self.lock.notify_all()
        self.changed()
        return True

    def skip(self, delta):
        with self.lock:
            if not self.queue:
                return False
            self.index = max(0, min(len(self.queue), self.index + delta))
            self.paused = False
            self.interrupt_locked("loading")
        self.changed()
        return True

    def stop(self):
        with self.lock:
            if self.state == "stopped":
                return False
            self.paused = False
            self.interrupt_locked("stopped")
        self.changed()
        return True

    def run(self):
        failures = 0
        while True:
            with self.lock:
                while self.state == "stopped":
                    self.lock.wait()
                generation = self.generation
                track = self.queue[self.index] if self.index < len(self.queue) else None
                previous = self.queue[-1] if self.queue else None
            if track is None:
                try:
                    more = self.extend(previous)
                except Exception as error:
                    logging.warning("无法获取更多歌曲：%s", error)
                    more = []
                with self.lock:
                    if generation != self.generation:
                        continue
                    if more:
                        self.number_locked(more)
                        if self.mode == "shuffle":
                            random.shuffle(more)
                        self.queue.extend(more)
                        continue
                    self.state = "stopped"
                self.changed()
                continue
            try:
                result = self.play_track(track, generation)
            except Exception:
                logging.exception("歌曲播放失败")
                result = "failed"
            with self.lock:
                if generation != self.generation:
                    continue
                failures = failures + 1 if result == "failed" else 0
                if failures >= 5:
                    failures = 0
                    self.state = "stopped"
                else:
                    if not (result == "ended" and self.mode == "repeat_one"):
                        self.index += 1
                    self.generation += 1
                    self.state = "loading"
                    self.position = 0.0
            self.changed()

    def set_playing(self, generation):
        with self.lock:
            if generation != self.generation or self.state != "loading":
                return
            self.state = "playing"
        self.changed()

    def play_track(self, track, generation):
        def alive():
            return self.generation == generation

        url, headers = self.resolve(track)
        if not alive():
            return "interrupted"
        if not url:
            logging.info("歌曲无法播放：%s - %s", track["artist"], track["title"])
            return "failed"
        logging.info("开始播放：%s - %s（%s）", track["artist"], track["title"], track.get("via") or track["src"])
        download = Download(url, headers)
        decoder = Mpg123()
        fed = 0
        rate, channels = 44100, 2
        frames = 0
        gain = None
        broken = 0
        written = False
        pending = b""
        try:
            while alive():
                if self.paused:
                    replay = self.pause_output(rate, channels)
                    frames -= len(replay) // (2 * channels)
                    pending = replay + pending
                    with self.lock:
                        while self.paused and alive():
                            self.lock.wait()
                    continue
                wanted = self.duck_level if self.ducked else 1.0
                if wanted != gain:
                    decoder.volume(wanted)
                    gain = wanted
                if pending:
                    code, pcm, pending = Mpg123.OK, pending, b""
                else:
                    code, pcm = decoder.read()
                if code == Mpg123.NEW_FORMAT:
                    rate, channels = decoder.format()
                    self.close_output()
                    continue
                if pcm:
                    if not self.write(pcm, rate, channels, generation):
                        pending = pcm
                        if self.paused:
                            continue
                        broken += 1
                        if broken >= 3 and not written:
                            return "failed"
                        time.sleep(0.2)
                        continue
                    if not written:
                        written = True
                        self.set_playing(generation)
                    broken = 0
                    frames += len(pcm) // (2 * channels)
                    self.position = frames / rate
                if code == Mpg123.NEED_MORE:
                    chunk = download.get(fed, alive)
                    if chunk is None:
                        if not alive():
                            break
                        if download.error is not None:
                            logging.warning("歌曲下载失败：%s", download.error)
                            if not written:
                                return "failed"
                        self.finish_output()
                        return "ended"
                    fed += 1
                    decoder.feed(chunk)
                elif code == Mpg123.DONE:
                    self.finish_output()
                    return "ended"
                elif code < 0 and code != Mpg123.OK:
                    logging.warning("解码失败（%d）", code)
                    if not written:
                        return "failed"
                    self.finish_output()
                    return "ended"
            return "interrupted"
        finally:
            download.cancelled = True
            decoder.close()
            if not alive():
                self.close_output()

    def write(self, pcm, rate, channels, generation):
        with self.lock:
            if self.generation != generation or self.paused:
                return False
            output = self.output
            if output is None or output.poll() is not None:
                output = self.output = subprocess.Popen(
                    [
                        "/usr/bin/pw-cat", "--playback", "--media-role", MUSIC_ROLE,
                        "--rate", str(rate), "--channels", str(channels),
                        "--format", "s16", "--latency", "200ms", "-",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
                self.history.clear()
        try:
            output.stdin.write(pcm)
        except (OSError, ValueError):
            # A pause kills pw-cat mid-write; pause_output still needs its pipe.
            if not self.paused:
                self.close_output()
            return False
        self.history += pcm
        del self.history[:-rate * channels * 2 * 2]
        return True

    def pause_output(self, rate, channels):
        """Stops pw-cat and returns the written PCM it has not played yet."""
        with self.lock:
            output = self.output
        if output is None:
            return b""
        try:
            unread = struct.unpack("i", fcntl.ioctl(output.stdin.fileno(), termios.FIONREAD, b"\0" * 4))[0]
        except (OSError, ValueError):
            unread = 0
        self.close_output()
        frame = 2 * channels
        keep = min(len(self.history), unread + int(RESUME_REWIND * rate) * frame) // frame * frame
        replay = bytes(self.history[len(self.history) - keep:])
        self.history.clear()
        return replay

    def close_output(self):
        with self.lock:
            output, self.output = self.output, None
        if output is None:
            return
        if output.poll() is None:
            output.terminate()
        try:
            output.wait(timeout=2)
        except subprocess.TimeoutExpired:
            output.kill()
            output.wait()
        if output.stdin:
            try:
                output.stdin.close()
            except OSError:
                pass

    def finish_output(self):
        with self.lock:
            output, self.output = self.output, None
        if output is None:
            return
        try:
            output.stdin.close()
            output.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            output.kill()
            output.wait()

    def shutdown(self):
        self.stop()
        self.close_output()


class Source:
    """A music platform. Tracks are dicts with src (= name), id, title, artist and dur in seconds;
    a source may add private fields. stream() must return an MP3 URL, which is all Mpg123 decodes."""

    name = ""
    label = ""

    def available(self):
        """Whether stream() can currently succeed (e.g. the account is logged in)."""
        return True

    def search(self, keyword, limit=20):
        return []

    def recommend(self):
        return []

    def favorites(self):
        return []

    def stream(self, track):
        """Returns (url, headers), or (None, None) when the track cannot be played."""
        return None, None

    def lyric(self, track):
        return ""

    def find(self, track):
        """Returns this source's copy of a track from another source, or None."""
        artist = track["artist"].split("/")[0]
        wanted_title, wanted_artist = plain(track["title"]), plain(artist)
        for candidate in self.search(f"{track['title']} {artist}", 10):
            if plain(candidate["title"]) == wanted_title and wanted_artist in plain(candidate["artist"]):
                return candidate
        return None


class Netease(Source):
    name = "163"
    label = "网易云音乐"

    def search(self, keyword, limit=20):
        data = urllib.parse.urlencode({"s": keyword, "type": 1, "limit": limit, "offset": 0}).encode()
        request = urllib.request.Request(
            "https://music.163.com/api/search/get/web", data=data, headers=NETEASE_HEADERS,
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read(1 << 20).decode("utf-8"))
        tracks = []
        for song in (payload.get("result") or {}).get("songs") or []:
            if song.get("fee") not in NETEASE_FREE:
                continue
            try:
                tracks.append({
                    "src": self.name,
                    "id": int(song["id"]),
                    "title": str(song["name"]),
                    "artist": "/".join(a.get("name") or "" for a in song.get("artists") or []),
                    "dur": round((song.get("duration") or 0) / 1000),
                })
            except (KeyError, TypeError, ValueError):
                continue
        return tracks

    def stream(self, track):
        return f"https://music.163.com/song/media/outer/url?id={track['id']}.mp3", NETEASE_HEADERS


class QQMusic(Source):
    name = "qq"
    label = "QQ音乐"

    def __init__(self, on_login):
        self.on_login = on_login
        self.loop = asyncio.new_event_loop()
        self.client = None
        self.ready = threading.Event()
        self.login_task = None
        self.vip = False
        threading.Thread(target=self.run, name="qqmusic", daemon=True).start()

    def run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self.start())
        self.loop.run_forever()

    async def start(self):
        try:
            from qqmusic_api import Client, Credential
            credential = Credential()
            try:
                with open(CREDENTIAL_FILE, "r", encoding="utf-8") as source:
                    credential = Credential.model_validate(json.load(source))
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as error:
                logging.warning("QQ音乐凭证读取失败：%s", error)
            self.client = Client(credential=credential, device_path=DEVICE_FILE)
        except Exception as error:
            logging.warning("QQ音乐不可用：%s", error)
            return
        finally:
            self.ready.set()
        while True:
            try:
                await self.keep_alive()
            except Exception as error:
                logging.warning("QQ音乐登录状态检查失败：%s", error)
            await asyncio.sleep(6 * 3600)

    def logged_in(self):
        return self.client is not None and self.client.credential.is_valid()

    def available(self):
        return self.logged_in()

    def call(self, factory, timeout=15):
        if not self.ready.wait(timeout) or self.client is None:
            raise RuntimeError("QQ音乐服务不可用")

        async def runner():
            return await factory(self.client)

        future = asyncio.run_coroutine_threadsafe(runner(), self.loop)
        try:
            return future.result(timeout)
        except FutureTimeout:
            future.cancel()
            raise

    @staticmethod
    def track(song):
        return {
            "src": QQMusic.name,
            "id": song.id,
            "mid": song.mid,
            "media": song.file.media_mid,
            "title": re.sub(r"</?em>", "", song.name or song.title),
            "artist": "/".join(singer.name for singer in song.singer if singer.name),
            "dur": song.interval,
        }

    def search(self, keyword, limit=20):
        result = self.call(lambda c: c.search.search_by_type(keyword=keyword, num=limit))
        return [self.track(song) for song in result.song or [] if song.mid]

    def recommend(self):
        result = self.call(lambda c: c.recommend.get_guess_recommend())
        return [self.track(song) for song in result.songs if song.mid]

    def favorites(self):
        euin = self.client.credential.encrypt_uin if self.client else ""
        if not euin:
            return []
        result = self.call(lambda c: c.user.get_fav_song(euin, num=100))
        return [self.track(song) for song in result.songs if song.mid]

    def lyric(self, track):
        result = self.call(lambda c: c.lyric.get_lyric(track["mid"]))
        return result.lyric or ""

    def stream(self, track):
        from qqmusic_api.modules.song import SongFileInfo, SongFileType

        if not self.logged_in():
            return None, None

        info = [SongFileInfo(mid=track["mid"], media_mid=track.get("media") or None)]
        for file_type in (SongFileType.MP3_320, SongFileType.MP3_128):
            result = self.call(lambda c, t=file_type: c.song.get_song_urls(info, file_type=t))
            for item in result.data:
                if item.result == 0 and item.purl:
                    return QQ_STREAM + item.purl, QQ_HEADERS
        return None, None

    def save_credential(self, credential):
        try:
            write_private(
                CREDENTIAL_FILE,
                json.dumps(credential.model_dump(by_alias=True), ensure_ascii=False).encode("utf-8"),
            )
        except OSError as error:
            logging.warning("QQ音乐凭证保存失败：%s", error)

    async def keep_alive(self):
        credential = self.client.credential
        if not credential.is_valid():
            self.on_login({"s": "none"})
            return
        lifetime = credential.key_expires_in
        stale = not lifetime or time.time() > credential.musickey_create_time + lifetime / 2
        if stale:
            try:
                refreshed = await self.client.login.refresh_credential()
                self.save_credential(refreshed)
                logging.info("QQ音乐凭证已刷新")
            except Exception as error:
                logging.warning("QQ音乐凭证刷新失败：%s", error)
                if await self.client.login.check_expired():
                    self.on_login({"s": "expired"})
                    return
        await self.publish_account()

    async def publish_account(self):
        try:
            info = await self.client.user.get_vip_info()
            identity = info.identity
            self.vip = bool(identity.vip or identity.huge_vip or info.svip)
        except Exception as error:
            logging.warning("QQ音乐会员信息获取失败：%s", error)
        self.on_login({"s": "ok", "vip": int(self.vip)})

    def start_login(self, kind):
        def schedule():
            if self.login_task is not None:
                self.login_task.cancel()
            self.login_task = self.loop.create_task(self.login_flow(kind))

        self.loop.call_soon_threadsafe(schedule)

    def cancel_login(self):
        def cancel():
            if self.login_task is not None:
                self.login_task.cancel()
                self.login_task = None

        self.loop.call_soon_threadsafe(cancel)

    def logout(self):
        async def run():
            from qqmusic_api import Credential
            if self.login_task is not None:
                self.login_task.cancel()
            try:
                await self.client.login.logout()
            except Exception:
                pass
            self.client.credential = Credential()
            try:
                os.unlink(CREDENTIAL_FILE)
            except FileNotFoundError:
                pass
            self.vip = False
            self.on_login({"s": "none"})

        if self.ready.wait(5) and self.client is not None:
            asyncio.run_coroutine_threadsafe(run(), self.loop)

    async def login_flow(self, kind):
        from qqmusic_api.models.login import QRLoginType

        login_type = {"qq": QRLoginType.QQ, "wx": QRLoginType.WX}.get(kind, QRLoginType.MOBILE)
        self.on_login({"s": "wait", "t": kind})
        try:
            if self.client is None:
                raise RuntimeError("QQ音乐服务不可用")
            qr = await self.client.login.get_qrcode(login_type)
            self.on_login({"s": "qr", "t": kind, "img": base64.b64encode(qr.data).decode("ascii")})
            if login_type == QRLoginType.MOBILE:
                import anyio

                async for result in self.client.login.checking_mobile_qrcode(
                    qr, deadline=anyio.current_time() + 180,
                ):
                    if await self.login_event(result, kind):
                        return
            else:
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    await asyncio.sleep(2)
                    if await self.login_event(await self.client.login.check_qrcode(qr), kind):
                        return
            self.on_login({"s": "expired", "t": kind})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logging.warning("QQ音乐登录失败：%s", error)
            self.on_login({"s": "error", "t": kind, "msg": str(error)[:60]})

    async def login_event(self, result, kind):
        event = result.event.name
        if event == "CONF":
            self.on_login({"s": "scanned", "t": kind})
        elif event == "TIMEOUT":
            self.on_login({"s": "expired", "t": kind})
            return True
        elif event == "REFUSE":
            self.on_login({"s": "refused", "t": kind})
            return True
        elif event == "DONE" and result.credential is not None:
            self.client.credential = result.credential
            self.save_credential(result.credential)
            logging.info("QQ音乐登录成功")
            await self.publish_account()
            return True
        return False


class MusicService:
    def __init__(self, emit, duck_level):
        self.emit = emit
        self.account = None
        self.flow = None
        self.lyric_key = None
        self.lyric = None
        self.held = False
        self.qq = QQMusic(self.on_login)
        self.sources = [self.qq, Netease()]
        self.player = Player(self.resolve, self.extend, self.on_player, duck_level, self.load_mode())
        self.tools = TOOLS

    @staticmethod
    def load_mode():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as source:
                mode = json.load(source).get("mode")
        except FileNotFoundError:
            mode = None
        except (OSError, ValueError, AttributeError) as error:
            logging.warning("音乐设置读取失败：%s", error)
            mode = None
        return mode if mode in MODES else MODES[0]

    def set_mode(self, mode):
        self.player.set_mode(mode)
        try:
            write_private(SETTINGS_FILE, json.dumps({"mode": mode}).encode("utf-8"))
        except OSError as error:
            logging.warning("音乐设置保存失败：%s", error)

    def source_of(self, track):
        return next((source for source in self.sources if source.name == track["src"]), None)

    def on_player(self, snapshot):
        self.emit("music", **snapshot)
        key = snapshot.get("k")
        track = self.player.current()
        if key and track is not None and key != self.lyric_key:
            self.lyric_key = key
            self.lyric = None
            threading.Thread(target=self.fetch_lyric, args=(track, key), name="music-lyric", daemon=True).start()

    def fetch_lyric(self, track, key):
        text = track.get("lrc")
        if text is None:
            text = self.find_lyric(track)
            track["lrc"] = text
        if key == self.lyric_key:
            self.lyric = {"k": key, "lrc": text[:LYRIC_LIMIT]}
            self.emit("lyric", **self.lyric)

    def find_lyric(self, track):
        own = self.source_of(track)
        for source in sorted(self.sources, key=lambda s: s is not own):
            try:
                match = track if source is own else source.find(track)
                text = source.lyric(match) if match else ""
            except Exception as error:
                logging.warning("%s歌词获取失败：%s", source.label, error)
                continue
            if text:
                return text
        return ""

    def on_login(self, state):
        if state["s"] in ("ok", "none", "expired"):
            self.account = state
            self.flow = None
        else:
            self.flow = state
        self.emit("mlogin", **state)

    def announce(self):
        self.on_player(self.player.snapshot())
        if self.lyric:
            self.emit("lyric", **self.lyric)
        if self.account:
            self.emit("mlogin", **self.account)
        if self.flow:
            self.emit("mlogin", **self.flow)

    def active(self):
        return self.player.active()

    def duck(self, on):
        self.player.ducked = on

    def hold(self):
        """Pauses playback for a conversation; release() resumes it unless the user changed playback."""
        if not self.held and self.player.pause():
            self.held = True

    def release(self):
        held, self.held = self.held, False
        if held and self.player.state == "paused":
            self.player.resume()

    def describe(self):
        track = self.player.current()
        state = self.player.state
        if track is None or state == "stopped":
            return ""
        name = f"{track['artist']}的《{track['title']}》"
        mode = f"播放模式为{MODE_NAMES[self.player.mode]}。"
        if self.held:
            return f"当前正在播放{name}，对话期间已自动暂停，对话结束后会自动继续。{mode}"
        return (f"当前音乐已暂停：{name}。" if state == "paused" else f"当前正在播放{name}。") + mode

    def resolve(self, track):
        """Falls back to other sources when the track's own source cannot stream it."""
        if track.get("url") and time.time() < track.get("url_until", 0):
            return track["url"], track["headers"]
        own = self.source_of(track)
        for source in sorted(self.sources, key=lambda s: s is not own):
            if not source.available():
                continue
            try:
                match = track if source is own else source.find(track)
                url, headers = source.stream(match) if match else (None, None)
            except Exception as error:
                logging.warning("%s播放地址获取失败：%s", source.label, error)
                continue
            if url:
                if source is not own:
                    track["via"] = source.name
                track.update(url=url, headers=headers, url_until=time.time() + URL_LIFETIME)
                return url, headers
        return None, None

    def first_found(self, method, *args):
        """Returns the first non-empty result in source priority order; raises only if every source failed."""
        failures = 0
        for source in self.sources:
            try:
                tracks = getattr(source, method)(*args)
            except Exception as error:
                logging.warning("%s %s 失败：%s", source.label, method, error)
                failures += 1
                continue
            if tracks:
                return tracks
        if failures == len(self.sources):
            raise RuntimeError(f"所有音源 {method} 均失败")
        return []

    def search(self, query):
        return unique_titles(self.first_found("search", query))

    def recommendations(self):
        try:
            return self.first_found("recommend")
        except RuntimeError:
            return []

    def extend(self, last):
        played = {track_key(track) for track in self.player.queue}
        return [track for track in self.recommendations() if track_key(track) not in played]
    def play_query(self, query):
        query = (query or "").strip()
        if query == "我喜欢":
            try:
                tracks = self.first_found("favorites")
            except RuntimeError:
                tracks = []
            if not tracks:
                return ("需要先在设置里登录QQ音乐，才能播放你的收藏" if not self.qq.logged_in()
                        else "没有获取到你收藏的歌曲"), False
        elif not query:
            tracks = self.recommendations()
        else:
            try:
                tracks = self.search(query)
                base = re.sub(r"的(?:歌曲?|音乐)$", "", query)
                if base and base != query and not (tracks and plain(tracks[0]["title"]) == plain(query)):
                    tracks = self.search(base)
            except Exception as error:
                logging.warning("歌曲搜索失败：%s", error)
                return "音乐服务暂时连接不上，请稍后再试", False
        if not tracks:
            return "没有找到相关的歌曲", False
        self.held = False
        candidates = list(range(len(tracks)))
        if query in ("", "我喜欢") and self.player.mode == "shuffle":
            random.shuffle(candidates)
        unplayable = set()
        for index in candidates[:5]:
            if self.resolve(tracks[index])[0]:
                break
            unplayable.add(index)
        else:
            if not self.qq.logged_in():
                return "这首歌需要会员权限，请先在设置里登录QQ音乐", False
            return "这首歌暂时没有播放权限", False
        track = tracks[index]
        self.player.play([t for i, t in enumerate(tracks) if i not in unplayable], track)
        artist = track["artist"].split("/")[0]
        return (f"好的，播放{artist}的{track['title']}" if artist else f"好的，播放{track['title']}"), True

    def change_mode(self, mode):
        """Returns (reply, playback_started); also starts playback when idle unless repeating one."""
        self.set_mode(mode)
        reply = f"已切换为{MODE_NAMES[mode]}"
        if self.player.active() or mode == "repeat_one":
            return reply, self.player.active()
        started_reply, started = self.play_query("")
        if not started:
            return reply, False
        return f"{reply}，{started_reply.removeprefix('好的，')}", True

    def control(self, action):
        if action in MODES:
            return self.change_mode(action)
        player = self.player
        held, self.held = self.held, False
        if action == "pause":
            return ("已暂停" if held or player.pause() else "现在没有在播放音乐"), False
        if action == "resume":
            if player.resume():
                return "继续播放", True
            return self.play_query("")
        if action == "next":
            return ("好的，下一首" if player.skip(1) else "现在没有在播放音乐"), player.active()
        if action == "previous":
            return ("好的，上一首" if player.skip(-1) else "现在没有在播放音乐"), player.active()
        if action == "stop":
            return ("已停止播放" if player.stop() else "现在没有在播放音乐"), False
        return "不支持这个操作", False

    @staticmethod
    def volume(level=None, change=None):
        try:
            value = set_volume(level, change)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            logging.warning("音量调节失败：%s", error)
            return "音量调节失败"
        return f"音量已调到百分之{value}" if value is not None else "音量已调整"

    def run_command(self, name, argument):
        """Returns (reply, ok, playback_started)."""
        if name == "play":
            reply, started = self.play_query(argument)
            return reply, started, started
        if name in ("louder", "quieter"):
            reply = self.volume(change=10 if name == "louder" else -10)
            return reply, reply != "音量调节失败", False
        reply, started = self.control(name)
        return reply, not reply.startswith(("现在没有", "不支持")), started

    def call_tool(self, name, arguments):
        """Returns (reply, ok, playback_started)."""
        try:
            args = json.loads(arguments or "{}")
        except ValueError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        if name == "play_music":
            reply, started = self.play_query(clean_query(_PUNCT.sub("", str(args.get("query") or ""))))
            return reply, started, started
        if name == "control_music":
            action = str(args.get("action") or "")
            reply, started = self.control(action)
            return reply, not reply.startswith(("现在没有", "不支持")), started
        if name == "set_volume":
            try:
                level = int(args["level"]) if args.get("level") is not None else None
                change = int(args["change"]) if args.get("change") is not None else None
            except (TypeError, ValueError):
                return "没有听懂要调到多大音量", False, False
            reply = self.volume(level, change)
            return reply, reply != "音量调节失败", False
        return "不支持这个操作", False, False

    def ui_command(self, action):
        if action == "mode":
            self.set_mode(MODES[(MODES.index(self.player.mode) + 1) % len(MODES)])
            return
        self.held = False
        if action == "toggle":
            if self.player.state in ("playing", "loading"):
                self.player.pause()
                return
            if not self.player.resume():
                threading.Thread(target=self.play_query, args=("",), name="music-start", daemon=True).start()
        elif action == "next":
            self.player.skip(1)
        elif action == "prev":
            self.player.skip(-1)
        elif action == "stop":
            self.player.stop()

    def login_command(self, kind):
        if kind in ("mobile", "qq", "wx"):
            self.qq.start_login(kind)
        elif kind == "cancel":
            self.qq.cancel_login()
            self.flow = None
        elif kind == "logout":
            self.qq.logout()

    def shutdown(self):
        self.player.shutdown()
