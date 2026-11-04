# Final evaluation on the Raspberry Pi 5 (30–31 Oct 2026)

Every number in `PPT.pdf` and `Report.pdf` comes from a file in this folder.

**Setup:** Raspberry Pi 5 (8 GB), Raspberry Pi OS Lite 64-bit, kernel `6.18.50+rpt-rpi-2712`, BCC 0.31.0, Python 3.13.5. The Pi (`10.58.105.107`) and a Windows 11 laptop running `trafficgen.py server` (`10.58.105.150`) were on the same phone hotspot over Wi-Fi.

**Command:** `sudo bash experiments/run_final.sh 10.58.105.150`, started 23:15. Then the two follow-ups listed below.

| Path | What it is | Used for |
|---|---|---|
| `run_final.log` | full console log of the run | everything |
| `doctor.txt` | environment check after the run | slide 2 |
| `proof_20261030-231542/summary.md` | proof suite: 14 pass, 1 fail (+2 measurements) | slides 3a, 4 |
| `proof_*/partial_sends.json` … `loopback_detection.json` | 9 correctness tests, each with truth vs tracker | slide 3a, report Table VI |
| `proof_*/throughput_overhead.json` | first overhead test (unpinned; loopback baseline jumped 26 ↔ 70 Gbit/s, so it was redone) | slide 5 (challenge) |
| `proof_*/ebpf_cost_all_tests.json` | per-program eBPF run time (`kernel.bpf_stats`), map memory, misses | slide 4 |
| `proof_*/concurrent_connections.json`, `concurrent_processes.json`, `event_rate.json` | scalability | slide 4 |
| `proof_*/soak.json`, `soak.db` | 30-min soak of `bwtrack record`: RSS/CPU samples and the SQLite history it wrote | slide 4, Fig. 6 |
| `proof_*/map_overflow_lru.json`, `start_mid_transfer.json` | reliability | slide 4 |
| `loss_run1..3/*/` | loss experiment 0/1/2/5/10 %, three repeats (`report.html`, `results.csv/json`, `series.csv`) | slide 3b |
| `loss_combined/combined.md` | mean ± sd of the three runs | slide 3b, report Tables IV–V |
| `delay20/*/` | same with +20 ms delay each way | slide 3b, Fig. 5 |
| `diag_processes_20261031-003010.json` | follow-up to the one FAIL: 5 × 50 processes, 250/250 exact (00:30) | slide 4 |
| `overhead_20261031-005043.json` | overhead redone: pinned cores, OFF-ON-OFF, plus real Wi-Fi (00:50) | slide 4, report Table VII |
| `charts/` | charts used in the PPT/report and `make_charts.py` to regenerate them | |

**Known gaps (reported, not hidden):**
- Loss run 3, 10 % TCP upload: our traffic generator timed out waiting for the final reply while TCP was backing off, so that point has n = 2. The timeout is now 300 s.
- `concurrent_processes`: 49/50 in the suite. It was not reproduced in 250 further tries (`diag_processes_*.json`).
