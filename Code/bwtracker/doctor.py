"""`bwtrack doctor` - checks everything the tracker needs and prints the exact fix."""
from __future__ import annotations

import glob
import gzip
import os
import shutil
import subprocess
import sys

from . import netutil

OK, WARN, FAIL = "\033[32m OK \033[0m", "\033[33mWARN\033[0m", "\033[31mFAIL\033[0m"
REQUIRED_FUNCS = ["tcp_sendmsg", "tcp_cleanup_rbuf", "udp_sendmsg", "skb_consume_udp"]
OPTIONAL_FUNCS = ["tcp_rcv_established", "udpv6_sendmsg", "__udp_enqueue_schedule_skb", "tcp_sendpage"]


def kernel_config() -> dict:
    rel = os.uname().release
    text = ""
    for path in (f"/boot/config-{rel}", "/proc/config.gz"):
        try:
            if path.endswith(".gz"):
                if not os.path.exists(path):
                    subprocess.run(["modprobe", "configs"], capture_output=True)
                with gzip.open(path, "rt") as f:
                    text = f.read()
            else:
                with open(path) as f:
                    text = f.read()
            break
        except OSError:
            continue
    cfg = {}
    for line in text.splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k] = v
    return cfg


def tracefs() -> str:
    for p in ("/sys/kernel/tracing", "/sys/kernel/debug/tracing"):
        if os.path.exists(os.path.join(p, "events")):
            return p
    if os.geteuid() == 0:
        subprocess.run(["mount", "-t", "tracefs", "nodev", "/sys/kernel/tracing"], capture_output=True)
        if os.path.exists("/sys/kernel/tracing/events"):
            return "/sys/kernel/tracing"
    return ""


def fix_cmd(platform: str) -> str:
    if platform == "wsl2":
        return "sudo ./scripts/setup_wsl.sh      (builds headers for the WSL2 kernel, installs BCC)"
    if "raspberry" in platform.lower():
        return "sudo ./scripts/setup_pi.sh       (apt: python3-bpfcc bpfcc-tools linux-headers-rpi-2712)"
    return "sudo apt install -y python3-bpfcc bpfcc-tools linux-headers-$(uname -r)"


def run(args) -> int:
    plat = netutil.detect_platform()
    rel = os.uname().release
    problems = 0
    print(f"Platform: {plat}   kernel {rel}   arch {os.uname().machine}   python {sys.version.split()[0]}\n")

    def check(ok, label, detail="", fix="", warn=False):
        nonlocal problems
        tag = OK if ok else (WARN if warn else FAIL)
        print(f"[{tag}] {label}" + (f"  - {detail}" if detail else ""))
        if not ok and fix:
            print(f"        fix: {fix}")
        if not ok and not warn:
            problems += 1

    check(sys.platform.startswith("linux"), "Running on Linux", fix="eBPF only exists in the Linux kernel")
    check(os.geteuid() == 0, "Running as root", fix="sudo ./bwtrack doctor")

    cfg = kernel_config()
    if cfg:
        for opt, why in (("CONFIG_BPF_SYSCALL", "eBPF itself"), ("CONFIG_KPROBES", "kprobes"),
                         ("CONFIG_BPF_EVENTS", "attaching eBPF to kprobes/tracepoints")):
            check(cfg.get(opt) == "y", f"{opt}=y", why,
                  fix="this kernel cannot run the tracker; use a kernel built with this option")
        check(cfg.get("CONFIG_NET_SCH_NETEM") in ("y", "m"), "CONFIG_NET_SCH_NETEM", "packet-loss experiments",
              warn=True, fix="loss experiments need netem; live tracking still works")
    else:
        check(False, "Kernel config readable", "could not read /boot/config-* or /proc/config.gz", warn=True)

    tf = tracefs()
    check(bool(tf), "tracefs mounted", tf, fix="sudo mount -t tracefs nodev /sys/kernel/tracing")
    if tf:
        check(os.path.exists(os.path.join(tf, "kprobe_events")), "kprobe_events available",
              fix="kernel built without CONFIG_KPROBE_EVENTS")
        for tp in ("tcp/tcp_retransmit_skb", "sock/inet_sock_set_state"):
            check(os.path.exists(os.path.join(tf, "events", tp)), f"tracepoint {tp}")

    try:
        with open("/proc/kallsyms") as f:
            syms = {line.split()[2] for line in f if len(line.split()) >= 3}
    except OSError:
        syms = set()
    for fn in REQUIRED_FUNCS:
        check(fn in syms, f"kernel function {fn}", "required")
    for fn in OPTIONAL_FUNCS:
        if fn == "tcp_sendpage" and fn not in syms:
            print(f"[{OK}] kernel function tcp_sendpage  - absent (normal on kernels >= 6.5)")
            continue
        check(fn in syms, f"kernel function {fn}", "optional", warn=True,
              fix="modprobe ipv6" if fn == "udpv6_sendmsg" else "")

    hdr = f"/lib/modules/{rel}/build"
    has_hdr = os.path.exists(os.path.join(hdr, "include", "linux", "kconfig.h")) or \
        os.path.exists(os.path.join(hdr, "include", "generated", "autoconf.h"))
    kheaders = os.path.exists("/sys/kernel/kheaders.tar.xz")
    check(has_hdr or kheaders, "Kernel headers for the running kernel",
          hdr if has_hdr else ("via /sys/kernel/kheaders.tar.xz" if kheaders else "missing " + hdr),
          fix=fix_cmd(plat))

    bcc_ok, bcc_ver = False, ""
    for p in ("/usr/lib/python3/dist-packages",):
        if p not in sys.path:
            sys.path.append(p)
    try:
        import bcc
        bcc_ok, bcc_ver = True, getattr(bcc, "__version__", "?")
    except Exception as e:
        bcc_ver = str(e)
    check(bcc_ok, "BCC python module", bcc_ver, fix=fix_cmd(plat))

    for tool, why in (("tc", "netem loss experiments"), ("iperf3", "optional benchmark peer")):
        check(shutil.which(tool) is not None, f"{tool} installed", why, warn=True,
              fix=f"sudo apt install -y {'iproute2' if tool == 'tc' else tool}")

    if plat == "wsl2":
        print("\nNote (WSL2): the tracker sees only processes running INSIDE the WSL Linux VM. "
              "Windows programs (Chrome, Steam, Discord...) use the Windows network stack and are "
              "invisible to Linux eBPF. Use the Raspberry Pi for real measurements.")

    if args.compile and problems == 0 and os.geteuid() == 0 and bcc_ok:
        print("\nTrial compile + attach of the real eBPF program...")
        from .collector import KernelProbes, TrackerError
        kp = KernelProbes()
        try:
            kp.load(log=lambda s: print("   ", s))
            for k, fn in kp.attached:
                print(f"    attached {k:<10} {fn}")
            for k, fn, why in kp.skipped:
                print(f"    skipped  {k:<10} {fn}: {why}")
            print(f"[{OK}] eBPF program loads and attaches")
        except TrackerError as e:
            problems += 1
            print(f"[{FAIL}] {e}")
        finally:
            kp.close()

    print("\n" + ("All checks passed. Start with:  sudo ./bwtrack live" if problems == 0 else
                  f"{problems} problem(s) found - apply the fixes above."))
    return 0 if problems == 0 else 1
