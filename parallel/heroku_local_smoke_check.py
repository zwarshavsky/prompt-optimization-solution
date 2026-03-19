#!/usr/bin/env python3
"""
Local "Heroku-style" checks without deploying or pushing.

- Parses Procfile (expects cwd = repo root, same as `heroku local`)
- Verifies worker/main imports with repo root as cwd (matches worker dyno)
- Optionally boots Streamlit briefly (same argv shape as web dyno)

Usage (from repo root):
  python3 parallel/heroku_local_smoke_check.py
  # or with project venv:
  cd scripts/python && ./venv/bin/python3 ../../parallel/heroku_local_smoke_check.py

For true Procfile emulation locally (needs Heroku CLI + .env):
  cd <repo-root>
  heroku local web    # in another terminal
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PARALLEL = REPO / "parallel"
VENV_PY = REPO / "scripts" / "python" / "venv" / "bin" / "python3"


def ok(msg: str) -> None:
    print(f"OK  {msg}")


def fail(msg: str) -> None:
    print(f"FAIL {msg}")
    raise SystemExit(1)


def python_exe() -> str:
    if VENV_PY.is_file():
        return str(VENV_PY)
    return sys.executable


def run(cmd: list[str], *, cwd: Path, env: dict | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def check_procfile() -> None:
    proc = REPO / "Procfile"
    if not proc.is_file():
        fail("Procfile missing at repo root")
    text = proc.read_text(encoding="utf-8")
    if "streamlit run scripts/python/app.py" not in text:
        fail("Procfile web command should run scripts/python/app.py")
    if "scripts/python/worker.py" not in text:
        fail("Procfile worker should run scripts/python/worker.py")
    ok("Procfile references app.py + worker.py from repo root")


def check_imports_from_repo_root() -> None:
    """Heroku runs worker as `python -u scripts/python/worker.py` with cwd = repo root."""
    py = python_exe()
    code = """
import os, sys
# Same as worker.py: scripts/python on path
sys.path.insert(0, os.path.join(os.getcwd(), "scripts", "python"))
import worker_utils  # noqa
import main  # noqa
import salesforce_api  # noqa
print("imports_ok")
"""
    r = run([py, "-c", code], cwd=REPO, timeout=120)
    if r.returncode != 0 or "imports_ok" not in (r.stdout or ""):
        fail(
            "Import chain failed (cwd=repo root):\n"
            + (r.stderr or r.stdout or "")[:2000]
        )
    ok(f"Imports (worker_utils, main, salesforce_api) cwd={REPO.name}/")


def check_streamlit_boot() -> None:
    """Match web dyno: python -m streamlit run scripts/python/app.py --server.port=$PORT (non-blocking read)."""
    py = python_exe()
    port = os.environ.get("SMOKE_STREAMLIT_PORT", "18765")
    env = {**os.environ, "PORT": port}
    cmd = [
        py,
        "-u",
        "-m",
        "streamlit",
        "run",
        "scripts/python/app.py",
        "--server.port",
        port,
        "--server.address",
        "127.0.0.1",
        "--server.headless",
        "true",
    ]
    p = subprocess.Popen(
        cmd,
        cwd=str(REPO),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    buf: list[str] = []
    deadline = time.time() + 35.0
    try:
        assert p.stdout is not None
        while time.time() < deadline:
            line = p.stdout.readline()
            if line:
                buf.append(line)
                blob = "".join(buf)
                if re.search(r"(Local URL|Network URL|You can now view)", blob, re.I):
                    ok(f"Streamlit started (port {port}, headless)")
                    p.terminate()
                    try:
                        p.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        p.kill()
                    return
            elif p.poll() is not None:
                break
            time.sleep(0.05)
        combined = "".join(buf)
        p.terminate()
        try:
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()
        if re.search(r"(Local URL|Network URL|You can now view)", combined, re.I):
            ok(f"Streamlit started (port {port}, headless)")
            return
        if p.returncode not in (0, None, -15, -9):  # terminated/killed
            pass
        if "Error" in combined or "Traceback" in combined:
            fail(f"Streamlit failed to start:\n{combined[-3500:]}")
        ok("Streamlit boot: no URL within 35s (machine slow?); set SKIP_STREAMLIT_BOOT=1 to skip")
    except Exception as e:
        try:
            p.kill()
        except Exception:
            pass
        fail(f"Streamlit boot exception: {e}")


def run_validate_heroku_readiness() -> None:
    script = PARALLEL / "validate_heroku_readiness.py"
    if not script.is_file():
        fail("parallel/validate_heroku_readiness.py missing")
    py = python_exe()
    r = run([py, str(script)], cwd=REPO, timeout=60)
    sys.stdout.write(r.stdout or "")
    sys.stderr.write(r.stderr or "")
    if r.returncode != 0:
        fail("validate_heroku_readiness.py failed")
    ok("validate_heroku_readiness.py passed")


def main() -> None:
    print(f"Repo root: {REPO}")
    print(f"Python:    {python_exe()}\n")
    check_procfile()
    run_validate_heroku_readiness()
    check_imports_from_repo_root()
    if os.environ.get("SKIP_STREAMLIT_BOOT", "").lower() in ("1", "true", "yes"):
        print("SKIP_STREAMLIT_BOOT=1 — skipping Streamlit boot")
    else:
        check_streamlit_boot()
    print("\n---")
    print("Local Heroku-style smoke checks finished.")
    print("Optional: install Heroku CLI, add .env with DATABASE_URL + GEMINI_API_KEY, then:")
    print(f"  cd {REPO}")
    print("  heroku local web")


if __name__ == "__main__":
    main()
