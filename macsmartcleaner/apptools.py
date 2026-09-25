"""Full-screen tools: Space Lens (browse folders by size) and the Uninstaller."""
from __future__ import annotations

import curses
import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import safety, sizes, ui, uninstall
from .cleaner import move_to_trash
from .context import Context
from .sizes import Usage, human
from .tuikit import CYAN, GREEN, GREY, MAGENTA, YELLOW, Canvas, wrap

DATA_VOLUME = "/System/Volumes/Data"


# =========================================================================== Space Lens

@dataclass
class Entry:
    name: str
    path: str
    is_dir: bool
    usage: Usage


class LensScreen(Canvas):
    """Browse any folder sorted by size; drill in, go back, send things to the Trash."""

    def __init__(self, ctx: Context, start: Optional[str] = None):
        self.ctx = ctx
        self.path = os.path.abspath(start or ctx.home)
        self.entries: List[Entry] = []
        self.cur = 0
        self.top = 0
        self.flash = ""
        self.frame = 0
        self.history: Dict[str, Tuple[int, int]] = {}  # path -> (cursor, top)
        self.listings: Dict[str, List[Entry]] = {}
        self.busy = False
        self.progress = [0, 0, ""]  # files, bytes, current folder

    # ---- measuring (background thread so the screen keeps animating) -------
    def _load(self, path: str) -> None:
        if path in self.listings:
            self.entries = self.listings[path]
            return
        entries: List[Entry] = []
        dirs: List[str] = []
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                        if not is_dir:
                            st = e.stat(follow_symlinks=False)
                            u = Usage(bytes=getattr(st, "st_blocks", 0) * 512 or st.st_size, files=1,
                                      newest_mtime=st.st_mtime)
                            entries.append(Entry(e.name, e.path, False, u))
                        else:
                            dirs.append(e.path)
                    except OSError:
                        continue
        except OSError as e:
            self.flash = f"Can't open {path}: {e.strerror or e}" + ("" if self.ctx.is_root else " (try sudo)")
        self.progress = [0, 0, ""]

        def on_dir(d: str, files: int, nbytes: int) -> None:
            self.progress[0] += files
            self.progress[1] += nbytes
            self.progress[2] = d

        result: Dict[str, Usage] = {}

        def work() -> None:
            result.update(sizes.measure_many(dirs, on_dir=on_dir))

        self.busy = True
        t = threading.Thread(target=work, daemon=True)
        t.start()
        while t.is_alive():
            self.frame += 1
            self.draw_measuring(path)
            t.join(0.1)
        self.busy = False
        for d in dirs:
            entries.append(Entry(os.path.basename(d), d, True, result.get(d, Usage())))
        entries.sort(key=lambda e: e.usage.bytes, reverse=True)
        self.listings[path] = entries
        self.entries = entries

    def go(self, path: str) -> None:
        self.history[self.path] = (self.cur, self.top)
        self.path = path
        self.cur, self.top = self.history.get(path, (0, 0))
        self._load(path)
        self.cur = min(self.cur, max(0, len(self.entries) - 1))

    # ---- drawing ---------------------------------------------------------------
    def draw_measuring(self, path: str) -> None:
        s = self.scr
        s.erase()
        h, w = self.size()
        self.title(0, 1, "◧ Space Lens", self.frame)
        self.put(0, 16, self._display(path)[: w - 18], curses.color_pair(GREY))
        spin = ui.SPINNER[self.frame % len(ui.SPINNER)]
        y = h // 2 - 1
        self.put(y, 4, f"{spin} Measuring {self._display(path)}", curses.A_BOLD | curses.color_pair(CYAN))
        files, nbytes, cur = self.progress
        self.put(y + 1, 6, f"{files:,} files · {human(nbytes)}", curses.color_pair(YELLOW) | curses.A_BOLD)
        self.put(y + 2, 6, self._display(cur)[: w - 8], curses.color_pair(GREY))
        s.refresh()

    def _display(self, p: str) -> str:
        home = self.ctx.home
        return "~" + p[len(home):] if p == home or p.startswith(home + "/") else p

    def draw(self) -> None:
        s = self.scr
        s.erase()
        h, w = self.size()
        total = sum(e.usage.bytes for e in self.entries)
        self.title(0, 1, "◧ Space Lens", 0)
        self.put(0, 16, self._display(self.path)[: w - 32], curses.A_BOLD)
        tot = f"{human(total)} in {len(self.entries)} items"
        self.put(0, w - len(tot) - 2, tot, curses.color_pair(GREY))
        self.hline(1)
        list_top, list_h = 2, h - 2 - 4
        if self.cur < self.top:
            self.top = self.cur
        if self.cur >= self.top + list_h:
            self.top = self.cur - list_h + 1
        biggest = max([e.usage.bytes for e in self.entries] + [1])
        if not self.entries:
            self.put(list_top + 1, 3, "Empty folder (or macOS won't let us look inside without sudo).",
                     curses.color_pair(GREY))
        for n, e in enumerate(self.entries[self.top: self.top + list_h]):
            idx = self.top + n
            y = list_top + n
            is_cur = idx == self.cur
            base = curses.A_REVERSE if is_cur else 0
            if is_cur:
                self.put(y, 0, " " * w, curses.A_REVERSE)
            share = e.usage.bytes / total if total else 0
            self.put(y, 1, "▸" if is_cur else " ", base | curses.A_BOLD)
            self.put(y, 3, f"{human(e.usage.bytes):>9}", base | curses.A_BOLD)
            if not is_cur:
                self.bar(y, 13, e.usage.bytes / biggest, 16)
            self.put(y, 30, f"{share * 100:4.1f}%", base | curses.color_pair(GREY))
            icon = "▸ " if e.is_dir else "  "
            label = icon + e.name + ("/" if e.is_dir else "")
            self.put(y, 37, label[: w - 50], base | (curses.color_pair(CYAN) if e.is_dir else 0))
            extra = f"{e.usage.files:,} files" if e.is_dir else ""
            if e.usage.errors and not self.ctx.is_root:
                extra = "partly unreadable"
            self.put(y, w - 13, extra[:12], base | curses.color_pair(GREY))
        self.hline(h - 4)
        if self.entries:
            e = self.entries[self.cur]
            age = int(self.ctx.days_since(e.usage.newest_mtime)) if e.usage.newest_mtime else None
            self.put(h - 3, 2, self._display(e.path)[: w - 4], curses.color_pair(GREY))
            info = (f"last changed {age} days ago" if age is not None else "") + \
                   ("   · needs sudo to see everything inside" if e.usage.errors and not self.ctx.is_root else "")
            self.put(h - 3 + 0, 2, "", 0)
            self.put(h - 3, max(2, w - len(info) - 2), info, curses.color_pair(GREY))
        self.footer([("↑↓", "move"), ("enter/→", "open"), ("←", "back"), ("o", "Finder"),
                     ("d", "delete to Trash"), ("~", "home"), ("/", "whole disk"), ("q", "quit")], self.flash)
        s.refresh()

    # ---- actions ---------------------------------------------------------------
    def trash_current(self) -> None:
        e = self.entries[self.cur]
        try:
            safety.check(e.path, self.ctx, trash=True)
        except safety.UnsafePath as err:
            self.flash = f"Protected: {err}"[:200]
            return
        lines = [f"Move {e.name} to the Trash?", "", f"{human(e.usage.bytes)} · {self._display(e.path)}",
                 "", "You can put it back from the Trash until you empty it."]
        if not self.dialog(lines, yes="Move to Trash"):
            return
        try:
            move_to_trash([e.path], self.ctx)
        except (OSError, safety.UnsafePath) as err:
            self.flash = f"Couldn't move it: {getattr(err, 'strerror', None) or err}"
            return
        self.entries.remove(e)
        # parents' totals are now stale
        for p in list(self.listings):
            if self.path == p or self.path.startswith(p.rstrip("/") + "/"):
                if p != self.path:
                    self.listings.pop(p, None)
        self.cur = min(self.cur, max(0, len(self.entries) - 1))
        self.flash = f"✔ Moved {e.name} to the Trash ({human(e.usage.bytes)})."

    def loop(self, scr) -> str:
        self.scr = scr
        curses.curs_set(0)
        self.init_colors()
        scr.keypad(True)
        with sizes.cache_session():
            if not self.entries:
                self._load(self.path)
            while True:
                self.draw()
                k = scr.getch()
                self.flash = ""
                n = len(self.entries)
                if k in (ord("q"), 27):
                    return "back"
                if k in (curses.KEY_DOWN, ord("j")):
                    self.cur = min(self.cur + 1, max(0, n - 1))
                elif k in (curses.KEY_UP, ord("k")):
                    self.cur = max(self.cur - 1, 0)
                elif k == curses.KEY_NPAGE:
                    self.cur = min(self.cur + 10, max(0, n - 1))
                elif k == curses.KEY_PPAGE:
                    self.cur = max(self.cur - 10, 0)
                elif k in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT, ord("l")) and n:
                    e = self.entries[self.cur]
                    if e.is_dir:
                        self.go(e.path)
                elif k in (curses.KEY_LEFT, curses.KEY_BACKSPACE, 127, ord("h")):
                    parent = os.path.dirname(self.path.rstrip("/")) or "/"
                    if parent != self.path:
                        child = self.path
                        self.go(parent)
                        for i, e in enumerate(self.entries):
                            if e.path == child:
                                self.cur = i
                elif k == ord("~"):
                    self.go(self.ctx.home)
                elif k == ord("/"):
                    self.go(DATA_VOLUME if os.path.isdir(DATA_VOLUME) else "/")
                elif k == ord("o") and n:
                    self.ctx.run(["open", "-R", self.entries[self.cur].path], timeout=10)
                    self.flash = "Shown in Finder."
                elif k in (ord("d"), ord("x"), curses.KEY_DC) and n:
                    self.trash_current()
                elif k == ord("r"):
                    self.listings.pop(self.path, None)
                    self._load(self.path)


# =========================================================================== Uninstaller

class UninstallScreen(Canvas):
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.apps: List[uninstall.App] = []
        self.cur = 0
        self.top = 0
        self.flash = ""
        self.sort = "size"
        self.selected: set = set()
        self.frame = 0
        self.progress = [0, 0, ""]

    def _load(self) -> None:
        self.apps = uninstall.list_apps(self.ctx)

        def on_dir(d: str, files: int, nbytes: int) -> None:
            self.progress[0] += files
            self.progress[1] += nbytes
            self.progress[2] = d

        t = threading.Thread(target=lambda: (uninstall.measure_apps(self.apps, on_dir),
                                             uninstall.load_last_used(self.apps, self.ctx)), daemon=True)
        t.start()
        while t.is_alive():
            self.frame += 1
            s = self.scr
            s.erase()
            h, w = self.size()
            self.title(0, 1, "⌫ Uninstaller", self.frame)
            y = h // 2 - 1
            self.put(y, 4, f"{ui.SPINNER[self.frame % 10]} Measuring {len(self.apps)} apps",
                     curses.A_BOLD | curses.color_pair(CYAN))
            self.put(y + 1, 6, f"{self.progress[0]:,} files · {human(self.progress[1])}",
                     curses.color_pair(YELLOW) | curses.A_BOLD)
            self.put(y + 2, 6, self.progress[2][: w - 8], curses.color_pair(GREY))
            s.refresh()
            t.join(0.1)
        self.resort()

    def resort(self) -> None:
        if self.sort == "size":
            self.apps.sort(key=lambda a: a.total, reverse=True)
        elif self.sort == "used":
            self.apps.sort(key=lambda a: a.last_used or 0)
        else:
            self.apps.sort(key=lambda a: a.name.lower())

    def draw(self) -> None:
        s = self.scr
        s.erase()
        h, w = self.size()
        self.title(0, 1, "⌫ Uninstaller", 0)
        sel = [a for a in self.apps if a.path in self.selected]
        info = f"Selected: {len(sel)} app(s), {human(sum(a.total for a in sel))}"
        self.put(0, w - len(info) - 2, info, curses.A_BOLD | curses.color_pair(GREEN))
        self.put(1, 1, "Removes an app and everything it left around your Mac. All of it goes to the Trash.",
                 curses.color_pair(GREY))
        self.hline(2)
        detail_h = 7
        list_top, list_h = 3, h - 3 - detail_h - 2
        if self.cur < self.top:
            self.top = self.cur
        if self.cur >= self.top + list_h:
            self.top = self.cur - list_h + 1
        biggest = max([a.total for a in self.apps] + [1])
        for n, a in enumerate(self.apps[self.top: self.top + list_h]):
            idx = self.top + n
            y = list_top + n
            is_cur = idx == self.cur
            base = curses.A_REVERSE if is_cur else 0
            if is_cur:
                self.put(y, 0, " " * w, curses.A_REVERSE)
            on = a.path in self.selected
            self.put(y, 1, "▸" if is_cur else " ", base | curses.A_BOLD)
            self.put(y, 3, "[x]" if on else "[ ]", base | (curses.color_pair(GREEN) | curses.A_BOLD if on else
                                                           curses.color_pair(GREY)))
            self.put(y, 7, f"{human(a.total):>9}", base | curses.A_BOLD)
            if not is_cur:
                self.bar(y, 17, a.total / biggest, 10)
            used = uninstall.human_age(a.last_used)
            old = a.last_used is None or (time.time() - a.last_used) > 180 * 86400
            self.put(y, 29, f"{used:<10}", base | curses.color_pair(YELLOW if old else GREY))
            self.put(y, 40, a.name[: max(10, w // 3)], base | curses.A_BOLD)
            self.put(y, 41 + max(10, w // 3), a.version[:12], base | curses.color_pair(GREY))
        dy = h - detail_h - 2
        self.hline(dy)
        if self.apps:
            a = self.apps[self.cur]
            self.put(dy + 1, 1, a.name, curses.A_BOLD)
            self.put(dy + 1, 3 + len(a.name), f"{a.bundle_id} · {a.path}"[: w - 6 - len(a.name)], curses.color_pair(GREY))
            if a.files:
                kinds = uninstall.summary_by_kind(a.files)
                line = f"App {human(a.size)}  +  " + "  ".join(f"{k} {human(v)}" for k, v in
                                                               sorted(kinds.items(), key=lambda kv: -kv[1]))
                for k2, l2 in enumerate(wrap(line, w - 4)[:4]):
                    self.put(dy + 2 + k2, 2, l2, curses.color_pair(MAGENTA) if k2 == 0 else curses.color_pair(GREY))
                if any(adm for _p, _s, adm in a.files):
                    self.put(dy + detail_h - 1, 2, "Some system files need your password.", curses.color_pair(GREY))
            else:
                self.put(dy + 2, 2, "Press space to select - its leftovers (settings, caches, containers, agents)"
                         " are found and listed here.", curses.color_pair(GREY))
        self.footer([("↑↓", "move"), ("space", "select"), ("d", "uninstall selected"),
                     ("s", f"sort: {self.sort}"), ("o", "Finder"), ("q", "back")], self.flash)
        s.refresh()

    def loop(self, scr) -> str:
        self.scr = scr
        curses.curs_set(0)
        self.init_colors()
        scr.keypad(True)
        with sizes.cache_session():
            if not self.apps:
                self._load()
            while True:
                self.draw()
                k = scr.getch()
                self.flash = ""
                n = len(self.apps)
                if k in (ord("q"), 27):
                    return "back"
                if k in (curses.KEY_DOWN, ord("j")):
                    self.cur = min(self.cur + 1, max(0, n - 1))
                elif k in (curses.KEY_UP, ord("k")):
                    self.cur = max(self.cur - 1, 0)
                elif k == curses.KEY_NPAGE:
                    self.cur = min(self.cur + 10, max(0, n - 1))
                elif k == curses.KEY_PPAGE:
                    self.cur = max(self.cur - 10, 0)
                elif k == ord("s"):
                    self.sort = {"size": "used", "used": "name", "name": "size"}[self.sort]
                    self.resort()
                elif k == ord("o") and n:
                    self.ctx.run(["open", "-R", self.apps[self.cur].path], timeout=10)
                elif k == ord(" ") and n:
                    a = self.apps[self.cur]
                    if a.path in self.selected:
                        self.selected.discard(a.path)
                    else:
                        if not a.files:
                            uninstall.collect(a, self.ctx)
                        self.selected.add(a.path)
                    self.cur = min(self.cur + 1, n - 1)
                elif k in (10, 13) and n:
                    uninstall.collect(self.apps[self.cur], self.ctx)
                elif k in (ord("d"), ord("x"), curses.KEY_DC):
                    sel = [a for a in self.apps if a.path in self.selected]
                    if not sel:
                        self.flash = "Select apps with space first."
                        continue
                    lines = [f"Uninstall {len(sel)} app(s)?", ""] + \
                            [f"  {a.name}: app + {len(a.files)} related item(s), {human(a.total)}" for a in sel[:8]] + \
                            ["", "Running apps are quit first. Everything goes to the Trash."]
                    if self.dialog(lines, yes="Uninstall"):
                        return "uninstall"

    def chosen(self) -> List[uninstall.App]:
        return [a for a in self.apps if a.path in self.selected]


def run_uninstall(apps: List[uninstall.App], ctx: Context) -> Tuple[int, List[str]]:
    import sys
    if sys.stdout.isatty():
        sys.stdout.write("\033[H\033[2J")
    ui.banner("uninstalling")
    moved = 0
    problems: List[str] = []
    for a in apps:
        with ui.Spinner(f"Uninstalling {a.name}") as spin:
            n, probs = uninstall.uninstall(a, ctx)
            moved += n
            problems += [f"{a.name}: {p}" for p in probs]
            spin.done(not probs, f"{human(n)} moved to the Trash" if not probs else probs[0])
    color = ui.color_enabled(sys.stdout)
    G, B, R = (ui.GREEN, ui.BOLD, ui.RESET) if color else ("", "", "")
    print(f"\n  {G}{B}✨ {len(apps)} app(s) removed{R} - {human(moved)} moved to the Trash "
          "(empty it to free the space, or put things back if you change your mind).")
    return moved, problems
