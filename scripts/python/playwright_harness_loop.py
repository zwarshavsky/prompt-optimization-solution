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
import importlib.util
import random
import sys
import time
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from salesforce_api import get_next_index_name, get_salesforce_credentials  # noqa: E402
from worker_utils import get_db_connection  # noqa: E402


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _new_run_id() -> str:
    return f"harness_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{random.randint(1000, 9999)}"


BASELINE_HYBRID_BLOCK = """        print("   [create_index] Builder opened. Hybrid + RagFileUDMO...", flush=True)
        hybrid_btn = builder.get_by_text("Hybrid search", exact=False).or_(builder.get_by_text("Hybrid Search", exact=False)).first
        try:
            await hybrid_btn.wait_for(state="visible", timeout=15000)
        except Exception:
            snap_path = state_dir / f"{run_id}_baseline_hybrid_missing.png"
            await builder.screenshot(path=str(snap_path), full_page=True)
            try:
                summary = await builder.evaluate(\"\"\"() => {
                    const txt = (el) => (el && (el.innerText || el.textContent || '').trim()) || '';
                    const buttons = Array.from(document.querySelectorAll('button, [role=\"button\"], label, span'))
                        .map(txt).filter(Boolean).slice(0, 80);
                    return { url: location.href, title: document.title, buttons };
                }\"\"\")
                print(f"   [create_index] DIAG baseline hybrid missing: {summary}", flush=True)
            except Exception as diag_e:
                print(f"   [create_index] DIAG baseline evaluation failed: {diag_e}", flush=True)
            print(f"   [create_index] DIAG screenshot saved: {snap_path}", flush=True)
            raise
        await hybrid_btn.click()
        await asyncio.sleep(0.5)
        searchbox = builder.get_by_role("searchbox", name="Search data model objects…")
        await searchbox.wait_for(state="visible", timeout=15000)
        await searchbox.fill("rag")
"""

BASELINE_OBJECT_NEW_FALLBACK = """            except Exception:
                print("   [create_index] 'New' still not visible; opening SearchIndex new-record URL directly...", flush=True)
                await page.goto(f"{base}/lightning/o/SearchIndex/new", wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(1.0)
                opened_new_flow_direct = True
"""

BASELINE_SETUP_URL_LINE = '        setup_url = f"{base}/lightning/setup/DataSemanticSearch/home"'

SETUP_URL_CANDIDATES_BLOCK = """        setup_candidates = [
            f"{base}/lightning/setup/SetupOneHome/home",
            f"{base}/lightning/setup/SemanticSearch/home",
            f"{base}/lightning/setup/DataSemanticSearch/home",
            f"{base}/lightning/setup/EinsteinSearch/home",
        ]
        setup_url = setup_candidates[0]
        for cand in setup_candidates:
            try:
                await page.goto(cand, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(0.8)
                t = (await page.title() or "").lower()
                if "page not found" in t or "not found" in t:
                    print(f"   [create_index] setup candidate not usable: {cand} title={t}", flush=True)
                    continue
                has_quick_find = await page.get_by_placeholder("Quick Find").count()
                if has_quick_find == 0:
                    print(f"   [create_index] setup candidate missing Quick Find: {cand} title={t}", flush=True)
                    continue
                setup_url = cand
                print(f"   [create_index] setup candidate selected: {setup_url}", flush=True)
                break
            except Exception as e:
                print(f"   [create_index] setup candidate failed: {cand} err={e}", flush=True)
                continue
"""

SETUP_ONLY_FALLBACK = """            except Exception:
                print("   [create_index] 'New' still not visible on object pages; forcing setup-only re-entry.", flush=True)
                await page.goto(setup_url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(1.0)
                qf = page.get_by_placeholder("Quick Find")
                try:
                    await qf.wait_for(state="visible", timeout=8000)
                    await qf.fill("Search Indexes")
                    await asyncio.sleep(0.8)
                    setup_link = page.get_by_role("link", name="Search Indexes").first
                    await setup_link.click(timeout=10000)
                    await page.wait_for_load_state("domcontentloaded", timeout=20000)
                    await asyncio.sleep(0.8)
                except Exception:
                    pass
                await new_btn.wait_for(state="visible", timeout=12000)
"""


STRATEGY_BLOCKS: dict[str, str] = {
    "baseline": BASELINE_HYBRID_BLOCK,
    "searchbox_first": """        print("   [create_index] Builder opened. Hybrid + RagFileUDMO... [searchbox_first]", flush=True)
        searchbox = builder.get_by_role("searchbox", name="Search data model objects…")
        try:
            await searchbox.wait_for(state="visible", timeout=7000)
            print("   [create_index] searchbox visible without Hybrid click.", flush=True)
        except Exception:
            hybrid_btn = builder.get_by_text("Hybrid search", exact=False).or_(builder.get_by_text("Hybrid Search", exact=False)).first
            try:
                await hybrid_btn.wait_for(state="visible", timeout=12000)
            except Exception:
                snap_path = state_dir / f"{run_id}_searchbox_first_hybrid_missing.png"
                await builder.screenshot(path=str(snap_path), full_page=True)
                print(f"   [create_index] DIAG screenshot saved: {snap_path}", flush=True)
                raise
            await hybrid_btn.click()
            await asyncio.sleep(0.5)
            await searchbox.wait_for(state="visible", timeout=15000)
        await searchbox.fill("rag")
""",
    "hybrid_role_first": """        print("   [create_index] Builder opened. Hybrid + RagFileUDMO... [hybrid_role_first]", flush=True)
        searchbox = builder.get_by_role("searchbox", name="Search data model objects…")
        hybrid_role = builder.get_by_role("radio", name=re.compile("hybrid", re.IGNORECASE)).first
        try:
            await hybrid_role.wait_for(state="visible", timeout=8000)
            await hybrid_role.click()
            await asyncio.sleep(0.5)
        except Exception:
            hybrid_btn = builder.get_by_text("Hybrid search", exact=False).or_(builder.get_by_text("Hybrid Search", exact=False)).first
            try:
                await hybrid_btn.wait_for(state="visible", timeout=12000)
            except Exception:
                snap_path = state_dir / f"{run_id}_hybrid_role_first_hybrid_missing.png"
                await builder.screenshot(path=str(snap_path), full_page=True)
                print(f"   [create_index] DIAG screenshot saved: {snap_path}", flush=True)
                raise
            await hybrid_btn.click()
            await asyncio.sleep(0.5)
        await searchbox.wait_for(state="visible", timeout=15000)
        await searchbox.fill("rag")
""",
    "searchbox_only": """        print("   [create_index] Builder opened. Hybrid + RagFileUDMO... [searchbox_only]", flush=True)
        searchbox = builder.get_by_role("searchbox", name="Search data model objects…")
        try:
            await searchbox.wait_for(state="visible", timeout=20000)
        except Exception:
            snap_path = state_dir / f"{run_id}_searchbox_only_missing.png"
            await builder.screenshot(path=str(snap_path), full_page=True)
            print(f"   [create_index] DIAG screenshot saved: {snap_path}", flush=True)
            raise
        await searchbox.fill("rag")
""",
}


def _load_create_index_func(strategy: str) -> Callable:
    source_path = SCRIPT_DIR / "playwright_scripts.py"
    source = source_path.read_text(encoding="utf-8")
    if strategy not in STRATEGY_BLOCKS:
        strategy = "baseline"
    replacement = STRATEGY_BLOCKS[strategy]
    if BASELINE_SETUP_URL_LINE in source:
        source = source.replace(BASELINE_SETUP_URL_LINE, SETUP_URL_CANDIDATES_BLOCK, 1)
    else:
        print("[harness] WARN: setup_url line not found; candidate setup patch skipped.", flush=True)
    if strategy == "setup_only_recovery":
        if BASELINE_OBJECT_NEW_FALLBACK not in source:
            raise RuntimeError("Could not find object-new fallback block for setup_only_recovery strategy.")
        source = source.replace(BASELINE_OBJECT_NEW_FALLBACK, SETUP_ONLY_FALLBACK, 1)
    if strategy != "baseline":
        if BASELINE_HYBRID_BLOCK in source:
            source = source.replace(BASELINE_HYBRID_BLOCK, replacement, 1)
        else:
            # Keep strategy execution alive even if upstream source formatting changed.
            # This prevents matrix runs from aborting on brittle text replacement.
            print(f"[harness] WARN: hybrid patch block not found for strategy={strategy}; continuing without hybrid override.", flush=True)
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tf:
        tf.write(source)
        temp_path = tf.name
    spec = importlib.util.spec_from_file_location(f"playwright_scripts_{strategy}", temp_path)
    if not spec or not spec.loader:
        raise RuntimeError("Failed to load temporary Playwright module.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    func = getattr(module, "_create_search_index_ui", None)
    if not callable(func):
        raise RuntimeError("Patched module missing _create_search_index_ui.")
    return func


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
    strategy: str,
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
    _upsert_harness_run(run_id, "running", f"Harness attempt starting for {index_name} using {cfg_source} strategy={strategy}")

    def should_abort() -> bool:
        return False

    started = time.time()
    try:
        create_index_func = _load_create_index_func(strategy)
        index_id, full_name = await create_index_func(
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
            "strategy": strategy,
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
            "strategy": strategy,
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
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    if not strategies:
        strategies = ["baseline"]
    while True:
        count += 1
        run_id = args.run_id or _new_run_id()
        strategy = strategies[(count - 1) % len(strategies)]
        print(f"[harness] attempt={count} run_id={run_id} strategy={strategy}", flush=True)
        result = await _run_once(
            yaml_path=Path(args.yaml).resolve(),
            index_prefix=args.index_prefix,
            parser_prompt=args.parser_prompt,
            state_dir=state_dir,
            run_id=run_id,
            headless=args.headless,
            strategy=strategy,
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
    p.add_argument(
        "--strategies",
        default="baseline,searchbox_first,hybrid_role_first,searchbox_only,setup_only_recovery",
        help="Comma-separated strategy sequence for harness-only rapid variants",
    )
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

