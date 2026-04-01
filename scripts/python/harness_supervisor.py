#!/usr/bin/env python3
"""
Self-healing supervisor for the Playwright harness loop.

Runs the harness as a child process, forwards logs, and restarts on:
- non-zero exit
- output silence (stalled run)
"""

from __future__ import annotations

import argparse
import os
import queue
import shlex
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _reader_thread(pipe, out_queue: queue.Queue[str]) -> None:
    try:
        for line in iter(pipe.readline, ""):
            out_queue.put(line)
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _terminate_child(child: subprocess.Popen, grace_seconds: int) -> None:
    if child.poll() is not None:
        return
    try:
        child.terminate()
    except Exception:
        return
    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        if child.poll() is not None:
            return
        time.sleep(0.25)
    try:
        child.kill()
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Supervise playwright_harness_loop.py with auto-restart.")
    parser.add_argument("--stall-seconds", type=int, default=420, help="Restart child if no stdout for this duration.")
    parser.add_argument("--restart-delay-seconds", type=int, default=8, help="Delay before restart after failure/stall.")
    parser.add_argument("--graceful-shutdown-seconds", type=int, default=15, help="Grace period before SIGKILL.")
    parser.add_argument(
        "--child-command",
        default="python -u scripts/python/playwright_harness_loop.py --yaml /app/inputs/prompt_optimization_input.yaml --index-prefix Test_20260324 --headless --sleep-seconds 120",
        help="Command used to launch the harness child.",
    )
    args = parser.parse_args()

    cmd = shlex.split(args.child_command)
    print(f"[supervisor] start utc={_utc_now()} cmd={' '.join(cmd)}", flush=True)

    stop_event = threading.Event()

    def _handle_shutdown(signum, _frame):
        print(f"[supervisor] received signal={signum} utc={_utc_now()} stopping...", flush=True)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    attempt = 0
    while not stop_event.is_set():
        attempt += 1
        print(f"[supervisor] launch attempt={attempt} utc={_utc_now()}", flush=True)
        child = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=os.environ.copy(),
        )
        assert child.stdout is not None
        q: queue.Queue[str] = queue.Queue()
        t = threading.Thread(target=_reader_thread, args=(child.stdout, q), daemon=True)
        t.start()

        last_output = time.time()
        stalled = False
        while not stop_event.is_set():
            try:
                line = q.get(timeout=1.0)
                last_output = time.time()
                print(line, end="", flush=True)
            except queue.Empty:
                pass

            if child.poll() is not None:
                break

            silence = time.time() - last_output
            if silence > args.stall_seconds:
                stalled = True
                print(
                    f"[supervisor] stall detected silence_seconds={int(silence)} > {args.stall_seconds}; restarting child utc={_utc_now()}",
                    flush=True,
                )
                _terminate_child(child, args.graceful_shutdown_seconds)
                break

        # Drain any remaining buffered lines.
        while True:
            try:
                line = q.get_nowait()
                print(line, end="", flush=True)
            except queue.Empty:
                break

        if stop_event.is_set():
            _terminate_child(child, args.graceful_shutdown_seconds)
            break

        exit_code = child.poll()
        print(f"[supervisor] child ended exit_code={exit_code} stalled={stalled} utc={_utc_now()}", flush=True)
        time.sleep(args.restart_delay_seconds)

    print(f"[supervisor] stopped utc={_utc_now()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
