#!/usr/bin/python3
import base64
import bisect
import io
import logging
import math
import os
import re
import select
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from fbdev import Console, Framebuffer  # noqa: E402
from ime import PinyinInput  # noqa: E402
from system import (  # noqa: E402
    DIM_CHOICES,
    WAKE_PATTERN,
    WEATHER_CODES,
    Settings,
    SystemStatus,
    VoiceLink,
    credentials_configured,
    read_wake_phrase,
    request_wake_word,
    voice_running,
)
from touch import Touchscreen  # noqa: E402

W, H = 800, 1280
BG = (0, 0, 0)
CARD = (24, 24, 24)
CARD_PRESSED = (48, 48, 48)
LINE = (34, 34, 34)
TEXT = (238, 238, 238)
DIM = (150, 150, 150)
FAINT = (92, 92, 92)
ACCENT = TEXT
ACCENT_PRESSED = (170, 170, 170)
GOOD = TEXT
WARN = (200, 200, 200)
BAD = TEXT
WHITE = (255, 255, 255)
ACTIVE = {"wake", "listen", "think", "answer", "speak"}
CLOUD_STATES = {"ready", "busy", "offline", "auth", "error"}
WEEKDAYS = "一二三四五六日"
PHASE_TEXT = {
    "wake": ("我在，请说", TEXT),
    "listen": ("正在聆听…", TEXT),
    "think": ("正在思考…", DIM),
    "answer": ("正在回答", DIM),
    "speak": ("正在播报", DIM),
}
ERRORS = {
    "network": "网络连接失败，请检查 Wi-Fi",
    "auth": "阿里云鉴权失败，请重新配置凭据",
    "config": "语音助手尚未正确配置",
    "mic": "麦克风暂时不可用",
    "llm": "模型没有返回内容，请再试一次",
    "service": "云端服务暂时不可用，请稍后再试",
    "nospeech": "没有听清，请再说一次",
}
DIM_LABELS = {0: "从不", 60: "1 分钟", 300: "5 分钟", 900: "15 分钟", 1800: "30 分钟"}
MUSIC_ACTIVE = {"loading", "playing", "paused"}
MUSIC_MODES = {"shuffle": "随机", "order": "顺序", "repeat_one": "单曲"}
LOGIN_FLOW = {"wait", "qr", "scanned"}
LOGIN_KINDS = (("mobile", "QQ音乐App"), ("qq", "QQ"), ("wx", "微信"))
LOGIN_APPS = {"mobile": "QQ音乐App", "qq": "QQ", "wx": "微信"}
LRC_TAG = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
# Reported position counts audio already queued in the pipe and PipeWire buffer.
LYRIC_DELAY = 0.5
LYRIC_SPAN = 4
UI_FONTS = (
    ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 2),
    ("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
)
DIGIT_FONTS = (("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),) + UI_FONTS
THIN_FONTS = (("/usr/share/fonts/truetype/dejavu/DejaVuSans-ExtraLight.ttf", 0),) + DIGIT_FONTS


def parse_lrc(text):
    lines = []
    for raw in text.splitlines():
        tags = LRC_TAG.findall(raw)
        content = LRC_TAG.sub("", raw).strip()
        if not tags or not content:
            continue
        for minutes, seconds in tags:
            lines.append((int(minutes) * 60 + float(seconds), content))
    lines.sort(key=lambda line: line[0])
    return lines


def mix(a, b, t):
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


def inside(rect, x, y, margin=0):
    return rect[0] - margin <= x < rect[2] + margin and rect[1] - margin <= y < rect[3] + margin


def text_field(message):
    value = message.get("text")
    return value if isinstance(value, str) else ""


class Fonts:
    def __init__(self):
        custom = os.getenv("THINKSMART_UI_FONT", "").strip()
        candidates = ((custom, 0),) + UI_FONTS if custom else UI_FONTS
        self.ui_face = next((face for face in candidates if os.path.exists(face[0])), None)
        self.digit_face = next((face for face in DIGIT_FONTS if os.path.exists(face[0])), None)
        self.thin_face = next((face for face in THIN_FONTS if os.path.exists(face[0])), None)
        if self.ui_face is None:
            logging.warning("未找到中文字体，界面文字可能无法显示")
        self.cache = {}
        self.widths = {}

    def get(self, face, size):
        key = (face, size)
        font = self.cache.get(key)
        if font is None:
            if face:
                font = ImageFont.truetype(face[0], size, index=face[1])
            else:
                try:
                    font = ImageFont.load_default(size)
                except TypeError:
                    font = ImageFont.load_default()
            self.cache[key] = font
            self.widths[id(font)] = {}
        return font

    def ui(self, size):
        return self.get(self.ui_face, size)

    def digits(self, size):
        return self.get(self.digit_face, size)

    def thin(self, size):
        return self.get(self.thin_face, size)

    def char_width(self, font, char):
        table = self.widths[id(font)]
        width = table.get(char)
        if width is None:
            width = table[char] = font.getlength(char)
        return width


class Widget:
    __slots__ = ("name", "rect", "paint", "tap", "drag")

    def __init__(self, name, rect, paint, tap=None, drag=None):
        self.name = name
        self.rect = rect
        self.paint = paint
        self.tap = tap
        self.drag = drag


class Orb:
    SCALE = 2

    def __init__(self):
        self.canvas = None
        self.pen = None

    def render(self, frame, rect, state, t, level):
        side = (rect[2] - rect[0]) * self.SCALE
        if self.canvas is None or self.canvas.size[0] != side:
            self.canvas = Image.new("L", (side, side), 0)
            self.pen = ImageDraw.Draw(self.canvas)
        d = self.pen
        d.rectangle((0, 0, side, side), fill=0)
        c = side / 2
        core = side * 0.25
        limit = c - 4
        stroke = max(4, round(side * 0.008))
        slow = 0.5 + 0.5 * math.sin(t * 1.6)
        fast = 0.5 + 0.5 * math.sin(t * 4.2)

        def ring(r, value, width=stroke):
            r = min(limit, r)
            d.ellipse((c - r, c - r, c + r, c + r), outline=int(value), width=int(width))

        def ripples(count, speed, peak):
            for index in range(count):
                p = (t * speed + index / count) % 1.0
                ring(core * (1.0 + 0.9 * p), peak * (1.0 - p) ** 1.6, stroke * 0.75)

        def bars(count, spacing, energy):
            bar = max(8, round(core * 0.09))
            for index in range(count):
                offset = index - (count - 1) / 2
                wave = 0.55 + 0.45 * math.sin(t * 9 + index * 1.3)
                height = max(bar + 2, core * 0.8 * energy * (1.0 - abs(offset) * 0.2) * wave)
                x = c + offset * spacing
                d.rounded_rectangle((x - bar / 2, c - height / 2, x + bar / 2, c + height / 2),
                                    radius=bar // 2, fill=245)

        if state == "idle":
            ring(core * (1.22 + 0.06 * slow), 26 + 18 * slow)
            ring(core, 150 + 70 * slow)
        elif state == "wake":
            ripples(3, 1.3, 220)
            ring(core, 255, stroke * 1.5)
        elif state == "listen":
            ring(core * (1.2 + 0.45 * level), 40 + 120 * level)
            ring(core * (1.0 + 0.08 * level), 255, stroke * 1.5)
            bars(5, core * 0.22, 0.15 + 0.85 * level)
        elif state == "think":
            ring(core, 60)
            start = (t * 240) % 360
            d.arc((c - core, c - core, c + core, c + core), start, start + 100, fill=255, width=int(stroke * 1.5))
            for index in range(3):
                glow = 0.5 + 0.5 * math.sin(t * 5 - index * 0.9)
                r = core * 0.06
                x = c + (index - 1) * core * 0.3
                d.ellipse((x - r, c - r, x + r, c + r), fill=int(90 + 160 * glow))
        elif state in ("answer", "speak"):
            ripples(2, 0.6, 120)
            ring(core, 230)
            bars(3, core * 0.3, 0.35 + 0.2 * fast)
        elif state == "muted":
            ring(core, 70)
            draw_mic(d, c, c - core * 0.06, round(core * 0.34), 170, True)
        elif state == "offline":
            ring(core, 50)
            for index in range(3):
                r = core * 0.05
                x = c + (index - 1) * core * 0.3
                d.ellipse((x - r, c - r, x + r, c + r), fill=110)
        elif state == "error":
            ring(core, 170)
            w = core * 0.05
            d.rounded_rectangle((c - w, c - core * 0.42, c + w, c + core * 0.12), radius=w, fill=240)
            d.ellipse((c - w * 1.2, c + core * 0.24, c + w * 1.2, c + core * 0.24 + w * 2.4), fill=240)

        frame.paste(self.canvas.reduce(self.SCALE).convert("RGB"), rect[:2])

def draw_wifi(d, x, y, bars, on, off):
    for index, radius in enumerate((7, 13, 19)):
        d.arc((x - radius, y - radius, x + radius, y + radius), 225, 315,
              fill=on if bars > index else off, width=3)
    d.ellipse((x - 3, y - 3, x + 3, y + 3), fill=on if bars > 0 else off)


def draw_plug(d, x, cy, color):
    d.rounded_rectangle((x - 10, cy - 6, x + 10, cy + 7), radius=3, outline=color, width=2)
    d.line((x - 4, cy - 12, x - 4, cy - 6), fill=color, width=3)
    d.line((x + 4, cy - 12, x + 4, cy - 6), fill=color, width=3)
    d.line((x, cy + 7, x, cy + 13), fill=color, width=3)


def draw_speaker(d, x, cy, color, level):
    d.rectangle((x, cy - 4, x + 5, cy + 4), fill=color)
    d.polygon([(x + 5, cy - 4), (x + 13, cy - 10), (x + 13, cy + 10), (x + 5, cy + 4)], fill=color)
    if level < 0:
        d.line((x + 17, cy - 5, x + 26, cy + 5), fill=color, width=2)
        d.line((x + 17, cy + 5, x + 26, cy - 5), fill=color, width=2)
        return
    for index, radius in enumerate((7, 12)):
        if level > index:
            d.arc((x + 9 - radius, cy - radius, x + 9 + radius, cy + radius), -45, 45, fill=color, width=2)


def draw_mic(d, cx, cy, size, color, muted):
    stroke = max(3, size // 7)
    d.rounded_rectangle((cx - size * 0.36, cy - size, cx + size * 0.36, cy + size * 0.25),
                        radius=size * 0.36, fill=color)
    d.arc((cx - size * 0.64, cy - size * 0.62, cx + size * 0.64, cy + size * 0.62), 0, 180,
          fill=color, width=stroke)
    d.line((cx, cy + size * 0.62, cx, cy + size * 0.95), fill=color, width=stroke)
    d.line((cx - size * 0.38, cy + size * 0.95, cx + size * 0.38, cy + size * 0.95), fill=color, width=stroke)
    if muted:
        d.line((cx - size * 0.8, cy - size * 0.95, cx + size * 0.8, cy + size * 1.0),
               fill=color, width=max(4, size // 5))


def draw_gear(d, cx, cy, r, color, hole):
    for index in range(8):
        angle = index * math.pi / 4
        dx, dy = math.cos(angle), math.sin(angle)
        px, py = -dy * r * 0.2, dx * r * 0.2
        d.polygon([
            (cx + px + dx * r * 0.5, cy + py + dy * r * 0.5),
            (cx - px + dx * r * 0.5, cy - py + dy * r * 0.5),
            (cx - px + dx * r, cy - py + dy * r),
            (cx + px + dx * r, cy + py + dy * r),
        ], fill=color)
    d.ellipse((cx - r * 0.78, cy - r * 0.78, cx + r * 0.78, cy + r * 0.78), fill=color)
    d.ellipse((cx - r * 0.34, cy - r * 0.34, cx + r * 0.34, cy + r * 0.34), fill=hole)


def draw_cloud(d, cx, cy, s, color):
    d.ellipse((cx - s * 0.55, cy - s * 0.12, cx - s * 0.05, cy + s * 0.36), fill=color)
    d.ellipse((cx - s * 0.32, cy - s * 0.44, cx + s * 0.28, cy + s * 0.16), fill=color)
    d.ellipse((cx - s * 0.02, cy - s * 0.2, cx + s * 0.55, cy + s * 0.36), fill=color)
    d.rounded_rectangle((cx - s * 0.5, cy + s * 0.06, cx + s * 0.5, cy + s * 0.36), radius=s * 0.15, fill=color)


def draw_sun(d, cx, cy, s, day, background):
    if not day:
        d.ellipse((cx - s * 0.32, cy - s * 0.32, cx + s * 0.32, cy + s * 0.32), fill=TEXT)
        d.ellipse((cx - s * 0.12, cy - s * 0.42, cx + s * 0.44, cy + s * 0.14), fill=background)
        return
    for index in range(8):
        angle = index * math.pi / 4
        d.line((cx + math.cos(angle) * s * 0.36, cy + math.sin(angle) * s * 0.36,
                cx + math.cos(angle) * s * 0.5, cy + math.sin(angle) * s * 0.5), fill=TEXT, width=max(3, int(s * 0.05)))
    d.ellipse((cx - s * 0.24, cy - s * 0.24, cx + s * 0.24, cy + s * 0.24), fill=TEXT)


def draw_weather_icon(d, kind, cx, cy, s, day, background):
    cloud = DIM
    if kind == "sun":
        draw_sun(d, cx, cy, s, day, background)
    elif kind == "partly":
        draw_sun(d, cx - s * 0.18, cy - s * 0.18, s * 0.8, day, background)
        draw_cloud(d, cx + s * 0.06, cy + s * 0.08, s * 0.85, cloud)
    elif kind == "fog":
        for index in range(3):
            y = cy - s * 0.25 + index * s * 0.25
            d.rounded_rectangle((cx - s * 0.48, y - 4, cx + s * 0.48 - index * s * 0.1, y + 4), radius=4, fill=cloud)
    else:
        lift = s * 0.14 if kind in ("rain", "snow", "storm") else 0
        draw_cloud(d, cx, cy - lift, s, FAINT if kind == "storm" else cloud)
        base = cy + s * 0.3
        if kind == "rain":
            for index in range(3):
                x = cx - s * 0.25 + index * s * 0.25
                d.line((x, base, x - s * 0.08, base + s * 0.2), fill=TEXT, width=max(3, int(s * 0.05)))
        elif kind == "snow":
            for index in range(3):
                x = cx - s * 0.25 + index * s * 0.25
                d.ellipse((x - 4, base + s * 0.08 - 4, x + 4, base + s * 0.08 + 4), fill=WHITE)
        elif kind == "storm":
            d.polygon([(cx + s * 0.05, base - s * 0.04), (cx - s * 0.12, base + s * 0.16), (cx, base + s * 0.16),
                       (cx - s * 0.08, base + s * 0.34), (cx + s * 0.16, base + s * 0.1), (cx + s * 0.03, base + s * 0.1)],
                      fill=TEXT)

class App:
    def __init__(self):
        self.running = True
        self.fb = Framebuffer(os.getenv("THINKSMART_UI_FRAMEBUFFER", "/dev/fb0"))
        if (self.fb.width, self.fb.height) != (W, H):
            logging.warning("显示尺寸为 %dx%d，界面按 %dx%d 布局", self.fb.width, self.fb.height, W, H)
        self.console = Console()
        self.frame = Image.new("RGB", (W, H))
        self.draw = ImageDraw.Draw(self.frame)
        self.bg = self.make_background()
        self.bg_crops = {}
        self.fonts = Fonts()
        self.orb = Orb()
        self.ime = PinyinInput()
        self.settings = Settings()
        self.status = SystemStatus(self.settings)
        self.link = VoiceLink()
        self.touch = Touchscreen(W, H)
        self.touch_retry = 0.0
        self.touch_warned = False

        self.screen = "home"
        self.layout = None
        self.widgets = []
        self.named = {}
        self.orb_rect = None
        self.dirty = set()
        self.full_redraw = True
        self.pressed = None
        self.active = None
        self.swallow = False
        self.drag_value = None
        self.rate_drag = None
        self.wrap_cache = {}

        now = time.monotonic()
        self.phase = "idle"
        self.conv_until = 0.0
        self.partial = ""
        self.user_text = ""
        self.answer = ""
        self.error_code = None
        self.error_until = 0.0
        self.notice = None
        self.alarm_active = False
        self.cloud = "unknown"
        self.mic_on = self.settings["mic"]
        self.wake_phrase = read_wake_phrase()
        self.wake_pending = None
        self.music = {"s": "stopped"}
        self.music_at = 0.0
        self.account = {"s": "unknown", "vip": False}
        self.login = None
        self.login_kind = "mobile"
        self.qr_image = None
        self.lyric_key = None
        self.lyric_lines = []
        self.lyric_times = []
        self.lyric_index = -1
        self.next_lyric = 0.0
        self.voice_online = voice_running()
        self.configured = credentials_configured()
        self.level = 0.0
        self.level_target = 0.0
        self.last_activity = now
        self.dimmed = False
        self.asleep = False
        self.status_version = -1
        self.clock_key = None
        self.next_tick = 0.0
        self.next_frame = 0.0
        self.next_flush = 0.0
        self.next_voice_check = 0.0
        self.next_config_check = 0.0
        self.last_failure_log = 0.0

        saved = self.settings["brightness"]
        self.brightness = saved or self.status.backlight.read_percent() or 80
        if saved and self.status.backlight.writable():
            self.status.set_brightness(saved)

        self.kb_purpose = "wake"
        self.kb_text = ""
        self.kb_buffer = ""
        self.kb_latin = False
        self.kb_page = 0
        self.kb_message = ("", DIM)
        self.candidates = []
        self.consume = 0
        self.link.send("hello")

    @staticmethod
    def make_background():
        return Image.new("RGB", (W, H), BG)

    # ---- layout -------------------------------------------------------

    def wanted_layout(self, now):
        if self.screen == "places":
            return f"places:{self.status.places_state}:{len(self.status.places)}"
        if self.screen == "music":
            return "music:account" if self.account["s"] == "ok" else "music:login"
        if self.screen != "home":
            return self.screen
        if self.phase in ACTIVE or now < self.conv_until:
            return "conv"
        if not self.music_visible(now):
            return "idle"
        return "idle:music:lyrics" if self.has_lyrics() else "idle:music"

    def has_lyrics(self):
        return bool(self.lyric_lines) and self.lyric_key == self.music.get("k")

    def music_visible(self, now):
        return (
            self.voice_online
            and self.music.get("s") in MUSIC_ACTIVE
            and not (self.error_code and now < self.error_until)
            and not (self.notice and now < self.notice[2])
        )

    def build(self, layout):
        self.layout = layout
        self.pressed = None
        self.active = None
        self.drag_value = None
        self.orb_rect = None
        kind = layout.split(":")[0]
        builder = {
            "idle": self.build_idle,
            "conv": self.build_conv,
            "settings": self.build_settings,
            "keyboard": self.build_keyboard,
            "places": self.build_places,
            "music": self.build_music,
        }[kind]
        self.widgets = builder()
        self.named = {widget.name: widget for widget in self.widgets}
        if len(self.bg_crops) > 96:
            self.bg_crops.clear()
        self.full_redraw = True

    def status_widget(self):
        return Widget("status", (0, 0, W, 52), self.paint_status)

    def bottom_buttons(self):
        return [
            Widget("mic", (40, 1168, 260, 1256), self.paint_mic_button, tap=self.toggle_mic),
            Widget("settings", (540, 1168, 760, 1256), self.paint_settings_button, tap=self.open_settings),
        ]

    def build_idle(self):
        if self.layout == "idle:music:lyrics":
            self.lyric_index = -2
            center = Widget("lyrics", (24, 276, 776, 1016), self.paint_lyrics, tap=self.tap_orb)
        else:
            self.orb_rect = (180, 400, 620, 840)
            center = Widget("orb", self.orb_rect, None, tap=self.tap_orb)
        if self.layout.startswith("idle:music"):
            bottom = Widget("player", (40, 1032, 760, 1142), self.paint_player, tap=self.tap_player)
        else:
            bottom = Widget("hint", (40, 880, 760, 960), self.paint_hint)
        return [
            self.status_widget(),
            Widget("clock", (40, 64, 420, 190), self.paint_clock),
            Widget("date", (40, 190, 420, 236), self.paint_date),
            Widget("weather", (420, 64, 760, 236), self.paint_weather),
            center,
            bottom,
        ] + self.bottom_buttons()

    def build_conv(self):
        self.orb_rect = (260, 68, 540, 348)
        return [
            self.status_widget(),
            Widget("orb", self.orb_rect, None, tap=self.tap_orb),
            Widget("phase", (0, 356, W, 420), self.paint_phase),
            Widget("user", (40, 432, 760, 568), self.paint_user),
            Widget("answer", (40, 580, 760, 1148), self.paint_answer),
            Widget("stop", (290, 1168, 510, 1256), self.paint_stop, tap=self.tap_stop),
        ] + self.bottom_buttons()

    def header(self, title, button, action):
        return [
            self.status_widget(),
            Widget("title", (0, 72, 560, 172), lambda d, r, p: self.paint_title(d, r, title)),
            Widget("header", (580, 88, 760, 156),
                   lambda d, r, p: self.paint_button(d, r, p, button, primary=True), tap=action),
        ]

    def build_settings(self):
        return self.header("设置", "完成", self.close_settings) + [
            Widget("volume", (40, 190, 760, 302), self.paint_volume, drag=self.drag_volume),
            Widget("brightness", (40, 306, 760, 418), self.paint_brightness, drag=self.drag_brightness),
            Widget("speed", (40, 422, 760, 534), self.paint_speed, drag=self.drag_speed),
            Widget("wake", (40, 538, 760, 622), self.paint_wake_row, tap=lambda x, y: self.open_keyboard("wake")),
            Widget("city", (40, 626, 760, 710), self.paint_city_row, tap=lambda x, y: self.open_keyboard("city")),
            Widget("microw", (40, 714, 760, 798), self.paint_mic_row, tap=self.toggle_mic),
            Widget("think", (40, 802, 760, 886), self.paint_think_row, tap=self.toggle_thinking),
            Widget("dim", (40, 890, 760, 974), self.paint_dim_row, tap=self.cycle_dim),
            Widget("qqmusic", (40, 978, 760, 1062), self.paint_qqmusic_row, tap=self.open_music),
            Widget("notice", (40, 1066, 760, 1100), self.paint_notice),
            Widget("info", (40, 1104, 760, 1280), self.paint_info),
        ]

    def build_music(self):
        widgets = self.header("QQ音乐", "返回", self.close_music)
        if self.layout == "music:account":
            return widgets + [
                Widget("account", (40, 220, 760, 420), self.paint_account),
                Widget("logout", (240, 470, 560, 560),
                       lambda d, r, p: self.paint_button(d, r, p, "退出登录"), tap=self.logout_music),
            ]
        for index, (kind, label) in enumerate(LOGIN_KINDS):
            left = 40 + index * 246
            widgets.append(Widget(
                f"tab:{kind}",
                (left, 204, left + 228, 284),
                lambda d, r, p, k=kind, t=label: self.paint_button(d, r, p, t, primary=self.login_kind == k),
                tap=lambda x, y, k=kind: self.start_login(k),
            ))
        return widgets + [
            Widget("qr", (160, 320, 640, 800), self.paint_qr, tap=self.tap_qr),
            Widget("login_status", (40, 824, 760, 884), self.paint_login_status),
            Widget("login_hint", (40, 900, 760, 1040), self.paint_login_hint),
        ]

    def build_keyboard(self):
        title = "修改唤醒词" if self.kb_purpose == "wake" else "天气城市"
        widgets = self.header(title, "取消", self.close_keyboard) + [
            Widget("field", (40, 196, 760, 336), self.paint_field),
            Widget("message", (40, 344, 760, 404), self.paint_kb_message),
            Widget("cands", (0, 596, W, 700), self.paint_cands, tap=self.tap_cands),
        ]

        def key(name, rect, label, primary=False):
            widgets.append(Widget(
                "key:" + name,
                rect,
                lambda d, r, p: self.paint_key(d, r, p, name, label, primary),
                tap=lambda x, y: self.press_key(name),
            ))

        for letters, left, top in (("qwertyuiop", 14, 716), ("asdfghjkl", 53, 830)):
            for index, letter in enumerate(letters):
                x = left + index * 78
                key(letter, (x, top, x + 70, top + 100), letter)
        key("mode", (17, 944, 123, 1044), "")
        for index, letter in enumerate("zxcvbnm"):
            x = 131 + index * 78
            key(letter, (x, 944, x + 70, 1044), letter)
        key("back", (677, 944, 783, 1044), "")
        key("clear", (14, 1058, 164, 1158), "清空")
        key("space", (172, 1058, 558, 1158), "空格")
        key("ok", (566, 1058, 786, 1158), "确定", primary=True)
        return widgets

    def build_places(self):
        widgets = self.header("选择城市", "返回", lambda x, y: self.set_screen("keyboard"))
        places = self.status.places
        if self.status.places_state != "done" or not places:
            widgets.append(Widget("places", (40, 220, 760, 320), self.paint_places_message))
        for index in range(min(6, len(places))):
            top = 200 + index * 128
            widgets.append(Widget(
                f"place:{index}",
                (40, top, 760, top + 112),
                lambda d, r, p, i=index: self.paint_place(d, r, p, i),
                tap=lambda x, y, i=index: self.select_place(i),
            ))
        return widgets

    # ---- rendering ----------------------------------------------------

    def bg_crop(self, rect):
        crop = self.bg_crops.get(rect)
        if crop is None:
            crop = self.bg_crops[rect] = self.bg.crop(rect)
        return crop

    def render(self, now):
        if self.full_redraw:
            self.full_redraw = False
            self.dirty.clear()
            self.frame.paste(self.bg, (0, 0))
            for widget in self.widgets:
                if widget.paint:
                    widget.paint(self.draw, widget.rect, self.pressed == widget.name)
            if self.orb_rect:
                self.paint_orb(now)
            self.fb.blit(self.frame, (0, 0, W, H))
            self.next_frame = now + 0.04
            return
        if not self.dirty or now < self.next_flush:
            return
        names = self.dirty
        self.dirty = set()
        self.next_flush = now + 0.04
        for widget in self.widgets:
            if widget.name in names and widget.paint:
                self.frame.paste(self.bg_crop(widget.rect), widget.rect[:2])
                widget.paint(self.draw, widget.rect, self.pressed == widget.name)
                self.fb.blit(self.frame, widget.rect)

    def animate(self, now):
        self.frame.paste(self.bg_crop(self.orb_rect), self.orb_rect[:2])
        self.paint_orb(now)
        self.fb.blit(self.frame, self.orb_rect)

    def orb_state(self, now):
        if not self.configured or not self.voice_online:
            return "offline"
        if not self.mic_on:
            return "muted"
        if self.phase in ACTIVE:
            return self.phase
        if now < self.error_until and self.error_code != "nospeech":
            return "error"
        return "idle"

    def paint_orb(self, now):
        self.level += (self.level_target - self.level) * 0.35
        self.level_target *= 0.86
        self.orb.render(self.frame, self.orb_rect, self.orb_state(now), now, self.level)

    def fit(self, text, font, width):
        if font.getlength(text) <= width:
            return text
        while text and font.getlength(text + "…") > width:
            text = text[:-1]
        return text + "…"

    def wrap(self, font, text, width):
        key = (id(font), width, text)
        lines = self.wrap_cache.get(key)
        if lines is not None:
            return lines
        lines = []
        for paragraph in text.split("\n"):
            line = ""
            used = 0.0
            for char in paragraph:
                size = self.fonts.char_width(font, char)
                if line and used + size > width:
                    lines.append(line)
                    line, used = char, size
                else:
                    line += char
                    used += size
            lines.append(line)
        if len(self.wrap_cache) > 24:
            self.wrap_cache.clear()
        self.wrap_cache[key] = lines
        return lines

    def panel(self, d, rect, pressed=False):
        if pressed:
            d.rounded_rectangle(rect, radius=24, fill=CARD_PRESSED)

    def row(self, d, rect, pressed=False):
        self.panel(d, rect, pressed)
        d.line((rect[0] + 8, rect[3] - 1, rect[2] - 8, rect[3] - 1), fill=LINE, width=2)

    def cloud_label(self):
        if not self.configured:
            return "未配置", WARN
        if not self.voice_online:
            return "语音离线", BAD
        return {
            "ready": ("阿里云 就绪", FAINT),
            "busy": ("阿里云 处理中", DIM),
            "offline": ("阿里云 离线", BAD),
            "auth": ("阿里云 鉴权失败", BAD),
            "error": ("阿里云 异常", WARN),
        }.get(self.cloud, ("阿里云 连接中", DIM))

    def current_volume(self):
        if self.drag_value is not None and self.active is self.named.get("volume"):
            return self.drag_value
        pending = self.status.pending_volume
        return pending if pending is not None else self.status.volume

    def paint_status(self, d, rect, pressed):
        x1 = rect[2]
        cy = (rect[1] + rect[3]) // 2
        font = self.fonts.ui(21)
        network = self.status.network
        kind = network["kind"]
        if kind == "wifi":
            signal = network.get("signal")
            bars = 3 if signal is None or signal >= 66 else 2 if signal >= 33 else 1
            draw_wifi(d, 48, cy + 8, bars, DIM, LINE)
            label, color = network.get("ssid") or "Wi-Fi", FAINT
        elif kind in ("ethernet", "usb"):
            draw_plug(d, 48, cy, DIM)
            label, color = ("有线网络" if kind == "ethernet" else "USB 网络"), FAINT
        else:
            draw_wifi(d, 48, cy + 8, 0, FAINT, FAINT)
            d.line((34, cy - 10, 62, cy + 10), fill=BAD, width=3)
            label, color = "未联网", BAD
        d.text((76, cy), self.fit(label, font, 220), font=font, fill=color, anchor="lm")

        volume = self.current_volume()
        muted = self.status.muted or volume == 0
        volume_text = "静音" if muted else ("--" if volume is None else f"{volume}%")
        right = x1 - 40
        d.text((right, cy), volume_text, font=font, fill=FAINT, anchor="rm")
        speaker_x = right - font.getlength(volume_text) - 34
        draw_speaker(d, speaker_x, cy, DIM, -1 if muted else (0 if volume is None else 1 if volume < 50 else 2))

        label, color = self.cloud_label()
        d.text((speaker_x - 22, cy), label, font=font, fill=color, anchor="rm")

    def paint_clock(self, d, rect, pressed):
        d.text((rect[0] + 4, rect[3] - 14), time.strftime("%H:%M"),
               font=self.fonts.thin(108), fill=TEXT, anchor="ls")

    def paint_date(self, d, rect, pressed):
        now = time.localtime()
        d.text((rect[0] + 10, (rect[1] + rect[3]) // 2),
               f"{now.tm_mon}月{now.tm_mday}日  星期{WEEKDAYS[now.tm_wday]}",
               font=self.fonts.ui(28), fill=DIM, anchor="lm")

    def paint_weather(self, d, rect, pressed):
        x0, y0, x1, y1 = rect
        right = x1 - 4
        weather = self.status.weather
        small = self.fonts.ui(24)
        if not weather:
            if self.status.network["kind"] == "none":
                detail = "等待网络连接…"
            elif self.status.weather_failed:
                detail = "天气获取失败"
            else:
                detail = "正在获取天气…"
            d.text((right, y1 - 23), self.fit(detail, small, x1 - x0), font=small, fill=FAINT, anchor="rm")
            return
        description, kind = WEATHER_CODES.get(weather.get("code"), ("未知", "cloud"))
        temp_font = self.fonts.thin(64)
        temp = f"{round(weather['temp'])}°"
        icon = 60
        baseline = y0 + 112
        d.text((right, baseline), temp, font=temp_font, fill=TEXT, anchor="rs")
        left = right - temp_font.getlength(temp) - 18 - icon
        draw_weather_icon(d, kind, left + icon / 2, baseline - 24, icon, weather.get("day", True), BG)
        parts = [self.settings["location"]["name"], description]
        days = weather.get("days") or []
        if days:
            parts.append(f"{round(days[0]['min'])}°/{round(days[0]['max'])}°")
        if self.status.weather_failed:
            parts.append("离线")
        line = self.fit(" · ".join(parts), small, x1 - x0)
        d.text((right, y1 - 23), line, font=small, fill=DIM, anchor="rm")

    def hint(self, now):
        if self.error_code and now < self.error_until:
            return ERRORS.get(self.error_code, ERRORS["service"]), WARN if self.error_code == "nospeech" else BAD
        if self.notice and now < self.notice[2]:
            return self.notice[0], self.notice[1]
        if not self.configured:
            return "请先配置阿里云百炼凭据", WARN
        if not self.voice_online:
            return "语音服务未运行，正在等待…", WARN
        if not self.mic_on:
            return "麦克风已关闭，轻触左下角开启", WARN
        return f"说“{self.wake_phrase}”，或轻触圆环", FAINT

    def paint_hint(self, d, rect, pressed):
        text, color = self.hint(time.monotonic())
        font = self.fonts.ui(30)
        d.text((W // 2, (rect[1] + rect[3]) // 2), self.fit(text, font, rect[2] - rect[0]),
               font=font, fill=color, anchor="mm")

    def paint_mic_button(self, d, rect, pressed):
        self.panel(d, rect, pressed)
        cy = (rect[1] + rect[3]) // 2
        color = DIM if self.mic_on else TEXT
        draw_mic(d, rect[0] + 62, cy - 2, 24, color, not self.mic_on)
        d.text((rect[0] + 104, cy), "麦克风" if self.mic_on else "已静音",
               font=self.fonts.ui(30), fill=color, anchor="lm")

    def paint_settings_button(self, d, rect, pressed):
        self.panel(d, rect, pressed)
        cy = (rect[1] + rect[3]) // 2
        draw_gear(d, rect[0] + 62, cy, 24, DIM, CARD_PRESSED if pressed else BG)
        d.text((rect[0] + 104, cy), "设置", font=self.fonts.ui(30), fill=DIM, anchor="lm")

    def paint_stop(self, d, rect, pressed):
        if self.phase not in ACTIVE and not self.alarm_active:
            return
        d.rounded_rectangle(rect, radius=52, fill=CARD_PRESSED if pressed else BG, outline=FAINT, width=2)
        cy = (rect[1] + rect[3]) // 2
        d.rounded_rectangle((rect[0] + 58, cy - 12, rect[0] + 82, cy + 12), radius=4, fill=TEXT)
        d.text((rect[0] + 100, cy), "停止" if self.alarm_active else "结束", font=self.fonts.ui(30), fill=TEXT, anchor="lm")
    @staticmethod
    def clock_text(seconds):
        seconds = max(0, int(seconds))
        return f"{seconds // 60}:{seconds % 60:02d}"

    def music_position(self, now):
        position = float(self.music.get("pos") or 0)
        if self.music.get("s") == "playing":
            position += now - self.music_at
        duration = float(self.music.get("dur") or 0)
        return min(position, duration) if duration > 0 else position, duration

    def paint_player(self, d, rect, pressed):
        x0, y0, x1, y1 = rect
        state = self.music.get("s")
        controls = x1 - 336
        title_font = self.fonts.ui(34)
        d.text((x0 + 8, y0 + 34), self.fit(self.music.get("t") or "", title_font, controls - x0 - 24),
               font=title_font, fill=TEXT, anchor="lm")
        position, duration = self.music_position(time.monotonic())
        if state == "loading":
            detail = f"{self.music.get('a') or ''}  ·  正在加载…"
        else:
            detail = self.music.get("a") or ""
            if duration > 0:
                detail += f"  {self.clock_text(position)} / {self.clock_text(duration)}"
            if state == "paused":
                detail += "  ·  已暂停"
        small = self.fonts.ui(26)
        d.text((x0 + 8, y0 + 78), self.fit(detail.strip(), small, controls - x0 - 24),
               font=small, fill=DIM, anchor="lm")
        left, right, y = x0 + 8, x1 - 8, y1 - 4
        d.line((left, y, right, y), fill=LINE, width=2)
        if duration > 0:
            d.line((left, y, left + (right - left) * min(1.0, position / duration), y), fill=DIM, width=2)
        cy = y0 + 52
        cx = controls + 42
        d.rounded_rectangle((cx - 34, cy - 22, cx + 34, cy + 22), radius=22, outline=FAINT, width=2)
        d.text((cx, cy), MUSIC_MODES.get(self.music.get("m"), MUSIC_MODES["shuffle"]),
               font=self.fonts.ui(24), fill=DIM, anchor="mm")
        cx = controls + 126
        d.rectangle((cx - 15, cy - 15, cx - 11, cy + 15), fill=DIM)
        d.polygon([(cx + 15, cy - 15), (cx - 9, cy), (cx + 15, cy + 15)], fill=DIM)
        cx = controls + 210
        d.ellipse((cx - 36, cy - 36, cx + 36, cy + 36), outline=FAINT, width=2)
        if state == "paused":
            d.polygon([(cx - 10, cy - 17), (cx - 10, cy + 17), (cx + 18, cy)], fill=TEXT)
        else:
            d.rectangle((cx - 12, cy - 16, cx - 5, cy + 16), fill=TEXT)
            d.rectangle((cx + 5, cy - 16, cx + 12, cy + 16), fill=TEXT)
        cx = controls + 294
        d.rectangle((cx + 11, cy - 15, cx + 15, cy + 15), fill=DIM)
        d.polygon([(cx - 15, cy - 15), (cx + 9, cy), (cx - 15, cy + 15)], fill=DIM)

    def update_lyric(self, now):
        position = self.music_position(now)[0] - LYRIC_DELAY
        index = bisect.bisect_right(self.lyric_times, position) - 1
        if index != self.lyric_index:
            self.lyric_index = index
            self.dirty.add("lyrics")
        if self.music.get("s") == "playing" and index + 1 < len(self.lyric_times):
            self.next_lyric = now + max(0.05, self.lyric_times[index + 1] - position)
        else:
            self.next_lyric = now + 1.0

    def paint_lyrics(self, d, rect, pressed):
        x0, y0, x1, y1 = rect
        cy = (y0 + y1) // 2
        width = x1 - x0 - 32
        for offset in range(-LYRIC_SPAN, LYRIC_SPAN + 1):
            index = self.lyric_index + offset
            if not 0 <= index < len(self.lyric_lines):
                continue
            text = self.lyric_lines[index][1]
            if offset == 0:
                font = next((self.fonts.ui(size) for size in (52, 46) if self.fonts.ui(size).getlength(text) <= width),
                            self.fonts.ui(40))
                color = TEXT
            else:
                font = self.fonts.ui(36)
                color = mix(DIM, BG, (abs(offset) - 1) / LYRIC_SPAN)
            y = cy + offset * 80 + (14 if offset > 0 else -14 if offset < 0 else 0)
            d.text((W // 2, y), self.fit(text, font, width), font=font, fill=color, anchor="mm")

    def paint_qqmusic_row(self, d, rect, pressed):
        state = self.account["s"]
        if not self.voice_online:
            value = "语音服务未运行"
        elif state == "ok":
            value = "已登录 · 会员" if self.account["vip"] else "已登录"
        elif state == "expired":
            value = "登录已过期"
        else:
            value = "未登录"
        self.paint_row(d, rect, pressed, "QQ音乐", value)

    def paint_account(self, d, rect, pressed):
        x0, y0, x1, y1 = rect
        d.text((W // 2, y0 + 60), "已登录QQ音乐", font=self.fonts.ui(44), fill=TEXT, anchor="mm")
        detail = "会员账号，可播放会员歌曲" if self.account["vip"] else "非会员账号，会员歌曲将尝试改用网易云免费资源"
        d.text((W // 2, y0 + 130), self.fit(detail, self.fonts.ui(28), x1 - x0), font=self.fonts.ui(28),
               fill=DIM, anchor="mm")

    def paint_qr(self, d, rect, pressed):
        x0, y0, x1, y1 = rect
        state = (self.login or {}).get("s")
        if self.qr_image is not None and state in ("qr", "scanned"):
            d.rounded_rectangle(rect, radius=24, fill=WHITE)
            image = self.qr_image
            self.frame.paste(image, ((x0 + x1 - image.width) // 2, (y0 + y1 - image.height) // 2))
            if state == "scanned":
                d.rounded_rectangle((x0 + 120, y0 + 200, x1 - 120, y1 - 200), radius=20, fill=BG)
                d.text(((x0 + x1) // 2, (y0 + y1) // 2), "已扫码", font=self.fonts.ui(36), fill=TEXT, anchor="mm")
            return
        d.rounded_rectangle(rect, radius=24, outline=LINE, width=2)
        if not self.voice_online:
            text = "语音服务未运行"
        elif state == "wait":
            text = "正在获取二维码…"
        elif state is None:
            text = "轻触获取二维码"
        else:
            text = "轻触刷新二维码"
        d.text(((x0 + x1) // 2, (y0 + y1) // 2), text, font=self.fonts.ui(32), fill=DIM, anchor="mm")

    def paint_login_status(self, d, rect, pressed):
        login = self.login or {}
        state = login.get("s")
        app = LOGIN_APPS.get(self.login_kind, "QQ音乐App")
        text, color = {
            "qr": (f"请用{app}扫码登录", TEXT),
            "scanned": ("已扫码，请在手机上确认登录", TEXT),
            "expired": ("二维码已过期，轻触刷新", WARN),
            "refused": ("已取消登录，轻触二维码重试", WARN),
            "error": ("登录失败：" + str(login.get("msg") or "请稍后重试"), BAD),
        }.get(state, ("", DIM))
        if text:
            font = self.fonts.ui(32)
            d.text((W // 2, (rect[1] + rect[3]) // 2), self.fit(text, font, rect[2] - rect[0]),
                   font=font, fill=color, anchor="mm")

    def paint_login_hint(self, d, rect, pressed):
        app = LOGIN_APPS.get(self.login_kind, "QQ音乐App")
        lines = (f"在手机上打开{app}，用“扫一扫”扫描上方二维码", "登录后可播放会员歌曲，凭证只保存在本机")
        font = self.fonts.ui(26)
        for index, line in enumerate(lines):
            d.text((W // 2, rect[1] + 30 + index * 44), self.fit(line, font, rect[2] - rect[0]),
                   font=font, fill=FAINT, anchor="mm")

    def paint_phase(self, d, rect, pressed):
        now = time.monotonic()
        if self.phase in PHASE_TEXT:
            text, color = PHASE_TEXT[self.phase]
        elif self.error_code and now < self.error_until:
            text, color = self.hint(now)
        else:
            text, color = f"说“{self.wake_phrase}”继续对话", FAINT
        font = self.fonts.ui(36)
        d.text((W // 2, (rect[1] + rect[3]) // 2), self.fit(text, font, W - 80), font=font, fill=color, anchor="mm")

    def paint_user(self, d, rect, pressed):
        x0, y0, x1, _y1 = rect
        text = self.partial or self.user_text
        final = bool(self.user_text) and not self.partial
        if not text:
            if self.phase not in ("wake", "listen"):
                return
            text = "请说出你的问题…"
        d.text((x0 + 4, y0 + 14), "你说", font=self.fonts.ui(24), fill=FAINT, anchor="lm")
        font = self.fonts.ui(36)
        lines = self.wrap(font, text, x1 - x0 - 8)
        if len(lines) > 2:
            lines = ["…" + lines[-2]] + lines[-1:]
        y = y0 + 34
        for line in lines:
            d.text((x0 + 4, y), line, font=font, fill=TEXT if final else DIM)
            y += 48

    def paint_answer(self, d, rect, pressed):
        x0, y0, x1, y1 = rect
        d.line((x0 + 4, y0 + 2, x1 - 4, y0 + 2), fill=LINE, width=2)
        if not self.answer:
            return
        font = self.fonts.ui(38)
        line_height = 58
        lines = self.wrap(font, self.answer, x1 - x0 - 8)
        visible = (y1 - y0 - 40) // line_height
        if len(lines) > visible:
            lines = lines[-visible:]
        y = y0 + 28
        for line in lines:
            d.text((x0 + 4, y), line, font=font, fill=TEXT)
            y += line_height

    def paint_title(self, d, rect, title):
        d.text((48, (rect[1] + rect[3]) // 2), title, font=self.fonts.ui(48), fill=TEXT, anchor="lm")

    def paint_button(self, d, rect, pressed, label, primary=False):
        if primary:
            d.rounded_rectangle(rect, radius=34, fill=ACCENT_PRESSED if pressed else ACCENT)
        else:
            d.rounded_rectangle(rect, radius=34, fill=CARD_PRESSED if pressed else BG, outline=FAINT, width=2)
        d.text(((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2), label,
               font=self.fonts.ui(32), fill=BG if primary else TEXT, anchor="mm")

    def paint_slider(self, d, rect, label, value, enabled, shown=None):
        x0, y0, x1, y1 = rect
        self.row(d, rect)
        d.text((x0 + 32, y0 + 38), label, font=self.fonts.ui(34), fill=TEXT, anchor="lm")
        if not enabled or value is None:
            d.text((x1 - 32, y0 + 38), "不可调节", font=self.fonts.ui(30), fill=FAINT, anchor="rm")
        else:
            d.text((x1 - 32, y0 + 38), shown or f"{value}%", font=self.fonts.ui(32), fill=DIM, anchor="rm")
        left, right, y = x0 + 40, x1 - 40, y1 - 34
        d.rounded_rectangle((left, y - 3, right, y + 3), radius=3, fill=LINE)
        if enabled and value is not None:
            knob = left + (right - left) * value / 100
            d.rounded_rectangle((left, y - 3, max(left + 6, knob), y + 3), radius=3, fill=ACCENT)
            d.ellipse((knob - 18, y - 18, knob + 18, y + 18), fill=WHITE)

    def slider_value(self, name, x):
        rect = self.named[name].rect
        left, right = rect[0] + 40, rect[2] - 40
        return round(min(1.0, max(0.0, (x - left) / (right - left))) * 100)

    def paint_volume(self, d, rect, pressed):
        self.paint_slider(d, rect, "音量", self.current_volume(), True)

    def paint_brightness(self, d, rect, pressed):
        self.paint_slider(d, rect, "屏幕亮度", self.brightness, self.status.backlight.writable())

    def current_rate(self):
        return self.rate_drag if self.rate_drag is not None else self.settings["speech_rate"]

    def paint_speed(self, d, rect, pressed):
        rate = self.current_rate()
        self.paint_slider(d, rect, "语速", round((rate - 0.5) / 1.5 * 100), True, f"{rate:.1f}×")

    def paint_row(self, d, rect, pressed, label, value, color=DIM):
        x0, y0, x1, y1 = rect
        cy = (y0 + y1) // 2
        self.row(d, rect, pressed)
        d.text((x0 + 32, cy), label, font=self.fonts.ui(34), fill=TEXT, anchor="lm")
        right = x1 - 32
        d.line([(right - 12, cy - 12), (right, cy), (right - 12, cy + 12)], fill=FAINT, width=4)
        font = self.fonts.ui(32)
        d.text((right - 32, cy), self.fit(value, font, 380), font=font, fill=color, anchor="rm")

    def paint_wake_row(self, d, rect, pressed):
        if self.wake_pending:
            self.paint_row(d, rect, pressed, "唤醒词", "正在应用…", ACCENT)
        else:
            self.paint_row(d, rect, pressed, "唤醒词", f"“{self.wake_phrase}”")

    def paint_city_row(self, d, rect, pressed):
        self.paint_row(d, rect, pressed, "天气城市", self.settings["location"]["name"])

    def paint_dim_row(self, d, rect, pressed):
        self.paint_row(d, rect, pressed, "自动调暗", DIM_LABELS.get(self.settings["dim_after"], "5 分钟"))

    def paint_mic_row(self, d, rect, pressed):
        self.paint_toggle(d, rect, pressed, "语音唤醒", self.mic_on)

    def paint_think_row(self, d, rect, pressed):
        self.paint_toggle(d, rect, pressed, "深度思考", self.settings["thinking"])

    def paint_toggle(self, d, rect, pressed, label, on):
        x0, y0, x1, y1 = rect
        cy = (y0 + y1) // 2
        self.row(d, rect, pressed)
        d.text((x0 + 32, cy), label, font=self.fonts.ui(34), fill=TEXT, anchor="lm")
        track = (x1 - 128, cy - 26, x1 - 32, cy + 26)
        d.rounded_rectangle(track, radius=26, fill=TEXT if on else LINE)
        knob = track[2] - 26 if on else track[0] + 26
        d.ellipse((knob - 20, cy - 20, knob + 20, cy + 20), fill=BG if on else DIM)
        d.text((track[0] - 20, cy), "开启" if on else "关闭", font=self.fonts.ui(30), fill=DIM, anchor="rm")

    def paint_notice(self, d, rect, pressed):
        if self.notice and time.monotonic() < self.notice[2]:
            d.text((W // 2, (rect[1] + rect[3]) // 2), self.notice[0],
                   font=self.fonts.ui(28), fill=self.notice[1], anchor="mm")

    def paint_info(self, d, rect, pressed):
        x0, y0 = rect[0], rect[1]
        network = self.status.network
        kind = network["kind"]
        if kind == "wifi":
            net = f"Wi-Fi {network.get('ssid') or ''}".strip()
            if network.get("signal") is not None:
                net += f" · 信号 {network['signal']}%"
        elif kind == "none":
            net = "未连接"
        else:
            net = f"{'有线' if kind == 'ethernet' else 'USB'}（{network['iface']}）"
        if not self.configured:
            voice = "未配置凭据"
        else:
            voice = "运行中" if self.voice_online else "未运行"
        weather = self.status.weather
        if weather:
            stamp = time.strftime("%m-%d %H:%M", time.localtime(weather["fetched"]))
            sky = f"更新于 {stamp}" + (" · 当前离线" if self.status.weather_failed else "")
        else:
            sky = "暂无数据"
        rows = (
            ("网络", net),
            ("地址", network.get("ip") or "—"),
            ("语音服务", voice),
            ("阿里云", self.cloud_label()[0].replace("阿里云 ", "")),
            ("天气", sky),
        )
        font = self.fonts.ui(24)
        y = y0 + 20
        for label, value in rows:
            d.text((x0 + 8, y), label, font=font, fill=FAINT, anchor="lm")
            d.text((x0 + 150, y), self.fit(value, font, 560), font=font, fill=DIM, anchor="lm")
            y += 34

    def paint_field(self, d, rect, pressed):
        x0, y0, x1, y1 = rect
        d.line((x0 + 8, y0 + 92, x1 - 8, y0 + 92), fill=DIM, width=2)
        font = self.fonts.ui(44)
        width = x1 - x0 - 60
        text = self.kb_text
        buffer = self.kb_buffer
        while text and font.getlength(text + buffer) > width:
            text = text[1:]
        x = x0 + 28
        y = y0 + 54
        if text:
            d.text((x, y), text, font=font, fill=TEXT, anchor="lm")
            x += font.getlength(text)
        if buffer:
            d.text((x, y), buffer, font=font, fill=ACCENT, anchor="lm")
            end = x + font.getlength(buffer)
            d.line((x, y + 28, end, y + 28), fill=ACCENT, width=2)
            x = end
        d.rectangle((x + 4, y - 26, x + 7, y + 26), fill=ACCENT)
        if self.kb_purpose == "wake":
            hint = f"2–8 个汉字，当前：{self.wake_phrase}"
        else:
            hint = "输入城市名，中文或拼音均可（如 beijing）"
        d.text((x0 + 28, y1 - 22), hint, font=self.fonts.ui(24), fill=FAINT, anchor="lm")

    def paint_kb_message(self, d, rect, pressed):
        text, color = self.kb_message
        if text:
            d.text((W // 2, (rect[1] + rect[3]) // 2), text, font=self.fonts.ui(28), fill=color, anchor="mm")

    def paint_cands(self, d, rect, pressed):
        x0, y0, x1, y1 = rect
        cy = (y0 + y1) // 2
        d.line((x0, y0, x1, y0), fill=LINE, width=2)
        d.line((x0, y1 - 1, x1, y1 - 1), fill=LINE, width=2)
        if self.kb_latin:
            d.text((W // 2, cy), "字母输入", font=self.fonts.ui(28), fill=FAINT, anchor="mm")
            return
        if not self.kb_buffer:
            d.text((W // 2, cy), "输入拼音后在这里选择汉字", font=self.fonts.ui(28), fill=FAINT, anchor="mm")
            return
        if not self.candidates:
            d.text((W // 2, cy), "没有匹配的汉字", font=self.fonts.ui(28), fill=FAINT, anchor="mm")
            return
        page = self.candidates[self.kb_page * 8:(self.kb_page + 1) * 8]
        font = self.fonts.ui(44)
        for index, char in enumerate(page):
            d.text((80 + index * 80 + 40, cy), char, font=font, fill=TEXT, anchor="mm")
        if self.kb_page > 0:
            d.polygon([(48, cy - 18), (28, cy), (48, cy + 18)], fill=DIM)
        if (self.kb_page + 1) * 8 < len(self.candidates):
            d.polygon([(752, cy - 18), (772, cy), (752, cy + 18)], fill=DIM)

    def paint_key(self, d, rect, pressed, name, label, primary):
        x0, y0, x1, y1 = rect
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        if primary:
            d.rounded_rectangle(rect, radius=18, fill=ACCENT_PRESSED if pressed else ACCENT)
        else:
            d.rounded_rectangle(rect, radius=18, fill=CARD_PRESSED if pressed else CARD)
        if name == "mode":
            label = "英" if self.kb_latin else "中"
            d.text((cx, cy), label, font=self.fonts.ui(36), fill=ACCENT, anchor="mm")
        elif name == "back":
            d.polygon([(cx - 26, cy), (cx - 10, cy - 18), (cx + 24, cy - 18), (cx + 24, cy + 18), (cx - 10, cy + 18)],
                      outline=TEXT, width=3)
            d.line((cx - 2, cy - 8, cx + 14, cy + 8), fill=TEXT, width=3)
            d.line((cx - 2, cy + 8, cx + 14, cy - 8), fill=TEXT, width=3)
        else:
            d.text((cx, cy), label, font=self.fonts.ui(36 if len(label) == 1 else 32),
                   fill=BG if primary else TEXT, anchor="mm")

    def paint_places_message(self, d, rect, pressed):
        text = {
            "searching": ("正在搜索城市…", DIM),
            "error": ("搜索失败，请检查网络后重试", BAD),
            "empty": ("没有找到匹配的城市", WARN),
        }.get(self.status.places_state, ("", DIM))
        d.text((W // 2, (rect[1] + rect[3]) // 2), text[0], font=self.fonts.ui(32), fill=text[1], anchor="mm")

    def paint_place(self, d, rect, pressed, index):
        places = self.status.places
        if index >= len(places):
            return
        place = places[index]
        self.row(d, rect, pressed)
        d.text((rect[0] + 32, rect[1] + 38), place["name"], font=self.fonts.ui(34), fill=TEXT, anchor="lm")
        d.text((rect[0] + 32, rect[1] + 80), self.fit(place["detail"] or "—", self.fonts.ui(24), 640),
               font=self.fonts.ui(24), fill=DIM, anchor="lm")

    # ---- actions ------------------------------------------------------

    def set_screen(self, screen):
        self.screen = screen

    def set_notice(self, text, color, seconds):
        self.notice = (text, color, time.monotonic() + seconds)
        self.dirty |= {"notice", "hint"}

    def open_settings(self, *_args):
        self.screen = "settings"

    def close_settings(self, *_args):
        self.screen = "home"

    def tap_orb(self, *_args):
        if not self.voice_online:
            return
        if self.phase in ACTIVE:
            self.link.send("stop")
        elif self.mic_on:
            self.link.send("talk")

    def tap_player(self, x, y):
        zone = (x - (self.named["player"].rect[2] - 336)) // 84
        if zone >= 0:
            self.link.send("music", a=("mode", "prev", "toggle", "next")[min(3, zone)])

    def open_music(self, *_args):
        self.screen = "music"
        if self.account["s"] != "ok" and (self.login or {}).get("s") not in LOGIN_FLOW:
            self.start_login(self.login_kind)

    def close_music(self, *_args):
        self.cancel_login()
        self.screen = "settings"

    def cancel_login(self):
        if (self.login or {}).get("s") in LOGIN_FLOW:
            self.link.send("login", t="cancel")
        self.login = None
        self.qr_image = None

    def start_login(self, kind):
        self.login_kind = kind
        self.login = {"s": "wait", "t": kind}
        self.qr_image = None
        self.link.send("login", t=kind)
        self.dirty |= {"qr", "login_status", "login_hint"} | {f"tab:{k}" for k, _ in LOGIN_KINDS}

    def tap_qr(self, *_args):
        if (self.login or {}).get("s") not in ("wait", "qr", "scanned"):
            self.start_login(self.login_kind)

    def logout_music(self, *_args):
        self.link.send("login", t="logout")

    def on_login(self, message):
        state = message.get("s")
        if state in ("ok", "none", "expired"):
            self.account = {"s": state, "vip": bool(message.get("vip"))}
            self.login = None
            self.qr_image = None
        elif self.screen == "music" and message.get("t") == self.login_kind:
            self.login = {key: message.get(key) for key in ("s", "t", "msg")}
            if state == "qr":
                self.qr_image = self.decode_qr(message.get("img"))
        self.dirty |= {"qqmusic", "qr", "login_status"}

    @staticmethod
    def decode_qr(data):
        try:
            image = Image.open(io.BytesIO(base64.b64decode(data or ""))).convert("L")
        except (ValueError, OSError) as error:
            logging.warning("二维码解析失败：%s", error)
            return None
        side = 440
        factor = side // max(image.size)
        if factor > 1:
            image = image.resize((image.width * factor, image.height * factor), Image.NEAREST)
        elif max(image.size) > side:
            image.thumbnail((side, side), Image.LANCZOS)
        return image.convert("RGB")

    def tap_stop(self, *_args):
        if self.alarm_active:
            self.link.send("alarm_stop")
        elif self.phase in ACTIVE:
            self.link.send("stop")

    def toggle_mic(self, *_args):
        enabled = not self.settings["mic"]
        self.settings.update(mic=enabled)
        self.mic_on = enabled
        self.link.send("mic", on=int(enabled))
        self.dirty |= {"mic", "microw", "hint"}

    def send_prefs(self):
        self.link.send("prefs", rate=self.settings["speech_rate"], think=int(self.settings["thinking"]))

    def toggle_thinking(self, *_args):
        self.settings.update(thinking=not self.settings["thinking"])
        self.send_prefs()
        self.dirty.add("think")

    def drag_speed(self, x, y, phase):
        rate = round(0.5 + 1.5 * self.slider_value("speed", x) / 100, 1)
        if phase == "up":
            self.rate_drag = None
            if rate != self.settings["speech_rate"]:
                self.settings.update(speech_rate=rate)
                self.send_prefs()
        else:
            self.rate_drag = rate
        self.dirty.add("speed")

    def cycle_dim(self, *_args):
        current = self.settings["dim_after"]
        index = DIM_CHOICES.index(current) if current in DIM_CHOICES else 0
        self.settings.update(dim_after=DIM_CHOICES[(index + 1) % len(DIM_CHOICES)])
        self.dirty.add("dim")

    def drag_volume(self, x, y, phase):
        value = self.slider_value("volume", x)
        self.drag_value = None if phase == "up" else value
        self.status.set_volume(value)
        self.dirty |= {"volume", "status"}

    def drag_brightness(self, x, y, phase):
        if not self.status.backlight.writable():
            return
        self.brightness = max(5, self.slider_value("brightness", x))
        self.status.set_brightness(self.brightness)
        if phase == "up":
            self.settings.update(brightness=self.brightness)
        self.dirty.add("brightness")

    def open_keyboard(self, purpose):
        self.kb_purpose = purpose
        self.kb_text = ""
        self.kb_buffer = ""
        available = self.ime.available()
        self.kb_latin = purpose == "city" and not available
        self.kb_message = ("", DIM)
        if not available:
            if purpose == "wake":
                self.kb_message = ("拼音词典不可用，无法输入汉字", BAD)
            else:
                self.kb_message = ("拼音词典不可用，请用拼音字母输入城市名", WARN)
        self.update_candidates()
        self.screen = "keyboard"

    def close_keyboard(self, *_args):
        self.screen = "settings"

    def update_candidates(self):
        if self.kb_latin:
            self.candidates, self.consume = [], 0
        else:
            self.candidates, self.consume = self.ime.candidates(self.kb_buffer)
        self.kb_page = 0

    def pick(self, index):
        if index >= len(self.candidates):
            return
        limit = 8 if self.kb_purpose == "wake" else 30
        if len(self.kb_text) < limit:
            self.kb_text += self.candidates[index]
        self.kb_buffer = self.kb_buffer[self.consume:]
        self.update_candidates()
        self.dirty |= {"field", "cands"}

    def tap_cands(self, x, y):
        if self.kb_latin or not self.candidates:
            return
        if x < 80:
            self.kb_page = max(0, self.kb_page - 1)
        elif x >= 720:
            if (self.kb_page + 1) * 8 < len(self.candidates):
                self.kb_page += 1
        else:
            self.pick(self.kb_page * 8 + (x - 80) // 80)
            return
        self.dirty.add("cands")

    def press_key(self, name):
        self.kb_message = ("", DIM)
        if name == "back":
            if self.kb_buffer:
                self.kb_buffer = self.kb_buffer[:-1]
            else:
                self.kb_text = self.kb_text[:-1]
        elif name == "mode":
            if self.kb_purpose == "city" and self.ime.available():
                self.kb_latin = not self.kb_latin
                self.kb_text += self.kb_buffer
                self.kb_buffer = ""
                self.dirty.add("key:mode")
        elif name == "clear":
            self.kb_text = ""
            self.kb_buffer = ""
        elif name == "space":
            if self.kb_buffer and self.candidates:
                self.pick(0)
                return
            if self.kb_purpose == "city" and self.kb_text and len(self.kb_text) < 30:
                self.kb_text += " "
        elif name == "ok":
            self.submit_keyboard()
            return
        elif self.kb_latin:
            if len(self.kb_text) < 30:
                self.kb_text += name
        elif len(self.kb_buffer) < 16:
            self.kb_buffer += name
        self.update_candidates()
        self.dirty |= {"field", "cands", "message"}

    def submit_keyboard(self):
        self.dirty |= {"message", "field", "cands"}
        if self.kb_purpose == "wake":
            if self.kb_buffer:
                self.kb_message = ("请先在候选栏中选择汉字", WARN)
                return
            phrase = self.kb_text.strip()
            if not WAKE_PATTERN.fullmatch(phrase):
                self.kb_message = ("唤醒词需为 2–8 个汉字", BAD)
                return
            if phrase == self.wake_phrase:
                self.screen = "settings"
                return
            if not request_wake_word(phrase):
                self.kb_message = ("提交失败，请稍后重试", BAD)
                return
            self.wake_pending = (phrase, time.monotonic() + 90)
            self.set_notice("正在应用新的唤醒词，语音服务将短暂重启…", ACCENT, 90)
            self.screen = "settings"
            return
        query = (self.kb_text + self.kb_buffer).strip()
        if not query:
            self.kb_message = ("请输入城市名", WARN)
            return
        self.status.find_places(query)
        self.screen = "places"

    def select_place(self, index):
        places = self.status.places
        if index >= len(places):
            return
        place = places[index]
        self.settings.update(location={"name": place["name"], "lat": place["lat"], "lon": place["lon"]})
        self.status.refresh_weather()
        self.set_notice(f"天气城市已设为{place['name']}", GOOD, 6)
        self.screen = "settings"

    # ---- events -------------------------------------------------------

    def set_phase(self, phase, now):
        self.phase = phase
        self.dirty |= {"phase", "user", "answer", "stop", "hint"}

    def show_error(self, code, now, seconds):
        self.error_code = code
        self.error_until = now + seconds
        if self.layout == "conv":
            self.conv_until = max(self.conv_until, now + seconds)
        self.dirty |= {"hint", "phase"}

    def on_voice(self, message, now):
        event = message["e"]
        if event == "level":
            try:
                value = float(message.get("v", 0))
            except (TypeError, ValueError):
                value = 0.0
            self.level_target = max(self.level_target, min(1.0, max(0.0, value / 100) ** 0.5 * 1.3))
            return
        if not self.voice_online:
            self.voice_online = True
            self.dirty |= {"status", "hint", "info"}
        if event == "ready":
            wake = message.get("wake")
            if isinstance(wake, str) and wake:
                self.wake_phrase = wake[:16]
            wanted = self.settings["mic"]
            if bool(message.get("mic", 1)) != wanted:
                self.link.send("mic", on=int(wanted))
            self.mic_on = wanted
            self.send_prefs()
            phase = message.get("phase")
            if phase in ACTIVE:
                self.phase = phase
            self.full_redraw = True
            return
        if event == "cloud":
            state = message.get("s")
            if state in CLOUD_STATES:
                self.cloud = state
                self.dirty |= {"status", "info"}
            return
        if event == "mic":
            self.mic_on = bool(message.get("on"))
            self.dirty |= {"mic", "microw", "hint"}
            return
        if event == "music":
            self.music = message
            self.music_at = now
            self.dirty.add("player")
            return
        if event == "mlogin":
            self.on_login(message)
            return
        if event == "lyric":
            lrc = message.get("lrc")
            self.lyric_key = message.get("k")
            self.lyric_lines = parse_lrc(lrc) if isinstance(lrc, str) else []
            self.lyric_times = [line[0] for line in self.lyric_lines]
            self.lyric_index = -2
            return
        if event == "alarm":
            self.activity(now)
            self.screen = "home"
            self.user_text = "提醒"
            self.partial = ""
            self.answer = text_field(message)
            self.alarm_active = True
            self.conv_until = now + 120
            self.set_phase("idle", now)
            self.full_redraw = True
            return
        if event == "alarm_end":
            self.alarm_active = False
            self.dirty.add("stop")
            return
        if event == "sleep":
            self.sleep()
            return
        self.activity(now)
        if event == "wake":
            self.partial = self.user_text = self.answer = ""
            self.error_code = None
            self.set_phase("wake", now)
        elif event in ("listen", "think", "speak"):
            self.set_phase(event, now)
        elif event == "partial":
            self.partial = text_field(message)
            self.dirty.add("user")
        elif event == "user":
            self.user_text = text_field(message)
            self.partial = ""
            self.dirty.add("user")
        elif event == "delta":
            if self.phase != "answer":
                self.set_phase("answer", now)
            self.answer = (self.answer + text_field(message))[-4000:]
            self.dirty.add("answer")
        elif event == "idle":
            if self.phase in ACTIVE:
                hold = 12 if (self.answer or self.user_text) else 3
                self.conv_until = max(self.conv_until, now + hold)
                self.set_phase("idle", now)
        elif event == "nospeech":
            self.show_error("nospeech", now, 5)
        elif event == "error":
            code = message.get("code")
            if self.phase in ACTIVE:
                self.conv_until = now + 10
                self.set_phase("idle", now)
            self.show_error(code if code in ERRORS else "service", now, 10)

    def activity(self, now):
        self.last_activity = now
        self.asleep = False
        if self.dimmed:
            self.update_dim(now)

    def sleep(self):
        if not self.status.backlight.writable():
            return
        self.screen = "home"
        self.conv_until = 0.0
        self.asleep = self.dimmed = True
        self.status.set_brightness(0)

    def update_dim(self, now):
        if self.asleep:
            return
        limit = self.settings["dim_after"]
        backlight = self.status.backlight
        wanted = (
            limit > 0
            and self.screen == "home"
            and self.phase not in ACTIVE
            and now - self.last_activity > limit
            and backlight.writable()
        )
        if wanted != self.dimmed:
            self.dimmed = wanted
            self.status.set_brightness(max(1.0, self.brightness * 0.12) if wanted else self.brightness)

    def on_touch(self, kind, x, y, now):
        was_dimmed = self.dimmed
        self.activity(now)
        if kind == "down" and was_dimmed:
            self.swallow = True
            return
        if self.swallow:
            if kind == "up":
                self.swallow = False
            return
        if kind == "down":
            widget = None
            for candidate in reversed(self.widgets):
                if (candidate.tap or candidate.drag) and inside(candidate.rect, x, y):
                    widget = candidate
                    break
            self.active = widget
            if widget is None:
                return
            if widget.drag:
                widget.drag(x, y, "down")
            else:
                self.pressed = widget.name
                self.dirty.add(widget.name)
            return
        widget = self.active
        if widget is None:
            return
        if kind == "move":
            if widget.drag:
                widget.drag(x, y, "move")
            elif self.pressed == widget.name and not inside(widget.rect, x, y, 24):
                self.pressed = None
                self.dirty.add(widget.name)
            return
        self.active = None
        if widget.drag:
            widget.drag(x, y, "up")
            return
        if self.pressed == widget.name:
            self.pressed = None
            self.dirty.add(widget.name)
            widget.tap(x, y)

    def tick(self, now):
        clock = time.strftime("%H:%M")
        if clock != self.clock_key:
            self.clock_key = clock
            self.dirty |= {"clock", "date"}
        if now >= self.next_voice_check:
            self.next_voice_check = now + 2
            online = voice_running()
            if online != self.voice_online:
                self.voice_online = online
                if online:
                    self.link.send("hello")
                else:
                    self.cloud = "unknown"
                    self.music = {"s": "stopped"}
                    if self.phase in ACTIVE:
                        self.set_phase("idle", now)
                self.dirty |= {"status", "hint", "info"}
        if now >= self.next_config_check:
            self.next_config_check = now + 10
            configured = credentials_configured()
            if configured != self.configured:
                self.configured = configured
                self.dirty |= {"status", "hint", "info"}
        if self.status.version != self.status_version:
            self.status_version = self.status.version
            self.dirty |= {"status", "weather", "info", "volume", "city"}
        if self.error_code and now >= self.error_until:
            self.error_code = None
            self.dirty |= {"hint", "phase"}
        if self.notice and now >= self.notice[2]:
            self.notice = None
            self.dirty |= {"notice", "hint"}
        if self.wake_pending:
            phrase, deadline = self.wake_pending
            current = read_wake_phrase()
            if current == phrase:
                self.wake_pending = None
                self.wake_phrase = phrase
                self.set_notice(f"唤醒词已改为“{phrase}”", GOOD, 6)
                self.dirty |= {"wake", "hint"}
            elif now >= deadline:
                self.wake_pending = None
                self.set_notice("唤醒词更新失败，请稍后重试", BAD, 8)
                self.dirty.add("wake")
        if (self.layout or "").startswith("idle:music") and self.music.get("s") == "playing":
            self.dirty.add("player")
        logging_in = self.screen == "music" and (self.login or {}).get("s") in LOGIN_FLOW
        if self.screen != "home" and now - self.last_activity > (300 if logging_in else 120):
            self.cancel_login()
            self.screen = "home"
        self.update_dim(now)

    # ---- main loop ----------------------------------------------------

    def stop(self, *_args):
        self.running = False

    def ensure_touch(self, now):
        if self.touch.fd is not None or now < self.touch_retry:
            return
        try:
            self.touch.open()
            self.touch_warned = False
        except (OSError, RuntimeError) as error:
            self.touch_retry = now + 10
            if not self.touch_warned:
                self.touch_warned = True
                logging.warning("触摸屏不可用：%s", error)

    def step(self):
        now = time.monotonic()
        self.ensure_touch(now)
        fds = [self.link.fileno()]
        if self.touch.fd is not None:
            fds.append(self.touch.fd)
        animating = self.orb_rect is not None and not self.dimmed
        deadline = self.next_tick
        if animating:
            deadline = min(deadline, self.next_frame)
        if self.layout == "idle:music:lyrics":
            self.update_lyric(now)
            deadline = min(deadline, self.next_lyric)
        if self.dirty:
            deadline = min(deadline, self.next_flush)
        try:
            readable, _, _ = select.select(fds, [], [], max(0.0, deadline - now))
        except (OSError, ValueError):
            readable = []
        now = time.monotonic()
        if self.link.fileno() in readable:
            for message in self.link.receive():
                self.on_voice(message, now)
        if self.touch.fd is not None and self.touch.fd in readable:
            for kind, x, y in self.touch.read():
                self.on_touch(kind, x, y, now)
        if now >= self.next_tick:
            self.next_tick = now + 0.5
            self.tick(now)
        wanted = self.wanted_layout(now)
        if wanted != self.layout:
            self.build(wanted)
        if self.layout == "idle:music:lyrics":
            self.update_lyric(now)
        self.render(now)
        if self.orb_rect is not None and not self.dimmed and now >= self.next_frame:
            fast = self.phase in ACTIVE or now < self.error_until
            self.next_frame = now + (1 / 24 if fast else 1 / 12)
            self.animate(now)

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.console.enter()
        while self.running:
            try:
                self.step()
            except Exception:
                now = time.monotonic()
                if now - self.last_failure_log > 10:
                    self.last_failure_log = now
                    logging.exception("界面刷新失败")
                self.full_redraw = True
                time.sleep(0.5)

    def shutdown(self):
        try:
            self.fb.clear()
        finally:
            self.console.leave()
            self.touch.close()
            self.link.close()
            self.fb.close()


def main():
    logging.basicConfig(
        level=os.getenv("THINKSMART_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    app = App()
    try:
        app.run()
    finally:
        app.shutdown()


if __name__ == "__main__":
    main()
