# 🤖 AI Used

> **Course:** CS331 - Computer Networks · **Project:** T001 · Per-Process Bandwidth Tracker using eBPF (final version: **bwtrack**)

This folder contains the **raw, unedited conversations** with the AI tools used in this project: every project-related prompt we typed and the AI's reply, in order.

| File | AI tool | Period | Contents |
|---|---|---|---|
| [`Claude_conversation_2026-10-26_to_2026-11-04.md`](Claude_conversation_2026-10-26_to_2026-11-04.md) | Claude (Anthropic), model `claude-opus-5-5`, one long session | 26 Oct – 4 Nov 2026 | Final phase: review feedback, rebuild from scratch, WSL2 testing, Raspberry Pi 5 setup, packet-loss experiments, study & viva guide and nutshell, proof suite, final run on the Pi, presentation, report |
| [`midsem/`](midsem/) | Google Antigravity CLI (Gemini) | Sep 2026 | Mid-semester prototype |

## What the Claude conversation covers

| # | Date (2026) | Topic | What we asked | What the AI gave |
|:-:|---|---|---|---|
| 1 | 26 Oct | Project rebuild | Review feedback (mock only, "not real-time", 100 % accuracy from loopback); rebuild from scratch | Diagnosis of the midsem code; new eBPF tracker (`bwtracker/`), setup scripts, traffic generator, experiment suite, docs |
| 2 | 26 Oct | Running on the laptop (WSL2) | How to run it on Windows 11 WSL2; "is it correct?" with screenshots | WSL2 setup (kernel headers), live/web/terminal views; fixes for the chart clock-jump bug and file-permission errors |
| 3 | 26 Oct | Raspberry Pi 5 setup | Never used a Pi: cables, charger, SD card, Imager options, Wi-Fi/hotspot, SSH, copying code | Step-by-step setup; fix for the kernel 6.18 compile error (`compat.py`); tracker live on the Pi |
| 4 | 26 Oct | First packet-loss experiment | How to see accuracy; check the results | Pi ↔ laptop experiment at 0–10 % loss; explanation of app vs wire vs NIC, UDP loss, Mathis model |
| 5 | 26 Oct | Viva preparation | Detailed PDF on every concept, alternatives and basic questions; a nutshell version | 44-page study & viva guide, 4-page nutshell PDF; added how the experiment script works and how to use history/accuracy in the terminal |
| 6 | 29 Oct | README, report, team overview | Detailed README, short PDF for the team, report in the instructor's template, repo package | README, team overview PDF, report in the template |
| 7 | 29 Oct | Final presentation format | Instructor's 5-slide format; every claim needs proof; build from scratch | Proof suite (correctness, performance, scalability, reliability); found and fixed two tracker bugs; one-command final run |
| 8 | 30–31 Oct | Final run on the Pi | Step-by-step guidance during the run; what each part proves; which screenshots to take | Live checking of results; follow-up tests (50-process check, overhead check with pinned cores and real Wi-Fi) |
| 9 | 31 Oct | PPT and report with proof | New slides in the instructor's format; screenshots as proof; repo name; instructor's PowerPoint template | 12-slide deck (5 sections + proof slides) in the template; 10-page report with proof appendix |

**About the Claude log:** exported from claude.ai (Settings → Export data) and converted to Markdown with timestamps (IST). Screenshots and files we attached, and files the AI sent, are marked by name. The AI worked in its own cloud sandbox (test VMs, compiling, rendering documents); those internal commands and their output are not chat messages and are not included. It had no access to our laptop or Raspberry Pi: we ran every command there ourselves and sent screenshots back.

**Tested and checked by us:** we ran every step ourselves on our laptop (WSL2) and on the Raspberry Pi 5: setup, live tracking, the packet-loss experiments, the proof suite and the follow-up tests. All screenshots in the log are from our own runs, and each result was checked before we moved on. We also proofread the code documentation, the README, the report and the presentation.

**How we checked the AI's output:** every version was run on WSL2 and the Raspberry Pi 5; every number in the report and presentation comes from our own runs on the Pi and is compared with independent ground truth (byte counts printed by the workloads, UDP sequence numbers, kernel counters). The raw results are in `Code/experiments/pi_results/2026-10-30_final/`.
