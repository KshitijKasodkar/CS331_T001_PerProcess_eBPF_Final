# eBPF bandwidth tracker - accuracy and packet-loss experiments
Host `kasodkar`, kernel `6.18.50+rpt-rpi-2712`, Raspberry Pi 5 Model B Rev 1.1. Peer `10.58.105.150` via `wlan0`. Loss applied with `tc netem` (both), extra delay 0.0 ms. Started 2026-10-30 23:52:12.

## 1. TCP upload: socket layer vs wire
The app-layer counter (kprobe on `tcp_sendmsg` return) should match what the app sent exactly. The wire counter (`tcp_sock->bytes_sent`) is larger whenever segments are lost and resent.

| netem loss % | sendfile | app sent (truth) | eBPF app tx | app accuracy % | eBPF wire tx | retransmitted B | retx % of wire | retx segs | NIC tx B | goodput Mbit/s | sRTT ms | per-sample (0.5 s) app vs wire dev % (mean/max) | wire lags app by s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.0 | False | 20971539 | 20971539 | 100.0 | 21529019 | 557480 | 2.589 | 385 | 22510975 | 160.96 | 19.66 | 41.21/100.0 | 0.5 |
| 1.0 | False | 20971539 | 20971539 | 100.0 | 21408835 | 437296 | 2.043 | 302 | 22140045 | 123.748 | 11.25 | 35.19/100.0 | 0.5 |
| 2.0 | False | 20971539 | 20971539 | 100.0 | 21401595 | 430056 | 2.009 | 297 | 21928283 | 91.79 | 7.93 | 36.64/100.0 | 0.5 |
| 5.0 | False | 20971539 | 20971539 | 100.0 | 22400515 | 1428976 | 6.379 | 987 | 21991673 | 11.29 | 9.33 | 28.9/100.0 | 2.0 |
| 10.0 | False | 20971539 | 20971539 | 100.0 | 23495403 | 2523864 | 10.742 | 1743 | 22086911 | 1.16 | 35.66 | 90.99/805.19 | 26.55 |

## 2. TCP download
| netem loss % | app received (truth) | eBPF app rx | accuracy % | peer retx % (if peer traced) | goodput Mbit/s |
|---|---|---|---|---|---|
| 0.0 | 20971520 | 20971520 | 100.0 |  | 110.606 |
| 1.0 | 20971520 | 20971520 | 100.0 |  | 15.161 |
| 2.0 | 20971520 | 20971520 | 100.0 |  | 12.801 |
| 5.0 | 20971520 | 20971520 | 100.0 |  | 8.695 |
| 10.0 | 20971520 | 20971520 | 100.0 |  | 6.041 |

## 3. UDP upload: the sender cannot see loss
| netem loss % | app sent (truth) | eBPF sender tx | accuracy % | true loss % (seq numbers) | eBPF measured loss % (both ends traced) | eBPF rcvbuf drops |
|---|---|---|---|---|---|---|
| 0.0 | 12500400 | 12500400 | 100.0 | 0.154 |  |  |
| 1.0 | 12500400 | 12500400 | 100.0 | 0.941 |  |  |
| 2.0 | 12500400 | 12500400 | 100.0 | 2.554 |  |  |
| 5.0 | 12500400 | 12500400 | 100.0 | 5.625 |  |  |
| 10.0 | 12500400 | 12500400 | 100.0 | 9.907 |  |  |

## 4. UDP download
| netem loss % | peer sent | app received (truth) | eBPF app rx | accuracy % | true loss % | eBPF measured loss % |
|---|---|---|---|---|---|---|
| 0.0 | 12500400 | 12475200 | 12475200 | 100.0 | 0.202 | 0.202 |
| 1.0 | 12500400 | 12363600 | 12363600 | 100.0 | 1.094 | 1.094 |
| 2.0 | 12500400 | 12236400 | 12236400 | 100.0 | 2.112 | 2.112 |
| 5.0 | 12500400 | 11828400 | 11828400 | 100.0 | 5.376 | 5.376 |
| 10.0 | 12499200 | 11206800 | 11206800 | 100.0 | 10.34 | 10.34 |

## 5. UDP receive-buffer overflow (host-side drops)
| sent pkts | app received pkts | kernel RcvbufErrors (truth) | eBPF drops | eBPF drop events | eBPF rx accuracy % |
|---|---|---|---|---|---|
| 3000 | 15 | 2985 | 2985 | 2985 | 100.0 |

## Tracker health during the runs
| mode | loss % | kretprobe misses | tracker CPU % (peak) | eBPF ns/run | eBPF CPU % |
|---|---|---|---|---|---|
| tcp-up | 0.0 | 0 | 2.0 | 293.5 | 0.043 |
| tcp-down | 0.0 | 0 | 2.0 | 161.2 | 0.232 |
| udp-up | 0.0 | 0 | 1.99 | 593.1 | 0.304 |
| udp-down | 0.0 | 0 | 2.0 | 583.3 | 0.199 |
| tcp-up | 1.0 | 0 | 2.0 | 541.3 | 0.063 |
| tcp-down | 1.0 | 0 | 2.0 | 359.6 | 0.085 |
| udp-up | 1.0 | 0 | 2.0 | 470.8 | 0.197 |
| udp-down | 1.0 | 0 | 2.0 | 439.5 | 0.122 |
| tcp-up | 2.0 | 0 | 2.0 | 468.5 | 0.054 |
| tcp-down | 2.0 | 0 | 2.0 | 471.7 | 0.087 |
| udp-up | 2.0 | 0 | 2.0 | 403.5 | 0.194 |
| udp-down | 2.0 | 0 | 2.0 | 474.8 | 0.122 |
| tcp-up | 5.0 | 0 | 2.0 | 1043.8 | 0.059 |
| tcp-down | 5.0 | 0 | 2.0 | 429.2 | 0.065 |
| udp-up | 5.0 | 0 | 2.0 | 592.4 | 0.307 |
| udp-down | 5.0 | 0 | 2.0 | 682.5 | 0.189 |
| tcp-up | 10.0 | 0 | 4.0 | 1155.0 | 0.05 |
| tcp-down | 10.0 | 0 | 2.0 | 427.6 | 0.056 |
| udp-up | 10.0 | 0 | 2.0 | 905.7 | 0.302 |
| udp-down | 10.0 | 0 | 2.0 | 498.9 | 0.111 |
| udp-rcvbuf | 0 |  |  |  |  |
