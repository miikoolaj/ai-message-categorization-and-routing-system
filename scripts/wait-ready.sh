#!/usr/bin/env bash
# Wait until the API reports readiness (Ollama + model + SMTP reachable).
# Usage: scripts/wait-ready.sh [timeout_seconds=900]
set -euo pipefail

TIMEOUT="${1:-900}"
URL="${API_URL:-http://127.0.0.1:${API_PORT:-8080}}/api/v1/health/ready"
start=$(date +%s)

echo "Waiting for ${URL} (timeout ${TIMEOUT}s)..."
while true; do
  if curl -fsS --max-time 5 "$URL" >/dev/null 2>&1; then
    echo "API is ready after $(( $(date +%s) - start ))s."
    exit 0
  fi
  elapsed=$(( $(date +%s) - start ))
  if (( elapsed >= TIMEOUT )); then
    echo "ERROR: API not ready after ${TIMEOUT}s." >&2
    echo "Diagnose with: docker compose ps -a && docker compose logs ollama-init api" >&2
    exit 1
  fi
  init_state=$(docker compose ps -a --format '{{.Service}} {{.State}}' 2>/dev/null | awk '$1=="ollama-init"{print $2}')
  echo "  ...not ready yet (${elapsed}s; ollama-init: ${init_state:-unknown})"
  sleep 5
done
