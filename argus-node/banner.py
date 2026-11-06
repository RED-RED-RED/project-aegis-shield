"""ASCII startup banner for the ARGUS node agent (plain ASCII, journald-safe)."""

_ART = r"""
    _    ____   ____ _   _ ____
   / \  |  _ \ / ___| | | / ___|
  / _ \ | |_) | |  _| | | \___ \
 / ___ \|  _ <| |_| | |_| |___) |
/_/   \_\_| \_\\____|\___/|____/
"""


def banner(node_id: str, site: str) -> str:
    return (
        _ART
        + f"  AEGIS sensor node  |  {node_id}  |  site: {site}\n"
        + "  Remote ID: WiFi NAN + Beacon, BT4/BT5 LR, 915 MHz SDR, GNSS\n"
    )
