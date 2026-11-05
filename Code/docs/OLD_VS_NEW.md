# What was wrong in the first version, and what changed

| # | first version | problem | this version |
|---|---|---|---|
| 1 | Default path on WSL2 failed with `kheaders not found`; only `--mock` ran | BCC needs headers of the running kernel and WSL2 ships none | `scripts/setup_wsl.sh` builds them from Microsoft's kernel source; `scripts/setup_pi.sh` installs them on the Pi; `doctor` explains any missing piece |
| 2 | Mock mode generated random numbers for `steam`, `discord`, `slack`... | Not measurement. On WSL2 those are **Windows** programs, which Linux eBPF cannot see at all | No mock mode. Every number comes from kernel probes |
| 3 | Re-declared `struct sock_common` / `struct sock` by hand | Breaks as soon as the kernel layout differs; hides the real kernel structures | Uses the real kernel headers (`<net/sock.h>`, `<linux/tcp.h>`) |
| 4 | Counted the `size` argument of `tcp_sendmsg` / `udp_sendmsg` | `size` = requested, not sent. Partial writes and `EAGAIN` were overcounted | kprobe + **kretprobe**: counts the return value |
| 5 | `tcp_cleanup_rbuf(copied)` counted directly | double counts with `MSG_WAITALL`/`SO_RCVLOWAT` | counts the advance of `tp->copied_seq` |
| 6 | No UDP receive hook | UDP download was always 0 | `skb_consume_udp` (IPv4 + IPv6), sender address from the IP header |
| 7 | UDP remote address taken from the socket | unconnected `sendto()` sockets have no remote address: all DNS traffic went to `0.0.0.0` | destination read from `msg->msg_name` per datagram |
| 8 | IPv4 only | IPv6 traffic attributed to garbage addresses | IPv4, IPv6 and v4-mapped |
| 9 | One perf event per `send()` call, Python callback per event | at high throughput the buffer overflows and lost events = lost bytes; heavy CPU | counters aggregated **in the kernel** (maps), read once per interval; only rare events (retransmissions, drops) use the perf buffer |
| 10 | No notion of retransmission / loss | "accuracy 100 %" was claimed from a **loopback** test, where nothing is ever lost | per-socket wire bytes, retransmitted bytes/segments, RTT, cwnd from `struct tcp_sock`; `tcp_retransmit_skb` tracepoint; UDP drop detection; netem experiments against ground truth |
| 11 | Loopback mixed with network traffic | double counting, inflated totals | loopback and host-local flows detected, hidden by default, explained in CONCEPTS.md |
| 12 | 25 tests mostly against the mock | tested the simulation, not the tracker | unit tests for the aggregation logic + experiments on real kernels (WSL2 kernel 5.15 and Raspberry Pi kernel 6.18). |
| 13 | FastAPI / extra pip packages | Raspberry Pi OS blocks system-wide pip (PEP 668), and a venv cannot see `python3-bpfcc` | standard library only |
