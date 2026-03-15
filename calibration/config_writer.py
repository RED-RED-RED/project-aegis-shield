"""
calibration/config_writer.py
============================
Writes calibrated parameters back to the codebase in two ways:

  1. YAML calibration file  — read by the trilateration engine at startup.
     Stored at server/analysis/calibration.yaml (or path from env var).
     Contains per-node overrides + global fallback.

  2. Trilateration patch   — optional direct update to trilateration.py
     global constants (RSSI_REF_DBM, PATH_LOSS_EXP) if only one node
     is deployed or user requests a simple single-constant update.

The trilateration engine is updated to load calibration.yaml on startup.
Per-node values override the global constants for that node's observations.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from calibration.engine import CalibrationResult

log = logging.getLogger("calibration.writer")

DEFAULT_YAML_PATH = Path(__file__).parent.parent / "aegis-server" / "analysis" / "calibration.yaml"
TRILATERATION_PY  = Path(__file__).parent.parent / "aegis-server" / "analysis" / "trilateration.py"


def write_calibration_yaml(
    results: dict[str, CalibrationResult],
    output_path: Optional[Path] = None,
    notes: str = "",
) -> Path:
    """
    Write per-node calibration parameters to YAML.

    Format:
        calibrated_at: "2024-03-15T10:30:00Z"
        notes: "Field calibration, open field, 3 nodes"
        global:
          rssi_ref_dbm: -20.0
          path_loss_exp: 2.7
        nodes:
          ARGUS-01:
            rssi_ref_dbm: -18.4
            path_loss_exp: 2.45
            quality: excellent
            r_squared: 0.912
            rmse_m: 48.2
            sample_count: 347
          ARGUS-02:
            ...
    """
    out_path = output_path or DEFAULT_YAML_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Compute weighted global fallback (weighted by sample count × R²)
    global_n    = _weighted_mean(results, "fitted_n")
    global_rref = _weighted_mean(results, "fitted_rssi_ref")

    doc = {
        "calibrated_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "notes":         notes or f"Calibrated from {len(results)} nodes",
        "global": {
            "rssi_ref_dbm":  round(global_rref, 2),
            "path_loss_exp": round(global_n, 3),
        },
        "nodes": {}
    }

    for node_id, r in results.items():
        if node_id == "__global__":
            continue
        doc["nodes"][node_id] = r.to_config_dict()
        if r.warnings:
            doc["nodes"][node_id]["warnings"] = r.warnings

    with open(out_path, "w") as f:
        yaml.dump(doc, f, default_flow_style=False, sort_keys=False)

    log.info(f"Calibration YAML written to {out_path}")
    return out_path


def patch_trilateration_py(
    result: CalibrationResult,
    py_path: Optional[Path] = None,
    dry_run: bool = False,
) -> str:
    """
    Directly patch the global constants in trilateration.py.
    Creates a backup first. Returns diff summary.

    Only appropriate when a single global model is desired (e.g. single-node
    deployment or when all nodes have similar fitted values).
    """
    path = py_path or TRILATERATION_PY
    if not path.exists():
        raise FileNotFoundError(f"trilateration.py not found at {path}")

    content = path.read_text()

    # Find and replace RSSI_REF_DBM and PATH_LOSS_EXP lines
    new_rref = result.fitted_rssi_ref
    new_n    = result.fitted_n

    old_rref_line = re.search(r"^RSSI_REF_DBM\s*=.*$", content, re.MULTILINE)
    old_n_line    = re.search(r"^PATH_LOSS_EXP\s*=.*$", content, re.MULTILINE)

    if not old_rref_line or not old_n_line:
        raise ValueError("Could not find RSSI_REF_DBM or PATH_LOSS_EXP in trilateration.py")

    old_rref = old_rref_line.group(0)
    old_n    = old_n_line.group(0)

    new_rref_line = (f"RSSI_REF_DBM    = {new_rref:.2f}    "
                     f"# Calibrated {datetime.now().strftime('%Y-%m-%d')} "
                     f"(R²={result.r_squared:.3f}, RMSE={result.rmse_m:.0f}m, "
                     f"n={len(result.residuals_m)} samples)")
    new_n_line    = (f"PATH_LOSS_EXP   = {new_n:.3f}      "
                     f"# Calibrated from {result.node_id}")

    diff = (
        f"  RSSI_REF_DBM:  {old_rref.split('=')[1].split('#')[0].strip()} "
        f"→ {new_rref:.2f} dBm\n"
        f"  PATH_LOSS_EXP: {old_n.split('=')[1].split('#')[0].strip()} "
        f"→ {new_n:.3f}"
    )

    if dry_run:
        log.info(f"[DRY RUN] Would patch trilateration.py:\n{diff}")
        return diff

    # Backup original
    backup_path = path.with_suffix(f".py.bak_{int(time.time())}")
    backup_path.write_text(content)
    log.info(f"Backup created: {backup_path}")

    # Apply patches
    content = content.replace(old_rref, new_rref_line, 1)
    content = content.replace(old_n,    new_n_line,    1)
    path.write_text(content)

    log.info(f"Patched trilateration.py:\n{diff}")
    return diff


def generate_patch_snippet(results: dict[str, CalibrationResult]) -> str:
    """
    Generate a human-readable config snippet the user can paste into
    their config if they prefer manual application.
    """
    lines = [
        "# ── AEGIS Calibration Results ──────────────────────────────",
        f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "#",
        "# Paste into server/analysis/calibration.yaml",
        "# or set ARGUS_CALIBRATION_PATH env var to point at a custom file",
        "#",
    ]

    for node_id, r in results.items():
        if node_id == "__global__":
            continue
        q_icon = "✓" if r.quality in ("excellent","good") else "!"
        lines += [
            f"# {q_icon} {node_id}  ({r.quality})",
            f"#   n={r.fitted_n:.3f}  RSSI_ref={r.fitted_rssi_ref:.1f}dBm  "
            f"R²={r.r_squared:.3f}  RMSE={r.rmse_m:.0f}m  "
            f"p68={r.percentile_68_m:.0f}m  n_samples={r.sample_count}",
        ]
        if r.warnings:
            for w in r.warnings:
                lines.append(f"#   ⚠ {w}")
    return "\n".join(lines)


def _weighted_mean(results: dict[str, CalibrationResult], attr: str) -> float:
    total_w, total_wv = 0.0, 0.0
    for r in results.values():
        if r.sample_count < 5:
            continue
        w = r.sample_count * max(r.r_squared, 0.1)
        total_w  += w
        total_wv += w * getattr(r, attr)
    if total_w == 0:
        return getattr(next(iter(results.values())), attr)
    return total_wv / total_w
