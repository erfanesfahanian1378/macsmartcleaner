"""Which apps are installed, and whether a Library folder belongs to one of them."""
from __future__ import annotations

import glob
import os
import plistlib
import re
from typing import Optional, Set, Tuple

from .context import Context

APP_DIRS = ("/Applications", "/Applications/Utilities", "/System/Applications", "/System/Applications/Utilities",
            "/System/Library/CoreServices", "~/Applications", "/Applications/Setapp")

_BUNDLE_ID = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+){2,}$")
_TEAM_PREFIX = re.compile(r"^[A-Z0-9]{10}\.")


def installed_apps(ctx: Context) -> Tuple[Set[str], Set[str]]:
    """Return (bundle ids, lower-cased app names) of installed apps, including apps nested in folders."""
    ids: Set[str] = set()
    names: Set[str] = set()
    for base in APP_DIRS:
        root = ctx.path(base)
        for app in glob.glob(os.path.join(root, "*.app")) + glob.glob(os.path.join(root, "*", "*.app")):
            names.add(os.path.splitext(os.path.basename(app))[0].lower())
            try:
                with open(os.path.join(app, "Contents", "Info.plist"), "rb") as fh:
                    info = plistlib.load(fh)
                if info.get("CFBundleIdentifier"):
                    ids.add(str(info["CFBundleIdentifier"]).lower())
                for k in ("CFBundleName", "CFBundleDisplayName"):
                    if info.get(k):
                        names.add(str(info[k]).lower())
            except Exception:  # noqa: BLE001 - unreadable/odd plist: the folder name is enough
                pass
    return ids, names


_spotlight_state: dict = {}


def spotlight_ok(ctx: Context) -> bool:
    """True if Spotlight indexes the data volume, so an empty mdfind result can be trusted."""
    if ctx.root != "/":
        return False
    if "ok" not in _spotlight_state:
        res = ctx.run(["mdutil", "-s", "/System/Volumes/Data"], timeout=15, as_user=False)
        _spotlight_state["ok"] = bool(res is not None and res.returncode == 0 and "enabled" in res.stdout.lower())
    return _spotlight_state["ok"]


def bundle_id_of(folder_name: str) -> Optional[str]:
    """'ABCDE12345.group.com.foo.bar' / 'com.foo.bar.savedState' -> 'com.foo.bar' (None if not an id)."""
    n = folder_name
    for suffix in (".savedState", ".binarycookies", ".plist"):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
    n = _TEAM_PREFIX.sub("", n)
    for prefix in ("systemgroup.", "groups.", "group."):
        if n.startswith(prefix):
            n = n[len(prefix):]
            break
    if not _BUNDLE_ID.match(n):
        return None
    return n.lower()


# Apple components whose ids don't start with com.apple (legacy acquisitions and system groups)
APPLE_PREFIXES = ("com.apple.", "apple.", "com.appleinternal.", "is.workflow.", "com.workflow.",
                  "com.shazam.", "com.beats.", "com.filemaker.", "com.claris.")


def is_apple(bundle_id: str) -> bool:
    b = bundle_id.lower()
    return b.startswith(APPLE_PREFIXES) or ".com.apple." in f".{b}" or "com.apple" in b


def installed_somewhere(bundle_id: str, ctx: Context) -> bool:
    """Ask Spotlight whether any app or helper with this id (or from this vendor) exists anywhere."""
    if not spotlight_ok(ctx):
        return False
    vendor = ".".join(bundle_id.split(".")[:2])
    query = f'kMDItemCFBundleIdentifier == "{bundle_id}*"c || kMDItemCFBundleIdentifier == "{vendor}.*"c'
    res = ctx.run(["mdfind", query], timeout=15, as_user=True)
    return bool(res is not None and res.returncode == 0 and res.stdout.strip())


def belongs_to_installed(bundle_id: str, ids: Set[str]) -> bool:
    """True if an installed app plausibly owns this id (same id, a helper of it, or the same vendor)."""
    if bundle_id in ids:
        return True
    vendor = ".".join(bundle_id.split(".")[:2])
    for i in ids:
        if bundle_id.startswith(i + ".") or i.startswith(bundle_id + "."):
            return True
        if i.startswith(vendor + "."):
            return True
    return False
