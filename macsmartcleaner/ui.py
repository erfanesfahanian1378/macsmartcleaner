"""Live terminal progress: spinner, gradient percentage bar, live counters.

The point is that a long scan never looks frozen. While work runs, a
background thread redraws a small block ~12 times a second:

   ⠹ Measuring known junk                                          2/4
     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╸━━━━━━━━━━━━━━━━━━━━━━  47%
     184,302 files · 93.1 GB · 0:42 · ~/Library/Caches/com.spotify.client/Data

and finished stages become permanent check lines above it. When stderr is
not a terminal (scheduled runs, pipes) it falls back to plain log lines.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from typing import List, Optional, Sequence, Tuple

from .sizes import human

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# gradient stops (RGB): cyan -> blue -> violet -> pink
GRADIENT = [(0, 215, 255), (80, 120, 255), (170, 90, 255), (255, 80, 200)]


def color_enabled(stream=None) -> bool:
    stream = stream or sys.stderr
    return stream.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


def _rgb_to_256(r: int, g: int, b: int) -> int:
    def q(v: int) -> int:
        return 0 if v < 48 else 1 if v < 115 else (v - 35) // 40
    return 16 + 36 * q(r) + 6 * q(g) + q(b)


def _gradient(t: float) -> Tuple[int, int, int]:
    t = min(max(t, 0.0), 1.0) * (len(GRADIENT) - 1)
    i = min(int(t), len(GRADIENT) - 2)
    f = t - i
    a, b = GRADIENT[i], GRADIENT[i + 1]
    return tuple(int(a[k] + (b[k] - a[k]) * f) for k in range(3))  # type: ignore[return-value]


def fg(rgb: Tuple[int, int, int]) -> str:
    if os.environ.get("COLORTERM") in ("truecolor", "24bit"):
        return f"\033[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    return f"\033[38;5;{_rgb_to_256(*rgb)}m"


RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
GREEN, YELLOW, CYAN = "\033[32m", "\033[33m", "\033[36m"


def gradient_text(text: str) -> str:
    n = max(len(text) - 1, 1)
    return "".join(fg(_gradient(i / n)) + ch for i, ch in enumerate(text)) + RESET


def banner(subtitle: str = "", stream=None) -> None:
    stream = stream or sys.stderr
    if color_enabled(stream):
        stream.write(f"\n  {BOLD}{gradient_text('◆ macsmartcleaner')}{RESET}  {DIM}{subtitle}{RESET}\n\n")
    else:
        stream.write(f"macsmartcleaner - {subtitle}\n")
    stream.flush()


def fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _shorten(path: str, width: int) -> str:
    home = os.path.expanduser("~")
    if path.startswith(home):
        path = "~" + path[len(home):]
    if len(path) <= width:
        return path
    if width < 8:
        return path[:width]
    keep = width - 1
    return path[: keep // 3] + "…" + path[-(keep - keep // 3):]


class Reporter:
    """No-op progress sink; also the interface scanner/discover/cleaner call."""

    def begin(self, key: str, label: str, total: Optional[int] = None) -> None: ...
    def step(self, n: int = 1, current: Optional[str] = None) -> None: ...
    def fraction(self, frac: float) -> None: ...
    def current(self, text: str) -> None: ...
    def count(self, files: int = 0, nbytes: int = 0) -> None: ...
    def end(self, summary: str = "") -> None: ...
    def close(self) -> None: ...

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


NULL = Reporter()


class PlainReporter(Reporter):
    """For non-interactive output: one line per stage, no animation."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stderr
        self.t0 = time.time()
        self.label = ""

    def begin(self, key, label, total=None):
        self.label, self.t0 = label, time.time()
        self.stream.write(f"- {label}...\n")
        self.stream.flush()

    def end(self, summary=""):
        self.stream.write(f"  done in {fmt_elapsed(time.time() - self.t0)}" + (f": {summary}" if summary else "") + "\n")
        self.stream.flush()


class LiveReporter(Reporter):
    FPS = 12

    def __init__(self, stages: Sequence[Tuple[str, int]], stream=None, counter_label: str = "found"):
        """``stages``: (key, weight) for every stage that will run, in order."""
        self.stream = stream or sys.stderr
        self.weights = dict(stages)
        self.order = [k for k, _ in stages]
        self.counter_label = counter_label
        self.lock = threading.Lock()
        self.done_weight = 0
        self.key = ""
        self.label = "Starting"
        self.total: Optional[int] = None
        self.completed = 0
        self.frac: Optional[float] = None
        self.cur = ""
        self.files = 0
        self.bytes = 0
        self.t_start = self.t_stage = time.time()
        self.frame = 0
        self.shown_pct = 0.0
        self.drawn_lines = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.stream.write("\033[?25l")  # hide cursor
        self._thread.start()

    # ---- api called from worker threads ------------------------------------
    def begin(self, key, label, total=None):
        with self.lock:
            self.key, self.label, self.total = key, label, total
            self.completed, self.frac, self.cur = 0, None, ""
            self.t_stage = time.time()

    def step(self, n=1, current=None):
        with self.lock:
            self.completed += n
            if current:
                self.cur = current

    def fraction(self, frac):
        with self.lock:
            self.frac = frac

    def current(self, text):
        self.cur = text  # atomic attribute swap; no lock needed

    def count(self, files=0, nbytes=0):
        with self.lock:
            self.files += files
            self.bytes += nbytes

    def end(self, summary=""):
        with self.lock:
            self.done_weight += self.weights.get(self.key, 0)
            took = fmt_elapsed(time.time() - self.t_stage)
            line = f"  {GREEN}✔{RESET} {self.label}" + (f"  {DIM}{summary} · {took}{RESET}" if summary else
                                                          f"  {DIM}{took}{RESET}")
            self._clear()
            self.stream.write(line + "\n")
            self.key, self.cur, self.total, self.frac = "", "", None, None
            self._draw()

    def close(self):
        if self._stop.is_set():
            return
        self._stop.set()
        self._thread.join(timeout=1)
        with self.lock:
            self._clear()
            self.stream.write("\033[?25h")  # show cursor
            self.stream.flush()

    # ---- rendering ---------------------------------------------------------
    def _stage_frac(self) -> float:
        if self.frac is not None:
            return min(max(self.frac, 0.0), 1.0)
        if self.total:
            return min(self.completed / self.total, 1.0)
        return 0.0

    def percent(self) -> float:
        total_w = sum(self.weights.values()) or 1
        cur_w = self.weights.get(self.key, 0) if self.key else 0
        pct = 100.0 * (self.done_weight + cur_w * self._stage_frac()) / total_w
        self.shown_pct = max(self.shown_pct, min(pct, 100.0))  # never go backwards
        return self.shown_pct

    def _clear(self):
        if self.drawn_lines:
            self.stream.write("\r\033[K" + "\033[1A\033[K" * (self.drawn_lines - 1))
            self.drawn_lines = 0

    def _bar(self, width: int, pct: float) -> str:
        exact = width * pct / 100
        full = int(exact)
        shimmer = self.frame % (width + 12) - 6  # bright glint sweeping across the filled part
        out = []
        for i in range(width):
            if i < full:
                rgb = _gradient(i / max(width - 1, 1))
                if abs(i - shimmer) <= 1:
                    rgb = tuple(min(255, c + 90) for c in rgb)  # type: ignore[assignment]
                out.append(fg(rgb) + "━")
            elif i == full and pct < 100:
                out.append(fg(_gradient(i / max(width - 1, 1))) + ("╸" if exact - full >= 0.5 else " "))
                out.append(RESET + DIM)
            else:
                out.append("━")
        return "".join(out) + RESET

    def _draw(self):
        cols = shutil.get_terminal_size((80, 20)).columns
        width = max(20, min(cols, 100) - 4)
        pct = self.percent()
        spin = SPINNER[self.frame % len(SPINNER)]
        stage_no = (self.order.index(self.key) + 1) if self.key in self.order else len(self.order)
        right = f"{stage_no}/{len(self.order)}"
        label = self.label[: width - len(right) - 4]
        line1 = f"  {CYAN}{spin}{RESET} {BOLD}{label}{RESET}{' ' * (width - len(label) - len(right) - 2)}{DIM}{right}{RESET}"
        pct_txt = f"{pct:3.0f}%"
        line2 = f"    {self._bar(width - len(pct_txt) - 3, pct)} {BOLD}{pct_txt}{RESET}"
        stats = f"{self.files:,} files · {human(self.bytes)} {self.counter_label} · {fmt_elapsed(time.time() - self.t_start)}"
        room = width - len(stats) - 3
        cur = _shorten(self.cur, room) if room > 10 and self.cur else ""
        line3 = f"    {YELLOW}{stats}{RESET}" + (f"{DIM} · {cur}{RESET}" if cur else "")
        self.stream.write(line1 + "\n" + line2 + "\n" + line3)
        self.stream.flush()
        self.drawn_lines = 3

    def _loop(self):
        while not self._stop.wait(1 / self.FPS):
            with self.lock:
                self.frame += 1
                self._clear()
                self._draw()


def make_reporter(stages: Sequence[Tuple[str, int]], quiet: bool = False, counter_label: str = "found") -> Reporter:
    if quiet:
        return NULL
    if color_enabled(sys.stderr):
        return LiveReporter(stages, counter_label=counter_label)
    return PlainReporter()
