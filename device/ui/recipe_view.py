"""The kitchen recipe page: one big step at a time, readable from across the counter, with big buttons
for hands that happen to be clean. A step that waits shows one timer button: ▶ and its minutes to
start, the countdown and ■ to stop. The voice service owns the recipe; taps send it commands and the
page follows the state it sends back."""

import time

from theme import ACCENT, ACCENT_PRESSED, BAD, CARD, CARD_PRESSED, DIM, FAINT, LINE, ON_ACCENT, TEXT, WARN, Widget

STEP_SIZES = (60, 52, 44, 38)
CONFIRM_SECONDS = 3
CHIP_HEIGHT = 104
NAV_TOP, NAV_BOTTOM = 1140, 1256
PLAYER_HEIGHT = 110


def recipe_timers(recipe, alarm_items):
    """This dish's running timers as {"step", "seconds", "id"}, soonest first."""
    prefix = f"{recipe.get('title')}·第"
    found = []
    for item in alarm_items:
        label = item.get("label") or ""
        if item.get("kind") == "timer" and label.startswith(prefix) and isinstance(item.get("due"), (int, float)):
            step = label[len(prefix):].rstrip("步")
            found.append({"step": int(step) if step.isdigit() else 0, "id": item.get("id"),
                          "seconds": max(0, int(item["due"] - time.time()))})
    return sorted(found, key=lambda timer: timer["seconds"])


def clock(seconds):
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class RecipeView:
    def __init__(self, app):
        self.app = app
        self.exit_armed_until = 0.0

    @property
    def recipe(self):
        return self.app.recipe or {}

    def showing_ingredients(self):
        return self.recipe.get("view") == "ingredients" or self.recipe.get("index", -1) < 0

    def current_step(self):
        steps, index = self.recipe.get("steps") or [], self.recipe.get("index", -1)
        return steps[index] if 0 <= index < len(steps) and not self.showing_ingredients() else None

    def timers(self):
        """(the current step's running timer or None, the other steps' running timers)."""
        timers = recipe_timers(self.recipe, self.app.alarm_items)
        number = self.recipe.get("index", -1) + 1
        mine = next((t for t in timers if t["step"] == number), None) if self.current_step() else None
        return mine, [t for t in timers if t is not mine][:3]

    def layout(self, music):
        mine, others = self.timers()
        view = "ingredients" if self.showing_ingredients() else "steps"
        ids = ",".join(t["id"] or "" for t in ([mine] if mine else []) + others)
        return f"recipe:{view}:{self.recipe.get('index')}:{ids}" + (":music" if music else "")

    def widgets(self):
        app = self.app
        music = app.layout_has("music")
        _mine, others = self.timers()
        bottom = NAV_TOP - 20
        if music:
            bottom -= PLAYER_HEIGHT + 16
        if others:
            bottom -= CHIP_HEIGHT + 16
        body = (40, 252, 760, bottom)
        widgets = [
            app.status_widget(),
            Widget("recipe_title", (40, 60, 580, 200), self.paint_title),
            Widget("recipe_exit", (596, 72, 760, 160), self.paint_exit, tap=self.tap_exit),
            Widget("recipe_progress", (40, 214, 760, 230), self.paint_progress),
            Widget("recipe_body", body, self.paint_body),
        ]
        step = self.current_step()
        if step and step.get("minutes"):
            widgets.append(Widget("recipe_step_timer", self.step_layout(body)[3],
                                  lambda d, r, p: self.paint_chip(d, r, p, self.timers()[0], step["minutes"]),
                                  tap=self.tap_step_timer))
        top = bottom + 16
        if others:
            width = (720 - 16 * (len(others) - 1)) / len(others)
            for number, timer in enumerate(others):
                left = round(40 + number * (width + 16))
                widgets.append(Widget(f"recipe_timer:{number}", (left, top, round(left + width), top + CHIP_HEIGHT),
                                      lambda d, r, p, i=number: self.paint_chip(d, r, p, self.other_timer(i)),
                                      tap=lambda x, y, ident=timer["id"]: self.cancel(ident)))
            top += CHIP_HEIGHT + 16
        if music:
            widgets.append(Widget("player", (40, top, 760, top + PLAYER_HEIGHT), app.paint_player, tap=app.tap_player))
        widgets += [
            Widget("recipe_prev", (40, NAV_TOP, 262, NAV_BOTTOM), self.button("上一步"),
                   tap=lambda x, y: self.send("previous")),
            Widget("recipe_toggle", (282, NAV_TOP, 518, NAV_BOTTOM),
                   self.button(lambda: "步骤" if self.showing_ingredients() else "食材"), tap=self.tap_toggle),
            Widget("recipe_next", (538, NAV_TOP, 760, NAV_BOTTOM), self.button("下一步", primary=True),
                   tap=lambda x, y: self.send("next")),
        ]
        return widgets

    def other_timer(self, number):
        others = self.timers()[1]
        return others[number] if number < len(others) else None

    def chip_names(self):
        return {"recipe_step_timer"} | {f"recipe_timer:{i}" for i in range(3)}

    # Taps

    def send(self, action):
        self.app.link.send("recipe", a=action)

    def cancel(self, ident):
        self.app.link.send("alarm_cmd", a="cancel", id=ident)

    def tap_toggle(self, *_args):
        if not self.showing_ingredients():
            self.send("ingredients")
        else:
            self.send("repeat" if self.recipe.get("index", -1) >= 0 else "next")

    def tap_step_timer(self, *_args):
        running = self.timers()[0]
        if running:
            self.cancel(running["id"])
        else:
            self.send("timer")

    def tap_exit(self, *_args):
        # Two taps, so a wet elbow does not end the recipe.
        now = time.monotonic()
        if now < self.exit_armed_until:
            self.exit_armed_until = 0.0
            self.send("stop")
        else:
            self.exit_armed_until = now + CONFIRM_SECONDS
        self.app.dirty.add("recipe_exit")

    def tick(self, now):
        """Repaints what changes every tick: the countdowns and an expiring exit confirmation."""
        dirty = self.chip_names()
        if self.exit_armed_until and now >= self.exit_armed_until:
            self.exit_armed_until = 0.0
            dirty.add("recipe_exit")
        return dirty

    # Painting

    def button(self, label, primary=False):
        def paint(d, rect, pressed):
            if primary:
                fill = ACCENT_PRESSED if pressed else ACCENT
            else:
                fill = CARD_PRESSED if pressed else CARD
            d.rounded_rectangle(rect, radius=(rect[3] - rect[1]) // 2, fill=fill)
            d.text(((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2), label() if callable(label) else label,
                   font=self.app.fonts.ui(42), fill=ON_ACCENT if primary else TEXT, anchor="mm")
        return paint

    def paint_title(self, d, rect, pressed):
        app, recipe = self.app, self.recipe
        x0, y0, x1, _ = rect
        font = app.fonts.ui(60)
        d.text((x0 + 8, y0 + 48), app.fit(recipe.get("title") or "", font, x1 - x0 - 16), font=font, fill=TEXT, anchor="lm")
        steps = recipe.get("steps") or []
        index = recipe.get("index", -1)
        where = f"第 {index + 1} / {len(steps)} 步" if self.current_step() else f"食材 · 共 {len(steps)} 步"
        d.text((x0 + 8, y0 + 116), f"{where}   {time.strftime('%H:%M')}", font=app.fonts.ui(34), fill=DIM, anchor="lm")

    def paint_exit(self, d, rect, pressed):
        armed = time.monotonic() < self.exit_armed_until
        d.rounded_rectangle(rect, radius=(rect[3] - rect[1]) // 2,
                            fill=BAD if armed else (CARD_PRESSED if pressed else CARD))
        d.text(((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2), "再点退出" if armed else "退出",
               font=self.app.fonts.ui(30 if armed else 34), fill=ON_ACCENT if armed else DIM, anchor="mm")

    def paint_progress(self, d, rect, pressed):
        steps = len(self.recipe.get("steps") or [])
        index = self.recipe.get("index", -1)
        if not steps:
            return
        x0, y0, x1, y1 = rect
        gap = 8
        width = (x1 - x0 - gap * (steps - 1)) / steps
        for step in range(steps):
            left = x0 + step * (width + gap)
            d.rounded_rectangle((left, y0, left + width, y1), radius=(y1 - y0) // 2,
                                fill=ACCENT if step <= index else LINE)

    def paint_body(self, d, rect, pressed):
        if self.showing_ingredients():
            self.paint_ingredients(d, rect)
        elif self.current_step():
            self.paint_step(d, rect)

    def preview(self):
        steps, index = self.recipe.get("steps") or [], self.recipe.get("index", -1)
        return steps[index + 1]["text"] if 0 <= index and index + 1 < len(steps) else ""

    def step_layout(self, rect):
        """(font, lines, line height, timer button rect) of the current step in the body rect."""
        app = self.app
        x0, y0, x1, y1 = rect
        step = self.current_step()
        reserved = (CHIP_HEIGHT + 32 if step.get("minutes") else 0) + (96 if self.preview() else 0)
        for size in STEP_SIZES:
            font = app.fonts.ui(size)
            lines = app.wrap(font, step["text"], x1 - x0 - 16)
            line_height = round(size * 1.4)
            if len(lines) * line_height <= y1 - y0 - reserved or size == STEP_SIZES[-1]:
                break
        lines = lines[:max(1, (y1 - y0 - reserved) // line_height)]
        top = y0 + len(lines) * line_height + 24
        return font, lines, line_height, (x0 + 8, top, x1 - 8, top + CHIP_HEIGHT)

    def paint_step(self, d, rect):
        app = self.app
        x0, y0, x1, y1 = rect
        font, lines, line_height, _button = self.step_layout(rect)
        for number, line in enumerate(lines):
            d.text((x0 + 8, y0 + line_height // 2 + number * line_height), line, font=font, fill=TEXT, anchor="lm")
        preview = self.preview()
        if preview:
            small = app.fonts.ui(32)
            d.line((x0 + 8, y1 - 84, x1 - 8, y1 - 84), fill=LINE, width=2)
            d.text((x0 + 8, y1 - 40), app.fit(f"下一步：{preview}", small, x1 - x0 - 16), font=small, fill=FAINT, anchor="lm")

    def paint_ingredients(self, d, rect):
        app = self.app
        x0, y0, x1, y1 = rect
        items = self.recipe.get("ingredients") or ["这道菜没有列出食材"]
        font = app.fonts.ui(42)
        columns = 2 if len(items) > 9 else 1
        column_width = (x1 - x0) // columns
        rows = (len(items) + columns - 1) // columns
        line_height = min(84, max(52, (y1 - y0) // max(1, rows)))
        for number, item in enumerate(items):
            column, row = divmod(number, rows)
            x = x0 + 8 + column * column_width
            y = y0 + line_height // 2 + row * line_height
            if y > y1 - line_height // 2:
                break
            d.ellipse((x, y - 7, x + 14, y + 7), fill=ACCENT)
            d.text((x + 32, y), app.fit(item, font, column_width - 48), font=font, fill=TEXT, anchor="lm")

    def paint_chip(self, d, rect, pressed, timer, minutes=None):
        """The current step's timer button (minutes given): ▶ and the minutes to start, the countdown
        and ■ to stop. Another step's running timer also shows its step number."""
        x0, y0, x1, y1 = rect
        radius = (y1 - y0) // 2
        font = self.app.fonts.digits(46 if x1 - x0 > 300 else 38)
        if timer is None:
            if not minutes:
                return
            d.rounded_rectangle(rect, radius=radius, outline=WARN, width=3, fill=CARD_PRESSED if pressed else None)
            self.icon_and_text(d, rect, "play", clock(minutes * 60), WARN, font)
            return
        urgent = timer["seconds"] < 60
        fill = (ACCENT_PRESSED if pressed else ACCENT) if urgent else (CARD_PRESSED if pressed else CARD)
        label = clock(timer["seconds"])
        if minutes is None and timer["step"]:
            label = f"{timer['step']}  {label}"
        self.icon_and_text(d, rect, "stop", label, ON_ACCENT if urgent else TEXT, font, fill)

    def icon_and_text(self, d, rect, icon, text, color, font, fill=None):
        x0, y0, x1, y1 = rect
        if fill:
            d.rounded_rectangle(rect, radius=(y1 - y0) // 2, fill=fill)
        cy, size = (y0 + y1) // 2, 30
        x = (x0 + x1 - size - 24 - font.getlength(text)) / 2
        if icon == "play":
            d.polygon([(x, cy - size // 2 - 2), (x, cy + size // 2 + 2), (x + size, cy)], fill=color)
        else:
            d.rounded_rectangle((x + 2, cy - size // 2 + 2, x + size - 2, cy + size // 2 - 2), radius=4, fill=color)
        d.text((x + size + 24, cy), text, font=font, fill=color, anchor="lm")
