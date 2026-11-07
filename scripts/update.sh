#!/usr/bin/env bash
# scripts/update.sh
# ============================================================
# Update an existing AEGIS install to the latest committed code.
#
# Usage:
#   sudo bash scripts/update.sh server    # Pull latest, rebuild UI, rebuild+restart containers
#   sudo bash scripts/update.sh node      # Pull latest, update deps, restart the node service
#   bash scripts/update.sh version        # Show current commit + API version
#
# Options (server/node):
#   --skip-ui        server only: don't rebuild the dashboard
#   --no-pull        skip the git update (rebuild from the current checkout)
#
# The server path assumes the Docker stack from deploy-aegis-server.sh.
# The node path assumes the systemd install from deploy-argus-node.sh
# (repo at /opt/argus-node, venv at /opt/argus-node/venv).
# ============================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
fail() { echo -e "${RED}  ✗ $*${NC}"; exit 1; }
warn() { echo -e "${YELLOW}  !${NC} $*"; }
info() { echo -e "${BLUE}  →${NC} $*"; }
step() { echo -e "\n${BOLD}[$1]${NC} $2"; }

# Repo root = parent of this script's directory. Each machine updates its own
# checkout (server clone, or /opt/argus-node on a node).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
COMPOSE_FILE="$REPO_ROOT/aegis-server/docker/docker-compose.yml"
DC="docker compose -f $COMPOSE_FILE"

MODE="${1:-}"; shift 2>/dev/null || true
SKIP_UI=false; NO_PULL=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-ui) SKIP_UI=true;;
    --no-pull) NO_PULL=true;;
    *) warn "Unknown argument: $1";;
  esac
  shift
done

usage() {
  echo "Usage: bash scripts/update.sh {server|node|version} [--skip-ui] [--no-pull]"
  exit 1
}

current_commit() { git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo "unknown"; }

# ── server ──────────────────────────────────────────────────────────────────
update_server() {
  command -v docker &>/dev/null || fail "Docker not found — is this the server?"
  [[ -f "$COMPOSE_FILE" ]] || fail "Compose file not found at $COMPOSE_FILE"

  step "1/4" "Fetching latest code"
  if [[ "$NO_PULL" == false ]]; then
    info "Was at $(current_commit)"
    git -C "$REPO_ROOT" pull --ff-only || \
      fail "git pull failed (local changes or diverged history). Resolve, then re-run with --no-pull to rebuild as-is."
    ok "Now at $(current_commit)"
  else
    warn "Skipping git pull (--no-pull); rebuilding $(current_commit)"
  fi

  step "2/4" "Rebuilding dashboard (AEGIS Shield)"
  if [[ "$SKIP_UI" == false ]]; then
    command -v npm &>/dev/null || fail "npm not found — install Node.js or re-run with --skip-ui"
    ( cd "$REPO_ROOT/aegis-shield" && npm install --silent && npm run build )
    [[ -f "$REPO_ROOT/aegis-shield/dist/index.html" ]] || fail "UI build produced no dist/index.html"
    # nginx serves aegis-shield/dist as a read-only volume, so the new build is
    # live on the next request — no container restart needed for the UI.
    ok "Dashboard rebuilt → aegis-shield/dist/"
  else
    warn "Skipping UI build (--skip-ui)"
  fi

  step "3/4" "Rebuilding and restarting the API container"
  # Recreates the aegis-server image with the new code. init_db() runs on
  # startup and creates any new tables (CREATE TABLE IF NOT EXISTS), so there
  # is no separate migration step.
  $DC up -d --build aegis-server
  ok "aegis-server rebuilt and restarted"

  step "4/4" "Health check"
  sleep 3
  local status
  status=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health || echo "000")
  if [[ "$status" == "200" ]]; then
    ok "API healthy (HTTP 200) at http://localhost:8000/health"
  else
    warn "API returned HTTP $status — check logs: $DC logs --tail=40 aegis-server"
  fi
  echo -e "\n${BOLD}Server updated to $(current_commit).${NC}"
}

# ── node ────────────────────────────────────────────────────────────────────
update_node() {
  local venv="$REPO_ROOT/venv"
  local reqs="$REPO_ROOT/argus-node/requirements.txt"
  [[ -f "$reqs" ]] || fail "Node requirements not found at $reqs — is this an ARGUS node?"
  [[ $EUID -eq 0 ]] || fail "Run as root: sudo bash scripts/update.sh node"

  step "1/4" "Fetching latest code"
  if [[ "$NO_PULL" == false ]]; then
    info "Was at $(current_commit)"
    git -C "$REPO_ROOT" fetch origin
    git -C "$REPO_ROOT" reset --hard origin/main
    ok "Now at $(current_commit)"
  else
    warn "Skipping git update (--no-pull); reinstalling $(current_commit)"
  fi

  step "2/4" "Updating Python dependencies"
  [[ -x "$venv/bin/pip" ]] || fail "venv not found at $venv — run deploy-argus-node.sh first"
  "$venv/bin/pip" install --upgrade pip -q
  "$venv/bin/pip" install -r "$reqs" -q
  ok "Dependencies up to date"

  step "3/4" "Re-applying pyrtlsdr patch (Python 3.13)"
  # Idempotent; keeps the RTL-SDR / HackRF import working on 3.13 venvs.
  if [[ -f "$SCRIPT_DIR/patch-pyrtlsdr.sh" ]]; then
    bash "$SCRIPT_DIR/patch-pyrtlsdr.sh" "$venv" || warn "pyrtlsdr patch step reported an issue"
    ok "pyrtlsdr patch applied"
  else
    warn "patch-pyrtlsdr.sh not found — skipping"
  fi

  step "4/4" "Restarting the node service"
  systemctl restart argus-node.service
  sleep 2
  if systemctl is-active --quiet argus-node.service; then
    ok "argus-node.service is running"
  else
    warn "argus-node.service is not active — check: journalctl -u argus-node -n 40"
  fi
  echo -e "\n${BOLD}Node updated to $(current_commit).${NC}"
}

# ── version ───────────────────────────────────────────────────────────────────
show_version() {
  echo -e "${BOLD}AEGIS — version${NC}"
  echo "  commit : $(current_commit)"
  echo "  branch : $(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  echo "  date   : $(git -C "$REPO_ROOT" log -1 --format=%cd --date=short 2>/dev/null || echo unknown)"
  local api
  api=$(curl -s --max-time 3 http://localhost:8000/health 2>/dev/null || true)
  if [[ -n "$api" ]]; then
    local ver
    ver=$(printf '%s' "$api" | python3 -c "import sys,json; print(json.load(sys.stdin).get('version','?'))" 2>/dev/null || echo "?")
    echo "  API    : v$ver (http://localhost:8000 reachable)"
  else
    echo "  API    : not reachable on localhost:8000 (fine on a node, or server is down)"
  fi
}

case "$MODE" in
  server)  update_server;;
  node)    update_node;;
  version) show_version;;
  *)       usage;;
esac
