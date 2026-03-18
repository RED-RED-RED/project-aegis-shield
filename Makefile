# Makefile — AEGIS Platform development shortcuts
.PHONY: test test-node test-server test-cal ui-build ui-dev \
        server-up server-down server-logs server-rebuild \
        node-deploy node-start node-logs node-status \
        healthcheck calibrate lint clean help

# ── Testing ────────────────────────────────────────────────────────────────

test: test-node test-server test-cal
	@echo ""
	@echo "  ✓ All tests passed"

test-node:
	@echo "── ARGUS Node — parser tests (11) ──"
	python -m pytest argus-node/tests/ -v

test-server:
	@echo "── AEGIS Server — alert engine + analysis + integrations ──"
	python -m pytest aegis-server/tests/ -v

test-cal:
	@echo "── Calibration — engine + collector + writer (31) ──"
	python -m pytest calibration/tests/ -v

# ── AEGIS Shield (UI) ──────────────────────────────────────────────────────

ui-build:
	cd aegis-shield && npm install && npm run build
	@echo "  ✓ AEGIS Shield built → aegis-shield/dist/"

ui-dev:
	cd aegis-shield && npm install && npm run dev

# ── AEGIS Server (Docker Compose) ─────────────────────────────────────────

server-up:
	@test -f aegis-server/docker/.env || \
	  (cp aegis-server/docker/.env.example aegis-server/docker/.env && \
	   echo "  Created aegis-server/docker/.env — set passwords before starting" && exit 1)
	cd aegis-server/docker && docker compose up -d
	@echo "  ✓ AEGIS Server stack running"
	@echo "    AEGIS Shield : http://localhost"
	@echo "    API          : http://localhost:8000"
	@echo "    MQTT         : localhost:1883"

server-down:
	cd aegis-server/docker && docker compose down

server-logs:
	cd aegis-server/docker && docker compose logs -f aegis-server

server-rebuild:
	cd aegis-server/docker && docker compose up -d --build aegis-server

server-status:
	cd aegis-server/docker && docker compose ps

# ── ARGUS Node ─────────────────────────────────────────────────────────────

node-deploy:
	@echo "  Deploy to a Pi with:"
	@echo "  scp scripts/deploy-argus-node.sh pi@<PI_IP>:~"
	@echo "  ssh pi@<PI_IP> 'sudo bash ~/deploy-argus-node.sh'"

node-start:
	sudo systemctl start argus-node

node-stop:
	sudo systemctl stop argus-node

node-logs:
	sudo journalctl -fu argus-node

node-status:
	sudo systemctl status argus-node

# ── Calibration ────────────────────────────────────────────────────────────

calibrate:
	@echo "  Usage:"
	@echo "  python calibration/calibrate.py \\"
	@echo "    --server http://localhost:8000 \\"
	@echo "    --drone-id YOUR_DRONE_SERIAL \\"
	@echo "    --gpx my_flight.gpx \\"
	@echo "    --plots"

# ── Health check ───────────────────────────────────────────────────────────

healthcheck:
	python scripts/healthcheck.py --server http://localhost

# ── Update ─────────────────────────────────────────────────────────────────

update:
	bash scripts/update.sh

# ── Linting ────────────────────────────────────────────────────────────────

lint:
	@find argus-node aegis-server calibration -name "*.py" \
	  -not -path "*/__pycache__/*" | xargs python3 -m py_compile
	@echo "  ✓ Python syntax OK"
	@command -v shellcheck >/dev/null 2>&1 && \
	  shellcheck scripts/*.sh argus-node/systemd/*.sh --severity=warning && \
	  echo "  ✓ Shell scripts OK" || echo "  (shellcheck not installed — skipping)"

# ── Cleanup ────────────────────────────────────────────────────────────────

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	find . -name "*.bak_*" -delete 2>/dev/null || true
	rm -rf aegis-shield/dist aegis-shield/node_modules 2>/dev/null || true
	@echo "  ✓ Cleaned"

# ── Help ───────────────────────────────────────────────────────────────────

help:
	@echo ""
	@echo "  AEGIS Platform — Make targets"
	@echo "  ════════════════════════════════════════════════"
	@echo ""
	@echo "  Testing"
	@echo "    make test           Run all tests"
	@echo "    make test-node      ARGUS Node parser (11)"
	@echo "    make test-server    AEGIS Server — alerts + analysis + integrations"
	@echo "    make test-cal       Calibration engine (31)"
	@echo ""
	@echo "  AEGIS Shield (UI)"
	@echo "    make ui-build       Production build → aegis-shield/dist/"
	@echo "    make ui-dev         Dev server on :5173 (hot reload)"
	@echo ""
	@echo "  AEGIS Server"
	@echo "    make server-up      Start Docker stack"
	@echo "    make server-down    Stop Docker stack"
	@echo "    make server-logs    Follow aegis-server logs"
	@echo "    make server-rebuild Rebuild API container"
	@echo "    make server-status  Show container status"
	@echo ""
	@echo "  ARGUS Nodes"
	@echo "    make node-deploy    Print deployment instructions"
	@echo "    make node-start     Start argus-node service (local)"
	@echo "    make node-logs      Follow argus-node journal"
	@echo "    make node-status    Service status"
	@echo ""
	@echo "  Operations"
	@echo "    make healthcheck    Verify full deployment"
	@echo "    make calibrate      Print calibration usage"
	@echo "    make update         Pull + test + rebuild + restart"
	@echo "    make lint           Syntax check Python + shell"
	@echo "    make clean          Remove build artefacts"
	@echo ""
