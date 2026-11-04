"""Live terminal dashboard (curses, Python standard library only).

Keys:  p/f  process view / flow (per remote IP) view
       t    protocol filter ALL -> TCP -> UDP
       s    cycle sort order          l  show/hide loopback traffic
       i    show/hide idle rows       e  show/hide event log
       up/down  scroll                q  quit
"""
from __future__ import annotations

import curses
import time

from . import views
from .model import filter_snapshot
from .runner import Hub


class LiveUI:
    def __init__(self, hub: Hub, args):
        self.hub = hub
        self.view = "proc"
        self.proto = (args.proto or "ALL").upper()
        self.sort = "rate"
        self.loopback = args.loopback
        self.show_idle = False
        self.show_events = True
        self.scroll = 0
        self.args = args

    def run(self) -> None:
        curses.wrapper(self._main)

    def _colors(self):
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        pairs = {"title": curses.COLOR_CYAN, "tx": curses.COLOR_MAGENTA, "rx": curses.COLOR_GREEN,
                 "wire": curses.COLOR_BLUE, "retx": curses.COLOR_RED, "drop": curses.COLOR_RED,
                 "pid": curses.COLOR_CYAN, "proto": curses.COLOR_YELLOW, "remote": curses.COLOR_CYAN,
                 "dim": curses.COLOR_WHITE, "name": -1}
        self.cp = {}
        for i, (k, c) in enumerate(pairs.items(), start=1):
            curses.init_pair(i, c, bg)
            self.cp[k] = curses.color_pair(i)
        self.cp[""] = 0

    def _main(self, scr) -> None:
        curses.curs_set(0)
        scr.nodelay(True)
        scr.timeout(150)
        self._colors()
        seq = -1
        snap = None
        dirty = True
        while True:
            if self.hub.error:
                raise SystemExit(f"collector stopped: {self.hub.error}")
            with self.hub.cond:
                if self.hub.seq != seq:
                    snap, seq, dirty = self.hub.latest, self.hub.seq, True
            ch = scr.getch()
            if ch != -1:
                dirty = True
                if ch in (ord("q"), ord("Q"), 27):
                    return
                self._key(ch)
            if dirty and snap is not None:
                self._draw(scr, snap)
                dirty = False

    def _key(self, ch: int) -> None:
        c = chr(ch) if 0 <= ch < 256 else ""
        if c == "p":
            self.view, self.scroll = "proc", 0
        elif c == "f":
            self.view, self.scroll = "flow", 0
        elif c == "t":
            self.proto = {"ALL": "TCP", "TCP": "UDP", "UDP": "ALL"}[self.proto]
        elif c == "s":
            self.sort = views.SORT_ORDER[(views.SORT_ORDER.index(self.sort) + 1) % len(views.SORT_ORDER)]
        elif c == "l":
            self.loopback = not self.loopback
        elif c == "i":
            self.show_idle = not self.show_idle
        elif c == "e":
            self.show_events = not self.show_events
        elif ch == curses.KEY_DOWN:
            self.scroll += 1
        elif ch == curses.KEY_UP:
            self.scroll = max(0, self.scroll - 1)

    def _put(self, scr, y, x, text, attr=0):
        h, w = scr.getmaxyx()
        if y >= h or x >= w:
            return
        try:
            scr.addnstr(y, x, text, max(0, w - x - 1), attr)
        except curses.error:
            pass

    def _draw(self, scr, snap) -> None:
        scr.erase()
        h, w = scr.getmaxyx()
        a = self.args
        view = filter_snapshot(snap, self.proto, self.loopback, a.pid, a.comm, a.ip,
                               None if self.show_idle else 10.0)
        title = (f" bwtrack - eBPF per-process bandwidth   {time.strftime('%H:%M:%S')}   "
                 f"interval {snap.interval:.2f}s ")
        self._put(scr, 0, 0, title.ljust(w), self.cp["title"] | curses.A_REVERSE)
        y = 1
        for line in views.header_lines(snap, view):
            self._put(scr, y, 1, line)
            y += 1
        loop_txt = "shown" if self.loopback else f"hidden ({int(view.totals.get('loopback_hidden', 0))})"
        status = (f" view: {'PROCESSES' if self.view == 'proc' else 'FLOWS per remote IP'}   proto: {self.proto}"
                  f"   sort: {views.SORTS[self.sort][0]}   loopback: {loop_txt}   idle: "
                  f"{'shown' if self.show_idle else 'hidden'}")
        self._put(scr, y, 0, status, curses.A_BOLD)
        y += 1
        self._put(scr, y, 0, " [p]rocesses [f]lows [t]proto [s]ort [l]oopback [i]dle [e]vents [q]uit",
                  self.cp["dim"] | curses.A_DIM)
        y += 2

        if self.view == "proc":
            rows, cols = view.procs, views.PROC_COLS
            if not self.show_idle:
                rows = [p for p in rows if p.tx_rate or p.rx_rate or p.wire_tx_rate]
        else:
            rows, cols = view.flows, views.FLOW_COLS
        rows = sorted(rows, key=views.SORTS[self.sort][1])
        cols = views.fit_columns(cols, w - 1)

        x = 0
        for c in cols:
            self._put(scr, y, x, views.cell(c[0], c[1], c[2]), curses.A_BOLD | curses.A_UNDERLINE)
            x += c[1] + 1
        y += 1

        ev_lines = list(self.hub.events)[-6:] if self.show_events else []
        room = h - y - (len(ev_lines) + 2 if ev_lines else 1)
        self.scroll = min(self.scroll, max(0, len(rows) - room))
        for r in rows[self.scroll: self.scroll + max(0, room)]:
            x = 0
            idle = getattr(r, "idle_s", 0) > 3 and not (r.tx_rate or r.rx_rate)
            for c in cols:
                attr = self.cp.get(c[4], 0)
                txt = c[3](r)
                if c[4] in ("retx", "drop") and txt in ("-", "0.0%"):
                    attr = 0
                if idle or getattr(r, "loopback", False):
                    attr |= curses.A_DIM
                self._put(scr, y, x, views.cell(txt, c[1], c[2]), attr)
                x += c[1] + 1
            y += 1
        if not rows:
            self._put(scr, y, 2, "No matching network activity yet. Generate some traffic "
                                 "(curl, ping is ICMP and NOT tracked, iperf3, browser...).", curses.A_DIM)
        if ev_lines:
            ey = h - len(ev_lines) - 1
            self._put(scr, ey, 0, " recent events (retransmissions = packet loss on the path) ".ljust(w, "-"),
                      self.cp["dim"])
            for i, e in enumerate(ev_lines):
                self._put(scr, ey + 1 + i, 1, views.event_line(e), self.cp["retx"])
        scr.refresh()
