#!/usr/bin/env python3
"""
scripts/healthcheck.py
======================
Quick deployment verification. Run from anywhere on your network
to check that all components are reachable and functioning.

Usage:
    python healthcheck.py --server 192.168.1.100

Output is colour-coded: green ✓ passing, red ✗ failing.
"""

import argparse
import json
import socket
import sys
import time
from urllib.request import urlopen, Request
from urllib.error import URLError


RESET  = "\033[0m"
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
BOLD   = "\033[1m"


def ok(msg):   print(f"  {GREEN}✓{RESET} {msg}")
def fail(msg): print(f"  {RED}✗{RESET} {msg}")
def warn(msg): print(f"  {YELLOW}!{RESET} {msg}")
def head(msg): print(f"\n{BOLD}{msg}{RESET}")


def check_tcp(host, port, label, timeout=3):
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        ok(f"{label} ({host}:{port})")
        return True
    except Exception as e:
        fail(f"{label} ({host}:{port}) — {e}")
        return False


def check_http(url, label, expected_key=None, timeout=5):
    try:
        req = Request(url, headers={"Accept": "application/json"})
        with urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read())
        if expected_key and expected_key not in body:
            fail(f"{label} — response missing '{expected_key}'")
            return False
        ok(f"{label}")
        return body
    except URLError as e:
        fail(f"{label} — {e.reason}")
        return False
    except Exception as e:
        fail(f"{label} — {e}")
        return False


def check_websocket(host, port, label, timeout=4):
    """Basic TCP handshake check for WS port."""
    return check_tcp(host, port, label, timeout)


def main():
    parser = argparse.ArgumentParser(description="AEGIS health check")
    parser.add_argument("--server", default="localhost", help="Server IP/hostname")
    parser.add_argument("--api-port",   type=int, default=8000)
    parser.add_argument("--http-port",  type=int, default=80)
    parser.add_argument("--mqtt-port",  type=int, default=1883)
    parser.add_argument("--db-port",    type=int, default=5432)
    args = parser.parse_args()

    h = args.server
    print(f"\n{BOLD}AEGIS Deployment Health Check{RESET}")
    print(f"Target server: {BOLD}{h}{RESET}")
    print("─" * 44)

    results = {}

    # ── Network connectivity ───────────────────────────────────────────────
    head("Network")
    results["mqtt"]  = check_tcp(h, args.mqtt_port,  "MQTT broker (Mosquitto)")
    results["api"]   = check_tcp(h, args.api_port,   "FastAPI backend")
    results["http"]  = check_tcp(h, args.http_port,  "Nginx / AEGIS Shield")
    results["db"]    = check_tcp(h, args.db_port,    "TimescaleDB")

    # ── API endpoints ──────────────────────────────────────────────────────
    head("API Endpoints")
    base = f"http://{h}:{args.api_port}"

    health = check_http(f"{base}/health", "/health", expected_key="status")
    if health:
        ok(f"  Server version: {health.get('version','—')}")

    nodes_resp = check_http(f"{base}/api/nodes", "GET /api/nodes")
    if isinstance(nodes_resp, list):
        online = [n for n in nodes_resp if n.get("status") == "online"]
        ok(f"  {len(online)}/{len(nodes_resp)} nodes online")
        for node in nodes_resp:
            status = node.get("status", "unknown")
            color = GREEN if status == "online" else RED
            print(f"    {color}{'●' if status=='online' else '○'}{RESET} "
                  f"{node['node_id']} — {status} "
                  f"(CPU {node.get('cpu_pct','—')}% MEM {node.get('mem_pct','—')}%)")

    drones_resp = check_http(f"{base}/api/detections/tracks", "GET /api/detections/tracks")
    if isinstance(drones_resp, list):
        no_rid = [d for d in drones_resp if not d.get("has_valid_rid")]
        ok(f"  {len(drones_resp)} active drones ({len(no_rid)} with no RID)")

    alerts_resp = check_http(f"{base}/api/alerts?acknowledged=false&limit=10",
                              "GET /api/alerts (unacknowledged)")
    if isinstance(alerts_resp, list) and len(alerts_resp) > 0:
        high = [a for a in alerts_resp if a.get("level") == "high"]
        if high:
            warn(f"  {len(high)} HIGH alerts outstanding:")
            for a in high[:3]:
                print(f"    {RED}!{RESET} {a['title']}")

    check_http(f"{base}/api/analysis/stats", "GET /api/analysis/stats")

    # ── AEGIS Shield ──────────────────────────────────────────────────────────
    head("AEGIS Shield")
    try:
        req = Request(f"http://{h}:{args.http_port}/",
                      headers={"Accept": "text/html"})
        with urlopen(req, timeout=5) as r:
            body = r.read().decode()
        if "RID" in body or "argus" in body.lower():
            ok("AEGIS Shield HTML served correctly")
        else:
            warn("AEGIS Shield reachable but RID content not detected")
    except Exception as e:
        fail(f"AEGIS Shield — {e}")

    # ── Summary ────────────────────────────────────────────────────────────
    head("Summary")
    passed = sum(1 for v in results.values() if v)
    total  = len(results)
    color  = GREEN if passed == total else RED
    print(f"  {color}{passed}/{total} core services reachable{RESET}\n")

    if passed < total:
        print(f"  {YELLOW}Tip: run 'make server-logs' to check for errors{RESET}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
