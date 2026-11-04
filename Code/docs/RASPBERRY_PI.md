# Running on the Raspberry Pi 5 (step by step)

The Pi is the real target: a real Linux kernel on real hardware with a real
network link. WSL2 is fine for development, but it only sees Linux processes
inside WSL and has no `netem`, so the measurements and loss experiments belong
on the Pi.

## 1. Prepare the Pi

1. Flash **Raspberry Pi OS (64-bit)** (Bookworm or Trixie) with Raspberry Pi
   Imager. In the imager settings, enable SSH and set a user name and password.
2. Connect the Pi to the **same network as your laptop**. Ethernet gives the
   cleanest experiments; Wi-Fi shows natural packet loss, which is interesting
   too.
3. From the laptop (PowerShell or WSL): `ssh <user>@raspberrypi.local`
4. Update and reboot once, so the kernel and its headers match:
   ```bash
   sudo apt update && sudo apt full-upgrade -y && sudo reboot
   ```

## 2. Copy the project and install

From WSL on the laptop (the `Code` folder of this project):
```bash
scp -r Code <user>@raspberrypi.local:~/bwtracker
```
On the Pi:
```bash
cd ~/bwtracker
sudo ./scripts/setup_pi.sh        # BCC + headers for the running kernel + tc + iperf3, then doctor
```
The script ends with `doctor --compile`, which compiles and attaches the eBPF
program once. Every line should be `OK`. If it says the headers are missing,
reboot and run the script again: the kernel was upgraded but is not running yet.

## 3. Live tracking

```bash
sudo ./bwtrack live                     # terminal dashboard (q to quit)
sudo ./bwtrack web --port 8080          # web dashboard + /metrics + history
```
Open `http://raspberrypi.local:8080` in the laptop's browser.

Make some traffic on the Pi in a second SSH window:
```bash
curl -o /dev/null https://speed.cloudflare.com/__down?bytes=200000000      # download
curl -X POST --data-binary @/dev/zero --max-time 15 https://speed.cloudflare.com/__up   # upload
sudo apt update                                                             # several processes
python3 tools/trafficgen.py client <laptop-ip> --mode tcp-up --bytes 200M   # see section 4
```
Note that your SSH session and the web dashboard show up too (`sshd`,
`python3`). They are real traffic.

Useful keys in `live`: `f` per-remote-IP view, `t` TCP/UDP filter, `s` sort,
`l` show loopback, `e` events.

## 4. Experiments with real packet loss (what the professor asked for)

### 4a. Start the peer on the laptop

The peer is `tools/trafficgen.py`. It only needs Python, so **Windows Python
works** (WSL2 is behind NAT and the Pi cannot reach it without extra setup).

```powershell
# Windows PowerShell, in the Code\tools folder
python trafficgen.py server --port 5300
```
When Windows Firewall asks, allow Python on **private networks**. Find the
laptop IP with `ipconfig` (Wi-Fi or Ethernet adapter, e.g. 192.168.1.20).

### 4b. Run the experiment suite on the Pi

Use `tmux` so a dropped SSH session does not stop the run:
```bash
sudo apt install -y tmux && tmux
cd ~/bwtracker
sudo python3 experiments/run_experiments.py --peer 192.168.1.20 --loss 0,1,2,5,10 --delay 10
```
For each loss level, this:
- applies `tc netem loss X%` to the Pi's interface in both directions (an IFB device handles ingress),
- moves 20 MB up and down over TCP, and streams UDP at 20 Mbit/s up and down,
- compares eBPF counters with what both apps counted (ground truth) and with
  the NIC counters,
- removes netem again, even if you press Ctrl+C.

Results are written to `experiments/results/<time>/report.html` (tables + charts),
plus `report.md`, `results.csv` and `series.csv`. Copy them to the laptop:
```bash
scp -r <user>@raspberrypi.local:~/bwtracker/experiments/results .
```

> **SSH tip:** netem also delays and drops your SSH packets on that interface.
> Up to 10% loss SSH keeps working (slowly). For larger values, connect over
> Wi-Fi and run the experiment on Ethernet: `--iface eth0`.
> Emergency cleanup: `sudo tc qdisc del dev eth0 root; sudo tc qdisc del dev eth0 ingress`

### 4c. Ideas for extra results

| experiment | command | what to observe |
|---|---|---|
| Ethernet vs Wi-Fi, no artificial loss | `--loss 0` once per interface | natural retransmissions on Wi-Fi |
| Delay changes TCP's sensitivity to loss | `--delay 50 --loss 0,1,2,5` | goodput falls faster (Mathis: proportional to 1/RTT) |
| Host drops, not network drops | `--peer local --rcvbuf-test --modes tcp-up --loss 0` | UDP receive-buffer drops match the kernel's `RcvbufErrors` |
| No second computer | `--peer local --loss 0,1,5,10` | same tables over loopback |

## 5. Keep recording history (optional)

```bash
sudo cp scripts/bwtrack.service /etc/systemd/system/
sudo sed -i "s#/home/pi/bwtracker#$PWD#" /etc/systemd/system/bwtrack.service
sudo systemctl daemon-reload && sudo systemctl enable --now bwtrack
./bwtrack report --since 24h                    # top programs
./bwtrack report --by remote --since 24h        # top remote IPs
./bwtrack report --timeline --bucket 300 --since 24h
./bwtrack report --by process-remote --csv usage.csv
```
The web UI's History tab reads the same database.

## 6. Prometheus / Grafana (optional)

`/metrics` on the web port is standard Prometheus text format:
```yaml
scrape_configs:
  - job_name: bwtrack
    scrape_interval: 5s
    static_configs: [{ targets: ['raspberrypi.local:8080'] }]
```
Example query: `sum by (comm) (rate(bwtracker_bytes_total{direction="tx"}[1m]))`.

## Troubleshooting

| symptom | fix |
|---|---|
| `Unable to find kernel headers` | `sudo ./scripts/setup_pi.sh`. If it persists: `sudo reboot` and run it again. |
| `Failed to attach kprobe` / `kprobe_events: No such file` | `sudo mount -t tracefs nodev /sys/kernel/tracing`. If `CONFIG_KPROBES` is missing, the kernel is not a stock Raspberry Pi kernel. |
| compile takes long | first start takes about 5-15 s on a Pi 5 (clang compiles the program). |
| nothing shown for `ping` | ICMP doesn't go through TCP/UDP sockets. This is expected (see CONCEPTS.md). |
| experiment: `cannot reach trafficgen server` | the server isn't running on the laptop, or the Windows firewall blocked it. |
