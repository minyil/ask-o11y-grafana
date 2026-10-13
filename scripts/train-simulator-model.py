#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pandas==2.3.3", "scikit-learn==1.7.2"]
# ///
"""Train a what-if model from any CSV and export it for the asko11y-simulator-panel.

The exported JSON is self-contained (feature ranges, per-target model, cross-validated
metrics, parity samples), so the panel evaluates it in the browser while sliders move.
Training lives in ../simulator_model.py, shared with Ask O11y's build_simulator_model tool.

  uv run scripts/train-simulator-model.py data/u1_by_date.csv \
      --target 主蒸汽溫度_C --target 再熱蒸汽溫度_C \
      --feature 燃燒器角度_度 --feature 保證時段發電量_avg_MW \
      --time-column 日期 --out .scratch/simulator/u1.json --publish

--publish upserts a Grafana dashboard directly (GRAFANA_URL / GRAFANA_USER / GRAFANA_PASSWORD),
bypassing Ask O11y approval; it is for local operator use only.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import simulator_model  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("csv", type=Path)
    p.add_argument("--target", action="append", required=True, help="numeric column to predict (repeatable)")
    p.add_argument("--feature", action="append", required=True, help="numeric column exposed as a slider (repeatable)")
    p.add_argument("--model", choices=[*simulator_model.MODEL_KINDS, "auto"], default="linear",
                   help="linear = smooth ridge response (default); trees = gradient boosting; forest = random forest "
                        "(both stepwise responses); auto = whichever validates best")
    p.add_argument("--category", help="categorical column (e.g. product) shown as a selector; slider baselines follow its level")
    p.add_argument("--time-column", help="order rows by this column; validate by held-out days and forward in time")
    p.add_argument("--where", help="pandas query keeping rows before training, e.g. '運轉狀態 == 1' (backtick odd names)")
    p.add_argument("--resample", help="average rows into time windows first, e.g. 30min; needs --time-column")
    p.add_argument("--hold", help="features become the average over this trailing span (e.g. 2h), so a slider means "
                                   "'held at this level for that long'; needs --resample. Slow responses (thermal inertia) need it")
    p.add_argument("--title", help="dashboard title (default: derived from the CSV name)")
    p.add_argument("--out", type=Path, help="write the model JSON here")
    p.add_argument("--publish", action="store_true", help="upsert a Grafana dashboard with the simulator panel")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    raw = pd.read_csv(args.csv, encoding="utf-8-sig")
    try:
        spec, report = simulator_model.build_spec(
            raw, targets=args.target, features=args.feature, category=args.category, time_column=args.time_column,
            where=args.where, resample=args.resample, hold=args.hold, model=args.model,
            title=args.title, source_name=args.csv.name,
        )
    except simulator_model.SimulatorError as exc:
        raise SystemExit(str(exc)) from exc

    if len(report["candidate_mean_r2"]) > 1:
        print("model " + spec["model_kind"] + " chosen; mean validation R² " + json.dumps(report["candidate_mean_r2"]))
    for t in spec["targets"]:
        parts = []
        for key, m in t["metrics"].items():
            parts.append(f"{key} R²={m['r2']:.3f} MAE={m['mae']:.3g}")
            if "levels" in m:
                parts[-1] += " (" + ", ".join(f"{k} MAE={v['mae']:.3g} n={v['n']}" for k, v in m["levels"].items()) + ")"
        print(f"{t['name']}: " + " | ".join(parts))
    if "category" in spec:
        print("levels: " + ", ".join(f"{lv['name']} rows={lv['rows']} days={lv['days']}" for lv in spec["category"]["levels"]))
    source = spec["source"]
    print(f"rows used {source['rows']} of {source['raw_rows']} raw, dropped {source['dropped_rows']} incomplete{' windows' if args.resample else ''}")
    for warning in report["warnings"]:
        print("warning: " + warning)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(spec, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    if args.publish:
        print(f"dashboard {publish(spec)}")
    if not (args.out or args.publish):
        json.dump(spec, sys.stdout, ensure_ascii=False)
    return 0


def publish(spec: dict[str, Any]) -> str:
    base = os.environ.get("GRAFANA_URL", "http://127.0.0.1:3000").rstrip("/")
    user, password = os.environ.get("GRAFANA_USER", "admin"), os.environ.get("GRAFANA_PASSWORD", "admin")
    key = "|".join([spec["source"]["file"], *(t["name"] for t in spec["targets"])])
    uid = "sim-" + hashlib.sha256(key.encode()).hexdigest()[:12]
    dashboard = {
        "uid": uid,
        "title": spec["title"],
        "tags": ["simulator"],
        "editable": True,
        "time": {"from": "now-6h", "to": "now"},
        "timepicker": {"hidden": True},
        "panels": [{
            "id": 1,
            "type": simulator_model.PANEL_ID,
            "title": spec["title"],
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 22},
            "options": {"model": spec},
        }],
    }
    body = json.dumps({"dashboard": dashboard, "overwrite": True, "message": "train-simulator-model.py"}).encode()
    request = urllib.request.Request(
        base + "/api/dashboards/db", data=body, method="POST",
        headers={"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode(),
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"publish failed HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}") from exc
    return base + result.get("url", f"/d/{uid}")


if __name__ == "__main__":
    sys.exit(main())
