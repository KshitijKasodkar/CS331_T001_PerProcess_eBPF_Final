#!/usr/bin/env python3
"""Accuracy and packet-loss experiments for the eBPF bandwidth tracker.

Run as root on the Raspberry Pi (or any Linux box with BCC):

  # 1) two machines (recommended - real network): start the server on the laptop
  #      python trafficgen.py server            (Windows Python is fine)
  #    then on the Pi:
  sudo python3 experiments/run_experiments.py --peer 192.168.1.20 --loss 0,1,2,5,10

  # 2) single machine (no second computer): everything over loopback
  sudo python3 experiments/run_experiments.py --peer local --loss 0,1,5,10

For every loss level, `tc netem` drops that percentage of packets, the
traffic generator moves a KNOWN number of bytes, and we compare:

  ground truth (what the apps on both ends counted)
      vs
  what the eBPF tracker measured (socket layer + TCP wire layer)
      vs
  what the network interface counted (/proc/net/dev)

Results land in experiments/results/<timestamp>/ (CSV, JSON, Markdown, HTML with charts).
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bwtracker import netutil  # noqa: E402
from bwtracker.collector import Tracker, TrackerError  # noqa: E402

TRAFFICGEN = os.path.join(ROOT, "tools", "trafficgen.py")
IFB = "ifb-bwtrack"


def sh(cmd: str, check: bool = False) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd}\n{r.stderr}")
    return r


# --------------------------------------------------------------------- netem

class Netem:
    """Emulates a lossy link with Linux traffic control.

    egress : tc qdisc ... root netem   on the interface (packets we send)
    ingress: packets we receive are redirected to an IFB device that has its
             own netem qdisc (netem can only act on egress queues)."""

    def __init__(self, iface: str, direction: str):
        self.iface, self.direction = iface, direction

    def apply(self, loss: float, delay_ms: float) -> None:
        self.clear()
        if loss <= 0 and delay_ms <= 0:
            return
        spec = "netem limit 100000"
        if delay_ms > 0:
            spec += f" delay {delay_ms}ms"
        if loss > 0:
            spec += f" loss {loss}%"
        if self.direction in ("egress", "both") or self.iface == "lo":
            sh(f"tc qdisc replace dev {self.iface} root {spec}", check=True)
        if self.direction in ("ingress", "both") and self.iface != "lo":
            sh("modprobe ifb numifbs=0")
            sh(f"ip link add {IFB} type ifb")
            sh(f"ip link set {IFB} up", check=True)
            sh(f"tc qdisc replace dev {self.iface} handle ffff: ingress", check=True)
            sh(f"tc filter replace dev {self.iface} parent ffff: protocol all matchall "
               f"action mirred egress redirect dev {IFB}", check=True)
            sh(f"tc qdisc replace dev {IFB} root {spec}", check=True)

    def clear(self) -> None:
        sh(f"tc qdisc del dev {self.iface} root")
        sh(f"tc qdisc del dev {self.iface} ingress")
        sh(f"ip link del {IFB}")


def route_iface(peer: str) -> str:
    out = sh(f"ip route get {peer}").stdout.split()
    return out[out.index("dev") + 1] if "dev" in out else "eth0"


# --------------------------------------------------------------------- monitor

class Monitor(threading.Thread):
    """Polls the tracker and records per-interval numbers for the target PIDs."""

    def __init__(self, tracker: Tracker, iface: str):
        super().__init__(daemon=True)
        self.tracker, self.iface = tracker, iface
        self.targets: set = set()
        self.rows: list = []
        self.events: list = []
        self.health: list = []
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()

    def run(self) -> None:
        while not self.stop_flag.is_set():
            snap = self.tracker.poll()
            nic = snap.nic.get(self.iface, {})
            with self.lock:
                targets = set(self.targets)
                self.health.append(snap.health)
                for f in snap.flows:
                    if f.tgid not in targets:
                        continue
                    if not (f.d_tx or f.d_rx or f.d_wire_tx or f.d_retrans_segs or f.d_udp_drops):
                        continue
                    self.rows.append({
                        "t": time.monotonic(), "interval": snap.interval, "pid": f.tgid, "proto": f.proto,
                        "raddr": f.raddr, "d_tx": f.d_tx, "d_rx": f.d_rx, "d_wire_tx": f.d_wire_tx,
                        "d_acked": int(f.acked_rate * snap.interval), "d_retrans_bytes": f.d_retrans_bytes,
                        "d_retrans_segs": f.d_retrans_segs, "d_udp_drops": f.d_udp_drops,
                        "srtt_us": f.srtt_us, "cwnd": f.cwnd, "mss": f.mss,
                        "nic_tx": int(nic.get("tx_rate", 0) * snap.interval),
                        "nic_rx": int(nic.get("rx_rate", 0) * snap.interval)})
                self.events.extend(e for e in snap.events if e.tgid in targets)

    def collect(self, pids):
        with self.lock:
            self.targets = set(pids)
            self.rows, self.events, self.health = [], [], []

    def take(self):
        with self.lock:
            rows, ev, h = self.rows, self.events, self.health
            self.targets, self.rows, self.events, self.health = set(), [], [], []
        return rows, ev, h


def sum_rows(rows, pid=None, proto=None):
    s = collections.Counter()
    for r in rows:
        if (pid is None or r["pid"] == pid) and (proto is None or r["proto"] == proto):
            for k in ("d_tx", "d_rx", "d_wire_tx", "d_acked", "d_retrans_bytes", "d_retrans_segs", "d_udp_drops"):
                s[k] += r[k]
    return s


# --------------------------------------------------------------------- trials

def run_client(peer, port, mode, extra):
    cmd = [sys.executable, TRAFFICGEN, "client", peer, "--port", str(port), "--mode", mode] + extra
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p


def trial(mon: Monitor, args, mode: str, loss: float, server_pid, nic_before):
    if mode == "tcp-sendfile":
        mode, sf = "tcp-up", True
    else:
        sf = False
    extra = (["--bytes", args.tcp_bytes] + (["--sendfile"] if sf else []) if mode.startswith("tcp") else
             ["--rate", args.udp_rate, "--seconds", str(args.udp_seconds), "--size", str(args.udp_size)])
    p = run_client(args.peer_ip, args.port, mode, extra)
    pids = {p.pid} | ({server_pid} if server_pid else set())
    mon.collect(pids)
    out, err = p.communicate(timeout=600)
    time.sleep(args.interval * 3)       # let the last ACKs / close be observed
    rows, events, health = mon.take()
    if p.returncode != 0:
        raise RuntimeError(f"traffic generator failed: {err.strip()}")
    truth = json.loads(out.strip().splitlines()[-1])
    proto = "TCP" if mode.startswith("tcp") else "UDP"
    cli = sum_rows(rows, pid=p.pid, proto=proto)
    srv = sum_rows(rows, pid=server_pid, proto=proto) if server_pid else None
    ctx, crx = truth.get("control_tx_bytes", 0), truth.get("control_rx_bytes", 0)
    nic_after = netutil.read_nic_counters().get(args.iface)
    nic_tx = nic_after[3] - nic_before[3] if nic_after and nic_before else 0
    nic_rx = nic_after[0] - nic_before[0] if nic_after and nic_before else 0
    srtts = [r["srtt_us"] for r in rows if r["pid"] == p.pid and r["srtt_us"]]
    srtt_ms = (sum(srtts) / len(srtts) / 1000) if srtts else 0.0
    mss = max([r["mss"] for r in rows if r["pid"] == p.pid] or [0])

    r = {"mode": mode, "sendfile": sf, "loss_pct": loss, "delay_ms": args.delay, "duration_s": round(truth["duration"], 3),
         "client_pid": p.pid, "nic_tx_bytes": nic_tx, "nic_rx_bytes": nic_rx, "srtt_ms": round(srtt_ms, 2),
         "mss": mss, "events": len(events)}
    if mode == "tcp-up":
        app = truth["app_sent_bytes"] + ctx      # data + the JSON request line, same socket
        r.update({
            "truth_app_sent": app, "truth_peer_received": truth["peer_received_bytes"],
            "ebpf_app_tx": cli["d_tx"], "ebpf_wire_tx": cli["d_wire_tx"], "ebpf_acked": cli["d_acked"],
            "ebpf_retrans_bytes": cli["d_retrans_bytes"], "ebpf_retrans_segs": cli["d_retrans_segs"],
            "app_accuracy_pct": pct(cli["d_tx"], app),
            "wire_overhead_pct": round(100.0 * (cli["d_wire_tx"] - app) / app, 3) if app else 0,
            "retrans_pct": pct(cli["d_retrans_bytes"], cli["d_wire_tx"]),
            "goodput_mbps": round(app * 8 / truth["duration"] / 1e6, 3) if truth["duration"] else 0})
        if srv:
            r["ebpf_peer_rx"] = srv["d_rx"]
            r["peer_rx_accuracy_pct"] = pct(srv["d_rx"], truth["peer_received_bytes"] + ctx)
        r.update(realtime_stats(rows, p.pid))
    elif mode == "tcp-down":
        app = truth["app_received_bytes"] + crx
        r.update({"truth_app_received": app, "truth_peer_sent": truth["peer_sent_bytes"],
                  "ebpf_app_rx": cli["d_rx"], "app_accuracy_pct": pct(cli["d_rx"], app),
                  "goodput_mbps": round(app * 8 / truth["duration"] / 1e6, 3) if truth["duration"] else 0})
        if srv:
            r.update({"ebpf_peer_wire_tx": srv["d_wire_tx"], "ebpf_peer_retrans_bytes": srv["d_retrans_bytes"],
                      "retrans_pct": pct(srv["d_retrans_bytes"], srv["d_wire_tx"])})
    elif mode == "udp-up":
        app = truth["app_sent_bytes"]
        r.update({"truth_app_sent": app, "truth_sent_pkts": truth["sent_pkts"],
                  "truth_peer_received_pkts": truth["peer_received_pkts"],
                  "truth_peer_received_bytes": truth["peer_received_bytes"],
                  "truth_loss_pct": round(truth["udp_loss_pct"], 3),
                  "ebpf_app_tx": cli["d_tx"], "app_accuracy_pct": pct(cli["d_tx"], app),
                  "sender_view_delivered_pct": 100.0,
                  "send_errors": truth.get("send_errors", 0)})
        if srv:
            r.update({"ebpf_peer_rx": srv["d_rx"], "ebpf_udp_drops": srv["d_udp_drops"],
                      "ebpf_measured_loss_pct": round(100 - pct(srv["d_rx"], cli["d_tx"]), 3),
                      "peer_rx_accuracy_pct": pct(srv["d_rx"], truth["peer_received_bytes"])})
    elif mode == "udp-down":
        app = truth["app_received_bytes"]
        r.update({"truth_peer_sent": truth["peer_sent_bytes"], "truth_app_received": app,
                  "truth_loss_pct": round(truth["udp_loss_pct"], 3), "ebpf_app_rx": cli["d_rx"],
                  "app_accuracy_pct": pct(cli["d_rx"], app),
                  "ebpf_measured_loss_pct": round(100 - pct(cli["d_rx"], truth["peer_sent_bytes"]), 3)})
    hs = [h for h in health if h]
    if hs:
        r["kprobe_misses"] = hs[-1].get("kprobe_misses", 0)
        r["tracker_cpu_pct"] = round(max(h.get("tracker_cpu_pct", 0) for h in hs), 2)
        if hs[-1].get("bpf_stats"):
            r["bpf_avg_ns"] = round(sum(h["bpf_avg_ns"] for h in hs) / len(hs), 1)
            r["bpf_cpu_pct"] = round(max(h["bpf_cpu_pct"] for h in hs), 3)
    return r, rows


def realtime_stats(rows, pid):
    """How different are the per-interval rates at the socket layer and on the wire?"""
    iv = [x for x in rows if x["pid"] == pid and x["proto"] == "TCP"]
    if not iv:
        return {}
    devs = []
    for x in iv:
        if x["d_wire_tx"] > 0:
            devs.append(abs(x["d_tx"] - x["d_wire_tx"]) / x["d_wire_tx"] * 100)
    last_app = max((x["t"] for x in iv if x["d_tx"] > 0), default=0)
    last_wire = max((x["t"] for x in iv if x["d_wire_tx"] > 0), default=0)
    return {"interval_rate_dev_mean_pct": round(sum(devs) / len(devs), 2) if devs else 0,
            "interval_rate_dev_max_pct": round(max(devs), 2) if devs else 0,
            "wire_lag_s": round(max(0.0, last_wire - last_app), 2)}


def pct(a, b) -> float:
    return round(100.0 * a / b, 3) if b else 0.0


def udp_rcvbuf_trial(mon: Monitor, args):
    """A slow UDP reader with a tiny receive buffer: the kernel must DROP
    datagrams. Ground truth = kernel's own Udp RcvbufErrors counter."""
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024)
    rx.bind(("127.0.0.1", 0))
    port = rx.getsockname()[1]
    rx.settimeout(0.5)
    before = netutil.read_snmp().get("Udp", {}).get("RcvbufErrors", 0)
    sender = subprocess.Popen([sys.executable, "-c", f"""
import socket, time
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
for i in range(3000):
    s.sendto(bytes(1000), ("127.0.0.1", {port}))
"""])
    mon.collect({os.getpid(), sender.pid})
    got = 0
    t0 = time.time()
    while time.time() - t0 < 4:
        try:
            rx.recv(2048)
            got += 1
            time.sleep(0.002)       # deliberately slow reader
        except socket.timeout:
            if sender.poll() is not None:
                break
    sender.wait()
    time.sleep(args.interval * 3)
    rows, events, _ = mon.take()
    after = netutil.read_snmp().get("Udp", {}).get("RcvbufErrors", 0)
    me = sum_rows(rows, pid=os.getpid())
    snd = sum_rows(rows, pid=sender.pid)
    rx.close()
    return {"mode": "udp-rcvbuf", "loss_pct": 0, "truth_sent_pkts": 3000, "truth_app_received_pkts": got,
            "truth_kernel_rcvbuf_errors": after - before, "ebpf_udp_drops": me["d_udp_drops"],
            "ebpf_drop_events": sum(1 for e in events if e.kind == "UDP_DROP"),
            "ebpf_sender_tx": snd["d_tx"], "ebpf_app_rx": me["d_rx"], "truth_app_rx_bytes": got * 1000,
            "app_accuracy_pct": pct(me["d_rx"], got * 1000)}


# --------------------------------------------------------------------- report

def _tick(v: float) -> str:
    if abs(v) >= 100:
        return f"{v:,.0f}"
    return f"{v:.3g}"


def svg_chart(title, series, xlabel, ylabel, w=640, h=300, ymax=None, xmax=None, ymin=0.0):
    """Tiny dependency-free SVG line chart. series = [(name, color, [(x, y)], dashed)]"""
    pts = [p for s in series for p in s[2]]
    if not pts:
        return ""
    x0, x1 = 0, xmax or max(p[0] for p in pts) or 1
    y1 = ymax or max(p[1] for p in pts) * 1.1 or 1
    L, R, T, B = 64, 16, 28, 40
    sx = lambda x: L + (x - x0) / (x1 - x0) * (w - L - R)
    sy = lambda y: h - B - (y - ymin) / (y1 - ymin) * (h - T - B)
    o = [f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg" '
         f'font-family="sans-serif" font-size="12">',
         f'<text x="{L}" y="16" font-weight="600">{title}</text>']
    for i in range(5):
        yv = ymin + (y1 - ymin) * i / 4
        o.append(f'<line x1="{L}" x2="{w - R}" y1="{sy(yv):.1f}" y2="{sy(yv):.1f}" stroke="#ddd"/>')
        o.append(f'<text x="{L - 6}" y="{sy(yv) + 4:.1f}" text-anchor="end" fill="#555">{_tick(yv)}</text>')
    for i in range(6):
        xv = x0 + (x1 - x0) * i / 5
        o.append(f'<text x="{sx(xv):.1f}" y="{h - B + 16}" text-anchor="middle" fill="#555">{_tick(xv)}</text>')
    o.append(f'<text x="{(L + w - R) / 2}" y="{h - 6}" text-anchor="middle" fill="#333">{xlabel}</text>')
    o.append(f'<text x="14" y="{(T + h - B) / 2}" text-anchor="middle" fill="#333" '
             f'transform="rotate(-90 14 {(T + h - B) / 2})">{ylabel}</text>')
    lx = L + 8
    for name, color, data, dashed in series:
        if not data:
            continue
        d = " ".join(f"{'M' if i == 0 else 'L'}{sx(x):.1f},{sy(y):.1f}" for i, (x, y) in enumerate(data))
        dash = ' stroke-dasharray="5,4"' if dashed else ""
        o.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2"{dash}/>')
        for x, y in data:
            if len(data) < 30:
                o.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="3" fill="{color}"/>')
        o.append(f'<line x1="{lx}" x2="{lx + 18}" y1="{T + 4}" y2="{T + 4}" stroke="{color}" stroke-width="2"{dash}/>'
                 f'<text x="{lx + 22}" y="{T + 8}" fill="#333">{name}</text>')
        lx += 30 + 7 * len(name)
    o.append("</svg>")
    return "\n".join(o)


def write_report(outdir, results, series_rows, meta):
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "results.json"), "w") as f:
        json.dump({"meta": meta, "results": results}, f, indent=2)
    keys = sorted({k for r in results for k in r})
    with open(os.path.join(outdir, "results.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(results)
    if series_rows:
        with open(os.path.join(outdir, "series.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(series_rows[0].keys()))
            w.writeheader()
            w.writerows(series_rows)

    def table(rows, cols):
        if not rows:
            return "_no data_\n"
        s = "| " + " | ".join(c[0] for c in cols) + " |\n|" + "---|" * len(cols) + "\n"
        for r in rows:
            s += "| " + " | ".join(str(r.get(c[1], "")) for c in cols) + " |\n"
        return s

    tcp = [r for r in results if r["mode"] == "tcp-up"]
    tcpd = [r for r in results if r["mode"] == "tcp-down"]
    udp = [r for r in results if r["mode"] == "udp-up"]
    udpd = [r for r in results if r["mode"] == "udp-down"]
    rcv = [r for r in results if r["mode"] == "udp-rcvbuf"]
    md = [f"# eBPF bandwidth tracker - accuracy and packet-loss experiments\n",
          f"Host `{meta['host']}`, kernel `{meta['kernel']}`, {meta['platform']}. Peer `{meta['peer']}` via "
          f"`{meta['iface']}`. Loss applied with `tc netem` ({meta['direction']}), extra delay {meta['delay_ms']} ms. "
          f"Started {meta['started']}.\n"]
    for r in tcp:
        r["rt_dev"] = f"{r.get('interval_rate_dev_mean_pct', '')}/{r.get('interval_rate_dev_max_pct', '')}"
    md.append("\n## 1. TCP upload: socket layer vs wire\n")
    md.append("The app-layer counter (kprobe on `tcp_sendmsg` return) should match what the app sent exactly. "
              "The wire counter (`tcp_sock->bytes_sent`) is larger whenever segments are lost and resent.\n\n")
    md.append(table(tcp, [("netem loss %", "loss_pct"), ("sendfile", "sendfile"), ("app sent (truth)", "truth_app_sent"),
                          ("eBPF app tx", "ebpf_app_tx"), ("app accuracy %", "app_accuracy_pct"),
                          ("eBPF wire tx", "ebpf_wire_tx"), ("retransmitted B", "ebpf_retrans_bytes"),
                          ("retx % of wire", "retrans_pct"), ("retx segs", "ebpf_retrans_segs"),
                          ("NIC tx B", "nic_tx_bytes"), ("goodput Mbit/s", "goodput_mbps"),
                          ("sRTT ms", "srtt_ms"), ("per-sample (0.5 s) app vs wire dev % (mean/max)", "rt_dev"),
                          ("wire lags app by s", "wire_lag_s")]))
    if tcpd:
        md.append("\n## 2. TCP download\n")
        md.append(table(tcpd, [("netem loss %", "loss_pct"), ("app received (truth)", "truth_app_received"),
                               ("eBPF app rx", "ebpf_app_rx"), ("accuracy %", "app_accuracy_pct"),
                               ("peer retx % (if peer traced)", "retrans_pct"), ("goodput Mbit/s", "goodput_mbps")]))
    md.append("\n## 3. UDP upload: the sender cannot see loss\n")
    md.append(table(udp, [("netem loss %", "loss_pct"), ("app sent (truth)", "truth_app_sent"),
                          ("eBPF sender tx", "ebpf_app_tx"), ("accuracy %", "app_accuracy_pct"),
                          ("true loss % (seq numbers)", "truth_loss_pct"),
                          ("eBPF measured loss % (both ends traced)", "ebpf_measured_loss_pct"),
                          ("eBPF rcvbuf drops", "ebpf_udp_drops")]))
    if udpd:
        md.append("\n## 4. UDP download\n")
        md.append(table(udpd, [("netem loss %", "loss_pct"), ("peer sent", "truth_peer_sent"),
                               ("app received (truth)", "truth_app_received"), ("eBPF app rx", "ebpf_app_rx"),
                               ("accuracy %", "app_accuracy_pct"), ("true loss %", "truth_loss_pct"),
                               ("eBPF measured loss %", "ebpf_measured_loss_pct")]))
    if rcv:
        md.append("\n## 5. UDP receive-buffer overflow (host-side drops)\n")
        md.append(table(rcv, [("sent pkts", "truth_sent_pkts"), ("app received pkts", "truth_app_received_pkts"),
                              ("kernel RcvbufErrors (truth)", "truth_kernel_rcvbuf_errors"),
                              ("eBPF drops", "ebpf_udp_drops"), ("eBPF drop events", "ebpf_drop_events"),
                              ("eBPF rx accuracy %", "app_accuracy_pct")]))
    md.append("\n## Tracker health during the runs\n")
    md.append(table(results, [("mode", "mode"), ("loss %", "loss_pct"), ("kretprobe misses", "kprobe_misses"),
                              ("tracker CPU % (peak)", "tracker_cpu_pct"), ("eBPF ns/run", "bpf_avg_ns"),
                              ("eBPF CPU %", "bpf_cpu_pct")]))
    with open(os.path.join(outdir, "report.md"), "w") as f:
        f.write("".join(md))

    charts = []
    tcp = [r for r in tcp if not r.get("sendfile")]   # one point per loss level in the charts
    if tcp:
        xm = max(r["loss_pct"] for r in tcp) or 1
        charts.append(svg_chart("TCP: retransmitted share of wire bytes vs configured loss", [
            ("measured retx %", "#b5367a", [(r["loss_pct"], r["retrans_pct"]) for r in tcp], False),
            ("y = x", "#999", [(0, 0), (xm, xm)], True)], "netem loss %", "% of wire bytes", xmax=xm))
        mathis = []
        for r in tcp:
            p, rtt = r["loss_pct"] / 100, r["srtt_ms"] / 1000
            if p > 0 and rtt > 0:
                mathis.append((r["loss_pct"], min(1e4, (r.get("mss") or 1448) * 8 / rtt * 1.22 / math.sqrt(p) / 1e6)))
        charts.append(svg_chart("TCP goodput vs loss (and the Mathis model MSS/RTT * 1.22/sqrt(p))", [
            ("measured", "#3d5a80", [(r["loss_pct"], r["goodput_mbps"]) for r in tcp], False),
            ("Mathis prediction", "#999", mathis, True)], "netem loss %", "Mbit/s",
            ymax=max([r["goodput_mbps"] for r in tcp] + [m[1] for m in mathis if m[1] < 1e4]) * 1.15))
        worst = max(tcp, key=lambda r: r["loss_pct"])
        iv = [x for x in series_rows if x.get("trial") == f"tcp-up@{worst['loss_pct']}" and x["proto"] == "TCP"
              and x["pid"] == worst["client_pid"]]
        if iv:
            t0 = iv[0]["t"]
            charts.append(svg_chart(f"Per-interval rates, TCP upload at {worst['loss_pct']}% loss", [
                ("app (tcp_sendmsg)", "#b5367a", [(x["t"] - t0, x["d_tx"] * 8 / x["interval"] / 1e6) for x in iv], False),
                ("wire (bytes_sent)", "#3d5a80", [(x["t"] - t0, x["d_wire_tx"] * 8 / x["interval"] / 1e6) for x in iv], False),
                ("retransmitted", "#d23c3c", [(x["t"] - t0, x["d_retrans_bytes"] * 8 / x["interval"] / 1e6) for x in iv], False),
                ("NIC tx", "#7d868f", [(x["t"] - t0, x["nic_tx"] * 8 / x["interval"] / 1e6) for x in iv], True)],
                "seconds", "Mbit/s"))
    if udp:
        xm = max(r["loss_pct"] for r in udp) or 1
        s = [("sender-side eBPF view", "#b5367a", [(r["loss_pct"], 100.0) for r in udp], True),
             ("delivered (receiver truth)", "#1f8a70", [(r["loss_pct"], 100 - r["truth_loss_pct"]) for r in udp], False)]
        if any("ebpf_measured_loss_pct" in r for r in udp):
            s.append(("delivered (eBPF, both ends)", "#3d5a80",
                      [(r["loss_pct"], 100 - r["ebpf_measured_loss_pct"]) for r in udp if "ebpf_measured_loss_pct" in r], True))
        charts.append(svg_chart("UDP: what the sender sees vs what arrives", s, "netem loss %", "% of bytes delivered",
                                ymax=101, ymin=min([100 - r["truth_loss_pct"] for r in udp] + [95]) - 2, xmax=xm))
    html = ["<!doctype html><meta charset='utf-8'><title>bwtrack experiments</title>",
            "<style>body{font:15px/1.5 system-ui,sans-serif;max-width:1100px;margin:30px auto;padding:0 16px;color:#1c2a33}"
            "table{border-collapse:collapse;font-size:13px;margin:8px 0 20px}td,th{border:1px solid #ccd;padding:3px 7px;text-align:right}"
            "th{background:#eef2f1}svg{max-width:100%;height:auto;display:block;margin:14px 0 26px}</style>"]
    html.append(md_to_html("".join(md)))
    html.append("<h2>Charts</h2>" + "".join(charts))
    with open(os.path.join(outdir, "report.html"), "w") as f:
        f.write("\n".join(html))


def md_to_html(md: str) -> str:
    out, in_table = [], False
    for line in md.splitlines():
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(set(c) <= {"-"} for c in cells):
                continue
            tag = "th" if not in_table else "td"
            if not in_table:
                out.append("<table>")
                in_table = True
            out.append("<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if line.startswith("## "):
            out.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{line[2:]}</h1>")
        elif line.strip():
            out.append(f"<p>{line.replace('`', '')}</p>")
    if in_table:
        out.append("</table>")
    return "\n".join(out)


# --------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--peer", default="local", help="IP of the machine running `trafficgen.py server`, or 'local'")
    ap.add_argument("--port", type=int, default=5300)
    ap.add_argument("--iface", help="interface to apply netem on (default: route to peer, 'lo' for local)")
    ap.add_argument("--direction", choices=["egress", "ingress", "both"], default="both",
                    help="which direction gets the packet loss (default both)")
    ap.add_argument("--loss", default="0,1,2,5,10", help="comma separated loss percentages")
    ap.add_argument("--delay", type=float, default=0.0, help="extra one-way delay in ms (e.g. 20)")
    ap.add_argument("--modes", default="tcp-up,tcp-down,udp-up,udp-down",
                    help="any of tcp-up,tcp-down,udp-up,udp-down,tcp-sendfile")
    ap.add_argument("--tcp-bytes", default="20M")
    ap.add_argument("--udp-rate", default="20M")
    ap.add_argument("--udp-seconds", type=float, default=5)
    ap.add_argument("--udp-size", type=int, default=1200)
    ap.add_argument("--rcvbuf-test", action="store_true", help="also run the UDP receive-buffer overflow test")
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--out", default=os.path.join(ROOT, "experiments", "results"))
    ap.add_argument("--rebuild", metavar="DIR", help="only regenerate report.md/html from DIR's saved results")
    args = ap.parse_args()

    if args.rebuild:
        with open(os.path.join(args.rebuild, "results.json")) as f:
            saved = json.load(f)
        series = []
        sp = os.path.join(args.rebuild, "series.csv")
        if os.path.exists(sp):
            with open(sp) as f:
                for row in csv.DictReader(f):
                    for k, v in row.items():
                        try:
                            row[k] = float(v) if "." in v else int(v)
                        except (TypeError, ValueError):
                            pass
                    series.append(row)
        write_report(args.rebuild, saved["results"], series, saved["meta"])
        print(f"rebuilt {args.rebuild}/report.md and report.html")
        return 0

    if os.geteuid() != 0:
        print("run as root: sudo python3 experiments/run_experiments.py ...")
        return 2
    if not shutil.which("tc"):
        print("`tc` not found: sudo apt install -y iproute2")
        return 2

    server = None
    if args.peer in ("local", "localhost", "127.0.0.1"):
        args.peer_ip = "127.0.0.1"
        args.iface = args.iface or "lo"
        server = subprocess.Popen([sys.executable, TRAFFICGEN, "server", "--bind", "127.0.0.1",
                                   "--port", str(args.port)], stdout=subprocess.DEVNULL)
        time.sleep(0.5)
    else:
        args.peer_ip = args.peer
        args.iface = args.iface or route_iface(args.peer)
        try:
            socket.create_connection((args.peer, args.port), timeout=3).close()
        except OSError as e:
            print(f"cannot reach trafficgen server at {args.peer}:{args.port} ({e}).\n"
                  f"Start it on the peer:  python3 trafficgen.py server --port {args.port}")
            return 2

    losses = [float(x) for x in args.loss.split(",") if x.strip()]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    netem = Netem(args.iface, args.direction)
    tracker = Tracker(interval=args.interval, idle_timeout=20, bpf_stats=True)
    print(f"Loading eBPF tracker... (iface {args.iface}, peer {args.peer_ip})", flush=True)
    try:
        tracker.start(log=lambda s: print("  ", s, flush=True))
    except TrackerError as e:
        print(e)
        return 2
    mon = Monitor(tracker, args.iface)
    mon.start()

    results, series_rows = [], []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    meta = {"host": socket.gethostname(), "kernel": os.uname().release, "platform": netutil.detect_platform(),
            "peer": args.peer, "iface": args.iface, "direction": args.direction, "delay_ms": args.delay,
            "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        for loss in losses:
            netem.apply(loss, args.delay)
            print(f"\n--- netem loss {loss}% delay {args.delay} ms on {args.iface} ({args.direction}) ---", flush=True)
            for mode in modes:
                before = netutil.read_nic_counters().get(args.iface)
                try:
                    r, rows = trial(mon, args, mode, loss, server.pid if server else None, before)
                except Exception as e:
                    print(f"  {mode}: FAILED {e}", flush=True)
                    continue
                for x in rows:
                    x["trial"] = f"{mode}@{loss}"
                series_rows.extend(rows)
                results.append(r)
                print("  " + summary_line(r), flush=True)
        netem.clear()
        if args.rcvbuf_test:
            print("\n--- UDP receive-buffer overflow test ---", flush=True)
            r = udp_rcvbuf_trial(mon, args)
            results.append(r)
            print("  " + summary_line(r), flush=True)
    except KeyboardInterrupt:
        print("interrupted")
    finally:
        netem.clear()
        mon.stop_flag.set()
        time.sleep(args.interval * 2)
        tracker.stop()
        if server:
            server.terminate()

    outdir = os.path.join(args.out, stamp)
    write_report(outdir, results, series_rows, meta)
    print(f"\nWrote {outdir}/report.md, report.html, results.csv, series.csv")
    return 0


def summary_line(r) -> str:
    m = r["mode"]
    if m == "tcp-up":
        return (f"tcp-up{' (sendfile)' if r.get('sendfile') else ''}   app accuracy {r['app_accuracy_pct']}%  "
                f"wire {r['wire_overhead_pct']:+.3f}%  "
                f"retx {r['retrans_pct']}% ({r['ebpf_retrans_segs']} segs)  goodput {r['goodput_mbps']} Mbit/s  "
                f"rtt {r['srtt_ms']} ms  wire lag {r.get('wire_lag_s', 0)} s")
    if m == "tcp-down":
        return (f"tcp-down app accuracy {r['app_accuracy_pct']}%  goodput {r['goodput_mbps']} Mbit/s"
                + (f"  peer retx {r['retrans_pct']}%" if "retrans_pct" in r else ""))
    if m == "udp-up":
        return (f"udp-up   sender accuracy {r['app_accuracy_pct']}%  true loss {r['truth_loss_pct']}%"
                + (f"  eBPF-measured loss {r['ebpf_measured_loss_pct']}%" if "ebpf_measured_loss_pct" in r else
                   "  (sender-side eBPF cannot see it)"))
    if m == "udp-down":
        return (f"udp-down rx accuracy {r['app_accuracy_pct']}%  true loss {r['truth_loss_pct']}%  "
                f"eBPF-measured loss {r['ebpf_measured_loss_pct']}%")
    return (f"rcvbuf   kernel drops {r['truth_kernel_rcvbuf_errors']}  eBPF drops {r['ebpf_udp_drops']}  "
            f"rx accuracy {r['app_accuracy_pct']}%")


if __name__ == "__main__":
    sys.exit(main())
