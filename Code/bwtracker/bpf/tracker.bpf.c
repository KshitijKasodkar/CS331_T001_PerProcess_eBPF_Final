// SPDX-License-Identifier: GPL-2.0
/*
 * tracker.bpf.c - the KERNEL half of the per-process bandwidth tracker.
 *
 * BCC compiles this file at runtime (clang -> eBPF bytecode) against the
 * running kernel's headers, so the struct layouts below always match the
 * kernel that is actually running (WSL2 5.15, Raspberry Pi 6.x, ...).
 *
 * WHERE WE HOOK (and why) -- see docs/CONCEPTS.md for the long version
 * ---------------------------------------------------------------------
 *  App writes ->  tcp_sendmsg()        kprobe + kretprobe  TCP bytes the app SENT
 *  App reads  ->  tcp_cleanup_rbuf()   kprobe              TCP bytes the app READ
 *  App writes ->  udp_sendmsg()        kprobe + kretprobe  UDP bytes SENT (IPv4)
 *                 udpv6_sendmsg()      kprobe + kretprobe  UDP bytes SENT (IPv6)
 *  App reads  ->  skb_consume_udp()    kprobe              UDP bytes READ (v4+v6)
 *  Network    ->  tcp_rcv_established  kprobe              refresh TCP wire stats
 *  Network    ->  tcp:tcp_retransmit_skb   tracepoint      every TCP retransmission
 *  Network    ->  __udp_enqueue_schedule_skb kretprobe     UDP drops (rcvbuf full)
 *  Lifecycle  ->  sock:inet_sock_set_state tracepoint      final TCP snapshot on close
 *
 * The socket-layer hooks run in PROCESS context (the app's own syscall), so
 * bpf_get_current_pid_tgid() really is the process that moved the bytes.
 * The network hooks run in SOFTIRQ context (any process may be "current"),
 * so there we never trust the current pid; we look the socket up instead.
 *
 * All counters live in BPF maps. User space reads them once per interval
 * and turns cumulative counters into rates. No per-packet event is sent to
 * user space (that is what made the old version slow and lossy); only rare
 * events (retransmissions, drops) go through the perf buffer.
 */
#include <uapi/linux/ptrace.h>
#include <linux/types.h>

/* User space may insert compatibility definitions here when BCC's bundled
 * uapi/linux/bpf.h is older than this kernel's headers (see compat.py). */
/* BWT_COMPAT_SHIM */

#include <linux/sched.h>
#include <linux/socket.h>
#include <linux/in.h>
#include <linux/in6.h>
#include <linux/ip.h>
#include <linux/ipv6.h>
#include <linux/udp.h>
#include <linux/tcp.h>
#include <linux/skbuff.h>
#include <net/sock.h>
#include <net/inet_sock.h>

/* Values injected by user space with -D...; defaults keep the file compilable. */
#ifndef SNAP_INTERVAL_NS
#define SNAP_INTERVAL_NS 100000000ULL /* refresh TCP wire stats at most every 100 ms */
#endif
#ifndef MAX_FLOWS
#define MAX_FLOWS 16384
#endif

#define DIR_TX 0
#define DIR_RX 1

#define EV_TCP_RETRANS 1
#define EV_UDP_DROP    2

/* Read a kernel field safely. Kernel memory cannot be dereferenced directly
 * from eBPF; bpf_probe_read_kernel() copies it and fails gracefully. */
#define KREAD(dst, src) bpf_probe_read_kernel(&(dst), sizeof(dst), (const void *)&(src))

/* ------------------------------------------------------------------ maps */

/* One entry per (socket, process, remote endpoint).
 * Addresses are always stored as 16-byte IPv6; IPv4 becomes ::ffff:a.b.c.d. */
struct flow_key_t {
    u64 sk;        /* kernel address of struct sock - used only as a socket ID */
    u32 tgid;      /* process id (thread-group id) */
    u16 family;    /* AF_INET / AF_INET6 of the socket */
    u16 proto;     /* IPPROTO_TCP / IPPROTO_UDP */
    u16 lport;     /* local port, host byte order */
    u16 rport;     /* remote port, host byte order */
    u32 pad;
    u8  laddr[16];
    u8  raddr[16];
};

struct flow_val_t {
    u64 tx_bytes;  /* payload bytes the app successfully handed to the kernel */
    u64 rx_bytes;  /* payload bytes the app read from the kernel */
    u64 tx_calls;
    u64 rx_calls;
    u64 first_ns;
    u64 last_ns;
    /* TCP wire counters of this flow's sockets that have CLOSED, added by the
     * close handler. Live sockets are read from tcp_snaps instead. Without
     * this, a connection that opens and closes between two reads of user
     * space (HTTP request, short download) would leave no wire statistics. */
    u64 closed_sent;
    u64 closed_retrans;
    u64 closed_acked;
    u64 closed_received;
    u64 closed_segs;
    char comm[TASK_COMM_LEN];
};

/* Per TCP socket: the kernel's own RFC 4898 counters, copied from struct
 * tcp_sock. "base_*" is the value when we first saw the socket so that
 * connections opened before the tracker started do not produce a spike. */
struct tcp_snap_t {
    u64 bytes_sent;       /* payload bytes put on the wire, INCLUDING retransmissions */
    u64 bytes_retrans;    /* payload bytes retransmitted */
    u64 bytes_acked;      /* payload bytes the peer confirmed (goodput) */
    u64 bytes_received;   /* payload bytes received from the peer */
    u64 base_sent;
    u64 base_retrans;
    u64 base_acked;
    u64 base_received;
    u64 retrans_events;   /* counted by the tcp_retransmit_skb tracepoint */
    u64 update_ns;
    u64 born_ns;          /* when we started tracking this socket (detects reuse) */
    u32 total_retrans;    /* retransmitted segments over the connection lifetime */
    u32 base_total_retrans;
    u32 srtt_us;          /* smoothed round-trip time (microseconds) */
    u32 snd_cwnd;         /* congestion window (segments) */
    u32 mss;
    u32 lost_out;         /* segments the kernel currently believes are lost */
    u32 segs_out;
    u32 segs_in;
    u32 state;            /* TCP state (1 = ESTABLISHED, 7 = CLOSE ...) */
    u32 pad;
};

struct owner_t {
    u32 tgid;
    u32 has_key;
    char comm[TASK_COMM_LEN];
    struct flow_key_t key;   /* flow that last used the socket (for the close handler) */
};

/* Arguments of an in-flight sendmsg(), saved by the kprobe so the kretprobe
 * (which only sees the return value) can use them. */
struct send_args_t {
    u64 sk;
    u8  has_dst;      /* destination given in msg_name (unconnected UDP sendto) */
    u8  is_v6fn;      /* entered through udpv6_sendmsg() */
    u16 dport;
    u32 pad;
    u8  daddr[16];
};

struct event_t {
    u64 ts_ns;
    u64 sk;
    u32 tgid;
    u32 type;         /* EV_* */
    u32 value;        /* bytes of the retransmitted segment / error code of the drop */
    u16 family;
    u16 lport;
    u16 rport;
    u16 pad;
    u32 pad2;
    u8  laddr[16];
    u8  raddr[16];
    char comm[TASK_COMM_LEN];
};

BPF_TABLE("lru_hash", struct flow_key_t, struct flow_val_t, flows, MAX_FLOWS);
BPF_TABLE("lru_hash", u64, struct tcp_snap_t, tcp_snaps, MAX_FLOWS);
BPF_TABLE("lru_hash", u64, struct owner_t, sk_owner, MAX_FLOWS);
BPF_TABLE("lru_hash", u64, u32, rx_seq, MAX_FLOWS);       /* last tp->copied_seq per socket */
BPF_TABLE("lru_hash", u64, u64, udp_drops, 4096);         /* drops per UDP socket */
BPF_HASH(tcp_send_args, u64, struct send_args_t, 4096);   /* key = pid_tgid of the thread */
BPF_HASH(udp_send_args, u64, struct send_args_t, 4096);
BPF_PERCPU_ARRAY(enqueue_sk, u64, 1);                     /* sk of the in-flight UDP enqueue */
BPF_PERF_OUTPUT(events);

/* --------------------------------------------------------------- helpers */

static __always_inline void set_v4mapped(u8 *dst, __be32 addr)
{
    dst[10] = 0xff;
    dst[11] = 0xff;
    __builtin_memcpy(&dst[12], &addr, 4);
}

/* Fill family/ports/addresses of a flow key from a struct sock. */
static __always_inline int key_from_sock(struct flow_key_t *k, struct sock *sk)
{
    u16 family = 0, lport = 0;
    __be16 dport = 0;

    KREAD(family, sk->__sk_common.skc_family);
    KREAD(lport, sk->__sk_common.skc_num);
    KREAD(dport, sk->__sk_common.skc_dport);
    k->family = family;
    k->lport = lport;
    k->rport = ntohs(dport);

    if (family == AF_INET) {
        __be32 saddr = 0, daddr = 0;
        KREAD(saddr, sk->__sk_common.skc_rcv_saddr);
        KREAD(daddr, sk->__sk_common.skc_daddr);
        set_v4mapped(k->laddr, saddr);
        set_v4mapped(k->raddr, daddr);
        return 0;
    }
#if IS_ENABLED(CONFIG_IPV6)
    if (family == AF_INET6) {
        bpf_probe_read_kernel(k->laddr, 16, &sk->__sk_common.skc_v6_rcv_saddr);
        bpf_probe_read_kernel(k->raddr, 16, &sk->__sk_common.skc_v6_daddr);
        return 0;
    }
#endif
    return -1;
}

static __always_inline void remember_owner(u64 sk, u32 tgid, struct flow_key_t *k)
{
    struct owner_t o = {};
    o.tgid = tgid;
    bpf_get_current_comm(&o.comm, sizeof(o.comm));
    if (k) {
        o.has_key = 1;
        __builtin_memcpy(&o.key, k, sizeof(o.key));
    }
    sk_owner.update(&sk, &o);
}

/* Add bytes to the flow counters. Atomic adds because the same socket can be
 * used by several threads on several CPUs at the same time. */
static __always_inline void account(struct flow_key_t *k, u64 bytes, int dir)
{
    struct flow_val_t zero = {};
    struct flow_val_t *v = flows.lookup_or_try_init(k, &zero);
    if (!v)
        return;

    u64 now = bpf_ktime_get_ns();
    if (v->first_ns == 0) {
        v->first_ns = now;
        bpf_get_current_comm(&v->comm, sizeof(v->comm));
    }
    v->last_ns = now;

    if (dir == DIR_TX) {
        __sync_fetch_and_add(&v->tx_bytes, bytes);
        __sync_fetch_and_add(&v->tx_calls, 1);
    } else {
        __sync_fetch_and_add(&v->rx_bytes, bytes);
        __sync_fetch_and_add(&v->rx_calls, 1);
    }
}

/* Copy the kernel's own TCP counters for this socket into tcp_snaps.
 * create=1 only from process context (we know who owns the socket);
 * force=1 ignores the 100 ms rate limit. */
static __always_inline void tcp_snapshot(struct sock *sk, int create, int force)
{
    u64 key = (u64)sk;
    u64 now = bpf_ktime_get_ns();
    struct tcp_snap_t *s = tcp_snaps.lookup(&key);
    int fresh = 0;

    if (!s) {
        if (!create)
            return;
        struct tcp_snap_t zero = {};
        tcp_snaps.update(&key, &zero);
        s = tcp_snaps.lookup(&key);
        if (!s)
            return;
        fresh = 1;
    } else if (!force && now - s->update_ns < SNAP_INTERVAL_NS) {
        return;
    }

    struct tcp_sock *tp = (struct tcp_sock *)sk;
    u64 sent = 0, retrans = 0, acked = 0, received = 0;
    u32 total_retrans = 0, srtt = 0, cwnd = 0, mss = 0, lost = 0, so = 0, si = 0;
    u8 state = 0;

    KREAD(sent, tp->bytes_sent);
    KREAD(retrans, tp->bytes_retrans);
    KREAD(acked, tp->bytes_acked);
    KREAD(received, tp->bytes_received);
    KREAD(total_retrans, tp->total_retrans);
    KREAD(srtt, tp->srtt_us);
    KREAD(cwnd, tp->snd_cwnd);
    KREAD(mss, tp->mss_cache);
    KREAD(lost, tp->lost_out);
    KREAD(so, tp->segs_out);
    KREAD(si, tp->segs_in);
    KREAD(state, sk->__sk_common.skc_state);

    /* Kernel counters only grow during a connection's life. If one went
     * backwards, this is a NEW socket that reuses a freed socket's address. */
    if (!fresh && (sent < s->bytes_sent || received < s->bytes_received || acked < s->bytes_acked)) {
        fresh = 1;
        s->retrans_events = 0;
    }
    if (fresh) {
        s->born_ns = now;
        s->base_sent = sent;
        s->base_retrans = retrans;
        s->base_acked = acked;
        s->base_received = received;
        s->base_total_retrans = total_retrans;
    }
    s->bytes_sent = sent;
    s->bytes_retrans = retrans;
    s->bytes_acked = acked;
    s->bytes_received = received;
    s->total_retrans = total_retrans;
    s->srtt_us = srtt >> 3;          /* kernel stores srtt << 3 */
    s->snd_cwnd = cwnd;
    s->mss = mss;
    s->lost_out = lost;
    s->segs_out = so;
    s->segs_in = si;
    s->state = state;
    s->update_ns = now;
}

static __always_inline void fill_event_owner(struct event_t *e, u64 sk)
{
    struct owner_t *o = sk_owner.lookup(&sk);
    if (o) {
        e->tgid = o->tgid;
        __builtin_memcpy(e->comm, o->comm, sizeof(e->comm));
    }
}

/* ============================================================ TCP send */

/* kprobe: runs when tcp_sendmsg(sk, msg, size) is ENTERED. `size` is only
 * what the app ASKED to send; a non-blocking socket may accept less. So we
 * just remember the socket and wait for the return value. */
int on_tcp_send_enter(struct pt_regs *ctx)
{
    u64 id = bpf_get_current_pid_tgid();
    struct send_args_t a = {};
    a.sk = (u64)PT_REGS_PARM1(ctx);
    tcp_send_args.update(&id, &a);
    return 0;
}

/* kretprobe: runs when tcp_sendmsg RETURNS. Return value = bytes really
 * copied into the socket send buffer (or a negative error). */
int on_tcp_send_return(struct pt_regs *ctx)
{
    u64 id = bpf_get_current_pid_tgid();
    struct send_args_t *a = tcp_send_args.lookup(&id);
    if (!a)
        return 0;
    struct sock *sk = (struct sock *)a->sk;
    tcp_send_args.delete(&id);

    int ret = PT_REGS_RC(ctx);
    u32 tgid = id >> 32;
    if (ret <= 0 || tgid == 0)
        return 0;

    struct flow_key_t k = {};
    k.sk = (u64)sk;
    k.tgid = tgid;
    k.proto = IPPROTO_TCP;
    if (key_from_sock(&k, sk) < 0)
        return 0;

    tcp_snapshot(sk, 1, 0);
    account(&k, (u64)ret, DIR_TX);
    remember_owner((u64)sk, tgid, &k);
    return 0;
}

/* ============================================================ TCP receive */

/* kprobe: tcp_cleanup_rbuf(sk, copied) is called by tcp_recvmsg() after data
 * was copied to the app (it decides whether to send a window-update ACK).
 * It can be called more than once per recvmsg() with a CUMULATIVE `copied`
 * (e.g. with MSG_WAITALL), so counting `copied` directly double counts.
 * Instead we use tp->copied_seq: the sequence number up to which the app
 * has consumed data. Its increase since the last call = new bytes read. */
int on_tcp_cleanup_rbuf(struct pt_regs *ctx)
{
    struct sock *sk = (struct sock *)PT_REGS_PARM1(ctx);
    int copied = (int)PT_REGS_PARM2(ctx);
    u64 id = bpf_get_current_pid_tgid();
    u32 tgid = id >> 32;
    if (copied <= 0 || tgid == 0)
        return 0;

    struct tcp_sock *tp = (struct tcp_sock *)sk;
    u32 seq = 0, rcv_nxt = 0;
    u8 st = 0;
    KREAD(seq, tp->copied_seq);
    KREAD(rcv_nxt, tp->rcv_nxt);
    KREAD(st, sk->__sk_common.skc_state);
    /* The peer's FIN occupies one sequence number, and tcp_recvmsg() moves
     * copied_seq past it when the app reads the end of the stream. That
     * +1 is not data. After a FIN arrived (states below) and everything up
     * to rcv_nxt is consumed, the FIN was consumed too: don't count it. */
    if (seq == rcv_nxt && (st == TCP_CLOSE_WAIT || st == TCP_LAST_ACK ||
                           st == TCP_CLOSING || st == TCP_TIME_WAIT))
        seq -= 1;

    u64 skey = (u64)sk;
    u32 delta = (u32)copied;
    u32 *last = rx_seq.lookup(&skey);
    if (last) {
        u32 d = seq - *last;          /* u32 arithmetic handles wrap-around */
        if (d <= (u32)copied)         /* larger => stale entry, trust `copied` */
            delta = d;
        *last = seq;
    } else {
        rx_seq.update(&skey, &seq);
    }
    if (delta == 0)
        return 0;

    struct flow_key_t k = {};
    k.sk = skey;
    k.tgid = tgid;
    k.proto = IPPROTO_TCP;
    if (key_from_sock(&k, sk) < 0)
        return 0;

    tcp_snapshot(sk, 1, 0);
    account(&k, delta, DIR_RX);
    remember_owner(skey, tgid, &k);
    return 0;
}

/* ============================================================ TCP wire side */

/* kprobe on the receive fast path: called (in softirq) for every segment
 * that arrives on an ESTABLISHED socket - data or ACK. We use it only to
 * refresh the kernel counters (rate limited), so wire/acked bytes keep
 * updating even while the app is blocked and the send buffer drains. */
int on_tcp_rcv_established(struct pt_regs *ctx)
{
    struct sock *sk = (struct sock *)PT_REGS_PARM1(ctx);
    tcp_snapshot(sk, 0, 0);
    return 0;
}

/* Tracepoint: fired by the kernel each time a TCP segment is RETRANSMITTED
 * (timeout, fast retransmit, tail loss probe...). This is the visible
 * symptom of packet loss on the path. */
TRACEPOINT_PROBE(tcp, tcp_retransmit_skb)
{
    u64 skey = (u64)args->skaddr;
    struct tcp_snap_t *s = tcp_snaps.lookup(&skey);
    if (!s)
        return 0; /* socket opened before the tracker started and never used since */
    __sync_fetch_and_add(&s->retrans_events, 1);
    tcp_snapshot((struct sock *)skey, 0, 1);
    if (!sk_owner.lookup(&skey))
        return 0; /* counted, but no process to report it for */

    struct event_t e = {};
    struct sk_buff *skb = (struct sk_buff *)args->skbaddr;
    u32 len = 0;
    KREAD(len, skb->len);
    e.ts_ns = bpf_ktime_get_ns();
    e.sk = skey;
    e.type = EV_TCP_RETRANS;
    e.value = len;
    e.family = args->family;
    e.lport = args->sport;
    e.rport = args->dport;
    __builtin_memcpy(e.laddr, args->saddr_v6, 16);  /* v4 arrives v4-mapped */
    __builtin_memcpy(e.raddr, args->daddr_v6, 16);
    fill_event_owner(&e, skey);
    events.perf_submit(args, &e, sizeof(e));
    return 0;
}

/* Add a finished socket's wire counters (relative to its baseline) to the
 * flow that owns it, then forget the socket. */
static __always_inline void retire_socket(u64 skey, struct tcp_snap_t *s)
{
    struct owner_t *o = sk_owner.lookup(&skey);
    if (o && o->has_key) {
        struct flow_key_t k = {};
        __builtin_memcpy(&k, &o->key, sizeof(k));
        struct flow_val_t *v = flows.lookup(&k);
        if (v) {
            __sync_fetch_and_add(&v->closed_sent, s->bytes_sent - s->base_sent);
            __sync_fetch_and_add(&v->closed_retrans, s->bytes_retrans - s->base_retrans);
            __sync_fetch_and_add(&v->closed_acked, s->bytes_acked - s->base_acked);
            __sync_fetch_and_add(&v->closed_received, s->bytes_received - s->base_received);
            __sync_fetch_and_add(&v->closed_segs, (u32)(s->total_retrans - s->base_total_retrans));
        }
    }
    tcp_snaps.delete(&skey);
    rx_seq.delete(&skey);
    sk_owner.delete(&skey);
}

/* Tracepoint: TCP state machine changes. Take a last snapshot when the
 * connection closes so its final counters are not lost. */
TRACEPOINT_PROBE(sock, inet_sock_set_state)
{
    if (args->protocol != IPPROTO_TCP)
        return 0;
    u64 skey = (u64)args->skaddr;
    /* A connection is starting: its counters are 0, so start a snapshot
     * with base 0 right now. Then even the very first bytes it sends are
     * counted on the wire. (The memory of struct sock may have belonged to
     * an earlier, closed socket: whatever we stored for that is replaced.) */
    if (args->newstate == TCP_SYN_SENT || args->newstate == TCP_SYN_RECV ||
        (args->newstate == TCP_ESTABLISHED && args->oldstate == TCP_SYN_RECV)) {
        struct tcp_snap_t *old = tcp_snaps.lookup(&skey);
        if (old) {
            if (args->newstate == TCP_ESTABLISHED)
                return 0;
            /* Still holding a previous socket at this address: its close was
             * not seen (a tracepoint program is skipped if it fires while
             * another eBPF program runs on the same CPU). Retire it with its
             * last snapshot so its bytes are not lost. */
            retire_socket(skey, old);
        }
        struct tcp_snap_t zero = {};
        zero.born_ns = bpf_ktime_get_ns();
        zero.update_ns = zero.born_ns;
        zero.state = args->newstate;
        tcp_snaps.update(&skey, &zero);
        rx_seq.delete(&skey);
        return 0;
    }
    struct tcp_snap_t *s = tcp_snaps.lookup(&skey);
    if (!s)
        return 0;
    tcp_snapshot((struct sock *)skey, 0, 1);
    if (args->newstate != TCP_CLOSE)
        return 0;

    /* The connection is over: add its final wire counters to the flow it
     * belongs to and forget the socket (its memory will be reused). */
    retire_socket(skey, s);
    return 0;
}

/* ============================================================ UDP send */

/* UDP is connectionless: sendto() carries the destination in msg->msg_name,
 * and the socket's own daddr is 0. So we must read the destination from the
 * msghdr at entry. udpv6_sendmsg() calls udp_sendmsg() for IPv4-mapped
 * destinations, so a call can be NESTED; the outer call does the counting. */
static __always_inline int udp_send_enter(struct pt_regs *ctx, u8 is_v6fn)
{
    u64 id = bpf_get_current_pid_tgid();
    if (udp_send_args.lookup(&id))
        return 0; /* nested call: outer udpv6_sendmsg() will account */

    struct send_args_t a = {};
    a.sk = (u64)PT_REGS_PARM1(ctx);
    a.is_v6fn = is_v6fn;

    struct msghdr *msg = (struct msghdr *)PT_REGS_PARM2(ctx);
    void *name = NULL;
    int namelen = 0;
    KREAD(name, msg->msg_name);
    KREAD(namelen, msg->msg_namelen);
    if (name && namelen >= (int)sizeof(struct sockaddr_in)) {
        u16 fam = 0;
        bpf_probe_read_kernel(&fam, sizeof(fam), name);
        if (fam == AF_INET) {
            struct sockaddr_in sin = {};
            bpf_probe_read_kernel(&sin, sizeof(sin), name);
            a.has_dst = 1;
            a.dport = ntohs(sin.sin_port);
            set_v4mapped(a.daddr, sin.sin_addr.s_addr);
        } else if (fam == AF_INET6 && namelen >= (int)sizeof(struct sockaddr_in6)) {
            struct sockaddr_in6 sin6 = {};
            bpf_probe_read_kernel(&sin6, sizeof(sin6), name);
            a.has_dst = 1;
            a.dport = ntohs(sin6.sin6_port);
            __builtin_memcpy(a.daddr, &sin6.sin6_addr, 16);
        }
    }
    udp_send_args.update(&id, &a);
    return 0;
}

static __always_inline int udp_send_return(struct pt_regs *ctx, u8 is_v6fn)
{
    u64 id = bpf_get_current_pid_tgid();
    struct send_args_t *ap = udp_send_args.lookup(&id);
    if (!ap)
        return 0;
    if (ap->is_v6fn != is_v6fn)
        return 0; /* inner return of a nested call - ignore, keep the args */

    struct send_args_t a = *ap;
    udp_send_args.delete(&id);

    int ret = PT_REGS_RC(ctx);
    u32 tgid = id >> 32;
    if (ret <= 0 || tgid == 0)
        return 0;

    struct sock *sk = (struct sock *)a.sk;
    struct flow_key_t k = {};
    k.sk = a.sk;
    k.tgid = tgid;
    k.proto = IPPROTO_UDP;
    if (key_from_sock(&k, sk) < 0)
        return 0;
    if (a.has_dst) {
        k.rport = a.dport;
        __builtin_memcpy(k.raddr, a.daddr, 16);
    }
    account(&k, (u64)ret, DIR_TX);
    remember_owner(a.sk, tgid, 0);
    return 0;
}

int on_udp_send_enter(struct pt_regs *ctx)    { return udp_send_enter(ctx, 0); }
int on_udp_send_return(struct pt_regs *ctx)   { return udp_send_return(ctx, 0); }
int on_udpv6_send_enter(struct pt_regs *ctx)  { return udp_send_enter(ctx, 1); }
int on_udpv6_send_return(struct pt_regs *ctx) { return udp_send_return(ctx, 1); }

/* ============================================================ UDP receive */

/* skb_consume_udp(sk, skb, len) is called by udp_recvmsg() AND
 * udpv6_recvmsg() after `len` bytes were copied to the app. The sender's
 * address is taken from the packet itself (IP + UDP headers in the skb). */
int on_skb_consume_udp(struct pt_regs *ctx)
{
    struct sock *sk = (struct sock *)PT_REGS_PARM1(ctx);
    struct sk_buff *skb = (struct sk_buff *)PT_REGS_PARM2(ctx);
    int len = (int)PT_REGS_PARM3(ctx);
    u64 id = bpf_get_current_pid_tgid();
    u32 tgid = id >> 32;
    if (len <= 0 || tgid == 0)
        return 0;

    struct flow_key_t k = {};
    k.sk = (u64)sk;
    k.tgid = tgid;
    k.proto = IPPROTO_UDP;
    if (key_from_sock(&k, sk) < 0)
        return 0;

    unsigned char *head = NULL;
    u16 nh = 0, th = 0;
    KREAD(head, skb->head);
    KREAD(nh, skb->network_header);
    KREAD(th, skb->transport_header);

    u8 ver = 0;
    bpf_probe_read_kernel(&ver, 1, head + nh);
    ver >>= 4;
    if (ver == 4) {
        struct iphdr ip = {};
        bpf_probe_read_kernel(&ip, sizeof(ip), head + nh);
        __builtin_memset(k.raddr, 0, 16);
        __builtin_memset(k.laddr, 0, 16);
        set_v4mapped(k.raddr, ip.saddr);
        set_v4mapped(k.laddr, ip.daddr);
    } else if (ver == 6) {
        struct ipv6hdr ip6 = {};
        bpf_probe_read_kernel(&ip6, sizeof(ip6), head + nh);
        __builtin_memcpy(k.raddr, &ip6.saddr, 16);
        __builtin_memcpy(k.laddr, &ip6.daddr, 16);
    }
    struct udphdr uh = {};
    bpf_probe_read_kernel(&uh, sizeof(uh), head + th);
    k.rport = ntohs(uh.source);

    account(&k, (u64)len, DIR_RX);
    remember_owner(k.sk, tgid, 0);
    return 0;
}

/* __udp_enqueue_schedule_skb(sk, skb) puts an arriving datagram into the
 * socket receive queue. A negative return = queue full = datagram DROPPED
 * (the app is not reading fast enough). Runs in softirq context. */
int on_udp_enqueue_enter(struct pt_regs *ctx)
{
    u32 zero = 0;
    u64 *slot = enqueue_sk.lookup(&zero);
    if (slot)
        *slot = (u64)PT_REGS_PARM1(ctx);
    return 0;
}

int on_udp_enqueue_return(struct pt_regs *ctx)
{
    int rc = PT_REGS_RC(ctx);
    if (rc >= 0)
        return 0;
    u32 zero = 0;
    u64 *slot = enqueue_sk.lookup(&zero);
    if (!slot || *slot == 0)
        return 0;
    u64 skey = *slot;

    u64 one = 1;
    u64 *cnt = udp_drops.lookup(&skey);
    if (cnt)
        __sync_fetch_and_add(cnt, 1);
    else
        udp_drops.update(&skey, &one);

    struct event_t e = {};
    struct sock *sk = (struct sock *)skey;
    struct flow_key_t k = {};
    key_from_sock(&k, sk);
    e.ts_ns = bpf_ktime_get_ns();
    e.sk = skey;
    e.type = EV_UDP_DROP;
    e.value = (u32)(-rc);
    e.family = k.family;
    e.lport = k.lport;
    e.rport = k.rport;
    __builtin_memcpy(e.laddr, k.laddr, 16);
    __builtin_memcpy(e.raddr, k.raddr, 16);
    fill_event_owner(&e, skey);
    events.perf_submit(ctx, &e, sizeof(e));
    return 0;
}
