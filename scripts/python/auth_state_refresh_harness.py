#!/usr/bin/env python3
"""
Harness-only auth-state refresh utility.

Flow:
1) Reuse current SF_AUTH_STATE_B64 if present.
2) Login and pause for MFA code (via runs.checkpoint_info) when needed.
3) Force-load Lightning object pages to capture domain cookies.
4) Print refreshed storage state as base64 for heroku config:set.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import random
from datetime import datetime

from playwright.async_api import async_playwright

from salesforce_api import get_salesforce_credentials
from worker_utils import get_db_connection
from playwright_scripts import _is_authenticated_url, _is_mfa_or_verification_url, _wait_for_mfa_code_and_resume


def _new_run_id() -> str:
    return f"auth_refresh_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{random.randint(1000, 9999)}"


def _load_config_from_db() -> dict:
    conn = get_db_connection()
    if not conn:
        raise RuntimeError("No database connection for config lookup.")
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
                LIMIT 30
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
    raise RuntimeError("No valid Salesforce config found in runs table.")


def _extract_sf(cfg: dict) -> tuple[str, str, str]:
    sf = cfg.get("configuration", {}).get("salesforce", {})
    username = sf.get("username")
    password = sf.get("password")
    instance_url = sf.get("instanceUrl")
    if not all([username, password, instance_url]):
        raise ValueError("Missing Salesforce credentials.")
    return username, password, instance_url


def _upsert_run(run_id: str, status: str, message: str) -> None:
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
                    heartbeat_at = CURRENT_TIMESTAMP
                """,
                (
                    run_id,
                    status,
                    json.dumps({"mode": "auth_state_refresh_harness"}),
                    json.dumps({"status": status, "message": message}),
                ),
            )
        conn.commit()
    finally:
        conn.close()


async def main_async(args: argparse.Namespace) -> int:
    run_id = args.run_id or _new_run_id()
    _upsert_run(run_id, "running", "Auth refresh started")
    print(f"[auth-refresh] run_id={run_id}", flush=True)
    print("[auth-refresh] If MFA is required, submit code in Jobs UI for this run_id.", flush=True)

    cfg = _load_config_from_db()
    username, password, instance_url = _extract_sf(cfg)
    _, _ = get_salesforce_credentials(username=username, password=password, instance_url=instance_url)

    base = instance_url.rstrip("/")
    login_url = "https://login.salesforce.com" if "salesforce.com" in base else base
    lightning_base = base.replace(".my.salesforce.com", ".lightning.force.com")
    target_urls = [
        f"{lightning_base}/lightning/o/DataSemanticSearch/list?filterName=__Recent",
        f"{lightning_base}/lightning/o/DataSemanticSearch/home",
        f"{base}/lightning/o/DataSemanticSearch/list?filterName=__Recent",
        f"{base}/lightning/o/DataSemanticSearch/home",
    ]

    storage_state = None
    auth_b64 = args.auth_b64.strip() if args.auth_b64 else ""
    if not auth_b64:
        import os
        auth_b64 = os.getenv("SF_AUTH_STATE_B64", "").strip()
    if auth_b64:
        try:
            storage_state = json.loads(base64.b64decode(auth_b64).decode("utf-8"))
            print("[auth-refresh] Loaded existing SF_AUTH_STATE_B64.", flush=True)
        except Exception as e:
            print(f"[auth-refresh] Could not decode current SF_AUTH_STATE_B64: {e}", flush=True)

    def should_abort() -> bool:
        return False

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=args.headless, slow_mo=50)
        context = await browser.new_context(viewport={"width": 1280, "height": 720}, storage_state=storage_state)
        page = await context.new_page()
        await page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(1.0)

        current_url = page.url
        if not _is_authenticated_url(current_url):
            try:
                await page.get_by_role("textbox", name="Username").fill(username)
                await page.get_by_role("textbox", name="Password").fill(password)
                await page.get_by_role("button", name="Log In").click()
                await page.wait_for_load_state("domcontentloaded", timeout=60000)
                await asyncio.sleep(1.0)
            except Exception:
                pass

        current_url = page.url
        if _is_mfa_or_verification_url(current_url):
            resumed = await _wait_for_mfa_code_and_resume(page, run_id, should_abort, timeout_seconds=args.mfa_wait_seconds)
            if not resumed:
                _upsert_run(run_id, "failed", "MFA not completed in time")
                await browser.close()
                return 1

        current_url = page.url
        if not _is_authenticated_url(current_url):
            _upsert_run(run_id, "failed", f"Not authenticated after login flow: {current_url}")
            await browser.close()
            return 1

        # Prime both lightning and my domains into storage state.
        for u in target_urls:
            try:
                await page.goto(u, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(1.0)
                print(f"[auth-refresh] visited={page.url}", flush=True)
            except Exception as e:
                print(f"[auth-refresh] visit failed url={u} err={e}", flush=True)

        refreshed = await context.storage_state()
        raw = json.dumps(refreshed, separators=(",", ":"))
        refreshed_b64 = base64.b64encode(raw.encode("utf-8")).decode("utf-8")
        print("AUTH_STATE_B64_BEGIN", flush=True)
        print(refreshed_b64, flush=True)
        print("AUTH_STATE_B64_END", flush=True)
        await browser.close()

    _upsert_run(run_id, "completed", "Auth refresh completed")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Refresh Salesforce Playwright auth state (harness-only)")
    p.add_argument("--run-id", default=None, help="Run ID used for MFA code handoff")
    p.add_argument("--headless", action="store_true", help="Run browser headless")
    p.add_argument("--mfa-wait-seconds", type=int, default=1800, help="How long to wait for MFA code")
    p.add_argument("--auth-b64", default="", help="Optional existing storage-state base64 override")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

