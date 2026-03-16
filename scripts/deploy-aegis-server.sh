#!/usr/bin/env bash
# scripts/deploy-aegis-server.sh
# ============================================================
# Automated AEGIS Server deployment wizard.
# Run on any Linux machine with internet access.
# Installs Docker (if needed), configures, builds, and starts
# the full AEGIS server stack.
#
# Usage:
#   bash deploy-aegis-server.sh
#
# Unattended:
#   bash deploy-aegis-server.sh \
#     --unattended \
#     --db-password your_db_pass \
#     --mqtt-password your_mqtt_pass \
#     --host 0.0.0.0
# ============================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
fail() { echo -e "${RED}  ✗ $*${NC}"; exit 1; }
warn() { echo -e "${YELLOW}  !${NC} $*"; }
info() { echo -e "${BLUE}  →${NC} $*"; }
step() { echo -e "\n${BOLD}[$1/7]${NC} $2"; }

REPO_URL="https://github.com/your-org/aegis-platform.git"
INSTALL_DIR="$(pwd)/aegis-platform"
DOCKER_DIR="aegis-server/docker"

# ── Argument parsing ─────────────────────────────────────────────────────
UNATTENDED=false
OPT_DB_PASS=""; OPT_MQTT_PASS=""; OPT_HOST="0.0.0.0"; OPT_REPO=""
OPT_SKIP_UI=false; OPT_SKIP_DOCKER=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --unattended)    UNATTENDED=true;;
    --db-password)   OPT_DB_PASS="$2";   shift;;
    --mqtt-password) OPT_MQTT_PASS="$2"; shift;;
    --host)          OPT_HOST="$2";      shift;;
    --repo)          OPT_REPO="$2";      shift;;
    --skip-ui)       OPT_SKIP_UI=true;;
    --skip-docker)   OPT_SKIP_DOCKER=true;;
    *) warn "Unknown argument: $1";;
  esac
  shift 2>/dev/null || true
done

[[ -n "$OPT_REPO" ]] && REPO_URL="$OPT_REPO"

# ── Banner ───────────────────────────────────────────────────────────────
clear
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║      AEGIS Server  —  Deployment Wizard          ║${NC}"
echo -e "${BOLD}║      Airspace Protection Platform                ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo "  This script will deploy:"
echo "    • AEGIS Server  (FastAPI + TimescaleDB + Mosquitto)"
echo "    • AEGIS Shield  (React dashboard)"
echo "    • Nginx         (reverse proxy + static file serving)"
echo ""

# ── Interactive prompts ───────────────────────────────────────────────────
gen_password() {
  python3 -c "import secrets,string; \
    print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(24)))"
}

if [[ "$UNATTENDED" == false ]]; then
  echo "  Press Enter to accept suggested secure passwords, or type your own."
  echo ""

  SUGGESTED_DB=$(gen_password)
  SUGGESTED_MQTT=$(gen_password)

  echo "  Database password"
  echo "  Used by TimescaleDB and the AEGIS Server to authenticate"
  echo "  internal database connections. Never exposed externally."
  while true; do
    read -rsp "    Password [${SUGGESTED_DB:0:8}...]: " DB_PASS; echo
    DB_PASS="${DB_PASS:-$SUGGESTED_DB}"
    read -rsp "    Confirm password: " DB_PASS_CONFIRM; echo
    DB_PASS_CONFIRM="${DB_PASS_CONFIRM:-$SUGGESTED_DB}"
    [[ "$DB_PASS" == "$DB_PASS_CONFIRM" ]] && break
    echo -e "${RED}  Passwords do not match. Try again.${NC}"
  done
  echo ""

  echo "  MQTT password"
  echo "  Used by ARGUS sensor nodes to publish detections to the"
  echo "  Mosquitto broker. You will need this when deploying nodes."
  while true; do
    read -rsp "    Password [${SUGGESTED_MQTT:0:8}...]: " MQTT_PASS; echo
    MQTT_PASS="${MQTT_PASS:-$SUGGESTED_MQTT}"
    read -rsp "    Confirm password: " MQTT_PASS_CONFIRM; echo
    MQTT_PASS_CONFIRM="${MQTT_PASS_CONFIRM:-$SUGGESTED_MQTT}"
    [[ "$MQTT_PASS" == "$MQTT_PASS_CONFIRM" ]] && break
    echo -e "${RED}  Passwords do not match. Try again.${NC}"
  done
  echo ""

  read -rp "  Server bind host [0.0.0.0]: " BIND_HOST
  BIND_HOST="${BIND_HOST:-0.0.0.0}"
else
  DB_PASS="${OPT_DB_PASS:-$(gen_password)}"
  MQTT_PASS="${OPT_MQTT_PASS:-$(gen_password)}"
  BIND_HOST="${OPT_HOST}"
  [[ -n "$DB_PASS"   ]] || fail "--db-password required or will be auto-generated"
  [[ -n "$MQTT_PASS" ]] || fail "--mqtt-password required or will be auto-generated"
fi

# ── 1/7  Check / install Docker ──────────────────────────────────────────
step 1 "Docker"
if command -v docker &>/dev/null && docker compose version &>/dev/null 2>&1; then
  ok "Docker $(docker --version | grep -oP '\d+\.\d+\.\d+' | head -1) already installed"
elif [[ "$OPT_SKIP_DOCKER" == false ]]; then
  info "Installing Docker..."
  curl -fsSL --max-time 120 https://get.docker.com | sh
  # Add current user to docker group
  [[ -n "${SUDO_USER:-}" ]] && usermod -aG docker "$SUDO_USER" || true
  ok "Docker installed"
else
  fail "Docker not found and --skip-docker specified"
fi

# ── 2/7  Fetch AEGIS code ────────────────────────────────────────────────
step 2 "AEGIS platform code"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  info "Updating existing install at $INSTALL_DIR"
  git -C "$INSTALL_DIR" pull --ff-only
elif [[ -f "aegis-server/main.py" ]]; then
  info "Running from within repository — using current directory"
  INSTALL_DIR="$(pwd)"
else
  info "Cloning from $REPO_URL"
  git clone "$REPO_URL" "$INSTALL_DIR" --depth 1
  cd "$INSTALL_DIR"
fi
ok "Code at $INSTALL_DIR"

# ── 3/7  Configure environment ───────────────────────────────────────────
step 3 "Environment configuration"
cd "$INSTALL_DIR"

ENV_FILE="$DOCKER_DIR/.env"
cat > "$ENV_FILE" << ENV
# AEGIS Server — Environment
# Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")

DB_NAME=aegis
DB_USER=aegis
DB_PASSWORD=${DB_PASS}

MQTT_USER=rid
MQTT_PASSWORD=${MQTT_PASS}
ENV
chmod 600 "$ENV_FILE"
ok "Environment written to $ENV_FILE"

# Mosquitto password file
info "Creating Mosquitto password file..."
if command -v mosquitto_passwd &>/dev/null; then
  mosquitto_passwd -c -b "$DOCKER_DIR/mosquitto_passwd" rid "$MQTT_PASS"
else
  # Generate it via Docker if mosquitto tools not installed locally
  docker run --rm eclipse-mosquitto:2 \
    sh -c "mosquitto_passwd -c -b /tmp/mqpasswd rid '$MQTT_PASS' && cat /tmp/mqpasswd" \
    > "$DOCKER_DIR/mosquitto_passwd"
fi
[[ -s "$DOCKER_DIR/mosquitto_passwd" ]] || fail "Mosquitto password file is empty or missing"
chmod 644 "$DOCKER_DIR/mosquitto_passwd"
ok "Mosquitto password file created"

# ── 4/7  Build AEGIS Shield (UI) ─────────────────────────────────────────
step 4 "Building AEGIS Shield (React dashboard)"
if [[ "$OPT_SKIP_UI" == false ]]; then
  if command -v node &>/dev/null; then
    NODE_VER=$(node --version)
    info "Node.js $NODE_VER found"
  else
    info "Installing Node.js LTS..."
    curl -fsSL --max-time 120 https://deb.nodesource.com/setup_lts.x | bash -
    apt-get install -y nodejs
    ok "Node.js installed"
  fi

  cd aegis-shield
  npm install --silent
  npm run build
  cd ..
  ok "AEGIS Shield built → aegis-shield/dist/"
else
  warn "Skipping UI build (--skip-ui)"
fi

# ── 5/7  Start Docker stack ───────────────────────────────────────────────
step 5 "Starting Docker stack"
cd "$DOCKER_DIR"
docker compose pull --quiet
docker compose up -d --build
ok "Docker stack started"

# Wait for health checks (up to 60 s)
info "Waiting for services to become healthy..."
HEALTHY=0
for i in $(seq 1 30); do
  HEALTHY=$(docker compose ps --format json 2>/dev/null | \
    python3 -c "import sys,json; \
      data=sys.stdin.read(); \
      rows=[json.loads(l) for l in data.splitlines() if l.strip()]; \
      print(sum(1 for r in rows if r.get('Health','')=='healthy'))" 2>/dev/null || echo "0")
  [[ "$HEALTHY" -ge 2 ]] && break
  printf "  Waiting... (%ds)\r" "$((i * 2))"
  sleep 2
done
echo ""
if [[ "$HEALTHY" -lt 2 ]]; then
  warn "Services did not become healthy in 60s — check logs:"
  warn "  docker compose -f $DOCKER_DIR/docker-compose.yml logs --tail=40"
  warn "Continuing, but the deployment may need attention."
else
  ok "Services healthy ($HEALTHY containers)"
fi
cd "$INSTALL_DIR"

# ── 6/7  Run tests ────────────────────────────────────────────────────────
step 6 "Verification"
if command -v python3 &>/dev/null; then
  # Quick smoke test — hit the health endpoint
  sleep 3
  HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health || echo "000")
  if [[ "$HTTP_STATUS" == "200" ]]; then
    ok "API health endpoint: HTTP $HTTP_STATUS"
  else
    warn "API health endpoint returned HTTP $HTTP_STATUS — check docker compose logs aegis-server"
  fi
else
  warn "python3 not found — skipping smoke test"
fi

# ── 7/7  Save credentials ─────────────────────────────────────────────────
step 7 "Saving deployment summary"
SERVER_IP=$(hostname -I | awk '{print $1}')
SUMMARY_FILE="$INSTALL_DIR/DEPLOYMENT.md"

cat > "$SUMMARY_FILE" << SUMMARY
# AEGIS Deployment Summary
Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")

## Access

| Service | URL |
|---|---|
| AEGIS Shield (dashboard) | http://${SERVER_IP} |
| AEGIS Server (API) | http://${SERVER_IP}:8000 |
| API health | http://${SERVER_IP}:8000/health |
| MQTT broker | ${SERVER_IP}:1883 |

## Credentials

| Service | Username | Password |
|---|---|---|
| Database | aegis | ${DB_PASS} |
| MQTT | rid | ${MQTT_PASS} |

## ARGUS Node Configuration

Use these values in each node's \`/etc/argus-node/config.yaml\`:

\`\`\`yaml
mqtt:
  host: "${SERVER_IP}"
  port: 1883
  user: "rid"
  password: "${MQTT_PASS}"
\`\`\`

Or deploy automatically:
\`\`\`bash
sudo bash scripts/deploy-argus-node.sh \\
  --server-ip ${SERVER_IP} \\
  --mqtt-password "${MQTT_PASS}"
\`\`\`

## Management

\`\`\`bash
# View logs
docker compose -f aegis-server/docker/docker-compose.yml logs -f

# Restart API
docker compose -f aegis-server/docker/docker-compose.yml restart aegis-server

# Stop everything
docker compose -f aegis-server/docker/docker-compose.yml down

# Update to latest
bash scripts/update.sh
\`\`\`
SUMMARY
chmod 600 "$SUMMARY_FILE"
ok "Deployment summary saved to $SUMMARY_FILE"

# ── Done ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║           Deployment Complete!                   ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${GREEN}AEGIS Shield${NC}  : http://${SERVER_IP}"
echo -e "  ${GREEN}AEGIS Server${NC}  : http://${SERVER_IP}:8000"
echo -e "  ${GREEN}MQTT${NC}          : ${SERVER_IP}:1883 (user: rid)"
echo ""
echo -e "  ${YELLOW}Credentials saved to:${NC} DEPLOYMENT.md"
echo ""
echo "  Next: deploy ARGUS nodes with:"
echo -e "  ${BLUE}  sudo bash scripts/deploy-argus-node.sh \\${NC}"
echo -e "  ${BLUE}    --server-ip ${SERVER_IP} \\${NC}"
echo -e "  ${BLUE}    --mqtt-password '${MQTT_PASS:0:4}...'${NC}"
echo ""
echo "  Health check:"
echo "    python scripts/healthcheck.py --server http://${SERVER_IP}"
echo ""
