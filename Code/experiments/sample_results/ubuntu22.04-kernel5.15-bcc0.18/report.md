# eBPF bandwidth tracker - accuracy and packet-loss experiments
Host `testvm22`, kernel `5.15.0-25-generic`, linux. Peer `local` via `lo`. Loss applied with `tc netem` (both), extra delay 0.0 ms. Started 2026-10-25 23:21:19.

## 1. TCP upload: socket layer vs wire
The app-layer counter (kprobe on `tcp_sendmsg` return) should match what the app sent exactly. The wire counter (`tcp_sock->bytes_sent`) is larger whenever segments are lost and resent.

| netem loss % | sendfile | app sent (truth) | eBPF app tx | app accuracy % | eBPF wire tx | retransmitted B | retx % of wire | retx segs | NIC tx B | goodput Mbit/s | sRTT ms | per-second app vs wire dev % (mean/max) | wire lags app by s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.0 | False | 31457299 | 31457299 | 100.0 | 31457280 | 0 | 0.0 | 0 | 31530073 | 863.473 | 1.2 | 0.0/0.0 | 0.0 |
| 0.0 | True | 31457299 | 31457299 | 100.0 | 31457280 | 0 | 0.0 | 0 | 31522532 | 550.064 | 0.64 | 29.91/47.48 | 0.0 |
| 2.0 | False | 31457299 | 31457299 | 100.0 | 31785172 | 327892 | 1.032 | 11 | 31512268 | 747.327 | 1.04 | 16.02/22.5 | 0.0 |
| 2.0 | True | 31457299 | 31457299 | 100.0 | 31723520 | 266240 | 0.839 | 7 | 31522496 | 725.117 | 0.56 | 0.84/0.84 | 0.0 |
| 5.0 | False | 31457299 | 31457299 | 100.0 | 33029349 | 1572069 | 4.76 | 31 | 31510152 | 393.649 | 1.83 | 10.5/11.05 | 0.0 |
| 5.0 | True | 31457299 | 31457299 | 100.0 | 33459005 | 2001725 | 5.983 | 36 | 31605863 | 175.019 | 4.68 | 23.28/71.97 | 0.0 |
| 10.0 | False | 31457299 | 31457299 | 100.0 | 35812403 | 4355123 | 12.161 | 70 | 31961759 | 61.649 | 7.92 | 35.8/100.0 | 0.5 |
| 10.0 | True | 31457299 | 31457299 | 100.0 | 36166181 | 4708901 | 13.02 | 81 | 31632615 | 63.556 | 15.92 | 38.3/100.0 | 1.0 |

## 2. TCP download
| netem loss % | app received (truth) | eBPF app rx | accuracy % | peer retx % (if peer traced) | goodput Mbit/s |
|---|---|---|---|---|---|
| 0.0 | 31457280 | 31457280 | 100.0 | 0.0 | 723.73 |
| 2.0 | 31457280 | 31457280 | 100.0 | 2.24 | 837.737 |
| 5.0 | 31457280 | 31457280 | 100.0 | 5.416 | 385.48 |
| 10.0 | 31457280 | 31457280 | 100.0 | 10.237 | 35.417 |

## 3. UDP upload: the sender cannot see loss
| netem loss % | app sent (truth) | eBPF sender tx | accuracy % | true loss % (seq numbers) | eBPF measured loss % (both ends traced) | eBPF rcvbuf drops |
|---|---|---|---|---|---|---|
| 0.0 | 7500000 | 7500000 | 100.0 | 0.0 | 0.0 | 0 |
| 2.0 | 7500000 | 7500000 | 100.0 | 1.76 | 1.76 | 0 |
| 5.0 | 7500000 | 7500000 | 100.0 | 4.928 | 4.928 | 0 |
| 10.0 | 7500000 | 7500000 | 100.0 | 9.568 | 9.568 | 0 |

## 4. UDP download
| netem loss % | peer sent | app received (truth) | eBPF app rx | accuracy % | true loss % | eBPF measured loss % |
|---|---|---|---|---|---|---|
| 0.0 | 7500000 | 7500000 | 7500000 | 100.0 | 0.0 | 0.0 |
| 2.0 | 7500000 | 7351200 | 7351200 | 100.0 | 1.984 | 1.984 |
| 5.0 | 7500000 | 7102800 | 7102800 | 100.0 | 5.296 | 5.296 |
| 10.0 | 7500000 | 6774000 | 6774000 | 100.0 | 9.68 | 9.68 |

## 5. UDP receive-buffer overflow (host-side drops)
| sent pkts | app received pkts | kernel RcvbufErrors (truth) | eBPF drops | eBPF drop events | eBPF rx accuracy % |
|---|---|---|---|---|---|
| 3000 | 92 | 2908 | 2908 | 2908 | 100.0 |

## Tracker health during the runs
| mode | loss % | kretprobe misses | tracker CPU % (peak) | eBPF ns/run | eBPF CPU % |
|---|---|---|---|---|---|
| tcp-up | 0.0 | 0 | 9.9 | 1268.0 | 5.55 |
| tcp-up | 0.0 | 0 | 5.97 | 2124.6 | 11.061 |
| tcp-down | 0.0 | 0 | 20.98 | 211796.3 | 5.515 |
| udp-up | 0.0 | 0 | 13.73 | 5689.5 | 10.743 |
| udp-down | 0.0 | 0 | 7.97 | 7090.2 | 10.776 |
| tcp-up | 2.0 | 0 | 9.96 | 2684.7 | 3.444 |
| tcp-up | 2.0 | 0 | 7.97 | 2723.2 | 14.124 |
| tcp-down | 2.0 | 0 | 7.98 | 1468.7 | 5.37 |
| udp-up | 2.0 | 0 | 9.96 | 7066.3 | 14.802 |
| udp-down | 2.0 | 0 | 9.96 | 9057.9 | 11.003 |
| tcp-up | 5.0 | 0 | 11.97 | 3386.6 | 3.648 |
| tcp-up | 5.0 | 0 | 7.97 | 3901.4 | 5.675 |
| tcp-down | 5.0 | 0 | 5.99 | 4294.8 | 1.96 |
| udp-up | 5.0 | 0 | 5.98 | 6000.7 | 11.127 |
| udp-down | 5.0 | 0 | 7.97 | 7017.2 | 10.091 |
| tcp-up | 10.0 | 0 | 9.96 | 8454.1 | 1.821 |
| tcp-up | 10.0 | 0 | 9.98 | 20785.3 | 4.677 |
| tcp-down | 10.0 | 0 | 9.86 | 41952.4 | 1.801 |
| udp-up | 10.0 | 0 | 7.97 | 7143.4 | 9.999 |
| udp-down | 10.0 | 0 | 7.98 | 7446.4 | 10.54 |
| udp-rcvbuf | 0 |  |  |  |  |
