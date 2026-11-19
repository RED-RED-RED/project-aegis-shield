#!/usr/bin/env bash
# =============================================================================
# update-doc-list.sh — refresh the Remote ID make/model reference table
# =============================================================================
# The AEGIS server resolves a broadcast CTA-2063-A serial to a drone make/model
# using a reference table seeded from the FAA Declaration of Compliance list.
# This script refreshes that table. Two modes:
#
#   # Pull from the URL configured in Settings (rid_doc_source_url):
#   bash scripts/update-doc-list.sh
#
#   # Pull from a specific URL:
#   bash scripts/update-doc-list.sh --url https://example.gov/doc-list.csv
#
#   # Import a local CSV (air-gapped server — no outbound network):
#   bash scripts/update-doc-list.sh --file faa_doc.csv
#
# Options:
#   --server URL   AEGIS server base (default http://localhost:8000)
#   --url URL      One-off source URL to pull from (overrides the setting)
#   --file PATH    Import a local CSV instead of fetching
#
# If AEGIS_API_KEY is set in the environment, it is sent as X-Api-Key.
# Intended for manual use or a monthly cron/systemd timer on the server.
# =============================================================================
set -euo pipefail

SERVER="http://localhost:8000"
URL=""
FILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server) SERVER="$2"; shift 2;;
    --url)    URL="$2";    shift 2;;
    --file)   FILE="$2";   shift 2;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "Unknown argument: $1" >&2; exit 2;;
  esac
done

HDR=(-H "Content-Type: application/json")
[[ -n "${AEGIS_API_KEY:-}" ]] && HDR+=(-H "X-Api-Key: ${AEGIS_API_KEY}")

if [[ -n "$FILE" ]]; then
  [[ -f "$FILE" ]] || { echo "No such file: $FILE" >&2; exit 1; }
  echo "Importing $FILE into ${SERVER} …"
  # Send the CSV as a JSON string field so no multipart handling is needed.
  python3 - "$FILE" > /tmp/doc_payload.json <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    print(json.dumps({"csv": f.read()}))
PY
  curl -fsS "${HDR[@]}" -X POST "${SERVER}/api/system/rid-identity/import" \
       --data @/tmp/doc_payload.json
  rm -f /tmp/doc_payload.json
else
  if [[ -n "$URL" ]]; then
    echo "Refreshing from ${URL} via ${SERVER} …"
    curl -fsS "${HDR[@]}" -X POST "${SERVER}/api/system/rid-identity/refresh" \
         --data "$(python3 -c 'import json,sys; print(json.dumps({"url": sys.argv[1]}))' "$URL")"
  else
    echo "Refreshing from the configured source URL via ${SERVER} …"
    curl -fsS "${HDR[@]}" -X POST "${SERVER}/api/system/rid-identity/refresh" --data '{}'
  fi
fi
echo   # trailing newline after the JSON response
