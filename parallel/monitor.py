#!/usr/bin/env python3
"""
Live dashboard that reads status.json and pipeline logs to print periodic updates.

Usage:
  cd prompt-optimization-solution
  ./scripts/python/venv/bin/python3 parallel/monitor.py [--interval 30]

Reads:
  parallel/status.json       — supervisor's aggregated state
  parallel/P*_output.log     — per-pipeline log tails for recent activity
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
STATUS_FILE = SCRIPT_DIR / "status.json"

PIPELINE_LABELS = {
    "P1": "Simple Instructions",
    "P3": "Control (Full Config)",
    "P4": "Simplify-Every-Other",
    "P7": "Trend-Aware",
    "P10": "Gold Standard Start",
}

STATUS_ICONS = {
    "running": "🟢",
    "completed": "✅",
    "failed": "❌",
    "killed": "💀",
    "queued": "⏳",
}

SCORE_BAR_WIDTH = 20


def read_status():
    if not STATUS_FILE.exists():
        return None
    try:
        return json.loads(STATUS_FILE.read_text())
    except Exception:
        return None


def read_log_tail(log_path, bytes_from_end=6000):
    """Read the last N bytes of a log file and extract recent activity."""
    if not log_path or not Path(log_path).exists():
        return None
    try:
        with open(log_path, "r") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - bytes_from_end))
            return f.read()
    except Exception:
        return None


def extract_activity(tail):
    """Pull the latest meaningful activity line from log tail."""
    if not tail:
        return "no log yet"

    patterns = [
        (r"Creating Search Index: (.+)", lambda m: f"creating index {m.group(1)}"),
        (r"Polling index until Ready", lambda _: "waiting for index to build..."),
        (r"Status: (IN_PROGRESS|SUBMITTED|READY).*attempt (\d+)", lambda m: f"index {m.group(1).lower()} (poll #{m.group(2)})"),
        (r"Creating Retriever", lambda _: "creating retriever..."),
        (r"Polling retriever.*attempt (\d+)", lambda m: f"waiting for retriever (poll #{m.group(1)})"),
        (r"Invoking prompt.*Q(\d+)", lambda m: f"evaluating Q{m.group(1)}/15"),
        (r"STEP 2.*Testing.*Cycle (\d+)", lambda m: f"testing index (cycle {m.group(1)})"),
        (r"STEP 3.*Gemini", lambda _: "Gemini analyzing results..."),
        (r"STEP 1.*Create Index.*Cycle (\d+)", lambda m: f"creating index (cycle {m.group(1)})"),
        (r"REFINEMENT CYCLE (\d+)", lambda m: f"starting cycle {m.group(1)}"),
        (r"Composite Score: (\d+)", lambda m: f"scored {m.group(1)} composite"),
        (r"index-naming.*→ (.+)", lambda m: f"resolved name → {m.group(1)}"),
        (r"Save complete", lambda _: "index saved, looking up ID..."),
        (r"Acquired lock", lambda _: "acquired template lock"),
        (r"STEP 1: SKIPPED", lambda _: "cycle 1 — testing baseline"),
    ]

    lines = tail.strip().split("\n")
    for line in reversed(lines):
        for pattern, formatter in patterns:
            m = re.search(pattern, line)
            if m:
                return formatter(m)

    return "active"


def score_bar(score, max_score=30):
    filled = int((score / max_score) * SCORE_BAR_WIDTH) if max_score > 0 else 0
    filled = min(filled, SCORE_BAR_WIDTH)
    return "█" * filled + "░" * (SCORE_BAR_WIDTH - filled)


def format_duration(seconds):
    if seconds is None or seconds < 0:
        return "--:--"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    if h > 0:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def render(status, cycle_num):
    os.system("clear" if os.name != "nt" else "cls")

    now = datetime.now()
    updated = status.get("last_updated", "?")
    wall = status.get("wall_clock_elapsed", "?")

    running = status.get("running", 0)
    completed = status.get("completed", 0)
    failed = status.get("failed", 0)
    queued = status.get("queued", 0)
    total = status.get("pipelines_total", 5)

    best = status.get("best_result_so_far")
    best_str = f"{best['pipeline']} @ {best['composite_score']}" if best else "n/a"

    print("=" * 72)
    print(f"  PARALLEL RAG OPTIMIZATION — LIVE MONITOR  (poll #{cycle_num})")
    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S')}  |  wall: {wall}  |  best: {best_str}")
    print(f"  🟢 {running} running  ✅ {completed} done  ❌ {failed} fail  ⏳ {queued} queued  ({total} total)")
    print("=" * 72)
    print()

    pipelines = status.get("pipelines", {})

    # Header
    print(f"  {'Pipeline':<8} {'Status':<4} {'Cyc':>3}  {'Best':>4}  {'Score Bar':<{SCORE_BAR_WIDTH+2}}  {'Activity'}")
    print(f"  {'─'*8} {'─'*4} {'─'*3}  {'─'*4}  {'─'*(SCORE_BAR_WIDTH+2)}  {'─'*30}")

    for pid in ["P1", "P3", "P4", "P7", "P10"]:
        p = pipelines.get(pid, {})
        icon = STATUS_ICONS.get(p.get("status", "queued"), "?")
        cycles = p.get("cycles_completed", 0)
        best_comp = p.get("best_composite", 0)
        bar = score_bar(best_comp)
        restarts = p.get("restarts", 0)
        restart_tag = f" (R{restarts})" if restarts > 0 else ""

        log_file = p.get("log_file")
        tail = read_log_tail(log_file)
        activity = extract_activity(tail) if p.get("status") == "running" else p.get("status", "?")

        elapsed = ""
        if p.get("start_time"):
            try:
                st = datetime.fromisoformat(p["start_time"])
                elapsed = format_duration((now - st).total_seconds())
            except Exception:
                pass

        label = PIPELINE_LABELS.get(pid, pid)
        name_col = f"{pid} {label}"
        if len(name_col) > 28:
            name_col = name_col[:28]

        print(f"  {pid:<3} {icon}   C{cycles:<2} {best_comp:>4}  {bar}  {activity}{restart_tag}")

    print()

    # Recent events
    events = status.get("event_log_tail", [])
    if events:
        recent = events[-6:]
        print("  Recent events:")
        for ev in recent:
            print(f"    {ev}")
        print()

    # Per-pipeline detail (only for running)
    running_detail = [(pid, p) for pid, p in pipelines.items() if p.get("status") == "running"]
    if running_detail:
        print("  ── Running pipeline details ──")
        for pid, p in running_detail:
            log_file = p.get("log_file")
            tail = read_log_tail(log_file, bytes_from_end=2000)
            if not tail:
                continue

            scores = re.findall(r"Composite Score: (\d+)", tail)
            score_history = ", ".join(scores[-5:]) if scores else "n/a"

            cycle_matches = re.findall(r"REFINEMENT CYCLE (\d+)", tail)
            current_cycle = max((int(c) for c in cycle_matches), default=0)

            pass_matches = re.findall(r"(\d+) PASS", tail)
            partial_matches = re.findall(r"(\d+) PARTIAL", tail)
            fail_matches = re.findall(r"(\d+) FAIL", tail)
            last_pass = pass_matches[-1] if pass_matches else "?"
            last_partial = partial_matches[-1] if partial_matches else "?"
            last_fail = fail_matches[-1] if fail_matches else "?"

            label = PIPELINE_LABELS.get(pid, pid)
            excel = p.get("excel_file", "")
            print(f"    {pid} ({label})")
            print(f"      cycle: {current_cycle}  |  scores: [{score_history}]")
            print(f"      last eval: {last_pass}P / {last_partial}A / {last_fail}F")
            if excel:
                print(f"      excel: {excel}")
        print()

    print(f"  Status file: {STATUS_FILE}")
    print(f"  Next refresh in {{interval}}s... (Ctrl+C to stop)")


def main():
    parser = argparse.ArgumentParser(description="Live monitor for parallel RAG optimization")
    parser.add_argument("--interval", type=int, default=30, help="Seconds between refreshes (default: 30)")
    args = parser.parse_args()

    cycle = 0
    try:
        while True:
            cycle += 1
            status = read_status()
            if status:
                output = render(status, cycle)
                # Patch the interval placeholder in the last printed line
                sys.stdout.write(f"\033[1A\r  Next refresh in {args.interval}s... (Ctrl+C to stop)\n")
                sys.stdout.flush()
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Waiting for status.json...")

            # Check if all done
            if status:
                total = status.get("pipelines_total", 5)
                done = status.get("completed", 0) + status.get("failed", 0)
                if done >= total:
                    print("\n  All pipelines finished. Exiting monitor.")
                    break

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n  Monitor stopped.")


if __name__ == "__main__":
    main()
