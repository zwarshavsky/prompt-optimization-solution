#!/usr/bin/env python3
"""
Standalone Heroku harness loop for Playwright Cycle 2 Step 1.

Purpose:
- Run only the Search Index UI creation step in a repeatable loop.
- Keep auth-state reuse behavior exactly as in core code.
- Produce deterministic artifacts per attempt for continuous improvement.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from playwright_scripts import _create_search_index_ui  # noqa: E402
from salesforce_api import get_next_index_name, get_salesforce_credentials  # noqa: E402
from worker_utils import get_db_connection  # noqa: E402


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _new_run_id() -> str:
    return f"harness_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{random.randint(1000, 9999)}"


def _resolve_yaml_path(path: Path) -> Path:
    if path.exists():
        return path
    inputs_dir = Path("/app/inputs")
    preferred = [
        inputs_dir / "test_two_inputs.yaml",
        inputs_dir / "prompt_optimization_input.yaml",
    ]
    for candidate in preferred:
        if candidate.exists():
            return candidate
    matches = sorted(inputs_dir.glob("*.yaml"))
    if matches:
        return matches[0]
    raise FileNotFoundError(f"YAML not found: {path} (and no *.yaml in /app/inputs)")


def _load_yaml(path: Path) -> dict:
    resolved = _resolve_yaml_path(path)
    with resolved.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_config_from_db() -> dict:
    conn = get_db_connection()
    if not conn:
        raise RuntimeError("No database connection available for harness config fallback.")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT config
                FROM runs
                WHERE config IS NOT NULL
                  AND config::text <> 'null'
                  AND config ? 'configuration'
                ORDER BY updated_at DESC
                LIMIT 20
                """
            )
            rows = cur.fetchall()
        for row in rows:
            cfg = row[0] if row and row[0] else {}
            sf = (cfg.get("configuration", {}) or {}).get("salesforce", {}) if isinstance(cfg, dict) else {}
            if sf.get("username") and sf.get("password") and sf.get("instanceUrl"):
                return cfg
    finally:
        conn.close()
    raise RuntimeError("No valid config with Salesforce credentials found in runs table.")


def _extract_sf(cfg: dict) -> tuple[str, str, str]:
    sf = cfg.get("configuration", {}).get("salesforce", {})
    username = sf.get("username")
    password = sf.get("password")
    instance_url = sf.get("instanceUrl")
    if not all([username, password, instance_url]):
        raise ValueError("Missing Salesforce credentials in YAML.")
    return username, password, instance_url


def _upsert_harness_run(run_id: str, status: str, message: str) -> None:
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO runs (
                    run_id, status, config, progress, output_lines,
                    results, checkpoint_info, updated_at, heartbeat_at, started_at
                ) VALUES (
                    %s, %s, %s::jsonb, %s::jsonb, '[]'::jsonb,
                    '{}'::jsonb, '{}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (run_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    progress = EXCLUDED.progress,
                    updated_at = CURRENT_TIMESTAMP,
                    heartbeat_at = CURRENT_TIMESTAMP;
                """,
                (
                    run_id,
                    status,
                    json.dumps({"mode": "playwright_harness_loop"}),
                    json.dumps({"status": status, "message": message}),
                ),
            )
        conn.commit()
    finally:
        conn.close()


async def _run_once(
    *,
    yaml_path: Path,
    index_prefix: str,
    parser_prompt: str,
    state_dir: Path,
    run_id: str,
    headless: bool,
) -> dict:
    cfg_source = ""
    try:
        yaml_path = _resolve_yaml_path(yaml_path)
        cfg = _load_yaml(yaml_path)
        cfg_source = str(yaml_path)
    except Exception:
        cfg = _load_config_from_db()
        cfg_source = "database:runs.config"
    username, password, instance_url = _extract_sf(cfg)
    _, access_token = get_salesforce_credentials(
        username=username, password=password, instance_url=instance_url
    )
    index_name = get_next_index_name(instance_url, access_token, base_name=index_prefix)
    _upsert_harness_run(run_id, "running", f"Harness attempt starting for {index_name} using {cfg_source}")

    def should_abort() -> bool:
        return False

    started = time.time()
    try:
        index_id, full_name = await _create_search_index_ui(
            username=username,
            password=password,
            instance_url=instance_url,
            index_name=index_name,
            parser_prompt=parser_prompt,
            state_dir=state_dir,
            run_id=run_id,
            headless=headless,
            should_abort=should_abort,
            access_token=access_token,
        )
        elapsed = round(time.time() - started, 2)
        ok = bool(index_id)
        _upsert_harness_run(
            run_id,
            "completed" if ok else "failed",
            f"index_id={index_id or 'none'} elapsed={elapsed}s",
        )
        return {
            "ok": ok,
            "run_id": run_id,
            "index_id": index_id,
            "index_name": full_name or index_name,
            "elapsed_seconds": elapsed,
            "timestamp": _now(),
        }
    except Exception as e:
        elapsed = round(time.time() - started, 2)
        _upsert_harness_run(run_id, "failed", f"{type(e).__name__}: {e}")
        return {
            "ok": False,
            "run_id": run_id,
            "error": f"{type(e).__name__}: {e}",
            "elapsed_seconds": elapsed,
            "timestamp": _now(),
        }


async def main_async(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir = Path(args.artifacts_dir).resolve()
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    summary_path = artifacts_dir / "playwright_harness_history.jsonl"

    count = 0
    while True:
        count += 1
        run_id = args.run_id or _new_run_id()
        print(f"[harness] attempt={count} run_id={run_id}", flush=True)
        result = await _run_once(
            yaml_path=Path(args.yaml).resolve(),
            index_prefix=args.index_prefix,
            parser_prompt=args.parser_prompt,
            state_dir=state_dir,
            run_id=run_id,
            headless=args.headless,
        )
        with summary_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(result) + "\n")
        print(f"[harness] result={json.dumps(result)}", flush=True)

        if args.max_attempts > 0 and count >= args.max_attempts:
            break
        time.sleep(args.sleep_seconds)
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Standalone Playwright harness loop")
    p.add_argument("--yaml", required=True, help="YAML config path")
    p.add_argument("--index-prefix", required=True, help="Index prefix (e.g., Test_20260324)")
    p.add_argument(
        "--parser-prompt",
        default="Harness parser prompt for deterministic Step 1 automation.",
        help="Parser prompt text used for index creation",
    )
    p.add_argument("--headless", action="store_true", help="Run headless browser")
    p.add_argument("--sleep-seconds", type=int, default=120, help="Wait between attempts")
    p.add_argument("--max-attempts", type=int, default=0, help="0 means infinite loop")
    p.add_argument("--run-id", default=None, help="Optional fixed run_id (usually omit)")
    p.add_argument("--state-dir", default="scripts/python/state", help="State directory")
    p.add_argument(
        "--artifacts-dir",
        default="scripts/python/app_data/harness",
        help="Directory for JSONL attempt history",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

