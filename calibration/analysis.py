"""
calibration/analysis.py
=======================
Post-fit analysis and diagnostic plots.

Generates:
  1. Scatter plot   — measured RSSI vs log10(true_dist), fitted line overlay
  2. Residual plot  — distance error distribution per node
  3. Summary table  — all nodes side-by-side
  4. Environment    — node position map with coverage circles

Uses matplotlib (optional). If not installed, outputs a plain-text report.
"""

import logging
import math
import os
from pathlib import Path
from typing import Optional

from calibration.engine import CalibrationResult, CalibrationSample

log = logging.getLogger("calibration.analysis")


# ── Plain-text report (always available, no matplotlib needed) ────────────

def text_report(
    results: dict[str, CalibrationResult],
    samples: list[CalibrationSample],
) -> str:
    lines = []
    w = 64

    lines += [
        "═" * w,
        "  AEGIS CALIBRATION REPORT".center(w),
        "═" * w,
        "",
    ]

    # Per-node results
    for node_id, r in sorted(results.items()):
        q_icon = "✓" if r.quality in ("excellent", "good") else ("~" if r.quality == "acceptable" else "✗")
        lines += [
            f"  {q_icon} {node_id}  [{r.quality.upper()}]",
            f"  {'─'*56}",
            f"  Path-loss exponent (n)   : {r.fitted_n:.4f}",
            f"  RSSI at 1m (RSSI_REF)    : {r.fitted_rssi_ref:.2f} dBm",
            f"  Goodness of fit (R²)     : {r.r_squared:.4f}",
            f"  Dist RMSE                : {r.rmse_m:.1f} m",
            f"  Dist MAE                 : {r.mae_m:.1f} m",
            f"  68th percentile error    : {r.percentile_68_m:.1f} m  (~1σ)",
            f"  95th percentile error    : {r.percentile_95_m:.1f} m  (~2σ)",
            f"  Sample count             : {r.sample_count}",
            f"  Distance range           : {r.dist_range_m[0]:.0f} – {r.dist_range_m[1]:.0f} m",
            f"  RSSI range               : {r.rssi_range_dbm[0]:.0f} – {r.rssi_range_dbm[1]:.0f} dBm",
        ]
        if r.warnings:
            lines.append(f"  {'─'*56}")
            for w_msg in r.warnings:
                lines.append(f"  ⚠  {w_msg}")
        lines.append("")

    # Distance error histogram (text-based)
    lines += ["  Distance Error Distribution (all nodes)", "  " + "─"*56]
    all_res = []
    for r in results.values():
        all_res.extend(abs(x) for x in r.residuals_m)

    if all_res:
        buckets = [0, 50, 100, 150, 200, 300, 500, float("inf")]
        labels  = ["0–50m", "50–100m", "100–150m", "150–200m",
                   "200–300m", "300–500m", ">500m"]
        for i, label in enumerate(labels):
            count = sum(1 for x in all_res if buckets[i] <= x < buckets[i+1])
            pct   = count / len(all_res) * 100
            bar   = "█" * int(pct / 2)
            lines.append(f"  {label:>10}  {bar:<30} {pct:5.1f}% ({count})")

    lines += ["", "═" * w]
    return "\n".join(lines)


# ── Matplotlib plots (optional) ───────────────────────────────────────────

def generate_plots(
    results:  dict[str, CalibrationResult],
    samples:  list[CalibrationSample],
    out_dir:  Path,
    show:     bool = False,
) -> list[Path]:
    """
    Generate diagnostic plots. Returns list of saved file paths.
    Silently skips if matplotlib is not installed.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")   # Non-interactive backend for headless use
        import matplotlib.pyplot as plt
        import matplotlib.gridspec as gridspec
        import numpy as np
    except ImportError:
        log.warning("matplotlib not installed — skipping plot generation. "
                    "Install with: pip install matplotlib")
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []

    # Group samples by node
    by_node: dict[str, list[CalibrationSample]] = {}
    for s in samples:
        by_node.setdefault(s.node_id, []).append(s)

    node_ids = sorted(results.keys())
    n_nodes  = len(node_ids)
    if n_nodes == 0:
        return []

    # ── Plot 1: RSSI vs log10(distance) scatter + fitted lines ────────────
    fig, axes = plt.subplots(
        1, n_nodes,
        figsize=(5 * n_nodes, 4.5),
        squeeze=False,
    )
    fig.patch.set_facecolor("#0d1318")

    for col, node_id in enumerate(node_ids):
        ax = axes[0][col]
        ax.set_facecolor("#0d1318")

        r = results[node_id]
        node_samples = by_node.get(node_id, [])

        if not node_samples:
            ax.text(0.5, 0.5, "No samples", transform=ax.transAxes,
                    color="#3a5570", ha="center", va="center")
            ax.set_title(node_id, color="#e8a020")
            continue

        dists = np.array([s.true_dist for s in node_samples])
        rssis = np.array([s.rssi      for s in node_samples])
        log10_d = np.log10(dists)

        # Color by residual magnitude
        abs_res = np.abs(np.array(r.residuals_m)) if len(r.residuals_m) == len(dists) else np.zeros(len(dists))
        sc = ax.scatter(
            log10_d, rssis, c=abs_res, cmap="RdYlGn_r",
            vmin=0, vmax=200, alpha=0.6, s=12, zorder=3
        )
        plt.colorbar(sc, ax=ax, label="Dist error (m)").ax.yaxis.label.set_color("#7ecfea")

        # Fitted line
        d_range = np.linspace(max(dists.min(), 5), dists.max(), 200)
        rssi_fit = r.fitted_rssi_ref - 10 * r.fitted_n * np.log10(d_range)
        ax.plot(np.log10(d_range), rssi_fit, color="#e8a020", lw=2, zorder=5,
                label=f"n={r.fitted_n:.3f}")

        # 68% / 95% envelope (approximate)
        # sigma_rssi ≈ 10*n * sigma_dist / (d * ln10)  — not analytically clean,
        # use empirical RSSI residual std dev
        rssi_pred = r.fitted_rssi_ref - 10 * r.fitted_n * np.log10(d_range)
        rssi_std  = float(np.std(rssis - (r.fitted_rssi_ref - 10*r.fitted_n*log10_d)))
        ax.fill_between(np.log10(d_range),
                        rssi_fit - rssi_std, rssi_fit + rssi_std,
                        color="#e8a020", alpha=0.1, label="±1σ")

        # Decoration
        ax.set_xlabel("log₁₀(distance / m)", color="#7ecfea")
        ax.set_ylabel("RSSI (dBm)", color="#7ecfea")
        ax.set_title(
            f"{node_id}  —  {r.quality.upper()}\n"
            f"n={r.fitted_n:.3f}  R²={r.r_squared:.3f}  RMSE={r.rmse_m:.0f}m",
            color="#e8a020", fontsize=9
        )
        ax.tick_params(colors="#3a5570")
        ax.spines[:].set_color("#1e3040")
        ax.legend(fontsize=7, labelcolor="#c8dce8",
                  facecolor="#111c24", edgecolor="#1e3040")
        ax.grid(True, color="#111c24", lw=0.5)

    fig.suptitle("RSSI vs Distance — Calibration Fit", color="#c8dce8", fontsize=11)
    fig.tight_layout()

    path1 = out_dir / "cal_rssi_scatter.png"
    fig.savefig(path1, dpi=150, bbox_inches="tight", facecolor="#0d1318")
    plt.close(fig)
    saved.append(path1)
    log.info(f"Saved: {path1}")

    # ── Plot 2: Residual distributions ────────────────────────────────────
    fig2, ax2 = plt.subplots(figsize=(8, 4))
    fig2.patch.set_facecolor("#0d1318")
    ax2.set_facecolor("#0d1318")

    colors = ["#e8a020", "#39ff8a", "#7ecfea", "#ff3f5a", "#b39ddb"]
    for i, (node_id, r) in enumerate(sorted(results.items())):
        if not r.residuals_m:
            continue
        color = colors[i % len(colors)]
        ax2.hist(r.residuals_m, bins=40, alpha=0.6, color=color,
                 label=f"{node_id} (σ={r.percentile_68_m:.0f}m)", density=True)

    ax2.axvline(0, color="#c8dce8", lw=1.5, ls="--", alpha=0.7, label="Zero error")
    ax2.set_xlabel("Distance residual (m)  [predicted − true]", color="#7ecfea")
    ax2.set_ylabel("Density", color="#7ecfea")
    ax2.set_title("Distance Error Distribution per Node", color="#c8dce8")
    ax2.tick_params(colors="#3a5570")
    ax2.spines[:].set_color("#1e3040")
    ax2.legend(fontsize=8, labelcolor="#c8dce8",
               facecolor="#111c24", edgecolor="#1e3040")
    ax2.grid(True, color="#111c24", lw=0.5, axis="x")

    path2 = out_dir / "cal_residuals.png"
    fig2.tight_layout()
    fig2.savefig(path2, dpi=150, bbox_inches="tight", facecolor="#0d1318")
    plt.close(fig2)
    saved.append(path2)
    log.info(f"Saved: {path2}")

    # ── Plot 3: Range-binned accuracy ─────────────────────────────────────
    # Shows how accuracy degrades with distance — useful for setting
    # spoof-detection thresholds at different engagement ranges
    fig3, ax3 = plt.subplots(figsize=(8, 4))
    fig3.patch.set_facecolor("#0d1318")
    ax3.set_facecolor("#0d1318")

    all_dists, all_abs_res = [], []
    for r in results.values():
        node_samples = by_node.get(r.node_id, [])
        for j, s in enumerate(node_samples):
            if j < len(r.residuals_m):
                all_dists.append(s.true_dist)
                all_abs_res.append(abs(r.residuals_m[j]))

    if all_dists:
        import numpy as np
        bins = np.arange(0, max(all_dists) + 100, 100)
        bin_centers, medians, p68s, p95s = [], [], [], []

        for lo, hi in zip(bins[:-1], bins[1:]):
            mask = [(lo <= d < hi) for d in all_dists]
            vals = [v for v, m in zip(all_abs_res, mask) if m]
            if len(vals) < 5:
                continue
            bin_centers.append((lo + hi) / 2)
            medians.append(float(np.median(vals)))
            p68s.append(float(np.percentile(vals, 68)))
            p95s.append(float(np.percentile(vals, 95)))

        if bin_centers:
            ax3.fill_between(bin_centers, 0, p95s, alpha=0.15, color="#e8a020", label="95th pct")
            ax3.fill_between(bin_centers, 0, p68s, alpha=0.25, color="#39ff8a", label="68th pct (1σ)")
            ax3.plot(bin_centers, medians, color="#7ecfea", lw=2, label="Median")
            ax3.axhline(250, color="#ff3f5a", lw=1, ls="--", alpha=0.7, label="Spoof threshold (250m)")

    ax3.set_xlabel("True distance from node (m)", color="#7ecfea")
    ax3.set_ylabel("Absolute distance error (m)", color="#7ecfea")
    ax3.set_title("Error vs. Range — Informs Spoof Detection Threshold", color="#c8dce8")
    ax3.tick_params(colors="#3a5570")
    ax3.spines[:].set_color("#1e3040")
    ax3.legend(fontsize=8, labelcolor="#c8dce8",
               facecolor="#111c24", edgecolor="#1e3040")
    ax3.grid(True, color="#111c24", lw=0.5)

    path3 = out_dir / "cal_range_accuracy.png"
    fig3.tight_layout()
    fig3.savefig(path3, dpi=150, bbox_inches="tight", facecolor="#0d1318")
    plt.close(fig3)
    saved.append(path3)
    log.info(f"Saved: {path3}")

    if show:
        os.system(f"open {out_dir}" if os.name == "posix" else f"explorer {out_dir}")

    return saved
