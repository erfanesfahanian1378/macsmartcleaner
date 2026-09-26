"""System Data breakdown: account for the grey "System Data" bar, piece by piece.

macOS lumps into System Data everything it can't put in a named category. This
measures the usual pieces directly, including ones no cleaner shows:
  * other volumes in the same APFS container (VM, Update, Preboot, Recovery)
  * every APFS snapshot (Time Machine and backup apps like Carbon Copy Cloner)
  * Spotlight / CoreSpotlight indexes, swap, version history, logs, caches
Run with sudo, otherwise macOS hides most system folders.
"""
from __future__ import annotations

import os
import plistlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .context import Context
from .sizes import Usage, measure_many

DATA = "/System/Volumes/Data"

# (label, path, what it is / what to do)
PIECES: List[Tuple[str, str, str]] = [
    ("Spotlight index", DATA + "/.Spotlight-V100", "run `sudo msc spotlight` if this is more than a few GB"),
    ("Per-user search index (CoreSpotlight)", "~/Library/Metadata/CoreSpotlight",
     "Mail/Messages/Notes search data - `sudo msc spotlight --rebuild` resets it too"),
    ("Swap & sleep image", "/private/var/vm", "memory paged to disk; restart and close heavy apps to shrink"),
    ("Per-user temp & caches", "/private/var/folders", "`msc smart` cleans the safe part; a restart clears more"),
    ("System databases", "/private/var/db", "mostly logs (diagnostics/uuidtext) - `msc clean --only unified-logs`"),
    ("System logs", "/private/var/log", "`sudo msc clean` includes old rotated logs"),
    ("Document version history", DATA + "/.DocumentRevisions-V100", "managed by macOS - never delete by hand"),
    ("File-change journal", DATA + "/.fseventsd", "managed by macOS"),
    ("Aerial wallpaper videos", "/Library/Application Support/com.apple.idleassetsd", "`sudo msc clean --only aerial-videos`"),
    ("System caches", "/Library/Caches", "`sudo msc clean --tier caution` includes them"),
    ("System app data", "/Library/Application Support", "see the biggest folders below; Uninstaller removes app data"),
    ("Developer tools & simulator runtimes", "/Library/Developer", "Xcode > Settings > Components removes runtimes"),
    ("Pending macOS updates", "/Library/Updates", "install or cancel pending updates in Software Update"),
    ("macOS update leftovers", DATA + "/macOS Install Data", "`sudo msc clean --only macos-install-leftovers`"),
    ("Cloud files kept on this Mac (OneDrive, Dropbox, Google Drive)", "~/Library/CloudStorage",
     "purgeable: macOS evicts them when space runs low; Finder right-click > Free Up Space / Remove Download"),
    ("iCloud Drive files kept on this Mac", "~/Library/Mobile Documents",
     "purgeable with 'Optimize Mac Storage' (System Settings > Apple Account > iCloud > Drive)"),
    ("Your Library", "~/Library", "`msc` Deep Scan breaks this down"),
    ("Hidden folders in your home", "~/.cache", "tool caches - `msc` Deep Scan covers these"),
]


@dataclass
class Row:
    label: str
    path: str
    usage: Optional[Usage]
    advice: str


def measure_pieces(ctx: Context, on_dir=None) -> List[Row]:
    paths = [ctx.path(p) for _l, p, _a in PIECES]
    existing = [p for p in paths if os.path.lexists(p)]
    got = measure_many(existing, on_dir=on_dir)
    rows = []
    for (label, _p, advice), path in zip(PIECES, paths):
        u = got.get(path)
        rows.append(Row(label, path, u, advice))
    return rows


def biggest_children(paths: List[str], top: int = 6, on_dir=None) -> Dict[str, List[Tuple[str, int]]]:
    kids: Dict[str, List[str]] = {}
    for p in paths:
        try:
            kids[p] = [os.path.join(p, n) for n in os.listdir(p)]
        except OSError:
            kids[p] = []
    flat = [k for v in kids.values() for k in v]
    got = measure_many(flat, on_dir=on_dir)
    return {p: sorted(((os.path.basename(k), got[k].bytes) for k in v), key=lambda kv: -kv[1])[:top]
            for p, v in kids.items()}


# --------------------------------------------------------------------------- APFS

def parse_apfs_list(plist_bytes: bytes, data_device: Optional[str] = None) -> Tuple[int, int, List[Tuple[str, str, int]]]:
    """`diskutil apfs list -plist` -> (container size, free, [(volume, roles, bytes used)])
    for the container holding the Data volume."""
    try:
        doc = plistlib.loads(plist_bytes)
    except Exception:  # noqa: BLE001 - diskutil output changed or failed
        return 0, 0, []
    for c in doc.get("Containers", []):
        vols = c.get("Volumes", [])
        has_data = any("Data" in v.get("Roles", []) for v in vols)
        if data_device:
            has_data = any(v.get("DeviceIdentifier") == data_device for v in vols) or has_data
        if not has_data:
            continue
        rows = [(str(v.get("Name", "?")), ",".join(v.get("Roles", [])) or "-", int(v.get("CapacityInUse", 0)))
                for v in vols]
        return int(c.get("CapacityCeiling", 0)), int(c.get("CapacityFree", 0)), sorted(rows, key=lambda r: -r[2])
    return 0, 0, []


def apfs_volumes(ctx: Context) -> Tuple[int, int, List[Tuple[str, str, int]]]:
    res = ctx.run(["diskutil", "apfs", "list", "-plist"], timeout=30, as_user=False)
    if res is None or res.returncode != 0:
        return 0, 0, []
    return parse_apfs_list(res.stdout.encode())


_JXA_IMPORTANT = ('ObjC.import("Foundation"); var u = $.NSURL.fileURLWithPath("/"); '
                  'var k = "NSURLVolumeAvailableCapacityForImportantUsageKey"; '
                  'var r = u.resourceValuesForKeysError([k], null); String(r.objectForKey(k).js)')


def available_like_settings(ctx: Context) -> Optional[int]:
    """Free space the way System Settings/Finder show it (includes purgeable space)."""
    res = ctx.run(["osascript", "-l", "JavaScript", "-e", _JXA_IMPORTANT], timeout=20, as_user=False)
    try:
        return int(float(res.stdout.strip())) if res is not None and res.returncode == 0 else None
    except ValueError:
        return None


def purgeable(ctx: Context, really_free: int) -> Optional[int]:
    """Space macOS calls 'available' but that is still in use - mostly local snapshots."""
    shown = available_like_settings(ctx)
    return None if shown is None else max(0, shown - really_free)


def parse_snapshots(plist_bytes: bytes) -> List[str]:
    try:
        doc = plistlib.loads(plist_bytes)
    except Exception:  # noqa: BLE001
        return []
    return [str(s.get("SnapshotName", "?")) for s in doc.get("Snapshots", [])]


def snapshots(ctx: Context) -> List[str]:
    res = ctx.run(["diskutil", "apfs", "listSnapshots", "-plist", DATA], timeout=30, as_user=False)
    if res is None or res.returncode != 0:
        return []
    return parse_snapshots(res.stdout.encode())
