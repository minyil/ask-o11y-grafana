#!/usr/bin/env bash
# Stop the local stack. Data (Grafana DB, Redis AOF, artifacts) is kept.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
systemctl --user stop grafana-mcp@grafana-query-mcp.service grafana-mcp@sandbox-analysis-mcp.service grafana-mcp@artifact-bridge-mcp.service grafana-opensandbox.service || true
if docker ps >/dev/null 2>&1; then docker compose stop; else sg docker -c 'docker compose stop'; fi
