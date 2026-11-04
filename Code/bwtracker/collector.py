"""User-space half of the tracker.

  KernelProbes   loads tracker.bpf.c with BCC, attaches the probes, and reads
                 the BPF maps (cumulative counters) + the perf event buffer.
  Aggregator     turns cumulative counters into per-interval deltas and
                 rates, joins TCP wire statistics to flows, and groups flows
                 per process and per remote IP. Pure Python: unit-testable.
  Tracker        glues both together; `poll()` returns one Snapshot per
                 interval. Every UI (terminal, web, Prometheus, SQLite) is a
                 consumer of these snapshots.
"""
from __future__ import annotations

import collections
import ctypes
import os
import sys
import time
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Iterable, List, Optional, Tuple

from . import netutil
from .model import (PROTO_NAME, Event, FlowRow, ProcRow, RawFlow, RawTcp,
                    Snapshot, summarize)

BPF_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bpf", "tracker.bpf.c")

# (kind, kernel function, BPF handler, required?, why)
PROBES = [
    ("kprobe",    "tcp_sendmsg",                "on_tcp_send_enter",       True,  "TCP bytes sent by apps"),
    ("kretprobe", "tcp_sendmsg",                "on_tcp_send_return",      True,  "TCP bytes sent by apps"),
    ("kprobe",    "tcp_sendpage",               "on_tcp_send_enter",       False, "sendfile() on kernels < 6.5"),
    ("kretprobe", "tcp_sendpage",               "on_tcp_send_return",      False, "sendfile() on kernels < 6.5"),
    ("kprobe",    "tcp_cleanup_rbuf",           "on_tcp_cleanup_rbuf",     True,  "TCP bytes read by apps"),
    ("kprobe",    "tcp_rcv_established",        "on_tcp_rcv_established",  False, "live TCP wire/RTT stats"),
    ("kprobe",    "udp_sendmsg",                "on_udp_send_enter",       True,  "UDP bytes sent (IPv4)"),
    ("kretprobe", "udp_sendmsg",                "on_udp_send_return",      True,  "UDP bytes sent (IPv4)"),
    ("kprobe",    "udpv6_sendmsg",              "on_udpv6_send_enter",     False, "UDP bytes sent (IPv6)"),
    ("kretprobe", "udpv6_sendmsg",              "on_udpv6_send_return",    False, "UDP bytes sent (IPv6)"),
    ("kprobe",    "skb_consume_udp",            "on_skb_consume_udp",      True,  "UDP bytes read by apps"),
    ("kprobe",    "__udp_enqueue_schedule_skb", "on_udp_enqueue_enter",    False, "UDP receive-buffer drops"),
    ("kretprobe", "__udp_enqueue_schedule_skb", "on_udp_enqueue_return",   False, "UDP receive-buffer drops"),
]

EVENT_KIND = {1: "RETRANS", 2: "UDP_DROP"}


class TrackerError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Kernel side access (needs root + BCC)
# --------------------------------------------------------------------------

def import_bcc():
    # BCC's Python module is installed by the distro (python3-bpfcc), outside a venv.
    for p in ("/usr/lib/python3/dist-packages", "/usr/local/lib/python3/dist-packages"):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.append(p)
    try:
        import bcc  # noqa: F401
        from bcc import BPF
    except ImportError as e:
        raise TrackerError(
            "BCC python module not found. Install it with:\n"
            "  sudo apt install python3-bpfcc bpfcc-tools linux-headers-$(uname -r)\n"
            "then run:  sudo ./bwtrack doctor") from e
    return BPF


def _bcc_version_str() -> str:
    try:
        import bcc
        return str(getattr(bcc, "__version__", "unknown")).replace("/", "_")
    except Exception:
        return "unknown"


def _bcc_version() -> tuple:
    try:
        import bcc
        return tuple(int(x) for x in bcc.__version__.split("+")[0].split(".")[:2])
    except Exception:
        return (99, 0)


class KernelProbes:
    def __init__(self, max_flows: int = 16384, snap_interval_ms: int = 100,
                 kretprobe_maxactive: int = 512, verbose: bool = False):
        self.max_flows = max_flows
        self.snap_interval_ms = snap_interval_ms
        self.maxactive = kretprobe_maxactive
        self.verbose = verbose
        self.b = None
        self.attached: List[Tuple[str, str]] = []
        self.skipped: List[Tuple[str, str, str]] = []
        self.event_queue: Deque[Event] = collections.deque(maxlen=5000)
        self.perf_lost = 0
        self._flow_keys: Dict[tuple, object] = {}

    # ---------------------------------------------------------------- load
    def load(self, log: Callable[[str], None] = lambda s: None) -> None:
        if os.geteuid() != 0:
            raise TrackerError("eBPF needs root. Run with sudo.")
        BPF = import_bcc()
        cflags = [
            f"-DMAX_FLOWS={self.max_flows}",
            f"-DSNAP_INTERVAL_NS={self.snap_interval_ms * 1_000_000}ULL",
            "-Wno-macro-redefined", "-Wno-incompatible-pointer-types-discards-qualifiers",
        ]
        log("Compiling eBPF program against the running kernel's headers (BCC/clang)...")
        t0 = time.time()
        self.b = self._compile(BPF, cflags, log)
        log(f"Compiled in {time.time() - t0:.1f}s. Attaching probes...")

        # One pass over available_filter_functions (it is a big file).
        wanted = sorted({p[1] for p in PROBES})
        available = {f.decode() if isinstance(f, bytes) else f for f in
                     BPF.get_kprobe_functions(("^(" + "|".join(wanted) + ")$").encode())}
        for kind, fn, handler, required, why in PROBES:
            if fn not in available:
                if required:
                    raise TrackerError(f"Kernel function {fn} not found - cannot track {why}.")
                self.skipped.append((kind, fn, "not in this kernel"))
                continue
            try:
                if kind == "kprobe":
                    self.b.attach_kprobe(event=fn, fn_name=handler)
                else:
                    self.b.attach_kretprobe(event=fn, fn_name=handler, maxactive=self.maxactive)
                self.attached.append((kind, fn))
            except Exception as e:
                if required:
                    raise TrackerError(f"Failed to attach {kind} {fn}: {e}") from e
                self.skipped.append((kind, fn, str(e)))
        # Tracepoints (TRACEPOINT_PROBE in the C file) are attached by BCC automatically.
        self.attached.append(("tracepoint", "tcp:tcp_retransmit_skb"))
        self.attached.append(("tracepoint", "sock:inet_sock_set_state"))

        self.b["events"].open_perf_buffer(self._on_event, page_cnt=64, lost_cb=self._on_lost)
        log(f"Attached {len(self.attached)} probes" +
            (f", skipped {len(self.skipped)} optional" if self.skipped else ""))

    def _compile(self, BPF, cflags, log):
        """Compile tracker.bpf.c. If BCC's bundled bpf.h is older than this
        kernel's headers, add the missing definitions (see compat.py) and retry."""
        from . import compat
        with open(BPF_SOURCE) as f:
            source = f.read()
        version = _bcc_version_str()
        cache = compat.cache_path(version)
        shim = ""
        try:
            with open(cache) as f:
                shim = f.read()
        except OSError:
            pass
        uapi = None
        done: set = set()
        last_diag, last_err = "", None
        for attempt in range(8):
            text = source.replace(compat.MARKER, compat.MARKER + "\n" + shim)
            with compat.capture_fd2() as cap:
                try:
                    b = BPF(text=text, cflags=cflags)
                except Exception as e:  # BCC raises a plain Exception on compile errors
                    b, last_err = None, e
            if b is not None:
                if shim:
                    try:
                        os.makedirs(os.path.dirname(cache), exist_ok=True)
                        with open(cache, "w") as f:
                            f.write(shim)
                    except OSError:
                        pass
                return b
            last_diag = cap["text"]
            if uapi is None:
                uapi = compat.read_uapi()
            extra = compat.definitions_for(last_diag, uapi, done)
            if not extra:
                if shim and attempt == 0:      # stale cache: start from scratch
                    shim = ""
                    continue
                break
            names = sorted(n for n in done)
            log(f"  BCC's bpf.h is older than this kernel's headers; adding {len(names)} "
                f"definition(s) from the kernel's uapi/linux/bpf.h and retrying...")
            shim += extra + "\n"
        raise TrackerError(
            f"Could not compile the eBPF program ({last_err}).\n"
            f"First compiler errors:\n{compat.error_lines(last_diag)}\n\n"
            "If these mention missing headers, run:  sudo ./bwtrack doctor")

    def close(self) -> None:
        if self.b is not None:
            try:
                self.b.cleanup()
            except Exception:
                pass
            self.b = None

    # ---------------------------------------------------------------- events
    def _on_lost(self, count):
        self.perf_lost += count

    def _on_event(self, cpu, data, size):
        e = self.b["events"].event(data)
        proto = "TCP" if e.type == 1 else "UDP"
        self.event_queue.append(Event(
            ts=time.time(), kind=EVENT_KIND.get(e.type, str(e.type)), tgid=e.tgid,
            comm=e.comm.decode("utf-8", "replace"), proto=proto,
            laddr=netutil.addr_from_bytes(e.laddr), lport=e.lport,
            raddr=netutil.addr_from_bytes(e.raddr), rport=e.rport, value=e.value))

    def poll_events(self, timeout_ms: int) -> None:
        self.b.perf_buffer_poll(timeout=max(0, int(timeout_ms)))

    def drain_events(self) -> List[Event]:
        out = list(self.event_queue)
        self.event_queue.clear()
        return out

    # ---------------------------------------------------------------- maps
    def read_flows(self) -> List[RawFlow]:
        out = []
        self._flow_keys = {}
        for k, v in self._items(self.b["flows"]):
            rf = RawFlow(
                sk=k.sk, tgid=k.tgid, family=k.family, proto=k.proto,
                laddr=netutil.addr_from_bytes(k.laddr), lport=k.lport,
                raddr=netutil.addr_from_bytes(k.raddr), rport=k.rport,
                tx_bytes=v.tx_bytes, rx_bytes=v.rx_bytes, tx_calls=v.tx_calls,
                rx_calls=v.rx_calls, first_ns=v.first_ns, last_ns=v.last_ns,
                comm=v.comm.decode("utf-8", "replace"),
                closed_sent=v.closed_sent, closed_retrans=v.closed_retrans, closed_acked=v.closed_acked,
                closed_received=v.closed_received, closed_segs=v.closed_segs)
            self._flow_keys[rf.key] = k
            out.append(rf)
        return out

    def read_tcp(self) -> Dict[int, RawTcp]:
        out = {}
        for k, v in self._items(self.b["tcp_snaps"]):
            out[k.value] = RawTcp(
                sk=k.value,
                bytes_sent=max(0, v.bytes_sent - v.base_sent),
                bytes_retrans=max(0, v.bytes_retrans - v.base_retrans),
                bytes_acked=max(0, v.bytes_acked - v.base_acked),
                bytes_received=max(0, v.bytes_received - v.base_received),
                total_retrans=(v.total_retrans - v.base_total_retrans) & 0xFFFFFFFF,
                retrans_events=v.retrans_events, srtt_us=v.srtt_us, snd_cwnd=v.snd_cwnd,
                mss=v.mss, lost_out=v.lost_out, segs_out=v.segs_out, segs_in=v.segs_in,
                state=v.state, update_ns=v.update_ns, born_ns=v.born_ns)
        return out

    def read_udp_drops(self) -> Dict[int, int]:
        return {k.value: v.value for k, v in self._items(self.b["udp_drops"])}

    def delete_flows(self, keys: Iterable[tuple], sockets: Iterable[int]) -> None:
        flows = self.b["flows"]
        for key in keys:
            ck = self._flow_keys.get(key)
            if ck is not None:
                try:
                    del flows[ck]
                except KeyError:
                    pass
        for name in ("tcp_snaps", "sk_owner", "udp_drops", "rx_seq"):
            t = self.b[name]
            for sk in sockets:
                try:
                    del t[t.Key(sk)]
                except KeyError:
                    pass

    @staticmethod
    def _items(table):
        try:
            return list(table.items_lookup_batch())
        except Exception:
            return list(table.items())

    # ---------------------------------------------------------------- health
    def kprobe_misses(self) -> int:
        """kretprobes can MISS calls when too many are in flight at once
        (maxactive). The kernel reports misses in kprobe_profile."""
        tag = f"bcc_{os.getpid()}"
        for path in ("/sys/kernel/tracing/kprobe_profile", "/sys/kernel/debug/tracing/kprobe_profile"):
            try:
                with open(path) as f:
                    return sum(int(line.split()[2]) for line in f if tag in line)
            except (OSError, IndexError, ValueError):
                continue
        return 0

    def bpf_runtime_ns(self) -> Tuple[int, int]:
        """Total (run_time_ns, run_cnt) of our BPF programs; only non-zero when
        sysctl kernel.bpf_stats_enabled=1 (see --bpf-stats)."""
        total_ns = total_cnt = 0
        for fn in list(self.b.funcs.values()):
            try:
                with open(f"/proc/self/fdinfo/{fn.fd}") as f:
                    for line in f:
                        if line.startswith("run_time_ns:"):
                            total_ns += int(line.split()[1])
                        elif line.startswith("run_cnt:"):
                            total_cnt += int(line.split()[1])
            except (OSError, ValueError):
                pass
        return total_ns, total_cnt


# --------------------------------------------------------------------------
# Aggregation (pure Python)
# --------------------------------------------------------------------------

@dataclass
class _FlowState:
    raw: RawFlow
    prev_tx: int = 0
    prev_rx: int = 0
    d_tx: int = 0
    d_rx: int = 0
    wire_tx: int = 0
    acked: int = 0
    wire_rx: int = 0
    retrans_bytes: int = 0
    retrans_segs: int = 0
    d_wire_tx: int = 0
    d_acked: int = 0
    d_retrans_bytes: int = 0
    d_retrans_segs: int = 0
    udp_drops: int = 0
    d_udp_drops: int = 0
    srtt_us: int = 0
    cwnd: int = 0
    mss: int = 0
    lost_out: int = 0
    state: int = 0
    last_active_ns: int = 0
    prev_closed: tuple = (0, 0, 0, 0, 0)
    seen_before: bool = False


@dataclass
class _TcpState:
    prev: Optional[RawTcp] = None


def _delta(cur: int, prev: int) -> int:
    """Counter delta; a counter that went backwards was reset (socket reused)."""
    return cur - prev if cur >= prev else cur


class Aggregator:
    def __init__(self, idle_timeout: float = 30.0):
        self.idle_timeout_ns = int(idle_timeout * 1e9)
        self.flows: Dict[tuple, _FlowState] = {}
        self.tcp: Dict[int, _TcpState] = {}
        self.drops_prev: Dict[int, int] = {}
        # cumulative per (tgid, proto, loopback) - survives sockets closing
        self.proc_acc: Dict[Tuple[int, str, bool], Dict[str, int]] = {}
        self.proc_comm: Dict[int, str] = {}
        self.proc_dead_since: Dict[int, float] = {}

    def step(self, now_ns: int, interval: float, raw_flows: List[RawFlow],
             raw_tcp: Dict[int, RawTcp], raw_drops: Dict[int, int]):
        """Returns (flow_rows, stale_flow_keys, stale_sockets)."""
        seen = set()
        for rf in raw_flows:
            st = self.flows.get(rf.key)
            if st is None:
                st = self.flows[rf.key] = _FlowState(raw=rf)
            else:
                st.seen_before = True
            st.raw = rf
            st.d_tx = _delta(rf.tx_bytes, st.prev_tx)
            st.d_rx = _delta(rf.rx_bytes, st.prev_rx)
            st.prev_tx, st.prev_rx = rf.tx_bytes, rf.rx_bytes
            st.d_wire_tx = st.d_acked = st.d_retrans_bytes = st.d_retrans_segs = st.d_udp_drops = 0
            st.last_active_ns = max(st.last_active_ns, rf.last_ns)
            seen.add(rf.key)
        for key in list(self.flows):
            if key not in seen:          # deleted by us or evicted from the LRU map
                del self.flows[key]

        # 1) Sockets that CLOSED since the last read: the kernel added their
        # final wire counters to the flow (closed_*). Part of that may already
        # have been counted from the live snapshot last time; count the rest.
        for st in self.flows.values():
            r = st.raw
            if r.proto != 6:
                continue
            cur = (r.closed_sent, r.closed_acked, r.closed_retrans, r.closed_segs, r.closed_received)
            d = [_delta(c, p) for c, p in zip(cur, st.prev_closed)]
            st.prev_closed = cur
            if not any(d):
                continue
            ts = self.tcp.get(r.sk)
            live_prev = ts.prev if ts is not None else None
            gone = r.sk not in raw_tcp or (live_prev is not None and raw_tcp[r.sk].born_ns != live_prev.born_ns)
            if st.seen_before and live_prev is not None and gone:
                already = (live_prev.bytes_sent, live_prev.bytes_acked, live_prev.bytes_retrans,
                           live_prev.total_retrans, live_prev.bytes_received)
                d = [max(0, x - y) for x, y in zip(d, already)]
                ts.prev = None           # this socket is finished
            self._add_wire(st, *d, rt=None)

        # 2) Live sockets. Index flows by socket. The kernel may recycle the
        # address of a closed socket for a new one, so several flows can share
        # an `sk` value; the live counters belong to the most recently active
        # flow that was used AFTER that socket was born.
        by_sk: Dict[int, _FlowState] = {}
        for st in self.flows.values():
            rt = raw_tcp.get(st.raw.sk)
            if rt is not None and st.raw.proto == 6 and st.raw.last_ns < rt.born_ns:
                continue
            cur = by_sk.get(st.raw.sk)
            if cur is None or (st.raw.last_ns, st.raw.tx_bytes + st.raw.rx_bytes) > \
                    (cur.raw.last_ns, cur.raw.tx_bytes + cur.raw.rx_bytes):
                by_sk[st.raw.sk] = st

        for sk, rt in raw_tcp.items():
            ts = self.tcp.setdefault(sk, _TcpState())
            p = ts.prev
            if p is not None and p.born_ns != rt.born_ns:
                p = None          # a new socket reused this address: start from zero
            d_sent = _delta(rt.bytes_sent, p.bytes_sent if p else 0)
            d_acked = _delta(rt.bytes_acked, p.bytes_acked if p else 0)
            d_rtx = _delta(rt.bytes_retrans, p.bytes_retrans if p else 0)
            d_segs = _delta(rt.total_retrans, p.total_retrans if p else 0)
            d_recv = _delta(rt.bytes_received, p.bytes_received if p else 0)
            st = by_sk.get(sk)
            if st is None or st.raw.proto != 6:
                continue          # not (yet) used by a process: count it once it is
            ts.prev = rt
            self._add_wire(st, d_sent, d_acked, d_rtx, d_segs, d_recv, rt=rt)
        for sk in list(self.tcp):
            if sk not in raw_tcp:
                del self.tcp[sk]

        for sk, cnt in raw_drops.items():
            d = _delta(cnt, self.drops_prev.get(sk, 0))
            self.drops_prev[sk] = cnt
            st = by_sk.get(sk)
            if st is not None and d:
                st.d_udp_drops = d
                st.udp_drops += d
                st.last_active_ns = max(st.last_active_ns, now_ns)
        for sk in list(self.drops_prev):
            if sk not in raw_drops:
                del self.drops_prev[sk]

        # Cumulative per-process accounting
        for st in self.flows.values():
            if not (st.d_tx or st.d_rx or st.d_wire_tx or st.d_retrans_segs or st.d_udp_drops):
                continue
            r = st.raw
            proto = PROTO_NAME.get(r.proto, str(r.proto))
            loop = netutil.is_local_traffic(r.laddr, r.raddr)
            acc = self.proc_acc.setdefault((r.tgid, proto, loop), collections.defaultdict(int))
            acc["tx_bytes"] += st.d_tx
            acc["rx_bytes"] += st.d_rx
            acc["wire_tx_bytes"] += st.d_wire_tx
            acc["retrans_bytes"] += st.d_retrans_bytes
            acc["retrans_segs"] += st.d_retrans_segs
            acc["udp_drops"] += st.d_udp_drops
            self.proc_comm[r.tgid] = r.comm

        rows = self._group_rows(now_ns, interval)

        stale_keys, stale_socks = [], set()
        for key, st in list(self.flows.items()):
            if now_ns - st.last_active_ns > self.idle_timeout_ns:
                stale_keys.append(key)
                stale_socks.add(st.raw.sk)
                del self.flows[key]
        # sockets still used by a remaining flow must keep their kernel state
        stale_socks -= {st.raw.sk for st in self.flows.values()}

        self._expire_dead_processes()
        return rows, stale_keys, stale_socks

    @staticmethod
    def _add_wire(st, d_sent, d_acked, d_rtx, d_segs, d_recv, rt=None):
        st.d_wire_tx += d_sent
        st.d_acked += d_acked
        st.d_retrans_bytes += d_rtx
        st.d_retrans_segs += d_segs
        st.wire_tx += d_sent
        st.acked += d_acked
        st.retrans_bytes += d_rtx
        st.retrans_segs += d_segs
        st.wire_rx += d_recv
        if rt is not None:
            st.srtt_us, st.cwnd, st.mss, st.lost_out, st.state = rt.srtt_us, rt.snd_cwnd, rt.mss, rt.lost_out, rt.state
            if d_sent or d_acked or d_recv:
                st.last_active_ns = max(st.last_active_ns, rt.update_ns)

    def _group_rows(self, now_ns: int, interval: float) -> List[FlowRow]:
        """One row per (process, protocol, remote IP)."""
        groups: Dict[tuple, FlowRow] = {}
        lports: Dict[tuple, set] = collections.defaultdict(set)
        rports: Dict[tuple, set] = collections.defaultdict(set)
        socks: Dict[tuple, set] = collections.defaultdict(set)
        iv = interval if interval > 0 else 1.0
        for st in self.flows.values():
            r = st.raw
            proto = PROTO_NAME.get(r.proto, str(r.proto))
            gk = (r.tgid, proto, r.raddr)
            row = groups.get(gk)
            if row is None:
                loop = netutil.is_local_traffic(r.laddr, r.raddr)
                row = groups[gk] = FlowRow(tgid=r.tgid, comm=r.comm, proto=proto, laddr=r.laddr,
                                           lport=r.lport, raddr=r.raddr, rport=r.rport,
                                           loopback=loop, sockets=0, idle_s=1e9)
            lports[gk].add(r.lport)
            rports[gk].add(r.rport)
            socks[gk].add(r.sk)
            row.tx_bytes += r.tx_bytes
            row.rx_bytes += r.rx_bytes
            row.d_tx += st.d_tx
            row.d_rx += st.d_rx
            row.wire_tx_bytes += st.wire_tx
            row.d_wire_tx += st.d_wire_tx
            row.acked_bytes += st.acked
            row.wire_rx_bytes += st.wire_rx
            row.retrans_bytes += st.retrans_bytes
            row.d_retrans_bytes += st.d_retrans_bytes
            row.retrans_segs += st.retrans_segs
            row.d_retrans_segs += st.d_retrans_segs
            row.udp_drops += st.udp_drops
            row.d_udp_drops += st.d_udp_drops
            row.acked_rate += st.d_acked / iv
            if st.d_wire_tx or st.d_tx or st.d_rx or not row.srtt_us:
                row.srtt_us = max(row.srtt_us, st.srtt_us)
                row.cwnd = max(row.cwnd, st.cwnd)
                row.mss = max(row.mss, st.mss)
            row.lost_out += st.lost_out
            row.state = st.state
            row.idle_s = min(row.idle_s, max(0.0, (now_ns - st.last_active_ns) / 1e9))
        for gk, row in groups.items():
            row.sockets = len(socks[gk])
            row.lport = next(iter(lports[gk])) if len(lports[gk]) == 1 else 0
            row.rport = next(iter(rports[gk])) if len(rports[gk]) == 1 else 0
            row.tx_rate = row.d_tx / iv
            row.rx_rate = row.d_rx / iv
            row.wire_tx_rate = row.d_wire_tx / iv
            row.retrans_rate = row.d_retrans_bytes / iv
        return list(groups.values())

    def _expire_dead_processes(self):
        now = time.monotonic()
        live_tgids = {st.raw.tgid for st in self.flows.values()}
        for tgid in list(self.proc_comm):
            if tgid in live_tgids or netutil.pid_alive(tgid):
                self.proc_dead_since.pop(tgid, None)
                continue
            since = self.proc_dead_since.setdefault(tgid, now)
            if now - since > 300:
                for k in [k for k in self.proc_acc if k[0] == tgid]:
                    del self.proc_acc[k]
                self.proc_comm.pop(tgid, None)
                self.proc_dead_since.pop(tgid, None)


def build_proc_rows(flows: List[FlowRow], acc_by_tgid: Optional[Dict[int, ProcRow]] = None,
                    rates_only_from_flows: bool = False) -> List[ProcRow]:
    """Aggregate flow rows per process. Rates come from the flows; cumulative
    totals come from `acc_by_tgid` when given (they survive closed sockets)."""
    procs: Dict[int, ProcRow] = {}
    remotes: Dict[int, set] = collections.defaultdict(set)
    protos: Dict[int, set] = collections.defaultdict(set)
    for f in flows:
        p = procs.get(f.tgid)
        if p is None:
            p = procs[f.tgid] = ProcRow(tgid=f.tgid, comm=f.comm, cmdline=netutil.proc_cmdline(f.tgid))
        p.flows += f.sockets
        remotes[f.tgid].add(f.raddr)
        protos[f.tgid].add(f.proto)
        p.tx_rate += f.tx_rate
        p.rx_rate += f.rx_rate
        p.wire_tx_rate += f.wire_tx_rate
        p.retrans_rate += f.retrans_rate
        p.d_wire_tx += f.d_wire_tx
        p.d_retrans_bytes += f.d_retrans_bytes
        p.max_srtt_us = max(p.max_srtt_us, f.srtt_us if f.idle_s < 5 else 0)
        p.tx_bytes += f.tx_bytes
        p.rx_bytes += f.rx_bytes
        p.wire_tx_bytes += f.wire_tx_bytes
        p.retrans_bytes += f.retrans_bytes
        p.retrans_segs += f.retrans_segs
        p.udp_drops += f.udp_drops
    for tgid, p in procs.items():
        p.remotes = len(remotes[tgid])
        p.protos = sorted(protos[tgid])
        if acc_by_tgid and tgid in acc_by_tgid:
            a = acc_by_tgid[tgid]
            # never show less than what the live flows already prove
            p.tx_bytes = max(p.tx_bytes, a.tx_bytes)
            p.rx_bytes = max(p.rx_bytes, a.rx_bytes)
            p.wire_tx_bytes = max(p.wire_tx_bytes, a.wire_tx_bytes)
            p.retrans_bytes = max(p.retrans_bytes, a.retrans_bytes)
            p.retrans_segs = max(p.retrans_segs, a.retrans_segs)
            p.udp_drops = max(p.udp_drops, a.udp_drops)
    if acc_by_tgid and not rates_only_from_flows:
        for tgid, a in acc_by_tgid.items():
            if tgid not in procs:
                procs[tgid] = a
    return sorted(procs.values(), key=lambda p: -(p.tx_rate + p.rx_rate))


def acc_to_proc_rows(proc_acc, proc_comm, proto: str = "ALL", include_loopback: bool = True) -> Dict[int, ProcRow]:
    out: Dict[int, ProcRow] = {}
    for (tgid, pr, loop), a in proc_acc.items():
        if loop and not include_loopback:
            continue
        if proto != "ALL" and pr != proto:
            continue
        p = out.get(tgid)
        if p is None:
            p = out[tgid] = ProcRow(tgid=tgid, comm=proc_comm.get(tgid, "?"))
        if pr not in p.protos:
            p.protos.append(pr)
        p.tx_bytes += a["tx_bytes"]
        p.rx_bytes += a["rx_bytes"]
        p.wire_tx_bytes += a["wire_tx_bytes"]
        p.retrans_bytes += a["retrans_bytes"]
        p.retrans_segs += a["retrans_segs"]
        p.udp_drops += a["udp_drops"]
    return out


# --------------------------------------------------------------------------
# Tracker = kernel probes + aggregator + system counters
# --------------------------------------------------------------------------

class Tracker:
    def __init__(self, interval: float = 1.0, idle_timeout: float = 30.0,
                 max_flows: int = 16384, bpf_stats: bool = False, verbose: bool = False):
        self.interval = interval
        self.kernel = KernelProbes(max_flows=max_flows, verbose=verbose)
        self.agg = Aggregator(idle_timeout=idle_timeout)
        self.bpf_stats = bpf_stats
        self._prev_nic = None
        self._prev_t = None
        self._prev_cpu = None
        self._prev_bpf = None
        self._bpf_stats_old = None

    def start(self, log: Callable[[str], None] = lambda s: None) -> None:
        if self.bpf_stats:
            try:
                with open("/proc/sys/kernel/bpf_stats_enabled") as f:
                    self._bpf_stats_old = f.read().strip()
                with open("/proc/sys/kernel/bpf_stats_enabled", "w") as f:
                    f.write("1")
            except OSError:
                self.bpf_stats = False
        self.kernel.load(log)
        self._prev_t = time.monotonic()
        self._prev_nic = netutil.read_nic_counters()
        self._prev_cpu = netutil.self_cpu_seconds()
        self._prev_bpf = self.kernel.bpf_runtime_ns()
        # first read establishes the baseline for already-open sockets
        self.agg.step(time.monotonic_ns(), 0.0, self.kernel.read_flows(),
                      self.kernel.read_tcp(), self.kernel.read_udp_drops())

    def stop(self) -> None:
        self.kernel.close()
        if self._bpf_stats_old is not None:
            try:
                with open("/proc/sys/kernel/bpf_stats_enabled", "w") as f:
                    f.write(self._bpf_stats_old)
            except OSError:
                pass

    def poll(self) -> Snapshot:
        """Wait until the next interval boundary (handling events meanwhile),
        then read all counters and return a Snapshot."""
        deadline = self._prev_t + self.interval
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            self.kernel.poll_events(int(left * 1000) + 1)
        self.kernel.poll_events(0)

        now = time.monotonic()
        interval = now - self._prev_t
        self._prev_t = now
        now_ns = time.monotonic_ns()

        rows, stale_keys, stale_socks = self.agg.step(
            now_ns, interval, self.kernel.read_flows(), self.kernel.read_tcp(),
            self.kernel.read_udp_drops())
        if stale_keys:
            self.kernel.delete_flows(stale_keys, stale_socks)

        acc = acc_to_proc_rows(self.agg.proc_acc, self.agg.proc_comm)
        procs = build_proc_rows(rows, acc)

        nic_now = netutil.read_nic_counters()
        nic = {}
        for name, v in nic_now.items():
            pv = self._prev_nic.get(name, v)
            nic[name] = {
                "rx_rate": max(0, v[0] - pv[0]) / interval, "tx_rate": max(0, v[3] - pv[3]) / interval,
                "rx_pkts": max(0, v[1] - pv[1]) / interval, "tx_pkts": max(0, v[4] - pv[4]) / interval,
                "rx_drop": max(0, v[2] - pv[2]), "tx_drop": max(0, v[5] - pv[5]),
                "rx_bytes": v[0], "tx_bytes": v[3]}
        self._prev_nic = nic_now

        cpu = netutil.self_cpu_seconds()
        bpf_ns, bpf_cnt = self.kernel.bpf_runtime_ns()
        pns, pcnt = self._prev_bpf
        self._prev_bpf = (bpf_ns, bpf_cnt)
        health = {
            "tracker_cpu_pct": 100.0 * (cpu - self._prev_cpu) / interval,
            "kprobe_misses": self.kernel.kprobe_misses(),
            "perf_lost": self.kernel.perf_lost,
            "tracked_sockets": len(self.agg.flows),
            "bpf_stats": self.bpf_stats,
            "bpf_cpu_pct": 100.0 * (bpf_ns - pns) / 1e9 / interval if self.bpf_stats else 0.0,
            "bpf_avg_ns": (bpf_ns - pns) / (bpf_cnt - pcnt) if self.bpf_stats and bpf_cnt > pcnt else 0.0,
            "bpf_runs_per_s": (bpf_cnt - pcnt) / interval if self.bpf_stats else 0.0,
        }
        self._prev_cpu = cpu

        snap = Snapshot(ts=time.time(), interval=interval, flows=rows, procs=procs,
                        nic=nic, events=self.kernel.drain_events(), health=health)
        snap.totals = summarize([f for f in rows if not f.loopback])
        snap.proc_acc = self.agg.proc_acc          # used by filtered views
        snap.proc_comm = dict(self.agg.proc_comm)
        return snap

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
