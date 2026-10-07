#!/usr/bin/env python3
"""Resolve authorized opaque bindings for Ask O11y's native Dashboard writer.

Analysis uses original execution output refs, without report-format adapters.
This service never chooses panels, analyzes data, or writes Grafana.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


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
ml_plotly_contract = load_module("ml_plotly_contract", ROOT / "ml_plotly_contract.py")
ml_dashboard_contract = load_module("ml_dashboard_contract", ROOT / "ml_dashboard_contract.py")
ArtifactAuthError = artifact_store.ArtifactAuthError
ArtifactStore = artifact_store.ArtifactStore
WorkflowContractError = workflow_node.WorkflowContractError
authenticate_headers = mcp_security.authenticate_headers
error_response = workflow_node.error_response
parse_artifact_ref = workflow_node.parse_artifact_ref
require_runtime_token = mcp_security.require_runtime_token
require_service_identity = mcp_security.require_service_identity
runtime_bind_host = mcp_security.runtime_bind_host
success_response = workflow_node.success_response

try:
    PORT = int(os.environ.get("ARTIFACT_BRIDGE_MCP_PORT", "8773"))
except ValueError:
    PORT = 8773
SERVER_INFO = {"name": "artifact-bridge-mcp", "version": "0.3.0"}
PROTOCOL = "2025-03-26"
ARTIFACTS = ArtifactStore(os.environ.get("ANALYSIS_ARTIFACT_ROOT", ROOT / ".analysis-artifacts" / "runs"))
ARTIFACTS.cleanup_expired()
MAX_RPC_BODY_BYTES = 512 * 1024
MAX_DASHBOARD_BYTES = 384 * 1024
MAX_PANELS = 24
MAX_TARGETS = 48
MAX_ASSET_BINDINGS = 24
ARTIFACT_PUBLIC_BASE = os.environ.get("ARTIFACT_PUBLIC_BASE", "http://127.0.0.1:8777").rstrip("/")


def context_from_headers(headers) -> dict[str, str] | None:
    org = headers.get("X-Grafana-Org-Id") or headers.get("X-Org-Id")
    user = headers.get("X-Grafana-Actor-User-Id") or headers.get("X-Grafana-User-Id") or headers.get("X-Grafana-User") or headers.get("X-Forwarded-User") or headers.get("X-User-Id")
    return {"org_id": str(org), "user_id": str(user), "session_id": str(headers.get("X-Grafana-Session-Id") or "")} if org and user else None


def inject_header_context(msg: dict[str, Any], headers) -> dict[str, Any]:
    if msg.get("method") != "tools/call":
        return msg
    params = msg.setdefault("params", {})
    if not isinstance(params, dict):
        return msg
    args = params.setdefault("arguments", {})
    if not isinstance(args, dict):
        return msg
    context = context_from_headers(headers)
    args.pop("_server_context", None)
    if context:
        args["_server_context"] = context
    return msg


def context_from_args(args: dict[str, Any]) -> dict[str, str]:
    context = args.get("_server_context")
    if not isinstance(context, dict) or not context.get("org_id") or not context.get("user_id"):
        raise PermissionError("trusted execution context is required")
    return {"org_id": str(context["org_id"]), "user_id": str(context["user_id"]), "session_id": str(context.get("session_id") or "")}


def json_clone(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WorkflowContractError("dashboard JSON is invalid") from exc


def asset_expiry() -> int:
    try:
        return int(time.time()) + ARTIFACTS.retention_seconds
    except (TypeError, ValueError) as exc:
        raise WorkflowContractError("artifact retention is invalid") from exc


def replace_asset_placeholder(value: Any, placeholder: str, asset_url: str) -> tuple[Any, int]:
    if isinstance(value, str):
        return value.replace(placeholder, asset_url), value.count(placeholder)
    if isinstance(value, list):
        count = 0
        output = []
        for item in value:
            replaced, found = replace_asset_placeholder(item, placeholder, asset_url)
            output.append(replaced)
            count += found
        return output, count
    if isinstance(value, dict):
        count = 0
        output = {}
        for key, item in value.items():
            replaced, found = replace_asset_placeholder(item, placeholder, asset_url)
            output[key] = replaced
            count += found
        return output, count
    return value, 0


def replace_plotly_placeholder(value: Any, placeholder: str, figure: dict[str, Any]) -> tuple[Any, int]:
    """Replace exact-match placeholder strings with a sanitized figure object."""
    if value == placeholder:
        return json_clone(figure), 1
    if isinstance(value, str):
        if placeholder in value:
            raise WorkflowContractError("plotly placeholder must be the entire option value, not a substring")
        return value, 0
    if isinstance(value, list):
        count = 0
        output = []
        for item in value:
            replaced, found = replace_plotly_placeholder(item, placeholder, figure)
            output.append(replaced)
            count += found
        return output, count
    if isinstance(value, dict):
        count = 0
        output = {}
        for key, item in value.items():
            replaced, found = replace_plotly_placeholder(item, placeholder, figure)
            output[key] = replaced
            count += found
        return output, count
    return value, 0


def resolve_plotly_bindings(context: dict[str, str], panel: dict[str, Any], counters: dict[str, int]) -> dict[str, Any]:
    """Resolve askO11yPlotlyBindings into static, sanitized panel options."""
    bindings = panel.pop("askO11yPlotlyBindings", [])
    if not isinstance(bindings, list):
        raise WorkflowContractError("panel plotly bindings must be an array")
    # Overall dashboard bytes/panels already bound this transport; no second chart quota.
    counters["plotly"] += len(bindings)
    for binding in bindings:
        if not isinstance(binding, dict):
            raise WorkflowContractError("plotly binding is invalid")
        if set(binding) == {"placeholder", "$execution_ref", "output_index", "plugin_id"}:
            placeholder = binding["placeholder"]
            execution_ref = binding["$execution_ref"]
            output_index = binding["output_index"]
            plugin_id = binding["plugin_id"]
            if not isinstance(execution_ref, str) or isinstance(output_index, bool) or not isinstance(output_index, int) or output_index < 0:
                raise WorkflowContractError("plotly binding requires an opaque execution ref and non-negative output index")
            if parse_artifact_ref(execution_ref)[1] != ("sandbox-execution",):
                raise WorkflowContractError("plotly binding must reference a sandbox-execution artifact")
            execution = ARTIFACTS.read_json(context, execution_ref)
            try:
                result = execution["results"][output_index]
            except (KeyError, IndexError, TypeError) as exc:
                raise WorkflowContractError("plotly output index does not exist") from exc
            sanitized = _plotly_figure(result)
        else:
            raise WorkflowContractError(f"plotly binding must have exactly the keys placeholder, $execution_ref, output_index, plugin_id; got {sorted(binding)}")
        placeholder_name = placeholder[1:] if isinstance(placeholder, str) and placeholder.startswith("$") else ""
        if not placeholder_name or not placeholder_name[0].isalpha() or not all(char.isalnum() or char in "_-" for char in placeholder_name):
            raise WorkflowContractError("plotly placeholder must be a $-prefixed identifier containing only letters, digits, underscores, or hyphens")
        if plugin_id != ml_plotly_contract.PLOTLY_PLUGIN_ID:
            raise WorkflowContractError(f"plotly binding plugin id is not the approved {ml_plotly_contract.PLOTLY_PLUGIN_ID}")
        panel, replacements = replace_plotly_placeholder(panel, placeholder, sanitized)
        if replacements == 0:
            raise WorkflowContractError("plotly placeholder is not used by the panel")
    if "$plotly_" in json.dumps(panel, ensure_ascii=False):
        raise WorkflowContractError("dashboard contains an unresolved plotly placeholder")
    # Only actual executable option slots are forbidden; words inside figure labels
    # (for example 'transcript') do not create an execution capability.
    if {"script", "onclick", "callback"} & (panel.get("options") or {}).keys():
        raise WorkflowContractError("plotly panel options must not contain executable handlers")
    if bindings and (panel.get("options") or {}).get("renderMode") != "plotly":
        raise WorkflowContractError("plotly bindings require options.renderMode='plotly'")
    return panel


def resolve_asset_bindings(context: dict[str, str], panel: dict[str, Any], counters: dict[str, int]) -> dict[str, Any]:
    if "/assets/" in json.dumps(panel, ensure_ascii=False):
        raise WorkflowContractError("model-authored dashboard must use opaque asset bindings, not asset URLs")
    bindings = panel.pop("askO11yAssetBindings", [])
    if not isinstance(bindings, list) or len(bindings) > MAX_ASSET_BINDINGS:
        raise WorkflowContractError(f"panel asset bindings must be an array with at most {MAX_ASSET_BINDINGS} entries")
    counters["assets"] += len(bindings)
    if counters["assets"] > MAX_ASSET_BINDINGS:
        raise WorkflowContractError(f"dashboard has more than {MAX_ASSET_BINDINGS} asset bindings")
    for binding in bindings:
        if not isinstance(binding, dict):
            raise WorkflowContractError("asset binding is invalid")
        if set(binding) == {"placeholder", "$execution_ref", "output_index"}:
            placeholder = binding["placeholder"]
            execution_ref = binding["$execution_ref"]
            output_index = binding["output_index"]
            if not isinstance(execution_ref, str) or isinstance(output_index, bool) or not isinstance(output_index, int) or output_index < 0:
                raise WorkflowContractError("asset binding requires an opaque execution ref and non-negative output index")
            execution = ARTIFACTS.read_json(context, execution_ref)
            try:
                result = execution["results"][output_index]
                mime = result.get("mime") if isinstance(result, dict) else None
            except (KeyError, IndexError, TypeError) as exc:
                raise WorkflowContractError("asset output index does not exist") from exc
            if not isinstance(mime, dict) or not isinstance(mime.get("image/png"), str):
                raise WorkflowContractError("dashboard asset binding currently requires image/png")
        else:
            raise WorkflowContractError(f"asset binding must have exactly the keys placeholder, $execution_ref, output_index; got {sorted(binding)}")
        if not isinstance(placeholder, str) or not placeholder.startswith("$asset_url_") or not placeholder.removeprefix("$asset_url_").replace("_", "").replace("-", "").isalnum():
            raise WorkflowContractError("asset placeholder must start with $asset_url_ and contain only letters, digits, or underscores")
        asset_url = artifact_assets.sign_output_url(
            public_base=ARTIFACT_PUBLIC_BASE,
            secret=os.environ.get("MCP_SHARED_TOKEN", ""),
            context=context,
            execution_ref=execution_ref,
            output_index=output_index,
            expires_at=asset_expiry(),
        )
        panel, replacements = replace_asset_placeholder(panel, placeholder, asset_url)
        if replacements == 0:
            raise WorkflowContractError("asset placeholder is not used by the panel")
    if "$asset_url_" in json.dumps(panel, ensure_ascii=False):
        raise WorkflowContractError("dashboard contains an unresolved asset placeholder")
    return panel


def resolve_panels(context: dict[str, str], panels: Any, counters: dict[str, int]) -> list[dict[str, Any]]:
    if not isinstance(panels, list) or len(panels) > MAX_PANELS:
        raise WorkflowContractError(f"dashboard panels must be an array with at most {MAX_PANELS} entries")
    resolved = []
    for panel in panels:
        counters["panels"] += 1
        if counters["panels"] > MAX_PANELS:
            raise WorkflowContractError(f"dashboard has more than {MAX_PANELS} panels")
        if not isinstance(panel, dict):
            raise WorkflowContractError("dashboard panel is invalid")
        asset_bindings = panel.get("askO11yAssetBindings")
        if asset_bindings and panel.get("type") != ml_dashboard_contract.PLOTLY_PLUGIN_ID:
            raise WorkflowContractError("all analysis images must use asko11y-plotly-panel")
        if asset_bindings and not panel.get("askO11yPlotlyBindings"):
            options = panel.get("options")
            placeholders = {binding.get("placeholder") for binding in asset_bindings if isinstance(binding, dict)}
            if not isinstance(options, dict) or options.get("renderMode") != "image" or options.get("fallbackUrl") not in placeholders:
                raise WorkflowContractError("image panels require options.renderMode='image' and options.fallbackUrl set to an asset placeholder")
        if panel.get("type") == "text":
            ml_dashboard_contract.validate_narrative_content((panel.get("options") or {}).get("content", ""))
        raw_item = json_clone(panel)
        nested_panels = raw_item.pop("panels", None)
        item = resolve_asset_bindings(context, raw_item, counters)
        item = resolve_plotly_bindings(context, item, counters)
        targets = item.get("targets", [])
        if not isinstance(targets, list):
            raise WorkflowContractError("dashboard panel targets must be an array")
        counters["targets"] += len(targets)
        if counters["targets"] > MAX_TARGETS:
            raise WorkflowContractError(f"dashboard has more than {MAX_TARGETS} targets")
        if any(target != {} for target in targets):
            raise WorkflowContractError("artifact-backed dashboards require empty targets; native query dashboards use the Grafana writer directly")
        item["targets"] = targets
        if nested_panels is not None:
            item["panels"] = resolve_panels(context, nested_panels, counters)
        resolved.append(item)
    return resolved


def resolve_dashboard_refs(args: dict[str, Any]) -> dict[str, Any]:
    step = "resolve_dashboard_refs"
    try:
        context = context_from_args(args)
        dashboard = args.get("dashboard")
        if not isinstance(dashboard, dict):
            raise WorkflowContractError("dashboard is required")
        if len(json.dumps(dashboard, ensure_ascii=False).encode()) > MAX_DASHBOARD_BYTES:
            raise WorkflowContractError("dashboard exceeds resolver size limit")
        output = json_clone(dashboard)
        counters = {"panels": 0, "targets": 0, "assets": 0, "plotly": 0}
        output["panels"] = resolve_panels(context, output.get("panels", []), counters)
    except (ArtifactAuthError, PermissionError) as exc:
        return error_response(step=step, error=f"unauthorized artifact access: {exc}", recoverable=False, instruction="Stop; the opaque dashboard binding is not authorized for this context.")
    except WorkflowContractError as exc:
        # Contract messages are model-correctable and safe to relay; the host forwards them verbatim.
        return error_response(step=step, error=str(exc), recoverable=True, instruction="Revise only the dashboard binding placeholders; do not rerun successful query or analysis work.")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        # Raw exception text can carry host paths; keep it private.
        return error_response(step=step, error="dashboard binding could not be resolved", recoverable=True, instruction="Check that each $execution_ref and output_index was returned by a successful analysis; do not rerun successful query or analysis work.")
    return success_response(
        step=step,
        run_id="run_" + uuid.uuid4().hex,
        refs={},
        instruction="Internal host result: dispatch the resolved dashboard only to Ask O11y's built-in Grafana MCP; never expose it to the model.",
        evidence={"resolved_panels": counters["panels"], "resolved_targets": counters["targets"], "resolved_assets": counters["assets"], "resolved_plotly": counters["plotly"]},
        dashboard=output,
    )


def _plotly_figure(result: Any) -> dict[str, Any]:
    try:
        mime = result["mime"]
        payload = mime.get("application/vnd.plotly.v1+json") or mime.get("application/json")
        figure = json.loads(payload)
    except (AttributeError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise WorkflowContractError("Plotly output is invalid") from exc
    try:
        return ml_plotly_contract.sanitize_figure(figure)
    except ValueError as exc:
        raise WorkflowContractError(str(exc)) from exc


def grant_artifact_reuse(args: dict[str, Any]) -> dict[str, Any]:
    step = "grant_artifact_reuse"
    try:
        context = context_from_args(args)
        execution_ref, target_session = args.get("execution_ref"), args.get("target_session_id")
        if set(args) - {"execution_ref", "target_session_id", "_server_context"} or not isinstance(execution_ref, str) or not isinstance(target_session, str):
            raise WorkflowContractError("pass only an execution ref and the user-specified target session")
        grant = ARTIFACTS.grant_reuse(context, execution_ref, target_session)
        run_id, _ = parse_artifact_ref(execution_ref)
        return success_response(step=step, run_id=run_id, refs={"execution_ref": execution_ref, "grant_ref": grant["grant_ref"]}, instruction="The same org/user may reuse existing results in the explicitly approved target session. Upload ownership and new execution authority did not change.", evidence=grant)
    except (PermissionError, OSError, ValueError, TypeError, WorkflowContractError) as exc:
        return error_response(step=step, error=str(exc), recoverable=False, instruction="Reuse must be granted by the original owner session after explicit approval.")


TOOLS = [{
    "name": "resolve_dashboard_refs",
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    "description": "Internal-only: resolve authorized execution output refs inside a dashboard before dispatch to Ask O11y's built-in Grafana MCP. It never chooses panels or writes Grafana.",
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"dashboard": {"type": "object"}},
        "required": ["dashboard"],
    },
}, {
    "name": "grant_artifact_reuse",
    "description": "After explicit user approval in the original source session, grant reuse of existing execution results to one user-specified session of the same org/user. Does not grant uploads, input datasets, or new computation.",
    "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
    "inputSchema": {"type": "object", "additionalProperties": False, "properties": {"execution_ref": {"type": "string"}, "target_session_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{16,128}$"}}, "required": ["execution_ref", "target_session_id"]},
}]


def rpc_result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def rpc_error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


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
        name = params.get("name")
        handlers = {
            "resolve_dashboard_refs": (resolve_dashboard_refs, {"dashboard", "_server_context"}),
            "grant_artifact_reuse": (grant_artifact_reuse, {"execution_ref", "target_session_id", "_server_context"}),
        }
        if name not in handlers:
            return rpc_error(rid, -32602, f"unknown tool: {name}")
        arguments = params.get("arguments", {}) or {}
        if not isinstance(arguments, dict):
            return rpc_error(rid, -32602, "tool arguments must be an object")
        handler, allowed = handlers[name]
        unexpected = sorted(set(arguments) - allowed)
        output = error_response(step=name, error="unsupported tool arguments: " + ", ".join(unexpected), recoverable=False, instruction="Pass only declared tool arguments.") if unexpected else handler(arguments)
        content = [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}]
        return rpc_result(rid, {"content": content, "isError": not bool(output.get("ok"))})
    return None if rid is None else rpc_error(rid, -32601, f"method not found: {method}")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, obj: Any = None):
        body = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        self._send(405, {"error": "Streamable HTTP MCP uses POST"})

    def do_DELETE(self):
        self._send(405, {"error": "sessions are stateless"})

    def do_POST(self):
        if not authenticate_headers(self.headers):
            self._send(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_RPC_BODY_BYTES:
                self._send(413, {"error": f"request body exceeds {MAX_RPC_BODY_BYTES} bytes"})
                return
            message = json.loads(self.rfile.read(length) or b"{}")
            result = handle_rpc(inject_header_context(message, self.headers))
            self._send(202 if result is None else 200, result)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})

    def log_message(self, format, *args):
        return


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    require_runtime_token()
    require_service_identity()
    server = ThreadingHTTPServer((runtime_bind_host(), PORT), Handler)
    print(f"{SERVER_INFO['name']} {SERVER_INFO['version']} on {runtime_bind_host()}:{PORT}", file=sys.stderr)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
