#!/bin/sh
# Idempotent model provisioning for the Ollama server (runs in the one-shot `ollama-init` container).
#
# 1. If the model is already in the `ollama_data` volume -> exit 0 immediately (works offline).
# 2. Otherwise download it (`ollama pull`) with up to 3 attempts.
# 3. Verify the model is available; a non-zero exit code blocks the `api` service from starting.
set -eu

MODEL="${OLLAMA_MODEL:?OLLAMA_MODEL is required}"

if ollama show "$MODEL" >/dev/null 2>&1; then
  echo "[ollama-init] Model '$MODEL' already present - skipping download."
  exit 0
fi

attempt=1
while [ "$attempt" -le 3 ]; do
  echo "[ollama-init] Pulling '$MODEL' (attempt $attempt/3)..."
  if ollama pull "$MODEL"; then
    break
  fi
  attempt=$((attempt + 1))
  sleep $((attempt * 5))
done

if ! ollama show "$MODEL" >/dev/null 2>&1; then
  echo "[ollama-init] ERROR: model '$MODEL' is not available after download attempts." >&2
  exit 1
fi

echo "[ollama-init] Model '$MODEL' is ready."
