#!/bin/sh
# Idempotent model provisioning for the Ollama server (runs in the one-shot `ollama-init` container).
#
# 1. If the model is already in the `ollama_data` volume -> skip the download (works offline).
# 2. Otherwise download it (`ollama pull`) with up to 3 attempts.
# 3. Verify the model is available - and, if OLLAMA_MODEL_DIGEST is set, that the weights are
#    exactly the expected ones (a tag in the registry can be re-pointed to different weights).
# A non-zero exit code blocks the `api` service from starting (fail fast).
set -eu

MODEL="${OLLAMA_MODEL:?OLLAMA_MODEL is required}"

if ollama show "$MODEL" >/dev/null 2>&1; then
  echo "[ollama-init] Model '$MODEL' already present - skipping download."
else
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
fi

if [ -n "${OLLAMA_MODEL_DIGEST:-}" ]; then
  actual=$(ollama list | awk -v m="$MODEL" '$1 == m { print $2 }')
  case "$OLLAMA_MODEL_DIGEST" in
    "$actual"*) echo "[ollama-init] Digest verified: $actual" ;;
    *)
      echo "[ollama-init] ERROR: digest mismatch for '$MODEL': expected $OLLAMA_MODEL_DIGEST, got ${actual:-none}" >&2
      exit 1
      ;;
  esac
fi

echo "[ollama-init] Model '$MODEL' is ready."
