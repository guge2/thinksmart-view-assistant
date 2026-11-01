import fcntl
import logging
import mmap
import os
import struct
import termios

from PIL import Image

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
FBIOPAN_DISPLAY = 0x4606
KDSETMODE = 0x4B3A
KD_TEXT = 0x00
KD_GRAPHICS = 0x01
VT_ACTIVATE = 0x5606
FIX_FORMAT = "@16sLIIIIHHHI"
TRANSPOSE = {
    90: Image.Transpose.ROTATE_270,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}


def sysfs_int(device, attribute):
    try:
        with open(f"/sys/class/graphics/{device}/{attribute}", "r") as source:
            return int(source.read().strip().split(",")[0])
    except (OSError, ValueError):
        return 0


class Framebuffer:
    def __init__(self, path="/dev/fb0"):
        self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        try:
            self.setup(path)
        except Exception:
            os.close(self.fd)
            raise

    def setup(self, path):
        device = os.path.basename(path)
        self.var = bytearray(160)
        fcntl.ioctl(self.fd, FBIOGET_VSCREENINFO, self.var, True)
        fields = struct.unpack_from("=20I", self.var)
        self.xres, self.yres, xvirt, yvirt, xoff, yoff, bpp = fields[:7]
        red, blue = fields[8], fields[14]
        smem_len = 0
        stride = 0
        try:
            fix = bytearray(128)
            fcntl.ioctl(self.fd, FBIOGET_FSCREENINFO, fix, True)
            values = struct.unpack_from(FIX_FORMAT, fix)
            smem_len, stride = values[2], values[9]
        except OSError:
            pass
        if not self.xres or not self.yres:
            raise RuntimeError("framebuffer reports no resolution")
        if not bpp:
            bpp = sysfs_int(device, "bits_per_pixel")
        self.bytespp = (bpp + 7) // 8
        stride = stride or sysfs_int(device, "stride") or self.xres * self.bytespp
        yvirt = max(yvirt, self.yres)
        length = smem_len or stride * yvirt
        if stride < self.xres * self.bytespp or length < stride * (yoff + self.yres):
            raise RuntimeError("framebuffer geometry is inconsistent")
        self.stride = stride
        self.base = yoff * stride + xoff * self.bytespp
        self.mm = mmap.mmap(
            self.fd,
            length,
            mmap.MAP_SHARED,
            mmap.PROT_READ | mmap.PROT_WRITE,
        )
        self.encode = self.pick_encoder(bpp, red, blue)
        self.pan = os.getenv("THINKSMART_UI_FB_PAN") == "1"
        rotate = os.getenv("THINKSMART_UI_ROTATE", "").strip()
        if rotate in ("0", "90", "180", "270"):
            self.rotate = int(rotate)
        else:
            self.rotate = 90 if self.xres > self.yres else 0
        if self.rotate in (90, 270):
            self.width, self.height = self.yres, self.xres
        else:
            self.width, self.height = self.xres, self.yres
        logging.info(
            "帧缓冲 %s：%dx%d（虚拟 %dx%d）%dbpp stride=%d 旋转=%d",
            path, self.xres, self.yres, xvirt, yvirt, bpp, stride, self.rotate,
        )

    @staticmethod
    def pick_encoder(bpp, red, blue):
        if bpp == 32:
            raw = {
                (16, 0): "BGRX",
                (0, 16): "RGBX",
                (24, 8): "XBGR",
                (8, 24): "XRGB",
            }.get((red, blue), "BGRX")
            return lambda image: image.tobytes("raw", raw)
        if bpp == 24:
            raw = "RGB" if red == 0 else "BGR"
            return lambda image: image.tobytes("raw", raw)
        if bpp == 16:
            return lambda image: image.convert("BGR;16").tobytes()
        raise RuntimeError(f"unsupported framebuffer depth {bpp}")

    def physical_rect(self, rect):
        x0, y0, x1, y1 = rect
        w, h = self.width, self.height
        if self.rotate == 90:
            return h - y1, x0, h - y0, x1
        if self.rotate == 180:
            return w - x1, h - y1, w - x0, h - y0
        if self.rotate == 270:
            return y0, w - x1, y1, w - x0
        return rect

    def blit(self, image, rect):
        x0 = max(0, rect[0])
        y0 = max(0, rect[1])
        x1 = min(self.width, rect[2])
        y1 = min(self.height, rect[3])
        if x1 <= x0 or y1 <= y0:
            return
        if (x0, y0, x1, y1) == (0, 0, self.width, self.height):
            region = image
        else:
            region = image.crop((x0, y0, x1, y1))
        if self.rotate:
            region = region.transpose(TRANSPOSE[self.rotate])
        px0, py0, px1, py1 = self.physical_rect((x0, y0, x1, y1))
        data = self.encode(region)
        row = (px1 - px0) * self.bytespp
        offset = self.base + py0 * self.stride + px0 * self.bytespp
        if row == self.stride:
            self.mm[offset:offset + len(data)] = data
        else:
            view = memoryview(data)
            for start in range(0, len(data), row):
                self.mm[offset:offset + row] = view[start:start + row]
                offset += self.stride
        if self.pan:
            try:
                fcntl.ioctl(self.fd, FBIOPAN_DISPLAY, self.var)
            except OSError:
                self.pan = False

    def clear(self):
        try:
            size = self.stride * self.yres
            self.mm[self.base:self.base + size] = bytes(size)
        except (ValueError, OSError):
            pass

    def close(self):
        try:
            self.mm.close()
        finally:
            os.close(self.fd)


class Console:
    """Keeps fbcon off the panel while the display owns its VT."""

    def __init__(self):
        self.fd = 0 if os.isatty(0) else None
        self.saved = None
        self.graphics = False

    def enter(self):
        if self.fd is None:
            logging.warning("未连接控制终端，控制台文字可能覆盖界面")
            return
        try:
            name = os.ttyname(self.fd)
            if name.startswith("/dev/tty") and name[8:].isdigit():
                fcntl.ioctl(self.fd, VT_ACTIVATE, int(name[8:]))
        except OSError as error:
            logging.warning("无法切换虚拟终端：%s", error)
        try:
            self.saved = termios.tcgetattr(self.fd)
            mode = list(self.saved)
            mode[3] &= ~(termios.ECHO | termios.ICANON)
            termios.tcsetattr(self.fd, termios.TCSANOW, mode)
        except termios.error:
            self.saved = None
        try:
            os.write(self.fd, b"\033[?25l\033[2J")
        except OSError:
            pass
        try:
            fcntl.ioctl(self.fd, KDSETMODE, KD_GRAPHICS)
            self.graphics = True
        except OSError as error:
            logging.warning("无法切换到图形模式：%s", error)

    def leave(self):
        if self.fd is None:
            return
        if self.graphics:
            try:
                fcntl.ioctl(self.fd, KDSETMODE, KD_TEXT)
            except OSError:
                pass
            self.graphics = False
        if self.saved is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSANOW, self.saved)
            except termios.error:
                pass
        try:
            os.write(self.fd, b"\033[?25h\033[2J\033[H")
        except OSError:
            pass
