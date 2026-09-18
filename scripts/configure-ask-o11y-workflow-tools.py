#!/usr/bin/env python3
"""Configure Ask O11y for adaptive high-level analysis MCP tools.

Default mode is safe: build/validate the settings payload without contacting
Grafana. Use --apply with explicit Grafana auth env vars to update plugin
settings.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PLUGIN_ID = "consensys-asko11y-app"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / ".scratch" / "poc" / "ask-o11y-workflow-tools-settings.json"

SYSTEM_PROMPT = (ROOT / "ask-o11y/pkg/plugin/analyst_prompt.md").read_text(encoding="utf-8")

CAPABILITY_CONFIG = ROOT / "config" / "adaptive-mcp-capabilities.json"


def load_server_specs() -> list[dict[str, Any]]:
    try:
        raw = json.loads(CAPABILITY_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot load adaptive MCP capability config: {exc}") from exc
    servers = raw.get("servers") if isinstance(raw, dict) else None
    if not isinstance(servers, list) or any(not isinstance(item, dict) for item in servers):
        raise SystemExit("MCP capability config must define a server list")
    required = {"id", "name", "url_env", "local_url", "enabled_tools", "disabled_tools"}
    for server in servers:
        if set(server) != required or not isinstance(server["enabled_tools"], list) or not isinstance(server["disabled_tools"], list):
            raise SystemExit(f"invalid adaptive MCP capability entry: {server.get('id')}")
    ids = [str(item["id"]) for item in servers]
    if ids != ["grafana-query", "sandbox-analysis", "artifact-bridge"]:
        raise SystemExit(f"unexpected minimal runtime servers: {ids}")
    return servers


SERVER_SPECS = load_server_specs()


def tool_key(server_id: str, tool_name: str) -> str:
    return f"{server_id}_{tool_name}"


def server_url(spec: dict[str, Any], use_local_defaults: bool) -> str:
    value = os.environ.get(str(spec["url_env"]), "")
    if value:
        return value
    if use_local_defaults:
        return str(spec["local_url"])
    raise SystemExit(f"{spec['url_env']} is required (or pass --local-defaults for local development)")


def tool_selections(spec: dict[str, Any]) -> dict[str, bool]:
    selections: dict[str, bool] = {}
    for name in spec["enabled_tools"]:
        selections[name] = True
        selections[tool_key(str(spec["id"]), str(name))] = True
    for name in spec["disabled_tools"]:
        selections[name] = False
        selections[tool_key(str(spec["id"]), str(name))] = False
    return selections


TOOL_CALL_TIMEOUT_SECONDS = 3600


def build_servers(use_local_defaults: bool) -> list[dict[str, Any]]:
    return [
        {
            "id": spec["id"],
            "name": spec["name"],
            "url": server_url(spec, use_local_defaults),
            "type": "streamable-http",
            "enabled": True,
            "trusted": True,
            # Long Python analyses: upstream defaults each tool call to 30s.
            "timeoutSeconds": TOOL_CALL_TIMEOUT_SECONDS,
            "headers": {"Authorization": "", "X-Grafana-Org-Id": "", "X-Grafana-User": ""},
            "toolSelections": tool_selections(spec),
        }
        for spec in SERVER_SPECS
    ]


def build_json_data(existing: dict[str, Any], use_local_defaults: bool) -> dict[str, Any]:
    json_data = dict(existing)
    json_data.update(
        {
            "mcpServers": build_servers(use_local_defaults),
            "trustedMCPServers": {str(spec["id"]): True for spec in SERVER_SPECS},
            "useBuiltInMCP": True,
            "builtInMCPToolSelections": dict(json_data.get("builtInMCPToolSelections") or {}),
            "defaultSystemPrompt": json_data.get("defaultSystemPrompt") or SYSTEM_PROMPT,
            "maxParallelToolCalls": 1,
            "approvalPolicy": "approval-gated-writes",
        }
    )
    return json_data


def validate_payload(payload: dict[str, Any]) -> None:
    json_data = payload.get("jsonData")
    if not isinstance(json_data, dict):
        raise SystemExit("payload.jsonData is required")
    built_in_mcp = json_data.get("useBuiltInMCP")
    if not isinstance(built_in_mcp, bool) or not built_in_mcp:
        raise SystemExit("useBuiltInMCP must be true so Ask O11y retains native dynamic Grafana query/dashboard capabilities")
    if not isinstance(json_data.get("builtInMCPToolSelections"), dict):
        raise SystemExit("builtInMCPToolSelections must be an object")
    servers = json_data.get("mcpServers")
    if not isinstance(servers, list) or len(servers) != len(SERVER_SPECS):
        raise SystemExit("payload must contain the configured minimal runtime servers")
    expected_ids = {str(spec["id"]) for spec in SERVER_SPECS}
    actual_ids = {str(server.get("id")) for server in servers}
    if actual_ids != expected_ids:
        raise SystemExit(f"unexpected MCP servers: {sorted(actual_ids)}")
    for spec in SERVER_SPECS:
        server = next(item for item in servers if item.get("id") == spec["id"])
        headers = server.get("headers") if isinstance(server.get("headers"), dict) else {}
        if set(headers) != {"Authorization", "X-Grafana-Org-Id", "X-Grafana-User"} or any(headers.values()):
            raise SystemExit(f"secure MCP header placeholders missing: {spec['id']}")
        selections = server.get("toolSelections") if isinstance(server.get("toolSelections"), dict) else {}
        for name in spec["enabled_tools"]:
            direct_enabled = selections.get(name)
            prefixed_enabled = selections.get(tool_key(str(spec["id"]), str(name)))
            if not isinstance(direct_enabled, bool) or not direct_enabled or not isinstance(prefixed_enabled, bool) or not prefixed_enabled:
                raise SystemExit(f"enabled tool missing from selections: {spec['id']} {name}")
        for name in spec["disabled_tools"]:
            direct_disabled = selections.get(name)
            prefixed_disabled = selections.get(tool_key(str(spec["id"]), str(name)))
            if not isinstance(direct_disabled, bool) or direct_disabled or not isinstance(prefixed_disabled, bool) or prefixed_disabled:
                raise SystemExit(f"low-level tool not disabled: {spec['id']} {name}")
    if json_data.get("approvalPolicy") != "approval-gated-writes":
        raise SystemExit("external writes require the native approval gate")
    prompt = json_data.get("defaultSystemPrompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise SystemExit("defaultSystemPrompt must be nonempty text")


def auth_headers() -> dict[str, str]:
    token = os.environ.get("GRAFANA_TOKEN", "")
    if token:
        return {"Authorization": f"Bearer {token}"}
    user = os.environ.get("GRAFANA_USER", "")
    password = os.environ.get("GRAFANA_PASSWORD", "")
    if user and password:
        encoded = base64.b64encode(f"{user}:{password}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}
    raise SystemExit("GRAFANA_TOKEN or both GRAFANA_USER/GRAFANA_PASSWORD are required for --apply")


def secure_mcp_headers() -> dict[str, str]:
    token = os.environ.get("MCP_SHARED_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("MCP_SHARED_TOKEN with at least 32 characters is required for --apply")
    org_id = os.environ.get("ANALYSIS_SERVICE_ORG_ID", "1")
    user_id = os.environ.get("ANALYSIS_SERVICE_USER_ID", "ask-o11y")
    secure: dict[str, str] = {}
    for spec in SERVER_SPECS:
        prefix = f"mcpServerHeader.{spec['id']}."
        secure[prefix + "Authorization"] = f"Bearer {token}"
        secure[prefix + "X-Grafana-Org-Id"] = org_id
        secure[prefix + "X-Grafana-User"] = user_id
    for retired_id in ("engineering-analysis", "finance-analysis", "grafana-renderer"):
        prefix = f"mcpServerHeader.{retired_id}."
        secure[prefix + "Authorization"] = ""
        secure[prefix + "X-Grafana-Org-Id"] = ""
        secure[prefix + "X-Grafana-User"] = ""
    return secure


def apply_settings(grafana_url: str, payload: dict[str, Any], *, replace_system_prompt: bool = False) -> dict[str, Any]:
    url = grafana_url.rstrip("/") + f"/api/plugins/{PLUGIN_ID}/settings"
    headers = {"Content-Type": "application/json", **auth_headers()}
    request_payload = {**payload, "secureJsonData": secure_mcp_headers()}
    try:
        # Prompt replacement requires the operator's explicit opt-in.
        with urllib.request.urlopen(urllib.request.Request(url, headers=auth_headers()), timeout=30) as resp:
            existing = json.loads(resp.read()).get("jsonData") or {}  # null on a fresh install
        request_payload["jsonData"] = {**existing, **payload["jsonData"]}
        if "builtInMCPToolSelections" in existing:
            request_payload["jsonData"]["builtInMCPToolSelections"] = existing["builtInMCPToolSelections"]
        if existing.get("defaultSystemPrompt") and not replace_system_prompt:
            request_payload["jsonData"]["defaultSystemPrompt"] = existing["defaultSystemPrompt"]
        validate_payload(request_payload)
        req = urllib.request.Request(url, data=json.dumps(request_payload).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise SystemExit(f"Grafana settings update failed HTTP {exc.code}: {detail}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Grafana settings update failed: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-defaults", action="store_true", help="Use 127.0.0.1 MCP URLs for local development when URL env vars are unset.")
    parser.add_argument("--apply", action="store_true", help="POST the settings to Grafana; requires explicit Grafana auth env vars.")
    parser.add_argument("--replace-system-prompt", action="store_true", help="Explicitly replace the persisted system prompt with the maintained default when applying settings.")
    parser.add_argument("--grafana-url", default=os.environ.get("GRAFANA_URL", ""))
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()

    payload = {"enabled": True, "pinned": True, "jsonData": build_json_data({}, args.local_defaults)}
    validate_payload(payload)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.self_check:
        print(json.dumps({"ok": True, "settings_payload": str(args.out), "servers": [spec["id"] for spec in SERVER_SPECS], "built_in_mcp": True}, ensure_ascii=False, indent=2))
        return 0
    if args.apply:
        if not args.grafana_url:
            raise SystemExit("--grafana-url or GRAFANA_URL is required for --apply")
        result = apply_settings(args.grafana_url, payload, replace_system_prompt=args.replace_system_prompt)
        print(json.dumps({"ok": True, "settings_payload": str(args.out), "grafana_response": result}, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"ok": True, "dry_run": True, "settings_payload": str(args.out), "apply": "rerun with --apply and explicit Grafana auth env vars"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
