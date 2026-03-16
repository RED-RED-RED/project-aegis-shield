#!/bin/bash
# scripts/setup_interfaces.sh
# ============================================================
# Run before the ARGUS Node starts (ExecStartPre in systemd).
# Sets up Wi-Fi monitor mode and ensures Bluetooth is ready.
# ============================================================

set -euo pipefail

WIFI_PHY="phy1"          # Change to phy0 if Alfa is your only card
WIFI_IFACE="wlan1"       # Physical interface name
MON_IFACE="wlan1mon"     # Monitor mode interface name
BT_HCI="hci0"

echo "[setup] Configuring Wi-Fi monitor mode on $WIFI_IFACE → $MON_IFACE"

# Unblock Wi-Fi in case rfkill is blocking the adapter
rfkill unblock wifi 2>/dev/null || true

# Kill anything that might interfere (NetworkManager, wpa_supplicant)
# Only kill processes tied to our scan interface, not the system Wi-Fi
airmon-ng check kill 2>/dev/null || true

# Bring up monitor interface
if ip link show "$MON_IFACE" &>/dev/null; then
    echo "[setup] Monitor interface $MON_IFACE already exists — skipping"
else
    ip link set "$WIFI_IFACE" down 2>/dev/null || true
    iw dev "$WIFI_IFACE" set type monitor
    ip link set "$WIFI_IFACE" name "$MON_IFACE"
    ip link set "$MON_IFACE" up
    echo "[setup] Monitor interface $MON_IFACE created"
fi

# Set initial channel
iw dev "$MON_IFACE" set channel 6 || true

# ---- Bluetooth ----
echo "[setup] Bringing up Bluetooth $BT_HCI"
hciconfig "$BT_HCI" up 2>/dev/null || rfkill unblock bluetooth && hciconfig "$BT_HCI" up

# Disable page scan / inquiry scan (we're passive only)
hciconfig "$BT_HCI" noscan 2>/dev/null || true

echo "[setup] Interface setup complete"
exit 0
