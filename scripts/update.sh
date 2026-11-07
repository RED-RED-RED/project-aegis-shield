#!/usr/bin/env bash
# scripts/update.sh  —  Rolling update for an existing AEGIS deployment.
# bash scripts/update.sh [--skip-ui] [--skip-tests]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'
BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
warn() { echo -e "${YELLOW}  !${NC} $*"; }
step() { echo -e "\n${BOLD}[$1/6]${NC} $2"; }

SKIP_UI=false; SKIP_TESTS=false
while [[ $# -gt 0 ]]; do
  case "$1" in --skip-ui) SKIP_UI=true;; --skip-tests) SKIP_TESTS=true;; esac
  shift
done

echo -e "\n${BOLD}AEGIS Platform — Rolling Update${NC}\n"

step 1 "Pulling latest code"
PREV=$(git rev-parse --short HEAD)
git pull --ff-only
NEW=$(git rev-parse --short HEAD)
[[ "$PREV" == "$NEW" ]] && ok "Already up to date ($NEW)" || ok "Updated $PREV → $NEW"

step 2 "ARGUS node Python dependencies"
ARGUS_VENV="/opt/argus-node/venv"
ARGUS_REQ="$(git rev-parse --show-toplevel)/argus-node/requirements.txt"
if [[ -d "$ARGUS_VENV" && -f "$ARGUS_REQ" ]]; then
  "$ARGUS_VENV/bin/pip" install -q -r "$ARGUS_REQ"
  # Re-apply the pyrtlsdr Python 3.13 patch — pip install may have reinstalled
  # an unpatched version of pyrtlsdr if the pinned package was updated.
  bash "$SCRIPT_DIR/patch-pyrtlsdr.sh" "$ARGUS_VENV"
  ok "ARGUS node packages updated and patched"
else
  warn "ARGUS node venv not found at $ARGUS_VENV — skipping (run deploy-argus-node.sh first)"
fi

step 3 "Running tests"
if [[ "$SKIP_TESTS" == false ]]; then
  python3 -m pytest argus-node/tests/ aegis-server/tests/ calibration/tests/ \
    -q --tb=short || { warn "Tests failed — aborting"; exit 1; }
  ok "All tests pass"
else
  warn "Skipping tests (--skip-tests)"
fi

step 4 "Rebuilding AEGIS Shield"
if [[ "$SKIP_UI" == false ]]; then
  cd aegis-shield && npm install --silent && npm run build && cd ..
  ok "AEGIS Shield rebuilt"
else warn "Skipping UI rebuild (--skip-ui)"; fi

step 5 "Restarting services"
cd aegis-server/docker
docker compose build aegis-server --quiet
docker compose up -d aegis-server
ok "aegis-server restarted"
cd ../..

step 6 "Verification"
sleep 3
HTTP=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health 2>/dev/null || echo "000")
[[ "$HTTP" == "200" ]] && ok "API health check passed" || \
  warn "API returned HTTP $HTTP — check logs: make server-logs"

echo -e "\n  ${GREEN}Update complete!${NC}\n"
