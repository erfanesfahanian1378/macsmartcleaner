"""Live system metrics: CPU (per core), GPU, memory & pressure, swap, disk,
network, disk I/O, battery, thermal state and top processes.

macOS sources need no sudo: mach host_processor_info (via ctypes), vm_stat,
sysctl, ioreg, pmset, netstat, iostat, ps. Every parser is a pure function so
it can be tested against captured output. A /proc fallback keeps it usable on
Linux for development.
"""
from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Sequence, Tuple

IS_MAC = sys.platform == "darwin"


def _cmd(args: Sequence[str], timeout: float = 5) -> str:
    try:
        return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _sysctl(name: str) -> str:
    return _cmd(["sysctl", "-n", name], 2).strip()


# --------------------------------------------------------------------------- parsers (pure)

def parse_vm_stat(text: str) -> Dict[str, int]:
    """vm_stat -> bytes per category."""
    m = re.search(r"page size of (\d+) bytes", text)
    page = int(m.group(1)) if m else 4096
    out: Dict[str, int] = {}
    for line in text.splitlines():
        mm = re.match(r'\s*"?([^:"]+)"?:\s+(\d+)\.?\s*$', line)
        if mm:
            out[mm.group(1).strip()] = int(mm.group(2)) * page
    return out


def memory_breakdown(vm: Dict[str, int], total: int) -> Dict[str, int]:
    """Activity Monitor style: used = app + wired + compressed."""
    wired = vm.get("Pages wired down", 0)
    compressed = vm.get("Pages occupied by compressor", 0)
    purgeable = vm.get("Pages purgeable", 0)
    if "Anonymous pages" in vm:
        app = max(0, vm["Anonymous pages"] - purgeable)
    else:
        app = vm.get("Pages active", 0) + vm.get("Pages inactive", 0) - purgeable
    cached = vm.get("File-backed pages", 0) + purgeable
    used = min(total, app + wired + compressed) if total else app + wired + compressed
    return {"total": total, "used": used, "app": app, "wired": wired, "compressed": compressed,
            "cached": cached, "free": max(0, total - used - cached) if total else vm.get("Pages free", 0)}


def parse_swapusage(text: str) -> Tuple[int, int]:
    """'total = 2048.00M  used = 1024.50M  free = ...' -> (total, used) bytes."""
    def val(key: str) -> int:
        m = re.search(key + r"\s*=\s*([\d.]+)([KMGT]?)", text)
        if not m:
            return 0
        mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[m.group(2)]
        return int(float(m.group(1)) * mult)
    return val("total"), val("used")


def parse_ioreg_gpu(text: str) -> Dict[str, Optional[float]]:
    def num(key: str) -> Optional[float]:
        m = re.search(r'"' + re.escape(key) + r'"\s*=\s*(\d+)', text)
        return float(m.group(1)) if m else None
    util = num("Device Utilization %")
    return {
        "util": None if util is None else util / 100,
        "renderer": (num("Renderer Utilization %") or 0) / 100 if util is not None else None,
        "memory": num("In use system memory"),
        "cores": num("gpu-core-count"),
    }


def parse_pmset_batt(text: str) -> Optional[Dict[str, object]]:
    m = re.search(r"(\d+)%;\s*([^;]+);\s*([^\n]*)", text)
    if not m:
        return None
    source = re.search(r"drawing from '([^']+)'", text)
    remaining = re.search(r"(\d+:\d+) remaining", m.group(3))
    return {"percent": int(m.group(1)), "state": m.group(2).strip(),
            "remaining": remaining.group(1) if remaining else None,
            "source": source.group(1) if source else None}


def parse_pmset_therm(text: str) -> str:
    if not text.strip():
        return "unknown"
    speed = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", text)
    if speed and int(speed.group(1)) < 100:
        return f"throttled to {speed.group(1)}%"
    level = re.search(r"(thermal|performance) warning level.*?(\d+)", text, re.I)
    if level and int(level.group(2)) > 0:
        return f"{level.group(1).lower()} warning level {level.group(2)}"
    return "nominal"


def parse_ps(text: str) -> List[Dict[str, object]]:
    """ps -Aceo pid,pcpu,rss,comm -> list of processes."""
    procs = []
    for line in text.splitlines()[1:]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            procs.append({"pid": int(parts[0]), "cpu": float(parts[1]), "rss": int(parts[2]) * 1024,
                          "name": parts[3].strip()})
        except ValueError:
            continue
    return procs


def parse_netstat_ib(text: str) -> Tuple[int, int]:
    """Sum (in, out) bytes over physical interfaces from `netstat -ib`."""
    rx = tx = 0
    seen = set()
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 7 or "<Link#" not in line:
            continue
        name = parts[0].rstrip("*")
        if name in seen or name.startswith(("lo", "utun", "awdl", "llw", "bridge", "gif", "stf", "anpi", "ap")):
            continue
        seen.add(name)
        try:
            rx += int(parts[-5])
            tx += int(parts[-2])
        except ValueError:
            continue
    return rx, tx


def parse_iostat_total_mb(text: str) -> Optional[float]:
    """`iostat -Id` (cumulative KB/t xfrs MB per disk) -> total MB transferred."""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return None
    nums = lines[-1].split()
    try:
        vals = [float(v) for v in nums]
    except ValueError:
        return None
    return sum(vals[i] for i in range(2, len(vals), 3))


def parse_proc_stat(text: str) -> List[Tuple[int, int]]:
    """/proc/stat -> [(busy, total)] per cpu (Linux fallback)."""
    out = []
    for line in text.splitlines():
        if re.match(r"cpu\d+ ", line):
            v = [int(x) for x in line.split()[1:]]
            idle = v[3] + (v[4] if len(v) > 4 else 0)
            out.append((sum(v) - idle, sum(v)))
    return out


# --------------------------------------------------------------------------- per-core CPU via mach

class _MachCPU:
    """Per-core tick counters from host_processor_info (no subprocess, microseconds)."""

    def __init__(self):
        self.ok = False
        if not IS_MAC:
            return
        try:
            lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            self.lib = lib
            lib.mach_host_self.restype = ctypes.c_uint
            self.task = ctypes.c_uint.in_dll(lib, "mach_task_self_").value
            self.host = lib.mach_host_self()
            lib.host_processor_info.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_uint),
                                                ctypes.POINTER(ctypes.POINTER(ctypes.c_int)),
                                                ctypes.POINTER(ctypes.c_uint)]
            lib.vm_deallocate.argtypes = [ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t]
            self.ok = self.ticks() is not None
        except (OSError, AttributeError, ValueError):
            self.ok = False

    def ticks(self) -> Optional[List[Tuple[int, int]]]:
        count = ctypes.c_uint()
        info = ctypes.POINTER(ctypes.c_int)()
        info_count = ctypes.c_uint()
        if self.lib.host_processor_info(self.host, 2, ctypes.byref(count), ctypes.byref(info),
                                        ctypes.byref(info_count)) != 0:
            return None
        out = []
        for c in range(count.value):
            user, system, idle, nice = (info[c * 4 + k] & 0xFFFFFFFF for k in range(4))
            out.append((user + system + nice, user + system + nice + idle))
        addr = ctypes.cast(info, ctypes.c_void_p).value or 0
        self.lib.vm_deallocate(self.task, addr, info_count.value * ctypes.sizeof(ctypes.c_int))
        return out


# --------------------------------------------------------------------------- static machine info

@dataclass
class Machine:
    model: str = ""
    chip: str = ""
    os_name: str = ""
    cores: int = os.cpu_count() or 1
    p_cores: int = 0
    e_cores: int = 0
    memory: int = 0
    boot_time: float = 0.0


def machine_info() -> Machine:
    m = Machine()
    if IS_MAC:
        m.model = _sysctl("hw.model")
        m.chip = _sysctl("machdep.cpu.brand_string")
        m.os_name = "macOS " + _cmd(["sw_vers", "-productVersion"], 3).strip()
        m.memory = int(_sysctl("hw.memsize") or 0)
        m.p_cores = int(_sysctl("hw.perflevel0.logicalcpu") or 0)
        m.e_cores = int(_sysctl("hw.perflevel1.logicalcpu") or 0)
        boot = re.search(r"sec = (\d+)", _sysctl("kern.boottime"))
        m.boot_time = float(boot.group(1)) if boot else 0.0
    else:
        m.os_name = "Linux"
        try:
            with open("/proc/meminfo") as fh:
                m.memory = int(re.search(r"MemTotal:\s+(\d+)", fh.read()).group(1)) * 1024  # type: ignore[union-attr]
            with open("/proc/uptime") as fh:
                m.boot_time = time.time() - float(fh.read().split()[0])
            with open("/proc/cpuinfo") as fh:
                mm = re.search(r"model name\s*:\s*(.+)", fh.read())
                m.chip = mm.group(1).strip() if mm else ""
        except (OSError, AttributeError):
            pass
    return m


# --------------------------------------------------------------------------- live sampler

@dataclass
class Snapshot:
    cpu_total: Optional[float] = None
    cpu_cores: List[float] = field(default_factory=list)
    load: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    mem: Dict[str, int] = field(default_factory=dict)
    pressure: Optional[float] = None      # 0 (relaxed) .. 1 (critical)
    pressure_level: str = "unknown"       # normal | warning | critical
    swap_total: int = 0
    swap_used: int = 0
    gpu: Dict[str, Optional[float]] = field(default_factory=dict)
    disk_total: int = 0
    disk_used: int = 0
    disk_free: int = 0
    disk_read_mbs: Optional[float] = None
    net_rx_bps: Optional[float] = None
    net_tx_bps: Optional[float] = None
    battery: Optional[Dict[str, object]] = None
    thermal: str = "unknown"
    procs: List[Dict[str, object]] = field(default_factory=list)
    updated: float = 0.0


class Monitor:
    """Samples metrics on a background thread; ``snapshot()`` is cheap and thread-safe."""

    HISTORY = 120

    def __init__(self, home: str = os.path.expanduser("~"), interval: float = 1.0):
        self.home = home
        self.interval = interval
        self.machine = machine_info()
        self.mach = _MachCPU()
        self.lock = threading.Lock()
        self.snap = Snapshot()
        self.hist: Dict[str, Deque[float]] = {k: deque(maxlen=self.HISTORY)
                                              for k in ("cpu", "mem", "gpu", "rx", "tx", "disk")}
        self._prev_ticks: Optional[List[Tuple[int, int]]] = None
        self._prev_net: Optional[Tuple[float, int, int]] = None
        self._prev_io: Optional[Tuple[float, float]] = None
        self._tick = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> "Monitor":
        self.sample()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.sample()
            except Exception:  # noqa: BLE001 - never kill the UI because one metric failed
                pass

    def snapshot(self) -> Snapshot:
        with self.lock:
            return self.snap

    # ---- sampling ------------------------------------------------------------
    def _cpu(self, s: Snapshot) -> None:
        ticks = self.mach.ticks() if self.mach.ok else None
        if ticks is None and not IS_MAC:
            try:
                with open("/proc/stat") as fh:
                    ticks = parse_proc_stat(fh.read())
            except OSError:
                ticks = None
        if ticks:
            if self._prev_ticks and len(self._prev_ticks) == len(ticks):
                cores = []
                for (b0, t0), (b1, t1) in zip(self._prev_ticks, ticks):
                    cores.append((b1 - b0) / (t1 - t0) if t1 > t0 else 0.0)
                s.cpu_cores = cores
                s.cpu_total = sum(cores) / len(cores)
            self._prev_ticks = ticks
        elif IS_MAC:  # fallback: sum of ps %cpu
            procs = parse_ps(_cmd(["ps", "-Aceo", "pid,pcpu,rss,comm"]))
            s.cpu_total = min(1.0, sum(float(p["cpu"]) for p in procs) / 100 / self.machine.cores)
        try:
            s.load = os.getloadavg()
        except OSError:
            pass

    def _memory(self, s: Snapshot) -> None:
        total = self.machine.memory
        if IS_MAC:
            s.mem = memory_breakdown(parse_vm_stat(_cmd(["vm_stat"])), total)
            level = _sysctl("kern.memorystatus_level")
            if level.isdigit():
                s.pressure = max(0.0, min(1.0, 1 - int(level) / 100))
            vm_level = _sysctl("kern.memorystatus_vm_pressure_level")
            s.pressure_level = {"1": "normal", "2": "warning", "4": "critical"}.get(vm_level, "normal")
            s.swap_total, s.swap_used = parse_swapusage(_sysctl("vm.swapusage"))
        else:
            try:
                with open("/proc/meminfo") as fh:
                    info = {k: int(v) * 1024 for k, v in re.findall(r"(\w+):\s+(\d+)", fh.read())}
                avail = info.get("MemAvailable", 0)
                s.mem = {"total": total, "used": total - avail, "app": total - avail, "wired": 0,
                         "compressed": 0, "cached": info.get("Cached", 0), "free": info.get("MemFree", 0)}
                s.pressure = (total - avail) / total if total else None
                s.pressure_level = "normal" if (s.pressure or 0) < 0.8 else "warning"
                s.swap_total = info.get("SwapTotal", 0)
                s.swap_used = s.swap_total - info.get("SwapFree", 0)
            except OSError:
                pass

    def _disk(self, s: Snapshot) -> None:
        du = shutil.disk_usage(self.home)
        s.disk_total, s.disk_used, s.disk_free = du.total, du.used, du.free
        now = time.time()
        total_mb = parse_iostat_total_mb(_cmd(["iostat", "-Id"], 3)) if IS_MAC else self._linux_io_mb()
        if total_mb is not None:
            if self._prev_io:
                dt = now - self._prev_io[0]
                s.disk_read_mbs = max(0.0, (total_mb - self._prev_io[1]) / dt) if dt > 0 else None
            self._prev_io = (now, total_mb)

    @staticmethod
    def _linux_io_mb() -> Optional[float]:
        try:
            with open("/proc/diskstats") as fh:
                sectors = sum(int(p[5]) + int(p[9]) for p in (l.split() for l in fh) if len(p) > 9
                              and re.match(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+)$", p[2]))
            return sectors * 512 / 1e6
        except (OSError, ValueError):
            return None

    def _network(self, s: Snapshot) -> None:
        now = time.time()
        if IS_MAC:
            rx, tx = parse_netstat_ib(_cmd(["netstat", "-ib"], 3))
        else:
            rx = tx = 0
            try:
                with open("/proc/net/dev") as fh:
                    for line in fh.readlines()[2:]:
                        name, data = line.split(":", 1)
                        if name.strip() != "lo":
                            v = data.split()
                            rx += int(v[0])
                            tx += int(v[8])
            except (OSError, ValueError, IndexError):
                return
        if self._prev_net:
            dt = now - self._prev_net[0]
            if dt > 0:
                s.net_rx_bps = max(0.0, (rx - self._prev_net[1]) / dt)
                s.net_tx_bps = max(0.0, (tx - self._prev_net[2]) / dt)
        self._prev_net = (now, rx, tx)

    def _slow(self, s: Snapshot) -> None:
        if IS_MAC:
            s.gpu = parse_ioreg_gpu(_cmd(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"], 3))
            if self._tick % 10 == 0:
                s.battery = parse_pmset_batt(_cmd(["pmset", "-g", "batt"], 3))
                s.thermal = parse_pmset_therm(_cmd(["pmset", "-g", "therm"], 3))
        s.procs = parse_ps(_cmd(["ps", "-Aceo", "pid,pcpu,rss,comm"] if IS_MAC else
                                ["ps", "-eo", "pid,pcpu,rss,comm"], 3))

    def sample(self) -> None:
        prev = self.snapshot()
        s = Snapshot(gpu=prev.gpu, battery=prev.battery, thermal=prev.thermal, procs=prev.procs)
        self._cpu(s)
        self._memory(s)
        self._network(s)
        if self._tick % 2 == 0:
            self._disk(s)
            self._slow(s)
        else:
            s.disk_total, s.disk_used, s.disk_free = prev.disk_total, prev.disk_used, prev.disk_free
            s.disk_read_mbs = prev.disk_read_mbs
        s.updated = time.time()
        self._tick += 1
        with self.lock:
            self.snap = s
            if s.cpu_total is not None:
                self.hist["cpu"].append(s.cpu_total)
            if s.mem.get("total"):
                self.hist["mem"].append(s.mem["used"] / s.mem["total"])
            if s.gpu.get("util") is not None:
                self.hist["gpu"].append(s.gpu["util"])  # type: ignore[arg-type]
            if s.net_rx_bps is not None:
                self.hist["rx"].append(s.net_rx_bps)
                self.hist["tx"].append(s.net_tx_bps or 0.0)
            if s.disk_read_mbs is not None:
                self.hist["disk"].append(s.disk_read_mbs)

    def history(self, key: str) -> List[float]:
        with self.lock:
            return list(self.hist[key])


def fmt_rate(bps: Optional[float]) -> str:
    if bps is None:
        return "   --"
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1000:
            return f"{bps:5.0f} {unit}" if unit == "B/s" else f"{bps:5.1f} {unit}"
        bps /= 1000
    return f"{bps:5.1f} TB/s"


def fmt_uptime(boot: float) -> str:
    if not boot:
        return "?"
    s = int(time.time() - boot)
    d, h, m = s // 86400, s // 3600 % 24, s // 60 % 60
    return f"{d}d {h}h" if d else f"{h}h {m}m"
