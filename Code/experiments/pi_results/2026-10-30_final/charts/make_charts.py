"""Regenerate the charts used in the final PPT and report from the raw results in ../
    python3 make_charts.py   (needs matplotlib)"""
import json, math, glob
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager as fm
for f in glob.glob("/usr/share/fonts/opentype/inter/Inter-*.otf"): fm.fontManager.addfont(f)
import os
HERE = os.path.dirname(os.path.abspath(__file__))
R = os.path.dirname(HERE) + "/"
OUT = HERE + "/"
INK, INK2, GRID = "#1d2630", "#5f6b76", "#e3e7ea"
C1, C2, C3 = "#2a78d6", "#eb6834", "#1baf7a"
plt.rcParams.update({"font.family": "Inter", "font.size": 11, "axes.edgecolor": INK2, "axes.labelcolor": INK2,
    "xtick.color": INK2, "ytick.color": INK2, "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": .8, "axes.axisbelow": True, "legend.frameon": False})
comb = json.load(open(R + "loss_combined/combined.json"))["groups"]
def g(mode, key):
    rows = [r for r in comb if r["mode"] == mode and r["delay_ms"] == 0]
    return [r["loss_pct"] for r in rows], [r[key]["mean"] for r in rows], [r[key]["sd"] for r in rows]

# A: TCP upload, wire vs app
fig, ax = plt.subplots(figsize=(4.6, 3.1), dpi=200)
x, m, s = g("tcp-up", "wire_overhead_pct")
_, a, _ = g("tcp-up", "app_accuracy_pct")
ax.errorbar(x, m, yerr=s, color=C1, lw=2, marker="o", ms=6, capsize=3, label="wire bytes above app bytes")
ax.plot(x, [v - 100 for v in a], color=C2, lw=2, marker="s", ms=6, label="app-layer error (vs truth)")
ax.set_xlabel("netem loss % (both directions)"); ax.set_ylabel("%")
ax.set_xticks([0, 1, 2, 5, 10]); ax.legend(loc="upper left", fontsize=9.5)
ax.annotate(f"+{m[-1]:.1f} %", (x[-1], m[-1]), textcoords="offset points", xytext=(-38, 4), color=INK, fontsize=10)
ax.annotate("0 % at every level", (5, 0), textcoords="offset points", xytext=(-20, 8), color=INK, fontsize=10)
fig.tight_layout(); fig.savefig(OUT + "tcp_wire.png"); plt.close(fig)

# B: UDP loss
fig, ax = plt.subplots(figsize=(4.6, 3.1), dpi=200)
x, t, ts = g("udp-down", "truth_loss_pct")
_, e, _ = g("udp-down", "ebpf_measured_loss_pct")
xu, tu, _ = g("udp-up", "truth_loss_pct")
ax.plot([0, 10.5], [0, 10.5], color=GRID, lw=1.2, ls="--", zorder=1)
ax.plot(x, t, color=C1, lw=2, marker="o", ms=9, label="true loss (sequence numbers)", zorder=2)
ax.plot(x, e, color=C2, lw=0, marker="x", ms=8, mew=2, label="eBPF at receiver (Pi)", zorder=3)
ax.plot(xu, [0] * len(xu), color=C3, lw=2, marker="^", ms=6, label="eBPF at sender sees 0 %")
ax.set_xlabel("netem loss %"); ax.set_ylabel("measured UDP loss %")
ax.set_xticks([0, 1, 2, 5, 10]); ax.legend(loc="upper left", fontsize=9.5)
fig.tight_layout(); fig.savefig(OUT + "udp_loss.png"); plt.close(fig)

# C: goodput 0 vs 20 ms delay, with Mathis model for 20 ms
d20 = [r for r in json.load(open(glob.glob(R + "delay20/*/results.json")[0]))["results"] if r["mode"] == "tcp-up"]
fig, ax = plt.subplots(figsize=(4.6, 3.1), dpi=200)
x, m, s = g("tcp-up", "goodput_mbps")
ax.errorbar(x, m, yerr=s, color=C1, lw=2, marker="o", ms=6, capsize=3, label="no extra delay (n=3)")
xd = [r["loss_pct"] for r in d20]; md = [r["goodput_mbps"] for r in d20]
ax.plot(xd, md, color=C2, lw=2, marker="s", ms=6, label="+20 ms each way")
mx = [r["loss_pct"] for r in d20 if r["loss_pct"] > 0]
mm = [1448 * 8 / (r["srtt_ms"] / 1000) * 1.22 / math.sqrt(r["loss_pct"] / 100) / 1e6 for r in d20 if r["loss_pct"] > 0]
ax.plot(mx, mm, color=INK2, lw=1.4, ls="--", label="Mathis model (measured RTT)")
ax.set_yscale("log"); ax.set_xlabel("netem loss %"); ax.set_ylabel("TCP upload goodput, Mbit/s")
ax.set_xticks([0, 1, 2, 5, 10]); ax.legend(loc="upper right", fontsize=9)
fig.tight_layout(); fig.savefig(OUT + "goodput_delay.png"); plt.close(fig)

# D: overhead
ov = json.load(open(R + "overhead_20261031-005043.json"))["workloads"]
names = [("wifi_tcp_up_mbps", "Wi-Fi TCP upload"), ("wifi_tcp_down_mbps", "Wi-Fi TCP download"),
         ("tcp_loopback_gbps", "loopback TCP, 70 Gbit/s"), ("udp64_loopback_kpps", "loopback UDP, 500 k pkt/s of 64 B")]
fig, ax = plt.subplots(figsize=(5.6, 2.5), dpi=200)
ys = list(range(len(names)))[::-1]
for y, (k, lab) in zip(ys, names):
    w = ov[k]; r = 100 * w["on"]["mean"] / w["off"]["mean"]
    err = r * math.sqrt((w["on"]["sd"] / w["on"]["mean"]) ** 2 + (w["off"]["sd"] / w["off"]["mean"]) ** 2)
    ax.barh(y, r, height=.55, color=C1 if k.startswith("wifi") else "#86b6ef")
    ax.errorbar(r, y, xerr=err, color=INK, capsize=3, lw=1)
    ax.text(120, y, f"{r:.1f} %", va="center", fontsize=10, color=INK)
ax.axvline(100, color=INK2, lw=1, ls="--")
ax.set_yticks(ys); ax.set_yticklabels([n[1] for n in names]); ax.set_xlim(0, 142); ax.set_xticks([0, 25, 50, 75, 100])
ax.set_xlabel("throughput with tracker, % of without")
ax.grid(axis="y", visible=False)
fig.tight_layout(); fig.savefig(OUT + "overhead.png"); plt.close(fig)

# E: soak RSS
soak = json.load(open(glob.glob(R + "proof_*/soak.json")[0]))
fig, ax = plt.subplots(figsize=(3.6, 2.0), dpi=200)
ax.plot([s["min"] for s in soak["rss_samples"]], [s["rss_mb"] for s in soak["rss_samples"]], color=C1, lw=2)
ax.set_ylim(0, 260); ax.set_xlabel("minutes"); ax.set_ylabel("recorder RSS, MB")
fig.tight_layout(); fig.savefig(OUT + "soak_rss.png"); plt.close(fig)

# F: scalability
sc = json.load(open(glob.glob(R + "proof_*/concurrent_connections.json")[0]))["levels"]
fig, ax = plt.subplots(figsize=(3.6, 2.0), dpi=200)
ax.plot([r["connections"] for r in sc], [r["collector_cpu_pct"] for r in sc], color=C1, lw=2, marker="o", ms=6)
ax.set_ylim(0, 15); ax.set_xlabel("simultaneous connections (all exact)"); ax.set_ylabel("collector CPU %")
fig.tight_layout(); fig.savefig(OUT + "scal_cpu.png"); plt.close(fig)
print("ok")

# G: time series, 10 % loss upload, run 1
import csv
rows = [r for r in csv.DictReader(open(glob.glob(R + "loss_run1/*/series.csv")[0])) if r["trial"] == "tcp-up@10.0" and r["proto"] == "TCP"]
t0 = float(rows[0]["t"])
ts = [float(r["t"]) - t0 for r in rows]
iv = [float(r["interval"]) for r in rows]
import itertools
fig, ax = plt.subplots(figsize=(4.6, 2.6), dpi=200)
app = list(itertools.accumulate(int(r["d_tx"]) / 1e6 for r in rows))
wire = list(itertools.accumulate(int(r["d_wire_tx"]) / 1e6 for r in rows))
ax.plot(ts, app, color=C2, lw=2, label="app layer: bytes accepted by send()")
ax.plot(ts, wire, color=C1, lw=2, label="TCP wire: incl. retransmissions")
ax.set_xlabel("seconds since start of transfer"); ax.set_ylabel("cumulative MB"); ax.set_ylim(0, 26)
ax.annotate(f"{wire[-1]:.1f} MB on the wire", (ts[-1], wire[-1]), textcoords="offset points", xytext=(-112, 6), fontsize=9.5, color=INK)
ax.legend(loc="lower right", fontsize=9)
fig.tight_layout(); fig.savefig(OUT + "series10.png"); plt.close(fig)
print("series ok", len(rows))
