#!/usr/bin/env bash
# =============================================================================
# update-doc-list.sh — rebuild the Remote ID make/model reference table
# =============================================================================
# The KESTREL server resolves a broadcast Remote ID serial to a drone make/model
# by longest-prefix match against serial prefixes scraped from the FAA
# Declaration of Compliance (DoC) serial lists. This script triggers that
# rebuild (or imports a CSV on an air-gapped box). Modes:
#
#   # Scrape the FAA DoC API using the root in Settings (rid_doc_source_url):
#   bash scripts/update-doc-list.sh
#
#   # Scrape from a specific API root:
#   bash scripts/update-doc-list.sh --url https://uasdoc.faa.gov/api/v1
#
#   # Import a local CSV (serial_prefix,manufacturer,model) — no network:
#   bash scripts/update-doc-list.sh --file prefixes.csv
#
# Options:
#   --server URL   KESTREL server base (default http://localhost:8000)
#   --url URL      One-off FAA API root (overrides the setting)
#   --file PATH    Import a local CSV instead of scraping
#
# The scrape runs in the background on the server; this returns once it has
# started. Watch progress in System > Settings or `make server-logs`.
# If KESTREL_API_KEY is set in the environment, it is sent as X-Api-Key.
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
[[ -n "${KESTREL_API_KEY:-}" ]] && HDR+=(-H "X-Api-Key: ${KESTREL_API_KEY}")

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
    echo "Starting FAA scrape from ${URL} via ${SERVER} …"
    curl -fsS "${HDR[@]}" -X POST "${SERVER}/api/system/rid-identity/rebuild" \
         --data "$(python3 -c 'import json,sys; print(json.dumps({"url": sys.argv[1]}))' "$URL")"
  else
    echo "Starting FAA scrape from the configured API root via ${SERVER} …"
    curl -fsS "${HDR[@]}" -X POST "${SERVER}/api/system/rid-identity/rebuild" --data '{}'
  fi
fi
echo   # trailing newline after the JSON response
