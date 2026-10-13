#!/usr/bin/env python3
"""Live what-if simulator E2E: upload → build_simulator_model in OpenSandbox → bridge resolves the panel.

Uses a synthetic process dataset (two products, run-state filter, minute data) unless
SIMULATOR_E2E_CSV points at a real file, in which case SIMULATOR_E2E_ARGS (JSON) holds the
build_simulator_model arguments other than document_ref.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("multi_document_e2e", ROOT / "scripts/run-sandbox-multi-document-e2e.py")
e2e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e2e)
sys.path.insert(0, str(ROOT))
import simulator_model  # noqa: E402

SESSION_ID = "simulator-e2e-session"
BRIDGE_URL = os.environ.get("ARTIFACT_BRIDGE_MCP_URL", "http://127.0.0.1:8773/mcp")


def synthetic_csv() -> bytes:
    lines = ["時間,產品,運轉狀態,出口氣溫,擋板開度,環境溫度,軸承溫度"]
    start = datetime(2026, 8, 1)
    for minute in range(0, 6 * 24 * 60, 5):
        t = start + timedelta(minutes=minute)
        product = "IL" if t.day % 2 else "I型"
        gas = 140 + 20 * math.sin(minute / 300) + (10 if product == "I型" else 0)
        damper = 60 + 15 * math.cos(minute / 170)
        ambient = 25 + 1.5 * math.sin(minute / 1440 * 2 * math.pi)
        bearing = 40 + 0.15 * gas - 0.05 * damper + 0.8 * ambient + (2 if product == "I型" else 0)
        lines.append(f"{t:%Y-%m-%d %H:%M:%S},{product},{0 if minute % 700 < 30 else 1},{gas:.3f},{damper:.3f},{ambient:.3f},{bearing:.3f}")
    return ("\n".join(lines) + "\n").encode()


SYNTHETIC_ARGS = {
    "targets": ["軸承溫度"], "features": ["出口氣溫", "擋板開度", "環境溫度"], "category": "產品",
    "time_column": "時間", "where": "運轉狀態 == 1", "resample": "30min", "hold": "1h", "model": "auto",
    "title": "E2E 軸承溫度模擬",
}


def simulator_panel(binding: dict[str, Any], **override: Any) -> dict[str, Any]:
    panel = {
        "type": simulator_model.PANEL_ID, "title": "sim", "gridPos": {"x": 0, "y": 0, "w": 24, "h": 22}, "targets": [],
        "options": {"model": "$simulator_main"},
        "askO11ySimulatorBindings": [{"placeholder": "$simulator_main", "$execution_ref": binding["$execution_ref"],
                                      "output_index": binding["output_index"], "plugin_id": simulator_model.PANEL_ID}],
    }
    panel.update(override)
    return panel


def resolve(panels: list[dict[str, Any]]) -> dict[str, Any]:
    return e2e.rpc(BRIDGE_URL, "resolve_dashboard_refs", {"dashboard": {"title": "simulator e2e", "panels": panels}}, SESSION_ID)


def main() -> int:
    if len(e2e.TOKEN) < 32:
        raise RuntimeError("MCP_SHARED_TOKEN is required")
    path = os.environ.get("SIMULATOR_E2E_CSV")
    raw = Path(path).read_bytes() if path else synthetic_csv()
    args = json.loads(os.environ["SIMULATOR_E2E_ARGS"]) if path else SYNTHETIC_ARGS
    dataset_id = e2e.upload(Path(path).name if path else "synthetic-process.csv", raw, SESSION_ID)
    try:
        ref = e2e.document_ref(dataset_id, SESSION_ID)

        bad = e2e.rpc(e2e.SANDBOX_URL, "build_simulator_model", {**args, "document_ref": ref, "features": [*args["features"], "不存在的欄位"]}, SESSION_ID)
        if bad.get("ok") or not bad.get("recoverable") or "不存在的欄位" not in str(bad.get("error")):
            raise RuntimeError(f"unknown column was not reported as a correctable error: {bad}")

        built = e2e.rpc(e2e.SANDBOX_URL, "build_simulator_model", {**args, "document_ref": ref}, SESSION_ID, timeout=600)
        binding, report = built.get("simulator") or {}, built.get("report") or {}
        if not built.get("ok") or binding.get("plugin_id") != simulator_model.PANEL_ID or report.get("format") != simulator_model.REPORT_FORMAT:
            raise RuntimeError(f"training did not return a simulator binding and report: {built}")

        resolved = resolve([simulator_panel(binding)])
        model = ((resolved.get("dashboard") or {}).get("panels") or [{}])[0].get("options", {}).get("model")
        if not resolved.get("ok") or not isinstance(model, dict):
            raise RuntimeError(f"bridge did not resolve the simulator panel: {resolved}")
        simulator_model.validate_spec(model)
        if "askO11ySimulatorBindings" in json.dumps(resolved["dashboard"]):
            raise RuntimeError("resolved dashboard still carries bindings")

        refusals = {
            "inline model": [{**simulator_panel(binding), "options": {"model": model}, "askO11ySimulatorBindings": None}],
            "extra option": [simulator_panel(binding, options={"model": "$simulator_main", "theme": "x"})],
            "wrong output": [simulator_panel({**binding, "output_index": 0 if binding["output_index"] else 1})],
            "binding on other panel": [simulator_panel(binding, type="text")],
        }
        refusals["inline model"][0].pop("askO11ySimulatorBindings")
        for label, panels in refusals.items():
            outcome = resolve(panels)
            if outcome.get("ok"):
                raise RuntimeError(f"bridge accepted {label}")
            refusals[label] = outcome.get("error")

        print(json.dumps({
            "ok": True, "model_kind": report["model_kind"], "candidate_mean_r2": report["candidate_mean_r2"],
            "targets": [{t["name"]: {k: round(v["r2"], 3) for k, v in t["metrics"].items()}} for t in report["targets"]],
            "levels": report["levels"], "warnings": report["warnings"], "resolved_model_bytes": len(json.dumps(model)),
            "unknown_column_error": bad.get("error"), "refusals": refusals,
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        e2e.delete_upload(dataset_id, SESSION_ID)


if __name__ == "__main__":
    raise SystemExit(main())
