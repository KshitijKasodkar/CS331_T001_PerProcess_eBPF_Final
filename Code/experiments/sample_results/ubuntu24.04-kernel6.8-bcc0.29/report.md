# eBPF bandwidth tracker - accuracy and packet-loss experiments
Host `testvm`, kernel `6.8.0-31-generic`, linux. Peer `local` via `lo`. Loss applied with `tc netem` (both), extra delay 0.0 ms. Started 2026-10-25 23:26:36.

## 1. TCP upload: socket layer vs wire
The app-layer counter (kprobe on `tcp_sendmsg` return) should match what the app sent exactly. The wire counter (`tcp_sock->bytes_sent`) is larger whenever segments are lost and resent.

| netem loss % | sendfile | app sent (truth) | eBPF app tx | app accuracy % | eBPF wire tx | retransmitted B | retx % of wire | retx segs | NIC tx B | goodput Mbit/s | sRTT ms | per-second app vs wire dev % (mean/max) | wire lags app by s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.0 | False | 31457299 | 31457299 | 100.0 | 31457280 | 0 | 0.0 | 0 | 31523001 | 412.263 | 1.01 | 22.67/32.59 | 0.0 |
| 1.0 | False | 31457299 | 31457299 | 100.0 | 31522763 | 65483 | 0.208 | 1 | 31520961 | 782.158 | 1.46 | 55.13/69.66 | 0.0 |
| 2.0 | False | 31457299 | 31457299 | 100.0 | 32309089 | 851809 | 2.636 | 22 | 31526854 | 737.231 | 0.73 | 2.64/2.64 | 0.0 |
| 5.0 | False | 31457299 | 31457299 | 100.0 | 33095203 | 1637923 | 4.949 | 36 | 31576439 | 262.253 | 3.19 | 48.18/100.0 | 0.47 |
| 10.0 | False | 31457299 | 31457299 | 100.0 | 34560934 | 3601830 | 10.422 | 60 | 31767291 | 95.218 | 3.0 | 20.19/74.36 | 0.0 |

## 2. TCP download
| netem loss % | app received (truth) | eBPF app rx | accuracy % | peer retx % (if peer traced) | goodput Mbit/s |
|---|---|---|---|---|---|
| 0.0 | 31457280 | 31457280 | 100.0 | 0.0 | 599.248 |
| 1.0 | 31457280 | 31457280 | 100.0 | 1.234 | 554.034 |
| 2.0 | 31457280 | 31457280 | 100.0 | 2.24 | 413.404 |
| 5.0 | 31457280 | 31457280 | 100.0 | 4.619 | 303.908 |
| 10.0 | 31457280 | 31457280 | 100.0 | 13.086 | 57.074 |

## 3. UDP upload: the sender cannot see loss
| netem loss % | app sent (truth) | eBPF sender tx | accuracy % | true loss % (seq numbers) | eBPF measured loss % (both ends traced) | eBPF rcvbuf drops |
|---|---|---|---|---|---|---|
| 0.0 | 7500000 | 7500000 | 100.0 | 0.0 | 0.0 | 0 |
| 1.0 | 7500000 | 7500000 | 100.0 | 0.88 | 0.88 | 0 |
| 2.0 | 7496400 | 7496400 | 100.0 | 2.001 | 2.001 | 0 |
| 5.0 | 7500000 | 7500000 | 100.0 | 5.248 | 5.248 | 0 |
| 10.0 | 7495200 | 7495200 | 100.0 | 9.974 | 9.974 | 0 |

## 4. UDP download
| netem loss % | peer sent | app received (truth) | eBPF app rx | accuracy % | true loss % | eBPF measured loss % |
|---|---|---|---|---|---|---|
| 0.0 | 7500000 | 7500000 | 7500000 | 100.0 | 0.0 | 0.0 |
| 1.0 | 7500000 | 7420800 | 7420800 | 100.0 | 1.056 | 1.056 |
| 2.0 | 7500000 | 7356000 | 7356000 | 100.0 | 1.92 | 1.92 |
| 5.0 | 7500000 | 7143600 | 7143600 | 100.0 | 4.752 | 4.752 |
| 10.0 | 7500000 | 6782400 | 6782400 | 100.0 | 9.568 | 9.568 |

## 5. UDP receive-buffer overflow (host-side drops)
| sent pkts | app received pkts | kernel RcvbufErrors (truth) | eBPF drops | eBPF drop events | eBPF rx accuracy % |
|---|---|---|---|---|---|
| 3000 | 129 | 2871 | 2871 | 2871 | 100.0 |

## Tracker health during the runs
| mode | loss % | kretprobe misses | tracker CPU % (peak) | eBPF ns/run | eBPF CPU % |
|---|---|---|---|---|---|
| tcp-up | 0.0 | 0 | 28.83 | 3844.2 | 4.567 |
| tcp-down | 0.0 | 0 | 15.96 | 2040.6 | 7.063 |
| udp-up | 0.0 | 0 | 9.96 | 7728.6 | 12.545 |
| udp-down | 0.0 | 0 | 11.93 | 9701.3 | 12.964 |
| tcp-up | 1.0 | 0 | 9.94 | 3804.8 | 5.922 |
| tcp-down | 1.0 | 0 | 15.64 | 5653.8 | 4.84 |
| udp-up | 1.0 | 0 | 11.95 | 9303.7 | 12.532 |
| udp-down | 1.0 | 0 | 13.95 | 10149.5 | 12.687 |
| tcp-up | 2.0 | 0 | 13.95 | 2074.5 | 6.577 |
| tcp-down | 2.0 | 0 | 11.94 | 4244.9 | 3.548 |
| udp-up | 2.0 | 0 | 11.95 | 8079.3 | 15.758 |
| udp-down | 2.0 | 0 | 13.94 | 9401.4 | 15.255 |
| tcp-up | 5.0 | 0 | 15.81 | 6405.7 | 5.638 |
| tcp-down | 5.0 | 0 | 13.92 | 10673.6 | 4.287 |
| udp-up | 5.0 | 0 | 11.95 | 6612.8 | 11.652 |
| udp-down | 5.0 | 0 | 11.93 | 16438.9 | 13.457 |
| tcp-up | 10.0 | 0 | 13.95 | 12931.3 | 1.487 |
| tcp-down | 10.0 | 0 | 15.91 | 14172.5 | 2.554 |
| udp-up | 10.0 | 0 | 13.92 | 11333.4 | 14.057 |
| udp-down | 10.0 | 0 | 11.94 | 18424.9 | 15.958 |
| udp-rcvbuf | 0 |  |  |  |  |
