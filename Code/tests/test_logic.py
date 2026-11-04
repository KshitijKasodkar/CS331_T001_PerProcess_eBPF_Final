"""Unit tests for the pure-Python parts (no root, no eBPF needed):
    python3 -m unittest discover -s tests -v
The kernel side is validated by experiments/run_experiments.py, which
compares eBPF numbers with ground truth from real traffic."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bwtracker import storage  # noqa: E402
from bwtracker.collector import Aggregator, acc_to_proc_rows, build_proc_rows  # noqa: E402
from bwtracker.model import RawFlow, RawTcp, Snapshot, filter_snapshot, summarize  # noqa: E402
from bwtracker.prometheus import PromExporter  # noqa: E402

S = 1_000_000_000


def flow(sk=1, tgid=100, proto=6, raddr="93.184.216.34", rport=443, tx=0, rx=0, last=0, comm="curl",
         laddr="192.168.1.5", lport=40000, closed_sent=0, closed_acked=0):
    return RawFlow(sk=sk, tgid=tgid, family=2, proto=proto, laddr=laddr, lport=lport, raddr=raddr,
                   rport=rport, tx_bytes=tx, rx_bytes=rx, tx_calls=1, rx_calls=1, first_ns=0,
                   last_ns=last, comm=comm, closed_sent=closed_sent, closed_acked=closed_acked)


def tcp(sk=1, sent=0, retrans=0, acked=0, recv=0, segs=0, born=1, upd=0):
    return RawTcp(sk=sk, bytes_sent=sent, bytes_retrans=retrans, bytes_acked=acked, bytes_received=recv,
                  total_retrans=segs, retrans_events=segs, srtt_us=20000, snd_cwnd=10, mss=1448,
                  lost_out=0, segs_out=0, segs_in=0, state=1, update_ns=upd, born_ns=born)


class AggregatorTest(unittest.TestCase):
    def test_rates_from_cumulative_counters(self):
        a = Aggregator()
        a.step(1 * S, 1.0, [flow(tx=1000, rx=5000, last=1 * S)], {}, {})
        rows, _, _ = a.step(2 * S, 1.0, [flow(tx=3000, rx=9000, last=2 * S)], {}, {})
        r = rows[0]
        self.assertEqual((r.d_tx, r.d_rx), (2000, 4000))
        self.assertAlmostEqual(r.tx_rate, 2000)
        self.assertEqual((r.tx_bytes, r.rx_bytes), (3000, 9000))

    def test_tcp_wire_stats_join_flow(self):
        a = Aggregator()
        a.step(1 * S, 1.0, [flow(tx=10_000, last=S)], {1: tcp(sent=10_000, acked=8_000, upd=S)}, {})
        rows, _, _ = a.step(2 * S, 1.0, [flow(tx=20_000, last=2 * S)],
                            {1: tcp(sent=21_000, retrans=1_000, acked=18_000, segs=1, upd=2 * S)}, {})
        r = rows[0]
        self.assertEqual(r.d_wire_tx, 11_000)
        self.assertEqual(r.d_retrans_bytes, 1_000)
        self.assertAlmostEqual(r.retrans_pct, 100 * 1000 / 11000)
        self.assertEqual(r.retrans_segs, 1)

    def test_socket_address_reuse_restarts_from_zero(self):
        a = Aggregator()
        a.step(1 * S, 1.0, [flow(tx=5_000_000, last=S)], {1: tcp(sent=5_000_000, born=111, upd=S)}, {})
        # same kernel address, new connection (new born_ns) that already sent more than the old one
        rows, _, _ = a.step(2 * S, 1.0, [flow(tx=5_000_000, last=S), flow(lport=40001, tx=7_000_000, last=2 * S)],
                            {1: tcp(sent=7_000_000, born=222, upd=2 * S)}, {})
        self.assertEqual(sum(r.d_wire_tx for r in rows), 7_000_000)

    def test_reused_address_wire_stats_go_to_newest_flow(self):
        a = Aggregator()
        old = flow(lport=1, tx=9_000_000, last=1 * S)
        a.step(1 * S, 1.0, [old], {1: tcp(sent=9_000_000, born=1, upd=S)}, {})
        new = flow(lport=2, tx=100_000, last=2 * S)        # smaller, but newer
        rows, _, _ = a.step(2 * S, 1.0, [old, new], {1: tcp(sent=100_000, born=2, upd=2 * S)}, {})
        st = [s for s in a.flows.values() if s.raw.lport == 2][0]
        self.assertEqual(st.d_wire_tx, 100_000)

    def test_connection_opened_and_closed_between_reads(self):
        a = Aggregator()
        a.step(1 * S, 1.0, [], {}, {})
        rows, _, _ = a.step(2 * S, 1.0, [flow(tx=20_000, last=2 * S, closed_sent=20_000, closed_acked=20_000)], {}, {})
        self.assertEqual(rows[0].d_wire_tx, 20_000)

    def test_closed_socket_not_double_counted(self):
        a = Aggregator()
        r1, _, _ = a.step(1 * S, 1.0, [flow(tx=10_000, last=S)], {1: tcp(sent=6_000, born=5, upd=S)}, {})
        # closed: the kernel moved the final 10 000 B into closed_sent and dropped the snapshot
        r2, _, _ = a.step(2 * S, 1.0, [flow(tx=10_000, last=S, closed_sent=10_000)], {}, {})
        self.assertEqual(r1[0].d_wire_tx + r2[0].d_wire_tx, 10_000)

    def test_counter_going_backwards_is_treated_as_reset(self):
        a = Aggregator()
        a.step(1 * S, 1.0, [flow(tx=5000, last=S)], {}, {})
        rows, _, _ = a.step(2 * S, 1.0, [flow(tx=300, last=2 * S)], {}, {})   # LRU evicted & recreated
        self.assertEqual(rows[0].d_tx, 300)

    def test_idle_flows_are_expired(self):
        a = Aggregator(idle_timeout=5)
        a.step(1 * S, 1.0, [flow(tx=100, last=1 * S)], {}, {})
        _, stale, socks = a.step(10 * S, 1.0, [flow(tx=100, last=1 * S)], {}, {})
        self.assertEqual(len(stale), 1)
        self.assertEqual(socks, {1})
        self.assertEqual(a.flows, {})

    def test_group_per_remote_ip_and_loopback(self):
        a = Aggregator()
        raw = [flow(sk=1, lport=1, tx=100, last=S), flow(sk=2, lport=2, tx=200, last=S),
               flow(sk=3, raddr="127.0.0.1", laddr="127.0.0.1", tx=50, last=S, comm="redis")]
        rows, _, _ = a.step(1 * S, 1.0, raw, {}, {})
        by = {r.raddr: r for r in rows}
        self.assertEqual(by["93.184.216.34"].sockets, 2)
        self.assertEqual(by["93.184.216.34"].tx_bytes, 300)
        self.assertTrue(by["127.0.0.1"].loopback)
        self.assertFalse(by["93.184.216.34"].loopback)
        snap = Snapshot(ts=time.time(), interval=1, flows=rows, procs=[], nic={}, events=[], health={})
        view = filter_snapshot(snap, include_loopback=False)
        self.assertEqual(view.totals["loopback_hidden"], 1)
        self.assertEqual({f.comm for f in view.flows}, {"curl"})

    def test_process_totals_survive_closed_sockets(self):
        a = Aggregator(idle_timeout=2)
        a.step(1 * S, 1.0, [flow(tx=1000, last=S)], {}, {})
        a.step(2 * S, 1.0, [flow(tx=4000, last=2 * S)], {}, {})
        a.step(9 * S, 1.0, [flow(tx=4000, last=2 * S)], {}, {})            # expired now
        rows, _, _ = a.step(10 * S, 1.0, [], {}, {})
        acc = acc_to_proc_rows(a.proc_acc, a.proc_comm)
        procs = build_proc_rows(rows, acc)
        self.assertEqual(procs[0].tx_bytes, 4000)

    def test_udp_drops_attributed(self):
        a = Aggregator()
        a.step(1 * S, 1.0, [flow(proto=17, rx=100, last=S)], {}, {1: 0})
        rows, _, _ = a.step(2 * S, 1.0, [flow(proto=17, rx=200, last=2 * S)], {}, {1: 7})
        self.assertEqual(rows[0].d_udp_drops, 7)
        self.assertEqual(summarize(rows)["d_udp_drops"], 7)


class StorageTest(unittest.TestCase):
    def test_record_and_query(self):
        a = Aggregator()
        with tempfile.TemporaryDirectory() as d:
            rec = storage.Recorder(os.path.join(d, "t.db"), bucket=10)
            t0 = int(time.time()) // 10 * 10
            a.step(1 * S, 1.0, [flow(tx=0, last=S)], {}, {})
            for i in range(1, 4):
                rows, _, _ = a.step((i + 1) * S, 1.0, [flow(tx=1000 * i, rx=500 * i, last=(i + 1) * S)], {}, {})
                rec(Snapshot(ts=t0 + i, interval=1.0, flows=rows, procs=[], nic={}, events=[], health={}))
            rec.flush()
            got = storage.query_usage(rec.conn, "name", since=0)
            self.assertEqual(got[0]["comm"], "curl")
            self.assertEqual(got[0]["tx"], 3000)
            self.assertEqual(got[0]["rx"], 1500)
            tl = storage.query_timeline(rec.conn, since=0, bucket=60)
            self.assertEqual(sum(r["tx"] for r in tl), 3000)
            rec.close()

    def test_parse_since(self):
        self.assertAlmostEqual(storage.parse_since("2h"), time.time() - 7200, delta=2)
        with self.assertRaises(ValueError):
            storage.parse_since("yesterday-ish")


class PrometheusTest(unittest.TestCase):
    def test_exposition_format(self):
        a = Aggregator()
        a.step(1 * S, 1.0, [flow(tx=0, last=S)], {}, {})
        rows, _, _ = a.step(2 * S, 1.0, [flow(tx=1234, last=2 * S, comm='we"ird')], {}, {})
        snap = Snapshot(ts=time.time(), interval=1, flows=rows, procs=build_proc_rows(rows), nic={},
                        events=[], health={"kprobe_misses": 0})
        snap.proc_acc, snap.proc_comm = a.proc_acc, a.proc_comm
        p = PromExporter()
        p(snap)
        text = p.render()
        self.assertIn('bwtracker_bytes_total{pid="100",comm="we\\"ird",proto="tcp",loopback="false",direction="tx"} 1234', text)
        for line in text.splitlines():
            self.assertTrue(line.startswith("#") or " " in line)


if __name__ == "__main__":
    unittest.main()
