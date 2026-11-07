"""
core/server_health.py
======================
Periodically samples the AEGIS server box's own health (cpu/mem/disk, uptime,
DB and MQTT reachability) and writes it to node_health under a reserved
node_id. Nodes report their own health via heartbeats; the server has no
heartbeat, so it samples itself here. The System tab then charts nodes and the
server together.
"""

import asyncio
import logging
import time

import psutil

from db.database import get_pool

log = logging.getLogger("server-health")

# Reserved node_id for the server's own rows in node_health. The UI labels it
# "AEGIS Server"; it is never a real ARGUS node.
SERVER_ID = "__server__"

_START = time.time()


async def sample_once() -> None:
    """Take one health reading and insert it into node_health."""
    pool = await get_pool()
    cpu  = psutil.cpu_percent(interval=None)
    mem  = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    uptime = int(time.time() - _START)

    db_ok = False
    mqtt_ok = False
    async with pool.acquire() as conn:
        try:
            await conn.fetchval("SELECT 1")
            db_ok = True
            # MQTT is considered healthy if any node heartbeat arrived recently.
            last_hb = await conn.fetchval(
                "SELECT MAX(last_seen) FROM nodes "
                "WHERE last_seen > NOW() - INTERVAL '2 minutes'"
            )
            mqtt_ok = last_hb is not None
        except Exception:
            pass

        await conn.execute("""
            INSERT INTO node_health
                (node_id, cpu_pct, mem_pct, disk_pct, uptime_s, db_ok, mqtt_ok)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
        """, SERVER_ID, cpu, mem, disk, uptime, db_ok, mqtt_ok)


async def run_sampler(interval_s: int) -> None:
    """Sample the server's health every interval_s seconds until cancelled."""
    psutil.cpu_percent(interval=None)   # prime; first reading is otherwise 0
    log.info(f"Server health sampler running (every {interval_s}s)")
    while True:
        try:
            await sample_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug(f"server health sample failed: {e}")
        await asyncio.sleep(interval_s)
