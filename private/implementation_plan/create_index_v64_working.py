#!/usr/bin/env python3
"""
Known-working Search Index create script (v64.0).

WHY THIS SCRIPT EXISTS
----------------------
This file is the concrete "working POST" reference for this workspace. It encodes
the exact API pattern that successfully created indexes with LLM parser config:
- `parsingConfigurations[].config.id = parse_documents_using_llm`
- parser prompt persisted
- `searchType` persisted
- PDF chunking overrides persisted

VALIDATION STRATEGY (FASTEST SAFE ORDER)
----------------------------------------
Tier 1: FAST CREATE VALIDATION (usually 10-60s total)
  1) POST returns HTTP 2xx and response contains an index id
  2) Immediate GET by id returns HTTP 200
  3) GET confirms:
     - parser id present and correct
     - searchType matches requested type
     - PDF userValues include requested max/overlap values
  This tier proves payload contract compatibility quickly.

Tier 2: READINESS VALIDATION (can take minutes)
  4) Poll until `runtimeStatus == READY` (optional via --wait-ready)
  This proves the created index has finished processing.

Tier 3: FUNCTIONAL VALIDATION (outside this script)
  5) Run a smoke retrieval query against the new index
  This proves retrieval path is operational, not just config persistence.

TIMING EXPECTATIONS
-------------------
- SOAP login: ~1-5s
- Source GET + POST + verify GET: ~5-30s in normal conditions
- READY transition: highly variable (minutes; depends on file volume/processing)

FAILURE TRIAGE
--------------
- POST fails (4xx): payload shape/value contract issue
- POST fails (5xx): platform/transient; retry policy needed by caller/pipeline
- Verify GET missing parser/searchType/pdf values: payload mapping bug
- READY poll timeout: indexing backlog/runtime issue (infra gating should stop run)
"""

from __future__ import annotations

import argparse
import json
import time
import xml.etree.ElementTree as ET
from copy import deepcopy
from typing import Any, Dict, Optional

import requests


def _ms(start: float) -> int:
    return int((time.time() - start) * 1000)


def soap_login(username: str, password: str) -> str:
    login_url = "https://login.salesforce.com/services/Soap/u/60.0"
    soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<env:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
  <env:Body>
    <n1:login xmlns:n1="urn:partner.soap.sforce.com">
      <n1:username>{username}</n1:username>
      <n1:password>{password}</n1:password>
    </n1:login>
  </env:Body>
</env:Envelope>"""
    response = requests.post(
        login_url,
        data=soap_body,
        headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": "login"},
        timeout=30,
    )
    response.raise_for_status()
    root = ET.fromstring(response.text)
    ns = {"sf": "urn:partner.soap.sforce.com"}
    sid = root.find(".//sf:sessionId", ns)
    if sid is None or not sid.text:
        raise RuntimeError("SOAP login succeeded but sessionId missing.")
    return sid.text


def get_index(instance_url: str, sid: str, index_id: str, api_version: str = "v64.0") -> Dict[str, Any]:
    url = f"{instance_url.rstrip('/')}/services/data/{api_version}/ssot/search-index/{index_id}"
    response = requests.get(url, headers={"Authorization": f"Bearer {sid}"}, timeout=60)
    response.raise_for_status()
    return response.json()


def build_payload_from_existing(
    source_obj: Dict[str, Any],
    label: str,
    developer_name: str,
    search_type: str,
    pdf_max_tokens: Optional[int],
    pdf_overlap_tokens: Optional[int],
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "label": label,
        "developerName": developer_name,
        "sourceDmoDeveloperName": source_obj["sourceDmoDeveloperName"],
        "chunkDmoName": f"{label} chunk",
        "chunkDmoDeveloperName": f"{developer_name}_ch",
        "vectorDmoName": f"{label} index",
        "vectorDmoDeveloperName": f"{developer_name}_ix",
        "vectorEmbedding": {"vectorEmbeddingRelatedFields": []},
        "chunkingConfiguration": {"fileLevelConfiguration": {"perFileExtensions": []}},
        "vectorEmbeddingConfiguration": deepcopy(source_obj["vectorEmbeddingConfiguration"]),
        "searchType": search_type.upper(),
        "rankingConfigurations": [],
        "parsingConfigurations": deepcopy(source_obj.get("parsingConfigurations", [])),
        "preProcessingConfigurations": [],
        "transformConfigurations": deepcopy(source_obj.get("transformConfigurations", [])),
    }

    per_ext = source_obj.get("chunkingConfiguration", {}).get("perFileExtension", [])
    for ext in per_ext:
        rebuilt = {
            "fileExtension": ext["fileExtension"],
            "config": deepcopy(ext["config"]),
        }
        if ext.get("citations"):
            rebuilt["citations"] = deepcopy(ext["citations"])
        if ext["fileExtension"].lower() == "pdf" and (pdf_max_tokens or pdf_overlap_tokens is not None):
            uv = rebuilt["config"].get("userValues", [])
            out_uv = []
            seen = set()
            for item in uv:
                item_id = item.get("id")
                if item_id == "max_tokens" and pdf_max_tokens:
                    out_uv.append({"id": "max_tokens", "value": str(pdf_max_tokens)})
                    seen.add("max_tokens")
                elif item_id == "overlap_tokens" and pdf_overlap_tokens is not None:
                    out_uv.append({"id": "overlap_tokens", "value": str(pdf_overlap_tokens)})
                    seen.add("overlap_tokens")
                else:
                    out_uv.append(item)
            if pdf_max_tokens and "max_tokens" not in seen:
                out_uv.append({"id": "max_tokens", "value": str(pdf_max_tokens)})
            if pdf_overlap_tokens is not None and "overlap_tokens" not in seen:
                out_uv.append({"id": "overlap_tokens", "value": str(pdf_overlap_tokens)})
            rebuilt["config"]["userValues"] = out_uv
        payload["chunkingConfiguration"]["fileLevelConfiguration"]["perFileExtensions"].append(rebuilt)

    return payload


def create_index(instance_url: str, sid: str, payload: Dict[str, Any], api_version: str = "v64.0") -> Dict[str, Any]:
    url = f"{instance_url.rstrip('/')}/services/data/{api_version}/ssot/search-index"
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {sid}", "Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=120,
    )
    response.raise_for_status()
    return response.json()


def poll_ready(
    instance_url: str,
    sid: str,
    index_id: str,
    timeout_seconds: int = 1800,
    interval_seconds: int = 15,
    api_version: str = "v64.0",
) -> str:
    start = time.time()
    while True:
        idx = get_index(instance_url, sid, index_id, api_version)
        status = (idx.get("runtimeStatus") or "").upper()
        if status == "READY":
            return status
        if "FAILED" in status:
            raise RuntimeError(f"Index failed with status: {status}")
        if time.time() - start > timeout_seconds:
            raise TimeoutError(f"Timed out waiting for READY. Last status={status}")
        time.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create known-working Search Index payload via v64.0")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--instance-url", required=True)
    parser.add_argument("--source-index-id", default="18lKc000000oN30IAE")
    parser.add_argument("--search-type", default="HYBRID", choices=["VECTOR", "HYBRID", "vector", "hybrid"])
    parser.add_argument("--pdf-max-tokens", type=int, default=8000)
    parser.add_argument("--pdf-overlap-tokens", type=int, default=512)
    parser.add_argument("--wait-ready", action="store_true")
    parser.add_argument("--dump-payload", help="Path to dump JSON payload before POST")
    parser.add_argument(
        "--validation-mode",
        default="fast",
        choices=["fast", "ready"],
        help="fast=POST+GET config validation, ready=includes READY polling (same as --wait-ready).",
    )
    args = parser.parse_args()

    if args.validation_mode == "ready":
        args.wait_ready = True

    ts = time.strftime("%m%d%H%M%S")
    label = f"APIWorking {ts}"
    dev = f"AW{ts}"

    t0 = time.time()
    t = time.time()
    sid = soap_login(args.username, args.password)
    t_login_ms = _ms(t)

    t = time.time()
    source = get_index(args.instance_url, sid, args.source_index_id)
    t_source_get_ms = _ms(t)

    t = time.time()
    payload = build_payload_from_existing(
        source_obj=source,
        label=label,
        developer_name=dev,
        search_type=args.search_type,
        pdf_max_tokens=args.pdf_max_tokens,
        pdf_overlap_tokens=args.pdf_overlap_tokens,
    )
    t_build_payload_ms = _ms(t)

    if args.dump_payload:
        with open(args.dump_payload, 'w') as f:
            json.dump(payload, f, indent=2)

    t = time.time()
    created = create_index(args.instance_url, sid, payload)
    t_post_ms = _ms(t)

    created_id = created.get("id")
    if not created_id:
        raise RuntimeError(f"Create succeeded but id missing: {created}")

    t = time.time()
    verify = get_index(args.instance_url, sid, created_id)
    t_verify_get_ms = _ms(t)

    out: Dict[str, Any] = {
        "createdId": verify.get("id"),
        "label": verify.get("label"),
        "developerName": verify.get("developerName"),
        "runtimeStatus": verify.get("runtimeStatus"),
        "searchType": verify.get("searchType"),
        "parserId": (((verify.get("parsingConfigurations") or [{}])[0].get("config") or {}).get("id"))
        if verify.get("parsingConfigurations")
        else None,
    }

    pdf_uv = []
    for e in verify.get("chunkingConfiguration", {}).get("perFileExtension", []):
        if e.get("fileExtension", "").lower() == "pdf":
            pdf_uv = e.get("config", {}).get("userValues", [])
            break
    out["pdfUserValues"] = pdf_uv

    # Fast contract checks: these are the quickest high-signal validations.
    pdf_map = {x.get("id"): str(x.get("value")) for x in pdf_uv if isinstance(x, dict)}
    fast_checks = {
        "check_createdId_present": bool(out.get("createdId")),
        "check_searchType_matches": (str(out.get("searchType", "")).upper() == str(args.search_type).upper()),
        "check_parserId_matches": (out.get("parserId") == "parse_documents_using_llm"),
        "check_pdf_max_tokens_matches": (pdf_map.get("max_tokens") == str(args.pdf_max_tokens)),
        "check_pdf_overlap_tokens_matches": (pdf_map.get("overlap_tokens") == str(args.pdf_overlap_tokens)),
    }
    out["fastValidationChecks"] = fast_checks
    out["fastValidationPass"] = all(fast_checks.values())

    if args.wait_ready:
        t = time.time()
        final_status = poll_ready(args.instance_url, sid, created_id)
        t_ready_poll_ms = _ms(t)
        out["finalStatus"] = final_status
        out["readyValidationPass"] = final_status == "READY"
        out["timingMs"] = {
            "soapLogin": t_login_ms,
            "sourceGet": t_source_get_ms,
            "payloadBuild": t_build_payload_ms,
            "postCreate": t_post_ms,
            "verifyGet": t_verify_get_ms,
            "readyPoll": t_ready_poll_ms,
            "total": _ms(t0),
        }
    else:
        out["timingMs"] = {
            "soapLogin": t_login_ms,
            "sourceGet": t_source_get_ms,
            "payloadBuild": t_build_payload_ms,
            "postCreate": t_post_ms,
            "verifyGet": t_verify_get_ms,
            "total": _ms(t0),
        }

    out["validationMode"] = args.validation_mode
    out["operatorGuidance"] = {
        "fastModeMeaning": "POST worked and key persisted fields match. Use for rapid contract validation.",
        "readyModeMeaning": "Includes runtime READY confirmation; use as deployment gate.",
        "nextBestCheck": "Run retrieval smoke test against new index before full scoring cycle.",
    }

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
