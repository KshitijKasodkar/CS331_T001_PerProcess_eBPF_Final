"""Plain data structures shared by the collector, the UIs and the storage."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional


@dataclass
class RawFlow:
    """One entry of the kernel `flows` map (cumulative since the tracker started)."""
    sk: int
    tgid: int
    family: int
    proto: int          # 6 = TCP, 17 = UDP
    laddr: str
    lport: int
    raddr: str
    rport: int
    tx_bytes: int
    rx_bytes: int
    tx_calls: int
    rx_calls: int
    first_ns: int
    last_ns: int
    comm: str
    # wire counters of this flow's sockets that already closed (kernel side)
    closed_sent: int = 0
    closed_retrans: int = 0
    closed_acked: int = 0
    closed_received: int = 0
    closed_segs: int = 0

    @property
    def key(self):
        return (self.sk, self.tgid, self.proto, self.laddr, self.lport, self.raddr, self.rport)


@dataclass
class RawTcp:
    """One entry of the kernel `tcp_snaps` map: the kernel's own TCP counters."""
    sk: int
    bytes_sent: int
    bytes_retrans: int
    bytes_acked: int
    bytes_received: int
    total_retrans: int
    retrans_events: int
    srtt_us: int
    snd_cwnd: int
    mss: int
    lost_out: int
    segs_out: int
    segs_in: int
    state: int
    update_ns: int
    born_ns: int = 0


@dataclass
class Event:
    ts: float           # wall clock
    kind: str           # "RETRANS" | "UDP_DROP"
    tgid: int
    comm: str
    proto: str
    laddr: str
    lport: int
    raddr: str
    rport: int
    value: int          # retransmitted segment size / drop errno

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FlowRow:
    """A process <-> remote endpoint pair (one or more sockets)."""
    tgid: int
    comm: str
    proto: str
    laddr: str
    lport: int
    raddr: str
    rport: int
    loopback: bool
    sockets: int = 1
    # socket layer: bytes the APPLICATION wrote / read
    tx_rate: float = 0.0
    rx_rate: float = 0.0
    tx_bytes: int = 0
    rx_bytes: int = 0
    d_tx: int = 0
    d_rx: int = 0
    # TCP wire layer: what the kernel actually put on / took off the network
    wire_tx_rate: float = 0.0
    wire_tx_bytes: int = 0
    d_wire_tx: int = 0
    acked_rate: float = 0.0
    acked_bytes: int = 0
    wire_rx_bytes: int = 0
    retrans_rate: float = 0.0
    retrans_bytes: int = 0
    d_retrans_bytes: int = 0
    retrans_segs: int = 0
    d_retrans_segs: int = 0
    srtt_us: int = 0
    cwnd: int = 0
    mss: int = 0
    lost_out: int = 0
    state: int = 0
    # UDP receive-buffer drops
    udp_drops: int = 0
    d_udp_drops: int = 0
    idle_s: float = 0.0

    @property
    def retrans_pct(self) -> float:
        """Share of bytes put on the wire in this interval that were retransmissions."""
        return 100.0 * self.d_retrans_bytes / self.d_wire_tx if self.d_wire_tx else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["retrans_pct"] = round(self.retrans_pct, 2)
        return d


@dataclass
class ProcRow:
    tgid: int
    comm: str
    cmdline: str = ""
    flows: int = 0
    protos: List[str] = field(default_factory=list)
    remotes: int = 0
    tx_rate: float = 0.0
    rx_rate: float = 0.0
    wire_tx_rate: float = 0.0
    retrans_rate: float = 0.0
    # cumulative since the tracker started (kept even after sockets close)
    tx_bytes: int = 0
    rx_bytes: int = 0
    wire_tx_bytes: int = 0
    retrans_bytes: int = 0
    retrans_segs: int = 0
    udp_drops: int = 0
    max_srtt_us: int = 0
    d_wire_tx: int = 0
    d_retrans_bytes: int = 0

    @property
    def retrans_pct(self) -> float:
        return 100.0 * self.d_retrans_bytes / self.d_wire_tx if self.d_wire_tx else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["retrans_pct"] = round(self.retrans_pct, 2)
        return d


@dataclass
class Snapshot:
    ts: float
    interval: float
    flows: List[FlowRow]
    procs: List[ProcRow]
    nic: Dict[str, Dict[str, float]]
    events: List[Event]
    health: Dict[str, float]
    totals: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "interval": self.interval,
            "totals": self.totals,
            "nic": self.nic,
            "health": self.health,
            "procs": [p.to_dict() for p in self.procs],
            "flows": [f.to_dict() for f in self.flows],
            "events": [e.to_dict() for e in self.events],
        }


PROTO_NAME = {6: "TCP", 17: "UDP"}

TCP_STATES = {
    1: "ESTAB", 2: "SYN_SENT", 3: "SYN_RECV", 4: "FIN_WAIT1", 5: "FIN_WAIT2",
    6: "TIME_WAIT", 7: "CLOSE", 8: "CLOSE_WAIT", 9: "LAST_ACK", 10: "LISTEN",
    11: "CLOSING",
}


def filter_snapshot(snap: Snapshot, proto: str = "ALL", include_loopback: bool = False,
                    pid: Optional[int] = None, comm: Optional[str] = None,
                    cidr: Optional[str] = None, active_within: Optional[float] = None) -> Snapshot:
    """Return a view of `snap` with only matching flows, and processes/totals
    recomputed from those flows. Used by the live UIs."""
    from .netutil import ip_matches

    proto = (proto or "ALL").upper()
    flows = []
    hidden_loop = 0
    for f in snap.flows:
        if f.loopback and not include_loopback:
            hidden_loop += 1
            continue
        if proto != "ALL" and f.proto != proto:
            continue
        if pid is not None and f.tgid != pid:
            continue
        if comm and comm.lower() not in f.comm.lower():
            continue
        if cidr and not ip_matches(f.raddr, cidr):
            continue
        if active_within is not None and f.idle_s > active_within:
            continue
        flows.append(f)

    from .collector import acc_to_proc_rows, build_proc_rows  # local import: avoid a cycle
    acc = None
    if getattr(snap, "proc_acc", None) is not None and not cidr:
        acc = acc_to_proc_rows(snap.proc_acc, snap.proc_comm, proto, include_loopback)
        if pid is not None:
            acc = {k: v for k, v in acc.items() if k == pid}
        if comm:
            acc = {k: v for k, v in acc.items() if comm.lower() in v.comm.lower()}
    procs = build_proc_rows(flows, acc, rates_only_from_flows=acc is None)
    totals = summarize(flows)
    totals["loopback_hidden"] = hidden_loop
    return Snapshot(snap.ts, snap.interval, flows, procs, snap.nic, snap.events, snap.health, totals)


def summarize(flows: List[FlowRow]) -> Dict[str, float]:
    t = {"tx_rate": 0.0, "rx_rate": 0.0, "wire_tx_rate": 0.0, "retrans_rate": 0.0,
         "acked_rate": 0.0, "d_tx": 0, "d_rx": 0, "d_wire_tx": 0, "d_retrans_bytes": 0,
         "d_retrans_segs": 0, "d_udp_drops": 0, "tcp_tx_rate": 0.0, "tcp_rx_rate": 0.0,
         "udp_tx_rate": 0.0, "udp_rx_rate": 0.0, "flows": len(flows)}
    for f in flows:
        t["tx_rate"] += f.tx_rate
        t["rx_rate"] += f.rx_rate
        t["wire_tx_rate"] += f.wire_tx_rate
        t["retrans_rate"] += f.retrans_rate
        t["acked_rate"] += f.acked_rate
        t["d_tx"] += f.d_tx
        t["d_rx"] += f.d_rx
        t["d_wire_tx"] += f.d_wire_tx
        t["d_retrans_bytes"] += f.d_retrans_bytes
        t["d_retrans_segs"] += f.d_retrans_segs
        t["d_udp_drops"] += f.d_udp_drops
        p = f.proto.lower()
        t[f"{p}_tx_rate"] += f.tx_rate
        t[f"{p}_rx_rate"] += f.rx_rate
    t["retrans_pct"] = 100.0 * t["d_retrans_bytes"] / t["d_wire_tx"] if t["d_wire_tx"] else 0.0
    return t
