"""Screen size, colours and the touch widget shared by the display's pages."""

W, H = 800, 1280
# Flat warm black, warm white type, and a single signal orange for what is live right now.
BG = (12, 12, 13)
CARD = (27, 27, 29)
CARD_PRESSED = (48, 47, 50)
LINE = (38, 38, 40)
TEXT = (242, 240, 236)
DIM = (146, 143, 138)
FAINT = (86, 85, 82)
ACCENT = (255, 110, 60)
ACCENT_PRESSED = (214, 88, 46)
ON_ACCENT = (24, 12, 6)
GOOD = TEXT
WARN = (255, 170, 90)
BAD = (255, 90, 80)
WHITE = (255, 255, 255)


class Widget:
    __slots__ = ("name", "rect", "paint", "tap", "drag")

    def __init__(self, name, rect, paint, tap=None, drag=None):
        self.name = name
        self.rect = rect
        self.paint = paint
        self.tap = tap
        self.drag = drag
