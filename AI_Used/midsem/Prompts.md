# Prompts Given to AI

> **Course:** CS331 - Computer Networks  
> **Project:** Per-Process Bandwidth Tracker using eBPF  

---

## Overview

We gave a total of **39 prompts** across two main development sessions:
- **Session 1 (24 prompts):** From the initial project plan to attaching real eBPF probes in WSL2, fixing kernel issues, and tracking live browser traffic.
- **Session 2 (15 prompts):** Auditing code, writing accuracy and overhead benchmarks, adding a 10-option interactive menu in `run.sh`, adding a 12-process Windows mock, building the web dashboard, and polishing the repo.

Below are the actual prompts given, organized by what we were working on.

---

## Session 1: Building the Core Tracker & Debugging in WSL2

### 1. Planning & Setup

#### Prompt 1.1 (Initial Blueprint)
> `@[Project_Description.txt] this is the project im gonna do , i need u to make a 2 md files one the gemini.md for context of the entire project and one plan.md for entire start to end plan okay , really detailed plan ( but lean and bs nt like typical big tech , like we need to do this in 9 days, be precise okay , ) this will refered by ai agents to work in parts , so break the tasks in subtasks and independet we connect them in last okay , and 1 test folder for testing all the individual peices and also the last big piece`  
* **What happened:** AI analyzed the project requirements and generated `gemini.md` (architecture) and `plan.md` (9-day roadmap broken into small testable pieces).

#### Prompt 1.2 (Checking Requirements)
> `k i had installed requirements check if all working or not ,`  
* **What happened:** AI wrote and ran a dependency check script.

#### Prompt 1.3 (Setting Boundaries)
> `k wait , listen do only what i asked for k , from now onwards tell me before u do , i had installed @[requirements.txt] and i want you to check all are installed in the right version , write a py or shell script and test it tht is ,`  
* **What happened:** We told the AI to slow down and ask/inform before acting. AI wrote a clean script to verify package versions.

#### Prompt 1.4 & 1.5 (Progress Check & Human Docs)
> `k great , and update me about the project so which day are we in according to @[plan.md] , just update`  
> `ohh u did all the work ? i thought we just did the plan , great , k do one thing write documentation for ppl not agents okay to understand the project , make a docs folder and write documentation about all the files and in future we update those files as we change files k ,`  
* **What happened:** AI reported current progress and wrote clear documentation for humans.

---

### 2. First Runs in WSL2 & Initial Fixes

#### Prompt 1.6 & 1.7 (Running in WSL)
> `k cool how do i see out result in wsl , what command to run`  
> `make a shell script i wil run it okay ,`  
* **What happened:** AI created `run.sh` to run the project with one simple bash command.

#### Prompt 1.8 (Import Error)
> `harsh@DESKTOP-BFJA8VE:/mnt/c/Users/Hanamanthagouda/Desktop/CN_Project1$ sudo ./run.sh`  
> `ModuleNotFoundError: No module named 'src'`  
* **What happened:** When running with `sudo`, Python didn't find the `src` folder. AI fixed the Python path in `run.sh` and `src/main.py`.

#### Prompt 1.9 & 1.10 (Adding Protocol & Remote IPs)
> `great it worked , ... k in this i need the process name okay , like tcp / udp or any other , do that change`  
> `k now i need ips for the process okay ,`  
* **What happened:** We tested the mock UI. It worked, but we asked AI to add the protocol and remote IP addresses to the display table.

#### Prompt 1.11 (Missing BCC in WSL)
> `Failed to load eBPF probes: BCC is not installed or not supported on this platform. Run on Linux with bpfcc-tools or use MockBPFLoader.`  
* **What happened:** AI gave the exact Ubuntu apt command to install BCC and kernel development packages.

#### Prompt 1.12, 1.13, 1.14 (WSL2 Kernel Compilation Errors)
> `E: Unable to locate package linux-headers-6.18.33.2-microsoft-standard-WSL2`  
> `Failed to load eBPF probes: name 'sys' is not defined`  
> `include/linux/bpf.h:391:10: error: invalid application of 'sizeof' to an incomplete type 'struct bpf_task_work'`  
> `warning: 'ntohs' macro redefined ... RuntimeError: BPF loader is not initialized.`  
* **What happened:** WSL2 has a custom Microsoft kernel without standard header packages. Clang was hitting header macro errors and forward declaration issues. AI cleaned up `tracker.bpf.c` to remove deep header includes and fixed import bugs.

---

### 3. The "Zero Bytes" Bug & Live Kernel Breakthrough

#### Prompt 1.15, 1.16, 1.17 (Zero Bytes Showing)
> `Total Upload: 0.0 B/s | Total Download: 0.0 B/s ... k its working but can see any process`  
> `nope its not showing , i did all 3 tests no luck ,`  
> `still no, ;(,`  
* **What happened:** The tracker attached to the kernel without crashing, but showed 0 B/s even when running `curl`. AI initially tried tweaking filters, but traffic was still not showing.

#### Prompt 1.18 (Writing Diagnostic Probe `debug_bpf.py`)
> `harsh@DESKTOP-BFJA8VE:/mnt/c/Users/Hanamanthagouda/Desktop/CN_Project1$ sudo python3 debug_bpf.py`  
> `[SUCCESS] Probes attached! Listening for TCP traffic...`  
> `4526 curl tcp_sendmsg 517`  
> `4526 curl tcp_recv 2715`  
* **What happened:** AI created a separate 40-line diagnostic script `debug_bpf.py`. Running it proved that **the kernel probes WERE firing**! The issue was inside our Python userspace map processing and socket family checks.

#### Prompt 1.19 (Fixing Map Logic)
> `still no luck its showing 0 total bytes`  
* **What happened:** AI discovered that calling `.clear()` on BCC hash maps fails in Python, and fixed how deltas are computed in memory.

#### Prompt 1.20 & 1.21 (Success: 17.8 MB/s Live Capture!)
> `k great its working , Total Upload: 963.0 B/s | Total Download: 17.8 MB/s | Filter: ALL`  
> `│ 4670 │ curl │ 799.0 B/s │ 17.8 MB/s │ 799.0 B │ 17.8 MB │ TCP+UDP │ 10.255.255.254 (+2) │`  
> `i think its working fine chcek once , ... i did one by one okay ,`  
* **What happened:** Real traffic was captured live from the kernel! AI confirmed the rates and retention were accurate.

---

### 4. Testing Google Chrome in WSL2

#### Prompt 1.22 & 1.23 (Installing Chrome)
> `k how can i run this for chrome tabs ?`  
> `sudo apt install -y ./google-chrome-stable_current_amd64.deb ... Error!`  
* **What happened:** AI helped fix deb package installation to get Google Chrome running inside WSL2.

#### Prompt 1.24 (Understanding Browser Traffic)
> `k it worked , but i have many doubts i opened multiple chrome tabs but it showed me one process and why its 25+ ip adress ? ... can we make it by tab by tab ?`  
* **What happened:** Chrome showed up as `Chrome_ChildIOT` with 25+ IP addresses. AI explained why: Chrome sends all network traffic through a single network process, and modern websites connect to dozens of CDNs, trackers, and ad servers.

---

## Session 2: Benchmarks, Polish, Windows Mock & Web UI

### 5. Auditing & Completing Hardening Tasks

#### Prompt 2.1 & 2.2 (Context Sync)
> `the project is ready , just read it for the context okay , i will tell u th enext steps, do nothing`  
> `update the gemini.md file till the current project achivement okay ,`  
* **What happened:** AI read the entire codebase and updated `gemini.md` with the verified WSL2 milestones.

#### Prompt 2.3 & 2.4 (Implementing `nextSteps.md`)
> `k great i have this file @[nextSteps.md] it has furter instructions complete them one by one , step by step okay`  
> `u did everything in @[nextSteps.md] file ? yes or no ?`  
* **What happened:** AI completed all tasks: 100% byte accuracy benchmark (`test_accuracy.py`), CPU overhead benchmark (`test_overhead.py`), stress test, and Pytest suite. AI provided a full verification checklist.

#### Prompt 2.5 (All-in-One Interactive Demo Runner)
> `so now how can i demonstrate all the stuff can u modify the run.sh for all a 2 z project demo ? can u do it ?`  
* **What happened:** AI upgraded `run.sh` to include a 10-option interactive menu, flags for benchmarks, demo mode, and daemon control.

---

### 6. Table Layout, Scope Check & Cleanup

#### Prompt 2.6 (Column Reordering)
> `k i need you to change the columns order , pid , process name , protocol , remote ip adr, upload rate and downlaod rate`  
* **What happened:** AI updated the Rich terminal table to match the exact requested column order.

#### Prompt 2.7 (Scope Check Against Rubric)
> `@Description: Build a real-time network usage tracker using eBPF... k how much we covered ? and explain this line , Real-time per-process + per-IP bandwidth monitoring , ? give me just the status no code change okay`  
* **What happened:** AI confirmed 100% coverage of the course rubric and explained the technical meaning of per-process and per-IP tracking.

#### Prompt 2.8 & 2.9 (Cleaning Repo)
> `k now lets start cleaning the repo okay strip off all unnessary junk files , okay we are giving it a finishing touch okay`  
> `update the gemini.md and nuke all the md files`  
* **What happened:** AI deleted scratch and temporary markdown files and consolidated everything into `gemini.md`.

---

### 7. Windows Support, Background Daemon & Web UI

#### Prompt 2.10 (Windows 10+ Processes & Web App)
> `k i want to live run this project , but im in windows , and i also see like 10 different process to get the idea of things running and the terminal is blocked once i run the epfb thing then i cant do this histroy , filterng and all uk so i need this to run in background and otherstuff like a db queering , maybe we can do a web ui and try this stuff okay , connect everything to web app okay , and also have a option of cli commands okay update run.sh script to include these 10+ process okay`  
* **What happened:** Big update:
  - Upgraded `MockBPFLoader` to simulate 12 distinct apps (`chrome`, `spotify`, `discord`, `docker`, `zoom`, `slack`, etc.) with dynamic traffic.
  - Added background daemon mode so the terminal isn't blocked.
  - Built the FastAPI web dashboard.

#### Prompt 2.11 & 2.12 (Fixing Web URL)
> `this site cant be reaced ... The webpage at http://0.0.0.0:8080/ might be temporarily down ... ERR_ADDRESS_INVALID`  
> `great its working!! now just update the gemini.md file of all the status okay ,`  
* **What happened:** Browsers cannot open `0.0.0.0`. AI explained that `0.0.0.0` is just a server bind address, and told us to use `http://localhost:8080`. It worked!

#### Prompt 2.13 & 2.14 (README & Pip Fix)
> `prep a readme.md file for this project im pushing this project to repo ,`  
> `[ERROR] uvicorn is required for web dashboard ... sudo apt python3-pip , how to resolve this isue ,`  
* **What happened:** AI created a full `README.md` and gave the commands to install `uvicorn` and `pip` in WSL.

#### Prompt 2.15 (Adding Team Credentials)
> `k its a course project cs331 computer networks okay and add these details in readme file , T001 4 Per-Process Bandwidth Tracker using eBPF kshitij.kasodkar@iitgn.ac.in Jainish Shubham 23110175 Kasodkar Kshitij Akash 23110126 Hanamanthagouda Policepatil 22110140 Manav Mangal Jain 23110338 Thipparapu Rushitha 24110278 Ralebhat Priyanka Shriram`  
* **What happened:** AI formatted and added the official course metadata and team table to `README.md` and `gemini.md`.

---

## Key Takeaways from Our Prompting

1. **Keep requests small and step-by-step:** Whenever we broke tasks down, the AI wrote cleaner code and didn't hallucinate.
2. **Paste terminal errors directly:** Pasting the exact compiler error or stack trace allowed the AI to fix issues in one shot.
3. **Give clear boundaries:** Telling the AI "tell me before u do" and "no code change" kept the project on track and prevented unwanted refactoring.
