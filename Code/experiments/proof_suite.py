#!/usr/bin/env python3
"""Proof suite: every claim on the slides is produced by a test in this file.

    sudo python3 experiments/proof_suite.py                 # full run (~45 min, 30 min soak)
    sudo python3 experiments/proof_suite.py --quick         # ~8 min smoke run
    sudo python3 experiments/proof_suite.py --only correctness,scalability

Groups
  correctness   issues the design fixes: partial sends, MSG_WAITALL, sendfile,
                unconnected UDP to several IPs, IPv6 + v4-mapped UDP, threads,
                fork, 500 short connections (socket reuse), loopback detection
  performance   iperf3 TCP and small-packet UDP throughput WITH vs WITHOUT the
                tracker (3 runs each), tracker CPU/RSS, eBPF ns per probe run,
                BPF map memory, accuracy at full speed
  scalability   1..200 concurrent connections (each to its own IP), 1..50
                processes, small-message event rate
  reliability   soak (default 30 min) of the real `bwtrack record` process:
                RSS/CPU over time, misses, lost events, SQLite totals vs truth;
                undersized map (LRU) behaviour; tracker started mid-transfer

Every test compares the tracker with ground truth printed by the workload
process itself (experiments/proof/worker.py). Results:
  experiments/results/proof_<time>/ summary.md, summary.json, <test>.json, raw logs
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import platform
import shutil
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from bwtracker import netutil  # noqa: E402
from bwtracker.collector import Tracker, TrackerError  # noqa: E402

WORKER = os.path.join(ROOT, "experiments", "proof", "worker.py")
PY = sys.executable
PORT = [5600]


def next_port():
    PORT[0] += 1
    return PORT[0]


def pct(a, b):
    return round(100.0 * a / b, 4) if b else 0.0


def proc_cpu_s(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            v = f.read().rsplit(")", 1)[1].split()
        return (int(v[11]) + int(v[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, ValueError):
        return 0.0


def proc_rss_mb(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def sys_cpu():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    idle = v[3] + v[4]
    return sum(v), idle


def cpu_busy_pct(a, b):
    tot, idle = b[0] - a[0], b[1] - a[1]
    return 100.0 * (tot - idle) / tot if tot else 0.0


# ------------------------------------------------------------------ workers
class Proc:
    def __init__(self, *args, wait_ready=False):
        self.p = subprocess.Popen([PY, WORKER, *map(str, args)], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        self.pid = self.p.pid
        if wait_ready:
            line = self.p.stdout.readline()
            if not line.startswith("{"):
                raise RuntimeError(f"worker failed: {line}{self.p.stderr.read()}")

    def result(self, timeout=600):
        out, err = self.p.communicate(timeout=timeout)
        lines = [ln for ln in out.strip().splitlines() if ln.startswith("{")]
        if self.p.returncode != 0 or not lines:
            raise RuntimeError(f"worker error: {err.strip()[-800:]}")
        return json.loads(lines[-1])


# ------------------------------------------------------------------ monitor
class Monitor(threading.Thread):
    """Collects every non-zero flow row of every snapshot (all processes)."""

    def __init__(self, tracker):
        super().__init__(daemon=True)
        self.tracker = tracker
        self.rows = []
        self.health = []
        self.poll_ms = []
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()

    def run(self):
        while not self.stop_flag.is_set():
            snap = self.tracker.poll()
            t = time.monotonic()
            with self.lock:
                self.health.append((t, dict(snap.health)))
                for f in snap.flows:
                    if f.d_tx or f.d_rx or f.d_wire_tx:
                        self.rows.append({"t": t, "pid": f.tgid, "comm": f.comm, "proto": f.proto,
                                          "raddr": f.raddr, "loop": f.loopback, "sockets": f.sockets,
                                          "d_tx": f.d_tx, "d_rx": f.d_rx, "d_wire_tx": f.d_wire_tx})

    def mark(self):
        return time.monotonic()

    def sum(self, t0, pids=None, key=None, proto=None, raddr=None, wait=None):
        time.sleep(wait if wait is not None else self.tracker.interval * 3 + 0.2)
        with self.lock:
            rows = [r for r in self.rows if r["t"] >= t0 and (pids is None or r["pid"] in pids)
                    and (proto is None or r["proto"] == proto) and (raddr is None or r["raddr"] == raddr)]
        if key:
            return sum(r[key] for r in rows)
        return rows

    def health_since(self, t0):
        with self.lock:
            return [h for t, h in self.health if t >= t0]


# ------------------------------------------------------------------ results
class Results:
    def __init__(self, outdir):
        self.outdir = outdir
        self.items = collections.OrderedDict()
        os.makedirs(outdir, exist_ok=True)

    def add(self, group, name, data, ok=None):
        data["group"], data["name"] = group, name
        if ok is not None:
            data["pass"] = bool(ok)
        self.items[name] = data
        with open(os.path.join(self.outdir, f"{name}.json"), "w") as f:
            json.dump(data, f, indent=2)
        flag = "" if ok is None else ("PASS " if ok else "FAIL ")
        print(f"  {flag}{name}: {data.get('summary', '')}", flush=True)
        self.save()

    def save(self):
        with open(os.path.join(self.outdir, "summary.json"), "w") as f:
            json.dump(list(self.items.values()), f, indent=2)


def env_info():
    info = {"host": socket.gethostname(), "kernel": os.uname().release, "machine": os.uname().machine,
            "platform": netutil.detect_platform(), "python": platform.python_version(),
            "cpus": os.cpu_count(), "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        with open("/proc/meminfo") as f:
            info["mem_gb"] = round(int(f.readline().split()[1]) / 1024 / 1024, 1)
    except OSError:
        pass
    try:
        import bcc  # noqa
        info["bcc"] = getattr(bcc, "__version__", "?")
    except Exception:
        pass
    try:
        lines = open("/proc/cpuinfo").read().splitlines()
        for key in ("model name", "Model", "Hardware"):
            hit = [ln.split(":", 1)[1].strip() for ln in lines if ln.split(":")[0].strip() == key]
            if hit:
                info["cpu_model"] = hit[0]
                break
    except Exception:
        pass
    return info


# ======================================================================== CORRECTNESS
def t_correctness(mon, R, quick):
    G = "correctness"
    print("\n== correctness (issues fixed) ==", flush=True)
    MB = 1 << 20

    # 1. partial / non-blocking sends
    port = next_port()
    sink = Proc("sink_tcp", "--port", port, "--count", 1, "--slow", 0.002, "--chunk", 4096, "--rcvbuf", 65536,
                wait_ready=True)
    t0 = mon.mark()
    w = Proc("tcp_nonblock", "--port", port, "--seconds", 4 if quick else 8).result()
    s = sink.result()
    meas = mon.sum(t0, {w["pid"]}, "d_tx")
    R.add(G, "partial_sends", {
        "truth_accepted": w["accepted"], "requested_by_app": w["requested"], "calls": w["calls"],
        "partial_sends": w["partial_sends"], "eagain": w["eagain"], "peer_received": s["received"],
        "tracker_tx": meas, "error_pct": round(pct(meas, w["accepted"]) - 100, 4),
        "naive_size_counting_error_pct": round(pct(w["requested"], w["accepted"]) - 100, 1),
        "summary": f"tracker {meas} B = accepted {w['accepted']} B; counting the requested size "
                   f"(old method) would report {w['requested']} B ({pct(w['requested'], w['accepted']) - 100:+.0f} %)"},
        ok=meas == w["accepted"])

    # 2. MSG_WAITALL receive
    port = next_port()
    sink = Proc("sink_tcp", "--port", port, "--count", 1, "--waitall", "--chunk", 100000, wait_ready=True)
    t0 = mon.mark()
    w = Proc("tcp_send", "--port", port, "--bytes", 20 * MB, "--chunk", 7919).result()
    s = sink.result()
    meas = mon.sum(t0, {sink.pid}, "d_rx")
    R.add(G, "recv_waitall", {"truth_received": s["received"], "tracker_rx": meas,
                              "summary": f"receiver with MSG_WAITALL: tracker {meas} B, app {s['received']} B"},
          ok=meas == s["received"])

    # 3. sendfile
    port = next_port()
    sink = Proc("sink_tcp", "--port", port, "--count", 1, wait_ready=True)
    t0 = mon.mark()
    w = Proc("tcp_sendfile", "--port", port, "--bytes", 20 * MB).result()
    sink.result()
    meas = mon.sum(t0, {w["pid"]}, "d_tx")
    R.add(G, "sendfile", {"truth_sent": w["sent"], "tracker_tx": meas,
                          "summary": f"sendfile(): tracker {meas} B, app {w['sent']} B"}, ok=meas == w["sent"])

    # 4. unconnected UDP to 3 destinations
    port = next_port()
    sinks = {ip: Proc("sink_udp", "--bind", ip, "--port", port, "--idle", 2, wait_ready=True)
             for ip in ("127.0.0.2", "127.0.0.3", "127.0.0.4")}
    t0 = mon.mark()
    w = Proc("udp_multi", "--port", port, "--dests", ",".join(sinks), "--count", 300, "--size", 1000).result()
    got = {ip: p.result() for ip, p in sinks.items()}
    rows = {ip: mon.sum(t0, {w["pid"]}, "d_tx", raddr=ip, wait=0 if i else None) for i, ip in enumerate(sinks)}
    zero = mon.sum(t0, {w["pid"]}, "d_tx", raddr="0.0.0.0", wait=0)
    ok = all(rows[ip] == w["per_dest"][ip] for ip in sinks) and zero == 0
    R.add(G, "udp_unconnected_multi_dest", {
        "truth_per_dest": w["per_dest"], "tracker_per_dest": rows, "receiver_counts": {k: v["received"] for k, v in got.items()},
        "tracker_bytes_to_0.0.0.0": zero,
        "summary": "one sendto() socket -> 3 IPs: " + ", ".join(f"{ip} {rows[ip]}/{w['per_dest'][ip]}" for ip in sinks)},
        ok=ok)

    # 5. IPv6 TCP + UDP, and v4-mapped UDP (udpv6_sendmsg -> udp_sendmsg nesting)
    if socket.has_ipv6:
        try:
            port = next_port()
            sink = Proc("sink_tcp", "--bind", "::1", "--port", port, "--count", 1, wait_ready=True)
            t0 = mon.mark()
            w = Proc("tcp_send", "--host", "::1", "--port", port, "--bytes", 10 * MB).result()
            sink.result()
            m6 = mon.sum(t0, {w["pid"]}, "d_tx")
            port = next_port()
            usink = Proc("sink_udp", "--bind", "::", "--port", port, "--idle", 2, wait_ready=True)
            t0 = mon.mark()
            u6 = Proc("udp_send", "--host", "::1", "--port", port, "--count", 500).result()
            u4m = Proc("udp_send", "--host", "::ffff:127.0.0.1", "--port", port, "--count", 500).result()
            usink.result()
            mu6 = mon.sum(t0, {u6["pid"]}, "d_tx")
            mu4m = mon.sum(t0, {u4m["pid"]}, "d_tx", wait=0)
            R.add(G, "ipv6", {"tcp_v6_truth": w["sent"], "tcp_v6_tracker": m6, "udp_v6_truth": u6["sent"],
                              "udp_v6_tracker": mu6, "udp_v4mapped_truth": u4m["sent"], "udp_v4mapped_tracker": mu4m,
                              "summary": f"TCP ::1 {m6}/{w['sent']}, UDP ::1 {mu6}/{u6['sent']}, "
                                         f"UDP v4-mapped {mu4m}/{u4m['sent']} (not double counted)"},
                  ok=m6 == w["sent"] and mu6 == u6["sent"] and mu4m == u4m["sent"])
        except Exception as e:
            R.add(G, "ipv6", {"summary": f"skipped: {e}"})

    # 6. threads in one process
    port = next_port()
    sink = Proc("sink_tcp", "--port", port, "--count", 8, wait_ready=True)
    t0 = mon.mark()
    w = Proc("threads", "--port", port, "--count", 8, "--bytes", 5 * MB).result()
    sink.result()
    rows = mon.sum(t0, {w["pid"]})
    meas = sum(r["d_tx"] for r in rows)
    socks = max([r["sockets"] for r in rows] or [0])
    R.add(G, "threads_one_pid", {"truth": w["sent"], "tracker_tx": meas, "max_sockets_in_row": socks,
                                 "summary": f"8 threads -> one PID: {meas}/{w['sent']} B, {socks} sockets"},
          ok=meas == w["sent"])

    # 7. fork: each child its own PID
    port = next_port()
    sink = Proc("sink_tcp", "--port", port, "--count", 5, wait_ready=True)
    t0 = mon.mark()
    w = Proc("fork", "--port", port, "--count", 4, "--bytes", 3 * MB, "--parent-bytes", MB).result()
    sink.result()
    time.sleep(mon.tracker.interval * 3)
    per = {c["pid"]: (c["sent"], mon.sum(t0, {c["pid"]}, "d_tx", wait=0)) for c in w["children"]}
    par = mon.sum(t0, {w["pid"]}, "d_tx", wait=0)
    ok = all(a == b for a, b in per.values()) and par == w["parent_sent"]
    R.add(G, "fork_separate_pids", {"children": {str(k): {"truth": v[0], "tracker": v[1]} for k, v in per.items()},
                                    "parent_truth": w["parent_sent"], "parent_tracker": par,
                                    "summary": f"parent + 4 forked children: {sum(1 for a, b in per.values() if a == b)}/4 "
                                               f"children exact, parent {par}/{w['parent_sent']}"}, ok=ok)

    # 8. many short connections (socket address reuse)
    n = 200 if quick else 500
    port = next_port()
    sink = Proc("sink_tcp", "--port", port, "--count", n, wait_ready=True)
    t0 = mon.mark()
    w = Proc("short_conns", "--port", port, "--count", n, "--bytes", 20000).result()
    s = sink.result()
    meas = mon.sum(t0, {w["pid"]}, "d_tx")
    wire = mon.sum(t0, {w["pid"]}, "d_wire_tx", wait=0)
    R.add(G, "short_connections_reuse", {"connections": n, "truth": w["sent"], "tracker_tx": meas, "tracker_wire_tx": wire,
                                         "peer_received": s["received"],
                                         "summary": f"{n} short connections: app {meas}/{w['sent']} B, "
                                                    f"wire {wire} B ({pct(wire, w['sent']):.2f} %)"},
          ok=meas == w["sent"] and abs(wire - w["sent"]) <= 0.01 * w["sent"])

    # 9. loopback detection (127.x and the host's own LAN IP)
    rows = mon.sum(t0 - 1e9, wait=0)
    own = [ip for ip in netutil.local_addresses() if not ip.startswith("127.") and ip != "::1" and ":" not in ip]
    res = {"loopback_rows_flagged": all(r["loop"] for r in rows if r["raddr"].startswith("127.") or r["raddr"] == "::1")}
    if own:
        port = next_port()
        sink = Proc("sink_tcp", "--bind", own[0], "--port", port, "--count", 1, wait_ready=True)
        t0 = mon.mark()
        Proc("tcp_send", "--host", own[0], "--port", port, "--bytes", MB).result()
        sink.result()
        r2 = mon.sum(t0, None, raddr=own[0])
        res["own_ip"] = own[0]
        res["own_ip_flagged_loopback"] = bool(r2) and all(r["loop"] for r in r2)
    ok = res["loopback_rows_flagged"] and res.get("own_ip_flagged_loopback", True)
    res["summary"] = f"127.x flagged: {res['loopback_rows_flagged']}; own IP {res.get('own_ip', '-')} flagged: {res.get('own_ip_flagged_loopback', '-')}"
    R.add(G, "loopback_detection", res, ok=ok)


# ======================================================================== PERFORMANCE
def run_iperf(args, seconds):
    srv = subprocess.Popen(["iperf3", "-s", "-1", "-p", str(PORT[0] + 50)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    c0 = sys_cpu()
    cl = subprocess.Popen(["iperf3", "-c", "127.0.0.1", "-p", str(PORT[0] + 50), "-t", str(seconds), "-J"] + args,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, _ = cl.communicate(timeout=seconds + 60)
    c1 = sys_cpu()
    srv.wait(timeout=20)
    j = json.loads(out)
    end = j["end"]
    if "-u" in args:
        s = end["sum"]
        return {"bps": s["bits_per_second"], "bytes": s["bytes"], "packets": s.get("packets", 0),
                "lost_pct": s.get("lost_percent", 0), "sys_cpu_pct": cpu_busy_pct(c0, c1), "client_pid": cl.pid}
    return {"bps": end["sum_received"]["bits_per_second"], "bytes": end["sum_sent"]["bytes"],
            "sys_cpu_pct": cpu_busy_pct(c0, c1), "client_pid": cl.pid,
            "host_cpu_pct": end.get("cpu_utilization_percent", {}).get("host_total")}


def _default_signals():
    # a script started in the background inherits "ignore SIGINT"; undo that so Ctrl-C/SIGINT works
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


def stop_tool(p, sig=signal.SIGINT):
    """Stop a bwtrack process cleanly (it detaches its probes); kill it if it hangs."""
    if p.poll() is not None:
        return
    p.send_signal(sig)
    try:
        p.wait(timeout=30)
    except subprocess.TimeoutExpired:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


def start_tool(extra=()):
    """The real tool as a separate process: `bwtrack stream --json`."""
    p = subprocess.Popen([PY, "-m", "bwtracker", "stream", "--json", "--no-db", "--loopback", "--interval", "1",
                          *extra], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         preexec_fn=_default_signals)
    t0 = time.time()
    lines = []

    def reader():
        for line in p.stdout:
            lines.append((time.monotonic(), line))
    threading.Thread(target=reader, daemon=True).start()
    while time.time() - t0 < 300:
        if lines:
            return p, lines
        if p.poll() is not None:
            raise RuntimeError("bwtrack failed: " + p.stderr.read()[-800:])
        time.sleep(0.5)
    raise RuntimeError("bwtrack did not start")


def tool_bytes(lines, t0, pid, key="d_tx"):
    n = 0
    for t, line in lines:
        if t < t0:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        n += sum(f[key] for f in d["flows"] if f["tgid"] == pid)
    return n


def t_performance(R, quick, outdir):
    G = "performance"
    print("\n== performance (with vs without tracker) ==", flush=True)
    if not shutil.which("iperf3"):
        R.add(G, "throughput_overhead", {"summary": "skipped: iperf3 not installed (sudo apt install iperf3)"})
        return
    secs = 5 if quick else 10
    runs = 2 if quick else 3
    res = {"seconds": secs, "runs": runs, "tcp": {"off": [], "on": []}, "udp64": {"off": [], "on": []}}
    for i in range(runs):
        res["tcp"]["off"].append(run_iperf([], secs))
        res["udp64"]["off"].append(run_iperf(["-u", "-b", "0", "-l", "64"], secs))
    with open("/proc/sys/kernel/bpf_stats_enabled") as f:
        old = f.read().strip()
    tool, lines = start_tool()
    rss0 = proc_rss_mb(tool.pid)
    cpu_samples = []
    accuracy = []
    for i in range(runs):
        c0, t0 = proc_cpu_s(tool.pid), time.monotonic()
        r = run_iperf([], secs)
        time.sleep(3.5)
        r["tool_cpu_pct"] = 100 * (proc_cpu_s(tool.pid) - c0) / (time.monotonic() - t0)
        r["tool_seen_bytes"] = tool_bytes(lines, t0, r["client_pid"])
        accuracy.append((r["tool_seen_bytes"], r["bytes"]))
        res["tcp"]["on"].append(r)
        c0, t0 = proc_cpu_s(tool.pid), time.monotonic()
        r = run_iperf(["-u", "-b", "0", "-l", "64"], secs)
        time.sleep(3.5)
        r["tool_cpu_pct"] = 100 * (proc_cpu_s(tool.pid) - c0) / (time.monotonic() - t0)
        res["udp64"]["on"].append(r)
        cpu_samples.append(r["tool_cpu_pct"])
    c0, t0 = proc_cpu_s(tool.pid), time.monotonic()
    time.sleep(10)
    res["tool_idle_cpu_pct"] = 100 * (proc_cpu_s(tool.pid) - c0) / (time.monotonic() - t0)
    res["tool_rss_mb_start"], res["tool_rss_mb_end"] = round(rss0, 1), round(proc_rss_mb(tool.pid), 1)
    stop_tool(tool)
    # baseline again AFTER the tracker is gone, so slow drift (thermal, Wi-Fi, other load) cancels out
    for i in range(runs):
        res["tcp"]["off"].append(run_iperf([], secs))
        res["udp64"]["off"].append(run_iperf(["-u", "-b", "0", "-l", "64"], secs))

    def m(lst, k):
        v = [x[k] for x in lst]
        return statistics.mean(v), (statistics.stdev(v) if len(v) > 1 else 0.0)
    tcp_off, tcp_off_sd = m(res["tcp"]["off"], "bps")
    tcp_on, tcp_on_sd = m(res["tcp"]["on"], "bps")
    udp_off, _ = m(res["udp64"]["off"], "bps")
    udp_on, _ = m(res["udp64"]["on"], "bps")
    pps_off = statistics.mean(x["packets"] / secs for x in res["udp64"]["off"])
    pps_on = statistics.mean(x["packets"] / secs for x in res["udp64"]["on"])
    res["tcp_gbps_off"], res["tcp_gbps_on"] = round(tcp_off / 1e9, 3), round(tcp_on / 1e9, 3)
    res["tcp_gbps_off_sd"], res["tcp_gbps_on_sd"] = round(tcp_off_sd / 1e9, 3), round(tcp_on_sd / 1e9, 3)
    res["tcp_throughput_change_pct"] = round(100 * (tcp_on - tcp_off) / tcp_off, 2)
    res["udp64_kpps_off"], res["udp64_kpps_on"] = round(pps_off / 1e3, 1), round(pps_on / 1e3, 1)
    res["udp64_rate_change_pct"] = round(100 * (pps_on - pps_off) / pps_off, 2)
    res["tool_cpu_pct_under_tcp_load"] = round(statistics.mean(x["tool_cpu_pct"] for x in res["tcp"]["on"]), 2)
    res["tool_cpu_pct_under_udp_load"] = round(statistics.mean(cpu_samples), 2)
    res["accuracy_at_full_speed"] = [{"tracker": a, "iperf3": b, "pct": pct(a, b)} for a, b in accuracy]
    res["summary"] = (f"TCP loopback {res['tcp_gbps_off']} -> {res['tcp_gbps_on']} Gbit/s "
                      f"({res['tcp_throughput_change_pct']:+.1f} %); 64 B UDP {res['udp64_kpps_off']} -> "
                      f"{res['udp64_kpps_on']} kpps ({res['udp64_rate_change_pct']:+.1f} %); tool CPU "
                      f"{res['tool_cpu_pct_under_tcp_load']} % (TCP load), {res['tool_idle_cpu_pct']:.2f} % idle; "
                      f"RSS {res['tool_rss_mb_end']} MB; accuracy at full speed "
                      + ", ".join(f"{x['pct']:.3f} %" for x in res["accuracy_at_full_speed"]))
    R.add(G, "throughput_overhead", res)


def bpf_detail(tracker):
    """Per-program eBPF cost and map memory from the kernel (fdinfo)."""
    progs = {}
    for name, fn in tracker.kernel.b.funcs.items():
        name = name.decode() if isinstance(name, bytes) else name
        d = {}
        try:
            with open(f"/proc/self/fdinfo/{fn.fd}") as f:
                for line in f:
                    k, _, v = line.partition(":")
                    if k in ("run_time_ns", "run_cnt"):
                        d[k] = int(v)
        except OSError:
            pass
        progs[name] = d
    maps = {}
    for name in ("flows", "tcp_snaps", "sk_owner", "rx_seq", "udp_drops", "tcp_send_args", "udp_send_args"):
        try:
            fd = tracker.kernel.b[name].map_fd
            with open(f"/proc/self/fdinfo/{fd}") as f:
                for line in f:
                    if line.startswith("memlock:"):
                        maps[name] = int(line.split()[1])
        except Exception:
            pass
    return progs, maps


# ======================================================================== SCALABILITY
def t_scalability(mon, R, quick, tracker):
    G = "scalability"
    print("\n== scalability ==", flush=True)
    MB = 1 << 20
    levels = [1, 10, 50] if quick else [1, 10, 50, 100, 200]
    table = []
    for n in levels:
        port = next_port()
        sink = Proc("sink_tcp", "--port", port, "--count", n, wait_ready=True)
        h0 = len(mon.health)
        c0, t0 = netutil.self_cpu_seconds(), mon.mark()
        w = Proc("concurrent_ips", "--port", port, "--count", n, "--bytes", 2 * MB).result()
        sink.result()
        rows = mon.sum(t0, {w["pid"]})
        dt = time.monotonic() - t0
        per = collections.Counter()
        for r in rows:
            per[r["raddr"]] += r["d_tx"]
        exact = sum(1 for ip, b in w["per_ip"].items() if per.get(ip) == b)
        hs = [h for _, h in mon.health[h0:]]
        tr = time.perf_counter()
        nflows = len(tracker.kernel.read_flows())
        read_ms = (time.perf_counter() - tr) * 1000
        table.append({"connections": n, "exact_flows": exact, "total_truth": w["sent"], "total_tracker": sum(per.values()),
                      "collector_cpu_pct": round(100 * (netutil.self_cpu_seconds() - c0) / dt, 2),
                      "max_tracked_sockets": max([h.get("tracked_sockets", 0) for h in hs] or [0]),
                      "map_read_ms": round(read_ms, 2), "flows_in_map": nflows,
                      "kprobe_misses": hs[-1].get("kprobe_misses", 0) if hs else 0})
        print(f"    {n:4d} connections: {exact}/{n} exact, collector CPU {table[-1]['collector_cpu_pct']} %", flush=True)
    ok = all(r["exact_flows"] == r["connections"] for r in table)
    R.add(G, "concurrent_connections", {"levels": table, "summary": "; ".join(
        f"{r['connections']} conns: {r['exact_flows']}/{r['connections']} exact" for r in table)}, ok=ok)

    # processes
    nproc = 10 if quick else 50
    port = next_port()
    sink = Proc("sink_tcp", "--port", port, "--count", nproc, wait_ready=True)
    t0 = mon.mark()
    ws = [Proc("tcp_send", "--port", port, "--bytes", MB) for _ in range(nproc)]
    res = [w.result() for w in ws]
    sink.result()
    time.sleep(tracker.interval * 3 + 0.2)
    got = {r["pid"]: mon.sum(t0, {r["pid"]}, "d_tx", wait=0) for r in res}
    exact = sum(1 for r in res if got[r["pid"]] == r["sent"])
    R.add(G, "concurrent_processes", {"processes": nproc, "exact": exact,
                                      "mismatches": [{"pid": r["pid"], "truth": r["sent"], "tracker": got[r["pid"]]}
                                                     for r in res if got[r["pid"]] != r["sent"]],
                                      "summary": f"{nproc} processes at once: {exact}/{nproc} attributed exactly"},
          ok=exact == nproc)

    # event rate: tiny sends
    port = next_port()
    sink = Proc("sink_tcp", "--port", port, "--count", 1, wait_ready=True)
    p0, _ = bpf_detail(tracker)
    t0 = mon.mark()
    w = Proc("small_sends", "--port", port, "--size", 64, "--seconds", 3 if quick else 8).result()
    sink.result()
    meas = mon.sum(t0, {w["pid"]}, "d_tx")
    p1, maps = bpf_detail(tracker)
    hs = mon.health_since(t0)
    cost = {}
    for k in p1:
        dn = p1[k].get("run_cnt", 0) - p0.get(k, {}).get("run_cnt", 0)
        dt_ns = p1[k].get("run_time_ns", 0) - p0.get(k, {}).get("run_time_ns", 0)
        if dn > 0:
            cost[k] = {"runs": dn, "avg_ns": round(dt_ns / dn, 1)}
    R.add(G, "event_rate", {"send_calls": w["calls"], "calls_per_s": round(w["calls"] / w["seconds"]),
                            "truth": w["sent"], "tracker_tx": meas, "per_program_cost": cost, "map_memlock_bytes": maps,
                            "kprobe_misses": hs[-1].get("kprobe_misses", 0) if hs else None,
                            "perf_lost": hs[-1].get("perf_lost", 0) if hs else None,
                            "summary": f"{round(w['calls'] / w['seconds']):,} send() calls/s of 64 B: tracker "
                                       f"{meas}/{w['sent']} B; send probes "
                                       + ", ".join(f"{k} {v['avg_ns']} ns" for k, v in cost.items() if "send" in k)},
          ok=meas == w["sent"])


# ======================================================================== RELIABILITY
def t_reliability(R, quick, soak_min, outdir):
    G = "reliability"
    print("\n== reliability ==", flush=True)
    MB = 1 << 20

    # 1. undersized map: LRU eviction, no crash (and the same load with the default size)
    cap_default = None
    tool, lines = start_tool()
    port = next_port()
    usink = Proc("sink_udp", "--port", port, "--idle", 3, wait_ready=True)
    t0 = time.monotonic()
    w = Proc("udp_flows", "--port", port, "--count", 600, "--size", 200, "--seconds", 10).result()
    time.sleep(3.5)
    cap_default = pct(tool_bytes(lines, t0, w["pid"]), w["sent"])
    stop_tool(tool)
    usink.p.kill()
    tool, lines = start_tool(["--max-flows", "256"])
    port = next_port()
    usink = Proc("sink_udp", "--port", port, "--idle", 3, wait_ready=True)
    t0 = time.monotonic()
    w = Proc("udp_flows", "--port", port, "--count", 600, "--size", 200, "--seconds", 10).result()
    time.sleep(3.5)
    alive = tool.poll() is None
    seen = tool_bytes(lines, t0, w["pid"])
    flows_seen = 0
    for t, line in lines[-3:]:
        try:
            flows_seen = max(flows_seen, sum(1 for f in json.loads(line)["flows"] if f["tgid"] == w["pid"]))
        except ValueError:
            pass
    stop_tool(tool)
    usink.p.kill()
    R.add(G, "map_overflow_lru", {"map_capacity": 256, "flows_generated": 600, "tool_alive": alive,
                                  "truth": w["sent"], "tracker": seen, "captured_pct": pct(seen, w["sent"]),
                                  "captured_pct_default_16384": cap_default,
                                  "summary": f"600 UDP flows: default map {cap_default:.1f} % captured; forced 256-entry "
                                             f"map: tool alive={alive}, {pct(seen, w['sent']):.1f} % captured "
                                             f"(LRU evicts, memory stays bounded, no crash)"},
          ok=alive and cap_default > 99.9)

    # 2. tracker started in the middle of a transfer: no spike
    port = next_port()
    sink = Proc("sink_tcp", "--port", port, "--count", 1, wait_ready=True)
    rate = 2 * MB
    sender = Proc("tcp_send", "--port", port, "--bytes", 900 * rate, "--rate", rate, "--chunk", 16384)
    time.sleep(5)
    tool, lines = start_tool()      # returns once the probes are attached (compile can take a while)
    started_late = sender.p.poll() is None
    time.sleep(12)
    stop_tool(tool)
    sender.p.kill()
    sink.p.kill()
    rates_app, rates_wire = [], []
    for t, line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        for f in d["flows"]:
            if f["tgid"] == sender.pid:
                rates_app.append(f["tx_rate"])
                rates_wire.append(f["wire_tx_rate"])
    mx = max(rates_app + rates_wire + [0])
    R.add(G, "start_mid_transfer", {"true_rate_Bps": rate, "sender_running_when_attached": started_late,
                                    "app_rates": rates_app, "wire_rates": rates_wire,
                                    "max_reported_Bps": mx, "summary": f"tracker started 5 s into a 2 MB/s transfer: "
                                    f"max reported {mx / MB:.2f} MB/s (no spike from earlier bytes)"},
          ok=started_late and 0 < mx < rate * 1.6)

    # 3. soak test of the real recorder process
    db = os.path.join(outdir, "soak.db")
    rec = subprocess.Popen([PY, "-m", "bwtracker", "record", "--db", db, "--bucket", "10", "--interval", "1"],
                           cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                           preexec_fn=_default_signals)
    for _ in range(300):
        time.sleep(1)
        if os.path.exists(db) and os.path.getsize(db) > 0:
            break
    time.sleep(3)
    end = time.time() + soak_min * 60
    truth = collections.Counter()
    samples = []
    rounds = 0
    cpu0, t_start = proc_cpu_s(rec.pid), time.monotonic()
    port = next_port()
    tsink = Proc("sink_tcp", "--port", port, "--count", 0, wait_ready=True)
    usink = Proc("sink_udp", "--port", port, "--idle", 120, wait_ready=True)
    last_sample = 0
    while time.time() < end and rec.poll() is None:
        rounds += 1
        a = Proc("tcp_send", "--port", port, "--bytes", 3 * MB).result()
        b = Proc("udp_send", "--port", port, "--count", 300, "--size", 1000).result()
        c = Proc("short_conns", "--port", port, "--count", 20, "--bytes", 10000).result()
        for r in (a, b, c):
            truth[r["pid"]] += r["sent"]
        if time.monotonic() - last_sample > 30:
            last_sample = time.monotonic()
            samples.append({"min": round((time.monotonic() - t_start) / 60, 2), "rss_mb": round(proc_rss_mb(rec.pid), 1),
                            "cpu_s": round(proc_cpu_s(rec.pid) - cpu0, 2)})
        time.sleep(2)
    alive = rec.poll() is None
    elapsed = time.monotonic() - t_start
    cpu_pct = 100 * (proc_cpu_s(rec.pid) - cpu0) / elapsed
    time.sleep(12)
    stop_tool(rec, signal.SIGTERM)
    tsink.p.kill()
    usink.p.kill()
    con = sqlite3.connect(db)
    stored = dict(con.execute("SELECT pid, SUM(tx_bytes) FROM usage GROUP BY pid").fetchall())
    exact = sum(1 for pid, b in truth.items() if stored.get(pid) == b)
    rss = [s["rss_mb"] for s in samples]
    R.add(G, "soak", {"minutes": round(elapsed / 60, 1), "rounds": rounds, "processes": len(truth),
                      "recorder_alive": alive, "recorder_cpu_pct": round(cpu_pct, 2), "rss_samples": samples,
                      "rss_first_mb": rss[0] if rss else None, "rss_last_mb": rss[-1] if rss else None,
                      "exact_in_sqlite": exact, "truth_bytes": sum(truth.values()),
                      "sqlite_bytes": sum(stored.get(p, 0) for p in truth),
                      "summary": f"{elapsed / 60:.1f} min, {len(truth)} short-lived processes: recorder alive={alive}, "
                                 f"CPU {cpu_pct:.2f} %, RSS {rss[0] if rss else '?'} -> {rss[-1] if rss else '?'} MB, "
                                 f"{exact}/{len(truth)} processes exact in SQLite history"},
          ok=alive and exact == len(truth))


# ======================================================================== REPORT
def write_md(outdir, info, R, extra):
    L = [f"# bwtrack proof suite results\n", f"Host `{info.get('host')}`, {info.get('platform')}, kernel "
         f"`{info.get('kernel')}` {info.get('machine')}, CPU {info.get('cpu_model', '?')} x{info.get('cpus')}, "
         f"{info.get('mem_gb')} GB RAM, Python {info.get('python')}, BCC {info.get('bcc', '?')}. {info.get('time')}.\n"]
    for g in ("correctness", "performance", "scalability", "reliability"):
        items = [v for v in R.items.values() if v["group"] == g]
        if not items:
            continue
        L.append(f"\n## {g.capitalize()}\n\n| test | result | summary |\n|---|---|---|\n")
        for v in items:
            flag = {True: "PASS", False: "FAIL"}.get(v.get("pass"), "info")
            L.append(f"| `{v['name']}` | {flag} | {v.get('summary', '')} |\n")
    for name in ("throughput_overhead",):
        v = R.items.get(name)
        if v and "tcp_gbps_off" in v:
            L.append("\n## Throughput with vs without the tracker\n\n| workload | without | with | change |\n|---|---|---|---|\n")
            L.append(f"| TCP loopback (Gbit/s; without = {2 * v['runs']} runs before+after, with = {v['runs']} runs) | {v['tcp_gbps_off']} ± {v['tcp_gbps_off_sd']} | "
                     f"{v['tcp_gbps_on']} ± {v['tcp_gbps_on_sd']} | {v['tcp_throughput_change_pct']:+.2f} % |\n")
            L.append(f"| UDP 64 B packets (kpps) | {v['udp64_kpps_off']} | {v['udp64_kpps_on']} | {v['udp64_rate_change_pct']:+.2f} % |\n")
    v = R.items.get("concurrent_connections")
    if v:
        L.append("\n## Scalability\n\n| connections | exact flows | collector CPU % | sockets tracked | map read ms |\n|---|---|---|---|---|\n")
        for r in v["levels"]:
            L.append(f"| {r['connections']} | {r['exact_flows']}/{r['connections']} | {r['collector_cpu_pct']} | "
                     f"{r['max_tracked_sockets']} | {r['map_read_ms']} |\n")
    v = R.items.get("event_rate")
    if v and v.get("per_program_cost"):
        L.append("\n## eBPF cost per program run (kernel.bpf_stats)\n\n| program | runs | avg ns |\n|---|---|---|\n")
        for k, c in v["per_program_cost"].items():
            L.append(f"| `{k}` | {c['runs']:,} | {c['avg_ns']} |\n")
        if v.get("map_memlock_bytes"):
            tot = sum(v["map_memlock_bytes"].values())
            L.append(f"\nBPF map memory (memlock): {tot / 1024 / 1024:.1f} MB total: "
                     + ", ".join(f"{k} {b / 1024:.0f} KB" for k, b in v["map_memlock_bytes"].items()) + "\n")
    v = R.items.get("soak")
    if v and v.get("rss_samples"):
        L.append("\n## Soak: recorder memory over time\n\n| minute | RSS MB | CPU s |\n|---|---|---|\n")
        for s in v["rss_samples"]:
            L.append(f"| {s['min']} | {s['rss_mb']} | {s['cpu_s']} |\n")
    passed = sum(1 for x in R.items.values() if x.get("pass") is True)
    failed = sum(1 for x in R.items.values() if x.get("pass") is False)
    L.insert(1, f"\n**{passed} passed, {failed} failed** (plus informational measurements). Raw data: one JSON file per test in this folder.\n")
    with open(os.path.join(outdir, "summary.md"), "w") as f:
        f.write("".join(L))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="short version (~8 min)")
    ap.add_argument("--soak-min", type=float, default=None, help="soak duration in minutes (default 30, quick 2)")
    ap.add_argument("--only", default="correctness,performance,scalability,reliability")
    ap.add_argument("--out", default=os.path.join(ROOT, "experiments", "results"))
    a = ap.parse_args()
    if os.geteuid() != 0:
        print("run as root: sudo python3 experiments/proof_suite.py")
        return 2
    soak = a.soak_min if a.soak_min is not None else (2 if a.quick else 30)
    only = set(a.only.split(","))
    outdir = os.path.join(a.out, "proof_" + time.strftime("%Y%m%d-%H%M%S"))
    R = Results(outdir)
    info = env_info()
    with open(os.path.join(outdir, "environment.json"), "w") as f:
        json.dump(info, f, indent=2)
    print(f"bwtrack proof suite -> {outdir}\n{info}", flush=True)
    t_all = time.time()

    if "performance" in only:            # needs the system WITHOUT our tracker loaded first
        t_performance(R, a.quick, outdir)

    if only & {"correctness", "scalability"}:
        tracker = Tracker(interval=0.5, idle_timeout=20, bpf_stats=True)
        print("\nloading tracker ...", flush=True)
        try:
            tracker.start(log=lambda s: print("  ", s, flush=True))
        except TrackerError as e:
            print(e)
            return 2
        mon = Monitor(tracker)
        mon.start()
        try:
            if "correctness" in only:
                t_correctness(mon, R, a.quick)
            if "scalability" in only:
                t_scalability(mon, R, a.quick, tracker)
            progs, maps = bpf_detail(tracker)
            R.add("performance", "ebpf_cost_all_tests", {
                "per_program": {k: {"runs": v.get("run_cnt", 0),
                                    "avg_ns": round(v["run_time_ns"] / v["run_cnt"], 1) if v.get("run_cnt") else 0}
                                for k, v in progs.items()},
                "map_memlock_bytes": maps,
                "kprobe_misses": mon.health[-1][1].get("kprobe_misses") if mon.health else None,
                "perf_lost": mon.health[-1][1].get("perf_lost") if mon.health else None,
                "summary": f"{sum(v.get('run_cnt', 0) for v in progs.values()):,} eBPF runs, avg "
                           f"{sum(v.get('run_time_ns', 0) for v in progs.values()) / max(1, sum(v.get('run_cnt', 0) for v in progs.values())):.0f} ns; "
                           f"maps {sum(maps.values()) / 1048576:.1f} MB; kretprobe misses "
                           f"{mon.health[-1][1].get('kprobe_misses') if mon.health else '?'}, perf lost "
                           f"{mon.health[-1][1].get('perf_lost') if mon.health else '?'}"})
        finally:
            mon.stop_flag.set()
            time.sleep(1)
            tracker.stop()

    if "reliability" in only:
        t_reliability(R, a.quick, soak, outdir)

    write_md(outdir, info, R, None)
    print(f"\nDone in {(time.time() - t_all) / 60:.1f} min. Results: {outdir}/summary.md", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
