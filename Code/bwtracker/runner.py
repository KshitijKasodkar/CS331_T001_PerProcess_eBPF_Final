"""Runs the Tracker in a background thread and fans snapshots out to consumers
(terminal UI, web UI, SQLite recorder, Prometheus exporter)."""
from __future__ import annotations

import collections
import threading
import time
import traceback
from typing import Callable, Deque, List, Optional

from .collector import Tracker
from .model import Event, Snapshot


class Hub(threading.Thread):
    def __init__(self, tracker: Tracker, history_seconds: int = 600):
        super().__init__(name="collector", daemon=True)
        self.tracker = tracker
        self.consumers: List[Callable[[Snapshot], None]] = []
        self.latest: Optional[Snapshot] = None
        self.history: Deque[dict] = collections.deque(maxlen=history_seconds)
        self.proc_history: Deque[dict] = collections.deque(maxlen=history_seconds)
        self.events: Deque[Event] = collections.deque(maxlen=500)
        self.cond = threading.Condition()
        self.stop_flag = threading.Event()
        self.error: Optional[str] = None
        self.seq = 0

    def add(self, consumer: Callable[[Snapshot], None]) -> None:
        self.consumers.append(consumer)

    def run(self) -> None:
        try:
            while not self.stop_flag.is_set():
                snap = self.tracker.poll()
                self._record(snap)
                for c in self.consumers:
                    try:
                        c(snap)
                    except Exception:
                        traceback.print_exc()
        except Exception as e:  # surface the problem to the UI thread
            self.error = f"{type(e).__name__}: {e}"
            with self.cond:
                self.cond.notify_all()

    def _record(self, snap: Snapshot) -> None:
        t = snap.totals
        nic_rx = sum(v["rx_rate"] for k, v in snap.nic.items() if k != "lo")
        nic_tx = sum(v["tx_rate"] for k, v in snap.nic.items() if k != "lo")
        # "t" is a monotonic clock (never jumps); "ts" is wall-clock time, which
        # can jump on WSL2 or when NTP corrects the clock - so the chart uses "t".
        self.history.append({
            "t": round(time.monotonic(), 3), "ts": round(snap.ts, 3), "tx": t["tx_rate"], "rx": t["rx_rate"],
            "wire_tx": t["wire_tx_rate"], "retrans": t["retrans_rate"],
            "nic_tx": nic_tx, "nic_rx": nic_rx, "drops": t["d_udp_drops"]})
        self.proc_history.append({
            "ts": round(snap.ts, 3),
            "p": {f"{p.tgid}": [round(p.tx_rate), round(p.rx_rate)]
                  for p in snap.procs[:30] if p.tx_rate or p.rx_rate}})
        self.events.extend(snap.events)
        with self.cond:
            self.latest = snap
            self.seq += 1
            self.cond.notify_all()

    def wait_next(self, last_seq: int, timeout: float = 2.0):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != last_seq or self.error is not None, timeout=timeout)
            return self.latest, self.seq

    def stop(self) -> None:
        self.stop_flag.set()
        self.join(timeout=3)
        self.tracker.stop()


def start_hub(args, log=print) -> Hub:
    tracker = Tracker(interval=args.interval, idle_timeout=args.idle_timeout,
                      bpf_stats=getattr(args, "bpf_stats", False),
                      max_flows=getattr(args, "max_flows", 16384))
    tracker.start(log=log)
    hub = Hub(tracker)
    return hub


def wait_forever(hub: Hub) -> None:
    try:
        while hub.is_alive():
            time.sleep(0.5)
        if hub.error:
            print("collector stopped:", hub.error)
    except KeyboardInterrupt:
        pass
