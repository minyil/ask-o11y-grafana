#!/usr/bin/env python3
"""Configure a local Grafana for Ask O11y analysis: the csv-poc Infinity datasource and the Grafana LLM App.

Reads GRAFANA_URL / GRAFANA_USER / GRAFANA_PASSWORD and the LLM_* variables from the environment (see .env).
Idempotent: safe to rerun after `docker compose up`.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


BASE = env("GRAFANA_URL", "http://127.0.0.1:3000").rstrip("/")
AUTH = "Basic " + base64.b64encode(f"{env('GRAFANA_USER', 'admin')}:{env('GRAFANA_PASSWORD', 'admin')}".encode()).decode()


def call(method: str, path: str, body: Any = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(BASE + path, data=data, method=method, headers={"Authorization": AUTH, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        if exc.code == 404:
            return None
        raise SystemExit(f"{method} {path} failed HTTP {exc.code}: {detail}") from exc


def ensure_csv_datasource() -> None:
    allowed = ["http://127.0.0.1:8767", "http://127.0.0.1:8772"]
    payload = {
        "name": "csv-poc",
        "uid": "csv-poc",
        "type": "yesoreyeram-infinity-datasource",
        "access": "proxy",
        "jsonData": {"allowedHosts": allowed},
    }
    existing = call("GET", "/api/datasources/uid/csv-poc")
    if existing is None:
        call("POST", "/api/datasources", payload)
        print("created Infinity datasource csv-poc")
        return
    hosts = existing.setdefault("jsonData", {}).get("allowedHosts") or []
    existing["jsonData"]["allowedHosts"] = list(dict.fromkeys([*hosts, *allowed]))
    keep = {k: existing[k] for k in ["name", "type", "access", "url", "user", "database", "basicAuth", "basicAuthUser", "withCredentials", "isDefault", "jsonData"] if k in existing}
    call("PUT", "/api/datasources/uid/csv-poc", keep)
    print("updated Infinity datasource csv-poc")


def configure_llm_app() -> None:
    url, key = env("LLM_BASE_URL"), env("LLM_API_KEY")
    base_model, large_model = env("LLM_MODEL_BASE"), env("LLM_MODEL_LARGE") or env("LLM_MODEL_BASE")
    if not (url and key and base_model):
        print("LLM_BASE_URL / LLM_API_KEY / LLM_MODEL_BASE are not all set; skipping Grafana LLM App configuration", file=sys.stderr)
        return
    current = call("GET", "/api/plugins/grafana-llm-app/settings") or {}
    json_data = dict(current.get("jsonData") or {})
    json_data.update({
        "provider": "custom",
        "openAI": {**(json_data.get("openAI") or {}), "provider": "custom", "url": url.rstrip("/"), "organizationId": ""},
        "models": {"default": "base", "mapping": {"base": base_model, "large": large_model}},
        "disabled": False,
    })
    call("POST", "/api/plugins/grafana-llm-app/settings", {"enabled": True, "pinned": True, "jsonData": json_data, "secureJsonData": {"openAIKey": key}})
    print(f"configured Grafana LLM App: provider=custom url={url} base={base_model} large={large_model}")


def main() -> int:
    health = call("GET", "/api/health")
    if not health or health.get("database") != "ok":
        raise SystemExit(f"Grafana is not healthy at {BASE}: {health}")
    ensure_csv_datasource()
    configure_llm_app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
