"""
calibration/engine.py
=====================
Fits the RSSI path-loss model parameters (RSSI_REF, n) from a calibration
flight dataset.

Model:  RSSI = RSSI_REF - 10 * n * log10(d)

Linearised:  RSSI = a + b * log10(d)
             where a = RSSI_REF, b = -10*n

This is a standard ordinary least-squares problem after the log transform.

Inputs
------
A list of CalibrationSamples, each containing:
  - node_id:   which ARGUS node recorded it
  - rssi:      dBm reading from that node
  - true_dist: ground-truth distance (metres) from node to drone

Output
------
Per-node CalibrationResult with:
  - fitted_rssi_ref  (dBm)
  - fitted_n
  - r_squared        (goodness of fit, 0–1)
  - rmse_m           (root-mean-square distance error, metres)
  - residuals        (per-point, for plotting / QA)
  - sample_count
  - warnings         (list of human-readable caution strings)
"""

import logging
import math
import statistics
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

log = logging.getLogger("calibration.engine")

# ── Physical limits for sanity-checking fitted parameters ─────────────────
N_MIN, N_MAX               = 1.5, 5.0
RSSI_REF_MIN, RSSI_REF_MAX = -60.0, 10.0
MIN_SAMPLES_PER_NODE       = 20
MIN_DIST_M                 = 5.0
MAX_DIST_M                 = 2000.0
MIN_RSSI_DBM               = -105.0   # Below this is noise floor


@dataclass
class CalibrationSample:
    node_id:   str
    rssi:      float       # dBm
    true_dist: float       # metres — from ground-truth source
    ts:        float       # unix timestamp
    drone_lat: float = 0.0
    drone_lon: float = 0.0
    node_lat:  float = 0.0
    node_lon:  float = 0.0


@dataclass
class CalibrationResult:
    node_id:          str
    fitted_rssi_ref:  float          # dBm at 1m
    fitted_n:         float          # path-loss exponent
    r_squared:        float          # 0.0–1.0 (1.0 = perfect fit)
    rmse_m:           float          # root-mean-square distance error
    mae_m:            float          # mean absolute distance error
    sample_count:     int
    dist_range_m:     tuple[float, float]   # (min, max) of true distances used
    rssi_range_dbm:   tuple[float, float]
    residuals_m:      list[float]    # per-sample (predicted_dist - true_dist)
    percentile_68_m:  float          # 68th percentile absolute error (~1 sigma)
    percentile_95_m:  float          # 95th percentile
    warnings:         list[str] = field(default_factory=list)

    @property
    def quality(self) -> str:
        """Human-readable quality label."""
        if self.r_squared >= 0.85 and self.rmse_m < 80:  return "excellent"
        if self.r_squared >= 0.70 and self.rmse_m < 150: return "good"
        if self.r_squared >= 0.55 and self.rmse_m < 250: return "acceptable"
        return "poor"

    def to_config_dict(self) -> dict:
        return {
            "rssi_ref_dbm":  round(self.fitted_rssi_ref, 2),
            "path_loss_exp": round(self.fitted_n, 3),
            "calibrated":    True,
            "quality":       self.quality,
            "r_squared":     round(self.r_squared, 4),
            "rmse_m":        round(self.rmse_m, 1),
            "sample_count":  self.sample_count,
        }


class CalibrationEngine:
    """
    Fits RSSI path-loss model parameters per node using OLS regression.
    """

    def fit(
        self,
        samples: list[CalibrationSample],
        global_fallback: bool = True,
    ) -> dict[str, CalibrationResult]:
        """
        Fit parameters for each node independently.

        Returns: dict mapping node_id → CalibrationResult

        If global_fallback=True and a node has too few samples, it gets a
        result fitted from all samples pooled together.
        """
        # Group by node
        by_node: dict[str, list[CalibrationSample]] = {}
        for s in samples:
            by_node.setdefault(s.node_id, []).append(s)

        results: dict[str, CalibrationResult] = {}

        # Fit global model for fallback
        global_result = None
        if global_fallback and len(samples) >= MIN_SAMPLES_PER_NODE:
            global_result = self._fit_node("__global__", samples)

        for node_id, node_samples in by_node.items():
            filtered = self._filter_samples(node_samples)

            if len(filtered) < MIN_SAMPLES_PER_NODE:
                if global_result:
                    log.warning(
                        f"{node_id}: only {len(filtered)} usable samples "
                        f"(need {MIN_SAMPLES_PER_NODE}), using global fit"
                    )
                    r = global_result
                    r = CalibrationResult(
                        **{**r.__dict__,
                           "node_id": node_id,
                           "warnings": r.warnings + [
                               f"Fewer than {MIN_SAMPLES_PER_NODE} samples — "
                               f"global model used (n={global_result.fitted_n:.2f})"
                           ]}
                    )
                    results[node_id] = r
                else:
                    log.warning(f"{node_id}: skipped — too few samples ({len(filtered)})")
                continue

            results[node_id] = self._fit_node(node_id, filtered)

        return results

    def _filter_samples(self, samples: list[CalibrationSample]) -> list[CalibrationSample]:
        """Remove out-of-range or implausible readings."""
        return [
            s for s in samples
            if MIN_DIST_M <= s.true_dist <= MAX_DIST_M
            and s.rssi >= MIN_RSSI_DBM
        ]

    def _fit_node(
        self,
        node_id: str,
        samples: list[CalibrationSample],
    ) -> CalibrationResult:
        """
        OLS fit:  RSSI = a + b * log10(d)
        Returns fitted a (RSSI_REF) and b (-10*n).
        """
        rssi_vals  = np.array([s.rssi      for s in samples], dtype=float)
        dist_vals  = np.array([s.true_dist for s in samples], dtype=float)
        log10_dist = np.log10(dist_vals)

        # OLS via np.polyfit (linear regression in log-distance space)
        # RSSI = a + b * log10(d)  →  b = slope, a = intercept
        b, a = np.polyfit(log10_dist, rssi_vals, deg=1)

        fitted_rssi_ref = float(a)
        fitted_n        = float(-b / 10.0)

        # ── Predictions and residuals ─────────────────────────────────────
        rssi_pred   = a + b * log10_dist
        rssi_resid  = rssi_vals - rssi_pred

        # Convert RSSI residuals to distance residuals at each true distance
        # d_pred = 10 ^ ((a - RSSI_measured) / (-b))
        with np.errstate(divide='ignore', invalid='ignore'):
            d_pred = np.power(10.0, (a - rssi_vals) / (-b))
        dist_residuals_m = d_pred - dist_vals

        # ── Goodness of fit (R² on RSSI) ──────────────────────────────────
        ss_res = float(np.sum(rssi_resid ** 2))
        ss_tot = float(np.sum((rssi_vals - rssi_vals.mean()) ** 2))
        r_squared = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

        # ── Distance error metrics ─────────────────────────────────────────
        abs_dist_err = np.abs(dist_residuals_m)
        rmse_m = float(np.sqrt(np.mean(dist_residuals_m ** 2)))
        mae_m  = float(np.mean(abs_dist_err))
        p68    = float(np.percentile(abs_dist_err, 68))
        p95    = float(np.percentile(abs_dist_err, 95))

        # ── Warnings ──────────────────────────────────────────────────────
        warnings = []

        if not (N_MIN <= fitted_n <= N_MAX):
            warnings.append(
                f"Fitted n={fitted_n:.2f} is outside normal range "
                f"[{N_MIN}, {N_MAX}] — check for environmental anomalies"
            )
        if not (RSSI_REF_MIN <= fitted_rssi_ref <= RSSI_REF_MAX):
            warnings.append(
                f"Fitted RSSI_REF={fitted_rssi_ref:.1f} dBm is unusual — "
                f"verify hardware/antenna"
            )
        if r_squared < 0.55:
            warnings.append(
                f"R²={r_squared:.2f} is low — environment may violate log-distance "
                f"model (heavy multipath, obstacles in path)"
            )
        if rmse_m > 250:
            warnings.append(
                f"RMSE={rmse_m:.0f}m is high — calibration data quality is poor"
            )

        dist_range  = (float(dist_vals.min()), float(dist_vals.max()))
        rssi_range  = (float(rssi_vals.min()), float(rssi_vals.max()))
        dist_spread = dist_range[1] - dist_range[0]
        if dist_spread < 100:
            warnings.append(
                f"Distance range is only {dist_spread:.0f}m — "
                f"fly over a wider range (50–600m) for better fit"
            )

        log.info(
            f"{node_id}: n={fitted_n:.3f} RSSI_REF={fitted_rssi_ref:.1f}dBm "
            f"R²={r_squared:.3f} RMSE={rmse_m:.0f}m ({len(samples)} samples)"
        )

        return CalibrationResult(
            node_id          = node_id,
            fitted_rssi_ref  = fitted_rssi_ref,
            fitted_n         = fitted_n,
            r_squared        = r_squared,
            rmse_m           = rmse_m,
            mae_m            = mae_m,
            sample_count     = len(samples),
            dist_range_m     = dist_range,
            rssi_range_dbm   = rssi_range,
            residuals_m      = dist_residuals_m.tolist(),
            percentile_68_m  = p68,
            percentile_95_m  = p95,
            warnings         = warnings,
        )
