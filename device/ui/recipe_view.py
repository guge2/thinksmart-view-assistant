"""The kitchen recipe page: one big step at a time, readable from across the counter, with the dish's
running timers and buttons for hands that happen to be clean. The voice service owns the recipe; taps
send it commands and the page follows the state it sends back."""

import time

from theme import ACCENT, BAD, CARD, CARD_PRESSED, DIM, FAINT, LINE, ON_ACCENT, TEXT, WARN, Widget

STEP_SIZES = (56, 48, 40, 34)
EXIT_CONFIRM_SECONDS = 3


def recipe_timers(recipe, alarm_items):
    """(step number, seconds left) of this dish's running timers, soonest first."""
    prefix = f"{recipe.get('title')}·第"
    found = []
    for item in alarm_items:
        label = item.get("label") or ""
        if item.get("kind") == "timer" and label.startswith(prefix) and isinstance(item.get("due"), (int, float)):
            step = label[len(prefix):].rstrip("步")
            found.append((int(step) if step.isdigit() else 0, max(0, int(item["due"] - time.time()))))
    return sorted(found, key=lambda timer: timer[1])[:3]


class RecipeView:
    def __init__(self, app):
        self.app = app
        self.exit_armed_until = 0.0

    @property
    def recipe(self):
        return self.app.recipe or {}

    def showing_ingredients(self):
        return self.recipe.get("view") == "ingredients" or self.recipe.get("index", -1) < 0

    def layout(self, music):
        recipe = self.recipe
        timers = len(recipe_timers(recipe, self.app.alarm_items))
        view = "ingredients" if self.showing_ingredients() else "steps"
        return f"recipe:{view}:{recipe.get('index')}:{timers}" + (":music" if music else "")

    def widgets(self):
        app = self.app
        music = app.layout_has("music")
        timers = recipe_timers(self.recipe, app.alarm_items)
        body_bottom = 1150 - (110 if music else 0) - (84 if timers else 0)
        widgets = [
            app.status_widget(),
            Widget("recipe_title", (40, 64, 600, 196), self.paint_title),
            Widget("recipe_exit", (620, 80, 760, 144), self.paint_exit, tap=self.tap_exit),
            Widget("recipe_progress", (40, 204, 760, 216), self.paint_progress),
            Widget("recipe_body", (40, 236, 760, body_bottom), self.paint_body),
        ]
        top = body_bottom + 8
        if timers:
            widgets.append(Widget("recipe_timers", (40, top, 760, top + 68), self.paint_timers))
            top += 84
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

    def send(self, action):
        self.app.link.send("recipe", a=action)

    def tap_toggle(self, *_args):
        if not self.showing_ingredients():
            self.send("ingredients")
        else:
            self.send("repeat" if self.recipe.get("index", -1) >= 0 else "next")

    def tap_exit(self, *_args):
        # Two taps, so a wet elbow does not end the recipe.
        now = time.monotonic()
        if now < self.exit_armed_until:
            self.exit_armed_until = 0.0
            self.send("stop")
        else:
            self.exit_armed_until = now + EXIT_CONFIRM_SECONDS
        self.app.dirty.add("recipe_exit")

    def tick(self, now):
        """Repaints what changes every tick: the countdowns and the exit confirmation."""
        dirty = {"recipe_timers"}
        if self.exit_armed_until and now >= self.exit_armed_until:
            self.exit_armed_until = 0.0
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
        armed = time.monotonic() < self.exit_armed_until
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

    def paint_step(self, d, rect):
        app, recipe = self.app, self.recipe
        x0, y0, x1, y1 = rect
        steps = recipe.get("steps") or []
        index = recipe.get("index", 0)
        if not 0 <= index < len(steps):
            return
        step = steps[index]
        width = x1 - x0 - 16
        preview = steps[index + 1]["text"] if index + 1 < len(steps) else ""
        reserved = (96 if step.get("minutes") else 0) + (90 if preview else 0)
        for size in STEP_SIZES:
            font = app.fonts.ui(size)
            lines = app.wrap(font, step["text"], width)
            line_height = round(size * 1.4)
            if len(lines) * line_height <= y1 - y0 - reserved or size == STEP_SIZES[-1]:
                break
        y = y0 + line_height // 2
        for line in lines:
            if y > y1 - reserved:
                break
            d.text((x0 + 8, y), line, font=font, fill=TEXT, anchor="lm")
            y += line_height
        if step.get("minutes"):
            chip = f"需要等待 {step['minutes']} 分钟"
            small = app.fonts.ui(30)
            chip_width = small.getlength(chip) + 48
            top = y + 8
            d.rounded_rectangle((x0 + 8, top, x0 + 8 + chip_width, top + 60), radius=30, outline=WARN, width=2)
            d.text((x0 + 32, top + 30), chip, font=small, fill=WARN, anchor="lm")
        if preview:
            small = app.fonts.ui(28)
            d.line((x0 + 8, y1 - 80, x1 - 8, y1 - 80), fill=LINE, width=2)
            d.text((x0 + 8, y1 - 38), app.fit(f"下一步：{preview}", small, width), font=small, fill=FAINT, anchor="lm")

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

    def paint_timers(self, d, rect, pressed):
        app = self.app
        x0, y0, x1, y1 = rect
        timers = recipe_timers(self.recipe, app.alarm_items)
        if not timers:
            return
        width = (x1 - x0 - 16 * (len(timers) - 1)) / len(timers)
        font = app.fonts.ui(30)
        for number, (step, seconds) in enumerate(timers):
            left = x0 + number * (width + 16)
            urgent = seconds < 60
            d.rounded_rectangle((left, y0, left + width, y1), radius=(y1 - y0) // 2,
                                fill=ACCENT if urgent else CARD)
            label = f"第{step}步  {seconds // 60:02d}:{seconds % 60:02d}" if step else f"{seconds // 60:02d}:{seconds % 60:02d}"
            d.text((left + width / 2, (y0 + y1) // 2), label, font=font,
                   fill=ON_ACCENT if urgent else TEXT, anchor="mm")
