#!/usr/bin/env python3
"""Throughput with vs without the tracker, done carefully.

The first proof-suite run showed that TCP over loopback on the Pi 5 jumps
between ~26 and ~70 Gbit/s even WITHOUT the tracker (it depends on which CPU
cores the two iperf3 processes land on), so a plain average is meaningless.
Here every iperf3 run is pinned to fixed cores (-A), and the real network
(Wi-Fi to the laptop) is measured too, which is what users actually see.

Order: OFF block, ON block (real `bwtrack stream` process), OFF block again,
so slow drift cancels out.

    sudo python3 experiments/proof/overhead_check.py --peer 10.58.105.150
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "experiments"))
import proof_suite as ps  # noqa: E402

TG = os.path.join(ROOT, "tools", "trafficgen.py")


def iperf(args, seconds, cores, port=5999):
    srv = subprocess.Popen(["iperf3", "-s", "-1", "-p", str(port)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    out = subprocess.run(["iperf3", "-c", "127.0.0.1", "-p", str(port), "-t", str(seconds), "-J", "-A", cores] + args,
                         capture_output=True, text=True, timeout=seconds + 60).stdout
    srv.wait(timeout=20)
    e = json.loads(out)["end"]
    if "-u" in args:
        return e["sum"]["packets"] / seconds / 1e3          # kpps
    return e["sum_received"]["bits_per_second"] / 1e9        # Gbit/s


def wifi(peer, mode, size):
    out = subprocess.run([sys.executable, TG, "client", peer, "--mode", mode, "--bytes", size],
                         capture_output=True, text=True, timeout=600).stdout
    r = json.loads(out.strip().splitlines()[-1])
    b = r.get("app_sent_bytes") or r.get("app_received_bytes")
    return b * 8 / r["duration"] / 1e6                        # Mbit/s


def stats(v):
    return {"values": [round(x, 3) for x in v], "mean": round(statistics.mean(v), 3),
            "median": round(statistics.median(v), 3), "sd": round(statistics.stdev(v), 3) if len(v) > 1 else 0.0}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--peer", help="laptop running `trafficgen.py server` (adds the real Wi-Fi measurement)")
    ap.add_argument("--runs", type=int, default=4, help="runs per workload per block")
    ap.add_argument("--seconds", type=int, default=8)
    ap.add_argument("--cores", default="2,3", help="iperf3 -A client,server cores")
    ap.add_argument("--size", default="40M", help="Wi-Fi transfer size")
    ap.add_argument("--out", default=os.path.join(ROOT, "experiments", "results"))
    a = ap.parse_args()
    if os.geteuid() != 0:
        print("run with sudo")
        return 2
    work = {"tcp_loopback_gbps": lambda: iperf([], a.seconds, a.cores),
            "udp64_loopback_kpps": lambda: iperf(["-u", "-b", "0", "-l", "64"], a.seconds, a.cores)}
    if a.peer:
        work["wifi_tcp_up_mbps"] = lambda: wifi(a.peer, "tcp-up", a.size)
        work["wifi_tcp_down_mbps"] = lambda: wifi(a.peer, "tcp-down", a.size)
    data = {k: {"off_before": [], "on": [], "off_after": []} for k in work}

    def block(name):
        print(f"-- block {name}", flush=True)
        for i in range(a.runs):
            for k, f in work.items():
                v = f()
                data[k][name].append(v)
                print(f"   {k:22s} run {i + 1}: {v:.3f}", flush=True)

    block("off_before")
    print("-- starting tracker (bwtrack stream) ...", flush=True)
    tool, _ = ps.start_tool()
    c0, t0 = ps.proc_cpu_s(tool.pid), time.monotonic()
    block("on")
    tool_cpu = 100 * (ps.proc_cpu_s(tool.pid) - c0) / (time.monotonic() - t0)
    rss = ps.proc_rss_mb(tool.pid)
    ps.stop_tool(tool)
    block("off_after")

    res = {"env": ps.env_info(), "runs_per_block": a.runs, "iperf_seconds": a.seconds, "pinned_cores": a.cores,
           "peer": a.peer, "tool_cpu_pct": round(tool_cpu, 2), "tool_rss_mb": round(rss, 1), "workloads": {}}
    lines = ["# Throughput with vs without the tracker (pinned cores, OFF-ON-OFF)\n\n",
             f"{res['env'].get('platform')}, kernel {res['env'].get('kernel')}. {a.runs} runs per block. "
             f"Tracker process CPU while ON: {res['tool_cpu_pct']} %, RSS {res['tool_rss_mb']} MB.\n\n",
             "| workload | without (mean ± sd, n) | with (mean ± sd, n) | change of mean | change of median |\n|---|---|---|---|---|\n"]
    for k, d in data.items():
        off = d["off_before"] + d["off_after"]
        so, sn = stats(off), stats(d["on"])
        ch = 100 * (sn["mean"] - so["mean"]) / so["mean"]
        chm = 100 * (sn["median"] - so["median"]) / so["median"]
        res["workloads"][k] = {"off": so, "on": sn, "off_before": stats(d["off_before"]), "off_after": stats(d["off_after"]),
                               "change_mean_pct": round(ch, 2), "change_median_pct": round(chm, 2)}
        lines.append(f"| {k} | {so['mean']} ± {so['sd']} ({len(off)}) | {sn['mean']} ± {sn['sd']} ({len(d['on'])}) | "
                     f"{ch:+.1f} % | {chm:+.1f} % |\n")
    os.makedirs(a.out, exist_ok=True)
    base = os.path.join(a.out, "overhead_" + time.strftime("%Y%m%d-%H%M%S"))
    json.dump(res, open(base + ".json", "w"), indent=2)
    open(base + ".md", "w").write("".join(lines))
    print("\n" + "".join(lines))
    print("saved", base + ".json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
