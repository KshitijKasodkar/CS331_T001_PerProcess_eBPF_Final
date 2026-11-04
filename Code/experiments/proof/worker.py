#!/usr/bin/env python3
"""Workload processes for the proof suite. Each mode does one precisely
counted network activity and prints ONE JSON line with its own ground truth
(bytes, PIDs, addresses). Standard library only."""
from __future__ import annotations

import argparse
import errno
import json
import os
import select
import socket
import sys
import tempfile
import threading
import time


def out(obj):
    obj.setdefault("pid", os.getpid())
    print(json.dumps(obj), flush=True)


def recv_all(conn, bufsize=65536, flags=0):
    n = 0
    while True:
        b = conn.recv(bufsize, flags)
        if not b:
            return n
        n += len(b)


# --------------------------------------------------------------------- sinks
def sink_tcp(a):
    """Accept connections until --count connections finished (or forever).
    Prints the total received. Optional slow reader / small buffer / WAITALL."""
    fam = socket.AF_INET6 if ":" in a.bind else socket.AF_INET
    ls = socket.socket(fam, socket.SOCK_STREAM)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if a.rcvbuf:
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, a.rcvbuf)
    ls.bind((a.bind, a.port))
    ls.listen(1024)
    print(json.dumps({"ready": True, "pid": os.getpid()}), flush=True)
    total, done, lock = 0, 0, threading.Lock()
    per_peer = {}
    flags = socket.MSG_WAITALL if a.waitall else 0

    def handle(c, peer):
        nonlocal total, done
        n = 0
        try:
            while True:
                b = c.recv(a.chunk, flags)
                if not b:
                    break
                n += len(b)
                if a.slow:
                    time.sleep(a.slow)
        finally:
            c.close()
            with lock:
                total += n
                done += 1
                per_peer[peer] = per_peer.get(peer, 0) + n

    threads = []
    while a.count == 0 or len(threads) < a.count:
        c, addr = ls.accept()
        t = threading.Thread(target=handle, args=(c, addr[0]), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    out({"mode": "sink_tcp", "received": total, "connections": done, "per_peer": per_peer})


def sink_udp(a):
    fam = socket.AF_INET6 if ":" in a.bind else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    s.bind((a.bind, a.port))
    print(json.dumps({"ready": True, "pid": os.getpid()}), flush=True)
    s.settimeout(a.idle)
    n = pk = 0
    try:
        while True:
            b = s.recv(65535)
            n += len(b)
            pk += 1
    except socket.timeout:
        pass
    out({"mode": "sink_udp", "received": n, "packets": pk})


# --------------------------------------------------------------------- senders
def tcp_send(a):
    fam = socket.AF_INET6 if ":" in a.host else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    s.connect((a.host, a.port))
    total, chunk = a.bytes, bytes(a.chunk)
    sent = 0
    t0 = time.time()
    while sent < total:
        n = s.send(chunk[: min(len(chunk), total - sent)])
        sent += n
        if a.rate:   # bytes per second pacing
            ahead = sent / a.rate - (time.time() - t0)
            if ahead > 0:
                time.sleep(ahead)
    s.shutdown(socket.SHUT_WR)
    s.close()
    out({"mode": "tcp_send", "sent": sent, "host": a.host, "duration": time.time() - t0})


def tcp_nonblock(a):
    """Non-blocking sender against a slow reader: many send() calls return
    fewer bytes than requested or EAGAIN. Records requested vs accepted."""
    s = socket.create_connection((a.host, a.port))
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)
    s.setblocking(False)
    chunk = bytes(a.chunk)
    requested = accepted = calls = partial = eagain = 0
    t_end = time.time() + a.seconds
    while time.time() < t_end:
        calls += 1
        requested += len(chunk)
        try:
            n = s.send(chunk)
            accepted += n
            if n < len(chunk):
                partial += 1
        except BlockingIOError:
            eagain += 1
            select.select([], [s], [], 0.05)
    s.setblocking(True)
    s.shutdown(socket.SHUT_WR)
    s.close()
    out({"mode": "tcp_nonblock", "requested": requested, "accepted": accepted, "calls": calls,
         "partial_sends": partial, "eagain": eagain})


def tcp_sendfile(a):
    with tempfile.TemporaryFile() as f:
        f.truncate(a.bytes)
        s = socket.create_connection((a.host, a.port))
        sent = s.sendfile(f)
        s.shutdown(socket.SHUT_WR)
        s.close()
    out({"mode": "tcp_sendfile", "sent": sent})


def short_conns(a):
    chunk = bytes(a.bytes)
    total = 0
    for _ in range(a.count):
        s = socket.create_connection((a.host, a.port))
        s.sendall(chunk)
        total += len(chunk)
        s.close()
    out({"mode": "short_conns", "sent": total, "connections": a.count})


def threads_send(a):
    per = {}

    def run(i):
        s = socket.create_connection((a.host, a.port))
        s.sendall(bytes(a.bytes))
        s.close()
        per[i] = a.bytes

    ts = [threading.Thread(target=run, args=(i,)) for i in range(a.count)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    out({"mode": "threads", "sent": sum(per.values()), "threads": a.count})


def fork_send(a):
    r, w = os.pipe()
    kids = []
    for i in range(a.count):
        pid = os.fork()
        if pid == 0:
            os.close(r)
            s = socket.create_connection((a.host, a.port))
            s.sendall(bytes(a.bytes))
            s.close()
            os.write(w, (json.dumps({"pid": os.getpid(), "sent": a.bytes}) + "\n").encode())
            os._exit(0)
        kids.append(pid)
    os.close(w)
    for p in kids:
        os.waitpid(p, 0)
    data = os.read(r, 1 << 20).decode().strip().splitlines()
    s = socket.create_connection((a.host, a.port))
    s.sendall(bytes(a.parent_bytes))
    s.close()
    out({"mode": "fork", "children": [json.loads(x) for x in data], "parent_sent": a.parent_bytes})


def concurrent_ips(a):
    """N simultaneous TCP connections, each to a different 127.0.0.x address."""
    per = {}
    lock = threading.Lock()
    start = threading.Barrier(a.count)

    def run(i):
        ip = f"127.0.{(i + 2) // 250}.{(i + 2) % 250 + 1}" if a.count > 240 else f"127.0.0.{i + 2}"
        s = socket.create_connection((ip, a.port))
        start.wait()
        s.sendall(bytes(a.bytes))
        s.close()
        with lock:
            per[ip] = a.bytes

    ts = [threading.Thread(target=run, args=(i,)) for i in range(a.count)]
    t0 = time.time()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    out({"mode": "concurrent_ips", "per_ip": per, "sent": sum(per.values()), "duration": time.time() - t0})


def udp_multi(a):
    """One unconnected UDP socket, sendto() several destinations."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    per = {}
    dests = a.dests.split(",")
    for k, ip in enumerate(dests):
        n = 0
        for _ in range(a.count * (k + 1)):
            n += s.sendto(bytes(a.size), (ip, a.port))
            if _ % 50 == 0:
                time.sleep(0.001)
        per[ip] = n
    out({"mode": "udp_multi", "per_dest": per, "sent": sum(per.values())})


def udp_send(a):
    fam = socket.AF_INET6 if ":" in a.host else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_DGRAM)
    n = 0
    for i in range(a.count):
        n += s.sendto(bytes(a.size), (a.host, a.port))
        if i % 50 == 0:
            time.sleep(0.001)
    out({"mode": "udp_send", "sent": n, "host": a.host})


def small_sends(a):
    """Many tiny send() calls: stresses per-call probe cost."""
    s = socket.create_connection((a.host, a.port))
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    msg = bytes(a.size)
    n = calls = 0
    t_end = time.time() + a.seconds
    while time.time() < t_end:
        n += s.send(msg)
        calls += 1
    s.shutdown(socket.SHUT_WR)
    s.close()
    out({"mode": "small_sends", "sent": n, "calls": calls, "seconds": a.seconds})


def udp_flows(a):
    """Many UDP flows (distinct destination IPs) kept alive for a while."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    total = 0
    t_end = time.time() + a.seconds
    while time.time() < t_end:
        for i in range(a.count):
            ip = f"127.{1 + i // 62500}.{(i // 250) % 250}.{i % 250 + 1}"
            total += s.sendto(bytes(a.size), (ip, a.port))
        time.sleep(0.2)
    out({"mode": "udp_flows", "sent": total, "flows": a.count})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5400)
    ap.add_argument("--bytes", type=int, default=1 << 20)
    ap.add_argument("--parent-bytes", type=int, default=1 << 20)
    ap.add_argument("--chunk", type=int, default=65536)
    ap.add_argument("--count", type=int, default=0)
    ap.add_argument("--size", type=int, default=1000)
    ap.add_argument("--seconds", type=float, default=5)
    ap.add_argument("--rate", type=float, default=0)
    ap.add_argument("--slow", type=float, default=0)
    ap.add_argument("--rcvbuf", type=int, default=0)
    ap.add_argument("--waitall", action="store_true")
    ap.add_argument("--idle", type=float, default=3)
    ap.add_argument("--dests", default="127.0.0.1")
    a = ap.parse_args()
    {"sink_tcp": sink_tcp, "sink_udp": sink_udp, "tcp_send": tcp_send, "tcp_nonblock": tcp_nonblock,
     "tcp_sendfile": tcp_sendfile, "short_conns": short_conns, "threads": threads_send, "fork": fork_send,
     "concurrent_ips": concurrent_ips, "udp_multi": udp_multi, "udp_send": udp_send,
     "small_sends": small_sends, "udp_flows": udp_flows}[a.mode](a)


if __name__ == "__main__":
    main()
