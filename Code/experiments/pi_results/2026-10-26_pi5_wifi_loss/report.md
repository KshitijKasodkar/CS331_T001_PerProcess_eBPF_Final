# eBPF bandwidth tracker - accuracy and packet-loss experiments
Host `kasodkar`, kernel `6.18.50+rpt-rpi-2712`, Raspberry Pi 5 Model B Rev 1.1. Peer `10.58.105.150` via `wlan0`. Loss applied with `tc netem` (both), extra delay 0.0 ms. Started 2026-10-26 08:16:36.

## 1. TCP upload: socket layer vs wire
The app-layer counter (kprobe on `tcp_sendmsg` return) should match what the app sent exactly. The wire counter (`tcp_sock->bytes_sent`) is larger whenever segments are lost and resent.

| netem loss % | sendfile | app sent (truth) | eBPF app tx | app accuracy % | eBPF wire tx | retransmitted B | retx % of wire | retx segs | NIC tx B | goodput Mbit/s | sRTT ms | per-sample (0.5 s) app vs wire dev % (mean/max) | wire lags app by s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.0 | False | 10485779 | 10485779 | 100.0 | 10485760 | 0 | 0.0 | 0 | 10964511 | 156.208 | 24.0 | 88.75/98.59 | 0.0 |
| 1.0 | False | 10485779 | 10485779 | 100.0 | 10630560 | 144800 | 1.362 | 100 | 10964247 | 146.132 | 9.53 | 30.34/30.34 | 0.0 |
| 2.0 | False | 10485779 | 10485779 | 100.0 | 10878168 | 392408 | 3.607 | 271 | 10964379 | 124.618 | 12.08 | 47.37/55.77 | 0.0 |
| 5.0 | False | 10485779 | 10485779 | 100.0 | 11053376 | 567616 | 5.135 | 392 | 10970461 | 10.602 | 12.48 | 53.93/265.62 | 0.5 |
| 10.0 | False | 10485779 | 10485779 | 100.0 | 11846224 | 1360464 | 11.484 | 940 | 11083633 | 1.516 | 27.88 | 58.06/277.16 | 2.51 |

## 2. TCP download
| netem loss % | app received (truth) | eBPF app rx | accuracy % | peer retx % (if peer traced) | goodput Mbit/s |
|---|---|---|---|---|---|
| 0.0 | 10485760 | 10485760 | 100.0 |  | 156.894 |
| 1.0 | 10485760 | 10485760 | 100.0 |  | 15.266 |
| 2.0 | 10485760 | 10485760 | 100.0 |  | 13.543 |
| 5.0 | 10485760 | 10485760 | 100.0 |  | 8.871 |
| 10.0 | 10485760 | 10485760 | 100.0 |  | 6.048 |

## 3. UDP upload: the sender cannot see loss
| netem loss % | app sent (truth) | eBPF sender tx | accuracy % | true loss % (seq numbers) | eBPF measured loss % (both ends traced) | eBPF rcvbuf drops |
|---|---|---|---|---|---|---|
| 0.0 | 6250800 | 6250800 | 100.0 | 0.384 |  |  |
| 1.0 | 6250800 | 6250800 | 100.0 | 1.171 |  |  |
| 2.0 | 6250800 | 6250800 | 100.0 | 1.824 |  |  |
| 5.0 | 6250800 | 6250800 | 100.0 | 4.646 |  |  |
| 10.0 | 6250800 | 6250800 | 100.0 | 10.175 |  |  |

## 4. UDP download
| netem loss % | peer sent | app received (truth) | eBPF app rx | accuracy % | true loss % | eBPF measured loss % |
|---|---|---|---|---|---|---|
| 0.0 | 6249600 | 6249600 | 6249600 | 100.0 | 0.0 | 0.0 |
| 1.0 | 6249600 | 6171600 | 6171600 | 100.0 | 1.248 | 1.248 |
| 2.0 | 6249600 | 6121200 | 6121200 | 100.0 | 2.055 | 2.055 |
| 5.0 | 6249600 | 5910000 | 5910000 | 100.0 | 5.434 | 5.434 |
| 10.0 | 6250800 | 5602800 | 5602800 | 100.0 | 10.367 | 10.367 |

## 5. UDP receive-buffer overflow (host-side drops)
| sent pkts | app received pkts | kernel RcvbufErrors (truth) | eBPF drops | eBPF drop events | eBPF rx accuracy % |
|---|---|---|---|---|---|
| 3000 | 15 | 2985 | 2985 | 2985 | 100.0 |

## Tracker health during the runs
| mode | loss % | kretprobe misses | tracker CPU % (peak) | eBPF ns/run | eBPF CPU % |
|---|---|---|---|---|---|
| tcp-up | 0.0 | 0 | 1.99 | 267.4 | 0.039 |
| tcp-down | 0.0 | 0 | 0.0 | 102.0 | 0.186 |
| udp-up | 0.0 | 0 | 2.0 | 582.1 | 0.154 |
| udp-down | 0.0 | 0 | 2.0 | 323.9 | 0.064 |
| tcp-up | 1.0 | 0 | 3.99 | 296.6 | 0.051 |
| tcp-down | 1.0 | 0 | 2.0 | 380.1 | 0.077 |
| udp-up | 1.0 | 0 | 3.99 | 589.9 | 0.159 |
| udp-down | 1.0 | 0 | 2.0 | 447.7 | 0.099 |
| tcp-up | 2.0 | 0 | 2.0 | 381.5 | 0.052 |
| tcp-down | 2.0 | 0 | 2.0 | 422.6 | 0.082 |
| udp-up | 2.0 | 0 | 2.0 | 588.8 | 0.153 |
| udp-down | 2.0 | 0 | 2.0 | 360.7 | 0.064 |
| tcp-up | 5.0 | 0 | 3.99 | 612.8 | 0.024 |
| tcp-down | 5.0 | 0 | 2.0 | 546.2 | 0.056 |
| udp-up | 5.0 | 0 | 2.0 | 452.8 | 0.098 |
| udp-down | 5.0 | 0 | 2.0 | 361.8 | 0.063 |
| tcp-up | 10.0 | 0 | 2.0 | 1032.6 | 0.033 |
| tcp-down | 10.0 | 0 | 1.99 | 427.3 | 0.045 |
| udp-up | 10.0 | 0 | 2.0 | 647.0 | 0.103 |
| udp-down | 10.0 | 0 | 2.0 | 365.0 | 0.058 |
| udp-rcvbuf | 0 |  |  |  |  |
