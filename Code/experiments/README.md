# Experiments: accuracy of real-time tracking under packet loss

## Question

How close are the eBPF tracker's numbers to the truth, at each layer, when the
network loses packets? And what does packet loss look like from inside the
kernel?

## Method

```
  Pi (bwtrack + run_experiments.py)                         laptop
  ┌──────────────────────────────────────┐   real link     ┌──────────────────────┐
  │ trafficgen client ──► kernel ──► netem ├──────────────►│ trafficgen server     │
  │ (knows bytes sent)   (eBPF probes) (tc)│◄──────────────┤ (knows bytes received,│
  └──────────────────────────────────────┘                 │  UDP sequence numbers)│
                                                           └──────────────────────┘
```

For each loss level `p` in {0, 1, 2, 5, 10} %:
1. `tc qdisc ... netem loss p%` on the Pi's interface (egress and, through an IFB
   device, ingress).
2. Four transfers: TCP upload / download of a fixed size, UDP upload / download
   at a fixed rate with sequence-numbered datagrams. Optional: TCP upload via
   `sendfile()`.
3. Ground truth = what the two applications counted (and the UDP sequence numbers).
4. Measured = what the eBPF tracker attributed to the client's PID (socket layer
   and TCP wire layer), plus the interface counters.

Run (see `docs/RASPBERRY_PI.md` for the laptop side):
```bash
sudo python3 experiments/run_experiments.py --peer <laptop-ip> --loss 0,1,2,5,10 --delay 10
sudo python3 experiments/run_experiments.py --peer local --loss 0,1,5,10 --rcvbuf-test   # no laptop
```

## Metrics

| metric | definition |
|---|---|
| app accuracy % | eBPF socket-layer bytes / bytes the app counted × 100 |
| wire overhead % | (TCP bytes put on the wire − app bytes) / app bytes |
| retx % of wire | retransmitted bytes / wire bytes (an estimate of the loss rate seen by TCP) |
| per-sample app vs wire deviation | mean and max, over all 0.5 s samples, of the absolute difference between socket-layer bytes and wire bytes: the **real-time** error of measuring at one layer |
| wire lag | how long the wire keeps sending after the app's last `send()` returned (send buffer draining) |
| UDP true loss | 1 − received / sent datagrams (sequence numbers) |
| UDP eBPF-measured loss | 1 − eBPF rx bytes at receiver / eBPF tx bytes at sender (both ends traced) |
| Mathis bound | MSS / RTT × 1.22 / √p, the classic TCP throughput model, for comparison |

## Results

All reported results come from the Raspberry Pi 5 testbed, in `pi_results/`:

- `pi_results/2026-10-30_final/`: the final runs used in the report and slides
  (proof suite, three loss runs, the +20 ms delay run, overhead check).
- `pi_results/2026-10-26_pi5_wifi_loss/`: the first Pi run (one pass), kept for reference.

Each experiment run writes the same five files: `report.md` / `report.html`
(tables and charts), `results.json` / `results.csv` (one row per transfer) and
`series.csv` (every 0.5 s sample).
---

# Proof suite: correctness, performance, scalability, reliability

`proof_suite.py` checks each claim we make about the tracker. Every test
starts a workload process (`proof/worker.py`) that prints how many bytes it
really sent or received, its PID and the addresses it used. The test then
compares that number with what the tracker reported. Nothing is copied
from documentation; each number in our slides comes from one of these files.

```bash
sudo python3 experiments/proof_suite.py            # full (~45 min incl. 30 min soak)
sudo python3 experiments/proof_suite.py --quick    # ~10 min
sudo ./experiments/run_final.sh <laptop-ip>        # proof suite + 3x loss experiment + 20 ms delay run
```

| group | test | what it proves |
|---|---|---|
| correctness | `partial_sends` | non-blocking `send()` that accepts fewer bytes than asked: we count the accepted bytes (kretprobe return value), not the requested size |
| | `recv_waitall` | `MSG_WAITALL` reads (several `tcp_cleanup_rbuf` calls per read) and the FIN at the end of the stream are not counted twice / as data |
| | `sendfile` | zero-copy `sendfile()` is counted |
| | `udp_unconnected_multi_dest` | one unconnected UDP socket `sendto()` 3 IPs: bytes split per destination, nothing attributed to 0.0.0.0 |
| | `ipv6` | TCP and UDP over IPv6, and IPv4-mapped UDP through `udpv6_sendmsg -> udp_sendmsg` counted once |
| | `threads_one_pid` / `fork_separate_pids` | threads add up under one PID; forked children get their own PID |
| | `short_connections_reuse` | hundreds of short connections (kernel reuses socket memory): app and wire bytes still exact |
| | `loopback_detection` | 127.x, ::1 and the host's own LAN IP are marked as local traffic |
| performance | `throughput_overhead` | iperf3 TCP and 64-byte UDP throughput with vs without the tracker (3 runs each), tool CPU and memory, accuracy at full speed |
| | `ebpf_cost_all_tests` | nanoseconds per eBPF program run (kernel `bpf_stats`), BPF map memory, kretprobe misses, perf buffer losses |
| scalability | `concurrent_connections` | 1-200 simultaneous connections to different IPs, each one exact; collector CPU and map read time |
| | `concurrent_processes` | 10-50 processes at once |
| | `event_rate` | thousands of tiny `send()` calls per second, per-probe cost |
| reliability | `map_overflow_lru` | more flows than the map holds: LRU eviction, bounded memory, no crash (and 100 % with the default size) |
| | `start_mid_transfer` | starting the tracker in the middle of a transfer gives no false spike |
| | `soak` | the real `bwtrack record` process for 30 min with short-lived processes: CPU, memory over time, SQLite history equal to ground truth |

Output: `experiments/results/proof_<time>/summary.md` plus one JSON per test.
