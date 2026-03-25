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
from collections import deque
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
        await hybrid_btn.wait_for(state="visible", timeout=15000)
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
BASELINE_CHUNK_INPUTS_LINE = "        chunk_inputs = builder.locator(\"input[type='number'], input[inputmode='numeric'], [role='spinbutton']\")"
BASELINE_CHUNK_FILL_BLOCK = """        await chunk_inputs.nth(0).wait_for(state="visible", timeout=10000)
        # Max Tokens
        max_tokens_input = chunk_inputs.nth(0)
        await max_tokens_input.click()
        await max_tokens_input.fill("")
        await max_tokens_input.type("8000")
        await max_tokens_input.press("Tab")
        await asyncio.sleep(0.3)
        # Overlap Tokens — use click+clear+type+Tab to trigger change detection
        overlap_input = chunk_inputs.nth(1)
        await overlap_input.click()
        await overlap_input.fill("")
        await overlap_input.type("512")
        await overlap_input.press("Tab")
        await asyncio.sleep(0.3)
"""
CHUNK_FILL_BLOCK_REPLACEMENT = """        if not used_js_chunking:
            await chunk_inputs.nth(0).wait_for(state="visible", timeout=10000)
            # Max Tokens
            max_tokens_input = chunk_inputs.nth(0)
            await max_tokens_input.click()
            await max_tokens_input.fill("")
            await max_tokens_input.type("8000")
            await max_tokens_input.press("Tab")
            await asyncio.sleep(0.3)
            # Overlap Tokens — use click+clear+type+Tab to trigger change detection
            overlap_input = chunk_inputs.nth(1)
            await overlap_input.click()
            await overlap_input.fill("")
            await overlap_input.type("512")
            await overlap_input.press("Tab")
            await asyncio.sleep(0.3)
"""
BASELINE_CHUNK_ERROR_LINE = """            raise RuntimeError(f"Chunking inputs not found after 5 expand strategies. pdf_row text='{pdf_row_text}'")"""
BASELINE_TABLE_SAVE_BLOCK = """        save_btn = builder.get_by_role("table").get_by_role("button", name="Save")
        if await save_btn.is_visible():
            await save_btn.click()
            await asyncio.sleep(1)"""
TABLE_SAVE_REPLACEMENT = """        print("   [create_index] Save-gate: waiting for row Save to enable...", flush=True)
        save_clicked = False
        last_diag = {}
        for save_attempt in range(1, 46):
            # Re-query every attempt to avoid stale handles during LWC re-renders.
            save_candidates = [
                builder.get_by_role("table").get_by_role("button", name="Save"),
                builder.get_by_role("button", name="Save"),
                builder.locator("button:has-text('Save')"),
            ]
            for cand in save_candidates:
                try:
                    if await cand.count() == 0:
                        continue
                    btn = cand.first
                    if not await btn.is_visible():
                        continue
                    disabled = await btn.get_attribute("disabled")
                    aria_disabled = await btn.get_attribute("aria-disabled")
                    cls = (await btn.get_attribute("class")) or ""
                    is_disabled = (disabled is not None) or (aria_disabled == "true") or ("disabled" in cls.lower())
                    if not is_disabled:
                        await btn.click(timeout=8000)
                        await asyncio.sleep(1.0)
                        print(f"   [create_index] Save-gate: clicked Save on attempt {save_attempt}", flush=True)
                        save_clicked = True
                        break
                except Exception:
                    pass
            if save_clicked:
                break

            # Nudge validation and capture diagnostics while waiting.
            last_diag = await builder.evaluate(\"\"\"() => {
                const allRoots = () => {
                    const roots = [document];
                    const seen = new Set([document]);
                    const stack = [document];
                    while (stack.length) {
                        const root = stack.pop();
                        if (!root || !root.querySelectorAll) continue;
                        root.querySelectorAll('*').forEach((el) => {
                            if (el.shadowRoot && !seen.has(el.shadowRoot)) {
                                seen.add(el.shadowRoot);
                                roots.push(el.shadowRoot);
                                stack.push(el.shadowRoot);
                            }
                            if (el.tagName === 'IFRAME') {
                                try {
                                    const d = el.contentDocument;
                                    if (d && !seen.has(d)) {
                                        seen.add(d);
                                        roots.push(d);
                                        stack.push(d);
                                    }
                                } catch (_) {}
                            }
                        });
                    }
                    return roots;
                };
                const isNum = (el) => {
                    const t = (el.getAttribute('type') || '').toLowerCase();
                    const im = (el.getAttribute('inputmode') || '').toLowerCase();
                    const role = (el.getAttribute('role') || '').toLowerCase();
                    return t === 'number' || im === 'numeric' || role === 'spinbutton';
                };
                const nums = [];
                const saves = [];
                const saveLabels = new Set(['save', 'save & build', 'build', 'finish']);
                for (const r of allRoots()) {
                    if (!r.querySelectorAll) continue;
                    Array.from(r.querySelectorAll('input, [role=\"spinbutton\"]')).filter(isNum).forEach((n) => nums.push(n));
                    r.querySelectorAll('button').forEach((b) => {
                        const txt = ((b.innerText || b.textContent || '').trim() || '').toLowerCase();
                        if (saveLabels.has(txt) || txt.includes('save')) {
                            const disabled = !!b.disabled || b.getAttribute('aria-disabled') === 'true';
                            saves.push({
                                text: txt,
                                disabled,
                                ariaDisabled: b.getAttribute('aria-disabled'),
                                cls: b.className || ''
                            });
                            // If enabled, click directly from page context (works across shadow/iframe roots).
                            if (!disabled) {
                                try { b.click(); } catch (_) {}
                            }
                        }
                    });
                }
                nums.forEach((el) => {
                    try {
                        el.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
                        el.dispatchEvent(new FocusEvent('blur', { bubbles: true, composed: true }));
                    } catch (_) {}
                });
                return { numericInputs: nums.length, savesCount: saves.length, saves: saves.slice(0, 8) };
            }\"\"\")
            if save_attempt % 5 == 0:
                print(f"   [create_index] Save-gate wait attempt={save_attempt} diag={last_diag}", flush=True)
            await asyncio.sleep(1.0)
        if not save_clicked:
            print(f"   [create_index] Save-gate did not click Save; continuing with Next fallback. diag={last_diag}", flush=True)"""
CHUNK_ERROR_REPLACEMENT = """            print("   [create_index] Strategy 6: JS direct set with row+overlay retries", flush=True)
            js_chunk = await builder.evaluate(\"\"\"async () => {
                const isNum = (el) => {
                    if (!el) return false;
                    const t = (el.getAttribute('type') || '').toLowerCase();
                    const im = (el.getAttribute('inputmode') || '').toLowerCase();
                    const role = (el.getAttribute('role') || '').toLowerCase();
                    return t === 'number' || im === 'numeric' || role === 'spinbutton';
                };
                const walkInputs = (root, seen = new Set(), out = []) => {
                    if (!root || seen.has(root)) return out;
                    seen.add(root);
                    root.querySelectorAll('*').forEach((el) => {
                        if (isNum(el)) out.push(el);
                        if (el.shadowRoot) walkInputs(el.shadowRoot, seen, out);
                    });
                    return out;
                };
                const allRoots = () => {
                    const roots = [document];
                    const seen = new Set([document]);
                    const stack = [document];
                    while (stack.length) {
                        const root = stack.pop();
                        if (!root || !root.querySelectorAll) continue;
                        root.querySelectorAll('*').forEach((el) => {
                            if (el.shadowRoot && !seen.has(el.shadowRoot)) {
                                seen.add(el.shadowRoot);
                                roots.push(el.shadowRoot);
                                stack.push(el.shadowRoot);
                            }
                            if (el.tagName === 'IFRAME') {
                                try {
                                    const d = el.contentDocument;
                                    if (d && !seen.has(d)) {
                                        seen.add(d);
                                        roots.push(d);
                                        stack.push(d);
                                    }
                                } catch (_) {}
                            }
                        });
                    }
                    return roots;
                };
                const findHosts = () => {
                    const out = [];
                    for (const r of allRoots()) {
                        if (!r.querySelectorAll) continue;
                        r.querySelectorAll('runtime_cdp-search-index-chunking-strategy').forEach((h) => out.push(h));
                    }
                    return out;
                };
                const clickEditAffordances = () => {
                    const selectors = [
                        'button.slds-cell-edit__button',
                        'button[title=\"pdf\"]',
                        'lightning-button-icon',
                        '[data-id]'
                    ];
                    let clicks = 0;
                    for (const r of allRoots()) {
                        if (!r.querySelectorAll) continue;
                        for (const sel of selectors) {
                            r.querySelectorAll(sel).forEach((el) => {
                                const txt = ((el.innerText || el.textContent || el.getAttribute('title') || '') + '').toLowerCase();
                                if (txt.includes('pdf') || sel !== 'button[title=\"pdf\"]') {
                                    try {
                                        el.click();
                                        clicks++;
                                    } catch (_) {}
                                }
                            });
                        }
                    }
                    return clicks;
                };
                const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
                // January-era insight: don't proceed until chunking config appears mounted.
                // This avoids acting while the builder is still hydrating in Heroku headless.
                for (let warm = 1; warm <= 12; warm++) {
                    const bodyText = (document.body && document.body.textContent) ? document.body.textContent : '';
                    const hasIndicators = ['Passage Extraction', 'max_tokens', 'Chunking', 'perFileExtension'].some(k => bodyText.includes(k));
                    const hostCountNow = findHosts().length;
                    if (hostCountNow > 0 || hasIndicators) break;
                    await sleep(1000);
                }
                const timeline = [];
                for (let attempt = 1; attempt <= 6; attempt++) {
                    const hosts = findHosts();
                    const host = hosts[0] || null;
                    const overlay = document.querySelector('lightning-overlay-container, section[role="dialog"], [role="dialog"], .slds-modal, .uiModal');
                    const hostShadowChildCount = host && host.shadowRoot ? host.shadowRoot.childElementCount : -1;
                    const roots = allRoots();
                    if (host) roots.unshift(host.shadowRoot || host);
                    if (overlay) roots.unshift(overlay.shadowRoot || overlay);
                    let inputs = [];
                    for (const rt of roots) {
                        inputs = walkInputs(rt, new Set(), []);
                        if (inputs.length >= 2) break;
                    }
                    timeline.push({
                        attempt,
                        row: false,
                        host: !!host,
                        hostConnected: !!(host && host.isConnected),
                        hostShadowChildCount,
                        overlay: !!overlay,
                        overlayTag: overlay ? overlay.tagName : null,
                        inputs: inputs.length
                    });
                    if (inputs.length >= 2) {
                        const fire = (el, value) => {
                            el.focus();
                            el.value = value;
                            el.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
                            el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, composed: true }));
                            el.dispatchEvent(new KeyboardEvent('keyup', { key: 'Tab', bubbles: true, composed: true }));
                            el.blur();
                        };
                        fire(inputs[0], '8000');
                        fire(inputs[1], '512');
                        // Verify values persisted before declaring success.
                        const v0 = (inputs[0].value || '').toString();
                        const v1 = (inputs[1].value || '').toString();
                        if (!(v0.includes('8000') && v1.includes('512'))) {
                            await sleep(500);
                        }
                        return { ok: true, attempt, count: inputs.length, host: !!host, overlay: !!overlay, timeline };
                    }
                    await sleep(700);
                    const clicks = clickEditAffordances();
                    timeline[timeline.length - 1].clicks = clicks;
                }
                const finalHostCount = findHosts().length;
                return { ok: false, reason: 'inputs-not-mounted', hostCount: finalHostCount, timeline };
            }\"\"\")
            print(f"   [create_index] JS chunk set result: {js_chunk}", flush=True)
            if not js_chunk.get("ok"):
                raise RuntimeError(f"Chunking inputs not found after 6 strategies. pdf_row text='{pdf_row_text}' js={js_chunk}")
            used_js_chunking = True"""

SETUP_URL_CANDIDATES_BLOCK = """        setup_candidates = [
            f"{base}/lightning/o/DataSemanticSearch/home",
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
                # If object-home path exposes New, accept it as the entry path immediately.
                if "/lightning/o/datasemanticsearch/home" in cand.lower():
                    new_btn_probe = page.get_by_role("button", name="New")
                    if await new_btn_probe.count() > 0:
                        setup_url = page.url
                        print(f"   [create_index] object-home candidate selected (New visible): {setup_url}", flush=True)
                        break
                    if "search indexes" in t:
                        setup_url = page.url
                        print(f"   [create_index] object-home candidate selected (Search Indexes page): {setup_url}", flush=True)
                        break
                    print(f"   [create_index] object-home candidate reached but New not visible: {page.url}", flush=True)
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
    if BASELINE_CHUNK_INPUTS_LINE in source:
        source = source.replace(
            BASELINE_CHUNK_INPUTS_LINE,
            BASELINE_CHUNK_INPUTS_LINE + "\n        used_js_chunking = False",
            1,
        )
    else:
        print("[harness] WARN: chunk_inputs line not found; chunk-js patch skipped.", flush=True)
    if BASELINE_CHUNK_ERROR_LINE in source:
        source = source.replace(BASELINE_CHUNK_ERROR_LINE, CHUNK_ERROR_REPLACEMENT, 1)
    else:
        print("[harness] WARN: chunk error line not found; chunk-js strategy skipped.", flush=True)
    if BASELINE_CHUNK_FILL_BLOCK in source:
        source = source.replace(BASELINE_CHUNK_FILL_BLOCK, CHUNK_FILL_BLOCK_REPLACEMENT, 1)
    else:
        print("[harness] WARN: chunk fill block not found; chunk-js fill guard skipped.", flush=True)
    if BASELINE_TABLE_SAVE_BLOCK in source:
        source = source.replace(BASELINE_TABLE_SAVE_BLOCK, TABLE_SAVE_REPLACEMENT, 1)
    else:
        print("[harness] WARN: table save block not found; save-gate patch skipped.", flush=True)
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
    strategy_list = [s.strip() for s in args.strategies.split(",") if s.strip()]
    if not strategy_list:
        strategy_list = ["baseline"]
    strategies = deque(strategy_list)
    while True:
        count += 1
        run_id = args.run_id or _new_run_id()
        strategy = strategies[0]
        strategies.rotate(-1)
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

        # Live-reactive strategy steering based on concrete failure signatures.
        if not result.get("ok"):
            err = (result.get("error") or "").lower()
            preferred: list[str] = []
            if "hybrid search" in err or "searchbox" in err:
                preferred = ["searchbox_first", "baseline", "hybrid_role_first"]
            elif "pdf-row-missing" in err or "host-missing" in err or "inputs-not-mounted" in err:
                preferred = ["setup_only_recovery", "baseline", "searchbox_first"]
            elif "chunking inputs not found" in err:
                preferred = ["baseline", "setup_only_recovery", "searchbox_first"]
            if preferred:
                current = list(strategies)
                rest = [s for s in current if s not in preferred]
                strategies = deque([s for s in preferred if s in current] + rest)
                print(f"[harness] adaptive_reorder err_hint='{preferred[0]}' next={list(strategies)}", flush=True)

        if args.max_attempts > 0 and count >= args.max_attempts:
            break
        # Faster retries when failures are deterministic.
        sleep_seconds = args.sleep_seconds
        if not result.get("ok"):
            sleep_seconds = min(args.sleep_seconds, 30)
        time.sleep(sleep_seconds)
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

