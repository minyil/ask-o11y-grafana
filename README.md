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
   ```
   `compose.override.yaml` serves both `dist/` directories straight into Grafana.
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
