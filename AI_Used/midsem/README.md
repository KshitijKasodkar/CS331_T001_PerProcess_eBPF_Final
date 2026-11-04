# AI Usage Summary

> **Course:** CS331 - Computer Networks  
> **Project:** Per-Process Bandwidth Tracker using eBPF  
> **Primary AI Tool:** Google Antigravity CLI (`agy`) powered by Gemini  

---

## What is this folder?

This folder documents how we used AI to build, debug, and test our eBPF network tracker. 

Instead of just asking ChatGPT for code snippets, we used **Google Antigravity CLI (`agy`)**, an agentic AI coding tool that can read project files, write code, run terminal commands, and inspect errors directly in the workspace.

All the data in this folder was extracted directly from the actual session transcripts stored in folders `1` and `2`.

---

## Directory Overview

| File | What's Inside |
|---|---|
| [**`Tools.md`**](Tools.md) | The AI tools, models, and system libraries used throughout the project. |
| [**`Prompts.md`**](Prompts.md) | All 39 prompts given across the two sessions, organized by task. |
| [**`Thought_Process.md`**](Thought_Process.md) | How we collaborated with AI, how we solved tricky kernel bugs, and key decisions. |
| [**`Step_by_Step_Details.md`**](Step_by_Step_Details.md) | What was done at each step from start to finish. |

---

## Quick Numbers at a Glance

- **2 Development Sessions:** 
  - Session 1: Core eBPF probes, WSL2 setup, and solving live kernel capture.
  - Session 2: Benchmarking, test suite, Windows mock loader, web UI, and cleanup.
- **39 User Prompts:** 24 in Session 1, 15 in Session 2.
- **520 Tool Actions Executed by AI:** Reading files (169), writing files (62), editing files (56), running shell commands (78), background tasks & timers (155).
- **Core Results Achieved:**
  - Real eBPF kernel tracking working in WSL2/Linux.
  - 100% byte accuracy measured in controlled tests.
  - 25 out of 25 automated tests passing.
  - Realistic 12-process mock mode for testing on Windows without Linux.
