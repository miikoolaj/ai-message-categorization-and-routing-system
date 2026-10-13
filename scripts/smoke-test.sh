#!/usr/bin/env bash
# Smoke test of the Definition of Done against the RUNNING stack (docker compose up -d).
# Requires: curl, jq. Usage: scripts/smoke-test.sh
set -euo pipefail

API="${API_URL:-http://127.0.0.1:${API_PORT:-8080}}"
MAILPIT="${MAILPIT_URL:-http://127.0.0.1:${MAILPIT_UI_PORT:-8025}}"
for tool in curl jq; do
  command -v "$tool" >/dev/null || { echo "ERROR: '$tool' is required" >&2; exit 2; }
done

failures=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; failures=$((failures + 1)); }

"$(dirname "$0")/wait-ready.sh" "${WAIT_TIMEOUT:-900}"

echo "== Swagger / OpenAPI"
if curl -fsS "$API/api/v1/docs" | grep -q swagger-ui; then pass "Swagger UI at /api/v1/docs"; else fail "Swagger UI at /api/v1/docs"; fi
if curl -fsS "$API/api/v1/openapi.json" | jq -e '.paths["/api/v1/messages"]' >/dev/null; then
  pass "OpenAPI describes POST /api/v1/messages"
else
  fail "OpenAPI describes POST /api/v1/messages"
fi

check_case() {
  local message="$1" expected="$2"
  local sender="smoke-$(date +%s)-$RANDOM@example.com"
  echo "== \"$message\" (expected: $expected)"
  local response
  response=$(curl -sS --max-time 200 -X POST "$API/api/v1/messages" \
    -H 'Content-Type: application/json' \
    -d "$(jq -n --arg e "$sender" --arg m "$message" '{email: $e, message: $m}')")
  local department routed_by request_id
  department=$(jq -r '.department // empty' <<<"$response")
  routed_by=$(jq -r '.routed_by // empty' <<<"$response")
  request_id=$(jq -r '.request_id // empty' <<<"$response")
  if [[ "$routed_by" == "agent" ]]; then pass "routed by the AI agent (tool call)"; else fail "routed_by=$routed_by: $response"; fi
  if [[ "$department" == "$expected" ]]; then pass "API chose $department"; else fail "API chose '$department' (reason: $(jq -r '.reason // empty' <<<"$response"))"; fi

  local id="" attempt
  for attempt in $(seq 1 30); do
    id=$(curl -fsS -G "$MAILPIT/api/v1/search" --data-urlencode "query=reply-to:\"$sender\"" | jq -r '.messages[0].ID // empty')
    [[ -n "$id" ]] && break
    sleep 1
  done
  if [[ -z "$id" ]]; then fail "e-mail captured by Mailpit"; return; fi
  pass "e-mail captured by Mailpit (ID $id)"
  local mail to reply_to header_rid
  mail=$(curl -fsS "$MAILPIT/api/v1/message/$id")
  to=$(jq -r '.To[0].Address' <<<"$mail")
  reply_to=$(jq -r '.ReplyTo[0].Address' <<<"$mail")
  header_rid=$(curl -fsS "$MAILPIT/api/v1/message/$id/headers" | jq -r 'to_entries[] | select(.key | ascii_downcase == "x-request-id") | .value[0]')
  if [[ "$to" == "$expected" ]]; then pass "To: $to"; else fail "To: $to (expected $expected)"; fi
  if [[ "$reply_to" == "$sender" ]]; then pass "Reply-To: $reply_to"; else fail "Reply-To: $reply_to (expected $sender)"; fi
  if [[ "$header_rid" == "$request_id" ]]; then pass "X-Request-ID matches the API response"; else fail "X-Request-ID: $header_rid != $request_id"; fi
}

check_case "Nie działa mi komputer" "help-desk@example.com"
check_case "Chciałbym zgłosić urlop na jutro" "kadry@example.com"
check_case "Serwer produkcyjny nie odpowiada, nikt w firmie nie może pracować" "it@example.com"

echo
if (( failures == 0 )); then
  echo "SMOKE TEST PASSED - open $MAILPIT to see the e-mails."
else
  echo "SMOKE TEST FAILED ($failures check(s))." >&2
  exit 1
fi
