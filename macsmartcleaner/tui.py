"""Interactive full-screen browser: review scan results, select, and clean.

  ↑/↓ move · space select · tab switch list · a/c/n quick select · o reveal in Finder
  d delete selected · q quit

Built on curses (stdlib). Cleaning happens outside curses, with the normal live
progress display, so sudo password prompts and long deletions behave normally.
"""
from __future__ import annotations

import curses
import os
import shutil
import sys
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from . import cleaner, discover, ui
from .tuikit import Canvas, wrap
from .context import Context
from .discover import Hog
from .rules import Action, Safety
from .scanner import Finding
from .sizes import human

TABS = ["Junk & caches", "Projects", "Big folders", "Large files"]
HELP = [
    ("↑↓", "move"), ("space", "select"), ("tab", "lists"), ("a", "safe"),
    ("c", "+caution"), ("n", "none"), ("o", "Finder"), ("d", "delete"), ("r", "rescan"), ("q", "back"),
]


@dataclass
class Item:
    tab: int
    size: int
    size_known: bool
    title: str
    path: str
    tag: str          # safe | caution | review | info | likely-junk | orphaned | ...
    details: List[str]
    finding: Optional[Finding] = None
    hog: Optional[Hog] = None
    selected: bool = False

    @property
    def selectable(self) -> bool:
        if self.finding is not None:
            return self.finding.cleanable
        return self.hog is not None


def _items_from(ctx: Context, findings: Sequence[Finding], projects: Sequence[Finding],
                hogs: Sequence[Hog]) -> List[Item]:
    items: List[Item] = []
    for f in findings:
        tag = "info" if f.rule.action == Action.REPORT else f.rule.safety.value
        path = discover._display(ctx, f.root) if f.root else f.note
        details = [f.rule.why, "", "After cleaning: " + f.rule.impact]
        if f.rule.action == Action.REPORT:
            details.append("This one is cleaned from the app that owns it (see above).")
        if f.note and f.root:
            details.append(f.note)
        if f.newest_mtime:
            details.append(f"Last modified {int(ctx.days_since(f.newest_mtime))} days ago · {f.files:,} files")
        if f.rule.needs_root and not ctx.is_root:
            details.append("Needs admin rights: run `sudo msc` to clean this one.")
        items.append(Item(0, f.size, f.size_known, f.rule.name, path, tag, details, finding=f,
                          selected=f.cleanable and f.rule.safety == Safety.SAFE and not
                          (f.rule.needs_root and not ctx.is_root)))
    for p in projects:
        idle = int(ctx.days_since(p.newest_mtime)) if p.newest_mtime else None
        items.append(Item(1, p.size, True, p.rule.name, discover._display(ctx, p.root or ""), "caution",
                          [p.rule.why, "", "After cleaning: " + p.rule.impact,
                           f"Project last touched {idle} days ago." if idle is not None else ""],
                          finding=p, selected=idle is not None and idle >= 90))
    for h in hogs:
        if h.verdict in ("large", "old"):
            items.append(Item(3, h.usage.bytes, True, os.path.basename(h.path), h.display, h.verdict,
                              [h.reason, "",
                               "One of the biggest files in your home folder. Only you know if you still need it:",
                               "it is moved to the Trash, so you can undo that. Press o to see it in Finder."],
                              hog=h))
            continue
        items.append(Item(2, h.usage.bytes, True, os.path.basename(h.path), h.display, h.verdict,
                          [h.reason, "",
                           "Not a known junk location, so it's only moved to the Trash (you can undo that).",
                           "Press o to look inside with Finder before deciding."], hog=h))
    items.sort(key=lambda i: (i.tab, -i.size))
    return items


class Browser(Canvas):
    def __init__(self, ctx: Context, items: List[Item]):
        self.ctx = ctx
        self.items = items
        self.tab = 0
        self.cursor = [0] * len(TABS)
        self.scroll = [0] * len(TABS)
        self.flash = ""

    # ---- data helpers ----------------------------------------------------
    def rows(self, tab: Optional[int] = None) -> List[Item]:
        t = self.tab if tab is None else tab
        return [i for i in self.items if i.tab == t]

    def selected(self) -> List[Item]:
        return [i for i in self.items if i.selected]

    # ---- curses ------------------------------------------------------------
    def tag_attr(self, tag: str) -> int:
        n = {"safe": 1, "likely-junk": 1, "caution": 2, "orphaned": 2, "stale": 2, "old": 2, "review": 3,
             "data": 3, "large": 3, "info": 4, "system": 4}.get(tag, 7)
        return curses.color_pair(n)

    def draw(self) -> None:
        s = self.scr
        s.erase()
        h, w = s.getmaxyx()
        if h < 12 or w < 60:
            self.put(0, 0, "Make the terminal window bigger (at least 60x12).", curses.A_BOLD)
            s.refresh()
            return

        # header: title + disk bar
        self.put(0, 1, "◆ macsmartcleaner", curses.A_BOLD | curses.color_pair(self.grad[len(self.grad) // 2]))
        du = shutil.disk_usage(self.ctx.home)
        disk_txt = f" {human(du.used)} used · {human(du.free)} free"
        bw = max(10, min(30, w - 22 - len(disk_txt) - 2))
        self.bar(0, w - bw - len(disk_txt) - 1, du.used / du.total, bw)
        self.put(0, w - len(disk_txt) - 1, disk_txt, curses.color_pair(7))

        # tabs + selection total
        x = 1
        for t, name in enumerate(TABS):
            rows = self.rows(t)
            label = f" {name} ({human(sum(r.size for r in rows))}) "
            self.put(2, x, label, curses.color_pair(8) | curses.A_BOLD if t == self.tab else curses.color_pair(7))
            x += len(label) + 1
        sel = self.selected()
        sel_txt = f"Selected: {human(sum(i.size for i in sel))} in {len(sel)} item(s) "
        self.put(1, w - len(sel_txt) - 1, sel_txt, curses.A_BOLD | curses.color_pair(1))
        self.put(3, 0, "─" * w, curses.color_pair(7))

        # list
        rows = self.rows()
        detail_h = 6
        list_top, list_h = 4, h - 4 - detail_h - 2
        cur = self.cursor[self.tab] = min(self.cursor[self.tab], max(0, len(rows) - 1))
        if cur < self.scroll[self.tab]:
            self.scroll[self.tab] = cur
        if cur >= self.scroll[self.tab] + list_h:
            self.scroll[self.tab] = cur - list_h + 1
        biggest = max([r.size for r in rows] + [1])
        if not rows:
            self.put(list_top + 1, 3, "Nothing here - nice and clean.", curses.color_pair(7))
        for n, item in enumerate(rows[self.scroll[self.tab]: self.scroll[self.tab] + list_h]):
            y = list_top + n
            idx = self.scroll[self.tab] + n
            is_cur = idx == cur
            if is_cur:
                self.put(y, 0, " " * w, curses.A_REVERSE)
            base = curses.A_REVERSE if is_cur else 0
            self.put(y, 1, "▸" if is_cur else " ", base | curses.A_BOLD)
            box = "[x]" if item.selected else "[ ]" if item.selectable else " - "
            self.put(y, 3, box, base | (curses.color_pair(1) | curses.A_BOLD if item.selected else curses.color_pair(7)))
            size = human(item.size) if item.size_known else "   ?"
            if item.finding and not item.size_known:
                size = item.finding.note.split(" ")[0] + " snaps"
            self.put(y, 7, f"{size:>9}", base | curses.A_BOLD)
            if not is_cur:
                self.bar(y, 17, item.size / biggest if item.size_known else 0, 10)
            self.put(y, 29, f"{item.tag:<11}", base | self.tag_attr(item.tag))
            title_w = max(20, min(40, w // 3))
            self.put(y, 41, item.title[:title_w], base)
            self.put(y, 42 + title_w, item.path[: max(0, w - 43 - title_w)], base | (0 if is_cur else curses.color_pair(7)))
        if len(rows) > list_h:
            pos = f" {cur + 1}/{len(rows)} "
            self.put(list_top + list_h - 1, w - len(pos) - 1, pos, curses.color_pair(7))

        # details
        dy = h - detail_h - 2
        self.put(dy, 0, "─" * w, curses.color_pair(7))
        if rows:
            item = rows[cur]
            self.put(dy + 1, 1, item.title, curses.A_BOLD)
            self.put(dy + 1, 2 + len(item.title), f"· {item.tag}", self.tag_attr(item.tag))
            lines: List[str] = []
            for d in item.details:
                lines.extend(wrap(d, w - 4) if d else [""])
            for k, line in enumerate(lines[: detail_h - 1]):
                self.put(dy + 2 + k, 2, line, curses.color_pair(7) if k else 0)

        # footer
        self.put(h - 2, 0, "─" * w, curses.color_pair(7))
        x = 1
        if self.flash:
            self.put(h - 1, 1, self.flash, curses.A_BOLD | curses.color_pair(2))
        else:
            for key, what in HELP:
                if x + len(key) + len(what) + 3 > w:
                    break
                attr = curses.color_pair(9) | curses.A_BOLD if key == "d" else curses.color_pair(4) | curses.A_BOLD
                self.put(h - 1, x, f" {key} ", attr)
                self.put(h - 1, x + len(key) + 2, what, curses.color_pair(7))
                x += len(key) + len(what) + 4
        s.refresh()

    def confirm(self) -> bool:
        sel = self.selected()
        total = sum(i.size for i in sel)
        review = [i for i in sel if i.tag == "review"]
        trash = [i for i in sel if i.hog is not None]
        tm = [i for i in sel if i.finding and i.finding.rule.id == "tm-snapshots"]
        lines = [f"Delete {human(total)} in {len(sel)} item(s)?", ""]
        for t, name in enumerate(TABS):
            part = [i for i in sel if i.tab == t]
            if part:
                lines.append(f"  {name}: {len(part)} item(s), {human(sum(i.size for i in part))}")
        lines.append("")
        if review:
            lines.append(f"• {len(review)} item(s) marked REVIEW (may be data you want)")
        if trash:
            lines.append(f"• {len(trash)} folder(s) will be moved to the Trash")
        if tm:
            lines.append("• Time Machine snapshots: macOS will ask for your password")
        lines += ["", "Caches are rebuilt automatically by the apps that use them."]
        return self.dialog(lines, yes="Yes, clean")

    def loop(self, scr) -> str:
        self.scr = scr
        curses.curs_set(0)
        self.init_colors()
        scr.keypad(True)
        while True:
            self.draw()
            k = scr.getch()
            self.flash = ""
            rows = self.rows()
            cur = self.cursor[self.tab]
            h, _w = scr.getmaxyx()
            page = max(1, h - 14)
            if k in (ord("q"), 27):
                return "quit"
            elif k in (curses.KEY_DOWN, ord("j")):
                self.cursor[self.tab] = min(cur + 1, max(0, len(rows) - 1))
            elif k in (curses.KEY_UP, ord("k")):
                self.cursor[self.tab] = max(cur - 1, 0)
            elif k == curses.KEY_NPAGE:
                self.cursor[self.tab] = min(cur + page, max(0, len(rows) - 1))
            elif k == curses.KEY_PPAGE:
                self.cursor[self.tab] = max(cur - page, 0)
            elif k in (curses.KEY_HOME, ord("g")):
                self.cursor[self.tab] = 0
            elif k in (curses.KEY_END, ord("G")):
                self.cursor[self.tab] = max(0, len(rows) - 1)
            elif k in (9, curses.KEY_RIGHT, ord("l")):
                self.tab = (self.tab + 1) % len(TABS)
            elif k in (curses.KEY_BTAB, curses.KEY_LEFT, ord("h")):
                self.tab = (self.tab - 1) % len(TABS)
            elif k in (ord("1"), ord("2"), ord("3"), ord("4")):
                self.tab = k - ord("1")
            elif k == ord(" ") and rows:
                item = rows[cur]
                if item.selectable:
                    item.selected = not item.selected
                    self.cursor[self.tab] = min(cur + 1, len(rows) - 1)
                else:
                    self.flash = "This one can't be deleted from here - see the details below for how to clean it."
            elif k == ord("a"):
                for i in rows:
                    i.selected = i.selectable and i.tag in ("safe", "likely-junk") or (self.tab == 1 and i.selectable)
            elif k == ord("c"):
                for i in rows:
                    i.selected = i.selectable and i.tag in ("safe", "caution", "likely-junk", "orphaned", "stale")
            elif k == ord("n"):
                for i in rows:
                    i.selected = False
            elif k == ord("o") and rows:
                path = rows[cur].hog.path if rows[cur].hog else (rows[cur].finding.root if rows[cur].finding else None)
                if path:
                    self.ctx.run(["open", "-R", path], timeout=10, as_user=True)
                    self.flash = "Opened in Finder."
            elif k in (ord("d"), ord("x"), curses.KEY_DC):
                if not self.selected():
                    self.flash = "Nothing selected - press space on an item first."
                elif self.confirm():
                    return "clean"
            elif k == ord("r"):
                return "rescan"
            elif k == curses.KEY_RESIZE:
                pass


def perform_cleanup(ctx: Context, items: List[Item]) -> Tuple[int, List[str]]:
    """Run the selected cleanup in the normal terminal; returns (freed bytes, problems)."""
    sel = [i for i in items if i.selected]
    findings = [i.finding for i in sel if i.finding is not None]
    hogs = [i for i in sel if i.hog is not None]
    problems: List[str] = []

    if any(f.rule.command_needs_root for f in findings) and not ctx.is_root:
        print(f"\n  {ui.BOLD}Time Machine snapshots need your Mac password:{ui.RESET}")
        rc = os.system("sudo -v")  # interactive prompt must own the terminal
        if rc != 0:
            findings = [f for f in findings if not f.rule.command_needs_root]
            problems.append("Time Machine snapshots skipped (no password given).")

    if sys.stdout.isatty():
        sys.stdout.write("\033[H\033[2J")  # fresh screen for the cleanup run
    ui.banner("cleaning")
    free_before = shutil.disk_usage(ctx.home).free
    stages = [("clean", 90), ("trash", 10)] if hogs else [("clean", 100)]
    rep = ui.make_reporter(stages, counter_label="freed")
    with rep:
        outcomes = cleaner.execute(findings, ctx, dry_run=False, reporter=rep)
        if hogs:
            rep.begin("trash", "Moving folders to the Trash", total=len(hogs))
            for item in hogs:
                rep.current(item.hog.path)  # type: ignore[union-attr]
                try:
                    cleaner.move_to_trash([item.hog.path], ctx)  # type: ignore[union-attr]
                except Exception as e:  # noqa: BLE001 - report and continue
                    problems.append(f"{item.path}: {e}")
                    item.selected = False
                rep.step()
            rep.end(f"{len(hogs)} folder(s) moved - empty the Trash to free the space")
    freed = sum(o.freed for o in outcomes)
    for o in outcomes:
        problems.extend(f"{o.finding.rule.name}: {m}" for m in o.messages)
    free_after = shutil.disk_usage(ctx.home).free

    # update the list: drop what was cleaned, keep what failed
    ok_ids = {id(o.finding) for o in outcomes if o.ok and not o.messages}
    for item in list(items):
        if not item.selected:
            continue
        if item.hog is not None or (item.finding is not None and id(item.finding) in ok_ids):
            items.remove(item)
        else:
            item.selected = False
            for o in outcomes:
                if o.finding is item.finding:
                    item.size = max(0, item.size - o.freed)

    color = ui.color_enabled(sys.stdout)
    g, b, r, d = (ui.GREEN, ui.BOLD, ui.RESET, ui.DIM) if color else ("", "", "", "")
    print(f"\n  {g}{b}✨ Freed {human(freed)}{r}   free space {human(free_before)} → {b}{human(free_after)}{r}")
    if free_after - free_before < freed * 0.8:
        print(f"  {d}macOS can take a minute to show space from snapshots and purgeable files.{r}")
    if problems:
        print(f"\n  {ui.YELLOW if color else ''}Some things need attention:{r}")
        for p in problems[:15]:
            print(f"   {d}• {p}{r}")
        if len(problems) > 15:
            print(f"   {d}… and {len(problems) - 15} more (see ~/.local/state/macsmartcleaner/history.jsonl){r}")
    return freed, problems


Rescan = Callable[[], Tuple[Sequence[Finding], Sequence[Finding], Sequence[Hog]]]


def run(ctx: Context, findings: Sequence[Finding], projects: Sequence[Finding], hogs: Sequence[Hog],
        rescan: Optional[Rescan] = None) -> int:
    """Browse results; ``rescan`` (r key, or after a cleanup) re-runs the scan and rebuilds the list."""
    items = _items_from(ctx, findings, projects, hogs)
    browser = Browser(ctx, items)
    os.environ.setdefault("ESCDELAY", "25")  # snappy ESC key
    while True:
        action = curses.wrapper(browser.loop)
        if action == "clean":
            perform_cleanup(ctx, items)
            prompt = ("\n  Press Enter to go back to the list, r + Enter to rescan everything "
                      if rescan else "\n  Press Enter to go back to the list ")
            try:
                answer = input(prompt).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if answer != "r" or rescan is None:
                continue
            action = "rescan"
        if action == "rescan":
            if rescan is None:
                continue
            findings, projects, hogs = rescan()
            items[:] = _items_from(ctx, findings, projects, hogs)
            tab = browser.tab
            browser = Browser(ctx, items)
            browser.tab = tab
            browser.flash = "Rescanned - list is up to date."
            continue
        return 0
