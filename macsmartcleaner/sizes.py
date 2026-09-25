"""Fast, accurate on-disk size measurement.

Uses ``st_blocks`` (what APFS actually allocated) rather than ``st_size`` so
sparse files such as Docker.raw are reported at their real footprint, counts
hard-linked files once, and never crosses into another mounted volume.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# called after each directory with (dir path, files added, bytes added)
OnDir = Optional[Callable[[str, int, int], None]]


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


def measure(path: str, on_dir: OnDir = None) -> Usage:
    """Return the disk usage of ``path`` (file or directory tree).

    ``on_dir`` receives incremental progress so a UI can show live counters.
    """
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
        if on_dir:
            on_dir(path, u.files, u.bytes)
        return u

    dev = root_st.st_dev
    stack = [path]
    while stack:
        current = stack.pop()
        files0, bytes0 = u.files, u.bytes
        try:
            it = os.scandir(current)
        except OSError:
            u.errors += 1
            continue
        with it:
            try:
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
            except OSError:
                # listing can fail midway (cloud file providers time out, network
                # volumes drop); keep what we counted and move on
                u.errors += 1
        if on_dir:
            on_dir(current, u.files - files0, u.bytes - bytes0)
    if not u.files:
        u.newest_mtime = root_st.st_mtime
    return u


# --------------------------------------------------------------------------- parallel walker
#
# Python threads don't speed up directory walking: stat() is fast and the GIL
# turns more threads into contention (measured 5-13x slower). Worker
# *processes* do scale, so big scans fan out over a small pool of helper
# processes that talk JSON lines over pipes:
#   * each root is split into its immediate sub-folders -> one task each
#   * tasks are handed out one at a time to whichever worker is free
#   * workers stream progress, results are summed per root (identical to ``measure``)
#   * if a worker can't start or dies, its task is simply measured in-process
# Small jobs stay in-process (starting workers costs ~0.1s).

_session: "contextvars.ContextVar[Optional[_Session]]" = contextvars.ContextVar("msc_session", default=None)
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WORKER_CMD = "from macsmartcleaner.sizes import _worker_main; _worker_main()"


def _u2l(u: Usage) -> list:
    return [u.bytes, u.files, u.newest_mtime, u.errors]


def _l2u(v: list) -> Usage:
    return Usage(bytes=int(v[0]), files=int(v[1]), newest_mtime=float(v[2]), errors=int(v[3]))


class _Worker:
    def __init__(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = _PKG_PARENT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        self.proc = subprocess.Popen([sys.executable, "-c", _WORKER_CMD], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, bufsize=0)
        self.task: Optional[int] = None
        self.buf = b""

    def send(self, task_id: int, path: str, dev: int, cached: Dict[str, Usage]) -> None:
        self.task = task_id
        assert self.proc.stdin is not None
        msg = json.dumps([task_id, path, dev, {k: _u2l(v) for k, v in cached.items()}]) + "\n"
        self.proc.stdin.write(msg.encode("utf-8", "surrogatepass"))

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
            self.proc.wait()
        if self.proc.stdout:
            self.proc.stdout.close()


class _Session:
    def __init__(self):
        self.cache: Dict[str, Usage] = {}
        self.workers: List[_Worker] = []

    def get_workers(self, n: int) -> List[_Worker]:
        while len(self.workers) < n:
            self.workers.append(_Worker())
        return self.workers[:n]

    def drop(self, w: _Worker) -> None:
        if w in self.workers:
            self.workers.remove(w)
        w.close()

    def close(self) -> None:
        for w in self.workers:
            w.close()
        self.workers = []


@contextlib.contextmanager
def cache_session():
    """Within this block every measured root is remembered (later walks reuse it
    instead of walking again) and one set of worker processes serves all scans."""
    sess = _Session()
    token = _session.set(sess)
    try:
        yield sess
    finally:
        _session.reset(token)
        sess.close()


def _walk_tree(path: str, dev: int, cached: Dict[str, Usage],
               progress: Optional[Callable[[int, int, str], None]] = None,
               links: Optional[list] = None) -> Usage:
    """Measure one sub-tree, skipping folders whose totals are already known.

    Hard-linked files (st_nlink > 1) are appended to ``links`` as [ino, bytes, mtime]
    instead of being counted, so the caller can de-duplicate across sub-trees.
    """
    u = Usage()
    seen: set = set()
    pend = [0, 0]
    last = time.time()
    try:
        st0 = os.lstat(path)
    except OSError:
        return Usage(errors=1)
    _account(u, st0, seen)
    stack = [path]
    while stack:
        d = stack.pop()
        f0, b0 = u.files, u.bytes
        try:
            it = os.scandir(d)
        except OSError:
            u.errors += 1
            continue
        with it:
            try:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        u.errors += 1
                        continue
                    if st.st_dev != dev:
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        if entry.path in cached:
                            u.add(cached[entry.path])
                            continue
                        _account(u, st, seen)
                        stack.append(entry.path)
                    elif links is not None and st.st_nlink > 1:
                        blocks = getattr(st, "st_blocks", None)
                        links.append([st.st_ino, blocks * 512 if blocks is not None else st.st_size, st.st_mtime])
                    else:
                        _account(u, st, seen)
            except OSError:
                u.errors += 1
        if progress:
            pend[0] += u.files - f0
            pend[1] += u.bytes - b0
            if time.time() - last > 0.1:
                progress(pend[0], pend[1], d)
                pend[0] = pend[1] = 0
                last = time.time()
    if progress and (pend[0] or pend[1]):
        progress(pend[0], pend[1], path)
    return u


def _worker_main() -> None:
    """Entry point of a helper process: read tasks, stream progress and results."""
    out = sys.stdout

    def progress(files: int, nbytes: int, d: str) -> None:
        out.write(json.dumps(["P", files, nbytes, d]) + "\n")
        out.flush()

    for line in sys.stdin:
        try:
            task_id, path, dev, cached = json.loads(line)
            links: list = []
            u = _walk_tree(path, dev, {k: _l2u(v) for k, v in cached.items()}, progress, links)
            out.write(json.dumps(["R", task_id, _u2l(u), links]) + "\n")
        except Exception:  # noqa: BLE001 - report as unreadable, keep serving
            out.write(json.dumps(["R", task_id, [0, 0, 0.0, 1], []]) + "\n")
        out.flush()


def _under(cache: Dict[str, Usage], folder: str) -> Dict[str, Usage]:
    """Cached totals for folders inside ``folder`` (sorted-key bisect: cheap even for big caches)."""
    if not cache:
        return {}
    import bisect
    keys = _sorted_keys(cache)
    prefix = folder + os.sep
    out = {}
    i = bisect.bisect_left(keys, prefix)
    while i < len(keys) and keys[i].startswith(prefix):
        out[keys[i]] = cache[keys[i]]
        i += 1
    return out


_keys_memo: Dict[int, Tuple[int, List[str]]] = {}


def _sorted_keys(cache: Dict[str, Usage]) -> List[str]:
    memo = _keys_memo.get(id(cache))
    if memo is None or memo[0] != len(cache):
        memo = (len(cache), sorted(cache))
        _keys_memo.clear()
        _keys_memo[id(cache)] = memo
    return memo[1]


def measure_many(paths: Iterable[str], workers: Optional[int] = None, on_dir: OnDir = None,
                 on_root_done: Optional[Callable[[str], None]] = None) -> Dict[str, Usage]:
    """Measure many trees; results identical to calling ``measure`` on each."""
    sess = _session.get()
    cache = sess.cache if sess is not None else {}
    done = on_root_done or (lambda _p: None)
    results: Dict[str, Usage] = {}
    state: Dict[str, list] = {}  # root -> [usage, outstanding tasks, root stat, seen hard-link inodes]
    tasks: List[Tuple[str, str]] = []  # (root, sub-folder)

    def finish(root: str) -> None:
        u, _n, st, _seen = state.pop(root)
        if not u.files:
            u.newest_mtime = st.st_mtime
        results[root] = u
        if sess is not None:
            cache[root] = u
        done(root)

    for p in dict.fromkeys(paths):
        if p in cache:
            results[p] = cache[p]
            done(p)
            continue
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            results[p] = Usage(exists=False)
            done(p)
            continue
        except OSError:
            results[p] = Usage(errors=1)
            done(p)
            continue
        u = Usage()
        _account(u, st, set())
        if not stat.S_ISDIR(st.st_mode):
            results[p] = u
            if on_dir:
                on_dir(p, u.files, u.bytes)
            done(p)
            continue
        subs = []
        seen: set = set()
        try:
            with os.scandir(p) as it:
                for entry in it:
                    try:
                        est = entry.stat(follow_symlinks=False)
                    except OSError:
                        u.errors += 1
                        continue
                    if est.st_dev != st.st_dev:
                        continue
                    if stat.S_ISDIR(est.st_mode):
                        if entry.path in cache:
                            u.add(cache[entry.path])
                        else:
                            subs.append(entry.path)
                    else:
                        _account(u, est, seen)
        except OSError:
            u.errors += 1
        if on_dir and u.files:
            on_dir(p, u.files, u.bytes)
        state[p] = [u, len(subs), st, {k[1] for k in seen}]
        tasks.extend((p, sd) for sd in subs)
        if not subs:
            finish(p)

    def add_result(root: str, u: Usage, links: Sequence[Sequence[float]] = ()) -> None:
        entry = state[root]
        entry[0].add(u)
        for ino, nbytes, mtime in links:
            if ino not in entry[3]:
                entry[3].add(ino)
                entry[0].bytes += int(nbytes)
                entry[0].files += 1
                entry[0].newest_mtime = max(entry[0].newest_mtime, mtime)
        entry[1] -= 1
        if entry[1] == 0:
            finish(root)

    def local(i: int) -> None:
        root, sd = tasks[i]
        prog = (lambda f, b, d: on_dir(d, f, b)) if on_dir else None
        links: list = []
        u = _walk_tree(sd, state[root][2].st_dev, _under(cache, sd), prog, links)
        add_result(root, u, links)

    n = workers or min(8, os.cpu_count() or 2)
    if n <= 1 or len(tasks) < 16 or os.environ.get("MSC_NO_PROCESSES") == "1":
        for i in range(len(tasks)):
            local(i)
        return results

    owner = sess if sess is not None else _Session()
    try:
        _run_parallel(owner, n, tasks, state, cache, on_dir, add_result, local)
    finally:
        if owner is not sess:
            owner.close()
    return results


def _run_parallel(owner: "_Session", n: int, tasks: List[Tuple[str, str]], state: Dict[str, list],
                  cache: Dict[str, Usage], on_dir: OnDir, add_result, local) -> None:
    import selectors
    try:
        pool = owner.get_workers(n)
    except OSError:
        pool = []
    if not pool:
        for i in range(len(tasks)):
            local(i)
        return
    queue_ = list(range(len(tasks)))[::-1]  # pop() from the end = original order
    sel = selectors.DefaultSelector()
    for w in pool:
        sel.register(w.proc.stdout, selectors.EVENT_READ, w)

    def feed(w: _Worker) -> None:
        if not queue_:
            w.task = None
            return
        i = queue_.pop()
        root, sd = tasks[i]
        try:
            w.send(i, sd, state[root][2].st_dev, _under(cache, sd))
        except (OSError, ValueError):
            queue_.append(i)
            retire(w)

    def retire(w: _Worker) -> None:
        try:
            sel.unregister(w.proc.stdout)
        except (KeyError, ValueError):
            pass
        if w.task is not None:
            queue_.append(w.task)
            w.task = None
        owner.drop(w)
        pool.remove(w)

    for w in list(pool):
        feed(w)
    busy = lambda: any(w.task is not None for w in pool)  # noqa: E731
    while busy():
        for key, _ev in sel.select(timeout=1):
            w = key.data
            # raw reads + our own line splitting: buffered readline() would hide
            # already-received lines from select() and stall
            try:
                chunk = os.read(w.proc.stdout.fileno(), 1 << 16)
            except OSError:
                chunk = b""
            if not chunk:  # worker died: its task goes back to the queue
                retire(w)
                continue
            w.buf += chunk
            *lines, w.buf = w.buf.split(b"\n")
            for line in lines:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg[0] == "P":
                    if on_dir:
                        on_dir(msg[3], msg[1], msg[2])
                elif msg[0] == "R" and msg[1] == w.task:
                    root = tasks[msg[1]][0]
                    w.task = None
                    add_result(root, _l2u(msg[2]), msg[3] if len(msg) > 3 else ())
                    feed(w)
        if not pool:
            break
    for w in pool:
        try:
            sel.unregister(w.proc.stdout)
        except (KeyError, ValueError):
            pass
    sel.close()
    while queue_:  # anything left because workers died
        local(queue_.pop())


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
