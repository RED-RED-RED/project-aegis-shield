#!/usr/bin/env bash
# scripts/deploy-argus-node.sh
# ============================================================
# Automated ARGUS Node deployment wizard.
# Run on a fresh Raspberry Pi OS Lite 64-bit (Bookworm) as root.
#
# Usage:
#   sudo bash deploy-argus-node.sh
#
# Unattended (CI / pre-provisioning):
#   sudo bash deploy-argus-node.sh \
#     --unattended \
#     --node-id ARGUS-01 \
#     --server-ip 192.168.1.100 \
#     --mqtt-password your_password
# ============================================================

set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
fail() { echo -e "${RED}  ✗ $*${NC}"; exit 1; }
warn() { echo -e "${YELLOW}  !${NC} $*"; }
info() { echo -e "${BLUE}  →${NC} $*"; }
step() { echo -e "\n${BOLD}[$1/8]${NC} $2"; }

REPO_URL="https://github.com/your-org/aegis-platform.git"
INSTALL_DIR="/opt/argus-node"
CONFIG_DIR="/etc/argus-node"
LOG_DIR="/var/log/argus-node"

# ── Argument parsing ─────────────────────────────────────────────────────
UNATTENDED=false
OPT_NODE_ID=""; OPT_SERVER_IP=""; OPT_MQTT_PASS=""
OPT_MQTT_USER="rid"; OPT_WIFI_IFACE="wlan1"; OPT_SDR=false
OPT_REPO=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --unattended)    UNATTENDED=true;;
    --node-id)       OPT_NODE_ID="$2";    shift;;
    --server-ip)     OPT_SERVER_IP="$2";  shift;;
    --mqtt-password) OPT_MQTT_PASS="$2";  shift;;
    --mqtt-user)     OPT_MQTT_USER="$2";  shift;;
    --wifi-iface)    OPT_WIFI_IFACE="$2"; shift;;
    --enable-sdr)    OPT_SDR=true;;
    --repo)          OPT_REPO="$2";       shift;;
    *) warn "Unknown argument: $1";;
  esac
  shift 2>/dev/null || true
done

[[ -n "$OPT_REPO" ]] && REPO_URL="$OPT_REPO"
[[ $EUID -eq 0 ]]    || fail "Must run as root:  sudo bash $0"

# ── Banner ───────────────────────────────────────────────────────────────
clear
echo ""
echo -e "${BOLD}╔═══════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║        ARGUS Node  —  Deployment Wizard       ║${NC}"
echo -e "${BOLD}║      AEGIS Airspace Protection Platform       ║${NC}"
echo -e "${BOLD}╚═══════════════════════════════════════════════╝${NC}"
echo ""

# ── Interactive prompts ───────────────────────────────────────────────────
if [[ "$UNATTENDED" == false ]]; then
  DEFAULT_ID=$(hostname | tr '[:lower:]' '[:upper:]')
  echo "  Configure this Pi as an ARGUS sensor node."
  echo "  Press Enter to accept defaults shown in [brackets]."
  echo ""

  read -rp "  Node ID [${DEFAULT_ID}]: " NODE_ID
  NODE_ID="${NODE_ID:-$DEFAULT_ID}"

  read -rp "  AEGIS Server IP: " SERVER_IP
  [[ -n "$SERVER_IP" ]] || fail "Server IP is required"

  read -rp "  MQTT username [rid]: " MQTT_USER
  MQTT_USER="${MQTT_USER:-rid}"

  read -rsp "  MQTT password: " MQTT_PASS; echo
  [[ -n "$MQTT_PASS" ]] || fail "MQTT password is required"

  read -rp "  Wi-Fi adapter interface [wlan1]: " WIFI_IFACE
  WIFI_IFACE="${WIFI_IFACE:-wlan1}"

  read -rp "  Enable RTL-SDR scanner? [y/N]: " SDR_RESP
  [[ "${SDR_RESP,,}" == "y" ]] && ENABLE_SDR=true || ENABLE_SDR=false
else
  NODE_ID="${OPT_NODE_ID:-$(hostname | tr '[:lower:]' '[:upper:]')}"
  SERVER_IP="$OPT_SERVER_IP"
  MQTT_USER="$OPT_MQTT_USER"
  MQTT_PASS="$OPT_MQTT_PASS"
  WIFI_IFACE="$OPT_WIFI_IFACE"
  ENABLE_SDR="$OPT_SDR"
  [[ -n "$SERVER_IP" ]] || fail "--server-ip is required in unattended mode"
  [[ -n "$MQTT_PASS"  ]] || fail "--mqtt-password is required in unattended mode"
fi

SDR_STR="false"; [[ "$ENABLE_SDR" == true ]] && SDR_STR="true"

echo ""
info "Node ID  : ${BOLD}${NODE_ID}${NC}"
info "Server   : ${BOLD}${SERVER_IP}${NC}"
info "Wi-Fi    : ${WIFI_IFACE}mon"
info "SDR      : ${SDR_STR}"
echo ""

# ── 1/8  System packages ─────────────────────────────────────────────────
step 1 "System packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  python3 python3-pip python3-venv git curl wget \
  aircrack-ng iw wireless-tools rfkill \
  bluez bluez-tools \
  gpsd gpsd-clients \
  rtl-sdr librtlsdr-dev \
  usbutils
ok "Packages installed"

# ── 2/8  Hardware configuration ──────────────────────────────────────────
step 2 "Hardware configuration"

# RTL-SDR: blacklist DVB driver, add udev rule
echo "blacklist dvb_usb_rtl28xxu" > /etc/modprobe.d/rtl-sdr-blacklist.conf
cat > /etc/udev/rules.d/20-rtlsdr.rules << 'UDEV'
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", \
  GROUP="plugdev", MODE="0666", SYMLINK+="rtl_sdr"
UDEV
udevadm control --reload-rules
ok "RTL-SDR udev rules set"

# Enable UART for GPS
for CFG in /boot/firmware/config.txt /boot/config.txt; do
  [[ -f "$CFG" ]] || continue
  grep -q "enable_uart=1" "$CFG" || echo "enable_uart=1" >> "$CFG"
  ok "UART enabled in $CFG"
  break
done

# Disable serial console (frees UART for GPS)
raspi-config nonint do_serial_cons 1 2>/dev/null && ok "Serial console disabled" || \
  warn "Could not disable serial console via raspi-config — do it manually"

# ── 3/8  Fetch code ───────────────────────────────────────────────────────
step 3 "Fetching AEGIS platform code"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  info "Updating existing install at $INSTALL_DIR"
  git -C "$INSTALL_DIR" pull --ff-only
else
  info "Cloning from $REPO_URL"
  git clone "$REPO_URL" "$INSTALL_DIR" --depth 1
fi
ok "Code ready at $INSTALL_DIR"

# ── 4/8  Python venv ─────────────────────────────────────────────────────
step 4 "Python virtual environment"
python3 -m venv "$INSTALL_DIR/venv" --upgrade-deps
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/venv/bin/pip" install \
  -r "$INSTALL_DIR/argus-node/requirements.txt" -q
ok "Python dependencies installed"

# ── 5/8  Configuration ────────────────────────────────────────────────────
step 5 "Writing node configuration"
mkdir -p "$CONFIG_DIR" "$LOG_DIR"

cat > "$CONFIG_DIR/config.yaml" << YAML
# ARGUS Node Configuration
# Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Node: ${NODE_ID}

node_id: "${NODE_ID}"
site_name: "default"

mqtt:
  host: "${SERVER_IP}"
  port: 1883
  user: "${MQTT_USER}"
  password: "${MQTT_PASS}"
  tls: false
  topic_prefix: "argus"

gps:
  serial_port: "/dev/ttyAMA0"
  baud: 9600

wifi:
  enabled: true
  iface: "${WIFI_IFACE}mon"
  channel: 6

bt:
  enabled: true
  hci_index: 0
  coded_phy: true

sdr:
  enabled: ${SDR_STR}
  device_index: 0

heartbeat_interval_s: 10
dedup_window_s: 2.0
max_queue_size: 1000
YAML
chmod 640 "$CONFIG_DIR/config.yaml"
ok "Config written to $CONFIG_DIR/config.yaml"

# ── 6/8  gpsd ────────────────────────────────────────────────────────────
step 6 "GPS daemon (gpsd)"
cat > /etc/default/gpsd << 'GPSD'
START_DAEMON="true"
GPSD_OPTIONS="-n"
DEVICES="/dev/ttyAMA0"
USBAUTO="false"
GPSD
systemctl enable gpsd --quiet
systemctl start  gpsd 2>/dev/null && ok "gpsd started" || \
  warn "gpsd failed to start — needs GPS module attached and UART enabled (reboot pending)"

# ── 7/8  systemd service ──────────────────────────────────────────────────
step 7 "systemd service"
cp "$INSTALL_DIR/argus-node/systemd/argus-node.service" /etc/systemd/system/
chmod +x "$INSTALL_DIR/argus-node/systemd/setup_interfaces.sh"

# Patch install paths in service file
sed -i \
  -e "s|/opt/argus-node|${INSTALL_DIR}|g" \
  -e "s|ARGUS_CONFIG=.*|ARGUS_CONFIG=${CONFIG_DIR}/config.yaml|" \
  /etc/systemd/system/argus-node.service

systemctl daemon-reload
systemctl enable argus-node.service --quiet
ok "argus-node.service installed and enabled"

# ── 8/8  Hostname ─────────────────────────────────────────────────────────
step 8 "Hostname"
NEW_HOST=$(echo "$NODE_ID" | tr '[:upper:]' '[:lower:]' | tr '_' '-')
if [[ "$(hostname)" != "$NEW_HOST" ]]; then
  hostnamectl set-hostname "$NEW_HOST" 2>/dev/null && ok "Hostname → $NEW_HOST" || \
    warn "Could not set hostname — do manually: sudo hostnamectl set-hostname $NEW_HOST"
else
  ok "Hostname already set ($NEW_HOST)"
fi

# ── Summary ───────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔═══════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║           Deployment Complete!                ║${NC}"
echo -e "${BOLD}╚═══════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${GREEN}Node ID  :${NC} ${NODE_ID}"
echo -e "  ${GREEN}Server   :${NC} ${SERVER_IP}"
echo -e "  ${GREEN}Service  :${NC} argus-node  (auto-starts on boot)"
echo -e "  ${GREEN}Config   :${NC} ${CONFIG_DIR}/config.yaml"
echo -e "  ${GREEN}Logs     :${NC} journalctl -fu argus-node"
echo ""
echo -e "  ${YELLOW}⚠  A reboot is required to activate UART for GPS.${NC}"
echo ""
echo "  Post-reboot verification:"
echo "    sudo systemctl status argus-node"
echo "    sudo journalctl -fu argus-node"
echo ""

if [[ "$UNATTENDED" == false ]]; then
  echo ""
  echo -e "  ${YELLOW}Tip: The Pi 4 runs warm under load. A heatsink is strongly${NC}"
  echo -e "  ${YELLOW}recommended if the node will be deployed in an enclosure.${NC}"
  read -rp "  Reboot now? [Y/n]: " RB
  [[ "${RB,,}" == "n" ]] || reboot
fi
