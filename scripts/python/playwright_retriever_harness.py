#!/usr/bin/env python3
"""
Step 3 UI harness: Create Retriever via Playwright only.

This script is intentionally scoped to retriever UI creation/activation checks so we
can validate Step 3 behavior independently on Heroku.
"""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Optional, Tuple

import yaml

from playwright_scripts import _create_retriever_ui
from salesforce_api import (
    SearchIndexAPI,
    get_salesforce_credentials,
    poll_retriever_until_activated,
)


def _resolve_yaml_path(path: Path) -> Path:
    if path.exists():
        return path
    inputs_dir = Path("/app/inputs")
    preferred = [
        inputs_dir / "prompt_optimization_input.yaml",
        inputs_dir / "test_two_inputs.yaml",
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


def _extract_sf_creds(config: dict) -> Tuple[str, str, str]:
    sf = config.get("configuration", {}).get("salesforce", {})
    username = sf.get("username")
    password = sf.get("password")
    instance_url = sf.get("instanceUrl")
    if not all([username, password, instance_url]):
        raise ValueError("Missing Salesforce credentials in YAML configuration.salesforce")
    return username, password, instance_url


def _latest_index_name_for_prefix(instance_url: str, access_token: str, prefix: str) -> Optional[str]:
    api = SearchIndexAPI(instance_url, access_token)
    raw = api.list_indexes()
    details = raw.get("semanticSearchDefinitionDetails") or []
    patt = re.compile(rf"^{re.escape(prefix)}_V(\d+)$", re.IGNORECASE)
    best = (-1, None)
    for d in details:
        dev = (d.get("developerName") or "").strip()
        m = patt.match(dev)
        if not m:
            continue
        ver = int(m.group(1))
        if ver > best[0]:
            best = (ver, dev)
    return best[1]


async def _run(args: argparse.Namespace) -> int:
    yaml_cfg = _load_yaml(Path(args.yaml))
    username, password, instance_url_cfg = _extract_sf_creds(yaml_cfg)
    instance_url, access_token = get_salesforce_credentials(
        username=username, password=password, instance_url=instance_url_cfg
    )

    index_name = (args.index_name or "").strip()
    if not index_name:
        if not args.index_prefix:
            raise ValueError("Provide --index-name or --index-prefix")
        index_name = _latest_index_name_for_prefix(instance_url, access_token, args.index_prefix) or ""
        if not index_name:
            raise RuntimeError(f"No existing index found for prefix '{args.index_prefix}'")

    print(f"[retriever-harness] target index: {index_name}", flush=True)
    state_dir = Path(args.state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)

    def should_abort() -> bool:
        return False

    retriever_display_name, activate_clicked = await _create_retriever_ui(
        username=username,
        password=password,
        instance_url=instance_url,
        index_name=index_name,
        state_dir=state_dir,
        run_id=args.run_id,
        headless=args.headless,
        should_abort=should_abort,
    )

    result = {
        "ok": bool(retriever_display_name),
        "index_name": index_name,
        "retriever_display_name": retriever_display_name,
        "activate_clicked": bool(activate_clicked),
    }

    if retriever_display_name and args.poll_activation:
        api_name, label = poll_retriever_until_activated(
            instance_url=instance_url,
            access_token=access_token,
            retriever_display_name=retriever_display_name,
            timeout_seconds=args.poll_timeout_seconds,
            poll_interval=args.poll_interval_seconds,
        )
        result["retriever_api_name"] = api_name
        result["retriever_label"] = label
        result["activated"] = bool(api_name)
        result["ok"] = result["ok"] and bool(api_name)

    print(f"[retriever-harness] result={json.dumps(result)}", flush=True)
    return 0 if result["ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Step 3 retriever UI harness")
    parser.add_argument(
        "--yaml",
        default="/app/inputs/prompt_optimization_input.yaml",
        help="Path to YAML config",
    )
    parser.add_argument("--index-name", default="", help="Exact existing index developer name")
    parser.add_argument("--index-prefix", default="", help="Index prefix (uses latest *_Vn)")
    parser.add_argument("--state-dir", default="/app/.harness_state", help="State/artifact directory")
    parser.add_argument("--run-id", default="retriever_harness", help="Run identifier")
    parser.add_argument("--headless", action="store_true", help="Run browser headless")
    parser.add_argument("--poll-activation", action="store_true", help="Poll retriever API activation")
    parser.add_argument("--poll-timeout-seconds", type=int, default=600, help="Activation poll timeout")
    parser.add_argument("--poll-interval-seconds", type=int, default=10, help="Activation poll interval")
    args = parser.parse_args()

    try:
        return asyncio.run(_run(args))
    except Exception as e:
        print(f"[retriever-harness] fatal: {type(e).__name__}: {e}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())

