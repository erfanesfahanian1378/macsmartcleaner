"""Shared curses drawing helpers: colors, gradient bars, sparklines, dialogs."""
from __future__ import annotations

import curses
from typing import List, Optional, Sequence

from . import ui

SPARK = "▁▂▃▄▅▆▇█"

# color pair numbers
GREEN, YELLOW, MAGENTA, CYAN, WHITE, RED, GREY = 1, 2, 3, 4, 5, 6, 7
INVERT, DANGER = 8, 9
GRAD0 = 20  # 20..27 gradient cyan->pink
HEAT0 = 30  # 30..37 heat green->yellow->red


class Canvas:
    """Base for full-screen views. Subclasses set ``self.scr`` then call ``init_colors``."""

    scr: "curses.window"
    grad: List[int]
    heat: List[int]

    def init_colors(self) -> None:
        # curses hands every screen the same window: undo the menu's animation timeout, so
        # getch() waits for a key (else messages are cleared at once by the -1 "no key" reads)
        self.scr.timeout(-1)
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        rich = curses.COLORS >= 256
        base = {GREEN: curses.COLOR_GREEN, YELLOW: curses.COLOR_YELLOW, MAGENTA: curses.COLOR_MAGENTA,
                CYAN: curses.COLOR_CYAN, WHITE: curses.COLOR_WHITE, RED: curses.COLOR_RED,
                GREY: 244 if rich else curses.COLOR_WHITE}
        for n, color in base.items():
            curses.init_pair(n, color, bg)
        curses.init_pair(INVERT, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(DANGER, curses.COLOR_WHITE, curses.COLOR_RED)
        self.grad, self.heat = [], []
        if rich:
            heat_stops = [(60, 220, 120), (200, 220, 60), (255, 170, 40), (255, 70, 70)]
            for k in range(8):
                curses.init_pair(GRAD0 + k, ui._rgb_to_256(*ui._gradient(k / 7)), bg)
                self.grad.append(GRAD0 + k)
                t = k / 7 * (len(heat_stops) - 1)
                i = min(int(t), len(heat_stops) - 2)
                f = t - i
                rgb = tuple(int(heat_stops[i][c] + (heat_stops[i + 1][c] - heat_stops[i][c]) * f) for c in range(3))
                curses.init_pair(HEAT0 + k, ui._rgb_to_256(*rgb), bg)
                self.heat.append(HEAT0 + k)
        else:
            self.grad = [CYAN]
            self.heat = [GREEN, GREEN, YELLOW, YELLOW, YELLOW, RED, RED, RED]

    # ---- primitives ----------------------------------------------------------
    def size(self):
        return self.scr.getmaxyx()

    def put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w or x < 0:
            return
        try:
            self.scr.addnstr(y, x, text, max(0, w - x - (1 if y == h - 1 else 0)), attr)
        except curses.error:
            pass

    def hline(self, y: int) -> None:
        self.put(y, 0, "─" * self.size()[1], curses.color_pair(GREY))

    def grad_attr(self, t: float) -> int:
        return curses.color_pair(self.grad[min(len(self.grad) - 1, max(0, int(t * len(self.grad))))])

    def heat_attr(self, t: float) -> int:
        return curses.color_pair(self.heat[min(len(self.heat) - 1, max(0, int(t * len(self.heat))))])

    def bar(self, y: int, x: int, frac: float, width: int, heat: bool = False, empty: str = "·") -> None:
        """Horizontal bar. ``heat`` colors the whole bar green->red by how full it is."""
        frac = min(max(frac, 0.0), 1.0)
        exact = frac * width
        filled = int(exact)
        for i in range(width):
            attr = self.heat_attr(frac) if heat else self.grad_attr(i / max(width, 1))
            if i < filled:
                self.put(y, x + i, "█", attr)
            elif i == filled and exact - filled > 0.05:
                self.put(y, x + i, " ▏▎▍▌▋▊▉"[int((exact - filled) * 8)], attr)
            else:
                self.put(y, x + i, empty, curses.color_pair(GREY))

    def spark(self, y: int, x: int, values: Sequence[float], width: int, heat: bool = True) -> None:
        """Sparkline of the last ``width`` values (each 0..1)."""
        vals = list(values)[-width:]
        pad = width - len(vals)
        for i, v in enumerate(vals):
            v = min(max(v, 0.0), 1.0)
            self.put(y, x + pad + i, SPARK[min(7, int(v * 7.999))], self.heat_attr(v) if heat else self.grad_attr(v))

    def title(self, y: int, x: int, text: str, frame: int = 0) -> None:
        """Gradient title with a light sweeping across it."""
        glint = frame % (len(text) + 14) - 7
        for i, ch in enumerate(text):
            attr = self.grad_attr(i / max(len(text) - 1, 1)) | curses.A_BOLD
            if abs(i - glint) <= 1:
                attr = curses.color_pair(WHITE) | curses.A_BOLD
            self.put(y, x + i, ch, attr)

    def footer(self, keys: Sequence[tuple], flash: str = "") -> None:
        h, w = self.size()
        self.hline(h - 2)
        if flash:
            self.put(h - 1, 1, flash, curses.A_BOLD | curses.color_pair(YELLOW))
            return
        x = 1
        for key, what in keys:
            if x + len(key) + len(what) + 3 > w:
                break
            attr = curses.color_pair(DANGER) if what.lower().startswith(("delete", "disable", "remove")) \
                else curses.color_pair(INVERT)
            self.put(h - 1, x, f" {key} ", attr | curses.A_BOLD)
            self.put(h - 1, x + len(key) + 2, what, curses.color_pair(GREY))
            x += len(key) + len(what) + 4

    def dialog(self, lines: Sequence[str], yes: str = "Yes", no: str = "Cancel", danger: bool = True) -> bool:
        h, w = self.size()
        bw = min(w - 4, max(len(line) for line in lines) + 8)
        bh = len(lines) + 4
        win = curses.newwin(bh, bw, max(0, (h - bh) // 2), max(0, (w - bw) // 2))
        win.box()
        for k, line in enumerate(lines):
            try:
                win.addnstr(1 + k, 3, line, bw - 5, curses.A_BOLD if k == 0 else 0)
            except curses.error:
                pass
        try:
            win.addstr(bh - 2, 3, f" y  {yes} ", curses.color_pair(DANGER if danger else GREEN) | curses.A_BOLD)
            win.addstr(bh - 2, 9 + len(yes) + 2, f" n  {no} ", curses.color_pair(INVERT))
        except curses.error:
            pass
        win.refresh()
        while True:
            k = self.scr.getch()
            if k in (ord("y"), ord("Y")):
                return True
            if k in (ord("n"), ord("N"), 27, ord("q")):
                return False


def wrap(text: str, width: int) -> List[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    if line:
        lines.append(line)
    return lines


def pct(v: Optional[float]) -> str:
    return "  n/a" if v is None else f"{v * 100:4.0f}%"
