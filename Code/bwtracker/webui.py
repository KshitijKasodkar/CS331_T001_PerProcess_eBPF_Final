"""Web dashboard + JSON API + Prometheus /metrics, using only the standard library.

  GET /                 dashboard (bwtracker/web/index.html)
  GET /api/live         latest snapshot (filtered: ?proto=TCP&loopback=1&pid=&comm=&ip=)
  GET /api/series       last N seconds of totals for the live chart
  GET /api/history      ?since=1h&by=process|remote|proto|port&proto=&loopback=&top=
  GET /api/timeline     ?since=1h&bucket=60&pid=&comm=
  GET /api/events       ?since=1h  (retransmissions / drops from SQLite)
  GET /api/info         host, kernel, attached probes
  GET /metrics          Prometheus text format
"""
from __future__ import annotations

import json
import os
import platform
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import netutil, storage
from .model import filter_snapshot

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def serve(hub, prom, recorder, bind: str, port: int, args) -> ThreadingHTTPServer:
    info = {
        "host": platform.node(), "kernel": platform.release(), "arch": platform.machine(),
        "platform": netutil.detect_platform(), "interval": args.interval,
        "probes": [f"{k} {fn}" for k, fn in hub.tracker.kernel.attached],
        "skipped": [f"{k} {fn}: {why}" for k, fn, why in hub.tracker.kernel.skipped],
        "history": recorder is not None, "started": time.time(),
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = "bwtrack"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, separators=(",", ":")))

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(u.query).items()}
            try:
                if u.path in ("/", "/index.html"):
                    with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                if u.path == "/metrics":
                    return self._send(200, prom.render(), "text/plain; version=0.0.4")
                if u.path == "/api/info":
                    return self._json(info)
                if u.path == "/api/live":
                    return self._live(q)
                if u.path == "/api/series":
                    return self._json({"series": list(hub.history), "procs": list(hub.proc_history)})
                if u.path in ("/api/history", "/api/timeline", "/api/events"):
                    return self._history(u.path, q)
                return self._json({"error": "not found"}, 404)
            except BrokenPipeError:
                pass
            except Exception as e:  # keep the server alive, report the problem
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        def _live(self, q):
            snap = hub.latest
            if snap is None:
                return self._json({"waiting": True, "error": hub.error})
            pid = int(q["pid"]) if q.get("pid") else None
            view = filter_snapshot(snap, q.get("proto", "ALL"), q.get("loopback") == "1", pid,
                                   q.get("comm") or None, q.get("ip") or None,
                                   None if q.get("idle") == "1" else 5.0)
            d = view.to_dict()
            d["recent_events"] = [e.to_dict() for e in list(hub.events)[-40:]][::-1]
            d["error"] = hub.error
            return self._json(d)

        def _history(self, path, q):
            if recorder is None:
                return self._json({"error": "history disabled (started with --no-db)"}, 400)
            recorder.flush()
            since = storage.parse_since(q.get("since", "1h"))
            with recorder.lock:
                conn = recorder.conn
                if path == "/api/history":
                    rows = storage.query_usage(conn, q.get("by", "process"), since, None, q.get("proto"),
                                               q.get("loopback") == "1", None, q.get("comm") or None,
                                               q.get("raddr") or None, int(q.get("top", 25)))
                    return self._json({"rows": rows})
                if path == "/api/timeline":
                    pid = int(q["pid"]) if q.get("pid") else None
                    rows = storage.query_timeline(conn, since, int(q.get("bucket", 60)), None, q.get("proto"),
                                                  q.get("loopback") == "1", pid, q.get("comm") or None,
                                                  q.get("raddr") or None)
                    nic = storage.query_nic(conn, since, int(q.get("bucket", 60)))
                    return self._json({"rows": rows, "nic": nic})
                rows = storage.query_events(conn, since, q.get("kind"))
                return self._json({"rows": rows})

    httpd = ThreadingHTTPServer((bind, port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="web", daemon=True).start()
    return httpd
