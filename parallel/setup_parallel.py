#!/usr/bin/env python3
"""
One-time setup for parallel pipeline trial.

Steps per pipeline:
  1. Clone GenAiPromptTemplate via Metadata API (createMetadata)
  2. Create baseline search index via Playwright
  3. Poll index until COMPLETED
  4. Create retriever via Playwright
  5. Wire retriever to the cloned template via Metadata API (updateMetadata)
  6. Patch YAML config with the new searchIndexId

Usage:
  cd prompt-optimization-solution
  ./scripts/python/venv/bin/python3 parallel/setup_parallel.py [--pipeline P1] [--step clone|index|retriever|wire|all]
"""

import argparse
import asyncio
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
import yaml
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJ_ROOT / "scripts" / "python"
sys.path.insert(0, str(SCRIPTS_DIR))

from salesforce_api import (
    authenticate_soap,
    retrieve_metadata_via_api,
    find_index_id_by_name,
    get_retrievers,
    find_retriever_api_name,
    poll_index_until_ready,
    poll_retriever_until_activated,
    update_genai_prompt_with_retriever,
)
from playwright_scripts import _create_search_index_ui, _create_retriever_ui

METADATA_NS = "http://soap.sforce.com/2006/04/metadata"
SOAPENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

SOURCE_TEMPLATE = "RiteHite_Default_Parser_Test"

PIPELINES = {
    "P1":  {"yaml": "p1_simple.yaml",                "template": "RiteHite_Opt_P1",  "index_prefix": "Opt_P1"},
    "P3":  {"yaml": "p3_control.yaml",               "template": "RiteHite_Opt_P3",  "index_prefix": "Opt_P3"},
    "P4":  {"yaml": "p4_simplify_every_other.yaml",   "template": "RiteHite_Opt_P4",  "index_prefix": "Opt_P4"},
    "P7":  {"yaml": "p7_trend_aware.yaml",            "template": "RiteHite_Opt_P7",  "index_prefix": "Opt_P7"},
    "P10": {"yaml": "p10_gold_standard.yaml",         "template": "RiteHite_Opt_P10", "index_prefix": "Opt_P10"},
}

YAML_DIR = PROJ_ROOT / "inputs" / "trial_inputs_yml"
STATE_DIR = PROJ_ROOT / "parallel" / "setup_state"


def log(msg):
    print(f"[setup] {msg}", flush=True)


def _get_creds():
    sample_yaml = YAML_DIR / "p1_simple.yaml"
    with open(sample_yaml) as f:
        cfg = yaml.safe_load(f)
    sf = cfg["configuration"]["salesforce"]
    return sf["username"], sf["password"], sf["instanceUrl"]


def _soap_call(instance_url, access_token, action, body_xml):
    """Generic SOAP Metadata API call."""
    import requests

    envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="{SOAPENV_NS}" xmlns:met="{METADATA_NS}" xmlns:xsi="{XSI_NS}">
  <soapenv:Header>
    <met:SessionHeader><met:sessionId>{access_token}</met:sessionId></met:SessionHeader>
  </soapenv:Header>
  <soapenv:Body>{body_xml}</soapenv:Body>
</soapenv:Envelope>"""

    url = f"{instance_url.rstrip('/')}/services/Soap/m/65.0"
    headers = {"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": action}
    resp = requests.post(url, data=envelope, headers=headers, timeout=120)
    if not resp.ok:
        raise RuntimeError(f"SOAP {action} failed: {resp.status_code} {resp.text[:500]}")
    root = ET.fromstring(resp.text)
    fault = root.find(f".//{{{SOAPENV_NS}}}Fault")
    if fault is not None:
        fs = fault.find(f".//{{{SOAPENV_NS}}}faultstring")
        raise RuntimeError(f"SOAP fault: {fs.text if fs is not None else 'unknown'}")
    return root


def clone_template(instance_url, access_token, source_name, target_name):
    """Clone a GenAiPromptTemplate by reading source and creating a new one with createMetadata."""
    log(f"Reading source template: {source_name}")
    xml_str = retrieve_metadata_via_api(instance_url, access_token, "GenAiPromptTemplate", source_name)
    if not xml_str or not xml_str.strip():
        raise RuntimeError(f"Could not read template {source_name}")

    root = ET.fromstring(xml_str)
    ns = {"met": METADATA_NS}

    records_el = root[0] if len(root) > 0 else root

    def _set_text(parent, tag, value):
        el = parent.find(f"met:{tag}", ns) or parent.find(f"{{{METADATA_NS}}}{tag}")
        if el is not None:
            el.text = value
        else:
            new_el = ET.SubElement(parent, f"{{{METADATA_NS}}}{tag}")
            new_el.text = value

    _set_text(records_el, "fullName", target_name)
    _set_text(records_el, "masterLabel", target_name.replace("_", " "))

    inner_xml = ET.tostring(records_el, encoding="unicode")
    inner_xml = re.sub(r'<ns0:', '<met:', inner_xml)
    inner_xml = re.sub(r'</ns0:', '</met:', inner_xml)
    inner_xml = re.sub(r'xmlns:ns0="[^"]*"', '', inner_xml)

    body = f"""<met:createMetadata>
      <met:metadata xsi:type="met:GenAiPromptTemplate">{_extract_children_xml(records_el)}</met:metadata>
    </met:createMetadata>"""

    try:
        result_root = _soap_call(instance_url, access_token, "createMetadata", body)
        success_el = result_root.find(f".//{{{METADATA_NS}}}success")
        if success_el is not None and success_el.text == "true":
            log(f"  Created template: {target_name}")
            return True
        errors = result_root.findall(f".//{{{METADATA_NS}}}errors")
        for err in errors:
            msg_el = err.find(f"{{{METADATA_NS}}}message")
            log(f"  Error: {msg_el.text if msg_el is not None else 'unknown'}")
        return False
    except RuntimeError as e:
        if "DUPLICATE_DEVELOPER_NAME" in str(e) or "already exists" in str(e).lower():
            log(f"  Template {target_name} already exists, skipping clone")
            return True
        raise


def _extract_children_xml(element):
    """Extract the inner XML of an element's children as a string for SOAP body."""
    parts = []
    for child in element:
        tag = child.tag
        if "{" in tag:
            local = tag.split("}")[-1]
        else:
            local = tag
        parts.append(_element_to_met_xml(child, local))
    return "\n".join(parts)


def _element_to_met_xml(el, local_name=None):
    """Recursively convert an Element to met:-prefixed XML string."""
    if local_name is None:
        tag = el.tag
        local_name = tag.split("}")[-1] if "{" in tag else tag

    attrs = ""
    for k, v in el.attrib.items():
        if "type" in k:
            attrs += f' xsi:type="{v}"'
        else:
            attr_local = k.split("}")[-1] if "{" in k else k
            attrs += f' {attr_local}="{v}"'

    children_xml = ""
    for child in el:
        child_local = child.tag.split("}")[-1] if "{" in child.tag else child.tag
        children_xml += _element_to_met_xml(child, child_local)

    text = el.text or ""
    if children_xml:
        return f"<met:{local_name}{attrs}>{text}{children_xml}</met:{local_name}>"
    else:
        if text:
            escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return f"<met:{local_name}{attrs}>{escaped}</met:{local_name}>"
        return f"<met:{local_name}{attrs}/>"


def _get_default_parser_prompt(instance_url, access_token):
    """Read the default LLM parser prompt from a saved file, or fetch from the existing search index."""
    saved = PROJ_ROOT / "parallel" / "default_parser_prompt.txt"
    if saved.exists():
        text = saved.read_text(encoding="utf-8").strip()
        if text:
            return text

    from salesforce_api import SearchIndexAPI
    api = SearchIndexAPI(instance_url, access_token)
    existing_index_id = "18lKc000000oMxqIAE"
    try:
        idx = api.get_index(existing_index_id)
        for cfg in idx.get("parsingConfigurations", []):
            config_inner = cfg.get("config", {})
            for uv in config_inner.get("userValues", []):
                if uv.get("id") == "prompt":
                    prompt = uv.get("value", "")
                    if prompt:
                        saved.write_text(prompt, encoding="utf-8")
                        return prompt
    except Exception as e:
        log(f"  Warning: could not fetch default parser: {e}")
    return ""


def create_baseline_index(instance_url, access_token, username, password, pipeline_id, pipeline_cfg, headless=True):
    """Create a baseline search index for a pipeline via Playwright UI automation."""
    index_name = f"{pipeline_cfg['index_prefix']}_V1"
    state_dir = STATE_DIR / pipeline_id
    state_dir.mkdir(parents=True, exist_ok=True)

    log(f"Creating baseline search index: {index_name}")

    existing_id = find_index_id_by_name(instance_url, access_token, index_name, max_attempts=1)
    if existing_id:
        log(f"  Index {index_name} already exists (ID: {existing_id}), skipping creation")
        return existing_id, index_name

    parser_prompt = None
    if pipeline_id == "P10":
        gold_prompt_file = PROJ_ROOT / "scripts" / "python" / "app_data" / "V14_parser_prompt.txt"
        if gold_prompt_file.exists():
            parser_prompt = gold_prompt_file.read_text(encoding="utf-8").strip()
            log(f"  P10: Using gold standard parser ({len(parser_prompt)} chars)")
        else:
            log(f"  P10: Gold standard file not found at {gold_prompt_file}, using default parser")

    if not parser_prompt:
        parser_prompt = _get_default_parser_prompt(instance_url, access_token)
        log(f"  Using default parser prompt ({len(parser_prompt)} chars)")

    async def _run():
        should_abort = lambda: False
        idx_result = await _create_search_index_ui(
            username=username,
            password=password,
            instance_url=instance_url,
            index_name=index_name,
            parser_prompt=parser_prompt,
            state_dir=state_dir,
            run_id=f"setup_{pipeline_id}",
            headless=headless,
            should_abort=should_abort,
            access_token=access_token,
        )
        return idx_result

    result = asyncio.run(_run())
    if not result:
        raise RuntimeError(f"Failed to create search index {index_name}")

    index_id = find_index_id_by_name(instance_url, access_token, index_name)
    if not index_id:
        raise RuntimeError(f"Index {index_name} not found in API after creation")

    log(f"  Polling index {index_name} (ID: {index_id}) until ready...")
    ready = poll_index_until_ready(
        index_id, instance_url, access_token,
        timeout_seconds=2700, poll_interval=30,
    )
    if not ready:
        log(f"  WARNING: Index {index_name} not ready within timeout, continuing anyway")
    else:
        log(f"  Index ready: {index_name} (ID: {index_id})")
    return index_id, index_name


def create_baseline_retriever(instance_url, access_token, username, password, pipeline_id, index_name, headless=True):
    """Create a retriever for the pipeline's baseline index via Playwright."""
    state_dir = STATE_DIR / pipeline_id
    state_dir.mkdir(parents=True, exist_ok=True)

    log(f"Creating retriever for index: {index_name}")

    retrievers = get_retrievers(instance_url, access_token)
    existing = find_retriever_api_name(retrievers, index_name)
    if existing and existing[0]:
        log(f"  Retriever already exists: {existing[0]} ({existing[1]})")
        return existing[0], existing[1]

    async def _run():
        should_abort = lambda: False
        await _create_retriever_ui(
            username=username,
            password=password,
            instance_url=instance_url,
            index_name=index_name,
            state_dir=state_dir,
            run_id=f"setup_{pipeline_id}",
            headless=headless,
            should_abort=should_abort,
        )

    asyncio.run(_run())

    log(f"  Waiting for retriever activation...")
    time.sleep(10)
    retrievers = get_retrievers(instance_url, access_token)
    retriever_api, retriever_label = find_retriever_api_name(retrievers, index_name)
    if not retriever_api:
        raise RuntimeError(f"Retriever not found after creation for index {index_name}")
    log(f"  Retriever ready: {retriever_api}")
    return retriever_api, retriever_label


def wire_retriever_to_template(instance_url, access_token, template_name, retriever_api, retriever_label):
    """Wire the retriever to the cloned prompt template."""
    log(f"Wiring retriever {retriever_api} to template {template_name}")
    success = update_genai_prompt_with_retriever(
        instance_url, access_token, template_name, retriever_api, retriever_label
    )
    if not success:
        raise RuntimeError(f"Failed to wire retriever to template {template_name}")
    log(f"  Wired successfully")
    return True


def patch_yaml_config(pipeline_id, pipeline_cfg, search_index_id):
    """Update the YAML config with the actual searchIndexId."""
    yaml_path = YAML_DIR / pipeline_cfg["yaml"]
    with open(yaml_path, "r") as f:
        content = f.read()

    content = re.sub(
        r'searchIndexId:\s*"[^"]*"',
        f'searchIndexId: "{search_index_id}"',
        content,
    )
    with open(yaml_path, "w") as f:
        f.write(content)
    log(f"  Patched {yaml_path.name} with searchIndexId: {search_index_id}")


def save_setup_state(pipeline_id, state_data):
    """Save pipeline setup state to JSON for resumability."""
    state_file = STATE_DIR / f"{pipeline_id}_setup.json"
    with open(state_file, "w") as f:
        json.dump(state_data, f, indent=2)


def load_setup_state(pipeline_id):
    """Load pipeline setup state if it exists."""
    state_file = STATE_DIR / f"{pipeline_id}_setup.json"
    if state_file.exists():
        with open(state_file) as f:
            return json.load(f)
    return {}


def setup_pipeline(pipeline_id, pipeline_cfg, instance_url, access_token, username, password, headless=True, start_step="clone"):
    """Run full setup for a single pipeline, with resumability."""
    log(f"\n{'='*60}")
    log(f"SETTING UP PIPELINE: {pipeline_id}")
    log(f"  Template: {pipeline_cfg['template']}")
    log(f"  Index prefix: {pipeline_cfg['index_prefix']}")
    log(f"  YAML: {pipeline_cfg['yaml']}")
    log(f"{'='*60}")

    state = load_setup_state(pipeline_id)
    steps = ["clone", "index", "retriever", "wire", "patch"]
    start_idx = steps.index(start_step) if start_step in steps else 0

    if start_idx <= 0 and not state.get("cloned"):
        clone_template(instance_url, access_token, SOURCE_TEMPLATE, pipeline_cfg["template"])
        state["cloned"] = True
        save_setup_state(pipeline_id, state)

    if start_idx <= 1 and not state.get("index_id"):
        index_id, index_name = create_baseline_index(
            instance_url, access_token, username, password, pipeline_id, pipeline_cfg, headless
        )
        state["index_id"] = index_id
        state["index_name"] = index_name
        save_setup_state(pipeline_id, state)
    else:
        index_id = state.get("index_id")
        index_name = state.get("index_name", f"{pipeline_cfg['index_prefix']}_V1")

    if start_idx <= 2 and not state.get("retriever_api"):
        retriever_api, retriever_label = create_baseline_retriever(
            instance_url, access_token, username, password, pipeline_id, index_name, headless
        )
        state["retriever_api"] = retriever_api
        state["retriever_label"] = retriever_label
        save_setup_state(pipeline_id, state)
    else:
        retriever_api = state.get("retriever_api")
        retriever_label = state.get("retriever_label")

    if start_idx <= 3 and not state.get("wired"):
        wire_retriever_to_template(
            instance_url, access_token, pipeline_cfg["template"], retriever_api, retriever_label
        )
        state["wired"] = True
        save_setup_state(pipeline_id, state)

    if start_idx <= 4 and not state.get("yaml_patched"):
        patch_yaml_config(pipeline_id, pipeline_cfg, index_id)
        state["yaml_patched"] = True
        state["complete"] = True
        save_setup_state(pipeline_id, state)

    log(f"  Pipeline {pipeline_id} setup COMPLETE")
    return state


def main():
    parser = argparse.ArgumentParser(description="Setup parallel optimization pipelines")
    parser.add_argument("--pipeline", type=str, default=None, help="Run setup for a single pipeline (e.g. P1)")
    parser.add_argument("--step", type=str, default="clone", choices=["clone", "index", "retriever", "wire", "patch", "all"],
                        help="Start from this step (default: clone)")
    parser.add_argument("--headed", action="store_true", help="Run Playwright in headed mode (visible browser)")
    parser.add_argument("--skip-existing", action="store_true", help="Skip pipelines that already have setup state")
    args = parser.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    username, password, instance_url = _get_creds()
    log("Authenticating to Salesforce...")
    instance_url, access_token = authenticate_soap(username, password, instance_url)
    log(f"Authenticated: {instance_url}")

    headless = not args.headed
    start_step = "clone" if args.step == "all" else args.step

    pipelines_to_run = {}
    if args.pipeline:
        pid = args.pipeline.upper()
        if pid not in PIPELINES:
            log(f"Unknown pipeline: {pid}. Available: {list(PIPELINES.keys())}")
            sys.exit(1)
        pipelines_to_run[pid] = PIPELINES[pid]
    else:
        pipelines_to_run = PIPELINES

    results = {}
    for pid, pcfg in pipelines_to_run.items():
        if args.skip_existing:
            existing = load_setup_state(pid)
            if existing.get("complete"):
                log(f"Skipping {pid} (already complete)")
                results[pid] = existing
                continue

        try:
            state = setup_pipeline(pid, pcfg, instance_url, access_token, username, password, headless, start_step)
            results[pid] = state

            log("Re-authenticating (token refresh)...")
            instance_url, access_token = authenticate_soap(username, password, instance_url)
        except Exception as e:
            log(f"ERROR setting up {pid}: {e}")
            import traceback
            traceback.print_exc()
            results[pid] = {"error": str(e)}

    log("\n" + "=" * 60)
    log("SETUP SUMMARY")
    log("=" * 60)
    for pid, state in results.items():
        if state.get("complete"):
            log(f"  {pid}: COMPLETE (index={state.get('index_id')}, retriever={state.get('retriever_api')})")
        elif state.get("error"):
            log(f"  {pid}: FAILED ({state['error'][:80]})")
        else:
            log(f"  {pid}: PARTIAL (last step: {[k for k,v in state.items() if v and k != 'error'][-1] if state else 'none'})")


if __name__ == "__main__":
    main()
