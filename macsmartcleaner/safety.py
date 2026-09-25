"""Last line of defence: every path is checked here right before deletion.

A bug in a rule or glob must never be able to delete your home folder,
Documents, Photos library, keychains or anything owned by the OS.
"""
from __future__ import annotations

import fnmatch
import os
from typing import List

from .context import Context

# Deleting exactly these (or any parent of these) is always refused.
PROTECTED_IN_HOME = [
    "", "Library", "Library/Application Support", "Library/Caches", "Library/Containers",
    "Library/Group Containers", "Library/Preferences", "Library/Logs", "Library/Developer",
    "Library/Developer/Xcode", "Library/Mobile Documents", "Library/CloudStorage",
    "Documents", "Desktop", "Downloads", "Pictures", "Movies", "Music", "Public", "Applications",
    ".config", ".ssh", ".cache", ".npm", ".gradle", ".cargo", ".android",
]
PROTECTED_ABS = [
    "/", "/System", "/Library", "/Applications", "/Users", "/Users/Shared", "/private", "/private/var",
    "/private/var/folders", "/usr", "/bin", "/sbin", "/opt", "/Volumes", "/etc", "/var", "/tmp",
    "/Library/Caches", "/Library/Logs", "/Library/Developer",
]
# Nothing inside these is ever touched.
NEVER_INSIDE_HOME = [
    "Library/Keychains", "Library/Mobile Documents", "Library/CloudStorage", "Library/Messages",
    "Library/Mail", "Library/Photos", "Library/Preferences", "Library/Accounts", "Library/Cookies",
    "Library/Safari", ".ssh", ".gnupg", "Pictures/Photos Library.photoslibrary",
]
NEVER_INSIDE_ABS = ["/System", "/usr", "/bin", "/sbin", "/etc", "/private/etc", "/Applications"]
# Deletion is only allowed somewhere under one of these.
ALLOWED_ABS = [
    "/Library/Caches", "/Library/Logs", "/private/var/log", "/private/var/folders",
    "/Library/Developer/CoreSimulator/Caches", "/Users/Shared/UnrealEngine/Launcher/VaultCache",
    "/cores", "/macOS Install Data", "/System/Volumes/Data/macOS Install Data",
]
# Specific places inside otherwise off-limits areas that a rule may remove (glob patterns).
ALLOWED_EXCEPTIONS = [
    "/Applications/Install macOS *.app",   # old macOS installer apps (moved to Trash)
    "/Volumes/*/.Trashes/*/*",             # items in an external drive's trash
]


# Extra places allowed only when moving to the Trash (reversible): what an uninstaller removes.
TRASH_EXCEPTIONS = [
    "/Applications/*.app", "/Applications/*/*.app", "~/Applications/*.app", "~/Applications/*/*.app",
    "~/Library/Preferences/*.plist", "~/Library/Preferences/ByHost/*.plist",
    "/Library/LaunchAgents/*.plist", "/Library/LaunchDaemons/*.plist", "/Library/PrivilegedHelperTools/*",
    "/Library/Application Support/*", "/Library/Preferences/*.plist",
]


class UnsafePath(Exception):
    pass


def _is_apple_app(p: str) -> bool:
    if not p.endswith(".app"):
        return False
    try:
        import plistlib
        with open(os.path.join(p, "Contents", "Info.plist"), "rb") as fh:
            return str(plistlib.load(fh).get("CFBundleIdentifier", "")).lower().startswith("com.apple.")
    except Exception:  # noqa: BLE001 - unreadable: not provably Apple
        return False


def _match_components(path: str, pattern: str) -> bool:
    """Glob match where each * stays inside one path component (unlike fnmatch)."""
    a, b = path.rstrip("/").split("/"), pattern.rstrip("/").split("/")
    return len(a) == len(b) and all(fnmatch.fnmatchcase(x, y) for x, y in zip(a, b))


def _norm(p: str) -> str:
    # Resolve symlinks in the parent (so a symlinked parent can't escape) but
    # keep the final component: deleting a symlink removes only the link.
    parent, name = os.path.split(os.path.abspath(p))
    return os.path.join(os.path.realpath(parent), name) if name else os.path.realpath(parent)


def _within(child: str, parent: str) -> bool:
    child, parent = child.lower(), parent.rstrip("/").lower() or "/"
    return child == parent or child.startswith(parent + "/") or parent == "/"


def check(path: str, ctx: Context, trash: bool = False) -> str:
    """Return the normalized path if it is OK to delete, else raise UnsafePath.

    ``trash=True`` (moving to the Trash, which is reversible) additionally allows app bundles and
    an app's own settings files, but never anything from Apple.
    """
    p = _norm(path)
    home = os.path.realpath(ctx.home)

    def absolute(x: str) -> str:
        return os.path.realpath(ctx.path(x)) if ctx.root != "/" else os.path.realpath(x)

    protected: List[str] = [os.path.join(home, r) if r else home for r in PROTECTED_IN_HOME]
    protected += [absolute(a) for a in PROTECTED_ABS]
    for prot in protected:
        if _within(prot.rstrip("/") or "/", p):  # p is prot itself or a parent of it
            raise UnsafePath(f"refusing to delete protected location {p}")

    for pattern in ALLOWED_EXCEPTIONS:
        if _match_components(p, ctx.path(pattern)):
            return p
    if trash:
        name = os.path.basename(p).lower()
        for pattern in TRASH_EXCEPTIONS:
            if _match_components(p, ctx.path(pattern)):
                if name.startswith(("com.apple.", "apple", ".globalpreferences")) or _is_apple_app(p):
                    raise UnsafePath(f"refusing to remove Apple component {p}")
                return p

    never = [os.path.join(home, r) for r in NEVER_INSIDE_HOME] + [absolute(a) for a in NEVER_INSIDE_ABS]
    for n in never:
        if _within(p, n):
            raise UnsafePath(f"refusing to delete inside protected area {n}")

    allowed = [home] + [absolute(a) for a in ALLOWED_ABS]
    if not any(_within(p, a) and p.lower() != a.lower() for a in allowed):
        raise UnsafePath(f"{p} is outside the areas this tool may clean")
    return p
