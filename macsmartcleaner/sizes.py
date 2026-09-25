"""Fast, accurate on-disk size measurement.

Uses ``st_blocks`` (what APFS actually allocated) rather than ``st_size`` so
sparse files such as Docker.raw are reported at their real footprint, counts
hard-linked files once, and never crosses into another mounted volume.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass


@dataclass
class Usage:
    bytes: int = 0
    files: int = 0
    newest_mtime: float = 0.0
    errors: int = 0
    exists: bool = True

    def add(self, other: "Usage") -> None:
        self.bytes += other.bytes
        self.files += other.files
        self.errors += other.errors
        self.newest_mtime = max(self.newest_mtime, other.newest_mtime)


def _account(u: Usage, st: os.stat_result, seen: set) -> None:
    is_dir = stat.S_ISDIR(st.st_mode)
    if not is_dir and st.st_nlink > 1:
        key = (st.st_dev, st.st_ino)
        if key in seen:
            return
        seen.add(key)
    blocks = getattr(st, "st_blocks", None)
    u.bytes += blocks * 512 if blocks is not None else st.st_size
    if not is_dir:
        u.files += 1
        # only file mtimes count as "last used"; directory mtimes change on any create/delete
        if st.st_mtime > u.newest_mtime:
            u.newest_mtime = st.st_mtime


def measure(path: str) -> Usage:
    """Return the disk usage of ``path`` (file or directory tree)."""
    u = Usage()
    try:
        root_st = os.lstat(path)
    except FileNotFoundError:
        u.exists = False
        return u
    except OSError:
        u.errors += 1
        return u

    seen: set = set()
    _account(u, root_st, seen)
    if not stat.S_ISDIR(root_st.st_mode):
        return u

    dev = root_st.st_dev
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError:
            u.errors += 1
            continue
        with it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    u.errors += 1
                    continue
                if st.st_dev != dev:  # mounted volume (e.g. simulator runtimes)
                    continue
                _account(u, st, seen)
                if stat.S_ISDIR(st.st_mode):
                    stack.append(entry.path)
    if not u.files:
        u.newest_mtime = root_st.st_mtime
    return u


_UNITS = ["B", "KB", "MB", "GB", "TB"]


def human(n: float) -> str:
    """Format bytes the way Finder does (base 1000)."""
    n = float(n)
    for unit in _UNITS:
        if abs(n) < 1000 or unit == _UNITS[-1]:
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"  # pragma: no cover


def parse_size(text: str) -> int:
    """Parse '500MB', '1.5GB', '200' (bytes) into bytes."""
    s = text.strip().upper().replace(" ", "")
    for mult, unit in ((10**12, "TB"), (10**9, "GB"), (10**6, "MB"), (10**3, "KB"), (1, "B")):
        if s.endswith(unit):
            return int(float(s[: -len(unit)]) * mult)
    for mult, unit in ((10**12, "T"), (10**9, "G"), (10**6, "M"), (10**3, "K")):
        if s.endswith(unit):
            return int(float(s[: -len(unit)]) * mult)
    return int(float(s))
