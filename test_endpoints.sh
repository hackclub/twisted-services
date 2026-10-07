#!/usr/bin/env bash
set -euo pipefail

# Tests every bot endpoint using curl.
# Portable: does not depend on Python/venv, and can be run from any directory.
#
# Usage:
#   ./test_endpoints.sh \
#     --base-url http://127.0.0.1:8000 \
#     --auth-token YOUR_AUTH_BEARER_TOKEN \
#     --slack-signing-secret YOUR_SLACK_SIGNING_SECRET \
#     REVIEWER_ID USER_ID_1 [USER_ID_2 ...]
#
# Notes:
# - The server must already be running and configured with Slack creds + SHIP_CHANNEL_ID.
# - You can optionally omit Slack events test by passing --skip-slack-events.

BASE_URL="http://127.0.0.1:8000"
AUTH_TOKEN=""
SLACK_SIGNING_SECRET=""
# Default: do NOT test /slack/events because it requires the Slack signing secret.
TEST_SLACK_EVENTS=0

usage() {
  cat >&2 <<'USAGE'
Usage:
  test_endpoints.sh [--base-url URL] --auth-token TOKEN [--test-slack-events --slack-signing-secret SECRET] REVIEWER_ID USER_ID_1 [USER_ID_2 ...]

Required:
  --auth-token TOKEN

Optional:
  --test-slack-events
  --slack-signing-secret SECRET   (required only when --test-slack-events is set)

Examples:
  ./test_endpoints.sh --auth-token replace-me U_REVIEWER U_USER1 U_USER2
  ./test_endpoints.sh --base-url http://localhost:8000 --auth-token replace-me --test-slack-events --slack-signing-secret signingsecret U_REVIEWER U_USER1
USAGE
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing required command: $1" >&2; exit 2; }
}

need_cmd curl
need_cmd grep
need_cmd date

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url)
      BASE_URL="$2"; shift 2 ;;
    --auth-token)
      AUTH_TOKEN="$2"; shift 2 ;;
    --slack-signing-secret)
      SLACK_SIGNING_SECRET="$2"; shift 2 ;;
    --test-slack-events)
      TEST_SLACK_EVENTS=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    --)
      shift; break ;;
    -*)
      echo "Unknown option: $1" >&2
      usage
      exit 2
      ;;
    *)
      break
      ;;
  esac
done

if [[ -z "$AUTH_TOKEN" ]]; then
  echo "Missing --auth-token" >&2
  usage
  exit 2
fi

if [[ $TEST_SLACK_EVENTS -eq 1 ]]; then
  if [[ -z "$SLACK_SIGNING_SECRET" ]]; then
    echo "Missing --slack-signing-secret (required with --test-slack-events)" >&2
    usage
    exit 2
  fi
  need_cmd openssl
  need_cmd awk
fi

if [[ $# -lt 2 ]]; then
  usage
  exit 2
fi

REVIEWER_ID="$1"
shift
USER_IDS=("$@")

http_status() {
  # prints status code only
  curl -sS -o /dev/null -w "%{http_code}" "$@"
}

curl_json() {
  # prints body to stdout; status code to stderr prefixed with STATUS:
  local tmp
  tmp="$(mktemp)"
  local code
  code="$(curl -sS -o "$tmp" -w "%{http_code}" "$@")"
  echo "STATUS:$code" >&2
  cat "$tmp"
  rm -f "$tmp"
}

slack_signature() {
  local ts="$1"
  local body="$2"
  local base="v0:${ts}:${body}"
  local hex
  # openssl output: (stdin)= <hex>
  hex="$(printf '%s' "$base" | openssl dgst -sha256 -hmac "$SLACK_SIGNING_SECRET" -hex | awk '{print $NF}')"
  printf 'v0=%s' "$hex"
}

assert_status() {
  local got="$1"
  local want="$2"
  local label="$3"
  if [[ "$got" != "$want" ]]; then
    echo "FAIL [$label]: expected HTTP $want, got $got" >&2
    exit 1
  fi
  echo "OK   [$label] HTTP $got" >&2
}

# --- run ---
code="$(http_status "$BASE_URL/healthz")"
if [[ "$code" != "200" ]]; then
  echo "Server not reachable/healthy at $BASE_URL (GET /healthz -> $code)" >&2
  exit 1
fi

# 1) GET /healthz
assert_status "$code" 200 "GET /healthz"

# 2) POST /slack/events (signed url_verification)
ts="$(date +%s)"
if [[ $TEST_SLACK_EVENTS -eq 1 ]]; then
  challenge="test-challenge-$ts"
  body="{\"type\":\"url_verification\",\"challenge\":\"$challenge\"}"
  sig="$(slack_signature "$ts" "$body")"
  resp="$(curl -sS -w '\n%{http_code}' -X POST "$BASE_URL/slack/events" \
    -H 'Content-Type: application/json' \
    -H "X-Slack-Request-Timestamp: $ts" \
    -H "X-Slack-Signature: $sig" \
    --data "$body")"
  slack_body="$(printf '%s' "$resp" | head -n 1)"
  slack_code="$(printf '%s' "$resp" | tail -n 1)"
  assert_status "$slack_code" 200 "POST /slack/events url_verification"
  if ! printf '%s' "$slack_body" | grep -q "$challenge"; then
    echo "FAIL [POST /slack/events]: response did not contain challenge" >&2
    echo "Body: $slack_body" >&2
    exit 1
  fi
else
  echo "SKIP [POST /slack/events] (default)" >&2
fi

# Helper: auth header
AUTH_HEADER=( -H "Authorization: Bearer $AUTH_TOKEN" -H "Content-Type: application/json" )

# 3) POST /ship for each user
for uid in "${USER_IDS[@]}"; do
  pname="Test Project for $uid ($ts)"
  plink="https://example.com/$uid/$ts"
  payload="{\"user_id\":\"$uid\",\"project_name\":\"$pname\",\"project_link\":\"$plink\"}"
  code="$(curl -sS -o /tmp/ship.json -w "%{http_code}" -X POST "$BASE_URL/ship" "${AUTH_HEADER[@]}" --data "$payload")"
  assert_status "$code" 200 "POST /ship ($uid)"
  # minimal shape check (no jq/python dependency)
  if ! grep -q '"public"' /tmp/ship.json || ! grep -q '"dm"' /tmp/ship.json; then
    echo "FAIL [POST /ship ($uid)]: response missing expected keys" >&2
    cat /tmp/ship.json >&2
    exit 1
  fi
  rm -f /tmp/ship.json

done

# 4) POST /review-accept + /review-reject for each user
for uid in "${USER_IDS[@]}"; do
  pname="Reviewable Project for $uid ($ts)"
  plink="https://example.com/review/$uid/$ts"

  accept_payload="{\"user_id\":\"$uid\",\"project_name\":\"$pname\",\"project_link\":\"$plink\",\"reviewer_id\":\"$REVIEWER_ID\",\"feedback\":\"Looks good ($ts)\",\"currencies\":\"10 Gold\"}"
  code="$(http_status -X POST "$BASE_URL/review-accept" "${AUTH_HEADER[@]}" --data "$accept_payload")"
  assert_status "$code" 200 "POST /review-accept ($uid)"

  reject_payload="{\"user_id\":\"$uid\",\"project_name\":\"$pname\",\"project_link\":\"$plink\",\"reviewer_id\":\"$REVIEWER_ID\",\"feedback\":\"Needs changes ($ts)\"}"
  code="$(http_status -X POST "$BASE_URL/review-reject" "${AUTH_HEADER[@]}" --data "$reject_payload")"
  assert_status "$code" 200 "POST /review-reject ($uid)"

done

# 5) POST /fulfill_pending, /fulfill_approved, /fulfill_fullfilled for each user
for uid in "${USER_IDS[@]}"; do
  oid="$ts-${uid: -4}"
  base_payload="\"user_id\":\"$uid\",\"order_id\":\"$oid\",\"item_name\":\"Test Item\",\"qty\":\"1\",\"cost\":\"5 potions\""

  code="$(http_status -X POST "$BASE_URL/fulfill_pending" "${AUTH_HEADER[@]}" --data "{$base_payload}")"
  assert_status "$code" 200 "POST /fulfill_pending ($uid)"

  code="$(http_status -X POST "$BASE_URL/fulfill_approved" "${AUTH_HEADER[@]}" --data "{$base_payload}")"
  assert_status "$code" 200 "POST /fulfill_approved ($uid)"

  full_payload="{$base_payload,\"fulfilled_by\":\"Test Fulfillment\",\"tracking_details\":\"TRACK-$oid\"}"
  code="$(http_status -X POST "$BASE_URL/fulfill_fullfilled" "${AUTH_HEADER[@]}" --data "$full_payload")"
  assert_status "$code" 200 "POST /fulfill_fullfilled ($uid)"

done

# 6) POST /custom for each user
for uid in "${USER_IDS[@]}"; do
  msg="Hello <@$uid> (test message $ts)"
  payload="{\"target_id\":\"$uid\",\"message\":\"$msg\"}"
  code="$(http_status -X POST "$BASE_URL/custom" "${AUTH_HEADER[@]}" --data "$payload")"
  assert_status "$code" 200 "POST /custom ($uid)"

done

echo "All endpoint tests passed." >&2
