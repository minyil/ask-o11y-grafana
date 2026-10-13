---
name: building-simulators
description: >-
  Builds an interactive what-if simulator page from an uploaded dataset:
  sliders for adjustable parameters, live predictions of an outcome, and
  per-parameter response curves, trained by build_simulator_model and shown
  in an asko11y-simulator-panel. Use when the user wants to see how changing
  parameters would move an outcome — "simulate", "what if", "if A goes up,
  how much does the temperature change", 模擬, 調整參數 — even when they do not
  say dashboard.
metadata:
  version: "1.0"
  triggers: "模擬|what[- ]?if|simulat|調整.{0,8}(會|則|的話)|調.{0,6}(上升|下降|增加|減少)"
---

## When a simulator fits

A simulator answers "if I set these inputs, what outcome does the data suggest?" It needs an
uploaded CSV/XLSX (inspect_dataset returns its `document_ref` and columns), one or more numeric
outcomes, and numeric inputs that vary in the data. For a one-off question ("how strongly is A
related to T?") answer directly instead.

## 1. Agree on the columns before training

Propose, in the user's language, then wait for confirmation:

- **Targets** — the outcome(s) the user named.
- **Features (sliders)** — setpoints the operator can change, plus a few operating conditions
  (load, ambient temperature). Exclude:
  - other outcomes of the same process that move with the target (other temperatures, pressures
    or currents downstream of the same cause) — they make the simulation meaningless;
  - columns that are constant in the data (training rejects them);
  - duplicates of the same signal (a setpoint and its measured value — keep one).
- **Category** — a product/mode/recipe column, if present. Different products often have very
  different typical settings; without it the model mixes them up.
- **Time column, filtering and smoothing** for process/sensor data:
  - `where` keeps running rows only (e.g. `` `運轉狀態` == 1 ``);
  - `resample` (e.g. `30min`) averages minute-level rows;
  - `hold` (e.g. `2h`) makes each slider mean "held at this level for that long" — use it when
    the outcome reacts slowly (bearing or vessel temperatures, thermal inertia).

Ask about anything you cannot infer from column names; do not guess which columns are adjustable.

## 2. Train

Call `sandbox-analysis_build_simulator_model` with the confirmed arguments; keep `model: auto`
(it trains linear, gradient-boosted trees and random forest, and keeps the best-validating one)
unless the user asked for a specific model. If it returns a recoverable error, fix the argument it names
(or ask the user) and call again. Never reproduce the training with generated Python.

## 3. Explain the result before publishing

From `report`, tell the user in plain language:

- validation quality: `by_day` holds out whole days, `forward` predicts later rows from earlier
  ones, `kfold` is used when there is no time column. R² below ~0.3 means predictions for unseen
  days are weak — say so;
- the largest effects in `effects_p05_to_p95` (outcome change when one input moves from a low to
  a high typical value, others at typical values), and any that look implausible — they usually
  mean confounding, not physics;
- every entry in `warnings` (products with little data, sliders that never varied);
- that the page shows associations in historical data, not guaranteed results of intervening.

## 4. Publish (only when the user asks for the page)

Use the approval-gated `mcp-grafana_update_dashboard` with a complete dashboard. One simulator
panel per model; give it a full-width, tall grid (`w: 24`, `h: 22` or more) and no targets:

```json
{
  "type": "asko11y-simulator-panel",
  "title": "<short title>",
  "gridPos": {"x": 0, "y": 0, "w": 24, "h": 22},
  "targets": [],
  "options": {"model": "$simulator_main"},
  "askO11ySimulatorBindings": [{
    "placeholder": "$simulator_main",
    "$execution_ref": "<simulator.$execution_ref from the tool result>",
    "output_index": <simulator.output_index from the tool result>,
    "plugin_id": "asko11y-simulator-panel"
  }]
}
```

`options` must contain only `model`, set to the placeholder (it starts with `$simulator_`). The
host fills in the validated model after write approval — never copy model JSON into the
dashboard. A text panel beside it may summarise the validation and caveats. Follow the general
Preview rules (fresh UID, tags, folder and sharing explained) from the system instructions.
