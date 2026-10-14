#!/usr/bin/env bash
# Import a model tarball created by model-export.sh into the Ollama volume (no internet needed).
# Usage: scripts/model-import.sh ollama-models.tgz   (then: docker compose up -d)
set -euo pipefail

IN="${1:?usage: scripts/model-import.sh ollama-models.tgz}"
VOLUME="${OLLAMA_VOLUME:-ai-router_ollama_data}"
IMAGE="${OLLAMA_IMAGE:-alpine/ollama:0.40.3}"
DIR="$(cd "$(dirname "$IN")" && pwd)"

docker compose create ollama >/dev/null 2>&1 || true   # let Compose create (and label) the volume
docker volume create "$VOLUME" >/dev/null
docker run --rm --entrypoint tar -v "$VOLUME":/data -v "$DIR":/backup:ro "$IMAGE" \
  xzf "/backup/$(basename "$IN")" -C /data
echo "Imported $(basename "$IN") into $VOLUME. Start the stack with: docker compose up -d"
