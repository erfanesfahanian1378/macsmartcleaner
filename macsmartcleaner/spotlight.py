"""Spotlight Doctor: find out WHY the Spotlight index keeps growing, and stop it.

A Spotlight index (/System/Volumes/Data/.Spotlight-V100) is normally a few GB.
When it grows to tens or hundreds of GB and comes back after deleting it, Spotlight
is stuck re-indexing something: a cloud-storage folder whose provider keeps timing
out, folders with millions of constantly changing files (node_modules, build output,
caches, VM/Docker disks, model downloads), or a file that crashes the importer.

Deleting the folder only restarts the loop. The fix is to find what it is reading
(``watch``) and exclude that from indexing (``exclude``), then rebuild once.
"""
from __future__ import annotations

import os
import plistlib
import re
import shutil
import sys
import time
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .context import Context
from .sizes import measure, measure_many

STORE = "/System/Volumes/Data/.Spotlight-V100"
VOLCONF = STORE + "/VolumeConfiguration.plist"
GUARD_LABEL = "com.macsmartcleaner.spotlightguard"
GUARD_LOG = "/Library/Logs/macsmartcleaner-spotlight-guard.log"

# Folders that commonly send Spotlight into an endless indexing loop, with the reason.
SUSPECTS: List[Tuple[str, str]] = [
    ("~/Library/CloudStorage", "OneDrive/Dropbox/Google Drive files: indexing waits on the cloud provider, "
                               "which can time out and make Spotlight retry forever"),
    ("~/Library/Containers/com.docker.docker", "Docker's virtual disk changes all the time"),
    ("~/Library/Group Containers/HUGAGRAYAR.dev.orbstack", "OrbStack's virtual disk changes all the time"),
    ("~/Library/Developer", "Xcode build output and simulators: millions of files rewritten on every build"),
    ("~/Library/Android", "Android SDK and emulator images"),
    ("~/.cache", "tool caches (Hugging Face models, pip, uv...)"),
    ("~/.ollama", "local AI model files"),
    ("~/.lmstudio", "local AI model files"),
    ("~/.npm", "npm package cache"),
    ("~/.gradle", "Gradle caches"),
    ("~/.cargo", "Rust crate sources"),
    ("~/.rustup", "Rust toolchains"),
    ("~/go", "Go module cache"),
    ("~/miniconda3", "conda environments and package cache"),
    ("~/anaconda3", "conda environments and package cache"),
    ("~/miniforge3", "conda environments and package cache"),
    ("~/Parallels", "virtual machine disks"),
    ("~/Virtual Machines.localized", "virtual machine disks"),
    ("~/Library/Containers/com.utmapp.UTM", "virtual machine disks"),
    ("~/Library/Application Support/Steam", "game files"),
]


# --------------------------------------------------------------------------- facts

def index_size(ctx: Context) -> Optional[int]:
    """Size of the system Spotlight index, or None if we can't look inside (needs sudo)."""
    root = ctx.path(STORE)
    u = measure(root)
    if not u.exists:
        return None
    if u.errors and u.files == 0:
        return None
    return u.bytes


def store_breakdown(ctx: Context) -> List[Tuple[str, int]]:
    """Biggest parts of the index (Store-V2/<id>, journals...)."""
    root = ctx.path(STORE)
    parts: List[str] = []
    for base in (root, os.path.join(root, "Store-V2")):
        try:
            parts += [os.path.join(base, n) for n in os.listdir(base) if n != "Store-V2"]
        except OSError:
            pass
    got = measure_many(parts)
    rows = [(os.path.relpath(p, root), got[p].bytes) for p in parts]
    return sorted(rows, key=lambda r: -r[1])


def user_index_size(ctx: Context) -> int:
    """Per-user CoreSpotlight index (Mail, Messages, Notes, apps donating content)."""
    return measure(ctx.path("~/Library/Metadata/CoreSpotlight")).bytes


def status(ctx: Context) -> str:
    res = ctx.run(["mdutil", "-s", "/System/Volumes/Data"], timeout=20, as_user=False)
    if res is None:
        return "unknown (mdutil not available)"
    text = (res.stdout or res.stderr).strip().splitlines()
    return text[-1].strip() if text else "unknown"


def exclusions(ctx: Context) -> Optional[List[str]]:
    """Folders excluded in System Settings > Spotlight > Search Privacy (None if unreadable)."""
    try:
        with open(ctx.path(VOLCONF), "rb") as fh:
            conf = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return [str(x) for x in conf.get("Exclusions", [])]


def is_excluded(path: str, excluded: Sequence[str]) -> bool:
    p = os.path.realpath(path)
    for e in excluded:
        e = os.path.realpath(e)
        if p == e or p.startswith(e.rstrip("/") + "/"):
            return True
    return path.endswith(".noindex") or os.path.exists(os.path.join(path, ".metadata_never_index"))


@dataclass
class Suspect:
    path: str
    reason: str
    bytes: int
    files: int
    excluded: bool


def find_suspects(ctx: Context, excluded: Sequence[str], min_files: int = 20_000) -> List[Suspect]:
    """Known loop-causing folders that exist here, plus any home folder with a huge file count."""
    found = [(ctx.path(p), why) for p, why in SUSPECTS if os.path.isdir(ctx.path(p))]
    # project folders full of node_modules/build output are the other classic cause
    for guess in ("~/Developer", "~/Projects", "~/projects", "~/code", "~/Code", "~/src", "~/dev", "~/workspace",
                  "~/GitHub", "~/repos"):
        if os.path.isdir(ctx.path(guess)):
            found.append((ctx.path(guess), "code projects: node_modules, build folders and .git change constantly"))
    sizes = measure_many([p for p, _ in found])
    out = []
    for p, why in found:
        u = sizes[p]
        if u.files >= min_files or u.bytes >= 5_000_000_000 or "CloudStorage" in p:
            out.append(Suspect(p, why, u.bytes, u.files, is_excluded(p, excluded)))
    return sorted(out, key=lambda s: -s.files)


# --------------------------------------------------------------------------- watching activity

_FS_USAGE_PATH = re.compile(r"\s(/[^\t]*?)\s{2,}\S")


def parse_fs_usage(text: str) -> List[str]:
    """Paths touched, from `fs_usage -w -f filesys` output."""
    paths = []
    for line in text.splitlines():
        m = _FS_USAGE_PATH.search(line)
        if m:
            paths.append(m.group(1).strip())
    return paths


def group_paths(paths: Sequence[str], home: str, depth: int = 3) -> List[Tuple[str, int]]:
    """Count accesses per folder, a few levels deep, so the culprit folder stands out."""
    counts: Counter = Counter()
    for p in paths:
        if p.startswith("/System/Volumes/Data/"):
            p = p[len("/System/Volumes/Data"):]
        if p.startswith(STORE) or "/.Spotlight-V100" in p:
            continue  # Spotlight writing its own index is not the cause
        base = home if p.startswith(home + "/") else ""
        rel = p[len(base):].strip("/").split("/")
        key = (("~/" if base else "/") + "/".join(rel[: depth if base else depth + 1]))
        counts[key] += 1
    return counts.most_common(15)


def watch(ctx: Context, seconds: int = 30) -> Tuple[bool, List[Tuple[str, int]], str]:
    """Record which files Spotlight's workers read for ``seconds`` (needs sudo)."""
    if not ctx.is_root:
        return False, [], "watching Spotlight needs admin rights: run `sudo msc spotlight --watch`"
    res = ctx.run(["fs_usage", "-w", "-f", "filesys", "-t", str(seconds),
                   "mds", "mds_stores", "mdworker", "mdworker_shared", "corespotlightd"],
                  timeout=seconds + 30, as_user=False)
    if res is None:
        return False, [], "fs_usage is not available"
    return True, group_paths(parse_fs_usage(res.stdout), ctx.home), ""


# --------------------------------------------------------------------------- changing things (root)

def _restart_mds(ctx: Context) -> None:
    ctx.run(["launchctl", "kickstart", "-k", "system/com.apple.metadata.mds"], timeout=30, as_user=False)


def set_exclusions(ctx: Context, add: Sequence[str] = (), remove: Sequence[str] = ()) -> Tuple[bool, str]:
    """Edit Spotlight's privacy list (same list as System Settings > Spotlight > Search Privacy).

    A backup of the configuration is written next to it before any change.
    """
    if not ctx.is_root:
        return False, "changing Spotlight exclusions needs admin rights: run with sudo"
    conf_path = ctx.path(VOLCONF)
    try:
        with open(conf_path, "rb") as fh:
            conf = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError) as e:
        return False, f"can't read {VOLCONF}: {e}"
    backup = conf_path + ".msc-backup"
    if not os.path.exists(backup):
        shutil.copy2(conf_path, backup)
    current = [str(x) for x in conf.get("Exclusions", [])]
    for p in add:
        real = os.path.realpath(os.path.expanduser(p))
        if real not in current:
            current.append(real)
    rm = {os.path.realpath(os.path.expanduser(p)) for p in remove}
    current = [c for c in current if os.path.realpath(c) not in rm]
    conf["Exclusions"] = current
    tmp = conf_path + ".msc-tmp"
    with open(tmp, "wb") as fh:
        plistlib.dump(conf, fh, fmt=plistlib.FMT_BINARY)
    os.replace(tmp, conf_path)
    _restart_mds(ctx)
    return True, f"{len(current)} folder(s) now excluded from Spotlight (backup: {backup})"


def rebuild(ctx: Context) -> Tuple[bool, str]:
    sudo = [] if ctx.is_root else ["sudo"]
    res = ctx.run(sudo + ["mdutil", "-E", "/System/Volumes/Data"], timeout=120, as_user=False)
    ok = res is not None and res.returncode == 0
    return ok, "index erased; macOS rebuilds it in the background" if ok else "mdutil -E failed"


def set_indexing(ctx: Context, on: bool) -> Tuple[bool, str]:
    sudo = [] if ctx.is_root else ["sudo"]
    res = ctx.run(sudo + ["mdutil", "-a", "-i", "on" if on else "off"], timeout=60, as_user=False)
    ok = res is not None and res.returncode == 0
    return ok, ("Spotlight indexing turned " + ("on" if on else "off")) if ok else "mdutil failed"


# --------------------------------------------------------------------------- guard (LaunchDaemon)

def guard_plist(max_bytes: int) -> dict:
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return {
        "Label": GUARD_LABEL,
        "ProgramArguments": [sys.executable, "-m", "macsmartcleaner", "-q", "spotlight", "--guard-check",
                             "--max", str(max_bytes)],
        "EnvironmentVariables": {"PYTHONPATH": pkg_parent},
        "StartInterval": 3600,
        "RunAtLoad": True,
        "LowPriorityIO": True,
        "Nice": 10,
    }


def install_guard(ctx: Context, max_bytes: int) -> Tuple[bool, str]:
    if not ctx.is_root:
        return False, "installing the guard needs admin rights: run with sudo"
    path = ctx.path(f"/Library/LaunchDaemons/{GUARD_LABEL}.plist")
    with open(path, "wb") as fh:
        plistlib.dump(guard_plist(max_bytes), fh)
    os.chmod(path, 0o644)
    ctx.run(["launchctl", "bootout", "system", path], timeout=30, as_user=False)
    res = ctx.run(["launchctl", "bootstrap", "system", path], timeout=30, as_user=False)
    ok = res is None or res.returncode == 0
    return ok, (f"guard installed: checks every hour and rebuilds the index if it passes "
                f"{max_bytes / 1e9:.0f} GB (log: {GUARD_LOG})")


def remove_guard(ctx: Context) -> Tuple[bool, str]:
    path = ctx.path(f"/Library/LaunchDaemons/{GUARD_LABEL}.plist")
    if not os.path.exists(path):
        return True, "no guard installed"
    if not ctx.is_root:
        return False, "removing the guard needs admin rights: run with sudo"
    ctx.run(["launchctl", "bootout", "system", path], timeout=30, as_user=False)
    os.remove(path)
    return True, "guard removed"


def guard_check(ctx: Context, max_bytes: int) -> Tuple[bool, str]:
    size = index_size(ctx)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    if size is None:
        msg = f"{stamp} could not measure the index"
    elif size <= max_bytes:
        msg = f"{stamp} index {size / 1e9:.1f} GB - fine"
    else:
        ok, what = rebuild(ctx)
        msg = f"{stamp} index {size / 1e9:.1f} GB > {max_bytes / 1e9:.0f} GB - {what}"
    try:
        with open(ctx.path(GUARD_LOG), "a") as fh:
            fh.write(msg + "\n")
    except OSError:
        pass
    return True, msg


__all__ = ["index_size", "store_breakdown", "user_index_size", "status", "exclusions", "find_suspects", "watch",
           "set_exclusions", "rebuild", "set_indexing", "install_guard", "remove_guard", "guard_check",
           "parse_fs_usage", "group_paths", "Suspect", "Dict"]
