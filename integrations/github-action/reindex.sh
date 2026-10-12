#!/usr/bin/env bash
# Re-index a repo in CodeSage and (optionally) wait for the job to finish.
# Configured entirely through environment variables so secrets never get
# interpolated into the script text:
#   CODESAGE_API_URL   base URL of your CodeSage backend (required)
#   CODESAGE_API_KEY   API key, cbi_...                   (required)
#   CODESAGE_SOURCE    repo URL to index                  (required)
#   CODESAGE_REPO_NAME optional name to index it under
#   CODESAGE_COLLECTION optional collection name
#   CODESAGE_WAIT      "true" (default) to poll until done
#   CODESAGE_TIMEOUT_MINUTES  polling limit, default 30
set -euo pipefail

: "${CODESAGE_API_URL:?api-url is required}"
: "${CODESAGE_API_KEY:?api-key is required}"
: "${CODESAGE_SOURCE:?source is required}"
API="${CODESAGE_API_URL%/}"
WAIT="${CODESAGE_WAIT:-true}"
TIMEOUT_MIN="${CODESAGE_TIMEOUT_MINUTES:-30}"
POLL_SECONDS="${CODESAGE_POLL_SECONDS:-10}"

echo "::add-mask::${CODESAGE_API_KEY}"

body=$(jq -n \
  --arg source "$CODESAGE_SOURCE" \
  --arg repo_name "${CODESAGE_REPO_NAME:-}" \
  --arg collection "${CODESAGE_COLLECTION:-}" \
  '{source: $source}
   + (if $repo_name != "" then {repo_name: $repo_name} else {} end)
   + (if $collection != "" then {collection: $collection} else {} end)')

# One key per (source, workflow run, run attempt). It is generated ONCE and
# reused on every retry of the POST, so the server can recognise a retry and
# return the original job instead of queueing (and billing) a second one.
# Re-running the workflow bumps GITHUB_RUN_ATTEMPT, giving a fresh key.
IDEM_KEY="gh-$(printf '%s' "$CODESAGE_SOURCE|${GITHUB_RUN_ID:-local}|${GITHUB_RUN_ATTEMPT:-1}" | sha256sum | cut -c1-40)"

# Retry transient failures (network blips, 429 rate limit, 5xx) a few times.
# Retrying POST on 5xx/timeouts is only safe because of the Idempotency-Key
# header, which the backend's /ingest must honour.
request() {  # method path [data]  -> sets HTTP_CODE and RESP
  local method=$1 path=$2 data=${3:-} attempt out
  local -a args=(-sS -m 60 -w $'\n%{http_code}' -X "$method" "$API$path"
                 -H "CODEBASE12-Key: $CODESAGE_API_KEY")
  if [ "$method" = "POST" ]; then
    args+=(-H "Idempotency-Key: $IDEM_KEY")
  fi
  if [ -n "$data" ]; then
    args+=(-H "Content-Type: application/json" -d "$data")
  fi
  for attempt in 1 2 3 4; do
    out=$(curl "${args[@]}") || out=$'\n000'
    HTTP_CODE=${out##*$'\n'}
    RESP=${out%$'\n'*}
    case "$HTTP_CODE" in
      000|429|5??) [ "$attempt" -lt 4 ] && { sleep $((attempt * 5)); continue; } ;;
    esac
    return 0
  done
}

detail() { echo "$RESP" | jq -r '.detail // .' 2>/dev/null || echo "$RESP"; }

request POST /ingest "$body"
case "$HTTP_CODE" in
  202) ;;
  401) echo "::error::CodeSage rejected the API key (401). Check the secret and that the key isn't revoked."; exit 1 ;;
  402) echo "::error::Out of CodeSage credits (402). Buy more on the Developer page."; exit 1 ;;
  403) echo "::error::$(detail)"; exit 1 ;;
  429) echo "::error::Rate limited by CodeSage (429) after retries."; exit 1 ;;
  *)   echo "::error::Ingest request failed (HTTP $HTTP_CODE): $(detail)"; exit 1 ;;
esac

job_id=$(echo "$RESP" | jq -r .job_id)
echo "job-id=$job_id" >> "${GITHUB_OUTPUT:-/dev/null}"
echo "Queued CodeSage ingest job $job_id for $CODESAGE_SOURCE"

if [ "$WAIT" != "true" ]; then
  echo "status=queued" >> "${GITHUB_OUTPUT:-/dev/null}"
  exit 0
fi

deadline=$(( $(date +%s) + TIMEOUT_MIN * 60 ))
last=""
while :; do
  request GET "/ingest/status/$job_id"
  [ "$HTTP_CODE" = "200" ] || { echo "::error::Status check failed (HTTP $HTTP_CODE): $(detail)"; exit 1; }
  status=$(echo "$RESP" | jq -r .status)
  progress=$(echo "$RESP" | jq -r '.progress.detail // empty')
  line="$status${progress:+ - $progress}"
  [ "$line" = "$last" ] || { echo "  $line"; last=$line; }
  case "$status" in
    done)
      chunks=$(echo "$RESP" | jq -r '.result.chunks_indexed // 0')
      charged=$(echo "$RESP" | jq -r '.result.credits_charged // empty')
      echo "status=done" >> "${GITHUB_OUTPUT:-/dev/null}"
      echo "chunks-indexed=$chunks" >> "${GITHUB_OUTPUT:-/dev/null}"
      echo "Indexed $chunks chunks${charged:+ ($charged credits)}."
      exit 0 ;;
    error)
      echo "::error::CodeSage ingest failed: $(echo "$RESP" | jq -r '.error // "unknown error"')"
      echo "status=error" >> "${GITHUB_OUTPUT:-/dev/null}"
      exit 1 ;;
  esac
  [ "$(date +%s)" -lt "$deadline" ] || { echo "::error::Timed out after ${TIMEOUT_MIN} minutes waiting for the ingest job."; exit 1; }
  sleep "$POLL_SECONDS"
done
