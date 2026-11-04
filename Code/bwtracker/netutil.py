"""Small helpers: address formatting, loopback detection, NIC counters, /proc."""
from __future__ import annotations

import ipaddress
import os
import socket
from typing import Dict, Optional, Tuple

_V4MAPPED_PREFIX = b"\x00" * 10 + b"\xff\xff"


def addr_from_bytes(raw: bytes) -> str:
    """Kernel side stores every address as 16 bytes (IPv4 as ::ffff:a.b.c.d)."""
    raw = bytes(raw)
    if raw[:12] == _V4MAPPED_PREFIX:
        return socket.inet_ntop(socket.AF_INET, raw[12:])
    if raw == b"\x00" * 16:
        return "0.0.0.0"
    return socket.inet_ntop(socket.AF_INET6, raw)


def is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def fmt_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def fmt_rate(bps: float) -> str:
    return fmt_bytes(bps) + "/s"


def fmt_bits(bps: float) -> str:
    """bytes/s -> human bits/s (network people think in Mbit/s)."""
    b = bps * 8
    for unit in ("bit/s", "kbit/s", "Mbit/s", "Gbit/s"):
        if abs(b) < 1000.0 or unit == "Gbit/s":
            return f"{b:.1f} {unit}" if unit != "bit/s" else f"{b:.0f} {unit}"
        b /= 1000.0
    return f"{b:.1f} Gbit/s"


def endpoint(addr: str, port: int) -> str:
    return f"[{addr}]:{port}" if ":" in addr else f"{addr}:{port}"


def read_nic_counters() -> Dict[str, Tuple[int, int, int, int, int, int]]:
    """/proc/net/dev -> {iface: (rx_bytes, rx_packets, rx_drop, tx_bytes, tx_packets, tx_drop)}.
    These are the ground-truth byte counters of each network interface
    (they include all headers and all retransmissions)."""
    out: Dict[str, Tuple[int, int, int, int, int, int]] = {}
    try:
        with open("/proc/net/dev") as f:
            lines = f.readlines()[2:]
    except OSError:
        return out
    for line in lines:
        name, data = line.split(":", 1)
        v = data.split()
        out[name.strip()] = (int(v[0]), int(v[1]), int(v[3]), int(v[8]), int(v[9]), int(v[11]))
    return out


def read_snmp() -> Dict[str, Dict[str, int]]:
    """/proc/net/snmp -> {'Tcp': {...}, 'Udp': {...}} system-wide counters
    (RetransSegs, InErrors, RcvbufErrors...)."""
    out: Dict[str, Dict[str, int]] = {}
    try:
        with open("/proc/net/snmp") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for i in range(0, len(lines) - 1, 2):
        hdr, val = lines[i].split(), lines[i + 1].split()
        if hdr[0] != val[0]:
            continue
        proto = hdr[0].rstrip(":")
        out[proto] = {}
        for k, v in zip(hdr[1:], val[1:]):
            try:
                out[proto][k] = int(v)
            except ValueError:
                pass
    return out


_cmdline_cache: Dict[int, str] = {}


def proc_cmdline(pid: int) -> str:
    if pid in _cmdline_cache:
        return _cmdline_cache[pid]
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except OSError:
        cmd = ""
    if len(_cmdline_cache) > 4096:
        _cmdline_cache.clear()
    _cmdline_cache[pid] = cmd
    return cmd


def pid_alive(pid: int) -> bool:
    return os.path.exists(f"/proc/{pid}")


def self_cpu_seconds() -> float:
    try:
        with open("/proc/self/stat") as f:
            parts = f.read().rsplit(")", 1)[1].split()
        ticks = int(parts[11]) + int(parts[12])  # utime + stime
        return ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return 0.0


def detect_platform() -> str:
    try:
        rel = os.uname().release.lower()
    except AttributeError:
        return "unknown"
    if "microsoft" in rel or "wsl" in rel:
        return "wsl2"
    try:
        with open("/proc/device-tree/model", "rb") as f:
            model = f.read().decode(errors="replace").strip("\x00 \n")
        if "raspberry" in model.lower():
            return model
    except OSError:
        pass
    return "linux"


def ip_matches(addr: str, cidr: Optional[str]) -> bool:
    if not cidr:
        return True
    try:
        return ipaddress.ip_address(addr) in ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return addr == cidr


_local_cache = {"t": 0.0, "addrs": frozenset()}


def local_addresses() -> frozenset:
    """All IP addresses owned by this machine. Traffic to any of them (not only
    127.0.0.1) is routed through the loopback device and never hits the NIC."""
    import time as _t
    if _t.monotonic() - _local_cache["t"] < 10:
        return _local_cache["addrs"]
    addrs = set()
    try:  # IPv4: entries marked "/32 host LOCAL" in the FIB
        with open("/proc/net/fib_trie") as f:
            last_ip = None
            for line in f:
                s = line.strip()
                if s.startswith("|--") or s.startswith("+--"):
                    last_ip = s.split()[1].split("/")[0]
                elif "/32 host LOCAL" in s and last_ip:
                    addrs.add(last_ip)
    except OSError:
        pass
    try:  # IPv6
        with open("/proc/net/if_inet6") as f:
            for line in f:
                h = line.split()[0]
                addrs.add(socket.inet_ntop(socket.AF_INET6, bytes.fromhex(h)))
    except OSError:
        pass
    _local_cache["t"], _local_cache["addrs"] = _t.monotonic(), frozenset(addrs)
    return _local_cache["addrs"]


def is_local_traffic(laddr: str, raddr: str) -> bool:
    """True if the remote end is this same machine (loopback path)."""
    if is_loopback(raddr):
        return True
    return raddr != "0.0.0.0" and raddr in local_addresses()
