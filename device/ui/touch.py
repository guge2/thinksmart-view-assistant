import fcntl
import glob
import logging
import os
import struct

EVENT = struct.Struct("@llHHi")
EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03
SYN_REPORT = 0
SYN_DROPPED = 3
BTN_TOUCH = 0x14A
ABS_X = 0x00
ABS_Y = 0x01
ABS_MT_SLOT = 0x2F
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_TRACKING_ID = 0x39
NAME_HINTS = (
    "ft5x06",
    "ft5406",
    "ft5426",
    "edt-ft5",
    "edt_ft5",
    "focaltech",
    "fts_ts",
    "touchscreen",
)
FALLBACK_DEVICE = "/dev/input/event1"


def eviocgabs(axis):
    return 0x80184540 + axis


def env_flag(name):
    value = os.getenv(name, "").strip().lower()
    if value in ("1", "yes", "true"):
        return True
    if value in ("0", "no", "false"):
        return False
    return None


def find_touch_device():
    explicit = os.getenv("THINKSMART_UI_TOUCH_DEVICE", "").strip()
    if explicit:
        return explicit
    wanted = os.getenv("THINKSMART_UI_TOUCH_NAME", "").strip().lower()
    hints = (wanted,) if wanted else NAME_HINTS
    for node in sorted(glob.glob("/sys/class/input/event*")):
        try:
            with open(os.path.join(node, "device", "name"), "r") as source:
                name = source.read().strip().lower()
        except OSError:
            continue
        if any(hint in name for hint in hints):
            return "/dev/input/" + os.path.basename(node)
    return FALLBACK_DEVICE


def absinfo(fd, axis):
    buffer = bytearray(24)
    try:
        fcntl.ioctl(fd, eviocgabs(axis), buffer, True)
    except OSError:
        return None
    _value, minimum, maximum = struct.unpack_from("=iii", buffer)
    return (minimum, maximum) if maximum > minimum else None


class Touchscreen:
    """Single-pointer reader for MT protocol A/B and legacy ABS touch panels."""

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.fd = None

    def fileno(self):
        return self.fd

    def open(self):
        path = find_touch_device()
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        x_axis = absinfo(fd, ABS_MT_POSITION_X)
        y_axis = absinfo(fd, ABS_MT_POSITION_Y)
        self.mt = bool(x_axis and y_axis)
        if not self.mt:
            x_axis = absinfo(fd, ABS_X)
            y_axis = absinfo(fd, ABS_Y)
        if not x_axis or not y_axis:
            os.close(fd)
            raise RuntimeError(f"{path} has no absolute position axes")
        self.x_axis = x_axis
        self.y_axis = y_axis
        x_span = x_axis[1] - x_axis[0]
        y_span = y_axis[1] - y_axis[0]
        swap = env_flag("THINKSMART_UI_TOUCH_SWAP_XY")
        if swap is None:
            swap = x_span != y_span and (x_span > y_span) != (self.width > self.height)
        self.swap = swap
        self.invert_x = bool(env_flag("THINKSMART_UI_TOUCH_INVERT_X"))
        self.invert_y = bool(env_flag("THINKSMART_UI_TOUCH_INVERT_Y"))
        self.slot = 0
        self.contact = False
        self.tracking_seen = False
        self.button = False
        self.raw_x = x_axis[0]
        self.raw_y = y_axis[0]
        self.was_down = False
        self.last = (0, 0)
        self.dropped = False
        self.fd = fd
        logging.info(
            "触摸屏 %s：X%s Y%s 多点=%s 交换XY=%s",
            path, x_axis, y_axis, self.mt, self.swap,
        )

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def map(self):
        nx = (self.raw_x - self.x_axis[0]) / (self.x_axis[1] - self.x_axis[0])
        ny = (self.raw_y - self.y_axis[0]) / (self.y_axis[1] - self.y_axis[0])
        if self.swap:
            nx, ny = ny, nx
        if self.invert_x:
            nx = 1.0 - nx
        if self.invert_y:
            ny = 1.0 - ny
        nx = min(1.0, max(0.0, nx))
        ny = min(1.0, max(0.0, ny))
        return int(nx * (self.width - 1)), int(ny * (self.height - 1))

    def read(self):
        events = []
        if self.fd is None:
            return events
        try:
            data = os.read(self.fd, EVENT.size * 64)
        except BlockingIOError:
            return events
        except OSError as error:
            logging.warning("触摸屏读取失败：%s", error)
            self.close()
            return events
        for offset in range(0, len(data) - EVENT.size + 1, EVENT.size):
            _sec, _usec, kind, code, value = EVENT.unpack_from(data, offset)
            if kind == EV_ABS:
                if self.mt:
                    if code == ABS_MT_SLOT:
                        self.slot = value
                    elif self.slot == 0:
                        if code == ABS_MT_TRACKING_ID:
                            self.tracking_seen = True
                            self.contact = value >= 0
                        elif code == ABS_MT_POSITION_X:
                            self.raw_x = value
                        elif code == ABS_MT_POSITION_Y:
                            self.raw_y = value
                elif code == ABS_X:
                    self.raw_x = value
                elif code == ABS_Y:
                    self.raw_y = value
            elif kind == EV_KEY and code == BTN_TOUCH:
                self.button = bool(value)
            elif kind == EV_SYN:
                if code == SYN_DROPPED:
                    self.dropped = True
                elif code == SYN_REPORT:
                    if self.dropped:
                        self.dropped = False
                        continue
                    self.report(events)
        return events

    def report(self, events):
        down = self.contact if self.tracking_seen else self.button
        if down:
            point = self.map()
            if not self.was_down:
                events.append(("down",) + point)
            elif point != self.last:
                events.append(("move",) + point)
            self.last = point
        elif self.was_down:
            events.append(("up",) + self.last)
        self.was_down = down
