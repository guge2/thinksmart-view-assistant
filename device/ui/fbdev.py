import ctypes
import fcntl
import logging
import mmap
import os
import platform
import struct
import termios

from PIL import Image

# Position-independent ARM64 routine: blit_im32_bgrx(rows, sh, dst, dst_stride, x0, y0, x1, y1, rotate)
# Reads PIL Image's internal 32-bit RGBX row pointers directly, converts RGBX -> BGRX via NEON/bswap,
# and writes sequential bursts into the mmap'd framebuffer (0.13ms partial, 2.99ms full screen on rot=0).
_ARM64_BLIT_BGRX_HEX = (
    "fe0f1af8fc6f01a9fa6702a9f85f03a9f65704a9f44f05a9ff4310d1e87344b9"
    "a8040034ef400051e10700f9ff01056be50f02f9ca0a0054e80f42f9e800084b"
    "1f0500714b090054df00046b0d090054e90740f9cb7c4093ec040051297d7e93"
    "8924239b6a7c40932dc927cb897c40934d000d8bee0308aaef030daaf0030c2a"
    "11d870f8ce0500f110060051317a69b8310ac05a317e0853f14500b821ffff54"
    "29050091ad010a8b3f010beb41feff542f000014bf00076baa050054897c7e93"
    "ae24239bc800044b897c40930a7d4093ab7c40936c7c4093ed7c40934e000e8b"
    "050000146b050091ce010c8b7f010debe00300540f786bf81f110071ef09098b"
    "aa000054f0031faa1f02086b0b020054f5ffff17f2031faaf0030faaf1030eaa"
    "0006c13c412200913f000aeb521200910008206e0004386f2006813c2dffff54"
    "f003122a1f02086beafcff54f17970b8310ac05a317e0853d17930b810060091"
    "1f020aeb4bffff54dfffff17ff431091f44f45a9f65744a9f85f43a9fa6742a9"
    "fc6f41a9fe0746f8c0035fd64ec82fcbf10305aa8c4000112900074bf003032a"
    "e8031f2ad2c9218b8a7c031b6b6c1c538c7d4093cd7c4093e103092a0e7e4093"
    "f37d40932f7e4093f0040051510201d1f243009109000014744200d1e703132a"
    "080500119f020feb1042005131020191f30314aa2bf2ff549f01066bf403042a"
    "8c0500543410080bf6030a2af703042afa030caa55c8348bf97e7e93f8031faa"
    "f4031aaafa430091fc03382afb031faafe031aaafc001c0b1cd87cf89c03198b"
    "896b7bb87b1300917f0301f12909c05a297d0853c90704b841ffff5418070091"
    "5a1300911f4300f101feff54f8031faaf903162a4902188bbac2398b18030191"
    "200540ad1f0310f13903030b400700ad220141ad420301ade1feff549a420091"
    "f7420011d6020b0b5f030deb6dfbff549f02066b2af9ff54f503142ac745349b"
    "b47e4093f5031faaf603102a09d876f8d6060051297974b82909c05a297d0853"
    "e96835b8b5120091bf0201f101ffff5494060091e7000e8b9f020deb41feff54"
    "b6ffff17"
)


def _load_arm64_blitter():
    if platform.machine() != "aarch64":
        return None, None
    try:
        code = bytes.fromhex(_ARM64_BLIT_BGRX_HEX)
        buf = mmap.mmap(-1, 4096, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
        buf.write(code)
        addr = ctypes.addressof(ctypes.c_char.from_buffer(buf))
        fn = ctypes.CFUNCTYPE(
            None,
            ctypes.c_void_p, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        )(addr)
        return buf, fn
    except Exception:
        return None, None


_JIT_BUF, _JIT_BLIT_BGRX = _load_arm64_blitter()

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
        self._fast_blit = (
            _JIT_BLIT_BGRX is not None
            and bpp == 32
            and (red, blue) == (16, 0)
            and self.rotate in (0, 90)
        )
        self._mm_addr = (
            ctypes.addressof(ctypes.c_char.from_buffer(self.mm)) + self.base
            if self._fast_blit
            else 0
        )
        logging.info(
            "帧缓冲 %s：%dx%d（虚拟 %dx%d）%dbpp stride=%d 旋转=%d%s",
            path, self.xres, self.yres, xvirt, yvirt, bpp, stride, self.rotate,
            " (ARM64 零拷贝加速)" if self._fast_blit else "",
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
        if self._fast_blit and image.mode == "RGB" and image.size == (self.width, self.height):
            rows_ptr = dict(image.im.unsafe_ptrs).get("image32")
            if rows_ptr:
                _JIT_BLIT_BGRX(rows_ptr, self.height, self._mm_addr, self.stride, x0, y0, x1, y1, self.rotate)
                if self.pan:
                    try:
                        fcntl.ioctl(self.fd, FBIOPAN_DISPLAY, self.var)
                    except OSError:
                        self.pan = False
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
