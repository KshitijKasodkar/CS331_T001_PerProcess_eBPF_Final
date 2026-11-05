# Concepts

Background for the design: eBPF, kprobes and tracepoints, the kernel data path
through `tcp_sendmsg`/`udp_sendmsg`, loopback, and why real-time numbers at
different layers never agree exactly. The system design is in `ARCHITECTURE.md`.

---

## 1. What is eBPF, in one paragraph

eBPF lets us load a small, verified program into the running Linux kernel and
run it when something happens there (a function is called, a tracepoint fires,
a packet arrives). The program cannot crash the kernel: the **verifier** checks
every path before it is accepted (bounded loops, no invalid memory access,
limited stack of 512 bytes). It is then **JIT-compiled** to native ARM64 / x86
instructions. The program stores results in **BPF maps** (kernel hash tables)
that our Python program reads from user space.

We use **BCC**: our C file `bwtracker/bpf/tracker.bpf.c` is compiled by
clang **on the target machine at start-up**, against that machine's kernel
headers. That is why the Pi needs `linux-headers-rpi-2712` and why WSL2 needed
`scripts/setup_wsl.sh` (Microsoft ships no kernel headers for WSL2, so BCC cannot compile without it).

---

## 2. kprobes, kretprobes and tracepoints

### kprobe
A **kprobe** is a breakpoint that the kernel places at the first instruction of
any kernel function. When the CPU reaches it, the kernel runs our eBPF program
first, with a copy of the CPU registers (`struct pt_regs`), then continues
the function normally.

Function arguments are passed in registers by the calling convention:

| argument | x86-64 | ARM64 (Raspberry Pi 5) | macro we use |
|---|---|---|---|
| 1st | `rdi` | `x0` | `PT_REGS_PARM1(ctx)` |
| 2nd | `rsi` | `x1` | `PT_REGS_PARM2(ctx)` |
| 3rd | `rdx` | `x2` | `PT_REGS_PARM3(ctx)` |
| return value | `rax` | `x0` | `PT_REGS_RC(ctx)` |

So in `on_tcp_send_enter()` the line `PT_REGS_PARM1(ctx)` is "the `struct sock *sk`
argument of `tcp_sendmsg`". BCC picks the right register for the CPU it runs on,
so the same C file works on the Pi (ARM64) and on WSL2 (x86-64).

### kretprobe
A **kretprobe** runs when the function **returns**. It sees only the return
value, not the arguments (the argument registers were overwritten by then).
Therefore we always use a **pair**:

```
kprobe    tcp_sendmsg  -> save sk in map tcp_send_args[thread id]
kretprobe tcp_sendmsg  -> ret = PT_REGS_RC(ctx); look up sk; count `ret` bytes
```

Why not just count the `size` argument at entry? Because `size` is what the app
*asked* to send. A non-blocking socket with a full buffer may accept fewer
bytes, or fail with `-EAGAIN`. The **return value** is what really entered the
kernel. The old project counted `size` - that is one reason it could not be
accurate.

Caveat we measure and display: a kretprobe has a limited number of
simultaneous instances (`maxactive`, we set 512). If more threads are inside
`tcp_sendmsg` at the same moment (blocked on a full buffer), the kernel skips
the return probe and increments a **miss counter**. bwtrack shows it
("kretprobe misses") as evidence that the counts are complete.

### tracepoint
A **tracepoint** is a hook the kernel developers placed on purpose, with a
stable, documented set of fields (`/sys/kernel/tracing/events/tcp/tcp_retransmit_skb/format`).
Tracepoints are preferred when one exists because they do not change between
kernel versions. kprobes can attach to (almost) any function but the function
may be renamed or inlined in a future kernel.

We use two tracepoints:
- `tcp:tcp_retransmit_skb` - fires for **every retransmitted TCP segment**.
- `sock:inet_sock_set_state` - fires on every TCP state change (SYN_SENT,
  ESTABLISHED, FIN_WAIT, CLOSE...). We use it to take a final snapshot when a
  connection closes and to reset state when a new connection starts.

### Process context vs softirq context (important!)
- `tcp_sendmsg`, `tcp_cleanup_rbuf`, `udp_sendmsg`, `skb_consume_udp` are called
  **from the application's own system call**. "Current process" = the app.
  `bpf_get_current_pid_tgid()` is correct here. **This is the only place where
  per-process attribution is reliable.**
- `tcp_rcv_established`, `tcp_retransmit_skb`, `__udp_enqueue_schedule_skb` run
  when a **packet arrives** (softirq) or a **timer fires**. "Current process"
  is whatever was running on that CPU - often the wrong one. So there we look
  the socket up in our map `sk_owner` instead of trusting the current PID.

This is why tools that count at the network device (XDP/TC) **cannot** know the
process: at that layer the packet is not yet associated with a socket.

---

## 3. The path of the data: `tcp_sendmsg`, `udp_sendmsg` and friends

```
 Application         write()/send()/sendto()                 read()/recv()/recvfrom()
 ------------------------------|-----------------------------------------^------------------
 syscall layer           sock_sendmsg()                              sock_recvmsg()
                               |                                         |
 socket layer   TCP: tcp_sendmsg()  <-- kprobe+kretprobe       tcp_recvmsg() -> tcp_cleanup_rbuf() <-- kprobe
                UDP: udp_sendmsg()  <-- kprobe+kretprobe       udp_recvmsg() -> skb_consume_udp()  <-- kprobe
                     udpv6_sendmsg()<-- kprobe+kretprobe       udpv6_recvmsg()-> skb_consume_udp()
                               |                                         ^
 TCP engine     segment, cwnd, retransmit timers       receive queue, ACK generation
                tcp_write_xmit/__tcp_transmit_skb      tcp_rcv_established() <-- kprobe (stats refresh)
                tcp_retransmit_skb  <-- tracepoint     __udp_enqueue_schedule_skb() <-- kretprobe (drops)
                               |                                         ^
 IP layer       ip_queue_xmit / ip6_xmit               ip_rcv / ip6_rcv
                               |                                         ^
 qdisc          (tc netem lives here - it drops packets in our experiments)
                               |                                         ^
 driver / NIC   eth0 / wlan0     -> /proc/net/dev tx_bytes   rx_bytes <- /proc/net/dev
```

### `tcp_sendmsg(struct sock *sk, struct msghdr *msg, size_t size)`
Called when an app writes to a TCP socket. It **copies the data into the
socket's send buffer**, immediately transmits as much as the congestion and
receive windows allow, and returns the number of bytes accepted. It does
**not** wait for the data to be delivered: the rest is transmitted (and
retransmitted if lost) later by the TCP engine as ACKs arrive. So our "app
upload" counter measures *bytes the application handed to TCP*.

### `tcp_cleanup_rbuf(struct sock *sk, int copied)`
Called at the end of `tcp_recvmsg()` after `copied` bytes were copied to the
app (it decides whether a window-update ACK should be sent). It can be called
**twice in one `recv()`** with a cumulative `copied` (when the app uses
`MSG_WAITALL`/`SO_RCVLOWAT`). Counting `copied` naively double-counts. We use
`tp->copied_seq` - the TCP sequence number up to which the app has consumed
data - and count only how much it advanced. The peer's FIN also advances
`copied_seq` by one; that is not counted as data.

### `udp_sendmsg(struct sock *sk, struct msghdr *msg, size_t len)`
UDP has no connection: with `sendto()` the destination is in `msg->msg_name`
and the socket's own remote address is `0.0.0.0`. So at entry we read the
`sockaddr_in`/`sockaddr_in6` from the msghdr to know the **remote IP per
datagram**. That is how one DNS socket talking to 8.8.8.8 and 1.1.1.1 shows two
remote IPs. Special case: `udpv6_sendmsg()` calls `udp_sendmsg()` itself for
IPv4-mapped addresses (`::ffff:a.b.c.d`). We detect this nesting so the bytes
are counted once.

### `skb_consume_udp(struct sock *sk, struct sk_buff *skb, int len)`
Called by both `udp_recvmsg` and `udpv6_recvmsg` after `len` bytes were given
to the app. The sender's address is read from the **IP header inside the skb**
(`skb->head + skb->network_header`) and the source port from the UDP header.

### Kernel's own counters: `struct tcp_sock`
Every TCP socket already keeps RFC 4898 statistics. We copy them into a map
(`tcp_snaps`) at most every 100 ms per socket:

| field | meaning |
|---|---|
| `bytes_sent` | payload put on the wire **including retransmissions** |
| `bytes_retrans` | payload retransmitted |
| `bytes_acked` | payload the peer confirmed (true delivered bytes) |
| `bytes_received` | payload received from the peer |
| `total_retrans` | retransmitted segments |
| `srtt_us >> 3` | smoothed RTT |
| `snd_cwnd`, `mss_cache`, `lost_out` | congestion window, segment size, segments believed lost |

---

## 4. Loopback - what it is and why it matters

**Loopback** (`lo`, `127.0.0.1`, `::1`) is a virtual network interface inside the
kernel. A packet sent to `127.0.0.1` - **or to any of the machine's own
addresses, e.g. `192.168.1.42` when that is the Pi's own IP** - never reaches a
network card. The kernel hands it straight back to its own receive path.

Consequences for a bandwidth tracker:

1. **Double counting.** Both ends are local processes, so the same bytes appear
   once as upload (sender) and once as download (receiver). Adding all
   processes gives twice the real amount, and none of it used the network.
2. **No loss, huge MTU, near-zero RTT.** The loopback MTU is 65536, nothing is
   dropped (unless we add netem), so TCP never retransmits. **Any accuracy test
   over loopback will show 100 %.** That was the weakness of our mid-semester
   "100 % accuracy" benchmark.
4. **The NIC counters don't move.** `/proc/net/dev` for `eth0` stays flat while
   loopback traffic flows.

What bwtrack does: flows whose remote address is loopback **or one of the
host's own addresses** are marked `loopback`, **hidden by default** and
excluded from totals (press `l` in the terminal UI, or tick "Loopback" in the
web UI to see them). Loopback is still useful as a controlled lab:
`run_experiments.py --peer local` runs everything over `lo` with netem loss -
handy without a second computer, but all our reported measurements use a
real link (Pi <-> laptop).

---

## 5. Why real-time numbers are NOT 100 % accurate (and must not be)

A real-time tracker looks at the stack at one layer and
at one instant; different layers disagree **for real reasons**:

| effect | what you see in bwtrack |
|---|---|
| **Buffering**: `tcp_sendmsg` returns when data is in the send buffer (up to several MB with autotuning). The wire is slower. | "UP app/s" jumps ahead of "UP wire/s"; after the app finishes, the wire keeps sending for a while (`wire_lag_s` in experiments). |
| **Retransmissions**: lost segments are sent again. | "UP wire" > "UP app"; `RETX%` = retransmitted / wire bytes; red events. |
| **Headers**: each TCP segment adds 20 (IP) + 20-32 (TCP) + 14 (Ethernet) bytes; ACKs are pure headers. | NIC counters > socket counters by ~3-5 %. |
| **In flight**: sent but not yet acknowledged bytes. | "ACKED/s" lags "UP wire/s". |
| **UDP loss is invisible to the sender**: no ACKs, no retransmissions. | The sender's eBPF counters say 100 % sent; only the receiver (or sequence numbers) reveals loss. |
| **Host drops**: receive buffer full. | "UDP drop" column, `UDP_DROP` events. |
| **Sampling interval**: a 1-second bin cuts transfers at arbitrary points. | Per-interval app-vs-wire deviation (`interval_rate_dev_*` in the experiments). |
| **Probe misses** under extreme concurrency. | `kretprobe misses` in the footer (should stay 0). |
| **Not visible at all**: ICMP (ping), raw sockets, kernel-space traffic (NFS, WireGuard), and on WSL2 every Windows program. | They never call `tcp_sendmsg`/`udp_sendmsg`. Compare with NIC counters to see the gap. |

So bwtrack reports **three layers side by side** instead of one "accurate"
number: socket layer (what each process did), TCP wire layer (what TCP really
transmitted, incl. retransmissions), and NIC (everything incl. headers). The
experiments quantify the difference against ground truth.

**Measured on the Raspberry Pi 5** (`experiments/pi_results/2026-10-30_final/`):
- TCP app-layer bytes are exact over a whole transfer (100.0 %): TCP is reliable
  and we count at the app boundary. Per-second they differ from the wire.
- TCP wire bytes exceed app bytes by roughly the loss rate: `retx % ~ loss %`.
- UDP sender-side counters stay at 100 % while delivered drops to 1 - loss.
- TCP goodput collapses with loss roughly with the Mathis model:
  `throughput <= MSS / RTT * 1.22 / sqrt(p)`.
