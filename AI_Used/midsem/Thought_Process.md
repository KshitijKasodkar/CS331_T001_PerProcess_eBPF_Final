# AI Thought Process & Workflow

> **Course:** CS331 - Computer Networks  
> **Project:** Per-Process Bandwidth Tracker using eBPF  

---

## 1. How We Worked with AI (Workflow Integration)

Instead of using AI just to generate disconnected snippets, we integrated Google Antigravity CLI (`agy`) directly into our development loop.

The collaboration followed a simple, practical cycle:
1. **We set the goal or pasted the error:** We gave the high-level task (e.g. "make a shell runner", "benchmark accuracy") or pasted the exact terminal output when something broke.
2. **AI inspected the code and reasoned through it:** Antigravity checked the files directly, examined structure definitions and compiler errors, and planned small, targeted edits.
3. **AI wrote or updated the files:** Rather than rewriting entire files, it used targeted tools (`replace_file_content`) to change only what was necessary.
4. **We ran it and gave feedback:** We executed the code in WSL2 or Windows, checked the behavior, and pasted the results back to the AI.

This human-in-the-loop setup prevented the AI from making wild assumptions or breaking working parts of the project.

---

## 2. Solving the "Zero Bytes" Bug (The Hardest Problem)

The toughest technical hurdle happened during Session 1. The tracker attached to the Linux kernel in WSL2 without crashing, but the table always showed **0.0 B/s upload and download**, even when we ran `curl` in another terminal.

Here is how the AI reasoned through and fixed it:

### Step 1: Don't guess, isolate
Instead of blindly rewriting the whole aggregator or storage code, the AI suggested writing a minimal, 40-line diagnostic script called `debug_bpf.py`. It had no database, no aggregator, and no fancy UI—just pure BCC probes printing to `stdout`.

### Step 2: Finding out that the kernel WAS working
When we ran `sudo python3 debug_bpf.py`, the terminal printed events immediately:
```text
PID      COMMAND     PROBE          BYTES
4526     curl        tcp_sendmsg    517
4526     curl        tcp_recv       2715
```
This was a huge breakthrough: it proved that `kprobe/tcp_sendmsg` and `kprobe/tcp_cleanup_rbuf` were working properly in the WSL2 kernel. The problem was entirely in how our main program handled the data.

### Step 3: Finding the actual causes
Comparing `debug_bpf.py` with `tracker.bpf.c` and `ebpf_loader.py` revealed two bugs:
1. **Socket Address Filtering:** The C probe had a strict check on socket address families (`AF_INET`). Modern `curl` and browsers on Linux often use dual-stack IPv4-mapped IPv6 sockets, causing the probe to drop the packet early.
2. **BCC Map Clearing Bug:** In Python, calling `.clear()` on a BCC `BPF_HASH` map doesn't work cleanly and causes silent exceptions. 

### Step 4: The fix
The AI:
- Loosened the C probe check so valid socket traffic wasn't dropped.
- Changed userspace tracking to keep an in-memory session map that calculates rate deltas between polling intervals.

**Result:** The moment we ran `./run.sh` again, the table lit up showing **17.8 MB/s download** from our live curl download.

---

## 3. Explaining Chrome's Multi-Process & 25+ IPs

When we tested Google Chrome inside WSL2, we noticed two surprising things:
1. Multiple tabs only showed up as one process (`Chrome_ChildIOT`).
2. A single website was connecting to 25+ different IP addresses.

We asked the AI if we could track bandwidth tab-by-tab. The AI explained why this happens at the operating system and browser level:
- **Browser Sandboxing:** Chrome tabs run in isolated "renderer" processes that are forbidden from opening raw network sockets for security reasons.
- **Centralized Network Service:** When a tab needs to fetch a page, it sends an internal IPC message to Chrome's dedicated network thread (`Chrome_ChildIOT`). Because this thread is the one calling `sys_sendto` and `sys_recvfrom`, eBPF kernel hooks see only that PID.
- **Why 25+ IPs?** Modern websites fetch images, fonts, analytics, and scripts from Content Delivery Networks (CDNs like Cloudflare, Google, Akamai), ad servers, and WebSockets.

Understanding this prevented us from wasting days trying to separate tabs in the kernel, which is impossible without modifying Chrome itself.

---

## 4. Making the Project Work on Both Linux and Windows

Our course requires eBPF (which only runs on Linux kernels), but team members and evaluators often work on Windows.

The AI solved this by creating two clean execution modes in `src/main.py`:
- **Live Linux Mode (`BCCBPFLoader`):** Runs on Ubuntu/WSL2, compiles C probes into the kernel via BCC, and tracks real hardware traffic.
- **Realistic Windows Mock Mode (`MockBPFLoader`):** Simulates 12 realistic applications (`chrome`, `spotify`, `discord`, `docker`, `slack`, `node`, `postgres`, `zoom`, `steam`, `dnsmasq`, etc.) with dynamic traffic bursts, real public IPs, and mixed TCP/UDP protocols.

This allowed us to develop the UI, SQLite database, web dashboard, and automated tests directly on Windows without needing a Linux kernel every single time.

---

## 5. Non-Blocking Background Daemon & Database Concurrency

Running the live tracker blocked the terminal, meaning we couldn't run queries or check historical bandwidth while the tracker was active.

The AI solved this in two steps:
1. **Background Daemon:** Added `./run.sh --daemon` and `./run.sh --stop`, saving the process ID in a `.pid` file.
2. **SQLite WAL Mode:** Standard SQLite locks the entire database file during writes. The AI enabled WAL mode (`PRAGMA journal_mode=WAL;`), which allows the web UI or CLI to query data at the same time the background daemon is saving new traffic records.
