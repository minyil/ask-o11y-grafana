#!/usr/bin/env python3
"""Live two-upload merge E2E: CSV + XLSX in one session → one derived dataset."""
from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from openpyxl import Workbook

TOKEN = os.environ.get("MCP_SHARED_TOKEN", "")
ORG = os.environ.get("ANALYSIS_SERVICE_ORG_ID", "1")
USER = os.environ.get("ANALYSIS_SERVICE_USER_ID", "ask-o11y")
GRAFANA_QUERY_URL = os.environ.get("GRAFANA_QUERY_MCP_URL", "http://127.0.0.1:8772/mcp")
SANDBOX_URL = os.environ.get("SANDBOX_ANALYSIS_MCP_URL", "http://127.0.0.1:8777/mcp")
UPLOAD_URL = GRAFANA_QUERY_URL.removesuffix("/mcp") + "/uploads"
SESSION_ID = "multi-document-e2e"
OTHER_SESSION_ID = "multi-document-e2e-other"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Grafana-Org-Id": ORG, "X-Grafana-User": USER, "X-Grafana-Actor-User-Id": USER}

MERGE_CODE = """frames = []
for item in documents:
    if item['input_format'] == 'csv':
        frames.append(pd.read_csv(item['path']))
    else:
        frames.append(pd.read_excel(item['path'], sheet_name=item['sheet'] or 0))
merged = frames[0].merge(frames[1], on='date', how='inner')
emit_frame(merged, name='merged-a-b')
emit({'rows': len(merged), 'columns': list(merged.columns), 'filenames': [item['filename'] for item in documents], 'sum_b': int(merged['value_b'].sum())}, name='result.json')
"""


MCP_SESSIONS: dict[str, str] = {}


def post(url: str, payload: dict[str, Any], session_id: str, timeout: int) -> tuple[dict[str, Any], str | None]:
    headers = {**HEADERS, "Content-Type": "application/json", "X-Grafana-Session-Id": session_id}
    if url in MCP_SESSIONS:
        headers["Mcp-Session-Id"] = MCP_SESSIONS[url]
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response), response.headers.get("Mcp-Session-Id")


def rpc(url: str, name: str, arguments: dict[str, Any], session_id: str = SESSION_ID, timeout: int = 300) -> dict[str, Any]:
    try:
        if url not in MCP_SESSIONS:
            _reply, mcp_session = post(url, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "multi-document-e2e", "version": "1"}}}, session_id, 30)
            if mcp_session:
                MCP_SESSIONS[url] = mcp_session
        envelope, _ = post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}, session_id, timeout)
        return json.loads(envelope["result"]["content"][0]["text"])
    except (OSError, urllib.error.URLError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"{name} returned an invalid response") from exc


def upload(filename: str, raw: bytes, session_id: str) -> str:
    request = urllib.request.Request(
        UPLOAD_URL,
        data=raw,
        method="PUT",
        headers={**HEADERS, "X-Upload-Filename": urllib.parse.quote(filename), "X-Upload-Session-Id": session_id, "Content-Type": "application/octet-stream"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return str(json.load(response)["dataset_id"])
    except (OSError, urllib.error.URLError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f"{filename} upload failed") from exc


def delete_upload(dataset_id: str, session_id: str) -> None:
    request = urllib.request.Request(UPLOAD_URL + "/" + dataset_id, method="DELETE", headers={**HEADERS, "X-Upload-Session-Id": session_id})
    try:
        urllib.request.urlopen(request, timeout=30).close()
    except (OSError, urllib.error.URLError):
        pass


def xlsx_bytes() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet.append(["date", "value_b"])
    for day, value in [("2026-01-01", 10), ("2026-01-02", 20), ("2026-01-04", 40)]:
        sheet.append([day, value])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def document_ref(dataset_id: str, session_id: str = SESSION_ID) -> str:
    inspected = rpc(GRAFANA_QUERY_URL, "inspect_dataset", {"dataset_id": dataset_id}, session_id)
    ref = inspected.get("document_ref")
    if not inspected.get("ok") or not isinstance(ref, str):
        raise RuntimeError(f"inspection did not return document_ref: {inspected}")
    return ref


def main() -> int:
    if len(TOKEN) < 32:
        raise RuntimeError("MCP_SHARED_TOKEN is required")
    created: list[tuple[str, str]] = []
    try:
        a_id = upload("a.csv", b"date,value_a\n2026-01-01,1\n2026-01-02,2\n2026-01-03,3\n", SESSION_ID)
        created.append((a_id, SESSION_ID))
        b_id = upload("b.xlsx", xlsx_bytes(), SESSION_ID)
        created.append((b_id, SESSION_ID))
        other_id = upload("other.csv", b"date,value_b\n2026-01-01,99\n", OTHER_SESSION_ID)
        created.append((other_id, OTHER_SESSION_ID))
        a_ref, b_ref, other_ref = document_ref(a_id), document_ref(b_id), document_ref(other_id, OTHER_SESSION_ID)

        merged = rpc(SANDBOX_URL, "execute_python_preprocessing", {"document_ref": a_ref, "additional_document_refs": [b_ref], "python_code": MERGE_CODE, "seed": 42})
        inline = merged.get("output_summary", {}).get("inline_results", [])
        derived_dataset_id = merged.get("derived_dataset_id")
        if isinstance(derived_dataset_id, str):
            created.append((derived_dataset_id, SESSION_ID))
        expected = {"rows": 2, "columns": ["date", "value_a", "value_b"], "filenames": ["a.csv", "b.xlsx"], "sum_b": 30}
        if not merged.get("ok") or not inline or inline[0].get("value") != expected or not isinstance(derived_dataset_id, str):
            raise RuntimeError(f"merge did not produce the expected derived dataset: {merged}")
        provenance = merged.get("provenance", {})
        if provenance.get("additional_input_upload_ids") != [b_id] or provenance.get("input_upload_id") != a_id:
            raise RuntimeError(f"provenance does not record both inputs: {provenance}")

        derived = rpc(GRAFANA_QUERY_URL, "inspect_dataset", {"dataset_id": derived_dataset_id})
        fields = [field["name"] for field in derived.get("metadata", {}).get("fields", [])]
        if fields != ["date", "value_a", "value_b"]:
            raise RuntimeError(f"merged derived dataset is not inspectable: {derived}")

        cross = rpc(SANDBOX_URL, "execute_python_preprocessing", {"document_ref": a_ref, "additional_document_refs": [other_ref], "python_code": MERGE_CODE, "seed": 42})
        # The artifact store already refuses another session's document_ref;
        # the preprocessing same-session check is the backstop behind it.
        if cross.get("ok") or cross.get("derived_dataset_id"):
            raise RuntimeError(f"cross-session merge was not refused: {cross}")
        duplicate = rpc(SANDBOX_URL, "execute_python_preprocessing", {"document_ref": a_ref, "additional_document_refs": [a_ref], "python_code": MERGE_CODE, "seed": 42})
        if duplicate.get("ok") or "distinct" not in str(duplicate.get("error")):
            raise RuntimeError(f"duplicate document was not refused: {duplicate}")
        single = rpc(SANDBOX_URL, "execute_python_preprocessing", {"document_ref": a_ref, "python_code": "emit({'rows': len(pd.read_csv(document_path)), 'has_documents': 'documents' in globals()}, name='result.json')", "seed": 42})
        single_inline = single.get("output_summary", {}).get("inline_results", [])
        if not single.get("ok") or not single_inline or single_inline[0].get("value") != {"rows": 3, "has_documents": False}:
            raise RuntimeError(f"single-document preprocessing changed: {single}")

        print(json.dumps({"ok": True, "merged": inline[0]["value"], "derived_dataset_id": derived_dataset_id, "derived_fields": fields, "cross_session_error": cross.get("error"), "duplicate_error": duplicate.get("error")}, ensure_ascii=False, indent=2))
        return 0
    finally:
        for dataset_id, session_id in reversed(created):
            delete_upload(dataset_id, session_id)


if __name__ == "__main__":
    raise SystemExit(main())
