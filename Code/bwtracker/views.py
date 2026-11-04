"""Text table rendering shared by the curses UI and the plain `stream` mode."""
from __future__ import annotations

import time
from typing import Callable, List, Tuple

from .model import TCP_STATES, FlowRow, ProcRow, Snapshot
from .netutil import endpoint, fmt_bits, fmt_bytes, fmt_rate

# column: (title, width, align, getter, color-key)
Col = Tuple[str, int, str, Callable, str]


def _rtt(us: int) -> str:
    if not us:
        return "-"
    return f"{us / 1000:.1f}ms" if us < 100_000 else f"{us / 1000:.0f}ms"


def _pct(v: float, has: bool) -> str:
    return f"{v:.1f}%" if has else "-"


PROC_COLS: List[Col] = [
    ("PID", 7, ">", lambda p: str(p.tgid), "pid"),
    ("PROCESS", 15, "<", lambda p: p.comm, "name"),
    ("PROTO", 7, "<", lambda p: "/".join(p.protos), "proto"),
    ("REMOTES", 7, ">", lambda p: str(p.remotes) if p.remotes else "-", ""),
    ("UP app/s", 11, ">", lambda p: fmt_rate(p.tx_rate), "tx"),
    ("DOWN app/s", 11, ">", lambda p: fmt_rate(p.rx_rate), "rx"),
    ("UP wire/s", 11, ">", lambda p: fmt_rate(p.wire_tx_rate) if "TCP" in p.protos else "-", "wire"),
    ("RETX%", 6, ">", lambda p: _pct(p.retrans_pct, p.d_wire_tx > 0), "retx"),
    ("RTT", 8, ">", lambda p: _rtt(p.max_srtt_us), ""),
    ("RETX segs", 9, ">", lambda p: str(p.retrans_segs) if p.retrans_segs else "-", "retx"),
    ("UDP drop", 8, ">", lambda p: str(p.udp_drops) if p.udp_drops else "-", "drop"),
    ("TOTAL UP", 10, ">", lambda p: fmt_bytes(p.tx_bytes), "tx"),
    ("TOTAL DOWN", 10, ">", lambda p: fmt_bytes(p.rx_bytes), "rx"),
]

FLOW_COLS: List[Col] = [
    ("PID", 7, ">", lambda f: str(f.tgid), "pid"),
    ("PROCESS", 15, "<", lambda f: f.comm, "name"),
    ("PROTO", 5, "<", lambda f: f.proto, "proto"),
    ("REMOTE", 26, "<", lambda f: endpoint(f.raddr, f.rport) if f.rport else f"{f.raddr} (*)", "remote"),
    ("SOCK", 4, ">", lambda f: str(f.sockets), ""),
    ("UP app/s", 11, ">", lambda f: fmt_rate(f.tx_rate), "tx"),
    ("DOWN app/s", 11, ">", lambda f: fmt_rate(f.rx_rate), "rx"),
    ("UP wire/s", 11, ">", lambda f: fmt_rate(f.wire_tx_rate) if f.proto == "TCP" else "-", "wire"),
    ("ACKED/s", 11, ">", lambda f: fmt_rate(f.acked_rate) if f.proto == "TCP" else "-", ""),
    ("RETX%", 6, ">", lambda f: _pct(f.retrans_pct, f.d_wire_tx > 0), "retx"),
    ("RTT", 8, ">", lambda f: _rtt(f.srtt_us) if f.proto == "TCP" else "-", ""),
    ("CWND", 5, ">", lambda f: str(f.cwnd) if f.proto == "TCP" and f.cwnd else "-", ""),
    ("RETX segs", 9, ">", lambda f: str(f.retrans_segs) if f.retrans_segs else "-", "retx"),
    ("DROP", 5, ">", lambda f: str(f.udp_drops) if f.udp_drops else "-", "drop"),
    ("TOTAL UP", 10, ">", lambda f: fmt_bytes(f.tx_bytes), "tx"),
    ("TOTAL DOWN", 10, ">", lambda f: fmt_bytes(f.rx_bytes), "rx"),
    ("STATE", 9, "<", lambda f: TCP_STATES.get(f.state, "") if f.proto == "TCP" else "", ""),
]

SORTS = {
    "rate": ("total rate", lambda r: -(r.tx_rate + r.rx_rate)),
    "up": ("upload rate", lambda r: -r.tx_rate),
    "down": ("download rate", lambda r: -r.rx_rate),
    "bytes": ("total bytes", lambda r: -(r.tx_bytes + r.rx_bytes)),
    "retrans": ("retransmissions", lambda r: -(r.retrans_segs)),
    "pid": ("pid", lambda r: r.tgid),
}
SORT_ORDER = list(SORTS)


def fit_columns(cols: List[Col], width: int) -> List[Col]:
    """Drop columns from the right until the table fits the terminal."""
    out, used = [], 0
    for c in cols:
        if used + c[1] + 1 > width and out:
            break
        out.append(c)
        used += c[1] + 1
    return out


def cell(text: str, width: int, align: str) -> str:
    text = text if len(text) <= width else text[: width - 1] + "~"
    return f"{text:{align}{width}}"


def header_lines(snap: Snapshot, view: Snapshot) -> List[str]:
    t = view.totals
    nic = [(n, v) for n, v in snap.nic.items() if n != "lo" and (v["rx_bytes"] or v["tx_bytes"])]
    nic_txt = "  ".join(f"{n}: up {fmt_bits(v['tx_rate'])} down {fmt_bits(v['rx_rate'])}" for n, v in nic[:3])
    retx = f"{t['retrans_pct']:.2f}%" if t["d_wire_tx"] else "-"
    h = snap.health
    return [
        f"Apps (socket layer)  up {fmt_bits(t['tx_rate']):>14}  down {fmt_bits(t['rx_rate']):>14}"
        f"   | TCP wire up {fmt_bits(t['wire_tx_rate']):>13}  retrans {retx}  UDP drops {int(t['d_udp_drops'])}",
        f"NIC (ground truth)   {nic_txt}",
        f"tracker cpu {h['tracker_cpu_pct']:.1f}%  sockets {h['tracked_sockets']}  kprobe misses {h['kprobe_misses']}"
        f"  perf lost {h['perf_lost']}" + (f"  eBPF {h['bpf_avg_ns']:.0f} ns/run {h['bpf_cpu_pct']:.2f}% cpu"
                                            if h.get("bpf_stats") else ""),
    ]


def event_line(e) -> str:
    ts = time.strftime("%H:%M:%S", time.localtime(e.ts))
    if e.kind == "RETRANS":
        what = f"retransmitted {e.value} B"
    else:
        what = f"datagram dropped (receive buffer full, err {e.value})"
    who = f"{e.comm}({e.tgid})" if e.tgid else "?"
    return (f"{ts} {e.kind:<8} {who:<22} {e.proto} {endpoint(e.laddr, e.lport)} -> "
            f"{endpoint(e.raddr, e.rport)}  {what}")


def plain_table(rows, cols: List[Col]) -> List[str]:
    lines = [" ".join(cell(c[0], c[1], c[2]) for c in cols)]
    for r in rows:
        lines.append(" ".join(cell(c[3](r), c[1], c[2]) for c in cols))
    return lines
