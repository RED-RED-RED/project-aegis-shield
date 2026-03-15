#!/bin/bash
# scripts/setup-wireguard.sh
# ============================================================
# Sets up a WireGuard VPN so ARGUS nodes connect to the
# AEGIS platform over an encrypted tunnel — required if nodes
# are deployed outside your LAN (field deployment, rooftops, etc.)
#
# Run on SERVER first, then on each NODE.
#
# Usage:
#   On server:  sudo bash setup-wireguard.sh server
#   On node:    sudo bash setup-wireguard.sh node <SERVER_PUBLIC_IP> <NODE_INDEX>
#
# Example (3 nodes):
#   Server:  sudo bash setup-wireguard.sh server
#   Node 01: sudo bash setup-wireguard.sh node 203.0.113.10 1
#   Node 02: sudo bash setup-wireguard.sh node 203.0.113.10 2
#   Node 03: sudo bash setup-wireguard.sh node 203.0.113.10 3
# ============================================================

set -euo pipefail

ROLE="${1:-}"
SERVER_IP="${2:-}"
NODE_IDX="${3:-1}"

VPN_SUBNET="10.100.0"       # VPN will use 10.100.0.0/24
SERVER_VPN_IP="${VPN_SUBNET}.1"
SERVER_PORT=51820
WG_IFACE="wg0"

if ! command -v wg &>/dev/null; then
    echo "[wg] Installing WireGuard..."
    apt-get update -qq && apt-get install -y wireguard
fi

case "${ROLE}" in

# ── SERVER SETUP ──────────────────────────────────────────────────────────
server)
    echo "[wg] Configuring WireGuard SERVER (${SERVER_VPN_IP})"
    mkdir -p /etc/wireguard && chmod 700 /etc/wireguard

    # Generate server keypair if not present
    if [ ! -f /etc/wireguard/server_private.key ]; then
        wg genkey | tee /etc/wireguard/server_private.key | \
            wg pubkey > /etc/wireguard/server_public.key
        chmod 600 /etc/wireguard/server_private.key
    fi

    SERVER_PRIV=$(cat /etc/wireguard/server_private.key)
    SERVER_PUB=$(cat  /etc/wireguard/server_public.key)

    cat > /etc/wireguard/${WG_IFACE}.conf << EOF
[Interface]
Address    = ${SERVER_VPN_IP}/24
ListenPort = ${SERVER_PORT}
PrivateKey = ${SERVER_PRIV}

# Enable packet forwarding (nodes can reach server services via VPN)
PostUp   = iptables -A FORWARD -i %i -j ACCEPT; iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
PostDown = iptables -D FORWARD -i %i -j ACCEPT; iptables -t nat -D POSTROUTING -o eth0 -j MASQUERADE

# ── Add a [Peer] block below for each ARGUS node ──
# Generate node keys with: wg genkey | tee node01_priv | wg pubkey > node01_pub
# Then add:
#
# [Peer]
# PublicKey  = <NODE_PUBLIC_KEY>
# AllowedIPs = ${VPN_SUBNET}.2/32
#
EOF

    chmod 600 /etc/wireguard/${WG_IFACE}.conf
    systemctl enable wg-quick@${WG_IFACE}
    systemctl restart wg-quick@${WG_IFACE}

    echo ""
    echo "═══════════════════════════════════════════"
    echo "  WireGuard server configured!"
    echo "  Server public key: ${SERVER_PUB}"
    echo "  Server VPN IP:     ${SERVER_VPN_IP}"
    echo ""
    echo "  Next: run this script on each node with:"
    echo "  sudo bash setup-wireguard.sh node <THIS_SERVER_PUBLIC_IP> <NODE_INDEX>"
    echo ""
    echo "  Then add each node's public key to:"
    echo "  /etc/wireguard/${WG_IFACE}.conf as a [Peer]"
    echo "  and run: systemctl restart wg-quick@${WG_IFACE}"
    echo "═══════════════════════════════════════════"
    ;;

# ── NODE SETUP ────────────────────────────────────────────────────────────
node)
    if [ -z "${SERVER_IP}" ]; then
        echo "Error: SERVER_IP required. Usage: $0 node <SERVER_IP> <NODE_INDEX>"
        exit 1
    fi

    NODE_VPN_IP="${VPN_SUBNET}.$(( NODE_IDX + 1 ))"
    echo "[wg] Configuring WireGuard NODE (${NODE_VPN_IP} → ${SERVER_IP})"

    mkdir -p /etc/wireguard && chmod 700 /etc/wireguard

    # Generate node keypair
    if [ ! -f /etc/wireguard/node_private.key ]; then
        wg genkey | tee /etc/wireguard/node_private.key | \
            wg pubkey > /etc/wireguard/node_public.key
        chmod 600 /etc/wireguard/node_private.key
    fi

    NODE_PRIV=$(cat /etc/wireguard/node_private.key)
    NODE_PUB=$(cat  /etc/wireguard/node_public.key)

    # Prompt for server public key
    echo ""
    echo "Enter the SERVER public key (from /etc/wireguard/server_public.key on server):"
    read -r SERVER_PUB_KEY

    cat > /etc/wireguard/${WG_IFACE}.conf << EOF
[Interface]
Address    = ${NODE_VPN_IP}/24
PrivateKey = ${NODE_PRIV}
DNS        = 1.1.1.1

[Peer]
PublicKey  = ${SERVER_PUB_KEY}
Endpoint   = ${SERVER_IP}:${SERVER_PORT}
AllowedIPs = ${VPN_SUBNET}.0/24
# Keep tunnel alive through NAT
PersistentKeepalive = 25
EOF

    chmod 600 /etc/wireguard/${WG_IFACE}.conf
    systemctl enable wg-quick@${WG_IFACE}
    systemctl restart wg-quick@${WG_IFACE}

    echo ""
    echo "═══════════════════════════════════════════"
    echo "  WireGuard node configured!"
    echo "  Node VPN IP:      ${NODE_VPN_IP}"
    echo "  Node public key:  ${NODE_PUB}"
    echo ""
    echo "  Add this [Peer] block to the SERVER's"
    echo "  /etc/wireguard/${WG_IFACE}.conf :"
    echo ""
    echo "  [Peer]"
    echo "  PublicKey  = ${NODE_PUB}"
    echo "  AllowedIPs = ${NODE_VPN_IP}/32"
    echo ""
    echo "  Then restart the server:"
    echo "  sudo systemctl restart wg-quick@${WG_IFACE}"
    echo ""
    echo "  Update argus-node config.yaml:"
    echo "  mqtt.host: ${SERVER_VPN_IP}"
    echo "═══════════════════════════════════════════"
    ;;

*)
    echo "Usage: $0 <server|node> [SERVER_IP] [NODE_INDEX]"
    exit 1
    ;;
esac
