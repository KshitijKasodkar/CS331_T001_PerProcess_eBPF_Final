"""Prometheus text exposition (no external library needed).

Scrape config example (prometheus.yml):
    scrape_configs:
      - job_name: bwtracker
        static_configs: [{targets: ['raspberrypi.local:8080']}]
"""
from __future__ import annotations

import collections
import threading
from typing import Dict, Tuple

from .model import Snapshot


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _labels(**kw) -> str:
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in kw.items()) + "}"


class PromExporter:
    """Snapshot consumer that keeps the latest metrics text."""

    def __init__(self, max_remotes: int = 200):
        self.max_remotes = max_remotes
        self.remote_acc: Dict[Tuple[str, str, bool], list] = collections.defaultdict(lambda: [0, 0])
        self.text = "# no data yet\n"
        self.lock = threading.Lock()

    def __call__(self, snap: Snapshot) -> None:
        for f in snap.flows:
            if f.d_tx or f.d_rx:
                a = self.remote_acc[(f.raddr, f.proto, f.loopback)]
                a[0] += f.d_tx
                a[1] += f.d_rx
        out = []

        def metric(name, typ, help_, samples):
            out.append(f"# HELP {name} {help_}")
            out.append(f"# TYPE {name} {typ}")
            out.extend(f"{name}{lab} {val}" for lab, val in samples)

        acc = getattr(snap, "proc_acc", {})
        comm = getattr(snap, "proc_comm", {})
        bytes_s, wire_s, rtx_s, segs_s, drop_s = [], [], [], [], []
        for (tgid, proto, loop), a in sorted(acc.items()):
            base = dict(pid=tgid, comm=comm.get(tgid, "?"), proto=proto.lower(), loopback=str(loop).lower())
            bytes_s.append((_labels(**base, direction="tx"), a["tx_bytes"]))
            bytes_s.append((_labels(**base, direction="rx"), a["rx_bytes"]))
            if proto == "TCP":
                wire_s.append((_labels(**base), a["wire_tx_bytes"]))
                rtx_s.append((_labels(**base), a["retrans_bytes"]))
                segs_s.append((_labels(**base), a["retrans_segs"]))
            else:
                drop_s.append((_labels(**base), a["udp_drops"]))
        metric("bwtracker_bytes_total", "counter",
               "Payload bytes sent (tx) / read (rx) by each process at the socket layer.", bytes_s)
        metric("bwtracker_tcp_wire_tx_bytes_total", "counter",
               "TCP payload bytes put on the wire, including retransmissions.", wire_s)
        metric("bwtracker_tcp_retrans_bytes_total", "counter", "TCP payload bytes retransmitted.", rtx_s)
        metric("bwtracker_tcp_retrans_segments_total", "counter", "TCP segments retransmitted.", segs_s)
        metric("bwtracker_udp_drops_total", "counter",
               "UDP datagrams dropped because the socket receive buffer was full.", drop_s)

        rate_s = []
        for p in snap.procs:
            if p.tx_rate or p.rx_rate:
                rate_s.append((_labels(pid=p.tgid, comm=p.comm, direction="tx"), round(p.tx_rate, 1)))
                rate_s.append((_labels(pid=p.tgid, comm=p.comm, direction="rx"), round(p.rx_rate, 1)))
        metric("bwtracker_process_rate_bytes_per_second", "gauge",
               "Current per-process throughput over the last interval.", rate_s)

        rtt_s = [(_labels(pid=f.tgid, comm=f.comm, raddr=f.raddr), f.srtt_us / 1e6)
                 for f in snap.flows if f.proto == "TCP" and f.srtt_us and f.idle_s < 5]
        metric("bwtracker_tcp_srtt_seconds", "gauge", "Smoothed TCP round-trip time of active connections.", rtt_s)

        top = sorted(self.remote_acc.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))[: self.max_remotes]
        rem_s = []
        for (raddr, proto, loop), (tx, rx) in top:
            rem_s.append((_labels(raddr=raddr, proto=proto.lower(), loopback=str(loop).lower(), direction="tx"), tx))
            rem_s.append((_labels(raddr=raddr, proto=proto.lower(), loopback=str(loop).lower(), direction="rx"), rx))
        metric("bwtracker_remote_bytes_total", "counter", "Payload bytes per remote IP (all processes).", rem_s)

        nic_s = []
        for name, v in snap.nic.items():
            nic_s.append((_labels(iface=name, direction="rx"), v["rx_bytes"]))
            nic_s.append((_labels(iface=name, direction="tx"), v["tx_bytes"]))
        metric("bwtracker_nic_bytes_total", "counter", "Interface byte counters from /proc/net/dev (ground truth).", nic_s)

        h = snap.health
        metric("bwtracker_kprobe_misses_total", "counter", "kretprobe calls missed by the kernel.",
               [("", h.get("kprobe_misses", 0))])
        metric("bwtracker_perf_events_lost_total", "counter", "Events lost in the perf buffer.",
               [("", h.get("perf_lost", 0))])
        metric("bwtracker_cpu_percent", "gauge", "CPU used by the user-space tracker.",
               [("", round(h.get("tracker_cpu_pct", 0), 2))])
        with self.lock:
            self.text = "\n".join(out) + "\n"

    def render(self) -> str:
        with self.lock:
            return self.text
