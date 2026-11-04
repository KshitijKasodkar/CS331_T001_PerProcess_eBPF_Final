# bwtrack proof suite results

**14 passed, 1 failed** (plus informational measurements). Raw data: one JSON file per test in this folder.
Host `kasodkar`, Raspberry Pi 5 Model B Rev 1.1, kernel `6.18.50+rpt-rpi-2712` aarch64, CPU Raspberry Pi 5 Model B Rev 1.1 x4, 7.9 GB RAM, Python 3.13.5, BCC 0.31.0. 2026-10-30 23:15:42.

## Correctness

| test | result | summary |
|---|---|---|
| `partial_sends` | PASS | tracker 15535328 B = accepted 15535328 B; counting the requested size (old method) would report 32374784 B (+108 %) |
| `recv_waitall` | PASS | receiver with MSG_WAITALL: tracker 20971520 B, app 20971520 B |
| `sendfile` | PASS | sendfile(): tracker 20971520 B, app 20971520 B |
| `udp_unconnected_multi_dest` | PASS | one sendto() socket -> 3 IPs: 127.0.0.2 300000/300000, 127.0.0.3 600000/600000, 127.0.0.4 900000/900000 |
| `ipv6` | PASS | TCP ::1 10485760/10485760, UDP ::1 500000/500000, UDP v4-mapped 500000/500000 (not double counted) |
| `threads_one_pid` | PASS | 8 threads -> one PID: 41943040/41943040 B, 8 sockets |
| `fork_separate_pids` | PASS | parent + 4 forked children: 4/4 children exact, parent 1048576/1048576 |
| `short_connections_reuse` | PASS | 500 short connections: app 10000000/10000000 B, wire 10000000 B (100.00 %) |
| `loopback_detection` | PASS | 127.x flagged: True; own IP 10.58.105.107 flagged: True |

## Performance

| test | result | summary |
|---|---|---|
| `throughput_overhead` | info | TCP loopback 54.652 -> 44.801 Gbit/s (-18.0 %); 64 B UDP 498.2 -> 248.9 kpps (-50.0 %); tool CPU 0.33 % (TCP load), 0.20 % idle; RSS 220.5 MB; accuracy at full speed 100.000 %, 100.000 %, 100.000 % |
| `ebpf_cost_all_tests` | info | 7,338,783 eBPF runs, avg 294 ns; maps 11.4 MB; kretprobe misses 0, perf lost 0 |

## Scalability

| test | result | summary |
|---|---|---|
| `concurrent_connections` | PASS | 1 conns: 1/1 exact; 10 conns: 10/10 exact; 50 conns: 50/50 exact; 100 conns: 100/100 exact; 200 conns: 200/200 exact |
| `concurrent_processes` | FAIL | 50 processes at once: 49/50 attributed exactly |
| `event_rate` | PASS | 170,388 send() calls/s of 64 B: tracker 87238848/87238848 B; send probes on_tcp_send_enter 143.1 ns, on_tcp_send_return 603.6 ns |

## Reliability

| test | result | summary |
|---|---|---|
| `map_overflow_lru` | PASS | 600 UDP flows: default map 100.0 % captured; forced 256-entry map: tool alive=True, 0.6 % captured (LRU evicts, memory stays bounded, no crash) |
| `start_mid_transfer` | PASS | tracker started 5 s into a 2 MB/s transfer: max reported 2.03 MB/s (no spike from earlier bytes) |
| `soak` | PASS | 30.0 min, 2511 short-lived processes: recorder alive=True, CPU 1.90 %, RSS 220.3 -> 223.3 MB, 2511/2511 processes exact in SQLite history |

## Throughput with vs without the tracker

| workload | without | with | change |
|---|---|---|---|
| TCP loopback (Gbit/s; without = 6 runs before+after, with = 3 runs) | 54.652 ± 21.819 | 44.801 ± 3.45 | -18.02 % |
| UDP 64 B packets (kpps) | 498.2 | 248.9 | -50.04 % |

## Scalability

| connections | exact flows | collector CPU % | sockets tracked | map read ms |
|---|---|---|---|---|
| 1 | 1/1 | 4.0 | 1045 | 8.69 |
| 10 | 10/10 | 6.81 | 1063 | 12.62 |
| 50 | 50/50 | 5.96 | 1157 | 10.18 |
| 100 | 100/100 | 7.22 | 1357 | 12.71 |
| 200 | 200/200 | 12.43 | 1755 | 23.07 |

## eBPF cost per program run (kernel.bpf_stats)

| program | runs | avg ns |
|---|---|---|
| `tracepoint__sock__inet_sock_set_state` | 12 | 1032.2 |
| `on_tcp_send_enter` | 1,363,445 | 143.1 |
| `on_tcp_send_return` | 1,363,445 | 603.6 |
| `on_tcp_cleanup_rbuf` | 1,816,141 | 432.5 |
| `on_tcp_rcv_established` | 2,720,311 | 97.9 |

BPF map memory (memlock): 11.4 MB total: flows 3585 KB, tcp_snaps 3201 KB, sk_owner 2433 KB, rx_seq 1281 KB, udp_drops 321 KB, tcp_send_args 417 KB, udp_send_args 417 KB

## Soak: recorder memory over time

| minute | RSS MB | CPU s |
|---|---|---|
| 0.0 | 220.3 | 0.0 |
| 0.51 | 221.2 | 0.25 |
| 1.01 | 221.3 | 0.72 |
| 1.51 | 221.3 | 1.2 |
| 2.01 | 221.3 | 1.7 |
| 2.52 | 221.4 | 2.21 |
| 3.02 | 221.4 | 2.73 |
| 3.52 | 221.4 | 3.25 |
| 4.02 | 221.4 | 3.8 |
| 4.52 | 221.4 | 4.33 |
| 5.03 | 221.4 | 4.9 |
| 5.53 | 221.4 | 5.47 |
| 6.03 | 221.4 | 6.03 |
| 6.53 | 221.5 | 6.61 |
| 7.03 | 222.9 | 7.19 |
| 7.54 | 222.9 | 7.77 |
| 8.04 | 222.9 | 8.36 |
| 8.54 | 222.9 | 8.93 |
| 9.04 | 222.9 | 9.54 |
| 9.54 | 222.9 | 10.12 |
| 10.05 | 222.9 | 10.69 |
| 10.55 | 222.9 | 11.29 |
| 11.05 | 222.9 | 11.88 |
| 11.55 | 222.9 | 12.44 |
| 12.05 | 222.9 | 13.04 |
| 12.56 | 222.9 | 13.63 |
| 13.06 | 222.9 | 14.2 |
| 13.56 | 222.9 | 14.79 |
| 14.06 | 222.9 | 15.38 |
| 14.56 | 222.9 | 15.96 |
| 15.06 | 222.9 | 16.55 |
| 15.57 | 222.9 | 17.14 |
| 16.07 | 222.9 | 17.74 |
| 16.57 | 223.0 | 18.33 |
| 17.07 | 223.0 | 18.91 |
| 17.58 | 223.0 | 19.49 |
| 18.08 | 223.0 | 20.08 |
| 18.58 | 223.0 | 20.67 |
| 19.08 | 223.0 | 21.24 |
| 19.58 | 223.0 | 21.83 |
| 20.09 | 223.0 | 22.42 |
| 20.59 | 223.0 | 22.99 |
| 21.09 | 223.0 | 23.59 |
| 21.59 | 223.0 | 24.17 |
| 22.09 | 223.0 | 24.76 |
| 22.6 | 223.0 | 25.34 |
| 23.1 | 223.0 | 25.94 |
| 23.6 | 223.0 | 26.5 |
| 24.1 | 223.0 | 27.11 |
| 24.6 | 223.0 | 27.69 |
| 25.11 | 223.0 | 28.27 |
| 25.61 | 223.0 | 28.86 |
| 26.11 | 223.0 | 29.44 |
| 26.61 | 223.0 | 30.05 |
| 27.11 | 223.0 | 30.65 |
| 27.62 | 223.0 | 31.26 |
| 28.12 | 223.0 | 31.84 |
| 28.62 | 223.3 | 32.44 |
| 29.12 | 223.3 | 33.08 |
| 29.62 | 223.3 | 33.68 |
