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


def bundle_id_of(folder_name: str) -> Optional[str]:
    """'ABCDE12345.group.com.foo.bar' / 'com.foo.bar.savedState' -> 'com.foo.bar' (None if not an id)."""
    n = folder_name
    for suffix in (".savedState", ".binarycookies", ".plist"):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
    n = _TEAM_PREFIX.sub("", n)
    if n.startswith("group."):
        n = n[len("group."):]
    if not _BUNDLE_ID.match(n):
        return None
    return n.lower()


def is_apple(bundle_id: str) -> bool:
    return bundle_id.startswith(("com.apple.", "apple.", "com.appleinternal."))


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
