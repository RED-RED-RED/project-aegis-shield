#!/bin/bash
# install.sh
# ============================================================
# One-shot installer for AEGIS ARGUS Node on a fresh
# Raspberry Pi OS Lite (64-bit, Bookworm).
#
# Run as root:  sudo bash install.sh
# ============================================================

set -euo pipefail

APP_DIR="/opt/argus-node"
CONFIG_DIR="/etc/argus-node"
LOG_DIR="/var/log/argus-node"

echo "═══════════════════════════════════════════"
echo "  AEGIS ARGUS Node Installer"
echo "═══════════════════════════════════════════"

# ---- System packages ----
echo "[1/7] Installing system packages…"
apt-get update -qq
apt-get install -y \
    python3 python3-pip python3-venv \
    aircrack-ng iw wireless-tools \
    rtl-sdr librtlsdr-dev \
    gpsd gpsd-clients \
    bluez bluez-tools \
    git curl wget \
    --no-install-recommends

# RTL-SDR udev rules (so non-root can access the device)
cat > /etc/udev/rules.d/20-rtlsdr.rules << 'EOF'
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0666", SYMLINK+="rtl_sdr"
EOF
udevadm control --reload-rules

# ---- Application directory ----
echo "[2/7] Setting up application directory at $APP_DIR…"
mkdir -p "$APP_DIR" "$CONFIG_DIR" "$LOG_DIR"
cp -r . "$APP_DIR/"
chmod +x "$APP_DIR/systemd/setup_interfaces.sh"
ln -sf "$APP_DIR/systemd/setup_interfaces.sh" /usr/local/bin/rid-setup-ifaces

# ---- Python venv ----
echo "[3/7] Creating Python virtual environment…"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip -q
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
echo "     Python deps installed."

# ---- Config ----
echo "[4/7] Installing default config…"
if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
    cp "$APP_DIR/config/config.example.yaml" "$CONFIG_DIR/config.yaml"
    echo "     Created $CONFIG_DIR/config.yaml — EDIT THIS before starting!"
else
    echo "     Config already exists — skipping."
fi

# ---- gpsd config ----
echo "[5/7] Configuring gpsd…"
cat > /etc/default/gpsd << 'EOF'
START_DAEMON="true"
GPSD_OPTIONS="-n"
DEVICES="/dev/ttyAMA0"
USBAUTO="false"
EOF
systemctl enable gpsd
systemctl start gpsd || true

# Disable serial console so UART is free for GPS
if ! grep -q "enable_uart=1" /boot/firmware/config.txt 2>/dev/null; then
    echo "enable_uart=1" >> /boot/firmware/config.txt
    echo "     Enabled UART in /boot/firmware/config.txt (reboot needed)"
fi

# ---- systemd service ----
echo "[6/7] Installing systemd service…"
cp "$APP_DIR/systemd/argus-node.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable argus-node.service
echo "     Service installed (not started yet — configure first)."

# ---- nRF52840 BT5 firmware note ----
echo "[7/7] Bluetooth setup note…"
cat << 'EOF'

  ┌─────────────────────────────────────────────────────────┐
  │  nRF52840 BT5 Long Range setup                          │
  │                                                         │
  │  Flash the Zephyr HCI UART firmware to your dongle:     │
  │  https://github.com/zephyrproject-rtos/zephyr           │
  │  samples/bluetooth/hci_usb                              │
  │                                                         │
  │  This gives you a standard HCI USB device with          │
  │  full Coded PHY support under BlueZ.                    │
  └─────────────────────────────────────────────────────────┘

EOF

echo ""
echo "═══════════════════════════════════════════"
echo "  Installation complete!"
echo ""
echo "  Next steps:"
echo "  1. Edit $CONFIG_DIR/config.yaml"
echo "     - Set node_id, mqtt.host, mqtt.password"
echo "  2. Reboot (for UART/GPS to take effect)"
echo "  3. sudo systemctl start argus-node"
echo "  4. sudo journalctl -fu argus-node"
echo "═══════════════════════════════════════════"
