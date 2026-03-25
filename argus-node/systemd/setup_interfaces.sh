#!/bin/bash
# scripts/setup_interfaces.sh
# ============================================================
# Run before the ARGUS Node starts (ExecStartPre in systemd).
# Sets up Wi-Fi monitor mode and ensures Bluetooth is ready.
#
# Dual-adapter support: wlan2 is configured only when present.
# Single-adapter deployments are fully supported.
# ============================================================

set -euo pipefail

WIFI_IFACE_2G="wlan1"    # Primary adapter — 2.4 GHz
WIFI_IFACE_5G="wlan2"    # Secondary adapter — 5 GHz (optional)
BT_HCI="hci0"

# ---- Helper: put an interface into monitor mode ----
setup_monitor() {
    local iface="$1"
    local label="$2"

    echo "[setup] Configuring monitor mode on $iface ($label)"

    if ip link show "$iface" &>/dev/null; then
        ip link set "$iface" down 2>/dev/null || true
        iw dev "$iface" set type monitor
        ip link set "$iface" up
        echo "[setup] $iface ($label) now in monitor mode"
    else
        echo "[setup] WARNING: $iface not found — skipping ($label)"
    fi
}

# Unblock Wi-Fi in case rfkill is blocking the adapter
rfkill unblock wifi 2>/dev/null || true

# Kill anything that might interfere (NetworkManager, wpa_supplicant)
airmon-ng check kill 2>/dev/null || true

# ---- Primary adapter (2.4 GHz) ----
setup_monitor "$WIFI_IFACE_2G" "2.4 GHz"
iw dev "$WIFI_IFACE_2G" set channel 6 2>/dev/null || true

# ---- Secondary adapter (5 GHz) — only if present ----
if ip link show "$WIFI_IFACE_5G" &>/dev/null; then
    setup_monitor "$WIFI_IFACE_5G" "5 GHz"
    iw dev "$WIFI_IFACE_5G" set channel 36 2>/dev/null || true
else
    echo "[setup] $WIFI_IFACE_5G not found — single-adapter mode (2.4 GHz only)"
fi

# ---- Bluetooth ----
echo "[setup] Bringing up Bluetooth $BT_HCI"
hciconfig "$BT_HCI" up 2>/dev/null || rfkill unblock bluetooth && hciconfig "$BT_HCI" up

# Disable page scan / inquiry scan (we're passive only)
hciconfig "$BT_HCI" noscan 2>/dev/null || true

echo "[setup] Interface setup complete"
exit 0
