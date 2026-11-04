#!/usr/bin/env python3
"""Combine repeated runs of run_experiments.py into mean ± standard deviation.

    python3 experiments/combine_runs.py experiments/results/2026-11-0*_* --out experiments/results/combined_loss

Each input folder must contain results.json. Trials are grouped by
(mode, loss %, delay ms). Writes combined.md, combined.json, combined.csv.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import statistics

METRICS = {
    "tcp-up": ["app_accuracy_pct", "wire_overhead_pct", "retrans_pct", "ebpf_retrans_segs", "goodput_mbps",
               "interval_rate_dev_mean_pct", "wire_lag_s", "srtt_ms", "tracker_cpu_pct", "bpf_avg_ns", "bpf_cpu_pct",
               "kprobe_misses"],
    "tcp-sendfile": ["app_accuracy_pct", "wire_overhead_pct", "retrans_pct", "goodput_mbps"],
    "tcp-down": ["app_accuracy_pct", "goodput_mbps", "srtt_ms", "tracker_cpu_pct", "bpf_avg_ns", "kprobe_misses"],
    "udp-up": ["app_accuracy_pct", "truth_loss_pct", "sender_view_delivered_pct", "tracker_cpu_pct", "bpf_avg_ns"],
    "udp-down": ["app_accuracy_pct", "truth_loss_pct", "ebpf_measured_loss_pct", "tracker_cpu_pct", "bpf_avg_ns"],
}


def ms(v):
    v = [x for x in v if isinstance(x, (int, float))]
    if not v:
        return None
    return {"mean": round(statistics.mean(v), 3), "sd": round(statistics.stdev(v), 3) if len(v) > 1 else 0.0,
            "min": round(min(v), 3), "max": round(max(v), 3), "n": len(v)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    groups = collections.defaultdict(list)
    metas = []
    for d in a.dirs:
        p = os.path.join(d, "results.json")
        if not os.path.exists(p):
            print("skip (no results.json):", d)
            continue
        j = json.load(open(p))
        metas.append({"dir": os.path.basename(os.path.normpath(d)), **j.get("meta", {})})
        for r in j["results"]:
            mode = "tcp-sendfile" if r.get("sendfile") else r["mode"]
            groups[(mode, r.get("loss_pct", 0.0), r.get("delay_ms", 0.0))].append(r)
    os.makedirs(a.out, exist_ok=True)
    out = []
    for (mode, loss, delay), rs in sorted(groups.items()):
        row = {"mode": mode, "loss_pct": loss, "delay_ms": delay, "runs": len(rs)}
        for m in METRICS.get(mode, []):
            row[m] = ms([r.get(m) for r in rs])
        out.append(row)
    json.dump({"runs": metas, "groups": out}, open(os.path.join(a.out, "combined.json"), "w"), indent=2)
    with open(os.path.join(a.out, "combined.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["mode", "loss_pct", "delay_ms", "runs", "metric", "mean", "sd", "min", "max"])
        for row in out:
            for m in METRICS.get(row["mode"], []):
                v = row.get(m)
                if v:
                    w.writerow([row["mode"], row["loss_pct"], row["delay_ms"], row["runs"], m,
                                v["mean"], v["sd"], v["min"], v["max"]])
    L = [f"# Combined results ({len(metas)} runs)\n\n"]
    for m in metas:
        L.append(f"- `{m['dir']}`: {m.get('host')} {m.get('platform')} kernel {m.get('kernel')}, iface "
                 f"{m.get('iface')}, peer {m.get('peer')}, delay {m.get('delay_ms')} ms, {m.get('started')}\n")
    for mode in METRICS:
        rows = [r for r in out if r["mode"] == mode]
        if not rows:
            continue
        cols = [c for c in METRICS[mode] if any(r.get(c) for r in rows)]
        L.append(f"\n## {mode}  (mean ± sd over runs)\n\n| loss % | delay ms | runs | " + " | ".join(cols) + " |\n")
        L.append("|" + "---|" * (3 + len(cols)) + "\n")
        for r in rows:
            cells = []
            for c in cols:
                v = r.get(c)
                cells.append("-" if not v else (f"{v['mean']:g} ± {v['sd']:g}" if v["n"] > 1 else f"{v['mean']:g}"))
            L.append(f"| {r['loss_pct']:g} | {r['delay_ms']:g} | {r['runs']} | " + " | ".join(cells) + " |\n")
    open(os.path.join(a.out, "combined.md"), "w").write("".join(L))
    print("".join(L))
    print("written:", a.out)


if __name__ == "__main__":
    main()
