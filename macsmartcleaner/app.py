"""Home screen and the full-screen tools: status dashboard, startup items, optimizer.

Flow: `msc` opens the animated menu. Screens that need the real terminal
(password prompts, long cleanups) return an action; ``run`` leaves curses,
does the work with the normal live progress output, then comes back.
"""
from __future__ import annotations

import curses
import os
import sys
import time
from typing import Callable, List, Optional, Sequence, Tuple

from . import apptools, optimize, startup, sysinfo, ui
from .context import Context
from .sizes import human
from .tuikit import (CYAN, GREEN, GREY, INVERT, MAGENTA, RED, YELLOW, Canvas, pct, wrap)

LOGO = [
    "┏┳┓┏━┓┏━╸",
    "┃┃┃┗━┓┃  ",
    "╹ ╹┗━┛┗━╸",
]

MENU = [
    ("smart", "✦", "Smart Clean", "Clear caches & junk, skipping open apps"),
    ("scan", "◎", "Deep Scan", "See everything using space, pick what goes"),
    ("lens", "◧", "Space Lens", "Browse any folder by size, drill down, trash"),
    ("uninstall", "⌫", "Uninstaller", "Remove apps with all their leftovers"),
    ("startup", "↑", "Startup Items", "Apps & helpers that launch automatically"),
    ("optimize", "⚙", "Optimize", "DNS, memory, Finder/Dock, Spotlight & more"),
    ("status", "◔", "System Status", "Live CPU, GPU, memory, disk, network"),
    ("quit", "×", "Quit", ""),
]


# =========================================================================== menu

class Menu(Canvas):
    def __init__(self, ctx: Context, monitor: sysinfo.Monitor):
        self.ctx = ctx
        self.mon = monitor
        self.cur = 0
        self.frame = 0

    def draw(self) -> None:
        s = self.scr
        s.erase()
        h, w = self.size()
        step = 2 if h >= len(MENU) * 2 + 12 else 1  # compact menu on short terminals
        top = max(1, (h - (len(MENU) * step + 12)) // 2)
        # animated logo: gradient + a light sweeping across
        for i, line in enumerate(LOGO):
            x0 = (w - 34) // 2
            for j, ch in enumerate(line):
                glint = abs((j + i) - (self.frame % 30)) <= 1
                attr = (curses.color_pair(5) | curses.A_BOLD) if glint else (self.grad_attr(j / len(line)) | curses.A_BOLD)
                self.put(top + i, x0 + j, ch, attr)
        self.title(top, (w - 34) // 2 + 12, "macsmartcleaner", self.frame)
        self.put(top + 1, (w - 34) // 2 + 12, "keep your Mac lean", curses.color_pair(GREY))
        self.put(top + 2, (w - 34) // 2 + 12, self.mon.machine.chip[:40] or "", curses.color_pair(GREY))

        y = top + 5
        box_w = min(72, w - 4)
        x0 = (w - box_w) // 2
        for i, (_key, icon, name, desc) in enumerate(MENU):
            sel = i == self.cur
            if sel:
                pulse = "▶" if self.frame % 10 < 7 else "▷"
                self.put(y, x0, " " * box_w, curses.color_pair(INVERT))
                self.put(y, x0 + 1, f"{pulse} {icon}  {name}", curses.color_pair(INVERT) | curses.A_BOLD)
                self.put(y, x0 + 22, desc[: box_w - 27], curses.color_pair(INVERT))
            else:
                self.put(y, x0 + 3, f"{icon}  {name}", curses.A_BOLD)
                self.put(y, x0 + 22, desc[: box_w - 27], curses.color_pair(GREY))
            self.put(y, x0 + box_w - 3, str(i + 1) if i < len(MENU) - 1 else "q", curses.color_pair(GREY))
            y += step

        # live mini status
        snap = self.mon.snapshot()
        y = min(h - 4, y + 1)
        bw = 12
        parts: List[Tuple[str, float, str]] = []
        if snap.cpu_total is not None:
            parts.append(("CPU", snap.cpu_total, pct(snap.cpu_total).strip()))
        if snap.mem.get("total"):
            parts.append(("RAM", snap.mem["used"] / snap.mem["total"],
                          f"{snap.mem['used'] / 1e9:.1f}/{snap.mem['total'] / 1e9:.0f} GB"))
        if snap.disk_total:
            parts.append(("Disk", snap.disk_used / snap.disk_total, f"{human(snap.disk_free)} free"))
        seg = 5 + bw + 15
        x = max(1, (w - seg * len(parts)) // 2)
        for label, frac, txt in parts:
            self.put(y, x, label, curses.color_pair(GREY))
            self.bar(y, x + 5, frac, bw, heat=True)
            self.put(y, x + 6 + bw, txt, curses.A_BOLD)
            x += seg
        self.footer([("↑↓", "choose"), ("enter", "open"), (f"1-{len(MENU) - 1}", "jump"), ("q", "quit")])
        s.refresh()

    def loop(self, scr) -> str:
        self.scr = scr
        curses.curs_set(0)
        self.init_colors()
        scr.keypad(True)
        scr.timeout(90)
        while True:
            self.frame += 1
            self.draw()
            k = scr.getch()
            if k == -1:
                continue
            if k in (ord("q"), 27):
                return "quit"
            if k in (curses.KEY_DOWN, ord("j"), 9):
                self.cur = (self.cur + 1) % len(MENU)
            elif k in (curses.KEY_UP, ord("k")):
                self.cur = (self.cur - 1) % len(MENU)
            elif k in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT, ord(" ")):
                return MENU[self.cur][0]
            elif ord("1") <= k <= ord(str(len(MENU) - 1)):
                return MENU[k - ord("1")][0]


# =========================================================================== status dashboard

class StatusScreen(Canvas):
    # quitting these would log you out, freeze the screen or crash macOS
    PROTECTED = {"kernel_task", "launchd", "WindowServer", "loginwindow", "Finder", "Dock", "SystemUIServer",
                 "mds", "mds_stores", "coreaudiod", "hidd", "logd", "configd", "opendirectoryd", "powerd",
                 "syslogd", "UserEventAgent", "cfprefsd", "distnoted", "securityd", "trustd", "notifyd"}

    def __init__(self, monitor: sysinfo.Monitor):
        self.mon = monitor
        self.frame = 0
        self.sort = "cpu"
        self.sel = 0          # highlighted row in the process list
        self.shown: list = []  # processes currently on screen
        self.flash = ""

    def section(self, y: int, x: int, w: int, title: str, right: str = "") -> None:
        self.put(y, x, f"▌{title}", self.grad_attr(0.1) | curses.A_BOLD)
        if right:
            self.put(y, x + w - len(right) - 1, right, curses.color_pair(GREY))

    def meter(self, y: int, x: int, w: int, label: str, frac: Optional[float], text: str) -> None:
        self.put(y, x, f"{label:<6}", curses.color_pair(GREY))
        bw = max(6, w - 8 - len(text) - 2)
        self.bar(y, x + 7, frac or 0.0, bw, heat=True)
        self.put(y, x + 8 + bw, text, curses.A_BOLD)

    def draw(self) -> None:
        s = self.scr
        s.erase()
        h, w = self.size()
        snap = self.mon.snapshot()
        m = self.mon.machine
        spin = ui.SPINNER[self.frame % len(ui.SPINNER)]
        self.title(0, 1, "◔ System Status", self.frame)
        info = " · ".join(x for x in (m.model, m.chip, m.os_name, f"up {sysinfo.fmt_uptime(m.boot_time)}") if x)
        self.put(0, 19, info[: max(0, w - 30)], curses.color_pair(GREY))
        self.put(0, w - 8, f"{spin} live", curses.color_pair(GREEN) | curses.A_BOLD)

        two = w >= 100
        colw = (w - 3) // 2 if two else w - 2
        lx, rx = 1, (colw + 2 if two else 1)
        y = 2

        # ---- CPU ----------------------------------------------------------------
        load = " ".join(f"{v:.2f}" for v in snap.load)
        self.section(y, lx, colw, f" CPU {pct(snap.cpu_total).strip()}", f"load {load}")
        self.spark(y + 1, lx, self.mon.history("cpu"), colw - 1)
        cores = snap.cpu_cores
        ny = y + 2
        per_row = 2 if colw >= 44 else 1
        cw = (colw - 1) // per_row
        for i, v in enumerate(cores):
            kind = ""
            if m.e_cores and m.p_cores:
                kind = "E" if i < m.e_cores else "P"
            label = f"{kind}{i:<2}" if kind else f"#{i:<2}"
            cy, cx = ny + i // per_row, lx + (i % per_row) * cw
            self.put(cy, cx, label, curses.color_pair(GREY))
            self.bar(cy, cx + 4, v, max(4, cw - 10), heat=True)
            self.put(cy, cx + cw - 5, f"{v * 100:3.0f}%", curses.A_BOLD)
        cpu_end = ny + (len(cores) + per_row - 1) // per_row + 1

        # ---- Memory (right column, or below) ---------------------------------
        my = y if two else cpu_end
        mem = snap.mem
        total = mem.get("total") or 1
        level_color = {"normal": GREEN, "warning": YELLOW, "critical": RED}.get(snap.pressure_level, GREY)
        self.section(my, rx, colw, " Memory", f"{mem.get('used', 0) / 1e9:.1f} of {total / 1e9:.0f} GB used")
        self.spark(my + 1, rx, self.mon.history("mem"), colw - 1)
        # stacked bar: app | wired | compressed | cached
        bw = colw - 2
        segs = [("app", mem.get("app", 0), CYAN), ("wired", mem.get("wired", 0), MAGENTA),
                ("compressed", mem.get("compressed", 0), YELLOW), ("cached", mem.get("cached", 0), GREY)]
        x = rx
        for _name, val, color in segs:
            n = int(round(bw * val / total))
            self.put(my + 2, x, "█" * n, curses.color_pair(color))
            x += n
        self.put(my + 2, x, "·" * max(0, rx + bw - x), curses.color_pair(GREY))
        lx2 = rx
        for name, val, color in segs:
            txt = f"■ {name} {val / 1e9:.1f}G "
            self.put(my + 3, lx2, "■", curses.color_pair(color))
            self.put(my + 3, lx2 + 2, txt[2:], curses.color_pair(GREY))
            lx2 += len(txt)
        self.put(my + 4, rx, "Pressure", curses.color_pair(GREY))
        self.bar(my + 4, rx + 9, snap.pressure or 0, max(6, colw - 32), heat=True)
        self.put(my + 4, rx + 10 + max(6, colw - 32), f"● {snap.pressure_level}", curses.color_pair(level_color) | curses.A_BOLD)
        swap = f"{snap.swap_used / 1e9:.1f} of {snap.swap_total / 1e9:.1f} GB" if snap.swap_total else "none"
        self.put(my + 5, rx, f"Swap     {swap}", curses.color_pair(GREY))
        mem_end = my + 7

        # ---- GPU / thermal / battery ---------------------------------------------
        gy = mem_end if two else mem_end
        gpu = snap.gpu or {}
        gtxt = pct(gpu.get("util")).strip()
        cores_txt = f"{int(gpu['cores'])} cores" if gpu.get("cores") else ""
        self.section(gy, rx, colw, f" GPU {gtxt}", cores_txt)
        if gpu.get("util") is not None:
            self.spark(gy + 1, rx, self.mon.history("gpu"), colw - 1)
            gm = gpu.get("memory")
            self.put(gy + 2, rx, f"Renderer {pct(gpu.get('renderer')).strip()}" +
                     (f" · GPU memory {gm / 1e9:.1f} GB" if gm else ""), curses.color_pair(GREY))
        else:
            self.put(gy + 1, rx, "GPU statistics not available on this Mac.", curses.color_pair(GREY))
        therm_color = GREEN if snap.thermal == "nominal" else YELLOW
        self.put(gy + 3, rx, "Thermal  ", curses.color_pair(GREY))
        self.put(gy + 3, rx + 9, f"● {snap.thermal}", curses.color_pair(therm_color) | curses.A_BOLD)
        b = snap.battery
        if b:
            self.put(gy + 4, rx, "Battery  ", curses.color_pair(GREY))
            self.bar(gy + 4, rx + 9, int(b["percent"]) / 100, 12)  # type: ignore[arg-type]
            extra = f" {b['percent']}% · {b['state']}" + (f" · {b['remaining']} left" if b.get("remaining") else "")
            self.put(gy + 4, rx + 22, extra, curses.A_BOLD)
        right_end = gy + 6

        # ---- Disk + Network (left column below CPU) -------------------------------
        dy = cpu_end if two else right_end
        self.section(dy, lx, colw, " Storage", f"{human(snap.disk_free)} free")
        if snap.disk_total:
            self.meter(dy + 1, lx, colw, "Used", snap.disk_used / snap.disk_total,
                       f"{human(snap.disk_used)} / {human(snap.disk_total)}")
        io = snap.disk_read_mbs
        self.put(dy + 2, lx, f"I/O    {io:6.1f} MB/s" if io is not None else "I/O       --", curses.color_pair(GREY))
        self.spark(dy + 2, lx + 20, self.mon.history("disk") and [min(1, v / 500) for v in self.mon.history("disk")],
                   max(4, colw - 21), heat=False)
        ny2 = dy + 4
        self.section(ny2, lx, colw, " Network")
        rxs, txs = self.mon.history("rx"), self.mon.history("tx")
        peak = max(rxs + txs + [1.0])
        self.put(ny2 + 1, lx, f"↓ {sysinfo.fmt_rate(snap.net_rx_bps)}", curses.color_pair(CYAN) | curses.A_BOLD)
        self.spark(ny2 + 1, lx + 15, [v / peak for v in rxs], max(4, colw - 16), heat=False)
        self.put(ny2 + 2, lx, f"↑ {sysinfo.fmt_rate(snap.net_tx_bps)}", curses.color_pair(MAGENTA) | curses.A_BOLD)
        self.spark(ny2 + 2, lx + 15, [v / peak for v in txs], max(4, colw - 16), heat=False)
        left_end = ny2 + 4

        # ---- processes -------------------------------------------------------------
        py = max(left_end, right_end) if two else left_end
        rows = h - py - 3
        if rows >= 3:
            key = "cpu" if self.sort == "cpu" else "rss"
            procs = sorted(snap.procs, key=lambda p: p[key], reverse=True)[:rows - 2]  # type: ignore[index]
            self.shown = procs
            self.sel = min(self.sel, max(0, len(procs) - 1))
            self.section(py, 1, w - 2, f" Heaviest processes by {'CPU' if self.sort == 'cpu' else 'memory'}",
                         "c/m: sort · k: quit · K: force quit")
            self.put(py + 1, 3, f"{'PID':>7}  {'CPU':>6}  {'MEMORY':>9}  NAME", curses.color_pair(GREY))
            for i, p in enumerate(procs):
                cpu = float(p["cpu"])  # type: ignore[arg-type]
                y = py + 2 + i
                base = curses.A_REVERSE if i == self.sel else 0
                if i == self.sel:
                    self.put(y, 1, " " * (w - 2), curses.A_REVERSE)
                self.put(y, 1, "▸" if i == self.sel else " ", base | curses.A_BOLD)
                self.put(y, 3, f"{p['pid']:>7}  ", base | curses.color_pair(GREY))
                self.put(y, 12, f"{cpu:5.1f}%", base | self.heat_attr(min(1, cpu / 100)) | curses.A_BOLD)
                self.put(y, 20, f"{human(int(p['rss'])):>9}  {p['name']}"[: w - 22], base)  # type: ignore[arg-type]
        self.footer([("↑↓", "select"), ("k", "quit app"), ("K", "force quit"), ("c", "sort CPU"),
                     ("m", "sort memory"), ("q", "back")], self.flash)
        s.refresh()

    def loop(self, scr) -> str:
        self.scr = scr
        curses.curs_set(0)
        self.init_colors()
        scr.keypad(True)
        scr.timeout(250)
        while True:
            self.frame += 1
            self.draw()
            k = scr.getch()
            if k == -1:
                continue
            self.flash = ""
            if k in (ord("q"), 27, curses.KEY_LEFT):
                return "back"
            if k == ord("c"):
                self.sort = "cpu"
            elif k == ord("m"):
                self.sort = "mem"
            elif k in (curses.KEY_DOWN, ord("j")):
                self.sel += 1
            elif k == curses.KEY_UP:
                self.sel = max(0, self.sel - 1)
            elif k in (ord("k"), ord("K")) and self.shown:
                self.quit_process(self.shown[self.sel], force=k == ord("K"))

    def quit_process(self, p: dict, force: bool) -> None:
        import signal
        name, pid = str(p["name"]), int(p["pid"])
        if name in self.PROTECTED or pid <= 1 or pid == os.getpid():
            self.flash = f"{name} is part of macOS - quitting it would log you out or freeze the Mac."
            return
        verb = "Force quit" if force else "Quit"
        if not self.dialog([f"{verb} {name} (PID {pid})?", "",
                            "Unsaved work in it may be lost." if force else
                            "It gets the chance to save and close normally."], yes=verb, danger=True):
            return
        try:
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
            self.flash = f"✔ Sent {verb.lower()} to {name}."
        except ProcessLookupError:
            self.flash = f"{name} already exited."
        except PermissionError:
            self.flash = f"{name} belongs to another user or the system - run `sudo msc status` to quit it."


# =========================================================================== startup items

class StartupScreen(Canvas):
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.items: List[startup.StartupItem] = []
        self.cur = 0
        self.scroll = 0
        self.flash = ""
        self.pending: Optional[Tuple[startup.StartupItem, str]] = None

    def refresh_items(self) -> None:
        self.items = startup.scan(self.ctx)
        self.cur = min(self.cur, max(0, len(self.items) - 1))

    def status(self, it: startup.StartupItem) -> Tuple[str, int]:
        if it.broken:
            return "broken", RED
        if it.enabled is False:
            return "disabled", GREY
        if it.running:
            return "running", GREEN
        return "enabled", CYAN

    def draw(self) -> None:
        s = self.scr
        s.erase()
        h, w = self.size()
        self.title(0, 1, "↑ Startup Items", 0)
        counts = f"{sum(1 for i in self.items if i.enabled)} of {len(self.items)} enabled"
        self.put(0, w - len(counts) - 2, counts, curses.color_pair(GREY))
        self.put(1, 1, "Things that start automatically when you log in or boot. Fewer = faster startup & more free memory.",
                 curses.color_pair(GREY))
        self.hline(2)
        list_top, detail_h = 3, 6
        list_h = h - list_top - detail_h - 2
        if self.cur < self.scroll:
            self.scroll = self.cur
        if self.cur >= self.scroll + list_h:
            self.scroll = self.cur - list_h + 1
        if not self.items:
            self.put(list_top + 1, 3, "No third-party startup items found.", curses.color_pair(GREY))
        for n, it in enumerate(self.items[self.scroll: self.scroll + list_h]):
            idx = self.scroll + n
            y = list_top + n
            is_cur = idx == self.cur
            base = curses.A_REVERSE if is_cur else 0
            if is_cur:
                self.put(y, 0, " " * w, curses.A_REVERSE)
            st, color = self.status(it)
            dot = "●" if it.enabled and not it.broken else "○"
            self.put(y, 1, "▸" if is_cur else " ", base | curses.A_BOLD)
            self.put(y, 3, dot, base | curses.color_pair(color) | curses.A_BOLD)
            self.put(y, 5, f"{st:<9}", base | curses.color_pair(color))
            self.put(y, 15, f"{it.kind_label:<14}", base | curses.color_pair(GREY))
            nw = max(20, w // 3)
            self.put(y, 30, it.name[:nw], base | curses.A_BOLD)
            self.put(y, 31 + nw, it.label[: max(0, w - 32 - nw)], base | curses.color_pair(GREY))
        dy = h - detail_h - 2
        self.hline(dy)
        if self.items:
            it = self.items[self.cur]
            self.put(dy + 1, 1, it.name, curses.A_BOLD)
            self.put(dy + 1, 3 + len(it.name), f"· {it.kind_label}" + (" · needs admin password" if it.needs_root else ""),
                     curses.color_pair(GREY))
            lines = wrap(it.advice, w - 4)
            lines.append(f"Runs: {it.program or '?'}")
            lines.append(f"File: {it.path}")
            for k, line in enumerate(lines[: detail_h - 1]):
                self.put(dy + 2 + k, 2, line[: w - 3], (curses.color_pair(YELLOW) if it.broken and k == 0 else
                                                        0 if k == 0 else curses.color_pair(GREY)))
        keys = [("↑↓", "move"), ("space", "enable/disable"), ("x", "remove"), ("o", "Finder"),
                ("r", "refresh"), ("q", "back")]
        self.footer(keys, self.flash)
        s.refresh()

    def act(self, action: str) -> None:
        it = self.items[self.cur]
        if it.kind == "login-item" and action == "disable":
            action = "remove"
        verb = {"disable": "Disable", "enable": "Enable", "remove": "Remove"}[action]
        lines = [f"{verb} {it.name}?", "", *wrap(it.advice, 60)]
        if action == "remove":
            lines += ["", "The launch file goes to the Trash, so you can undo this." if it.kind != "login-item"
                      else "It is removed from Login Items; the app itself stays installed."]
        if it.needs_root:
            lines += ["", "This is a system item: you'll be asked for your password."]
        if not self.dialog(lines, yes=verb, danger=action != "enable"):
            return
        if it.needs_root and not self.ctx.is_root:
            self.pending = (it, action)  # run outside curses so sudo can prompt
            return
        ok, msg = startup.apply(it, action, self.ctx)
        self.flash = ("✔ " if ok else "✖ ") + msg
        self.refresh_items()

    def loop(self, scr) -> str:
        self.scr = scr
        curses.curs_set(0)
        self.init_colors()
        scr.keypad(True)
        if not self.items:
            self.put(1, 2, "Looking for startup items...", curses.A_BOLD)
            scr.refresh()
            self.refresh_items()
        while True:
            self.draw()
            k = scr.getch()
            self.flash = ""
            n = len(self.items)
            if k in (ord("q"), 27):
                return "back"
            if k in (curses.KEY_DOWN, ord("j")):
                self.cur = min(self.cur + 1, max(0, n - 1))
            elif k in (curses.KEY_UP, ord("k")):
                self.cur = max(self.cur - 1, 0)
            elif k == ord("r"):
                self.refresh_items()
                self.flash = "List refreshed."
            elif not n:
                continue
            elif k in (ord(" "), 10, 13):
                it = self.items[self.cur]
                self.act("remove" if it.broken else "enable" if it.enabled is False else "disable")
            elif k in (ord("x"), ord("d"), curses.KEY_DC):
                self.act("remove")
            elif k == ord("o"):
                self.ctx.run(["open", "-R", self.items[self.cur].path], timeout=10)
                self.flash = "Opened in Finder."
            if self.pending:
                return "sudo"


def run_pending_startup(screen: StartupScreen, ctx: Context) -> None:
    it, action = screen.pending  # type: ignore[misc]
    screen.pending = None
    print(f"\n  {ui.BOLD}{action.capitalize()} {it.name}{ui.RESET} needs your Mac password.")
    if os.system("sudo -v") != 0:
        screen.flash = "Cancelled - no password given."
        return
    with ui.Spinner(f"{action.capitalize()} {it.name}") as spin:
        ok, msg = startup.apply(it, action, ctx)
        spin.done(ok, "" if ok else msg)
    screen.flash = ("✔ " if ok else "✖ ") + msg
    screen.refresh_items()


# =========================================================================== optimizer

class OptimizeScreen(Canvas):
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.tasks = optimize.available(ctx)
        self.selected = {t.id for t in self.tasks if t.recommended}
        self.cur = 0
        self.top = 0
        self.flash = ""

    def _height(self, t: optimize.Task, w: int) -> int:
        return 1 + min(2, len(wrap(t.why + (f"  ({t.note})" if t.note else ""), w - 10)))

    def draw(self) -> None:
        s = self.scr
        s.erase()
        h, w = self.size()
        # keep the cursor visible: scroll so tasks top..cur fit on screen
        self.top = min(self.top, self.cur)
        while sum(self._height(t, w) for t in self.tasks[self.top: self.cur + 1]) > h - 6:
            self.top += 1
        self.title(0, 1, "⚙ Optimize", 0)
        self.put(1, 1, "Maintenance tasks using Apple's own tools. Recommended ones are pre-selected.",
                 curses.color_pair(GREY))
        self.hline(2)
        y = 3
        for i, t in enumerate(self.tasks):
            if i < self.top:
                continue
            if y + self._height(t, w) > h - 2:
                self.put(h - 3, w - 12, "↓ more", curses.color_pair(GREY))
                break
            is_cur = i == self.cur
            base = curses.A_REVERSE if is_cur else 0
            if is_cur:
                self.put(y, 0, " " * w, curses.A_REVERSE)
            on = t.id in self.selected
            self.put(y, 1, "▸" if is_cur else " ", base | curses.A_BOLD)
            self.put(y, 3, "[x]" if on else "[ ]", base | (curses.color_pair(GREEN) | curses.A_BOLD if on else curses.color_pair(GREY)))
            self.put(y, 7, t.name, base | curses.A_BOLD)
            tags = ("admin " if t.needs_root else "") + ("recommended" if t.recommended else "")
            self.put(y, w - len(tags) - 2, tags, base | curses.color_pair(CYAN if t.recommended else GREY))
            desc = t.why + (f"  ({t.note})" if t.note else "")
            for k, line in enumerate(wrap(desc, w - 10)[:2]):
                self.put(y + 1 + k, 7, line, curses.color_pair(GREY))
            y += self._height(t, w)
        n = len(self.selected)
        self.footer([("↑↓", "move"), ("space", "select"), ("enter", f"run {n} task(s)"), ("a", "all"),
                     ("n", "none"), ("q", "back")], self.flash)
        s.refresh()

    def loop(self, scr) -> str:
        self.scr = scr
        curses.curs_set(0)
        self.init_colors()
        scr.keypad(True)
        while True:
            self.draw()
            k = scr.getch()
            self.flash = ""
            if k in (ord("q"), 27):
                return "back"
            if k in (curses.KEY_DOWN, ord("j")):
                self.cur = min(self.cur + 1, len(self.tasks) - 1)
            elif k in (curses.KEY_UP, ord("k")):
                self.cur = max(self.cur - 1, 0)
            elif k == ord(" ") and self.tasks:
                tid = self.tasks[self.cur].id
                self.selected.symmetric_difference_update({tid})
            elif k == ord("a"):
                self.selected = {t.id for t in self.tasks}
            elif k == ord("n"):
                self.selected = set()
            elif k in (10, 13, curses.KEY_ENTER):
                if not self.selected:
                    self.flash = "Select at least one task with space."
                    continue
                return "run"

    def chosen(self) -> List[optimize.Task]:
        return [t for t in self.tasks if t.id in self.selected]


def run_optimize(tasks: Sequence[optimize.Task], ctx: Context, monitor: Optional[sysinfo.Monitor] = None) -> None:
    if sys.stdout.isatty():
        sys.stdout.write("\033[H\033[2J")
    ui.banner("optimizing")
    mem_before = monitor.snapshot().mem.get("used") if monitor else None
    results = optimize.run_tasks(tasks, ctx)
    ok = sum(1 for r in results if r.ok)
    color = ui.color_enabled(sys.stdout)
    G, B, R = (ui.GREEN, ui.BOLD, ui.RESET) if color else ("", "", "")
    print(f"\n  {G}{B}✨ {ok} of {len(results)} task(s) done{R}")
    if monitor and mem_before and any(r.task.id == "memory" and r.ok for r in results):
        time.sleep(1.2)
        after = monitor.snapshot().mem.get("used", mem_before)
        print(f"  Memory in use: {mem_before / 1e9:.1f} GB → {B}{after / 1e9:.1f} GB{R}")


# =========================================================================== main loop

def _pause(msg: str = "Press Enter to go back to the menu") -> None:
    try:
        input(f"\n  {msg} ")
    except (EOFError, KeyboardInterrupt):
        pass


def run(ctx: Context, deep_scan: Callable[[], int], smart_clean: Callable[[], int]) -> int:
    os.environ.setdefault("ESCDELAY", "25")
    monitor = sysinfo.Monitor(ctx.home).start()
    menu = Menu(ctx, monitor)
    startup_screen: Optional[StartupScreen] = None
    lens: Optional["apptools.LensScreen"] = None
    try:
        while True:
            action = curses.wrapper(menu.loop)
            if action == "quit":
                return 0
            if action == "smart":
                smart_clean()
                _pause()
            elif action == "scan":
                deep_scan()
            elif action == "status":
                curses.wrapper(StatusScreen(monitor).loop)
            elif action == "lens":
                lens = lens or apptools.LensScreen(ctx)
                curses.wrapper(lens.loop)
            elif action == "uninstall":
                un = apptools.UninstallScreen(ctx)
                while curses.wrapper(un.loop) == "uninstall":
                    chosen = un.chosen()
                    apptools.run_uninstall(chosen, ctx)
                    gone = {a.path for a in chosen if not os.path.exists(a.path)}
                    un.apps = [a for a in un.apps if a.path not in gone]
                    un.selected -= gone
                    _pause("Press Enter to go back to the app list")
            elif action == "startup":
                startup_screen = startup_screen or StartupScreen(ctx)
                while curses.wrapper(startup_screen.loop) == "sudo":
                    run_pending_startup(startup_screen, ctx)
            elif action == "optimize":
                screen = OptimizeScreen(ctx)
                if curses.wrapper(screen.loop) == "run":
                    run_optimize(screen.chosen(), ctx, monitor)
                    _pause()
    except KeyboardInterrupt:
        return 130
    finally:
        monitor.stop()
