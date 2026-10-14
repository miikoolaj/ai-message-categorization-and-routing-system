#!/usr/bin/env bash
# Export the downloaded model (Docker volume) to a tarball - for offline / air-gapped machines.
# Usage: scripts/model-export.sh [ollama-models.tgz]
set -euo pipefail

OUT="${1:-ollama-models.tgz}"
VOLUME="${OLLAMA_VOLUME:-ai-router_ollama_data}"
IMAGE="${OLLAMA_IMAGE:-alpine/ollama:0.40.3}"

docker volume inspect "$VOLUME" >/dev/null 2>&1 || { echo "Volume $VOLUME not found - run docker compose up -d first." >&2; exit 1; }
docker run --rm --entrypoint tar -v "$VOLUME":/data:ro -v "$(pwd)":/backup "$IMAGE" \
  czf "/backup/$OUT" -C /data .
echo "Exported $VOLUME to $(pwd)/$OUT ($(du -h "$OUT" | cut -f1))."
