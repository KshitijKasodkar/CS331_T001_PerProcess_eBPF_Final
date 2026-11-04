# eBPF bandwidth tracker - accuracy and packet-loss experiments
Host `kasodkar`, kernel `6.18.50+rpt-rpi-2712`, Raspberry Pi 5 Model B Rev 1.1. Peer `10.58.105.150` via `wlan0`. Loss applied with `tc netem` (both), extra delay 0.0 ms. Started 2026-10-31 00:02:34.

## 1. TCP upload: socket layer vs wire
The app-layer counter (kprobe on `tcp_sendmsg` return) should match what the app sent exactly. The wire counter (`tcp_sock->bytes_sent`) is larger whenever segments are lost and resent.

| netem loss % | sendfile | app sent (truth) | eBPF app tx | app accuracy % | eBPF wire tx | retransmitted B | retx % of wire | retx segs | NIC tx B | goodput Mbit/s | sRTT ms | per-sample (0.5 s) app vs wire dev % (mean/max) | wire lags app by s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.0 | False | 20971539 | 20971539 | 100.0 | 20971539 | 0 | 0.0 | 0 | 21928085 | 187.806 | 25.98 | 21.45/22.29 | 0.0 |
| 1.0 | False | 20971539 | 20971539 | 100.0 | 21181515 | 209976 | 0.991 | 146 | 21928167 | 165.633 | 18.57 | 38.82/75.36 | 0.0 |
| 2.0 | False | 20971539 | 20971539 | 100.0 | 21268379 | 296840 | 1.396 | 205 | 21928085 | 162.297 | 12.77 | 19.63/33.84 | 0.0 |
| 5.0 | False | 20971539 | 20971539 | 100.0 | 22226955 | 1255416 | 5.648 | 867 | 21946531 | 10.04 | 8.46 | 35.38/491.73 | 1.0 |

## 2. TCP download
| netem loss % | app received (truth) | eBPF app rx | accuracy % | peer retx % (if peer traced) | goodput Mbit/s |
|---|---|---|---|---|---|
| 0.0 | 20971520 | 20971520 | 100.0 |  | 186.827 |
| 1.0 | 20971520 | 20971520 | 100.0 |  | 15.359 |
| 2.0 | 20971520 | 20971520 | 100.0 |  | 14.445 |
| 5.0 | 20971520 | 20971520 | 100.0 |  | 11.327 |
| 10.0 | 20971520 | 20971520 | 100.0 |  | 6.358 |

## 3. UDP upload: the sender cannot see loss
| netem loss % | app sent (truth) | eBPF sender tx | accuracy % | true loss % (seq numbers) | eBPF measured loss % (both ends traced) | eBPF rcvbuf drops |
|---|---|---|---|---|---|---|
| 0.0 | 12500400 | 12500400 | 100.0 | 0.0 |  |  |
| 1.0 | 12500400 | 12500400 | 100.0 | 0.902 |  |  |
| 2.0 | 12500400 | 12500400 | 100.0 | 1.795 |  |  |
| 5.0 | 12500400 | 12500400 | 100.0 | 4.944 |  |  |
| 10.0 | 12500400 | 12500400 | 100.0 | 9.916 |  |  |

## 4. UDP download
| netem loss % | peer sent | app received (truth) | eBPF app rx | accuracy % | true loss % | eBPF measured loss % |
|---|---|---|---|---|---|---|
| 0.0 | 12499200 | 12442800 | 12442800 | 100.0 | 0.451 | 0.451 |
| 1.0 | 12500400 | 12374400 | 12374400 | 100.0 | 1.008 | 1.008 |
| 2.0 | 12499200 | 12252000 | 12252000 | 100.0 | 1.978 | 1.978 |
| 5.0 | 12500400 | 11875200 | 11875200 | 100.0 | 5.001 | 5.001 |
| 10.0 | 12499200 | 11239200 | 11239200 | 100.0 | 10.081 | 10.081 |

## Tracker health during the runs
| mode | loss % | kretprobe misses | tracker CPU % (peak) | eBPF ns/run | eBPF CPU % |
|---|---|---|---|---|---|
| tcp-up | 0.0 | 0 | 2.0 | 194.4 | 0.045 |
| tcp-down | 0.0 | 0 | 0.0 | 888.3 | 0.264 |
| udp-up | 0.0 | 0 | 2.0 | 486.3 | 0.198 |
| udp-down | 0.0 | 0 | 2.0 | 895.7 | 0.199 |
| tcp-up | 1.0 | 0 | 2.0 | 417.0 | 0.059 |
| tcp-down | 1.0 | 0 | 2.0 | 558.7 | 0.091 |
| udp-up | 1.0 | 0 | 2.0 | 404.6 | 0.196 |
| udp-down | 1.0 | 0 | 2.0 | 484.8 | 0.124 |
| tcp-up | 2.0 | 0 | 2.0 | 297.6 | 0.045 |
| tcp-down | 2.0 | 0 | 2.0 | 531.1 | 0.106 |
| udp-up | 2.0 | 0 | 2.0 | 397.0 | 0.192 |
| udp-down | 2.0 | 0 | 2.0 | 594.3 | 0.2 |
| tcp-up | 5.0 | 0 | 2.0 | 618.2 | 0.026 |
| tcp-down | 5.0 | 0 | 2.0 | 435.6 | 0.07 |
| udp-up | 5.0 | 0 | 2.0 | 480.1 | 0.201 |
| udp-down | 5.0 | 0 | 2.0 | 503.3 | 0.119 |
| tcp-down | 10.0 | 0 | 2.0 | 428.1 | 0.058 |
| udp-up | 10.0 | 0 | 2.0 | 407.3 | 0.195 |
| udp-down | 10.0 | 0 | 2.0 | 577.9 | 0.189 |
