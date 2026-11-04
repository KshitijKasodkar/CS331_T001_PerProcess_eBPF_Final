"""Historical storage in SQLite.

Every `bucket` seconds (default 10) the recorder writes one row per
(process, protocol, remote IP) with the bytes moved in that bucket. 10 s
buckets keep the database small enough for a Raspberry Pi SD card
(~1 row per active connection per 10 s) while still allowing minute-level
reports.
"""
from __future__ import annotations

import collections
import os
import platform
import sqlite3
import threading
import time
from typing import Dict, List, Optional, Tuple

from .model import Snapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    ts            INTEGER NOT NULL,   -- bucket start, unix seconds
    bucket        INTEGER NOT NULL,   -- bucket length in seconds
    pid           INTEGER NOT NULL,
    comm          TEXT    NOT NULL,
    proto         TEXT    NOT NULL,   -- TCP / UDP
    raddr         TEXT    NOT NULL,   -- remote IP
    rport         INTEGER NOT NULL,   -- remote port (0 = several)
    loopback      INTEGER NOT NULL,
    tx_bytes      INTEGER NOT NULL,   -- app payload sent   (socket layer)
    rx_bytes      INTEGER NOT NULL,   -- app payload read   (socket layer)
    wire_tx_bytes INTEGER NOT NULL,   -- TCP payload put on the wire incl. retransmissions
    retrans_bytes INTEGER NOT NULL,
    retrans_segs  INTEGER NOT NULL,
    udp_drops     INTEGER NOT NULL,
    max_srtt_us   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_ts   ON usage(ts);
CREATE INDEX IF NOT EXISTS idx_usage_pid  ON usage(pid, ts);
CREATE TABLE IF NOT EXISTS nic_usage (
    ts INTEGER NOT NULL, bucket INTEGER NOT NULL, iface TEXT NOT NULL,
    rx_bytes INTEGER NOT NULL, tx_bytes INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nic_ts ON nic_usage(ts);
CREATE TABLE IF NOT EXISTS events (
    ts REAL NOT NULL, kind TEXT NOT NULL, pid INTEGER, comm TEXT, proto TEXT,
    laddr TEXT, lport INTEGER, raddr TEXT, rport INTEGER, value INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bwtracker.db")


def connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


class Recorder:
    """Snapshot consumer that writes bucketed usage to SQLite."""

    def __init__(self, path: str = DEFAULT_DB, bucket: int = 10, retention_days: float = 30,
                 max_events_per_s: int = 200):
        self.path = path
        self.bucket = bucket
        self.retention_s = retention_days * 86400
        self.max_events_per_s = max_events_per_s
        self.conn = connect(path)
        self.lock = threading.Lock()
        self.cur_bucket: Optional[int] = None
        self.acc: Dict[Tuple, List[int]] = collections.defaultdict(lambda: [0] * 7)
        self.nic_acc: Dict[str, List[int]] = collections.defaultdict(lambda: [0, 0])
        self.last_purge = 0.0
        u = platform.uname()
        with self.lock:
            self.conn.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [
                ("host", u.node), ("kernel", u.release), ("machine", u.machine),
                ("started", str(int(time.time())))])
            self.conn.commit()

    def __call__(self, snap: Snapshot) -> None:
        b = int(snap.ts // self.bucket * self.bucket)
        if self.cur_bucket is None:
            self.cur_bucket = b
        if b != self.cur_bucket:
            self.flush()
            self.cur_bucket = b
        for f in snap.flows:
            if not (f.d_tx or f.d_rx or f.d_wire_tx or f.d_retrans_segs or f.d_udp_drops):
                continue
            a = self.acc[(f.tgid, f.comm, f.proto, f.raddr, f.rport, int(f.loopback))]
            a[0] += f.d_tx
            a[1] += f.d_rx
            a[2] += f.d_wire_tx
            a[3] += f.d_retrans_bytes
            a[4] += f.d_retrans_segs
            a[5] += f.d_udp_drops
            a[6] = max(a[6], f.srtt_us)
        for name, v in snap.nic.items():
            n = self.nic_acc[name]
            n[0] += int(v["rx_rate"] * snap.interval)
            n[1] += int(v["tx_rate"] * snap.interval)
        if snap.events:
            rows = [(e.ts, e.kind, e.tgid, e.comm, e.proto, e.laddr, e.lport, e.raddr, e.rport, e.value)
                    for e in snap.events[: int(self.max_events_per_s * max(snap.interval, 1))]]
            with self.lock:
                self.conn.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)", rows)

    def flush(self) -> None:
        if self.cur_bucket is None:
            return
        rows = [(self.cur_bucket, self.bucket, *k[:5], k[5], *v) for k, v in self.acc.items()]
        nic_rows = [(self.cur_bucket, self.bucket, name, v[0], v[1]) for name, v in self.nic_acc.items()
                    if v[0] or v[1]]
        self.acc.clear()
        self.nic_acc.clear()
        with self.lock:
            if rows:
                self.conn.executemany("INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            if nic_rows:
                self.conn.executemany("INSERT INTO nic_usage VALUES (?,?,?,?,?)", nic_rows)
            if time.time() - self.last_purge > 3600:
                cutoff = time.time() - self.retention_s
                for t in ("usage", "nic_usage", "events"):
                    self.conn.execute(f"DELETE FROM {t} WHERE ts < ?", (cutoff,))
                self.last_purge = time.time()
            self.conn.commit()

    def close(self) -> None:
        self.flush()
        with self.lock:
            self.conn.commit()
            self.conn.close()


# --------------------------------------------------------------------------
# Queries used by `bwtrack report` and the web UI history tab
# --------------------------------------------------------------------------

GROUPINGS = {
    "name": ("comm, COUNT(DISTINCT pid) AS pids", "comm"),
    "process": ("pid, comm", "pid, comm"),
    "remote": ("raddr", "raddr"),
    "proto": ("proto", "proto"),
    "port": ("proto, rport", "proto, rport"),
    "process-remote": ("pid, comm, proto, raddr", "pid, comm, proto, raddr"),
}


def _where(since: float, until: Optional[float], proto: Optional[str], include_loopback: bool,
           pid: Optional[int], comm: Optional[str], raddr: Optional[str]):
    w, p = ["ts >= ?"], [int(since)]
    if until:
        w.append("ts < ?")
        p.append(int(until))
    if proto and proto.upper() != "ALL":
        w.append("proto = ?")
        p.append(proto.upper())
    if not include_loopback:
        w.append("loopback = 0")
    if pid is not None:
        w.append("pid = ?")
        p.append(pid)
    if comm:
        w.append("comm LIKE ?")
        p.append(f"%{comm}%")
    if raddr:
        w.append("raddr = ?")
        p.append(raddr)
    return " AND ".join(w), p


def query_usage(conn, by: str = "process", since: float = 0, until: Optional[float] = None,
                proto: Optional[str] = None, include_loopback: bool = False, pid: Optional[int] = None,
                comm: Optional[str] = None, raddr: Optional[str] = None, top: int = 20) -> List[dict]:
    cols, group = GROUPINGS[by]
    where, params = _where(since, until, proto, include_loopback, pid, comm, raddr)
    sql = f"""
        SELECT {cols},
               SUM(tx_bytes) AS tx, SUM(rx_bytes) AS rx, SUM(wire_tx_bytes) AS wire_tx,
               SUM(retrans_bytes) AS retrans_bytes, SUM(retrans_segs) AS retrans_segs,
               SUM(udp_drops) AS udp_drops, MAX(max_srtt_us) AS max_srtt_us,
               MIN(ts) AS first_ts, MAX(ts + bucket) AS last_ts,
               GROUP_CONCAT(DISTINCT {'comm' if by == 'remote' else 'raddr'}) AS related
        FROM usage WHERE {where}
        GROUP BY {group}
        ORDER BY SUM(tx_bytes) + SUM(rx_bytes) DESC
        LIMIT ?"""
    cur = conn.execute(sql, (*params, top))
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def query_timeline(conn, since: float, bucket: int = 60, until: Optional[float] = None,
                   proto: Optional[str] = None, include_loopback: bool = False,
                   pid: Optional[int] = None, comm: Optional[str] = None,
                   raddr: Optional[str] = None) -> List[dict]:
    where, params = _where(since, until, proto, include_loopback, pid, comm, raddr)
    sql = f"""
        SELECT (ts / ?) * ? AS t, SUM(tx_bytes) AS tx, SUM(rx_bytes) AS rx,
               SUM(wire_tx_bytes) AS wire_tx, SUM(retrans_bytes) AS retrans_bytes,
               SUM(retrans_segs) AS retrans_segs, SUM(udp_drops) AS udp_drops
        FROM usage WHERE {where} GROUP BY t ORDER BY t"""
    cur = conn.execute(sql, (bucket, bucket, *params))
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def query_nic(conn, since: float, bucket: int = 60) -> List[dict]:
    cur = conn.execute("""SELECT (ts / ?) * ? AS t, iface, SUM(rx_bytes), SUM(tx_bytes)
                          FROM nic_usage WHERE ts >= ? AND iface != 'lo' GROUP BY t, iface ORDER BY t""",
                       (bucket, bucket, int(since)))
    return [{"t": r[0], "iface": r[1], "rx": r[2], "tx": r[3]} for r in cur.fetchall()]


def query_events(conn, since: float, kind: Optional[str] = None, limit: int = 200) -> List[dict]:
    sql = "SELECT * FROM events WHERE ts >= ?" + (" AND kind = ?" if kind else "") + " ORDER BY ts DESC LIMIT ?"
    params = (since, kind, limit) if kind else (since, limit)
    cur = conn.execute(sql, params)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def parse_since(s: str) -> float:
    """'90s', '15m', '2h', '7d', or an absolute 'YYYY-MM-DD[ HH:MM]' -> unix time."""
    s = s.strip()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s and s[-1] in units and s[:-1].replace(".", "", 1).isdigit():
        return time.time() - float(s[:-1]) * units[s[-1]]
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(s, fmt))
        except ValueError:
            pass
    raise ValueError(f"cannot parse time '{s}' (use e.g. 30m, 2h, 1d or 2026-10-26 14:00)")
