# Ask O11y analysis platform (Grafana fork)

Natural-language data analysis inside Grafana: a maintained fork of the Consensys
[Ask O11y](https://github.com/Consensys/ask-o11y-plugin) app plugin (`ask-o11y/`, based on upstream v0.3.16),
three Python MCP servers, an isolated OpenSandbox Python runtime and a Plotly panel.

```
Ask O11y (Grafana app, Go agent loop)
   ├─ grafana-query-mcp   :8772  authorized datasource reads → frame_ref
   ├─ sandbox-analysis-mcp:8777  LLM-written Python in a fresh OpenSandbox → figures/refs
   └─ artifact-bridge-mcp :8773  resolves figure refs for approved Dashboard writes
OpenSandbox control plane :8090 (docker runtime, execd with completion fix)
Grafana :3000 · Redis :6379 (chat sessions) · image-renderer :8081 · csv-server :8767
```

Design docs and decisions: `docs/adr/`, `docs/design/ask-o11y-minimal-source.md`, glossary in `CONTEXT.md`.

## Local development (Linux, Docker)

Prerequisites: Docker (user in the `docker` group), `uv`, Node 22+, Go 1.26 (placed at `.scratch/go/bin/go`).

1. **Secrets and settings** — copy the variables listed in `scripts/local-up.sh` into `.env` (never committed).
   Generate `MCP_SHARED_TOKEN` (≥32 chars), `GRAFANA_RENDERER_TOKEN`, `ASK_O11Y_SA_TOKEN`, and one key used for both
   `OPENSANDBOX_SERVER_API_KEY` and `SANDBOX_API_KEY`. Set `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL_BASE`,
   `LLM_MODEL_LARGE` for the Grafana LLM App (OpenAI-compatible).
2. **Build the plugins**
   ```sh
   (cd ask-o11y && npm ci --ignore-scripts) && GO_BIN=$PWD/.scratch/go/bin/go scripts/build-install-ask-o11y.sh
   (cd grafana-panels/asko11y-plotly-panel && npm ci && npm run build)
   (cd grafana-panels/asko11y-simulator-panel && npm ci && npm run build)
   ```
   `compose.override.yaml` serves the `dist/` directories straight into Grafana.
3. **Build the sandbox images**
   ```sh
   uv sync && uv tool install opensandbox-server==0.2.2
   GO=$PWD/.scratch/go/bin/go scripts/build-opensandbox-execd.sh      # prints the execd image ID
   docker build -t ask-o11y-sandbox-analysis:dev sandbox-analysis-mcp  # analysis image (pandas, sklearn, plotly…)
   ```
   Pin the execd image ID in `config/opensandbox.local.toml` (`runtime.execd_image`) and the analysis image
   digest in `.env` (`SANDBOX_IMAGE=ask-o11y-sandbox-analysis@sha256:…`).
4. **Link the user services** (once)
   ```sh
   ln -sfn "$PWD" ~/apps/grafana
   ln -sfn "$PWD/config/systemd/grafana-mcp@.service" "$PWD/config/systemd/grafana-opensandbox.service" ~/.config/systemd/user/
   systemctl --user daemon-reload && loginctl enable-linger "$USER"
   ```
5. **Start / stop**
   ```sh
   scripts/local-up.sh     # compose up, configure Grafana (datasource, LLM app, Ask O11y settings), start OpenSandbox + MCPs
   scripts/local-down.sh
   ```
   Grafana: <http://localhost:3000> (admin/admin). Logs: `.scratch/live-services/*.log`, `docker compose logs grafana`.

Chat sessions, share links and approvals live in Redis (`redis-data` volume, AOF on); Grafana's database is in the
`grafana-data` volume. Analysis artifacts and uploads are under `.analysis-artifacts/` (90-day retention).

OpenSandbox runs with `runc` here for development only; production needs gVisor/Kata and a non-loopback,
TLS asset gateway (see `sandbox-analysis-mcp/README.md`).

## What-if simulator

In Ask O11y: upload a CSV/XLSX in the chat and ask for a simulation page ("調整參數時，某溫度會上升或下降多少").
The `building-simulators` skill makes the assistant propose targets, slider features (excluding co-moving outcomes),
category, filtering, resampling and hold span, and wait for confirmation. It then trains with the sandbox tool
`build_simulator_model`, which runs the fixed code in `simulator_model.py` inside OpenSandbox, not generated Python. It
explains validation and effects, and only on request writes a dashboard: an `asko11y-simulator-panel` whose
`options.model` is a `$simulator_*` placeholder bound with `askO11ySimulatorBindings`. After write approval the
artifact bridge validates the trained model (format, structure, parity) and fills it in. Live check:
`scripts/run-simulator-e2e.py` (upload → sandbox training → bridge resolution and refusals).

Locally, without Ask O11y:

`scripts/train-simulator-model.py` fits a model on any CSV (numeric target and slider columns) and
publishes a dashboard with the `asko11y-simulator-panel`: one slider per feature, live predictions for
each target, and per-feature response curves computed in the browser.

```sh
set -a; . ./.env; set +a
uv run scripts/train-simulator-model.py data/u1_by_date.csv \
  --target 主蒸汽溫度_C --target 再熱蒸汽溫度_C \
  --feature 燃燒器角度_度 --feature 火上風門開度_FCP --feature 保證時段發電量_avg_MW \
  --feature 用煤量合計_保證 --feature 平均熱值_保證_kcalkg --feature 冷凝器出口水溫_C \
  --time-column 日期 --title "1號機 蒸汽溫度模擬" --publish
```

`--model linear` (default) gives smooth responses; `--model trees` (gradient boosting) and `--model forest`
(random forest, 100 trees, depth ≤ 8) capture non-linearity but respond in steps; `--model auto` keeps whichever
validates best. With `--time-column`, validation holds out whole days (rows of one
day share slow drifts) and also trains on the past to predict later rows; without it, a shuffled k-fold
is used. The panel shows these scores and warns when they are weak.

Minute-level process data: filter and average before training, and add a categorical column such as the
product. The panel then shows a dropdown; slider baselines and the "rarely seen" bands follow the chosen
product, and products with fewer than three days of data are flagged.

```sh
uv run scripts/train-simulator-model.py ../和平廠/4C_20260806-0909_1min_含產品.csv \
  --target 4FTI23_4C磨機出口耳軸承溫度 --feature 4FTI37_4C磨出口氣溫 --feature ... \
  --category 產品 --time-column 時間 --where '運轉狀態 == 1' --resample 30min --model trees --publish
```
Predictions describe associations in the data, not the effect of intervening on a parameter.
