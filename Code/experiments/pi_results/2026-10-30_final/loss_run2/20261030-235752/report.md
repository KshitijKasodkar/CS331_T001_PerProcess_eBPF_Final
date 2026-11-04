# eBPF bandwidth tracker - accuracy and packet-loss experiments
Host `kasodkar`, kernel `6.18.50+rpt-rpi-2712`, Raspberry Pi 5 Model B Rev 1.1. Peer `10.58.105.150` via `wlan0`. Loss applied with `tc netem` (both), extra delay 0.0 ms. Started 2026-10-30 23:57:52.

## 1. TCP upload: socket layer vs wire
The app-layer counter (kprobe on `tcp_sendmsg` return) should match what the app sent exactly. The wire counter (`tcp_sock->bytes_sent`) is larger whenever segments are lost and resent.

| netem loss % | sendfile | app sent (truth) | eBPF app tx | app accuracy % | eBPF wire tx | retransmitted B | retx % of wire | retx segs | NIC tx B | goodput Mbit/s | sRTT ms | per-sample (0.5 s) app vs wire dev % (mean/max) | wire lags app by s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.0 | False | 20971539 | 20971539 | 100.0 | 20971539 | 0 | 0.0 | 0 | 21928151 | 185.127 | 31.3 | 23.39/25.74 | 0.0 |
| 1.0 | False | 20971539 | 20971539 | 100.0 | 21101859 | 130320 | 0.618 | 90 | 21928085 | 178.824 | 27.96 | 47.72/89.06 | 0.0 |
| 2.0 | False | 20971539 | 20971539 | 100.0 | 21329195 | 357656 | 1.677 | 247 | 21928415 | 142.216 | 11.91 | 66.34/136.21 | 0.5 |
| 5.0 | False | 20971539 | 20971539 | 100.0 | 22248675 | 1277136 | 5.74 | 882 | 22044795 | 16.101 | 7.26 | 24.98/100.0 | 1.5 |
| 10.0 | False | 20971539 | 20971539 | 100.0 | 23511331 | 2539792 | 10.802 | 1755 | 22036135 | 1.812 | 23.13 | 88.32/4425.97 | 9.01 |

## 2. TCP download
| netem loss % | app received (truth) | eBPF app rx | accuracy % | peer retx % (if peer traced) | goodput Mbit/s |
|---|---|---|---|---|---|
| 0.0 | 20971520 | 20971520 | 100.0 |  | 119.653 |
| 1.0 | 20971520 | 20971520 | 100.0 |  | 16.146 |
| 2.0 | 20971520 | 20971520 | 100.0 |  | 13.016 |
| 5.0 | 20971520 | 20971520 | 100.0 |  | 8.708 |
| 10.0 | 20971520 | 20971520 | 100.0 |  | 6.439 |

## 3. UDP upload: the sender cannot see loss
| netem loss % | app sent (truth) | eBPF sender tx | accuracy % | true loss % (seq numbers) | eBPF measured loss % (both ends traced) | eBPF rcvbuf drops |
|---|---|---|---|---|---|---|
| 0.0 | 12500400 | 12500400 | 100.0 | 0.0 |  |  |
| 1.0 | 12500400 | 12500400 | 100.0 | 1.123 |  |  |
| 2.0 | 12500400 | 12500400 | 100.0 | 1.843 |  |  |
| 5.0 | 12500400 | 12500400 | 100.0 | 5.117 |  |  |
| 10.0 | 12500400 | 12500400 | 100.0 | 9.254 |  |  |

## 4. UDP download
| netem loss % | peer sent | app received (truth) | eBPF app rx | accuracy % | true loss % | eBPF measured loss % |
|---|---|---|---|---|---|---|
| 0.0 | 12499200 | 12499200 | 12499200 | 100.0 | 0.0 | 0.0 |
| 1.0 | 12500400 | 12373200 | 12373200 | 100.0 | 1.018 | 1.018 |
| 2.0 | 12500400 | 12236400 | 12236400 | 100.0 | 2.112 | 2.112 |
| 5.0 | 12499200 | 11882400 | 11882400 | 100.0 | 4.935 | 4.935 |
| 10.0 | 12500400 | 11247600 | 11247600 | 100.0 | 10.022 | 10.022 |

## Tracker health during the runs
| mode | loss % | kretprobe misses | tracker CPU % (peak) | eBPF ns/run | eBPF CPU % |
|---|---|---|---|---|---|
| tcp-up | 0.0 | 0 | 1.99 | 212.7 | 0.053 |
| tcp-down | 0.0 | 0 | 2.0 | 690.3 | 0.247 |
| udp-up | 0.0 | 0 | 2.0 | 504.4 | 0.196 |
| udp-down | 0.0 | 0 | 2.0 | 453.9 | 0.128 |
| tcp-up | 1.0 | 0 | 2.0 | 295.5 | 0.046 |
| tcp-down | 1.0 | 0 | 2.0 | 394.0 | 0.089 |
| udp-up | 1.0 | 0 | 2.0 | 601.1 | 0.315 |
| udp-down | 1.0 | 0 | 2.0 | 493.3 | 0.129 |
| tcp-up | 2.0 | 0 | 2.0 | 332.2 | 0.043 |
| tcp-down | 2.0 | 0 | 2.0 | 438.1 | 0.079 |
| udp-up | 2.0 | 0 | 2.0 | 687.6 | 0.198 |
| udp-down | 2.0 | 0 | 2.0 | 763.1 | 0.196 |
| tcp-up | 5.0 | 0 | 2.0 | 777.0 | 0.041 |
| tcp-down | 5.0 | 0 | 2.0 | 395.1 | 0.065 |
| udp-up | 5.0 | 0 | 2.0 | 691.5 | 0.211 |
| udp-down | 5.0 | 0 | 2.0 | 690.8 | 0.125 |
| tcp-up | 10.0 | 0 | 3.99 | 1147.4 | 0.056 |
| tcp-down | 10.0 | 0 | 2.0 | 376.4 | 0.057 |
| udp-up | 10.0 | 0 | 2.0 | 848.1 | 0.311 |
| udp-down | 10.0 | 0 | 3.99 | 579.3 | 0.182 |
