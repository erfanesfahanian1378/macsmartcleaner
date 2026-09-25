"""Runtime context: where home is, whether we're root, how to run commands.

Everything that touches the real machine goes through here so tests can point
the tool at a fake home directory and a fake command runner.
"""
from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

Runner = Callable[[Sequence[str], int], Optional[subprocess.CompletedProcess]]


def _default_runner(cmd: Sequence[str], timeout: int) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _real_user_home() -> str:
    """When run through sudo, still clean the invoking user's home."""
    sudo_user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and sudo_user:
        try:
            return pwd.getpwnam(sudo_user).pw_dir
        except KeyError:
            pass
    return os.path.expanduser("~")


@dataclass
class Context:
    home: str = field(default_factory=_real_user_home)
    root: str = "/"  # prefix for absolute paths (tests use a temp dir)
    is_root: bool = field(default_factory=lambda: os.geteuid() == 0)
    now: float = field(default_factory=time.time)
    runner: Runner = _default_runner
    sudo_user: Optional[str] = field(default_factory=lambda: os.environ.get("SUDO_USER"))

    def path(self, p: str) -> str:
        if p == "~":
            return self.home
        if p.startswith("~/"):
            return os.path.join(self.home, p[2:])
        if os.path.isabs(p) and self.root != "/":
            return os.path.join(self.root, p.lstrip("/"))
        return p

    def which(self, name: str) -> Optional[str]:
        if "/" in name:
            return name if os.access(name, os.X_OK) else None
        return shutil.which(name)

    def run(self, cmd: Sequence[str], timeout: int = 600, as_user: bool = True) -> Optional[subprocess.CompletedProcess]:
        """Run a command; returns None if the binary is missing or it could not start.

        When we're root via sudo, developer tools (brew, docker, conda...) are
        run as the real user because several of them refuse to run as root.
        """
        if not cmd or self.which(cmd[0]) is None:
            return None
        full: List[str] = list(cmd)
        if as_user and self.is_root and self.sudo_user:
            full = ["sudo", "-u", self.sudo_user, "--"] + full
        return self.runner(full, timeout)

    def days_since(self, ts: float) -> float:
        if not ts:
            return float("inf")
        return max(0.0, (self.now - ts) / 86400)
