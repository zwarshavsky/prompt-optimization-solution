#!/usr/bin/env python3
"""
Self-check for Heroku-related fixes (no DB, no network).

Run from repo root (use venv that has psycopg2):
  cd scripts/python && ./venv/bin/python3 ../../parallel/validate_heroku_readiness.py
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = REPO / "scripts" / "python"
sys.path.insert(0, str(PY))


def ok(msg: str) -> None:
    print(f"OK  {msg}")


def fail(msg: str) -> None:
    print(f"FAIL {msg}")
    raise SystemExit(1)


def main() -> None:
    # 1) Worker uploads path
    import worker_utils

    d = worker_utils.get_uploads_restore_dir()
    if not str(d).replace("\\", "/").endswith("scripts/python/app_data/uploads"):
        fail(f"get_uploads_restore_dir() unexpected: {d}")
    if d.parent.name != "app_data" or d.parent.parent.name != "python":
        fail(f"uploads dir not under scripts/python/app_data: {d}")
    ok(f"worker_utils.get_uploads_restore_dir() -> {d}")

    # 2) salesforce_api: abort log must not reference undefined current_model
    src = (PY / "salesforce_api.py").read_text(encoding="utf-8")
    if '"model": current_model' in src or "'model': current_model" in src:
        fail("salesforce_api.py still references undefined current_model in JSON/log dict")
    if '"model": "template_default"' not in src:
        fail("expected template_default string in abort log payload")
    ok("salesforce_api.py abort path uses template_default, not current_model")

    # 3) app.py includes indexPrefix / minCycles / maxCycles in job flow
    app_src = (PY / "app.py").read_text(encoding="utf-8")
    for needle in (
        'config_section["indexPrefix"]',
        'config_section["minCycles"]',
        'config_section["maxCycles"]',
        "form_index_prefix",
        "form_min_cycles",
        "form_max_cycles",
    ):
        if needle not in app_src:
            fail(f"app.py missing expected snippet: {needle}")
    ok("app.py wires indexPrefix + min/max cycles into config_section")

    # 4) load_pdfs_from_db uses get_uploads_restore_dir when output_dir is None
    load_src = inspect.getsource(worker_utils.load_pdfs_from_db)
    if "get_uploads_restore_dir" not in load_src:
        fail("load_pdfs_from_db should call get_uploads_restore_dir()")
    if 'scripts" / "python" / "app_data"' in load_src:
        fail("load_pdfs_from_db still contains broken doubled scripts path")
    ok("load_pdfs_from_db delegates default dir to get_uploads_restore_dir")

    print("\nAll Heroku readiness checks passed.")


if __name__ == "__main__":
    main()
