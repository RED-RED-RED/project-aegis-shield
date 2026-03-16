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
#
# MQTT username is always "rid" — set by the AEGIS Server at deploy time.
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

REPO_URL="https://github.com/RED-RED-RED/project-aegis-shield.git"
INSTALL_DIR="/opt/argus-node"
CONFIG_DIR="/etc/argus-node"
LOG_DIR="/var/log/argus-node"

# ── Argument parsing ─────────────────────────────────────────────────────
UNATTENDED=false
OPT_NODE_ID=""; OPT_SERVER_IP=""; OPT_MQTT_PASS=""
OPT_WIFI_IFACE="wlan1"; OPT_SDR=false
OPT_REPO=""; OPT_GPS_MODE="usb"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --unattended)    UNATTENDED=true;;
    --node-id)       OPT_NODE_ID="$2";    shift;;
    --server-ip)     OPT_SERVER_IP="$2";  shift;;
    --mqtt-password) OPT_MQTT_PASS="$2";  shift;;
    --wifi-iface)    OPT_WIFI_IFACE="$2"; shift;;
    --enable-sdr)    OPT_SDR=true;;
    --repo)          OPT_REPO="$2";       shift;;
    --gps-mode)      OPT_GPS_MODE="$2";   shift;;
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

  echo "  Node ID"
  echo "  A unique name for this sensor node — used in MQTT topics"
  echo "  and displayed in the AEGIS Shield dashboard."
  read -rp "    ID [${DEFAULT_ID}]: " NODE_ID
  NODE_ID="${NODE_ID:-$DEFAULT_ID}"
  echo ""

  echo "  AEGIS Server IP"
  echo "  The IP address of the machine running the AEGIS Server stack."
  echo "  Check DEPLOYMENT.md on the server if you are unsure."
  read -rp "    Server IP: " SERVER_IP
  [[ -n "$SERVER_IP" ]] || fail "Server IP is required"
  echo ""

  echo "  MQTT password"
  echo "  The MQTT broker password set during AEGIS Server deployment."
  echo "  Find it in DEPLOYMENT.md or aegis-server/docker/.env on the server."
  while true; do
    read -rsp "    Password: " MQTT_PASS; echo
    [[ -n "$MQTT_PASS" ]] || { echo -e "${RED}  Password is required.${NC}"; continue; }
    read -rsp "    Confirm password: " MQTT_PASS_CONFIRM; echo
    [[ "$MQTT_PASS" == "$MQTT_PASS_CONFIRM" ]] && break
    echo -e "${RED}  Passwords do not match. Try again.${NC}"
  done
  MQTT_USER="rid"
  echo ""

  read -rp "  Wi-Fi adapter interface [wlan1]: " WIFI_IFACE
  WIFI_IFACE="${WIFI_IFACE:-wlan1}"

  read -rp "  Enable RTL-SDR scanner? [y/N]: " SDR_RESP
  [[ "${SDR_RESP,,}" == "y" ]] && ENABLE_SDR=true || ENABLE_SDR=false

  read -rp "  GPS mode — usb (NEO-M9N) or uart (legacy NEO-M8N) [usb]: " GPS_MODE_RESP
  GPS_MODE="${GPS_MODE_RESP:-usb}"
else
  NODE_ID="${OPT_NODE_ID:-$(hostname | tr '[:lower:]' '[:upper:]')}"
  SERVER_IP="$OPT_SERVER_IP"
  MQTT_USER="rid"
  MQTT_PASS="$OPT_MQTT_PASS"
  WIFI_IFACE="$OPT_WIFI_IFACE"
  ENABLE_SDR="$OPT_SDR"
  GPS_MODE="$OPT_GPS_MODE"
  [[ -n "$SERVER_IP" ]] || fail "--server-ip is required in unattended mode"
  [[ -n "$MQTT_PASS"  ]] || fail "--mqtt-password is required in unattended mode"
fi

SDR_STR="false"; [[ "$ENABLE_SDR" == true ]] && SDR_STR="true"
GPS_MODE="${GPS_MODE:-usb}"

echo ""
info "Node ID  : ${BOLD}${NODE_ID}${NC}"
info "Server   : ${BOLD}${SERVER_IP}${NC}"
info "Wi-Fi    : ${WIFI_IFACE}mon"
info "SDR      : ${SDR_STR}"
info "GPS mode : ${GPS_MODE}"
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
ok "RTL-SDR udev rules set"

# u-blox USB GPS udev rule — stable /dev/ttyGPS symlink for NEO-M9N
# Vendor 1546 = u-blox, Product 01a9 = NEO-M9N
cat > /etc/udev/rules.d/21-ublox-gps.rules << 'UDEV'
SUBSYSTEM=="tty", ATTRS{idVendor}=="1546", ATTRS{idProduct}=="01a9", \
  SYMLINK+="ttyGPS", MODE="0666"
UDEV
ok "u-blox GPS udev rule set (/dev/ttyGPS symlink for NEO-M9N)"

udevadm control --reload-rules
udevadm trigger

# UART-specific setup — only needed for legacy UART GPS (mode=uart)
if [[ "$GPS_MODE" == "uart" ]]; then
  info "Configuring UART for legacy GPS…"
  for CFG in /boot/firmware/config.txt /boot/config.txt; do
    [[ -f "$CFG" ]] || continue
    grep -q "enable_uart=1" "$CFG" || echo "enable_uart=1" >> "$CFG"
    ok "UART enabled in $CFG"
    break
  done
  raspi-config nonint do_serial_cons 1 2>/dev/null && ok "Serial console disabled" || \
    warn "Could not disable serial console via raspi-config — do it manually"
else
  info "USB GPS mode — UART configuration skipped"
fi

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
  serial_port: "/dev/ttyACM0"   # USB GPS (NEO-M9N default)
  # serial_port: "/dev/ttyAMA0" # UART GPS (legacy)
  baud: 9600
  mode: "${GPS_MODE}"
  auto_detect: true

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

if [[ "$GPS_MODE" == "uart" ]]; then
  GPS_DEVICE="/dev/ttyAMA0"
  GPSD_USBAUTO="false"
else
  GPS_DEVICE="/dev/ttyACM0"
  GPSD_USBAUTO="true"
fi

cat > /etc/default/gpsd << GPSD
START_DAEMON="true"
GPSD_OPTIONS="-n"
DEVICES="${GPS_DEVICE}"
USBAUTO="${GPSD_USBAUTO}"
GPSD
systemctl enable gpsd --quiet
systemctl start  gpsd 2>/dev/null && ok "gpsd started" || \
  warn "gpsd failed to start — GPS module may need to be attached first"

# Post-install GPS device check
if [[ "$GPS_MODE" != "uart" ]]; then
  echo ""
  if [[ -e "/dev/ttyACM0" || -e "/dev/ttyGPS" ]]; then
    ok "GPS device found: $(ls /dev/ttyACM0 /dev/ttyGPS 2>/dev/null | head -1)"
  else
    warn "GPS device not found at /dev/ttyACM0 or /dev/ttyGPS"
    warn "Plug in the u-blox NEO-M9N USB receiver, then run:"
    warn "  ls /dev/ttyACM* /dev/ttyGPS"
    warn "  sudo systemctl restart argus-node"
  fi
fi

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
if [[ "$GPS_MODE" == "uart" ]]; then
  echo -e "  ${YELLOW}⚠  A reboot is required to activate UART for GPS.${NC}"
  echo ""
fi
echo "  Post-deployment verification:"
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
