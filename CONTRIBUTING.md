# Contributing to AEGIS Platform

## Repository layout

```
aegis-platform/
├── argus-node/        ARGUS sensor node (Python, runs on each Pi)
├── aegis-server/      AEGIS central server (FastAPI + TimescaleDB)
├── aegis-shield/      AEGIS Shield dashboard (React + Leaflet)
├── calibration/       RSSI path-loss calibration utility
├── scripts/           Deployment and maintenance scripts
└── docs/              Architecture docs and hardware guides
```

## Development setup

```bash
# Clone
git clone https://github.com/your-org/aegis-platform
cd aegis-platform

# Python deps (for server + calibration)
pip install -r aegis-server/requirements.txt
pip install -r calibration/requirements.txt
pip install pytest pytest-asyncio

# UI deps
cd aegis-shield && npm install && cd ..

# Run all tests
make test

# Start server stack locally (requires Docker)
make server-up

# Start UI dev server (proxies to localhost:8000)
make ui-dev
```

## Running tests

```bash
make test                          # all 83 tests
make test-node                     # ARGUS node parser tests (11)
make test-server                   # AEGIS server tests (41)
python -m pytest calibration/tests # calibration tests (31)
```

## Pull request checklist

- [ ] All 83 tests pass (`make test`)
- [ ] UI builds without errors (`make ui-build`)
- [ ] New code has corresponding tests
- [ ] MQTT topic names use `argus/` prefix
- [ ] Node IDs use `ARGUS-NN` format
- [ ] No credentials committed (check `.gitignore`)
- [ ] Deployment scripts updated if install paths changed

## Coding conventions

**Python:** PEP 8, type hints on public functions, docstrings on modules.
Logging via `logging.getLogger(__name__)` — no `print()` in production code.

**JavaScript/JSX:** Functional components, hooks only. State in Zustand store,
not component-local state unless purely UI. CSS-in-JS via template literals
at top of each component file.

**MQTT topics:** Always `argus/<node_id>/<message_type>` — never hardcode
the prefix, use `settings.mqtt_topic_prefix`.

**Naming:** ARGUS nodes, AEGIS server, AEGIS Shield (UI). See README for
the complete naming map.
