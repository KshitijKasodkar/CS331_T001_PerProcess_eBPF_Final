"""Command line entry point:  sudo python3 -m bwtracker <command>  (or ./bwtrack <command>)."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

from . import __version__


def _common(p: argparse.ArgumentParser, live: bool = True) -> None:
    p.add_argument("--interval", type=float, default=1.0, help="sampling interval in seconds (default 1)")
    p.add_argument("--proto", choices=["all", "tcp", "udp", "ALL", "TCP", "UDP"], default="ALL",
                   help="protocol filter")
    p.add_argument("--loopback", action="store_true",
                   help="include loopback (127.0.0.1 / ::1) traffic; hidden by default because it never "
                        "touches the network")
    p.add_argument("--pid", type=int, help="only this process id")
    p.add_argument("--comm", help="only processes whose name contains this text")
    p.add_argument("--ip", help="only this remote IP or CIDR (e.g. 142.250.0.0/16)")
    p.add_argument("--idle-timeout", type=float, default=30.0,
                   help="forget sockets idle for this many seconds (default 30)")
    p.add_argument("--db", default=None, help="SQLite file for history (default: data/bwtracker.db)")
    p.add_argument("--no-db", action="store_true", help="do not record history")
    p.add_argument("--bucket", type=int, default=10, help="history resolution in seconds (default 10)")
    p.add_argument("--retention-days", type=float, default=30, help="delete history older than this")
    p.add_argument("--max-flows", type=int, default=16384,
                   help="capacity of the kernel flow maps (LRU; default 16384)")
    p.add_argument("--bpf-stats", action="store_true",
                   help="enable kernel.bpf_stats_enabled to measure eBPF overhead (ns per probe run)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bwtrack", description="Per-process network bandwidth tracker (eBPF)")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("live", help="interactive terminal dashboard (default)")
    _common(p)
    p.add_argument("--web", type=int, metavar="PORT", help="also serve the web UI + /metrics on PORT")
    p.add_argument("--bind", default="0.0.0.0")

    p = sub.add_parser("stream", help="print one plain-text table per interval (logs, ssh, screenshots)")
    _common(p)
    p.add_argument("--view", choices=["proc", "flow"], default="flow")
    p.add_argument("--count", type=int, default=0, help="stop after N intervals")
    p.add_argument("--json", action="store_true", help="one JSON object per interval")
    p.add_argument("--top", type=int, default=15)

    p = sub.add_parser("web", help="web dashboard + Prometheus /metrics")
    _common(p)
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--bind", default="0.0.0.0")

    p = sub.add_parser("record", help="headless: record history to SQLite (+ optional /metrics)")
    _common(p)
    p.add_argument("--prom-port", type=int, help="serve web UI and /metrics on this port")
    p.add_argument("--bind", default="0.0.0.0")

    p = sub.add_parser("events", help="live stream of TCP retransmissions and UDP drops")
    _common(p)

    p = sub.add_parser("report", help="historical usage report from SQLite")
    p.add_argument("--db", default=None)
    p.add_argument("--since", default="1h", help="30m, 2h, 7d or 'YYYY-MM-DD HH:MM' (default 1h)")
    p.add_argument("--until", default=None)
    p.add_argument("--by", choices=["name", "process", "remote", "proto", "port", "process-remote"], default="name",
                   help="name = per program (all its PIDs), process = per PID")
    p.add_argument("--proto", default=None)
    p.add_argument("--pid", type=int)
    p.add_argument("--comm")
    p.add_argument("--remote", help="only this remote IP")
    p.add_argument("--loopback", action="store_true", help="include loopback traffic")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--timeline", action="store_true", help="traffic over time instead of a ranking")
    p.add_argument("--bucket", type=int, default=60, help="timeline bucket in seconds")
    p.add_argument("--csv", help="write CSV to this file ('-' = stdout)")

    p = sub.add_parser("doctor", help="check kernel/BCC/headers and print fixes")
    p.add_argument("--compile", action="store_true", help="also trial-compile and attach the eBPF program")

    args = ap.parse_args(argv)
    if args.cmd is None:
        args = ap.parse_args(["live"] + (argv or sys.argv[1:]))

    if getattr(args, "proto", None):
        args.proto = args.proto.upper()
    if hasattr(args, "db"):
        from .storage import DEFAULT_DB
        args.db = args.db or os.environ.get("BWTRACK_DB") or DEFAULT_DB

    if args.cmd == "doctor":
        from . import doctor
        return doctor.run(args)
    if args.cmd == "report":
        from . import report
        return report.run(args)
    return run_live(args)


def run_live(args) -> int:
    from .collector import TrackerError
    from .runner import start_hub, wait_forever

    log = (lambda s: print(s, file=sys.stderr, flush=True))
    port = {"web": getattr(args, "port", None), "live": getattr(args, "web", None),
            "record": getattr(args, "prom_port", None)}.get(args.cmd)
    if port:  # fail fast, before the slow eBPF compile
        import socket
        try:
            with socket.socket() as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((args.bind, port))
        except OSError as e:
            print(f"ERROR: cannot listen on {args.bind}:{port} ({e.strerror}). "
                  f"Is another bwtrack running? Try another port.", file=sys.stderr)
            return 2
    try:
        hub = start_hub(args, log=log)
    except TrackerError as e:
        print(f"\nERROR: {e}\n", file=sys.stderr)
        return 2

    recorder = None
    if not args.no_db:
        from .storage import Recorder
        recorder = Recorder(args.db, bucket=args.bucket, retention_days=args.retention_days)
        hub.add(recorder)
        log(f"Recording history to {args.db} ({args.bucket}s buckets)")

    if port:
        from .prometheus import PromExporter
        from .webui import serve
        prom = PromExporter()
        hub.add(prom)
        serve(hub, prom, recorder, args.bind, port, args)
        log(f"Web UI:   http://{'localhost' if args.bind in ('0.0.0.0', '::') else args.bind}:{port}/   "
            f"Prometheus: /metrics")

    def _term(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)

    hub.start()
    try:
        if args.cmd == "live":
            from .tui import LiveUI
            LiveUI(hub, args).run()
        elif args.cmd == "stream":
            stream(hub, args)
        elif args.cmd == "events":
            events(hub)
        else:
            wait_forever(hub)
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop()
        if recorder:
            recorder.close()
        print("tracker stopped, probes detached.", file=sys.stderr)
    return 0


def stream(hub, args) -> None:
    from . import views
    from .model import filter_snapshot
    seq, n = -1, 0
    while True:
        snap, seq = hub.wait_next(seq)
        if hub.error:
            raise SystemExit(hub.error)
        if snap is None:
            continue
        view = filter_snapshot(snap, args.proto, args.loopback, args.pid, args.comm, args.ip, 5.0)
        if args.json:
            print(json.dumps(view.to_dict()), flush=True)
        else:
            print(f"\n=== {time.strftime('%H:%M:%S')}  interval {snap.interval:.2f}s " + "=" * 60)
            for line in views.header_lines(snap, view):
                print(line)
            if args.view == "proc":
                rows = [p for p in view.procs if p.tx_rate or p.rx_rate or p.wire_tx_rate][: args.top]
                cols = views.PROC_COLS
            else:
                rows = sorted(view.flows, key=views.SORTS["rate"][1])[: args.top]
                cols = views.FLOW_COLS
            for line in views.plain_table(rows, cols):
                print(line)
            for e in snap.events[-5:]:
                print("  event:", views.event_line(e))
            sys.stdout.flush()
        n += 1
        if args.count and n >= args.count:
            return


def events(hub) -> None:
    from . import views
    print("Waiting for TCP retransmissions / UDP drops (Ctrl+C to stop)...", flush=True)
    seq = -1
    while True:
        snap, seq = hub.wait_next(seq)
        if hub.error:
            raise SystemExit(hub.error)
        if snap:
            for e in snap.events:
                print(views.event_line(e), flush=True)


if __name__ == "__main__":
    sys.exit(main())
