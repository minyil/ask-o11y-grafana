#!/usr/bin/env python3
"""Authenticated MCP boundary for ephemeral OpenSandbox Python analysis."""
from __future__ import annotations

import argparse
import base64
import builtins
import contextlib
import csv
import hashlib
import io
import importlib.util
import json
import math
import os
import re
import sys
import threading
import time
import tomllib
import urllib.parse
import uuid
from concurrent.futures import CancelledError
from contextvars import ContextVar
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


workflow_node = load_module("workflow_node", ROOT / "workflow_node.py")
artifact_store = load_module("artifact_store", ROOT / "artifact_store.py")
mcp_security = load_module("mcp_security", ROOT / "mcp_security.py")
artifact_assets = load_module("artifact_assets", ROOT / "artifact_assets.py")
uploaded_datasets = load_module("uploaded_datasets", ROOT / "uploaded_datasets.py")
ml_plotly_contract = load_module("ml_plotly_contract", ROOT / "sandbox-analysis-mcp/ml_plotly_contract.py")
ArtifactStore = artifact_store.ArtifactStore
ArtifactAuthError = artifact_store.ArtifactAuthError
WorkflowContractError = workflow_node.WorkflowContractError
authenticate_headers = mcp_security.authenticate_headers
error_response = workflow_node.error_response
parse_artifact_ref = workflow_node.parse_artifact_ref
require_runtime_token = mcp_security.require_runtime_token
require_service_identity = mcp_security.require_service_identity
runtime_bind_host = mcp_security.runtime_bind_host
success_response = workflow_node.success_response

try:
    PORT = int(os.environ.get("SANDBOX_ANALYSIS_MCP_PORT", "8777"))
except ValueError:
    PORT = 8777

SERVER_INFO = {"name": "sandbox-analysis-mcp", "version": "0.5.0"}
PROTOCOL = "2025-03-26"
MAX_CODE_BYTES = 32 * 1024
MAX_RPC_BODY_BYTES = 128 * 1024
MAX_INPUT_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 5 * 1024 * 1024
MAX_LOG_BYTES = 256 * 1024
MAX_INLINE_RESULT_BYTES = 32 * 1024
MAX_OUTPUT_FIELDS = 200
MAX_OUTPUT_ITEMS = 64
MAX_DERIVED_ROWS = 5_000
MAX_DERIVED_BYTES = 4 * 1024 * 1024
# One primary plus at most four additional uploads (the plugin's per-session
# limit), bounded in total so a multi-file merge cannot exhaust sandbox memory.
MAX_ADDITIONAL_DOCUMENTS = 4
MAX_COMBINED_DOCUMENT_BYTES = 100 * 1024 * 1024
DERIVED_FRAME_MIME = "application/vnd.ask-o11y.dataframe+json"
DEFAULT_SEED = 42
DEFAULT_PRESENTATION_MODE = "plotly"
PRESENTATION_MODES = ("plotly", "image")
ARTIFACT_PUBLIC_BASE = os.environ.get("ARTIFACT_PUBLIC_BASE", "http://127.0.0.1:8777").rstrip("/")
ARTIFACTS = ArtifactStore(os.environ.get("ANALYSIS_ARTIFACT_ROOT", ROOT / ".analysis-artifacts" / "runs"))
ARTIFACTS.cleanup_expired()


class PendingCall:
    """One authenticated MCP call's cancellation, not an analysis workflow."""

    def __init__(self):
        self.lock = threading.Lock()
        self.requested = False
        self.attempted = False
        self.confirmed = False
        self.stop: Callable[[], None] | None = None

    def cancel(self):
        with self.lock:
            self.requested = True
            if self.stop is not None and not self.attempted:
                self.attempted = True
                try:
                    self.stop()
                    self.confirmed = True
                except Exception:
                    # A lost kill acknowledgement is not proof of termination.
                    self.confirmed = False

    def bind(self, stop: Callable[[], None]):
        with self.lock:
            self.stop = stop
        if self.requested:
            self.cancel()
        self.check()

    def check(self):
        with self.lock:
            if self.confirmed:
                raise CancelledError("owned sandbox terminated")
            if self.attempted:
                raise TimeoutError("sandbox termination is unconfirmed")

    def detach(self):
        with self.lock:
            self.stop = None


CURRENT_CALL: ContextVar[PendingCall | None] = ContextVar("mcp_call", default=None)
ACTIVE_CALLS: dict[tuple[str, ...], PendingCall] = {}
ACTIVE_CALLS_LOCK = threading.Lock()


def _plotly_capability_description() -> str:
    capability = ml_plotly_contract.capability_summary()
    return (
        " Installed native Plotly presentation: "
        + json.dumps(capability, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + ". Figure rendering errors do not invalidate computation. Respect user restrictions; do not silently drop a figure or rerun an unknown computation."
    )


TOOLS = [
    {"name": "reconcile_operation", "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}, "description": "Recover a session-owned compute receipt from durable host completion evidence without running Python again. An indeterminate status is not success and never authorizes redispatch.", "inputSchema": {"type": "object", "additionalProperties": False, "properties": {"operation_id": {"type": "string", "pattern": "^[a-f0-9]{64}$", "description": "Omit to list recent owned operation statuses after a lost response; supply an ID to read its receipt."}}}},
    {
        "name": "execute_python_analysis",
        "description": "Run generated Python in a fresh network-denied OpenSandbox over an authorized Grafana frame. Choose the method yourself; no ML or report contract is required. df, pd, np, emit(value, name=None), display and emit_frame are available. Emit Plotly figures directly, JSON summaries as *.json, and downloads as *.csv. Results include execution_ref and figure output_index for Dashboard bindings; arrays need not pass through the model. The image includes SciPy, Matplotlib, Seaborn, Plotly, scikit-learn, statsmodels, SHAP, XGBoost, LightGBM, imbalanced-learn and Optuna. Correct a completed Python failure on the same frame; never redispatch an unknown operation." + _plotly_capability_description(),
        "inputSchema": {
            "type": "object",
            "properties": {
                "frame_ref": {"type": "string", "description": "Opaque authorized grafana-frame artifact ref."},
                "python_code": {"type": "string", "maxLength": MAX_CODE_BYTES, "description": "Python source executed only inside the isolated sandbox."},
                "seed": {"type": "integer", "minimum": 0, "maximum": 4294967295, "default": DEFAULT_SEED},
                "presentation_mode": {"type": "string", "enum": list(PRESENTATION_MODES), "default": DEFAULT_PRESENTATION_MODE, "description": "Defaults to interactive Plotly; set to image only for an explicitly requested static PNG."},
            },
            "required": ["frame_ref", "python_code"],
            "additionalProperties": False,
        },
    },
    {
        "name": "execute_python_preprocessing",
        "description": "Execute generated Python over one or more authorized original uploaded CSV/XLSX documents of the same session in a fresh network-denied OpenSandbox when the user's request permits reading the original documents. The sandbox receives document_path, input_format, pd, np, emit, and emit_frame. When additional_document_refs are given it also receives documents: a list of {path, input_format, filename, sheet} for every document, primary first, so the code can read, merge, join, or append them with pandas. emit_frame returns both a derived_frame_ref and a session-owned derived_dataset_id for later Sandbox or Grafana Query steps.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "document_ref": {"type": "string", "description": "Opaque authorized uploaded-document artifact ref returned by Grafana Query inspect_dataset."},
                "additional_document_refs": {"type": "array", "maxItems": MAX_ADDITIONAL_DOCUMENTS, "uniqueItems": True, "items": {"type": "string"}, "description": "Optional document_refs of other uploads in the same session, each returned by its own inspect_dataset call. Use only when the request needs several uploaded files combined or compared."},
                "python_code": {"type": "string", "maxLength": MAX_CODE_BYTES, "description": "Python source executed only inside the isolated sandbox."},
                "seed": {"type": "integer", "minimum": 0, "maximum": 4294967295, "default": DEFAULT_SEED}
            },
            "required": ["document_ref", "python_code"],
            "additionalProperties": False
        }
    },
    {
        "name": "list_python_analyses",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        "description": "List the authenticated user's recent Sandbox Analysis revisions so a later conversation can rediscover opaque refs without raw data.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "inspect_python_analysis",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        "description": "Read one authorized Sandbox Analysis revision's generated Python, fields, output summary, and provenance for revision; never returns frame rows.",
        "inputSchema": {
            "type": "object",
            "properties": {"provenance_ref": {"type": "string", "description": "Opaque sandbox-provenance ref returned by execute/list/revise."}},
            "required": ["provenance_ref"],
            "additionalProperties": False,
        },
    },
    {
        "name": "revise_python_analysis",
        "description": "Run complete replacement Python against an earlier revision's authorized frame. Reuse saved data without re-querying. Charts are optional; returned figures use execution_ref/output_index bindings." + _plotly_capability_description(),
        "inputSchema": {
            "type": "object",
            "properties": {
                "provenance_ref": {"type": "string", "description": "Opaque prior sandbox-provenance ref."},
                "python_code": {"type": "string", "maxLength": MAX_CODE_BYTES, "description": "Complete replacement Python source."},
                "seed": {"type": "integer", "minimum": 0, "maximum": 4294967295, "default": DEFAULT_SEED},
                "presentation_mode": {"type": "string", "enum": list(PRESENTATION_MODES), "default": DEFAULT_PRESENTATION_MODE, "description": "Defaults to interactive Plotly; set to image only for an explicitly requested static PNG."},
            },
            "required": ["provenance_ref", "python_code"],
            "additionalProperties": False,
        },
    },
]


def context_from_headers(headers) -> dict[str, str] | None:
    org = headers.get("X-Grafana-Org-Id") or headers.get("X-Org-Id")
    user = headers.get("X-Grafana-Actor-User-Id") or headers.get("X-Grafana-User-Id") or headers.get("X-Grafana-User") or headers.get("X-Forwarded-User") or headers.get("X-User-Id")
    if org and user:
        return {"org_id": str(org), "user_id": str(user), "session_id": str(headers.get("X-Grafana-Session-Id") or "")}
    return None


def inject_header_context(msg: dict[str, Any], headers) -> dict[str, Any]:
    if msg.get("method") != "tools/call":
        return msg
    params = msg.setdefault("params", {})
    if not isinstance(params, dict):
        return msg
    args = params.setdefault("arguments", {})
    if not isinstance(args, dict):
        return msg
    args.pop("context", None)
    args.pop("_server_context", None)
    context = context_from_headers(headers)
    if context is not None:
        args["_server_context"] = context
    return msg


def context_from_args(args: dict[str, Any]) -> dict[str, str]:
    raw = args.get("_server_context")
    if isinstance(raw, dict) and raw.get("org_id") and raw.get("user_id"):
        return {"org_id": str(raw["org_id"]), "user_id": str(raw["user_id"]), "session_id": str(raw.get("session_id") or "")}
    raise WorkflowContractError("verified artifact context is required")


def sandbox_policy() -> dict[str, Any]:
    return {
        "timeout_seconds": 3600,
        "ready_timeout_seconds": 3600,
        "resource": {"cpu": "4", "memory": "4Gi"},
        "network_default_action": "deny",
        "env": {},
        "volumes": [],
    }


def runtime_settings() -> dict[str, str]:
    image = os.environ.get("SANDBOX_IMAGE", "").strip()
    if not image:
        raise RuntimeError("SANDBOX_IMAGE is required")
    allow_unpinned = os.environ.get("SANDBOX_ALLOW_UNPINNED_IMAGE") == "1"
    if not re.fullmatch(r"(?:[^@\s]+@)?sha256:[a-f0-9]{64}", image) and not allow_unpinned:
        raise RuntimeError("SANDBOX_IMAGE must be digest-pinned; set SANDBOX_ALLOW_UNPINNED_IMAGE=1 only for local development")
    runtime_class = os.environ.get("SANDBOX_RUNTIME_CLASS", "").strip().lower()
    if runtime_class not in {"gvisor", "kata", "firecracker"}:
        if not (runtime_class == "runc" and os.environ.get("SANDBOX_ALLOW_RUNC") == "1"):
            raise RuntimeError("SANDBOX_RUNTIME_CLASS must be gvisor, kata, or firecracker; runc requires SANDBOX_ALLOW_RUNC=1 for local development")
    config_path = Path(os.environ.get("SANDBOX_SERVER_CONFIG", ""))
    if not config_path.is_file():
        raise RuntimeError("SANDBOX_SERVER_CONFIG must reference the OpenSandbox server TOML used by the control plane")
    try:
        config_bytes = config_path.read_bytes()
        server_config = tomllib.loads(config_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(f"cannot validate SANDBOX_SERVER_CONFIG: {exc}") from exc
    configured_type = str((server_config.get("secure_runtime") or {}).get("type") or "runc").lower()
    if configured_type != runtime_class:
        raise RuntimeError(f"SANDBOX_RUNTIME_CLASS {runtime_class!r} does not match control-plane secure_runtime {configured_type!r}")
    egress = server_config.get("egress") or {}
    if egress.get("mode") != "dns+nft" or not bool(egress.get("disable_ipv6")):
        raise RuntimeError("OpenSandbox control-plane config must enforce dns+nft egress with IPv6 disabled")
    protocol = os.environ.get("SANDBOX_PROTOCOL", "http").strip().lower()
    if protocol not in {"http", "https"}:
        raise RuntimeError("SANDBOX_PROTOCOL must be http or https")
    return {
        "image": image,
        "domain": os.environ.get("SANDBOX_DOMAIN", "localhost:8080").strip(),
        "protocol": protocol,
        "runtime_class": configured_type,
        "server_config_sha256": hashlib.sha256(config_bytes).hexdigest(),
    }


def read_authorized_frame(context: dict[str, str], frame_ref: str) -> tuple[str, dict[str, Any]]:
    run_id, parts = parse_artifact_ref(frame_ref)
    if parts != ("grafana-frame",):
        raise WorkflowContractError("frame_ref must reference a grafana-frame artifact")
    frames = ARTIFACTS.read_json(context, frame_ref)
    if not isinstance(frames, list) or len(frames) != 1 or not isinstance(frames[0], dict):
        raise WorkflowContractError("grafana-frame artifact must contain exactly one frame")
    return run_id, frames[0]


def read_authorized_document(context: dict[str, str], document_ref: str) -> tuple[bytes, dict[str, Any]]:
    _run_id, parts = parse_artifact_ref(document_ref)
    if parts != ("uploaded-document",):
        raise WorkflowContractError("document_ref must reference an uploaded-document artifact")
    document = ARTIFACTS.read_json(context, document_ref)
    if not isinstance(document, dict) or not isinstance(document.get("upload_id"), str) or not isinstance(document.get("session_id"), str):
        raise WorkflowContractError("uploaded document artifact is invalid")
    path, metadata = uploaded_datasets.read_source(context, document["upload_id"], document["session_id"])
    if not 0 < path.stat().st_size <= uploaded_datasets.MAX_UPLOAD_BYTES:
        raise WorkflowContractError("uploaded document size is invalid")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != document.get("source_sha256") or metadata.get("source_format") != document.get("source_format"):
        raise WorkflowContractError("uploaded document hash/format mismatch")
    return raw, document


def validate_frame(frame: dict[str, Any]) -> tuple[list[str], int]:
    fields = frame.get("schema", {}).get("fields")
    values = frame.get("data", {}).get("values")
    if not isinstance(fields, list) or not isinstance(values, list) or len(fields) != len(values):
        raise WorkflowContractError("columnar frame fields/values must be equal-length arrays")
    names = [field.get("name") if isinstance(field, dict) else None for field in fields]
    if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
        raise WorkflowContractError("columnar frame field names must be unique non-empty strings")
    if any(not isinstance(column, list) for column in values):
        raise WorkflowContractError("columnar frame values must be arrays")
    lengths = {len(column) for column in values}
    if len(lengths) != 1 or not lengths or next(iter(lengths)) == 0:
        raise WorkflowContractError("columnar frame columns must have one shared non-zero row count")
    return [str(name) for name in names], next(iter(lengths))


def wrapped_code(python_code: str, seed: int) -> str:
    # The full traceback belongs in the authorized execution artifact; the
    # model-visible response is redacted separately in execute_python_analysis.
    return f"from capture import run\nrun({python_code!r}, '/tmp/input-frame.json', {seed})"


def document_input_path(index: int, input_format: str) -> str:
    return f"/tmp/input-document.{input_format}" if index == 0 else f"/tmp/input-document-{index}.{input_format}"


def wrapped_document_code(python_code: str, input_format: str, seed: int, documents: list[dict[str, Any]] | None = None) -> str:
    run = f"run_document({python_code!r}, {document_input_path(0, input_format)!r}, {input_format!r}, {seed})"
    if not documents or len(documents) < 2:
        return f"from capture import run_document\n{run}"
    # The deployed sandbox image's capture.run_document takes one document, so
    # the extra ones are exposed by wrapping capture.execute, which
    # run_document resolves through the module globals at call time.
    listing = [{key: item.get(key) for key in ("path", "input_format", "filename", "sheet")} for item in documents]
    return (
        "import capture\n"
        f"_documents = {listing!r}\n"
        "_execute = capture.execute\n"
        "def _execute_with_documents(code, seed, namespace, *rest):\n"
        "    namespace['documents'] = [dict(item) for item in _documents]\n"
        "    return _execute(code, seed, namespace, *rest)\n"
        "capture.execute = _execute_with_documents\n"
        f"from capture import run_document\n{run}"
    )


def serialize_execution(execution: Any) -> dict[str, Any]:
    results = []
    for result in execution.result:
        results.append({"text": result.text, "timestamp": result.timestamp, "mime": dict(result.extra_properties)})
    error = None
    if execution.error is not None:
        error = {
            "name": execution.error.name,
            "value": execution.error.value,
            "timestamp": execution.error.timestamp,
            "traceback": list(execution.error.traceback),
        }
    complete = None
    if execution.complete is not None:
        complete = {
            "timestamp": execution.complete.timestamp,
            "execution_time_in_millis": execution.complete.execution_time_in_millis,
        }
    return {
        "execution_id": execution.id,
        "execution_count": execution.execution_count,
        "exit_code": execution.exit_code,
        "results": results,
        "stdout": [{"text": item.text, "timestamp": item.timestamp} for item in execution.logs.stdout],
        "stderr": [{"text": item.text, "timestamp": item.timestamp} for item in execution.logs.stderr],
        "error": error,
        "complete": complete,
    }


def read_sandbox_bytes(filesystem: Any, path: str, range_header: str) -> bytes:
    last_error = None
    for attempt in range(30):
        try:
            return filesystem.read_bytes(path, range_header=range_header)
        except Exception as exc:
            last_error = exc
            if attempt == 29:
                raise
            time.sleep(0.1)
    if last_error is None:  # pragma: no cover
        raise RuntimeError(f"failed to read sandbox file: {path}")
    raise last_error


def read_captured_outputs(filesystem: Any) -> list[dict[str, Any]]:
    manifest_bytes = read_sandbox_bytes(filesystem, "/tmp/sandbox-output/manifest.json", f"bytes=0-{MAX_OUTPUT_BYTES}")
    if len(manifest_bytes) > MAX_OUTPUT_BYTES:
        raise WorkflowContractError("sandbox output manifest exceeds limit")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowContractError("sandbox output manifest is invalid") from exc
    if not isinstance(manifest, list) or len(manifest) > MAX_OUTPUT_ITEMS:
        raise WorkflowContractError(f"sandbox output manifest must contain at most {MAX_OUTPUT_ITEMS} items")
    allowed_mime = {"text/plain", "text/csv", "text/html", "image/png", "application/json", "application/vnd.plotly.v1+json", DERIVED_FRAME_MIME}
    outputs = []
    total = len(manifest_bytes)
    output_root = Path("/tmp/sandbox-output")
    for item in manifest:
        if not isinstance(item, dict) or set(item) != {"path", "mime_type", "display_name"}:
            raise WorkflowContractError("sandbox output manifest item is invalid")
        path = Path(str(item["path"]))
        mime_type = str(item["mime_type"])
        display_name = item["display_name"]
        if not isinstance(display_name, str) or not display_name or len(display_name) > 120:
            raise WorkflowContractError("sandbox output display name is invalid")
        if path.parent != output_root or mime_type not in allowed_mime:
            raise WorkflowContractError("sandbox output path or MIME type is not allowed")
        payload = read_sandbox_bytes(filesystem, str(path), f"bytes=0-{MAX_OUTPUT_BYTES}")
        total += len(payload)
        if total > MAX_OUTPUT_BYTES:
            raise WorkflowContractError("sandbox captured outputs exceed limit")
        if mime_type == "image/png":
            outputs.append({"text": None, "timestamp": 0, "mime": {mime_type: base64.b64encode(payload).decode("ascii")}, "display_name": display_name})
        else:
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise WorkflowContractError("sandbox text output is not UTF-8") from exc
            outputs.append({"text": text if mime_type == "text/plain" else None, "timestamp": 0, "mime": {} if mime_type == "text/plain" else {mime_type: text}, "display_name": display_name})
    return outputs


def read_captured_logs(filesystem: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    logs = []
    for name in ("stdout", "stderr"):
        payload = read_sandbox_bytes(filesystem, f"/tmp/sandbox-output/{name}.txt", f"bytes=0-{MAX_LOG_BYTES}")
        if len(payload) > MAX_LOG_BYTES:
            raise WorkflowContractError(f"sandbox {name} exceeds limit")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkflowContractError(f"sandbox {name} is not UTF-8") from exc
        logs.append([] if not text else [{"text": text, "timestamp": 0}])
    return logs[0], logs[1]


def read_input_audit(filesystem: Any) -> dict[str, Any]:
    payload = read_sandbox_bytes(filesystem, "/tmp/sandbox-output/audit.json", "bytes=0-65535")
    try:
        audit = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowContractError("sandbox input audit is invalid") from exc
    required = {"input_rows", "valid_rows", "excluded_rows", "rules"}
    if not isinstance(audit, dict) or set(audit) != required:
        raise WorkflowContractError("sandbox input audit is incomplete")
    return audit


def execute_opensandbox_input(input_path: str, input_data: str | bytes, source: str, *, capture_required: bool = True, extra_inputs: tuple[tuple[str, bytes], ...] = ()) -> dict[str, Any]:
    from code_interpreter.models.code import SupportedLanguage
    from code_interpreter.sync.code_interpreter import CodeInterpreterSync
    from opensandbox.config import ConnectionConfigSync
    from opensandbox.models.execd_sync import ExecutionHandlersSync
    from opensandbox.models.filesystem import WriteEntry
    from opensandbox.models.sandboxes import NetworkPolicy
    from opensandbox.sync.sandbox import SandboxSync
    from opensandbox.sync.manager import SandboxManagerSync

    pending = CURRENT_CALL.get()
    if pending is not None and pending.requested:
        raise CancelledError("cancelled before sandbox creation")
    settings = runtime_settings()
    policy = sandbox_policy()
    connection = ConnectionConfigSync(
        domain=settings["domain"],
        protocol=settings["protocol"],
        api_key=os.environ.get("SANDBOX_API_KEY") or None,
        request_timeout=timedelta(seconds=policy["timeout_seconds"]),
    )
    sandbox = None
    try:
        sandbox = SandboxSync.create(
            settings["image"],
            timeout=timedelta(seconds=policy["timeout_seconds"]),
            skip_health_check=True,
            env=policy["env"],
            metadata={"service": SERVER_INFO["name"]},
            resource=policy["resource"],
            network_policy=NetworkPolicy(defaultAction=policy["network_default_action"]),
            entrypoint=["/opt/code-interpreter/code-interpreter.sh"],
            volumes=policy["volumes"],
            connection_config=connection,
        )
        if pending is not None:
            def stop_owned_sandbox():
                manager = SandboxManagerSync.create(ConnectionConfigSync(
                    domain=settings["domain"], protocol=settings["protocol"],
                    api_key=os.environ.get("SANDBOX_API_KEY") or None,
                    request_timeout=timedelta(seconds=10),
                ))
                try:
                    manager.kill_sandbox(sandbox.id)
                finally:
                    manager.close()
            pending.bind(stop_owned_sandbox)
        # The SDK's blocking readiness loop swallows cancellation exceptions.
        # Bind the owned ID first; check actual health without hiding cancellation.
        deadline = time.monotonic() + policy["ready_timeout_seconds"]
        while not sandbox.is_healthy():
            if pending is not None:
                pending.check()
            if time.monotonic() >= deadline:
                raise TimeoutError("sandbox readiness timed out")
            time.sleep(0.2)
        if pending is not None:
            pending.check()
        sandbox.files.write_files([WriteEntry(path=path, data=data, mode=600) for path, data in ((input_path, input_data), *extra_inputs)])
        code_service = CodeInterpreterSync.create(sandbox=sandbox).codes
        execution = code_service.run(
            source,
            language=SupportedLanguage.PYTHON,
            handlers=ExecutionHandlersSync(skip_accumulation=capture_required),
        )
        serialized = serialize_execution(execution)
        if pending is not None:
            pending.check()
        if not capture_required:
            return serialized
        try:
            serialized["input_audit"] = read_input_audit(sandbox.files)
            serialized["stdout"], serialized["stderr"] = read_captured_logs(sandbox.files)
            serialized["results"].extend(read_captured_outputs(sandbox.files))
        except Exception:
            if execution.error is None:
                raise
        if pending is not None:
            pending.check()
        return serialized
    except Exception:
        if pending is not None:
            pending.check()
        raise
    finally:
        if pending is not None:
            pending.detach()
        if sandbox is not None:
            if pending is None or not pending.attempted:
                with contextlib.suppress(Exception):
                    sandbox.kill()
            sandbox.close()


def execute_opensandbox(frame_bundle_json: str, python_code: str, seed: int) -> dict[str, Any]:
    return execute_opensandbox_input("/tmp/input-frame.json", frame_bundle_json, wrapped_code(python_code, seed))


def execute_document_opensandbox(document_bytes: bytes, input_format: str, python_code: str, seed: int, additional: tuple[tuple[bytes, dict[str, Any]], ...] = (), primary: dict[str, Any] | None = None) -> dict[str, Any]:
    if not additional:
        return execute_opensandbox_input(document_input_path(0, input_format), document_bytes, wrapped_document_code(python_code, input_format, seed))
    primary = primary or {}
    documents = [{"path": document_input_path(0, input_format), "input_format": input_format, "filename": primary.get("filename"), "sheet": primary.get("sheet")}]
    extra_inputs = []
    for index, (raw, document) in enumerate(additional, start=1):
        path = document_input_path(index, document["source_format"])
        documents.append({"path": path, "input_format": document["source_format"], "filename": document.get("filename"), "sheet": document.get("sheet")})
        extra_inputs.append((path, raw))
    return execute_opensandbox_input(documents[0]["path"], document_bytes, wrapped_document_code(python_code, input_format, seed, documents), extra_inputs=tuple(extra_inputs))


def output_figure_summary(execution: dict[str, Any], execution_ref: str) -> list[dict[str, Any]]:
    return [
        {"$execution_ref": execution_ref, "output_index": index,
         "display_name": result.get("display_name") or f"Figure {index + 1}",
         "figure_format": ml_plotly_contract.FIGURE_FORMAT}
        for index, result in enumerate(execution.get("results", []))
        if isinstance(result, dict) and "application/vnd.plotly.v1+json" in (result.get("mime") or {})
    ]


def output_asset_summary(execution: dict[str, Any], execution_ref: str) -> list[dict[str, Any]]:
    assets = []
    for index, result in enumerate(execution.get("results", [])):
        mime = result.get("mime") if isinstance(result, dict) else None
        if not isinstance(mime, dict) or not isinstance(mime.get("image/png"), str):
            continue
        assets.append({
            "output_index": index,
            "display_name": result.get("display_name") or f"Output {index + 1}",
            "mime_type": "image/png",
            "$execution_ref": execution_ref,
        })
    return assets


def inline_result_summary(execution: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    inline_results = []
    total = 0
    omitted = False
    for index, result in enumerate(execution.get("results", [])):
        if not isinstance(result, dict):
            continue
        mime = result.get("mime")
        text = result.get("text")
        if isinstance(text, str):
            mime_type, raw = "text/plain", text
        else:
            json_data = mime.get("application/json") if isinstance(mime, dict) else None
            if not isinstance(json_data, str):
                continue
            mime_type, raw = "application/json", json_data
        size = len(raw.encode("utf-8"))
        if size > MAX_INLINE_RESULT_BYTES - total:
            omitted = True
            continue
        try:
            # Python JSON permits NaN/Infinity (e.g. a singleton group's std).
            # Represent these as missing values in strict JSON; retain raw capture.
            value = raw if mime_type == "text/plain" else json.loads(raw, parse_constant=lambda _: None)
        except (json.JSONDecodeError, TypeError):
            omitted = True
            continue
        inline_results.append({"output_index": index, "display_name": result.get("display_name") or f"Output {index + 1}", "mime_type": mime_type, "value": value})
        total += size
    return inline_results, omitted


def output_download_summary(execution: dict[str, Any], execution_ref: str, context: dict[str, str]) -> list[dict[str, Any]]:
    try:
        expires_at = int(time.time()) + int(ARTIFACTS.retention_seconds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WorkflowContractError("artifact retention is invalid") from exc
    downloads = []
    for index, result in enumerate(execution.get("results", [])):
        mime = result.get("mime") if isinstance(result, dict) else None
        csv_data = mime.get("text/csv") if isinstance(mime, dict) else None
        if not isinstance(csv_data, str) or len(csv_data.encode("utf-8")) > artifact_assets.MAX_ASSET_BYTES:
            continue
        downloads.append({
            "output_index": index,
            "display_name": result.get("display_name") or f"Output {index + 1}",
            "mime_type": "text/csv",
            "url": artifact_assets.sign_output_url(public_base=ARTIFACT_PUBLIC_BASE, secret=os.environ.get("MCP_SHARED_TOKEN", ""), context=context, execution_ref=execution_ref, output_index=index, expires_at=expires_at),
            "expires_at": expires_at,
        })
    return downloads


def parse_derived_frame(execution: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    candidates = []
    for result in execution.get("results", []):
        mime = result.get("mime") if isinstance(result, dict) else None
        payload = mime.get(DERIVED_FRAME_MIME) if isinstance(mime, dict) else None
        if isinstance(payload, str):
            candidates.append((payload, result.get("display_name") or "derived-data.csv"))
    if not candidates:
        return None
    if len(candidates) != 1:
        raise WorkflowContractError("sandbox may emit exactly one derived frame")
    raw, display_name = candidates[0]
    if len(raw.encode("utf-8")) > MAX_DERIVED_BYTES:
        raise WorkflowContractError("derived frame exceeds bounded output limit")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkflowContractError("derived frame JSON is invalid") from exc
    if not isinstance(value, dict) or set(value) != {"format", "columns", "types", "data"} or value.get("format") != "ask-o11y-dataframe-v1":
        raise WorkflowContractError("derived frame contract is invalid")
    columns, logical_types, rows = value["columns"], value["types"], value["data"]
    allowed_types = {"string", "number", "boolean", "time"}
    if not isinstance(columns, list) or not 1 <= len(columns) <= MAX_OUTPUT_FIELDS or any(not isinstance(column, str) or not column for column in columns) or len(set(columns)) != len(columns):
        raise WorkflowContractError("derived frame fields are invalid")
    if not isinstance(logical_types, list) or len(logical_types) != len(columns) or any(item not in allowed_types for item in logical_types):
        raise WorkflowContractError("derived frame types are invalid")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_DERIVED_ROWS:
        raise WorkflowContractError("derived frame rows are invalid")
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            raise WorkflowContractError("derived frame row width is invalid")
        for cell in row:
            if cell is not None and (isinstance(cell, (dict, list)) or not isinstance(cell, (str, int, float, bool)) or isinstance(cell, float) and not math.isfinite(cell)):
                raise WorkflowContractError("derived frame contains unsupported cell values")
    fields = [{"name": name, "type": logical_type} for name, logical_type in zip(columns, logical_types, strict=True)]
    values = [[row[index] for row in rows] for index in range(len(columns))]
    return {"name": str(display_name), "schema": {"fields": fields}, "data": {"values": values}}, str(display_name)


def derived_frame_csv(frame: dict[str, Any]) -> bytes:
    fields = [field["name"] for field in frame["schema"]["fields"]]
    values = frame["data"]["values"]
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(fields)
    writer.writerows(zip(*values, strict=True))
    return output.getvalue().encode("utf-8")


def persist_derived_data(context: dict[str, str], execution: dict[str, Any], output_run_id: str, document: dict[str, Any] | None = None) -> tuple[dict[str, str], dict[str, Any] | None]:
    parsed = parse_derived_frame(execution)
    if parsed is None:
        return {}, None
    frame, display_name = parsed
    frame_ref = ARTIFACTS.write_json(context, output_run_id, "grafana-frame", [frame])
    refs = {"derived_frame_ref": frame_ref}
    summary = {"derived_frame_ref": frame_ref, "fields": frame["schema"]["fields"], "rows": len(frame["data"]["values"][0])}
    if document is not None:
        filename = (Path(display_name).stem.strip(". ")[:200] or "derived-data") + ".csv"
        metadata = uploaded_datasets.store_upload(context=context, session_id=document["session_id"], filename=filename, raw=derived_frame_csv(frame), parent_upload_id=document["upload_id"])
        summary.update({"derived_dataset_id": metadata["id"], "filename": metadata["filename"], "parent_upload_id": document["upload_id"]})
    return refs, summary


def output_summary(execution: dict[str, Any]) -> dict[str, Any]:
    mime_types = set()
    output_names = []
    tabular_outputs = []
    stdout_lines = 0
    for index, result in enumerate(execution["results"]):
        if result.get("text") is not None:
            mime_types.add("text/plain")
        mime = result.get("mime", {})
        mime_types.update(str(key) for key in mime)
        if isinstance(result.get("display_name"), str):
            output_names.append(result["display_name"])
        csv_data = mime.get("text/csv") if isinstance(mime, dict) else None
        if isinstance(csv_data, str):
            reader = csv.DictReader(io.StringIO(csv_data))
            rows = list(reader)
            fields = []
            for name in (reader.fieldnames or [])[:MAX_OUTPUT_FIELDS]:
                values = [row.get(name, "") for row in rows[:100] if row.get(name, "") != ""]
                logical_type = "string"
                if values:
                    try:
                        for value in values:
                            float(value)
                        logical_type = "number"
                    except ValueError:
                        try:
                            for value in values:
                                datetime.fromisoformat(value.replace("Z", "+00:00"))
                            logical_type = "time"
                        except ValueError:
                            logical_type = "string"
                fields.append({"name": name, "type": logical_type})
            tabular_outputs.append({"output_index": index, "display_name": result.get("display_name") or f"Output {index + 1}", "row_count": len(rows), "fields": fields})
    for item in execution["stdout"]:
        stdout_lines += len(str(item.get("text", "")).splitlines())
    inline_results, inline_results_truncated = inline_result_summary(execution)
    return {
        "result_count": len(execution["results"]),
        "inline_results": inline_results,
        "inline_results_truncated": inline_results_truncated,
        "mime_types": sorted(mime_types),
        "output_names": output_names,
        "tabular_outputs": tabular_outputs,
        "stdout_lines": stdout_lines,
        "stderr_lines": sum(len(str(item.get("text", "")).splitlines()) for item in execution["stderr"]),
    }


def execute_python_analysis(
    args: dict[str, Any],
    *,
    step: str = "execute_python_analysis",
    parent_provenance_ref: str | None = None,
) -> dict[str, Any]:
    try:
        presentation_mode = args.get("presentation_mode", DEFAULT_PRESENTATION_MODE)
        if presentation_mode not in PRESENTATION_MODES:
            return error_response(step=step, error=f"presentation_mode must be one of: {', '.join(PRESENTATION_MODES)}", recoverable=False, instruction="Stop; use the default Plotly presentation or explicitly select image for a static PNG.")
        normalized_args = {**args, "presentation_mode": presentation_mode}
        context = context_from_args(normalized_args)
        inputs = {key: value for key, value in normalized_args.items() if key not in {"context", "_server_context"}}
        inputs.setdefault("seed", DEFAULT_SEED)
        return ARTIFACTS.run_once(context, step, inputs, lambda: _execute_python_analysis(normalized_args, step=step, parent_provenance_ref=parent_provenance_ref))
    except (PermissionError, WorkflowContractError, OSError, TypeError, ValueError) as exc:
        return error_response(step=step, error=str(exc), recoverable=False, instruction="Do not repeat an indeterminate operation; inspect its existing execution evidence.")


def python_error_hint(error: Any, source: str) -> dict[str, Any]:
    """Expose the exception class and generated-code locations, never row values."""
    error = error if isinstance(error, dict) else {}
    name = error.get("name")
    cls = getattr(builtins, name, None) if isinstance(name, str) else None
    name = name if isinstance(cls, type) and issubclass(cls, BaseException) else "Exception"
    trace = str(error.get("traceback") or "")
    line_numbers = set()
    for value in re.findall(r'''<generated-analysis>["']?(?::|,\s*line\s+)(\d{1,6})(?!\d)''', trace):
        try:
            line_number = int(value)
        except (TypeError, ValueError):
            continue
        if 0 < line_number <= len(source.splitlines()):
            line_numbers.add(line_number)
    return {"name": name, "line_numbers": sorted(line_numbers)}


def _execute_python_analysis(
    args: dict[str, Any],
    *,
    step: str,
    parent_provenance_ref: str | None,
) -> dict[str, Any]:
    unexpected = sorted(set(args) - {"frame_ref", "python_code", "seed", "presentation_mode", "context", "_server_context"})
    if unexpected:
        return error_response(step=step, error="unsupported tool arguments: " + ", ".join(unexpected), recoverable=False, instruction="Stop; pass only the declared opaque frame ref, Python source, and seed.")
    frame_ref = args.get("frame_ref")
    python_code = args.get("python_code")
    seed = args.get("seed", DEFAULT_SEED)
    presentation_mode = args.get("presentation_mode", DEFAULT_PRESENTATION_MODE)
    if presentation_mode not in PRESENTATION_MODES:
        return error_response(step=step, error=f"presentation_mode must be one of: {', '.join(PRESENTATION_MODES)}", recoverable=False, instruction="Stop; use the default Plotly presentation or explicitly select image for a static PNG.")
    if not isinstance(frame_ref, str):
        return error_response(step=step, error="frame_ref is required", recoverable=False, instruction="Stop; Grafana Query must return a frame_ref first.")
    if not isinstance(python_code, str) or not python_code.strip():
        return error_response(step=step, error="python_code is required", recoverable=False, instruction="Stop; provide the confirmed Python analysis source.")
    code_bytes = python_code.encode("utf-8")
    if len(code_bytes) > MAX_CODE_BYTES:
        return error_response(step=step, error=f"python_code exceeds {MAX_CODE_BYTES} bytes", recoverable=False, instruction="Stop; reduce the code to the requested analysis only.")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 4294967295:
        return error_response(step=step, error="seed must be an integer from 0 to 4294967295", recoverable=False, instruction="Stop; provide a valid deterministic seed.")
    try:
        context = context_from_args(args)
        source_run_id, frame = read_authorized_frame(context, frame_ref)
        field_names, row_count = validate_frame(frame)
        frame_bundle_json = json.dumps({"frame": frame}, ensure_ascii=False, separators=(",", ":"))
    except (PermissionError, WorkflowContractError, ValueError, TypeError) as exc:
        return error_response(step=step, error=str(exc), recoverable=False, instruction="Stop; the input frame is invalid or not authorized.")
    if len(frame_bundle_json.encode("utf-8")) > MAX_INPUT_BUNDLE_BYTES:
        return error_response(step=step, error=f"authorized input exceeds {MAX_INPUT_BUNDLE_BYTES} bytes", recoverable=False, instruction="The complete authorized dataset exceeds the configured resource limit; do not sample, truncate, or silently change its scope.")
    code_sha256 = hashlib.sha256(code_bytes).hexdigest()
    try:
        execution = execute_opensandbox(frame_bundle_json, python_code, seed)
    except CancelledError:
        return error_response(step=step, error="analysis cancelled", recoverable=False,
                              instruction="Execution was cancelled; do not automatically restart it.", evidence={"effect_outcome": "cancelled"})
    except Exception as exc:
        return error_response(step=step, error=f"sandbox execution outcome unknown: {type(exc).__name__}", recoverable=False, instruction="Reconcile the existing execution before retrying; never rerun it blindly or execute on the MCP host.", evidence={"effect_outcome": "indeterminate"})
    if not isinstance(execution, dict) or (execution.get("complete") is None and execution.get("error") is None):
        return error_response(step=step, error="sandbox completion is unavailable", recoverable=False,
                              instruction="Check the existing execution; do not dispatch replacement code while its outcome is unknown.",
                              evidence={"effect_outcome": "indeterminate"})
    encoded_execution = json.dumps(execution, ensure_ascii=False).encode("utf-8")
    if len(encoded_execution) > MAX_OUTPUT_BYTES:
        return error_response(step=step, error=f"sandbox output exceeds {MAX_OUTPUT_BYTES} bytes", recoverable=False, instruction="Stop; request smaller displayed outputs.")
    validity = execution.get("input_audit")
    audit_counts = [validity.get(key) for key in ("input_rows", "valid_rows", "excluded_rows")] if isinstance(validity, dict) else []
    if (
        not isinstance(validity, dict)
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in audit_counts)
        or validity.get("input_rows") != row_count
        or validity.get("valid_rows", 0) + validity.get("excluded_rows", 0) != row_count
        or validity.get("rules") != []
    ):
        return error_response(step=step, error="sandbox execution returned an invalid trusted input audit", recoverable=False, instruction="Stop; do not trust outputs without host-verified validity evidence.")
    captured_results_sha256 = hashlib.sha256(json.dumps(execution.get("results", []), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    settings = runtime_settings()
    summary = output_summary(execution)
    summary["presentation_mode"] = presentation_mode
    output_run_id = ARTIFACTS.create_run(context)
    code_ref = ARTIFACTS.write_json(context, output_run_id, "sandbox-code", {"sha256": code_sha256, "source": python_code})
    provenance = {
        "runtime": "opensandbox",
        "runtime_class": settings["runtime_class"],
        "server_config_sha256": settings.get("server_config_sha256"),
        "image": settings["image"],
        "code_sha256": code_sha256,
        "code_ref": code_ref,
        "input_frame_ref": frame_ref,
        "executor_kind": step,
        "presentation_mode": presentation_mode,
        "trusted_ml_contract": False,
        "figure_format": ml_plotly_contract.FIGURE_FORMAT,
        "input_frame_sha256": hashlib.sha256(json.dumps(frame, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
        "input_fields": field_names,
        "seed": seed,
        "network": "deny",
        "resource": sandbox_policy()["resource"],
        "limits": {
            "timeout_seconds": sandbox_policy()["timeout_seconds"],
            "source_bytes": MAX_CODE_BYTES,
            "rpc_body_bytes": MAX_RPC_BODY_BYTES,
            "input_bundle_bytes": MAX_INPUT_BUNDLE_BYTES,
            "captured_execution_bytes": MAX_OUTPUT_BYTES,
            "stdout_stderr_bytes_each": MAX_LOG_BYTES,
            "inline_result_bytes": MAX_INLINE_RESULT_BYTES,
            "output_fields": MAX_OUTPUT_FIELDS,
            "signed_download_bytes": artifact_assets.MAX_ASSET_BYTES,
        },
        "validity": validity,

        "output_summary": summary,
        "parent_provenance_ref": parent_provenance_ref,
        "captured_results_sha256": captured_results_sha256,

    }
    execution_ref = ARTIFACTS.write_json(context, output_run_id, "sandbox-execution", execution)
    execution_error = execution.get("error")
    provenance["computation_status"] = "failed" if execution_error else "succeeded"
    summary["computation_status"] = provenance["computation_status"]
    summary["figures"] = output_figure_summary(execution, execution_ref)
    summary["assets"] = output_asset_summary(execution, execution_ref)
    summary["downloads"] = output_download_summary(execution, execution_ref, context)
    try:
        derived_refs, derived_summary = ({}, None) if execution_error else persist_derived_data(context, execution, output_run_id)
    except (OSError, PermissionError, ValueError, WorkflowContractError) as exc:
        return error_response(step=step, error=f"derived frame rejected: {exc}", recoverable=False, instruction="Stop; the Sandbox output did not satisfy the derived-data contract.", evidence={"execution_ref": execution_ref})
    if derived_summary is not None:
        summary["derived_data"] = derived_summary
    provenance_ref = ARTIFACTS.write_json(context, output_run_id, "sandbox-provenance", provenance)
    refs = {"execution_ref": execution_ref, "provenance_ref": provenance_ref, **derived_refs}
    if execution_error:
        hint = python_error_hint(execution_error, python_code)
        return error_response(
            step=step, error=f"Python {hint['name']} at generated code lines {hint['line_numbers']}",
            recoverable=True,
            instruction="Python has finished with an error. Reuse the same frame_ref with corrected complete Python; do not query again. Raw exception values remain private.",
            evidence={"refs": refs, "frame_ref": frame_ref, "code_sha256": code_sha256, "python_error": hint},
        )
    return success_response(
        step=step,
        run_id=output_run_id,
        refs=refs,
        instruction="Explain the observed results and limitations in the user's language. Reference saved figures by execution_ref and output_index; never copy their arrays. A Dashboard exists only after an authorized writer confirms it.",
        evidence={"input_rows": row_count},
        output_summary=summary,
        provenance={key: value for key, value in provenance.items() if key != "code_ref"},
    )


def execute_python_preprocessing(args: dict[str, Any]) -> dict[str, Any]:
    step = "execute_python_preprocessing"
    try:
        context = context_from_args(args)
        inputs = {key: value for key, value in args.items() if key not in {"context", "_server_context"}}
        inputs.setdefault("seed", DEFAULT_SEED)
        return ARTIFACTS.run_once(context, step, inputs, lambda: _execute_python_preprocessing(args))
    except (PermissionError, WorkflowContractError, OSError, TypeError, ValueError) as exc:
        return error_response(step=step, error=str(exc), recoverable=False, instruction="Reconcile any pending document operation before retrying.")


def _execute_python_preprocessing(args: dict[str, Any]) -> dict[str, Any]:
    step = "execute_python_preprocessing"
    unexpected = sorted(set(args) - {"document_ref", "additional_document_refs", "python_code", "seed", "context", "_server_context"})
    if unexpected:
        return error_response(step=step, error="unsupported tool arguments: " + ", ".join(unexpected), recoverable=False, instruction="Stop; pass only the declared document refs, Python source, and seed.")
    document_ref = args.get("document_ref")
    additional_refs = args.get("additional_document_refs") or []
    python_code = args.get("python_code")
    seed = args.get("seed", DEFAULT_SEED)
    if not isinstance(document_ref, str):
        return error_response(step=step, error="document_ref is required", recoverable=False, instruction="Stop; inspect an authorized uploaded dataset first.")
    if (not isinstance(additional_refs, list) or len(additional_refs) > MAX_ADDITIONAL_DOCUMENTS
            or any(not isinstance(ref, str) or not ref for ref in additional_refs)
            or len(set(additional_refs)) != len(additional_refs) or document_ref in additional_refs):
        return error_response(step=step, error=f"additional_document_refs must be up to {MAX_ADDITIONAL_DOCUMENTS} distinct document_refs other than document_ref", recoverable=False, instruction="Stop; inspect each uploaded dataset once and pass each returned document_ref once.")
    if not isinstance(python_code, str) or not python_code.strip():
        return error_response(step=step, error="python_code is required", recoverable=False, instruction="Stop; provide the confirmed Python preprocessing source.")
    code_bytes = python_code.encode("utf-8")
    if len(code_bytes) > MAX_CODE_BYTES:
        return error_response(step=step, error=f"python_code exceeds {MAX_CODE_BYTES} bytes", recoverable=False, instruction="Stop; reduce the code to the requested preprocessing only.")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 4294967295:
        return error_response(step=step, error="seed must be an integer from 0 to 4294967295", recoverable=False, instruction="Stop; provide a valid deterministic seed.")
    try:
        context = context_from_args(args)
        document_bytes, document = read_authorized_document(context, document_ref)
        input_format = document.get("source_format")
        if input_format not in {"csv", "xlsx"}:
            raise WorkflowContractError("uploaded document format is unsupported")
        additional = tuple(read_authorized_document(context, ref) for ref in additional_refs)
        upload_ids = [document["upload_id"], *(extra["upload_id"] for _raw, extra in additional)]
        if len(set(upload_ids)) != len(upload_ids):
            raise WorkflowContractError("each uploaded document may be passed only once")
        for _raw, extra in additional:
            # Each document is already owner-checked; one sandbox run must also
            # stay inside one conversation's uploads.
            if extra["session_id"] != document["session_id"]:
                raise WorkflowContractError("all uploaded documents must belong to the same session")
            if extra.get("source_format") not in {"csv", "xlsx"}:
                raise WorkflowContractError("uploaded document format is unsupported")
        if len(document_bytes) + sum(len(raw) for raw, _extra in additional) > MAX_COMBINED_DOCUMENT_BYTES:
            raise WorkflowContractError(f"uploaded documents exceed {MAX_COMBINED_DOCUMENT_BYTES // (1024 * 1024)} MB combined")
    except (PermissionError, OSError, WorkflowContractError, ValueError, TypeError) as exc:
        return error_response(step=step, error=str(exc), recoverable=False, instruction="Stop; the uploaded document is invalid or unauthorized.")
    code_sha256 = hashlib.sha256(code_bytes).hexdigest()
    try:
        execution = execute_document_opensandbox(document_bytes, input_format, python_code, seed, additional, document)
    except CancelledError:
        return error_response(step=step, error="preprocessing cancelled", recoverable=False,
                              instruction="Execution was cancelled; do not automatically restart it.", evidence={"effect_outcome": "cancelled"})
    except Exception as exc:
        return error_response(step=step, error=f"sandbox execution outcome unknown: {type(exc).__name__}", recoverable=False, instruction="Reconcile the existing execution before retrying.", evidence={"effect_outcome": "indeterminate"})
    if not isinstance(execution, dict) or (execution.get("complete") is None and execution.get("error") is None):
        return error_response(step=step, error="sandbox completion is unavailable", recoverable=False,
                              instruction="Reconcile the existing execution before retrying.", evidence={"effect_outcome": "indeterminate"})
    if len(json.dumps(execution, ensure_ascii=False).encode("utf-8")) > MAX_OUTPUT_BYTES:
        return error_response(step=step, error=f"sandbox output exceeds {MAX_OUTPUT_BYTES} bytes", recoverable=False, instruction="Stop; request smaller displayed outputs.")
    validity = execution.get("input_audit")
    if validity != {"input_rows": 0, "valid_rows": 0, "excluded_rows": 0, "rules": []}:
        return error_response(step=step, error="sandbox execution returned an invalid trusted document audit", recoverable=False, instruction="Stop; do not trust outputs without host-verified input evidence.")
    settings = runtime_settings()
    summary = output_summary(execution)
    output_run_id = ARTIFACTS.create_run(context)
    code_ref = ARTIFACTS.write_json(context, output_run_id, "sandbox-code", {"sha256": code_sha256, "source": python_code})
    provenance = {
        "runtime": "opensandbox",
        "runtime_class": settings["runtime_class"],
        "server_config_sha256": settings.get("server_config_sha256"),
        "image": settings["image"],
        "code_sha256": code_sha256,
        "code_ref": code_ref,
        "input_document_ref": document_ref,
        "input_upload_id": document["upload_id"],
        "input_format": input_format,
        "additional_input_document_refs": list(additional_refs),
        "additional_input_upload_ids": [extra["upload_id"] for _raw, extra in additional],
        "input_fields": [],
        "seed": seed,
        "network": "deny",
        "resource": sandbox_policy()["resource"],
        "limits": {"timeout_seconds": sandbox_policy()["timeout_seconds"], "source_bytes": MAX_CODE_BYTES, "document_bytes": uploaded_datasets.MAX_UPLOAD_BYTES, "captured_execution_bytes": MAX_OUTPUT_BYTES, "derived_frame_bytes": MAX_DERIVED_BYTES, "derived_rows": MAX_DERIVED_ROWS, "derived_fields": MAX_OUTPUT_FIELDS},
        "validity": validity,
        "output_summary": summary,
        "parent_provenance_ref": None,
    }
    execution_ref = ARTIFACTS.write_json(context, output_run_id, "sandbox-execution", execution)
    summary["figures"] = output_figure_summary(execution, execution_ref)
    provenance["trusted_ml_contract"] = False
    provenance["figure_format"] = ml_plotly_contract.FIGURE_FORMAT
    provenance["computation_status"] = "failed" if execution.get("error") else "succeeded"
    summary["computation_status"] = provenance["computation_status"]
    summary["assets"] = output_asset_summary(execution, execution_ref)
    summary["downloads"] = output_download_summary(execution, execution_ref, context)
    execution_error = execution.get("error")
    try:
        derived_refs, derived_summary = ({}, None) if execution_error else persist_derived_data(context, execution, output_run_id, document)
    except (OSError, PermissionError, ValueError, WorkflowContractError) as exc:
        return error_response(step=step, error=f"derived frame rejected: {exc}", recoverable=False, instruction="Stop; the Sandbox output did not satisfy the derived-data contract.", evidence={"execution_ref": execution_ref})
    if derived_summary is not None:
        summary["derived_data"] = derived_summary
    provenance_ref = ARTIFACTS.write_json(context, output_run_id, "sandbox-provenance", provenance)
    refs = {"execution_ref": execution_ref, "provenance_ref": provenance_ref, **derived_refs}
    if execution_error:
        hint = python_error_hint(execution_error, python_code)
        return error_response(step=step, error=f"Python {hint['name']} at generated code lines {hint['line_numbers']}", recoverable=True,
                              instruction="Python has finished with an error. Reuse the same document_ref and additional_document_refs with corrected complete code; raw exception values remain private.",
                              evidence={"refs": refs, "document_ref": document_ref, "additional_document_refs": list(additional_refs), "code_sha256": code_sha256, "python_error": hint})
    return success_response(
        step=step,
        run_id=output_run_id,
        refs=refs,
        instruction="Reuse these outputs or derived_frame_ref within this session. Python outputs are not certified ML. Dashboard creation still requires authorized writing.",
        evidence={"input_format": input_format, "source_sha256": document.get("source_sha256"), "additional_source_sha256": [extra.get("source_sha256") for _raw, extra in additional]},
        output_summary=summary,
        derived_frame_ref=derived_refs.get("derived_frame_ref"),
        derived_dataset_id=derived_summary.get("derived_dataset_id") if derived_summary else None,
        provenance={key: value for key, value in provenance.items() if key != "code_ref"},
    )


def read_provenance(context: dict[str, str], provenance_ref: str) -> dict[str, Any]:
    _run_id, parts = parse_artifact_ref(provenance_ref)
    if parts != ("sandbox-provenance",):
        raise WorkflowContractError("provenance_ref must reference sandbox-provenance")
    provenance = ARTIFACTS.read_json(context, provenance_ref)
    input_refs = [provenance.get("input_frame_ref"), provenance.get("input_document_ref")] if isinstance(provenance, dict) else []
    if not isinstance(provenance, dict) or sum(isinstance(value, str) for value in input_refs) != 1 or not isinstance(provenance.get("code_ref"), str):
        raise WorkflowContractError("sandbox provenance artifact is incomplete")
    return provenance


def list_python_analyses(args: dict[str, Any]) -> dict[str, Any]:
    unexpected = sorted(set(args) - {"context", "_server_context"})
    if unexpected:
        return error_response(step="list_python_analyses", error="unsupported tool arguments: " + ", ".join(unexpected), recoverable=False, instruction="Stop; this tool accepts no model-provided arguments.")
    try:
        context = context_from_args(args)
        refs = ARTIFACTS.list_refs(context, "sandbox-provenance", limit=20)
        analyses = []
        for ref in refs:
            provenance = read_provenance(context, ref)
            analyses.append({
                "provenance_ref": ref,
                "code_sha256": provenance.get("code_sha256"),
                "input_fields": provenance.get("input_fields", []),
                "output_summary": provenance.get("output_summary", {}),
                "computation_status": provenance.get("computation_status"),
                "parent_provenance_ref": provenance.get("parent_provenance_ref"),
            })
    except (PermissionError, WorkflowContractError, OSError) as exc:
        return error_response(step="list_python_analyses", error=str(exc), recoverable=False, instruction="Stop; recent analyses could not be listed securely.")
    return success_response(step="list_python_analyses", run_id="run_listing", instruction="Use inspect_python_analysis on the matching revision before generating replacement code.", analyses=analyses)


def inspect_python_analysis(args: dict[str, Any]) -> dict[str, Any]:
    unexpected = sorted(set(args) - {"provenance_ref", "context", "_server_context"})
    if unexpected:
        return error_response(step="inspect_python_analysis", error="unsupported tool arguments: " + ", ".join(unexpected), recoverable=False, instruction="Stop; pass only provenance_ref.")
    provenance_ref = args.get("provenance_ref")
    if not isinstance(provenance_ref, str):
        return error_response(step="inspect_python_analysis", error="provenance_ref is required", recoverable=False, instruction="Stop; list recent Sandbox analyses first.")
    try:
        context = context_from_args(args)
        provenance = read_provenance(context, provenance_ref)
        code = ARTIFACTS.read_json(context, provenance["code_ref"])
        if not isinstance(code, dict) or not isinstance(code.get("source"), str):
            raise WorkflowContractError("sandbox code artifact is incomplete")
    except (PermissionError, WorkflowContractError) as exc:
        return error_response(step="inspect_python_analysis", error=str(exc), recoverable=False, instruction="Stop; the analysis revision is invalid or unauthorized.")
    return success_response(
        step="inspect_python_analysis",
        run_id=parse_artifact_ref(provenance_ref)[0],
        instruction="Inspect the retained source and outputs; raw input rows/document bytes are intentionally not returned.",
        python_code=code["source"],
        provenance_ref=provenance_ref,
        code_sha256=provenance.get("code_sha256"),
        input_fields=provenance.get("input_fields", []),
        output_summary=provenance.get("output_summary", {}),
        computation_status=provenance.get("computation_status"),
        parent_provenance_ref=provenance.get("parent_provenance_ref"),
    )


def revise_python_analysis(args: dict[str, Any]) -> dict[str, Any]:
    unexpected = sorted(set(args) - {"provenance_ref", "python_code", "seed", "presentation_mode", "context", "_server_context"})
    if unexpected:
        return error_response(step="revise_python_analysis", error="unsupported tool arguments: " + ", ".join(unexpected), recoverable=False, instruction="Stop; pass only provenance_ref, replacement Python, and seed.")
    provenance_ref = args.get("provenance_ref")
    if not isinstance(provenance_ref, str):
        return error_response(step="revise_python_analysis", error="provenance_ref is required", recoverable=False, instruction="Stop; list and inspect a prior revision first.")
    try:
        context = context_from_args(args)
        provenance = read_provenance(context, provenance_ref)
    except (PermissionError, WorkflowContractError) as exc:
        return error_response(step="revise_python_analysis", error=str(exc), recoverable=False, instruction="Stop; the prior revision is invalid or unauthorized.")
    if not isinstance(provenance.get("input_frame_ref"), str):
        return error_response(step="revise_python_analysis", error="prior revision used a document input", recoverable=False, instruction="Call execute_python_preprocessing with the retained input_document_ref and complete replacement code.")
    return execute_python_analysis(
        {"frame_ref": provenance["input_frame_ref"], "python_code": args.get("python_code"), "seed": args.get("seed", DEFAULT_SEED), "presentation_mode": args.get("presentation_mode", DEFAULT_PRESENTATION_MODE), "_server_context": context},
        step="revise_python_analysis",
        parent_provenance_ref=provenance_ref,
    )


def rpc_result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def rpc_error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def reconcile_operation(args: dict[str, Any]) -> dict[str, Any]:
    try:
        context = context_from_args(args)
        if set(args) - {"operation_id", "_server_context", "context"}:
            raise WorkflowContractError("unsupported reconciliation arguments")
        operation_id = args.get("operation_id")
        if operation_id is None:
            statuses = ARTIFACTS.operation_statuses(context)
            return {"ok": True, "step": "reconcile_operation", "operations": statuses[:20], "has_more": len(statuses) > 20,
                    "instruction": "Read an operation by ID for its receipt. No computation was dispatched; unresolved work still blocks new computation in this session."}
        if not isinstance(operation_id, str):
            raise WorkflowContractError("operation_id must be a string")
        reconciled = ARTIFACTS.reconcile_operation(context, operation_id)
        return {"ok": True, "step": "reconcile_operation", **reconciled,
                "instruction": "Completed/failed/cancelled receipts are final evidence. Indeterminate remains blocked; no compute was dispatched."}
    except (PermissionError, WorkflowContractError, OSError, ValueError) as exc:
        return error_response(step="reconcile_operation", error=str(exc), recoverable=False, instruction="Use only an operation ID returned to this authenticated session; never invent completion evidence.")


def handle_rpc(msg: dict[str, Any]):
    method, rid = msg.get("method", ""), msg.get("id")
    if method == "initialize":
        return rpc_result(rid, {"protocolVersion": PROTOCOL, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO})
    if method == "ping":
        return rpc_result(rid, {})
    if method == "tools/list":
        return rpc_result(rid, {"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params", {})
        name = params.get("name", "")
        tool = next((item for item in TOOLS if item["name"] == name), None)
        if tool is None:
            return rpc_error(rid, -32602, f"unknown tool: {name}")
        arguments = params.get("arguments", {}) or {}
        if not isinstance(arguments, dict):
            return rpc_error(rid, -32602, "tool arguments must be an object")
        allowed = set(tool["inputSchema"]["properties"]) | {"context", "_server_context"}
        unexpected = sorted(set(arguments) - allowed)
        if unexpected:
            out = error_response(step=name, error="unsupported tool arguments: " + ", ".join(unexpected), recoverable=False, instruction="Stop; pass only arguments declared by this tool schema.")
        else:
            handlers = {
                "reconcile_operation": reconcile_operation,
                "execute_python_analysis": execute_python_analysis,
                "execute_python_preprocessing": execute_python_preprocessing,
                "list_python_analyses": list_python_analyses,
                "inspect_python_analysis": inspect_python_analysis,
                "revise_python_analysis": revise_python_analysis,
            }
            out = handlers[name](arguments)
        return rpc_result(rid, {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}], "isError": not out.get("ok", False)})
    if rid is None:
        return None
    return rpc_error(rid, -32601, f"method not found: {method}")


def handle_http_rpc(message: Any, headers, context: dict[str, str]):
    if not isinstance(message, dict) or not isinstance(message.get("params", {}), dict):
        return rpc_error(None, -32600, "invalid JSON-RPC request")
    params = message.get("params", {})
    method = message.get("method")
    request_id = params.get("requestId") if method == "notifications/cancelled" else message.get("id")
    session = str(headers.get("Mcp-Session-Id") or "")
    key = tuple(context.get(field, "") for field in ("org_id", "user_id", "session_id")) + (session, json.dumps(request_id))
    if method == "notifications/cancelled":
        with ACTIVE_CALLS_LOCK:
            pending = ACTIVE_CALLS.get(key)
        if pending is not None:
            pending.cancel()
        return None
    if method != "tools/call" or params.get("name") not in {"execute_python_analysis", "execute_python_preprocessing", "revise_python_analysis"}:
        return handle_rpc(inject_header_context(message, headers))
    if not re.fullmatch(r"[a-f0-9]{32}", session) or request_id is None or not context.get("session_id"):
        return rpc_error(message.get("id"), -32600, "initialize an MCP session and supply the authenticated application session before execution")
    pending = PendingCall()
    with ACTIVE_CALLS_LOCK:
        if key in ACTIVE_CALLS:
            return rpc_error(message.get("id"), -32600, "request is already active")
        ACTIVE_CALLS[key] = pending
    token = CURRENT_CALL.set(pending)
    try:
        return handle_rpc(inject_header_context(message, headers))
    finally:
        CURRENT_CALL.reset(token)
        with ACTIVE_CALLS_LOCK:
            ACTIVE_CALLS.pop(key, None)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, obj: Any = None, *, session_id: str | None = None):
        body = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        if session_id is not None:
            self.send_header("Mcp-Session-Id", session_id)
        if obj is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            # The cancelled caller may be gone; its durable receipt was already saved.
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/assets/"):
            token = self.path.removeprefix("/assets/").split("?", 1)[0]
            try:
                data, mime_type, _display_name = artifact_assets.read_signed_output(token, secret=os.environ.get("MCP_SHARED_TOKEN", ""), artifacts=ARTIFACTS)
            except (PermissionError, OSError, ValueError):
                return self._send(403, {"error": "artifact URL is invalid or expired"})
            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            if mime_type == "text/csv":
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(_display_name, safe=""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=300")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Access-Control-Allow-Origin", os.environ.get("GRAFANA_PUBLIC_ORIGIN", "http://localhost:3000"))
            self.end_headers()
            self.wfile.write(data)
            return
        self._send(405 if self.path.rstrip("/") == "/mcp" else 404, {"error": "POST JSON-RPC to /mcp"})

    def do_DELETE(self):
        self._send(200, {"ok": True})

    def do_POST(self):
        if self.path.rstrip("/") != "/mcp":
            return self._send(404, {"error": "not found"})
        context = authenticate_headers(self.headers)
        if context is None:
            return self._send(401, {"error": "authenticated MCP service identity is required"})
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return self._send(400, rpc_error(None, -32700, "invalid Content-Length"))
        if content_length < 0 or content_length > MAX_RPC_BODY_BYTES:
            return self._send(413, rpc_error(None, -32000, f"request body exceeds {MAX_RPC_BODY_BYTES} bytes"))
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return self._send(400, rpc_error(None, -32700, "parse error"))
        messages = payload if isinstance(payload, list) else [payload]
        replies = [reply for message in messages if (reply := handle_http_rpc(message, self.headers, context)) is not None]
        if not replies:
            return self._send(202)
        session_id = uuid.uuid4().hex if isinstance(payload, dict) and payload.get("method") == "initialize" else None
        self._send(200, replies if isinstance(payload, list) else replies[0], session_id=session_id)

    def log_message(self, format, *args):  # noqa: A002
        sys.stderr.write("sandbox-analysis-mcp " + format % args + "\n")


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    require_runtime_token()
    require_service_identity()
    runtime_settings()
    bind_host = runtime_bind_host()
    print(f"{SERVER_INFO['name']} {SERVER_INFO['version']} on {bind_host}:{PORT}", file=sys.stderr)
    ThreadingHTTPServer((bind_host, PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
