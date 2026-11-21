# KESTREL migration (Phase 2 — infra rename)

This is the fleet cutover for the deep rename: **AEGIS/ARGUS → KESTREL** across
directories, services, MQTT topics, env vars, paths, and node callsigns
(`ARGUS-0x → KN-0x`). The code side is done on the `kestrel-migration` branch;
this runbook is the Layer B work you run on the server and the three nodes.

## What changes

| Old | New |
|-----|-----|
| `aegis-server/` | `kestrel-server/` |
| `aegis-shield/` | `kestrel-dashboard/` |
| `argus-node/` | `kestrel-node/` |
| MQTT topic prefix `argus` (`argus/<node>/…`) | `kestrel` (`kestrel/<node>/…`) |
| env vars `AEGIS_*`, `ARGUS_*` | `KESTREL_*` (e.g. `AEGIS_API_KEY → KESTREL_API_KEY`) |
| `/opt`, `/etc`, `/var/lib`, `/var/log` `/argus-node` | `…/kestrel-node` |
| `argus-node.service` | `kestrel-node.service` |
| host/DNS `aegis-server` | `kestrel-server` |
| node callsigns `ARGUS-01/02/03` | `KN-01/02/03` |
| GitHub repo `project-aegis-shield` | `project-kestrel` |

**Not changed:** the TimescaleDB database (`ridmesh`), the DB/MQTT compose vars
(`DB_NAME`, `MQTT_USER`, …), the CARTO key var (`VITE_CARTO_API_KEY`). Your
7-day history, flights, and registry annotations are untouched.

## Cutover style

This is a **hard cutover**: the server and all three nodes flip to `kestrel/`
topics together in one maintenance window. Detections stop for the length of the
window (budget ~30 min). Pick a quiet time.

## 0. Before the window

1. Review and merge the `kestrel-migration` branch to `main`.
2. Rename the GitHub repo `project-aegis-shield → project-kestrel`
   (Settings → General → Rename). GitHub redirects old URLs, but update local
   remotes: `git remote set-url origin https://github.com/RED-RED-RED/project-kestrel.git`

## 1. Server

```bash
# stop the old stack (volumes persist; DB name ridmesh is unchanged)
cd <checkout>/aegis-server/docker && docker compose down

# get the renamed code (re-clone, or pull the merged main and move the dir)
cd ~ && git clone https://github.com/RED-RED-RED/project-kestrel.git
cd project-kestrel/kestrel-server/docker
cp <old>/.env .env            # carry over your real passwords/keys
```

Edit `.env`: rename any `AEGIS_*` keys to `KESTREL_*` (only `AEGIS_API_KEY` is
likely set). Leave `DB_*` and `MQTT_*` as they are.

**Host/DNS rename** (`aegis-server → kestrel-server`):
- Tailscale admin console → rename the machine to `kestrel-server`, confirm
  MagicDNS is on so nodes resolve the new name.
- On the box: `sudo hostnamectl set-hostname kestrel-server` and update
  `/etc/hosts` if you pin it there.

```bash
docker compose up -d --build
docker compose logs -f kestrel-server   # expect: subscribed to kestrel/#
```

Open `http://kestrel-server/` — dashboard should load (still no nodes yet).

## 2. Each node (KN-01, KN-02, KN-03)

Cleanest path is a fresh re-deploy to the new paths. **Preserve the survey**
first so the node keeps its calibrated position:

```bash
sudo systemctl stop argus-node && sudo systemctl disable argus-node
sudo cp /var/lib/argus-node/survey.json /tmp/survey.json   # keep the position fix
```

Re-run the installer with the KN callsign and the new server name:

```bash
curl -fsSL https://raw.githubusercontent.com/RED-RED-RED/project-kestrel/main/scripts/deploy-kestrel-node.sh \
  | sudo bash -s -- --node-id KN-01 --server-ip kestrel-server --mqtt-pass '<pass>'
# repeat on the other nodes with --node-id KN-02, KN-03
```

Restore the survey and restart:

```bash
sudo cp /tmp/survey.json /var/lib/kestrel-node/survey.json
sudo systemctl restart kestrel-node
journalctl -u kestrel-node -f    # expect: publishing kestrel/KN-01/detection …
```

If you'd rather move in place instead of re-cloning, the manual equivalent is:
`sudo mv /opt/argus-node /opt/kestrel-node`, same for `/etc`, `/var/lib`,
`/var/log`; edit `/etc/kestrel-node/config.yaml` to set `topic_prefix: kestrel`,
`node_id: KN-0x`, and the `kestrel-server` host/fallback; install the new
`kestrel-node.service`; `systemctl daemon-reload && systemctl enable --now
kestrel-node`; then remove the old `argus-node.service`.

## 3. Verify

- Dashboard `http://kestrel-server/` shows **KN-01/02/03 online**.
- Live map picks up a test flight; detections flow on `kestrel/#`.
- `System → health` shows all three nodes reporting.

## 4. Cleanup

- Remove leftover `/opt/argus-node` etc. if you moved rather than re-cloned.
- Delete the old server checkout once the new stack is confirmed.

## Rollback

The `main` branch before the merge still carries the AEGIS/ARGUS names. If the
window goes sideways: redeploy the old server stack and re-point nodes at the
old service/config. The database is shared and untouched, so there is no data
loss either direction.
