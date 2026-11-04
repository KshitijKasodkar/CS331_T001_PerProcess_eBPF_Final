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

## Sample results (development VM, NOT the Pi)

`sample_results/` contains two runs from the development VMs (QEMU, emulated
CPU, traffic over loopback with netem): Ubuntu 24.04 / kernel 6.8 / BCC 0.29 and
Ubuntu 22.04 / kernel 5.15 / BCC 0.18. They show that the method works. **Replace
them with your Pi runs for the report.** The absolute speeds and the "eBPF
ns/run" values are meaningless there: the CPU is emulated, so every eBPF run
costs microseconds instead of the ~100-300 ns expected on a Pi 5.

What they show (and what you should expect on the Pi):
- **TCP app-layer counts are exact** over a full transfer (100.000 %), with and
  without loss, for `send()` and `sendfile()`.
- **Wire bytes grow with loss.** At 10 % loss TCP put about 12 % more bytes on the wire
  than the app sent; `retx %` follows the configured loss rate.
- **Per-second rates differ between layers** even with 0 % loss, and more
  with loss. This is the real-time inaccuracy the professor mentioned.
- **UDP sender-side counters always say 100 %.** Only the receiver (eBPF on
  the receiving host, or sequence numbers) sees the loss, and when both ends are
  traced, the eBPF-measured loss matches the sequence-number loss to 0.01 %.
- **Receive-buffer drops** counted by eBPF equal the kernel's own
  `Udp: RcvbufErrors` counter exactly.

On the Pi with a real link, expect in addition: NIC bytes > wire bytes
(headers, ACKs), a visible wire lag for uploads (Wi-Fi especially), and
retransmissions even at 0 % netem loss on Wi-Fi.

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
