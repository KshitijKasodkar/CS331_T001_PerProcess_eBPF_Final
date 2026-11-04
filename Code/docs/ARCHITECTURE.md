# System architecture

```
                         KERNEL (eBPF programs, compiled by BCC at start-up)
 ┌──────────────────────────────────────────────────────────────────────────────────────┐
 │  process context (the app's own syscall)          softirq / timer context            │
 │                                                                                      │
 │  kprobe+kretprobe tcp_sendmsg ──┐                 kprobe tcp_rcv_established ──┐      │
 │  kprobe tcp_cleanup_rbuf ───────┤                 tracepoint tcp_retransmit_skb ┤     │
 │  kprobe+kretprobe udp_sendmsg ──┤                 kretprobe __udp_enqueue_... ──┤     │
 │  kprobe+kretprobe udpv6_sendmsg ┤                 tracepoint inet_sock_set_state┤     │
 │  kprobe skb_consume_udp ────────┤                                              │     │
 │                                 ▼                                              ▼     │
 │   BPF maps:  flows[(sk,pid,proto,laddr,lport,raddr,rport)] -> tx/rx bytes, comm       │
 │              tcp_snaps[sk] -> bytes_sent/retrans/acked/received, RTT, cwnd (100 ms)   │
 │              sk_owner[sk] -> pid, comm      udp_drops[sk] -> count                    │
 │              rx_seq[sk] -> last copied_seq  tcp/udp_send_args[thread] -> entry args   │
 │   perf buffer "events": TCP retransmissions, UDP drops (rare -> cheap)                │
 └───────────────────────────────┬──────────────────────────────────────────────────────┘
                                 │ read maps once per interval (bpf syscall)
 USER SPACE (Python 3, standard library only + python3-bpfcc)
 ┌───────────────────────────────▼──────────────────────────────────────────────────────┐
 │ collector.KernelProbes   load/attach, read maps, poll perf buffer, delete idle keys   │
 │ collector.Aggregator     cumulative -> per-interval deltas -> rates; join TCP wire     │
 │                          stats to flows; group per process and per remote IP;          │
 │                          keep per-process totals after sockets close                  │
 │ collector.Tracker        + NIC counters (/proc/net/dev) + health (misses, CPU)        │
 │                 │                                                                     │
 │      runner.Hub (thread) ── one Snapshot per interval ──┬──────────┬──────────┬──────┐ │
 │                                                         ▼          ▼          ▼      ▼ │
 │                        tui.LiveUI (curses)   webui (HTTP + JSON)  storage   prometheus │
 │                        views (stream mode)   web/index.html       SQLite    /metrics   │
 │                                                                     │                  │
 │                                                     report.py ◄─────┘ (history CLI)    │
 └──────────────────────────────────────────────────────────────────────────────────────┘
```

## Data flow, step by step

1. **An app calls `send()`** on a TCP socket. The kernel enters `tcp_sendmsg()`.
   Our kprobe saves the `struct sock *` in `tcp_send_args` keyed by the thread.
2. **`tcp_sendmsg()` returns** N bytes. Our kretprobe looks the socket up, reads
   ports and addresses from `sk->__sk_common`, and adds N to
   `flows[key].tx_bytes` with an atomic add. It also refreshes `tcp_snaps[sk]`
   (rate-limited to every 100 ms) with the kernel's own counters from
   `struct tcp_sock`.
3. **ACKs arrive** in softirq. `tcp_rcv_established` refreshes `tcp_snaps[sk]` so
   wire/acked counters keep moving while the app is blocked.
4. **A segment is lost** and resent. The `tcp_retransmit_skb` tracepoint increments
   a counter and pushes an event (process, ports, size) into the perf buffer.
5. **Every interval** (1 s by default) user space reads `flows`, `tcp_snaps`
   and `udp_drops`. The counters are **cumulative**, so the Aggregator subtracts
   the previous reading to get bytes in this interval, then divides by the
   interval to get the rate. Nothing is lost if a read is late. With per-packet
   events (the old design) a slow reader loses data.
6. The Snapshot goes to every consumer. The terminal UI redraws, the web UI
   serves it as JSON, the recorder adds it to the current 10-second bucket in
   SQLite, and the Prometheus exporter updates its counters.
7. Sockets idle for 30 s are deleted from the kernel maps. Per-process totals
   stay in user space (`proc_acc`), so totals never go backwards.

## TCP connection lifecycle (added after the proof suite)

The proof suite showed that a connection which opens, sends and closes between
two reads of user space left **no wire statistics**, and that the first chunk of
every connection was missing from the wire count. The fix:

1. `sock:inet_sock_set_state` to `SYN_SENT` / `SYN_RECV`: create the socket's
   `tcp_snaps` entry with all counters and baselines at **0**. A new connection
   has sent nothing yet, so even its first bytes are counted.
2. On the move to `TCP_CLOSE`: take a final snapshot, add (final − baseline) to
   the `closed_*` fields of the flow that last used the socket (key stored in
   `sk_owner`), then delete the snapshot and owner entries.
3. User space counts, per flow, *closed totals + live snapshot*. When a socket
   it had already counted live is retired, it subtracts what it had counted.
4. A tracepoint program is skipped if it fires while another eBPF program is
   running on the same CPU. If a close is missed, the next connection that
   reuses the socket address retires the old snapshot first.

Also fixed: the peer's FIN advances `copied_seq` by 1. When the stream end is
consumed (`copied_seq == rcv_nxt` in CLOSE_WAIT/LAST_ACK/CLOSING/TIME_WAIT), that
1 is not counted as data.

## Design decisions (and the alternatives we rejected)

| decision | why | alternative and why not |
|---|---|---|
| Hook the **socket layer** (`tcp_sendmsg` etc.) | It is the only layer where the current process is the owner of the data, so per-process attribution is exact. | XDP / TC / `net_dev_xmit`: see every packet but **no process**. They would need a 5-tuple-to-PID join and miss short-lived sockets. |
| **kretprobe** return value instead of the `size` argument | Counts bytes really accepted, not requested. | Counting `size` overcounts on non-blocking or interrupted sends. |
| `tp->copied_seq` delta in `tcp_cleanup_rbuf` | Correct with `MSG_WAITALL`/`SO_RCVLOWAT` and `MSG_PEEK`. | Counting `copied` double-counts (the bug in `tcptop`). |
| Read the kernel's **RFC 4898 counters** (`bytes_sent`, `bytes_retrans`, `bytes_acked`) | Exact wire/retransmission numbers per socket, maintained by TCP itself. | Counting segments at `__tcp_transmit_skb` is fragile (static function, GSO super-packets). |
| **In-kernel aggregation in maps** + periodic reads | Constant overhead no matter how many packets; no data lost if user space is slow. | One perf event per send call (old design): heavy at high throughput, and lost events become lost bytes. |
| **LRU hash** maps | Bounded memory; a leak from never-closed sockets cannot fill the map. | Plain hash maps can fill up and then reject new flows. |
| **BCC** (runtime compile) | Raspberry Pi OS kernels ship **without BTF**, so CO-RE (compile once, run anywhere) binaries cannot load. BCC compiles against the headers that match the running kernel. | libbpf + CO-RE would need BTF on the Pi, which means building a custom kernel. |
| Python **standard library only** (curses, http.server, sqlite3) | Works on Raspberry Pi OS without pip / venv (PEP 668 "externally managed" Python). | Flask/FastAPI/rich would need a venv that can still import the distro's `bcc` module. |
| Store **10 s buckets** per (process, proto, remote IP) | Small database on an SD card, still minute-level reports. | Per-second-per-flow rows: millions per day. |
| Loopback hidden by default | Loopback is not network traffic, and it double counts (see CONCEPTS.md). | |

## Files

| file | role |
|---|---|
| `bwtracker/bpf/tracker.bpf.c` | all eBPF programs and maps (kernel side) |
| `bwtracker/collector.py` | loading, attaching, reading maps, aggregation |
| `bwtracker/model.py` | data classes, filtering |
| `bwtracker/runner.py` | background collector thread, history ring buffers |
| `bwtracker/tui.py`, `views.py` | terminal UI and plain `stream` output |
| `bwtracker/webui.py`, `web/index.html` | web dashboard + JSON API + `/metrics` |
| `bwtracker/storage.py`, `report.py` | SQLite history and the `report` command |
| `bwtracker/prometheus.py` | Prometheus exposition |
| `bwtracker/doctor.py` | environment checks with exact fixes |
| `tools/trafficgen.py` | ground-truth traffic generator (TCP + UDP with sequence numbers) |
| `experiments/run_experiments.py` | netem loss experiments, report with charts |
| `tests/test_logic.py` | unit tests of aggregation, storage, Prometheus |
