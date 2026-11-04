#!/usr/bin/env bash
# One-time setup on Raspberry Pi OS (Bookworm / Trixie, 64-bit) for a Raspberry Pi 5.
#   sudo ./scripts/setup_pi.sh
# Installs BCC (compiles eBPF at runtime), the headers of the RUNNING kernel,
# iproute2 (tc/netem for loss experiments) and iperf3, then runs the doctor.
set -euo pipefail
[ "$EUID" -eq 0 ] || { echo "run with sudo"; exit 1; }
cd "$(dirname "$0")/.."

KREL="$(uname -r)"
echo "== Kernel: $KREL ($(cat /proc/device-tree/model 2>/dev/null | tr -d '\0'))"
if [ "$(getconf LONG_BIT)" != 64 ]; then
  echo "!! 32-bit userland detected. Use the 64-bit Raspberry Pi OS image."
fi

export DEBIAN_FRONTEND=noninteractive
# iperf3 asks whether to run as a background service at boot - answer "no" for us
echo "iperf3 iperf3/start_daemon boolean false" | debconf-set-selections
apt-get update
apt-get install -y bpfcc-tools python3-bpfcc iproute2 iperf3 sqlite3 kmod

# Headers must match the running kernel exactly. Pi 5 kernels are named *-rpi-2712.
if ! apt-get install -y "linux-headers-${KREL}"; then
  case "$KREL" in
    *2712*) apt-get install -y linux-headers-rpi-2712 ;;
    *v8*)   apt-get install -y linux-headers-rpi-v8 ;;
    *)      apt-get install -y raspberrypi-kernel-headers || true ;;
  esac
fi

if [ ! -e "/lib/modules/${KREL}/build" ]; then
  echo
  echo "!! Headers for ${KREL} are still missing."
  echo "   Your kernel was probably updated but not rebooted yet (headers exist for the NEW kernel)."
  echo "   Reboot, then run this script again:  sudo reboot"
  exit 1
fi

# tracefs is where kprobes are registered
mountpoint -q /sys/kernel/tracing || mount -t tracefs nodev /sys/kernel/tracing || true
# netem/ifb are modules on the Pi kernel (only needed for loss experiments)
modprobe sch_netem 2>/dev/null || true
modprobe ifb numifbs=0 2>/dev/null || true

echo
python3 -m bwtracker doctor --compile
