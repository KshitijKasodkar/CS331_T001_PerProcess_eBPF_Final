"""`bwtrack report` - historical usage reports from the SQLite database."""
from __future__ import annotations

import csv
import sys
import time

from . import storage
from .netutil import fmt_bits, fmt_bytes


def _bar(v: float, vmax: float, width: int = 40) -> str:
    n = 0 if vmax <= 0 else int(round(width * v / vmax))
    return "#" * n


def run(args) -> int:
    conn = storage.connect(args.db)
    since = storage.parse_since(args.since)
    until = storage.parse_since(args.until) if args.until else None
    span = f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(since))} -> " + (
        time.strftime('%Y-%m-%d %H:%M', time.localtime(until)) if until else "now")

    if args.timeline:
        rows = storage.query_timeline(conn, since, args.bucket, until, args.proto, args.loopback,
                                      args.pid, args.comm, args.remote)
        if args.csv:
            return _csv(args.csv, rows)
        print(f"Traffic timeline ({span}), {args.bucket}s buckets"
              + (f", pid {args.pid}" if args.pid else "") + (f", comm ~ {args.comm}" if args.comm else ""))
        if not rows:
            print("  no data - is the recorder running?  (sudo ./bwtrack record)")
            return 0
        vmax = max(r["tx"] + r["rx"] for r in rows)
        print(f"{'time':<16} {'up':>10} {'down':>10} {'avg rate':>14} {'retx':>6}  traffic")
        for r in rows:
            tot = r["tx"] + r["rx"]
            retx = f"{100 * r['retrans_bytes'] / r['wire_tx']:.1f}%" if r["wire_tx"] else "-"
            print(f"{time.strftime('%m-%d %H:%M:%S', time.localtime(r['t'])):<16} {fmt_bytes(r['tx']):>10} "
                  f"{fmt_bytes(r['rx']):>10} {fmt_bits(tot / args.bucket):>14} {retx:>6}  {_bar(tot, vmax)}")
        return 0

    rows = storage.query_usage(conn, args.by, since, until, args.proto, args.loopback,
                               args.pid, args.comm, args.remote, args.top)
    if args.csv:
        return _csv(args.csv, rows)
    title = {"name": "Top programs", "process": "Top processes (per PID)", "remote": "Top remote IPs", "proto": "By protocol",
             "port": "By protocol / remote port", "process-remote": "Top process -> remote IP pairs"}[args.by]
    print(f"{title} ({span})" + (f", proto {args.proto.upper()}" if args.proto else "")
          + ("" if args.loopback else ", loopback excluded"))
    if not rows:
        print("  no data - is the recorder running?  (sudo ./bwtrack record)")
        return 0
    key_cols = {"name": [("PROGRAM", 16, "comm"), ("PIDS", 5, "pids")],
                "process": [("PID", 7, "pid"), ("PROCESS", 16, "comm")],
                "remote": [("REMOTE IP", 40, "raddr")],
                "proto": [("PROTO", 6, "proto")],
                "port": [("PROTO", 6, "proto"), ("RPORT", 6, "rport")],
                "process-remote": [("PID", 7, "pid"), ("PROCESS", 16, "comm"), ("PROTO", 6, "proto"),
                                   ("REMOTE IP", 30, "raddr")]}[args.by]
    hdr = " ".join(f"{t:<{w}}" for t, w, _ in key_cols)
    print(f"{hdr} {'SENT':>10} {'RECEIVED':>10} {'TOTAL':>10} {'TCP RETX':>9} {'UDP DROP':>8}  "
          + ("PROCESSES" if args.by == "remote" else "REMOTE IPS" if args.by in ("process", "name") else ""))
    vmax = max(r["tx"] + r["rx"] for r in rows) or 1
    for r in rows:
        k = " ".join(f"{str(r[c])[:w]:<{w}}" for _, w, c in key_cols)
        retx = f"{100 * r['retrans_bytes'] / r['wire_tx']:.2f}%" if r["wire_tx"] else "-"
        related = (r.get("related") or "")
        rel_list = related.split(",") if related else []
        rel = ", ".join(rel_list[:3]) + (f" +{len(rel_list) - 3}" if len(rel_list) > 3 else "")
        print(f"{k} {fmt_bytes(r['tx']):>10} {fmt_bytes(r['rx']):>10} {fmt_bytes(r['tx'] + r['rx']):>10} "
              f"{retx:>9} {r['udp_drops'] or '-':>8}  {rel if args.by in ('remote', 'process', 'name') else ''}")
    tot_tx = sum(r["tx"] for r in rows)
    tot_rx = sum(r["rx"] for r in rows)
    print(f"{'(shown rows total)':<{len(hdr)}} {fmt_bytes(tot_tx):>10} {fmt_bytes(tot_rx):>10} "
          f"{fmt_bytes(tot_tx + tot_rx):>10}")
    return 0


def _csv(path: str, rows) -> int:
    f = sys.stdout if path == "-" else open(path, "w", newline="")
    if rows:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    if f is not sys.stdout:
        f.close()
        print(f"wrote {len(rows)} rows to {path}")
    return 0
