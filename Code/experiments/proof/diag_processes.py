#!/usr/bin/env python3
"""Follow-up for the concurrent_processes test: N processes start at the same
moment, each sends --bytes over TCP. Repeated --rounds times. For every
process we record truth vs tracker (two views: per-interval deltas summed,
and the tracker's own cumulative per-process total), read early (1.7 s) and
late (6 s), so we can tell a real miss from a "read too early" artefact.

    sudo python3 experiments/proof/diag_processes.py            # 5 rounds x 50 processes
"""
import argparse
import collections
import json
import os
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from bwtracker.collector import Tracker  # noqa: E402

W = os.path.join(ROOT, "experiments", "proof", "worker.py")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--procs", type=int, default=50)
    ap.add_argument("--bytes", type=int, default=1 << 20)
    ap.add_argument("--out", default=os.path.join(ROOT, "experiments", "results"))
    a = ap.parse_args()
    t = Tracker(interval=0.5, idle_timeout=60)
    t.start()
    lock = threading.Lock()
    rows = []          # (t, pid, d_tx)
    cum = {}           # pid -> cumulative tx from the tracker's own per-process accounting
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            s = t.poll()
            now = time.monotonic()
            with lock:
                for f in s.flows:
                    if f.d_tx:
                        rows.append((now, f.tgid, f.d_tx))
                for pid in {k[0] for k in s.proc_acc}:
                    cum[pid] = sum(v["tx_bytes"] for k, v in s.proc_acc.items() if k[0] == pid)
    th = threading.Thread(target=loop, daemon=True)
    th.start()
    report = {"procs": a.procs, "bytes": a.bytes, "rounds": []}
    port = 7100
    for rnd in range(a.rounds):
        port += 1
        sink = subprocess.Popen([sys.executable, W, "sink_tcp", "--port", str(port), "--count", str(a.procs)],
                                stdout=subprocess.PIPE, text=True)
        sink.stdout.readline()
        t0 = time.monotonic()
        ps = [subprocess.Popen([sys.executable, W, "tcp_send", "--port", str(port), "--bytes", str(a.bytes)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(a.procs)]
        truth = {}
        errors = []
        for p in ps:
            out, err = p.communicate()
            try:
                r = json.loads(out.strip().splitlines()[-1])
                truth[r["pid"]] = r["sent"]
            except Exception:
                errors.append({"pid": p.pid, "rc": p.returncode, "stderr": err[-300:]})
        try:
            sink.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            sink.kill()

        def view(after):
            time.sleep(after)
            with lock:
                per = collections.Counter()
                for tt, pid, d in rows:
                    if tt >= t0 and pid in truth:
                        per[pid] += d
                c = {pid: cum.get(pid) for pid in truth}
            return per, c
        early, cum_early = view(1.7)
        late, cum_late = view(4.3)
        bad = []
        for pid, b in truth.items():
            if early.get(pid, 0) != b or late.get(pid, 0) != b or cum_late.get(pid) != b:
                bad.append({"pid": pid, "truth": b, "early_sum": early.get(pid, 0), "late_sum": late.get(pid, 0),
                            "cumulative": cum_late.get(pid)})
        rr = {"round": rnd + 1, "worker_errors": errors, "exact_early": sum(1 for p in truth if early.get(p, 0) == truth[p]),
              "exact_late": sum(1 for p in truth if late.get(p, 0) == truth[p]),
              "exact_cumulative": sum(1 for p in truth if cum_late.get(p) == truth[p]),
              "processes_ok": len(truth), "mismatches": bad}
        report["rounds"].append(rr)
        print(f"round {rnd + 1}: {len(truth)} procs ran, exact early {rr['exact_early']}, late {rr['exact_late']}, "
              f"cumulative {rr['exact_cumulative']}, worker errors {len(errors)}", flush=True)
        for m in bad:
            print("   mismatch", m, flush=True)
        for e in errors:
            print("   worker error", e, flush=True)
    stop.set()
    time.sleep(1)
    report["kprobe_misses"] = t.kernel.kprobe_misses()
    report["perf_lost"] = t.kernel.perf_lost
    t.stop()
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "diag_processes_" + time.strftime("%Y%m%d-%H%M%S") + ".json")
    json.dump(report, open(path, "w"), indent=2)
    print("kprobe misses", report["kprobe_misses"], "perf lost", report["perf_lost"])
    print("saved", path)


if __name__ == "__main__":
    main()
