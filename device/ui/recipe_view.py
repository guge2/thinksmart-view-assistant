"""The kitchen recipe page: one big step at a time, readable from across the counter, with the dish's
running timers and buttons for hands that happen to be clean. The voice service owns the recipe; taps
send it commands and the page follows the state it sends back."""

import time

from theme import ACCENT, BAD, CARD, CARD_PRESSED, DIM, FAINT, LINE, ON_ACCENT, TEXT, WARN, Widget

STEP_SIZES = (56, 48, 40, 34)
CONFIRM_SECONDS = 3
CHIP_HEIGHT = 68


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
        # What a first tap armed and until when: "exit", or the id of a timer to cancel.
        self.armed, self.armed_until = None, 0.0

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
        recipe = self.recipe
        mine, others = self.timers()
        view = "ingredients" if self.showing_ingredients() else "steps"
        ids = ",".join(t["id"] or "" for t in ([mine] if mine else []) + others)
        return f"recipe:{view}:{recipe.get('index')}:{ids}" + (":music" if music else "")

    def widgets(self):
        app = self.app
        music = app.layout_has("music")
        mine, others = self.timers()
        body_bottom = 1150 - (110 if music else 0) - (CHIP_HEIGHT + 16 if others else 0)
        body = (40, 236, 760, body_bottom)
        widgets = [
            app.status_widget(),
            Widget("recipe_title", (40, 64, 600, 196), self.paint_title),
            Widget("recipe_exit", (620, 80, 760, 144), self.paint_exit, tap=lambda x, y: self.confirm("exit", "stop")),
            Widget("recipe_progress", (40, 204, 760, 216), self.paint_progress),
            Widget("recipe_body", body, self.paint_body),
        ]
        step = self.current_step()
        if step and step.get("minutes"):
            chip = self.step_layout(body)[3]
            widgets.append(Widget("recipe_step_timer", chip, lambda d, r, p: self.paint_chip(d, r, p, self.timers()[0]),
                                  tap=self.tap_step_timer))
        top = body_bottom + 8
        if others:
            width = (720 - 16 * (len(others) - 1)) / len(others)
            for number, timer in enumerate(others):
                left = round(40 + number * (width + 16))
                widgets.append(Widget(f"recipe_timer:{number}", (left, top, round(left + width), top + CHIP_HEIGHT),
                                      lambda d, r, p, i=number: self.paint_chip(d, r, p, self.other_timer(i)),
                                      tap=lambda x, y, ident=timer["id"]: self.confirm(ident)))
            top += CHIP_HEIGHT + 16
        if music:
            widgets.append(Widget("player", (40, top, 760, top + 110), app.paint_player, tap=app.tap_player))
        widgets += [
            Widget("recipe_prev", (40, 1170, 250, 1250),
                   lambda d, r, p: app.paint_button(d, r, p, "上一步"), tap=lambda x, y: self.send("previous")),
            Widget("recipe_toggle", (270, 1170, 530, 1250),
                   lambda d, r, p: app.paint_button(d, r, p, "看步骤" if self.showing_ingredients() else "食材"),
                   tap=self.tap_toggle),
            Widget("recipe_next", (550, 1170, 760, 1250),
                   lambda d, r, p: app.paint_button(d, r, p, "下一步", primary=True), tap=lambda x, y: self.send("next")),
        ]
        return widgets

    def other_timer(self, number):
        others = self.timers()[1]
        return others[number] if number < len(others) else None

    def send(self, action):
        self.app.link.send("recipe", a=action)

    def tap_toggle(self, *_args):
        if not self.showing_ingredients():
            self.send("ingredients")
        else:
            self.send("repeat" if self.recipe.get("index", -1) >= 0 else "next")

    def tap_step_timer(self, *_args):
        running = self.timers()[0]
        if running:
            self.confirm(running["id"])
        else:
            self.send("timer")

    def confirm(self, target, action=None):
        """Two taps, so a wet elbow neither ends the recipe nor cancels a timer."""
        now = time.monotonic()
        if self.armed == target and now < self.armed_until:
            self.armed = None
            if target == "exit":
                self.send(action)
            else:
                self.app.link.send("alarm_cmd", a="cancel", id=target)
        else:
            self.armed, self.armed_until = target, now + CONFIRM_SECONDS
        self.app.dirty |= self.chip_names() | {"recipe_exit"}

    def armed_for(self, target):
        return self.armed == target and time.monotonic() < self.armed_until

    def chip_names(self):
        return {"recipe_step_timer"} | {f"recipe_timer:{i}" for i in range(3)}

    def tick(self, now):
        """Repaints what changes every tick: the countdowns and an expiring confirmation."""
        dirty = self.chip_names()
        if self.armed and now >= self.armed_until:
            self.armed = None
            dirty.add("recipe_exit")
        return dirty

    def paint_title(self, d, rect, pressed):
        app, recipe = self.app, self.recipe
        x0, y0, x1, _ = rect
        font = app.fonts.ui(52)
        d.text((x0 + 8, y0 + 44), app.fit(recipe.get("title") or "", font, x1 - x0 - 16), font=font, fill=TEXT, anchor="lm")
        steps = recipe.get("steps") or []
        index = recipe.get("index", -1)
        where = f"第 {index + 1} / {len(steps)} 步" if index >= 0 and not self.showing_ingredients() else f"食材 · 共 {len(steps)} 步"
        small = app.fonts.ui(30)
        d.text((x0 + 8, y0 + 104), f"{where}   {time.strftime('%H:%M')}", font=small, fill=DIM, anchor="lm")

    def paint_exit(self, d, rect, pressed):
        armed = self.armed_for("exit")
        d.rounded_rectangle(rect, radius=32, fill=BAD if armed else (CARD_PRESSED if pressed else CARD))
        label = "再点退出" if armed else "退出"
        d.text(((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2), label,
               font=self.app.fonts.ui(28), fill=ON_ACCENT if armed else DIM, anchor="mm")

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
            color = ACCENT if step <= index else LINE
            d.rounded_rectangle((left, y0, left + width, y1), radius=(y1 - y0) // 2, fill=color)

    def paint_body(self, d, rect, pressed):
        if self.showing_ingredients():
            self.paint_ingredients(d, rect)
        else:
            self.paint_step(d, rect)

    def step_layout(self, rect):
        """(font, lines, line height, timer chip rect) of the current step in the body rect."""
        app = self.app
        x0, y0, x1, y1 = rect
        step = self.current_step()
        reserved = (CHIP_HEIGHT + 28 if step.get("minutes") else 0) + (90 if self.preview() else 0)
        for size in STEP_SIZES:
            font = app.fonts.ui(size)
            lines = app.wrap(font, step["text"], x1 - x0 - 16)
            line_height = round(size * 1.4)
            if len(lines) * line_height <= y1 - y0 - reserved or size == STEP_SIZES[-1]:
                break
        lines = lines[:max(1, (y1 - y0 - reserved) // line_height)]
        top = y0 + len(lines) * line_height + 20
        return font, lines, line_height, (x0 + 8, top, x0 + 368, top + CHIP_HEIGHT)

    def preview(self):
        steps, index = self.recipe.get("steps") or [], self.recipe.get("index", -1)
        return steps[index + 1]["text"] if 0 <= index and index + 1 < len(steps) else ""

    def paint_step(self, d, rect):
        if not self.current_step():
            return
        app = self.app
        x0, y0, x1, y1 = rect
        font, lines, line_height, _chip = self.step_layout(rect)
        for number, line in enumerate(lines):
            d.text((x0 + 8, y0 + line_height // 2 + number * line_height), line, font=font, fill=TEXT, anchor="lm")
        preview = self.preview()
        if preview:
            small = app.fonts.ui(28)
            d.line((x0 + 8, y1 - 80, x1 - 8, y1 - 80), fill=LINE, width=2)
            d.text((x0 + 8, y1 - 38), app.fit(f"下一步：{preview}", small, x1 - x0 - 16), font=small, fill=FAINT, anchor="lm")

    def paint_ingredients(self, d, rect):
        app = self.app
        x0, y0, x1, y1 = rect
        items = self.recipe.get("ingredients") or ["这道菜没有列出食材"]
        font = app.fonts.ui(36)
        columns = 2 if len(items) > 9 else 1
        column_width = (x1 - x0) // columns
        rows = (len(items) + columns - 1) // columns
        line_height = min(72, max(44, (y1 - y0) // max(1, rows)))
        for number, item in enumerate(items):
            column, row = divmod(number, rows)
            x = x0 + 8 + column * column_width
            y = y0 + line_height // 2 + row * line_height
            if y > y1 - line_height // 2:
                break
            d.ellipse((x, y - 6, x + 12, y + 6), fill=ACCENT)
            d.text((x + 28, y), app.fit(item, font, column_width - 44), font=font, fill=TEXT, anchor="lm")
        if self.recipe.get("index", -1) < 0:
            hint = app.fonts.ui(28)
            d.text(((x0 + x1) // 2, y1 - 24), "准备好了说“下一步”，或点右下角", font=hint, fill=FAINT, anchor="mm")

    def paint_chip(self, d, rect, pressed, timer):
        """A step's timer: "▶ 计时 N 分钟" to start it, its countdown with ✕ while it runs."""
        x0, y0, x1, y1 = rect
        cy, radius = (y0 + y1) // 2, (y1 - y0) // 2
        font = self.app.fonts.ui(30)
        if timer is None:
            step = self.current_step()
            if not step:
                return
            d.rounded_rectangle(rect, radius=radius, outline=WARN, width=2, fill=CARD_PRESSED if pressed else None)
            d.polygon([(x0 + 30, cy - 12), (x0 + 30, cy + 12), (x0 + 50, cy)], fill=WARN)
            d.text((x0 + 64, cy), f"计时 {step['minutes']} 分钟", font=font, fill=WARN, anchor="lm")
            return
        if self.armed_for(timer["id"]):
            d.rounded_rectangle(rect, radius=radius, fill=BAD)
            d.text(((x0 + x1) // 2, cy), "再点取消", font=font, fill=ON_ACCENT, anchor="mm")
            return
        urgent = timer["seconds"] < 60
        fill = ACCENT if urgent else (CARD_PRESSED if pressed else CARD)
        color = ON_ACCENT if urgent else TEXT
        d.rounded_rectangle(rect, radius=radius, fill=fill)
        label = (f"第{timer['step']}步  " if timer["step"] else "") + clock(timer["seconds"])
        d.text((x0 + 28, cy), self.app.fit(label, font, x1 - x0 - 84), font=font, fill=color, anchor="lm")
        cx, r = x1 - 36, 9
        d.line((cx - r, cy - r, cx + r, cy + r), fill=color if urgent else DIM, width=3)
        d.line((cx - r, cy + r, cx + r, cy - r), fill=color if urgent else DIM, width=3)
