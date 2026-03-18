#!/usr/bin/env python3
"""
Parallel pipeline supervisor — launches and monitors 5 optimization pipelines.

Responsibilities:
  - Launch pipelines in batches of 2 (to manage browser/API contention)
  - Monitor heartbeats via process liveness + log tailing
  - Restart crashed pipelines with --resume
  - Clean stale prompt-template lock files
  - Write status.json dashboard for human monitoring
  - Run compare_pipelines.py when all pipelines finish

Usage:
  cd prompt-optimization-solution
  ./scripts/python/venv/bin/python3 parallel/parallel_supervisor.py [--batch-size 2] [--headed]
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJ_ROOT / "scripts" / "python"
YAML_DIR = PROJ_ROOT / "inputs" / "trial_inputs_yml"
STATE_DIR = PROJ_ROOT / "scripts" / "python" / "app_data" / "state"
OUTPUTS_DIR = PROJ_ROOT / "scripts" / "python" / "app_data" / "outputs"
SUPERVISOR_DIR = PROJ_ROOT / "parallel"
STATUS_FILE = SUPERVISOR_DIR / "status.json"
PYTHON = PROJ_ROOT / "scripts" / "python" / "venv" / "bin" / "python3"
MAIN_PY = SCRIPTS_DIR / "main.py"

PIPELINES = {
    "P1":  {"yaml": "p1_simple.yaml",                "template": "RiteHite_Opt_P1"},
    "P3":  {"yaml": "p3_control.yaml",               "template": "RiteHite_Opt_P3"},
    "P4":  {"yaml": "p4_simplify_every_other.yaml",   "template": "RiteHite_Opt_P4"},
    "P7":  {"yaml": "p7_trend_aware.yaml",            "template": "RiteHite_Opt_P7"},
    "P10": {"yaml": "p10_gold_standard.yaml",         "template": "RiteHite_Opt_P10"},
}

BATCH_ORDER = [["P1", "P3", "P4", "P7", "P10"]]

MAX_RESTARTS = 2
HEARTBEAT_TIMEOUT_MINUTES = 60
POLL_INTERVAL_SECONDS = 60
PIPELINE_TIMEOUT_HOURS = 8

shutdown_requested = False


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [supervisor] {msg}", flush=True)


def signal_handler(signum, frame):
    global shutdown_requested
    log(f"Received signal {signum}, initiating graceful shutdown...")
    shutdown_requested = True


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


class PipelineProcess:
    """Tracks a single pipeline subprocess."""

    def __init__(self, pipeline_id, yaml_file, template_name):
        self.pipeline_id = pipeline_id
        self.yaml_file = yaml_file
        self.template_name = template_name
        self.process = None
        self.log_file = None
        self.log_path = None
        self.start_time = None
        self.end_time = None
        self.exit_code = None
        self.restarts = 0
        self.status = "queued"
        self.last_log_size = 0
        self.cycles_completed = 0
        self.best_composite = 0
        self.last_activity = None
        self.error_message = None
        self.excel_file = None

    def launch(self, is_resume=False):
        """Launch the pipeline as a subprocess."""
        yaml_path = YAML_DIR / self.yaml_file

        cmd = [
            str(PYTHON), str(MAIN_PY),
            "--full-workflow",
            "--yaml-input", str(yaml_path),
            "--clean-state" if not is_resume else "--resume",
        ]

        self.log_path = SUPERVISOR_DIR / f"{self.pipeline_id}_output.log"
        self.log_file = open(self.log_path, "a")

        self.log_file.write(f"\n{'='*60}\n")
        self.log_file.write(f"{'RESUME' if is_resume else 'LAUNCH'}: {self.pipeline_id} at {datetime.now().isoformat()}\n")
        self.log_file.write(f"Command: {' '.join(cmd)}\n")
        self.log_file.write(f"{'='*60}\n\n")
        self.log_file.flush()

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        self.process = subprocess.Popen(
            cmd,
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
            cwd=str(PROJ_ROOT),
            env=env,
        )

        self.start_time = datetime.now()
        self.last_activity = datetime.now()
        self.status = "running"
        log(f"  {self.pipeline_id} launched (PID {self.process.pid}, resume={is_resume})")

    def poll(self):
        """Check if the subprocess is still running. Update status."""
        if self.process is None:
            return

        retcode = self.process.poll()
        if retcode is not None:
            self.exit_code = retcode
            self.end_time = datetime.now()
            self.status = "completed" if retcode == 0 else "failed"
            if self.log_file:
                self.log_file.close()
                self.log_file = None
            return

        if self.log_path and self.log_path.exists():
            current_size = self.log_path.stat().st_size
            if current_size > self.last_log_size:
                self.last_activity = datetime.now()
                self._parse_log_tail(current_size)
                self.last_log_size = current_size

    def _parse_log_tail(self, current_size):
        """Parse recent log output for cycle/score info."""
        try:
            read_from = max(0, current_size - 4096)
            with open(self.log_path, "r") as f:
                f.seek(read_from)
                tail = f.read()

            cycle_matches = re.findall(r"REFINEMENT CYCLE (\d+)", tail)
            if cycle_matches:
                self.cycles_completed = max(int(c) for c in cycle_matches)

            composite_matches = re.findall(r"Composite Score: (\d+)", tail)
            if composite_matches:
                self.best_composite = max(int(c) for c in composite_matches)

            excel_matches = re.findall(r"Run-specific Excel file: (.+\.xlsx)", tail)
            if excel_matches:
                self.excel_file = excel_matches[-1].strip()

            error_matches = re.findall(r"CRITICAL ERROR: (.+)", tail)
            if error_matches:
                self.error_message = error_matches[-1].strip()[:200]
        except Exception:
            pass

    def is_hung(self):
        """Check if the pipeline appears hung (no log activity for timeout period)."""
        if self.status != "running" or self.last_activity is None:
            return False
        elapsed = (datetime.now() - self.last_activity).total_seconds() / 60
        return elapsed > HEARTBEAT_TIMEOUT_MINUTES

    def is_timed_out(self):
        """Check if the pipeline has exceeded the maximum runtime."""
        if self.status != "running" or self.start_time is None:
            return False
        elapsed = (datetime.now() - self.start_time).total_seconds() / 3600
        return elapsed > PIPELINE_TIMEOUT_HOURS

    def kill(self):
        """Force-kill the pipeline subprocess."""
        if self.process and self.process.poll() is None:
            log(f"  Killing {self.pipeline_id} (PID {self.process.pid})")
            try:
                os.kill(self.process.pid, signal.SIGTERM)
                time.sleep(5)
                if self.process.poll() is None:
                    os.kill(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.log_file:
            self.log_file.close()
            self.log_file = None
        self.status = "killed"

    def to_dict(self):
        return {
            "pipeline_id": self.pipeline_id,
            "status": self.status,
            "pid": self.process.pid if self.process else None,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "exit_code": self.exit_code,
            "restarts": self.restarts,
            "cycles_completed": self.cycles_completed,
            "best_composite": self.best_composite,
            "last_activity": self.last_activity.isoformat() if self.last_activity else None,
            "error_message": self.error_message,
            "excel_file": self.excel_file,
            "log_file": str(self.log_path) if self.log_path else None,
        }


def clean_stale_locks():
    """Remove lock files that may be left over from previous runs."""
    if not STATE_DIR.exists():
        return
    cleaned = 0
    for lock_file in STATE_DIR.glob("prompt_lock_RiteHite_Opt_*.lock"):
        lock_file.unlink()
        cleaned += 1
    for lock_file in STATE_DIR.glob("index_lock_*.lock"):
        try:
            data = json.loads(lock_file.read_text())
            if "setup_" in data.get("run_id", "") or "RiteHite_Opt" in data.get("run_id", ""):
                lock_file.unlink()
                cleaned += 1
        except Exception:
            pass
    if cleaned:
        log(f"Cleaned {cleaned} stale lock file(s)")


def write_status(pipelines, event_log, start_time):
    """Write status.json dashboard."""
    completed = sum(1 for p in pipelines.values() if p.status in ("completed",))
    failed = sum(1 for p in pipelines.values() if p.status in ("failed", "killed"))
    running = sum(1 for p in pipelines.values() if p.status == "running")
    queued = sum(1 for p in pipelines.values() if p.status == "queued")

    best = max((p.best_composite for p in pipelines.values()), default=0)
    best_pipeline = next((p.pipeline_id for p in pipelines.values() if p.best_composite == best and best > 0), None)

    status = {
        "last_updated": datetime.now().isoformat(),
        "wall_clock_elapsed": str(datetime.now() - start_time).split(".")[0],
        "pipelines_total": len(pipelines),
        "completed": completed,
        "failed": failed,
        "running": running,
        "queued": queued,
        "best_result_so_far": {"pipeline": best_pipeline, "composite_score": best} if best_pipeline else None,
        "pipelines": {pid: p.to_dict() for pid, p in pipelines.items()},
        "event_log_tail": event_log[-20:],
    }

    with open(STATUS_FILE, "w") as f:
        json.dump(status, f, indent=2)


def run_supervisor(batch_size=2, headed=False):
    """Main supervisor loop."""
    start_time = datetime.now()
    event_log = []

    def add_event(msg):
        event_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        log(msg)

    add_event(f"Supervisor started (batch_size={batch_size})")

    clean_stale_locks()

    pipelines = {}
    for pid, pcfg in PIPELINES.items():
        pipelines[pid] = PipelineProcess(pid, pcfg["yaml"], pcfg["template"])

    launch_queue = []
    for batch in BATCH_ORDER:
        launch_queue.extend(batch)

    running_slots = 0
    launched_set = set()

    def launch_next():
        nonlocal running_slots
        while running_slots < batch_size and launch_queue:
            pid = launch_queue.pop(0)
            p = pipelines[pid]
            is_resume = p.restarts > 0
            try:
                clean_stale_locks()
                p.launch(is_resume=is_resume)
                launched_set.add(pid)
                running_slots += 1
                add_event(f"Launched {pid} ({'resume' if is_resume else 'fresh'})")
            except Exception as e:
                add_event(f"Failed to launch {pid}: {e}")
                p.status = "failed"
                p.error_message = str(e)

    launch_next()
    write_status(pipelines, event_log, start_time)

    while not shutdown_requested:
        all_done = all(p.status in ("completed", "failed", "killed") for p in pipelines.values())
        if all_done:
            add_event("All pipelines finished")
            break

        for pid, p in pipelines.items():
            if p.status != "running":
                continue

            p.poll()

            if p.status in ("completed", "failed"):
                running_slots -= 1
                if p.status == "completed":
                    add_event(f"{pid} completed (cycles={p.cycles_completed}, composite={p.best_composite})")
                else:
                    add_event(f"{pid} failed (exit={p.exit_code}, error={p.error_message or 'unknown'})")

                    if p.restarts < MAX_RESTARTS:
                        p.restarts += 1
                        p.status = "queued"
                        launch_queue.insert(0, pid)
                        add_event(f"{pid} queued for restart ({p.restarts}/{MAX_RESTARTS})")

                launch_next()

            elif p.is_hung():
                add_event(f"{pid} appears hung (no activity for {HEARTBEAT_TIMEOUT_MINUTES}min)")
                p.kill()
                running_slots -= 1

                if p.restarts < MAX_RESTARTS:
                    p.restarts += 1
                    p.status = "queued"
                    launch_queue.insert(0, pid)
                    add_event(f"{pid} queued for restart after hang ({p.restarts}/{MAX_RESTARTS})")

                launch_next()

            elif p.is_timed_out():
                add_event(f"{pid} timed out after {PIPELINE_TIMEOUT_HOURS}h")
                p.kill()
                running_slots -= 1
                p.status = "failed"
                p.error_message = f"Timed out after {PIPELINE_TIMEOUT_HOURS}h"
                launch_next()

        write_status(pipelines, event_log, start_time)
        time.sleep(POLL_INTERVAL_SECONDS)

    if shutdown_requested:
        add_event("Shutdown requested — killing running pipelines")
        for p in pipelines.values():
            if p.status == "running":
                p.kill()
        write_status(pipelines, event_log, start_time)

    add_event("Running comparison analysis...")
    write_status(pipelines, event_log, start_time)

    try:
        compare_script = SUPERVISOR_DIR / "compare_pipelines.py"
        result = subprocess.run(
            [str(PYTHON), str(compare_script)],
            cwd=str(PROJ_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            add_event("Comparison analysis complete")
            log(result.stdout)
        else:
            add_event(f"Comparison analysis failed: {result.stderr[:200]}")
    except Exception as e:
        add_event(f"Comparison analysis error: {e}")

    write_status(pipelines, event_log, start_time)
    log(f"\nSupervisor finished. Total wall time: {datetime.now() - start_time}")
    log(f"Status dashboard: {STATUS_FILE}")


def main():
    parser = argparse.ArgumentParser(description="Parallel pipeline supervisor")
    parser.add_argument("--batch-size", type=int, default=2, help="Max concurrent pipelines (default: 2)")
    parser.add_argument("--headed", action="store_true", help="Run Playwright in headed mode")
    args = parser.parse_args()

    run_supervisor(batch_size=args.batch_size, headed=args.headed)


if __name__ == "__main__":
    main()
