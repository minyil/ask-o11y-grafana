#!/usr/bin/env bash
# Bring up the local stack on a developer machine: Grafana/Redis/renderer/csv-server
# (docker compose), OpenSandbox and the three analysis MCPs (user systemd units).
# Requires .env (see README) and the user systemd units linked from config/systemd.
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
set -a
. ./.env
set +a
for var in MCP_SHARED_TOKEN ANALYSIS_SERVICE_ORG_ID ANALYSIS_SERVICE_USER_ID SANDBOX_IMAGE SANDBOX_RUNTIME_CLASS SANDBOX_SERVER_CONFIG GRAFANA_RENDERER_TOKEN; do
	: "${!var:?$var is required in .env}"
done
mkdir -p .scratch/live-services "$ANALYSIS_ARTIFACT_ROOT" "$UPLOAD_DATASET_ROOT" "$ANALYSIS_CSV_OUTPUT_DIR"

# Use sg when this shell has not picked up the docker group yet.
if docker ps >/dev/null 2>&1; then DOCKER=(docker); else DOCKER=(sg docker -c); fi
run_docker() { if [[ ${DOCKER[0]} == docker ]]; then docker "$@"; else sg docker -c "docker $*"; fi; }

run_docker compose up -d
for _ in $(seq 1 60); do
	curl -fsS http://127.0.0.1:3000/api/health 2>/dev/null | grep -q '"database": *"ok"' && break
	sleep 2
done
curl -fsS http://127.0.0.1:3000/api/health >/dev/null || { echo 'Grafana did not become healthy' >&2; exit 1; }

.venv/bin/python scripts/configure-grafana-local.py
.venv/bin/python scripts/configure-ask-o11y-workflow-tools.py --local-defaults --apply --out .scratch/ask-o11y-settings.json >/dev/null
echo 'Ask O11y settings applied.'

systemctl --user daemon-reload
systemctl --user restart grafana-opensandbox.service
systemctl --user restart grafana-mcp@grafana-query-mcp.service grafana-mcp@sandbox-analysis-mcp.service grafana-mcp@artifact-bridge-mcp.service

for endpoint in 8090 8772 8773 8777 6379 8081 8767; do
	for _ in $(seq 1 30); do
		(echo >/dev/tcp/127.0.0.1/"$endpoint") >/dev/null 2>&1 && break
		sleep 1
	done
	(echo >/dev/tcp/127.0.0.1/"$endpoint") >/dev/null 2>&1 || { echo "service on :$endpoint did not start (see .scratch/live-services/*.log)" >&2; exit 1; }
done

.venv/bin/python scripts/configure-upload-datasource.py
echo 'Local stack is running: Grafana http://localhost:3000 (admin/admin), OpenSandbox :8090, MCPs :8772 :8773 :8777, Redis :6379.'
