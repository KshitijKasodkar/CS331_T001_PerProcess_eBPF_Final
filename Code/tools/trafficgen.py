#!/usr/bin/env python3
"""Traffic generator with ground truth - pure standard library, also runs on Windows.

The tracker's numbers are only meaningful if we can compare them with what
was REALLY sent and received. This tool moves an exact number of bytes and
reports what each end saw.

  Server (on the other machine, e.g. your laptop - Windows Python works):
      python3 trafficgen.py server --port 5300

  Client (on the Pi, while bwtrack runs):
      python3 trafficgen.py client 192.168.1.20 --mode tcp-up   --bytes 50M
      python3 trafficgen.py client 192.168.1.20 --mode tcp-down --bytes 50M
      python3 trafficgen.py client 192.168.1.20 --mode udp-up   --rate 20M --seconds 5
      python3 trafficgen.py client 192.168.1.20 --mode udp-down --rate 20M --seconds 5

UDP datagrams carry a sequence number, so the receiver counts exactly how
many were lost on the way (loss% = 1 - received/sent).
The client prints one JSON line with the results.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
import threading
import time

HDR = struct.Struct("!QII")   # session id, sequence number, total header magic
MAGIC = 0xB17EB17E


def parse_size(s: str) -> int:
    s = str(s).strip().upper().rstrip("B")
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
    if s and s[-1] in mult:
        return int(float(s[:-1]) * mult[s[-1]])
    return int(float(s))


def parse_rate(s: str) -> float:
    """'20M' -> 20e6 bit/s."""
    s = str(s).strip().upper().rstrip("BIT/S").rstrip("B")
    mult = {"K": 1e3, "M": 1e6, "G": 1e9}
    if s and s[-1] in mult:
        return float(s[:-1]) * mult[s[-1]]
    return float(s)


CTRL = {"tx": 0, "rx": 0}   # bytes of JSON control messages (so experiments can subtract them)


def recv_line(sock: socket.socket) -> dict:
    buf = b""
    while not buf.endswith(b"\n"):
        c = sock.recv(1)
        if not c:
            raise ConnectionError("peer closed")
        buf += c
    CTRL["rx"] += len(buf)
    return json.loads(buf)


def send_line(sock: socket.socket, obj: dict) -> None:
    data = (json.dumps(obj) + "\n").encode()
    CTRL["tx"] += len(data)
    sock.sendall(data)


# ---------------------------------------------------------------- UDP helpers

class UdpReceiver:
    """Counts datagrams of one session by sequence number."""

    def __init__(self, sock: socket.socket, session: int):
        self.sock, self.session = sock, session
        self.pkts = self.bytes = 0
        self.max_seq = -1
        self.reordered = self.dups = 0
        self.seen = set()
        self.stop = threading.Event()
        self.first = self.last = None

    def run(self) -> None:
        self.sock.settimeout(0.2)
        while not self.stop.is_set():
            try:
                data, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) < HDR.size:
                continue
            sid, seq, magic = HDR.unpack_from(data)
            if magic != MAGIC or sid != self.session:
                continue
            if seq in self.seen:
                self.dups += 1
                continue
            self.seen.add(seq)
            now = time.time()
            self.first = self.first or now
            self.last = now
            self.pkts += 1
            self.bytes += len(data)
            if seq < self.max_seq:
                self.reordered += 1
            self.max_seq = max(self.max_seq, seq)

    def result(self) -> dict:
        return {"received_pkts": self.pkts, "received_bytes": self.bytes, "reordered": self.reordered,
                "duplicates": self.dups,
                "duration": (self.last - self.first) if self.first and self.last else 0.0}


def udp_send(sock: socket.socket, addr, session: int, rate_bps: float, seconds: float, size: int) -> dict:
    """Paced sender: `size`-byte datagrams at `rate_bps` for `seconds`."""
    payload = bytearray(size)
    gap = size * 8 / rate_bps
    seq = sent = errors = 0
    t0 = time.perf_counter()
    nxt = t0
    while True:
        now = time.perf_counter()
        if now - t0 >= seconds:
            break
        if now < nxt:
            time.sleep(min(nxt - now, 0.002))
            continue
        HDR.pack_into(payload, 0, session, seq, MAGIC)
        try:
            sent += sock.sendto(payload, addr)
            seq += 1
        except OSError:          # ENOBUFS etc.: local queue full -> datagram never left
            errors += 1
        nxt += gap
    return {"sent_pkts": seq, "sent_bytes": sent, "send_errors": errors,
            "duration": time.perf_counter() - t0}


# ---------------------------------------------------------------- server

def serve(args) -> None:
    lsock = socket.socket(socket.AF_INET6 if ":" in args.bind else socket.AF_INET, socket.SOCK_STREAM)
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind((args.bind, args.port))
    lsock.listen(16)
    print(f"trafficgen server listening on {args.bind}:{args.port} (TCP control/data, UDP same port)", flush=True)
    while True:
        conn, peer = lsock.accept()
        threading.Thread(target=handle, args=(conn, peer, args), daemon=True).start()


def handle(conn: socket.socket, peer, args) -> None:
    try:
        req = recv_line(conn)
        mode = req["mode"]
        print(f"{time.strftime('%H:%M:%S')} {peer[0]} {mode} {req}", flush=True)
        if mode == "tcp-up":
            n, t0 = 0, time.time()
            while True:
                b = conn.recv(1 << 16)
                if not b:
                    break
                n += len(b)
            send_line(conn, {"received_bytes": n, "duration": time.time() - t0})
        elif mode == "tcp-down":
            total, chunk = int(req["bytes"]), bytes(1 << 16)
            sent = 0
            while sent < total:
                sent += conn.send(chunk[: min(len(chunk), total - sent)])
            conn.shutdown(socket.SHUT_WR)
            conn.recv(1)
        elif mode in ("udp-up", "udp-down"):
            fam = socket.AF_INET6 if ":" in args.bind else socket.AF_INET
            us = socket.socket(fam, socket.SOCK_DGRAM)
            us.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
            us.bind((args.bind, 0))
            send_line(conn, {"udp_port": us.getsockname()[1]})
            if mode == "udp-up":
                rx = UdpReceiver(us, req["session"])
                th = threading.Thread(target=rx.run, daemon=True)
                th.start()
                done = recv_line(conn)            # client finished sending
                time.sleep(1.0)                   # stragglers still in flight
                rx.stop.set()
                th.join()
                res = rx.result()
                res["sent_pkts"] = done["sent_pkts"]
                send_line(conn, res)
            else:
                hello = recv_line(conn)           # client's UDP port
                res = udp_send(us, (peer[0], hello["client_udp_port"]), req["session"],
                               float(req["rate"]), float(req["seconds"]), int(req["size"]))
                send_line(conn, res)
            us.close()
    except Exception as e:
        print("session error:", e, flush=True)
    finally:
        conn.close()


# ---------------------------------------------------------------- client

def client(args) -> dict:
    session = int.from_bytes(os.urandom(8), "big")
    res = {"mode": args.mode, "peer": args.host, "pid": os.getpid()}
    c = socket.create_connection((args.host, args.port), timeout=30)
    c.settimeout(300)   # at 10 % loss TCP backs off (RTO doubles); the final reply can take minutes
    res["local_port"] = c.getsockname()[1]
    t0 = time.time()
    if args.mode == "tcp-up":
        total = parse_size(args.bytes)
        send_line(c, {"mode": "tcp-up"})
        sent = 0
        if args.sendfile:          # zero-copy path: kernel uses tcp_sendpage (<6.5) or splice
            import tempfile
            with tempfile.TemporaryFile() as f:
                f.truncate(total)
                sent = c.sendfile(f)
        else:
            chunk = bytes(1 << 16)
            while sent < total:
                sent += c.send(chunk[: min(len(chunk), total - sent)])
        res["app_sent_bytes"] = sent
        c.shutdown(socket.SHUT_WR)
        peer = recv_line(c)
        res["peer_received_bytes"] = peer["received_bytes"]
    elif args.mode == "tcp-down":
        total = parse_size(args.bytes)
        send_line(c, {"mode": "tcp-down", "bytes": total})
        n = 0
        while True:
            b = c.recv(1 << 16)
            if not b:
                break
            n += len(b)
        c.send(b"x")
        CTRL["tx"] += 1
        res["app_received_bytes"] = n
        res["peer_sent_bytes"] = total
    else:
        rate = parse_rate(args.rate)
        send_line(c, {"mode": args.mode, "session": session, "rate": rate,
                      "seconds": args.seconds, "size": args.size})
        uport = recv_line(c)["udp_port"]
        fam = c.family
        us = socket.socket(fam, socket.SOCK_DGRAM)
        us.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        if args.mode == "udp-up":
            us.connect((args.host, uport))
            res["udp_local_port"] = us.getsockname()[1]
            s = udp_send(us, (args.host, uport), session, rate, args.seconds, args.size)
            send_line(c, {"sent_pkts": s["sent_pkts"]})
            peer = recv_line(c)
            res.update({"app_sent_bytes": s["sent_bytes"], "sent_pkts": s["sent_pkts"],
                        "send_errors": s["send_errors"], "peer_received_pkts": peer["received_pkts"],
                        "peer_received_bytes": peer["received_bytes"], "reordered": peer["reordered"]})
        else:
            us.bind(("::" if fam == socket.AF_INET6 else "0.0.0.0", 0))
            res["udp_local_port"] = us.getsockname()[1]
            rx = UdpReceiver(us, session)
            th = threading.Thread(target=rx.run, daemon=True)
            th.start()
            send_line(c, {"client_udp_port": us.getsockname()[1]})
            peer = recv_line(c)
            time.sleep(1.0)
            rx.stop.set()
            th.join()
            r = rx.result()
            res.update({"peer_sent_pkts": peer["sent_pkts"], "peer_sent_bytes": peer["sent_bytes"],
                        "app_received_pkts": r["received_pkts"], "app_received_bytes": r["received_bytes"],
                        "reordered": r["reordered"]})
        us.close()
        sent = res.get("sent_pkts", res.get("peer_sent_pkts", 0))
        got = res.get("peer_received_pkts", res.get("app_received_pkts", 0))
        res["udp_loss_pct"] = 100.0 * (sent - got) / sent if sent else 0.0
    res["duration"] = time.time() - t0
    res["control_tx_bytes"], res["control_rx_bytes"] = CTRL["tx"], CTRL["rx"]
    c.close()
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("server")
    s.add_argument("--bind", default="0.0.0.0")
    s.add_argument("--port", type=int, default=5300)
    c = sub.add_parser("client")
    c.add_argument("host")
    c.add_argument("--port", type=int, default=5300)
    c.add_argument("--mode", choices=["tcp-up", "tcp-down", "udp-up", "udp-down"], default="tcp-up")
    c.add_argument("--bytes", default="20M", help="TCP transfer size (e.g. 50M)")
    c.add_argument("--rate", default="10M", help="UDP rate in bit/s (e.g. 20M)")
    c.add_argument("--seconds", type=float, default=5.0, help="UDP duration")
    c.add_argument("--size", type=int, default=1200, help="UDP payload size")
    c.add_argument("--sendfile", action="store_true", help="tcp-up via sendfile() instead of send()")
    a = ap.parse_args()
    if a.cmd == "server":
        serve(a)
    else:
        print(json.dumps(client(a)), flush=True)


if __name__ == "__main__":
    main()
