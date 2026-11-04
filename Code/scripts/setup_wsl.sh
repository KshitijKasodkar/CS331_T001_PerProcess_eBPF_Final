#!/usr/bin/env bash
# One-time setup on WSL2 (Ubuntu/Debian) - fixes:
#   "modprobe: FATAL: Module kheaders not found ... Unable to find kernel headers"
#
# Microsoft does not ship a linux-headers package for the WSL2 kernel, and
# BCC needs headers to compile the eBPF program. This script downloads the
# source of EXACTLY your running WSL2 kernel from Microsoft's GitHub, prepares
# its headers (~3-6 minutes, ~1.5 GB temporary disk), and links them to
# /lib/modules/$(uname -r)/build where BCC looks for them.
#
#   sudo ./scripts/setup_wsl.sh
#
# Reminder: inside WSL2 eBPF sees only Linux processes running in WSL, never
# Windows programs. Use the Raspberry Pi for the real measurements.
set -euo pipefail
[ "$EUID" -eq 0 ] || { echo "run with sudo"; exit 1; }
cd "$(dirname "$0")/.."

KREL="$(uname -r)"                      # e.g. 5.15.167.4-microsoft-standard-WSL2
case "$KREL" in *microsoft*|*WSL*) ;; *) echo "This is not a WSL2 kernel ($KREL)."; exit 1 ;; esac
KVER="${KREL%%-*}"                      # 5.15.167.4
TAG="linux-msft-wsl-${KVER}"
SRC="/usr/src/wsl-kernel-${KVER}"

export DEBIAN_FRONTEND=noninteractive
# iperf3 asks whether to run as a background service at boot - answer "no" for us
echo "iperf3 iperf3/start_daemon boolean false" | debconf-set-selections
apt-get update
apt-get install -y bpfcc-tools python3-bpfcc iproute2 iperf3 sqlite3 \
    git build-essential flex bison bc libelf-dev libssl-dev dwarves python3 cpio kmod

if [ ! -e "${SRC}/include/generated/autoconf.h" ]; then
  echo "== Fetching WSL2 kernel source tag ${TAG}"
  rm -rf "${SRC}"
  git clone --depth 1 --branch "${TAG}" https://github.com/microsoft/WSL2-Linux-Kernel.git "${SRC}"
  cd "${SRC}"
  # Use the configuration of the kernel that is actually running.
  if [ -r /proc/config.gz ]; then
    zcat /proc/config.gz > .config
  else
    cp Microsoft/config-wsl .config
  fi
  make olddefconfig
  echo "== Preparing headers (make modules_prepare) - a few minutes"
  make -j"$(nproc)" modules_prepare
  cd - >/dev/null
fi

mkdir -p "/lib/modules/${KREL}"
ln -sfn "${SRC}" "/lib/modules/${KREL}/build"
ln -sfn "${SRC}" "/lib/modules/${KREL}/source"
mountpoint -q /sys/kernel/tracing || mount -t tracefs nodev /sys/kernel/tracing || true

echo
python3 -m bwtracker doctor --compile
