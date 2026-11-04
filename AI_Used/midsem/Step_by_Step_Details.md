# Step-by-Step AI Contributions

> **Course:** CS331 - Computer Networks  
> **Project:** Per-Process Bandwidth Tracker using eBPF  

---

## Chronological Project Stages

Here is what was done at every stage of the project, and how the AI contributed:

---

### Stage 1: Planning & Project Setup
* **What we needed:** Turn the raw course assignment (`Project_Description.txt`) into a practical 9-day plan.
* **What AI did:**
  - Created `gemini.md` to hold the complete project context and architecture.
  - Created `plan.md`, breaking the project into small, independent subtasks so components could be tested individually before connecting them together.
  - Set up the folder structure (`src/bpf`, `src/core`, `src/storage`, `src/ui`, `tests`).

---

### Stage 2: Checking Dependencies
* **What we needed:** Verify that all required Python packages were installed and compatible.
* **What AI did:**
  - Wrote `check_dependencies.py` to check Python versions and package availability (`rich`, `psutil`, `fastapi`, `sqlite3`).
  - Followed our instruction to verify dependencies before writing code.

---

### Stage 3: Writing the Core Tracker
* **What we needed:** Implement the initial data pipeline from the Linux kernel to the terminal screen.
* **What AI did:**
  - **Kernel C code (`src/bpf/tracker.bpf.c`):** Wrote kprobe handlers for `tcp_sendmsg` (upload), `tcp_cleanup_rbuf` (download), and `udp_sendmsg` (UDP traffic).
  - **Loader (`src/core/ebpf_loader.py`):** Wrote the BCC loader to compile and inject probes into the Linux kernel, plus a mock loader for cross-platform testing.
  - **Process Cache (`src/core/process_cache.py`):** Wrote a resolver to map PIDs to process names (`curl`, `chrome`) using `/proc` and `psutil`, with an in-memory cache to save CPU time.
  - **Aggregator (`src/core/aggregator.py`):** Calculated bandwidth speeds (B/s, KB/s, MB/s) by taking the byte delta over elapsed time.
  - **Storage (`src/storage/db.py`):** Set up SQLite tables to store process records, snapshots, and hourly rollups.
  - **Terminal UI (`src/ui/cli.py`):** Created a live Rich table showing PID, Process, Rates, and Bytes.

---

### Stage 4: Running in WSL2 & Fixing Kernel Setup Errors
* **What we needed:** Get the code running inside our Linux WSL2 environment.
* **What AI did:**
  - Created `run.sh` so we could run everything with `./run.sh`.
  - Fixed `ModuleNotFoundError: No module named 'src'` when running with `sudo` by setting `PYTHONPATH`.
  - Provided the exact `apt` command to install BCC packages on Ubuntu (`bpfcc-tools`, `python3-bpfcc`, `libbpfcc-dev`).
  - Fixed WSL2 Clang header issues (macro redefinition warnings for `ntohs`/`ntohl` and missing forward declarations).

---

### Stage 5: Solving the "Zero Bytes" Bug
* **What we needed:** Fix the issue where the live tracker showed `0.0 B/s` even when network traffic was active.
* **What AI did:**
  - Wrote `debug_bpf.py`, a simple 40-line probe to test if the kernel was firing.
  - Found that probes *were* firing, but the main code dropped packets due to a strict `AF_INET` socket check in C and an issue calling `.clear()` on BCC hash maps in Python.
  - Refactored the probe and implemented an in-memory differential session map in Python.
  - **Result:** Successfully captured live network traffic (17.8 MB/s download from `curl`).

---

### Stage 6: Testing Real Browser Traffic (Chrome)
* **What we needed:** Verify how the tracker handles real desktop applications with multiple tabs.
* **What AI did:**
  - Helped install Google Chrome inside WSL2.
  - Monitored real browsing traffic and captured live data under `Chrome_ChildIOT`.
  - Explained why all tabs share one process (browser sandboxing + central Network Service) and why one webpage connects to 25+ remote IP addresses (CDNs, fonts, analytics).

---

### Stage 7: Automated Test Suite (25 Tests)
* **What we needed:** Harden the codebase and verify every component with automated tests.
* **What AI did:**
  - Wrote unit tests for rate math, protocol filters (TCP/UDP/ALL), process cache TTL, SQLite persistence, and UI rendering.
  - Wrote an end-to-end integration test (`test_e2e_integration.py`).
  - Achieved **25 out of 25 passing tests** under Pytest.

---

### Stage 8: Accuracy & Low-Overhead Benchmarks
* **What we needed:** Prove that our byte accounting is accurate and doesn't slow down the machine.
* **What AI did:**
  - **Accuracy Test (`tests/test_accuracy.py`):** Blasted exactly 10 MB TCP upload/download and 1 MB UDP packets through a test socket and compared them with the kernel probe counts. Result: **100.00% accuracy (0.00% error)**.
  - **Overhead Test (`tests/test_overhead.py`):** Ran sustained 800+ Mbps local traffic and measured CPU/memory usage with `psutil`. Result: **0.00% net CPU overhead** (beating our < 2% target).

---

### Stage 9: Interactive Demo Menu & Column Reordering
* **What we needed:** Make the project easy to demo in front of evaluators, and reorder table columns.
* **What AI did:**
  - Updated `run.sh` with an interactive 10-option menu (`--menu`, `--demo`, `--test`, `--accuracy`, `--overhead`, etc.).
  - Reordered the terminal table columns to: `PID` -> `Process Name` -> `Protocol` -> `Remote IP` -> `Upload Rate` -> `Download Rate`.

---

### Stage 10: Windows 12-Process Mock & Background Daemon
* **What we needed:** Run the demo on Windows with realistic processes, without blocking the terminal.
* **What AI did:**
  - Enhanced `MockBPFLoader` to simulate 12 distinct apps (`chrome`, `spotify`, `discord`, `docker`, `slack`, `node`, `postgres`, `zoom`, `steam`, `dnsmasq`) with realistic bandwidth bursts, real public IPs, and mixed TCP/UDP.
  - Added background daemon mode (`run.sh --daemon` and `run.sh --stop`) with PID tracking so other commands can run concurrently.
  - Enabled SQLite WAL mode (`PRAGMA journal_mode=WAL;`) to prevent database lock errors when reading and writing simultaneously.

---

### Stage 11: Web Dashboard & REST API
* **What we needed:** A web interface for people who prefer a browser over the terminal.
* **What AI did:**
  - Created `src/ui/web.py` using FastAPI and Uvicorn.
  - Built a clean, responsive dark-mode dashboard with live bandwidth cards, auto-refreshing tables, top-talker bar charts, and REST API endpoints.
  - Fixed the `0.0.0.0` URL issue by directing the browser to `http://localhost:8080`.

---

### Stage 12: Final Polish & Course Attribution
* **What we needed:** Clean up temporary files, author the final `README.md`, and add team member details.
* **What AI did:**
  - Nuked scratch and temporary markdown files and merged all documentation into `gemini.md`.
  - Wrote the complete `README.md` for GitHub.
  - Added official course details: CS331 Computer Networks, TA mentors (Jainish, Shubham), and all 5 team members with roll numbers and emails.
