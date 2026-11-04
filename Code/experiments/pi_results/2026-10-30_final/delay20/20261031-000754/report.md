# eBPF bandwidth tracker - accuracy and packet-loss experiments
Host `kasodkar`, kernel `6.18.50+rpt-rpi-2712`, Raspberry Pi 5 Model B Rev 1.1. Peer `10.58.105.150` via `wlan0`. Loss applied with `tc netem` (both), extra delay 20.0 ms. Started 2026-10-31 00:07:54.

## 1. TCP upload: socket layer vs wire
The app-layer counter (kprobe on `tcp_sendmsg` return) should match what the app sent exactly. The wire counter (`tcp_sock->bytes_sent`) is larger whenever segments are lost and resent.

| netem loss % | sendfile | app sent (truth) | eBPF app tx | app accuracy % | eBPF wire tx | retransmitted B | retx % of wire | retx segs | NIC tx B | goodput Mbit/s | sRTT ms | per-sample (0.5 s) app vs wire dev % (mean/max) | wire lags app by s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.0 | False | 20971539 | 20971539 | 100.0 | 20971539 | 0 | 0.0 | 0 | 21928019 | 115.704 | 70.57 | 95.65/247.76 | 0.5 |
| 1.0 | False | 20971539 | 20971539 | 100.0 | 21191635 | 220096 | 1.039 | 152 | 21928693 | 3.741 | 50.26 | 14.58/100.0 | 0.5 |
| 2.0 | False | 20971539 | 20971539 | 100.0 | 21432003 | 460464 | 2.148 | 318 | 21928769 | 2.498 | 51.19 | 19.28/100.0 | 1.0 |
| 5.0 | False | 20971539 | 20971539 | 100.0 | 22268587 | 1297048 | 5.825 | 898 | 21997517 | 1.403 | 56.95 | 56.91/750.45 | 15.53 |
| 10.0 | False | 20971539 | 20971539 | 100.0 | 23647462 | 2675923 | 11.316 | 1849 | 22164233 | 0.461 | 78.51 | 91.25/2100.0 | 2.0 |

## 2. TCP download
| netem loss % | app received (truth) | eBPF app rx | accuracy % | peer retx % (if peer traced) | goodput Mbit/s |
|---|---|---|---|---|---|
| 0.0 | 20971520 | 20971520 | 100.0 |  | 130.939 |
| 1.0 | 20971520 | 20971520 | 100.0 |  | 3.077 |
| 2.0 | 20971520 | 20971520 | 100.0 |  | 2.45 |
| 5.0 | 20971520 | 20971520 | 100.0 |  | 1.998 |
| 10.0 | 20971520 | 20971520 | 100.0 |  | 1.432 |

## 3. UDP upload: the sender cannot see loss
_no data_

## Tracker health during the runs
| mode | loss % | kretprobe misses | tracker CPU % (peak) | eBPF ns/run | eBPF CPU % |
|---|---|---|---|---|---|
| tcp-up | 0.0 | 0 | 3.99 | 376.6 | 0.036 |
| tcp-down | 0.0 | 0 | 2.0 | 143.9 | 0.282 |
| tcp-up | 1.0 | 0 | 2.0 | 842.6 | 0.014 |
| tcp-down | 1.0 | 0 | 3.99 | 550.0 | 0.028 |
| tcp-up | 2.0 | 0 | 2.0 | 809.1 | 0.011 |
| tcp-down | 2.0 | 0 | 2.0 | 507.3 | 0.023 |
| tcp-up | 5.0 | 0 | 2.0 | 1145.7 | 0.014 |
| tcp-down | 5.0 | 0 | 2.0 | 512.9 | 0.018 |
| tcp-up | 10.0 | 0 | 2.0 | 1205.7 | 0.012 |
| tcp-down | 10.0 | 0 | 2.0 | 466.3 | 0.014 |
