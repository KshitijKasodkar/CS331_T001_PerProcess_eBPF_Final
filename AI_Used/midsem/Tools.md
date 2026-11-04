# AI Tools Used

> **Course:** CS331 - Computer Networks  
> **Project:** Per-Process Bandwidth Tracker using eBPF  

---

## 1. The Main AI System

### Google Antigravity CLI (`agy`)
Instead of copying and pasting code from a browser chat window, we used **Google Antigravity CLI** (`agy`). 
- It runs right in the terminal inside our project workspace.
- It can read files, write code, run shell commands, and read terminal errors directly.
- It tracks its own history in session logs (`transcript.jsonl`), which is how we parsed this data.

### The Underlying Model: Gemini 3.7 Flash (High Reasoning)
- We used Google's Gemini 3.7 Flash with reasoning/thinking enabled.
- This was crucial for:
  - Writing restricted kernel C code that passes the eBPF kernel verifier.
  - Understanding low-level Linux kernel structures like `struct sock`.
  - Debugging compiler warnings and missing Linux headers in WSL2.

---

## 2. Agent Tools (How the AI Worked in Our Project)

Antigravity gives the AI specific tools to interact with the project. Here is what was used across the 2 sessions:

| Tool Name | Times Used | What the AI Used It For |
|---|:---:|---|
| **`view_file`** | 169 | Reading source code, checking logs, and reading instructions (like `nextSteps.md`). |
| **`run_command`** | 78 | Running tests (`pytest`), checking installed packages, and testing scripts. |
| **`write_to_file`** | 62 | Creating new Python files, C probes, test cases, and runner scripts. |
| **`replace_file_content`** | 56 | Making small, targeted code fixes (fixing bugs without rewriting whole files). |
| **`schedule` & `manage_task`** | 127 | Waiting for background tasks to finish (e.g., long tests or benchmarks) without freezing. |
| **`list_dir` & `find_by_name`** | 22 | Finding files and checking folder layouts. |
| **`read_url_content`** | 5 | Looking up documentation on BCC and Linux networking APIs. |
| **`grep_search`** | 1 | Searching for specific code symbols across the project. |
| **Total** | **520** | **Total tool actions executed by AI** |

---

## 3. Libraries & Technologies Set Up by the AI

The AI wrote code that integrated these core libraries:

1. **BCC (`python3-bpfcc` / `libbpfcc-dev`):**  
   Compiles our C probe code on the fly and attaches it to Linux kernel functions.
2. **Linux Kernel Probes (`kprobes`):**  
   - `kprobe/tcp_sendmsg`: Tracks outgoing TCP data (Upload).
   - `kprobe/tcp_cleanup_rbuf`: Tracks consumed incoming TCP data (Download).
   - `kprobe/udp_sendmsg`: Tracks outgoing UDP packets.
3. **Python 3 & `psutil`:**  
   Resolves PIDs into process names (like `curl` or `chrome`) and caches them so we don't slow down the system reading `/proc` every millisecond.
4. **SQLite (WAL Mode):**  
   Stores hourly summaries and snapshots. Using WAL (Write-Ahead Logging) mode allows the web UI and CLI to read from the database while the tracker is writing to it at the same time.
5. **Rich:**  
   Builds the terminal UI table with color-coded upload/download speeds.
6. **FastAPI & Uvicorn:**  
   Provides a web dashboard at `http://localhost:8080` with REST endpoints (`/api/live`, `/api/history`, etc.).
7. **Pytest:**  
   Automates all 25 tests, including accuracy checks and stress tests.
