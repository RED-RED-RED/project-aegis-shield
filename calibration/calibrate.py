#!/usr/bin/env python3
"""
calibration/calibrate.py
========================
CLI entry point for the AEGIS calibration utility.

Workflow
--------
  1. Fly a cooperative drone in a known pattern while your nodes are active
  2. Export GPS track (GPX / SRT / CSV) from your controller app
  3. Run this script pointing at your server and the GPS file

Usage examples
--------------

  # Most common: GPX track from DJI Fly app, auto-detect flight window
  python calibrate.py \\
    --server http://192.168.1.100:8000 \\
    --drone-id FA3B920ETEST0001 \\
    --gpx flight.gpx

  # DJI SRT file (from video metadata)
  python calibrate.py \\
    --server http://192.168.1.100:8000 \\
    --drone-id FA3B920ETEST0001 \\
    --srt DJI_0042.SRT

  # Use drone's own RID broadcast as truth (cooperative calibration only)
  python calibrate.py \\
    --server http://192.168.1.100:8000 \\
    --drone-id FA3B920ETEST0001 \\
    --truth-source rid_broadcast \\
    --start "2024-03-15T10:15:00" \\
    --end   "2024-03-15T10:45:00"

  # Dry-run: show results but don't write any files
  python calibrate.py --server http://... --drone-id ... --gpx flight.gpx --dry-run

  # Generate diagnostic plots too
  python calibrate.py --server http://... --drone-id ... --gpx flight.gpx --plots

Output
------
  calibration.yaml  — per-node fitted parameters (written to server/analysis/)
  cal_report.txt    — human-readable report
  cal_*.png         — diagnostic plots (if --plots)
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running from any directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from calibration.collector import (
    parse_gpx, parse_srt, parse_csv_track,
    fetch_detections_from_api, fetch_nodes_from_api,
    build_samples,
)
from calibration.engine import CalibrationEngine
from calibration.config_writer import (
    write_calibration_yaml, patch_trilateration_py,
    generate_patch_snippet,
)
from calibration.analysis import text_report, generate_plots

# ── Logging ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("calibrate")

# ── ANSI colours ─────────────────────────────────────────────────────────

def c(text, code): return f"\033[{code}m{text}\033[0m"
GREEN  = lambda t: c(t, 32)
RED    = lambda t: c(t, 31)
YELLOW = lambda t: c(t, 33)
BOLD   = lambda t: c(t, 1)
DIM    = lambda t: c(t, 2)


def main():
    parser = argparse.ArgumentParser(
        description="AEGIS path-loss calibration utility",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # ── Source ────────────────────────────────────────────────────────────
    parser.add_argument("--server",    required=True, help="Server base URL, e.g. http://192.168.1.100:8000")
    parser.add_argument("--drone-id",  required=True, help="Drone ID as it appears in RID broadcasts")

    # GPS truth source (mutually exclusive)
    truth_group = parser.add_mutually_exclusive_group()
    truth_group.add_argument("--gpx",          help="GPX track file from controller app")
    truth_group.add_argument("--srt",          help="DJI SRT subtitle file (video GPS metadata)")
    truth_group.add_argument("--csv",          help="CSV track file (columns: timestamp,lat,lon,alt_m)")
    truth_group.add_argument("--truth-source", choices=["rid_broadcast"],
                              help="Use drone's own RID broadcast as truth (cooperative only)")

    # Time window (auto-detected from GPS file if not specified)
    parser.add_argument("--start", help="Start time ISO-8601 (auto-detected from GPS file if omitted)")
    parser.add_argument("--end",   help="End time ISO-8601   (auto-detected from GPS file if omitted)")
    parser.add_argument("--pad-s", type=float, default=30.0,
                        help="Seconds of padding around GPS track window (default: 30)")

    # Output
    parser.add_argument("--output",   help="Path for calibration.yaml (default: server/analysis/calibration.yaml)")
    parser.add_argument("--notes",    default="", help="Free-text notes for the calibration run")
    parser.add_argument("--dry-run",  action="store_true", help="Show results but don't write any files")
    parser.add_argument("--plots",    action="store_true", help="Generate matplotlib diagnostic plots")
    parser.add_argument("--patch-py", action="store_true",
                        help="Also patch RSSI_REF_DBM and PATH_LOSS_EXP in trilateration.py "
                             "(uses global weighted-average values)")
    parser.add_argument("--node",     action="append", dest="only_nodes",
                        help="Restrict to specific node(s) (repeatable)")

    args = parser.parse_args()

    print(f"\n{BOLD('AEGIS Calibration Utility')}")
    print(f"Server:   {args.server}")
    print(f"Drone ID: {args.drone_id}")

    # ── Load ground-truth track ───────────────────────────────────────────
    truth_track = []
    truth_source = "rid_broadcast"

    if args.gpx:
        print(f"GPX:      {args.gpx}")
        truth_track = parse_gpx(args.gpx)
        truth_source = "track"
    elif args.srt:
        print(f"SRT:      {args.srt}")
        truth_track = parse_srt(args.srt)
        truth_source = "track"
    elif args.csv:
        print(f"CSV:      {args.csv}")
        truth_track = parse_csv_track(args.csv)
        truth_source = "track"
    elif args.truth_source == "rid_broadcast":
        print(f"{YELLOW('Truth source: RID broadcast  (cooperative calibration only!)')}")
        truth_source = "rid_broadcast"
    else:
        print(RED("Error: provide --gpx, --srt, --csv, or --truth-source rid_broadcast"))
        sys.exit(1)

    # ── Determine time window ─────────────────────────────────────────────
    if args.start and args.end:
        start_ts = datetime.fromisoformat(args.start.replace("Z","+00:00")).timestamp()
        end_ts   = datetime.fromisoformat(args.end.replace("Z","+00:00")).timestamp()
    elif truth_track:
        start_ts = truth_track[0].ts  - args.pad_s
        end_ts   = truth_track[-1].ts + args.pad_s
        print(f"Window:   {datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime('%H:%M:%S')} "
              f"→ {datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime('%H:%M:%S')} UTC "
              f"({(end_ts-start_ts)/60:.1f} min)")
    else:
        print(RED("Error: --start and --end required when using --truth-source rid_broadcast"))
        sys.exit(1)

    duration_min = (end_ts - start_ts) / 60
    if duration_min < 3:
        print(YELLOW(f"Warning: window is only {duration_min:.1f} min — "
                     f"recommend at least 5–10 min for good sample density"))
    elif duration_min > 90:
        print(YELLOW(f"Warning: window is {duration_min:.1f} min — "
                     f"RSSI drift may affect accuracy over long sessions"))

    # ── Fetch data from server ────────────────────────────────────────────
    print(f"\n{DIM('Fetching data from server...')}")

    try:
        nodes = fetch_nodes_from_api(args.server)
    except Exception as e:
        print(RED(f"Error: could not reach server — {e}"))
        sys.exit(1)

    if args.only_nodes:
        nodes = [n for n in nodes if n["node_id"] in args.only_nodes]
        print(f"Filtering to nodes: {[n['node_id'] for n in nodes]}")

    online = [n for n in nodes if n.get("status") == "online"]
    print(f"Nodes:    {len(online)} online / {len(nodes)} total")
    for n in nodes:
        st = GREEN("●") if n.get("status") == "online" else RED("○")
        gps = f"  GPS: {n.get('lat','?'):.4f},{n.get('lon','?'):.4f}" if n.get("lat") else "  GPS: ?"
        print(f"  {st} {n['node_id']}{gps}")

    try:
        detections = fetch_detections_from_api(
            args.server, args.drone_id, start_ts, end_ts
        )
    except Exception as e:
        print(RED(f"Error: failed to fetch detections — {e}"))
        sys.exit(1)

    if not detections:
        print(RED(f"\nNo detections found for drone '{args.drone_id}' in window."))
        print(DIM("  Check drone ID spelling and time window."))
        sys.exit(1)

    print(f"Fetched:  {len(detections)} detection records")

    # ── Build calibration samples ─────────────────────────────────────────
    print(f"\n{DIM('Building calibration samples...')}")
    samples = build_samples(
        detections   = detections,
        truth_track  = truth_track,
        nodes        = nodes,
        truth_source = truth_source,
    )

    if not samples:
        print(RED("No usable samples built. Check node GPS positions are set."))
        sys.exit(1)

    by_node = {}
    for s in samples:
        by_node.setdefault(s.node_id, 0)
        by_node[s.node_id] += 1

    print(f"Samples:  {len(samples)} total")
    for nid, cnt in sorted(by_node.items()):
        print(f"  {nid}: {cnt} samples")

    # ── Fit model ─────────────────────────────────────────────────────────
    print(f"\n{DIM('Fitting path-loss model per node...')}")
    engine  = CalibrationEngine()
    results = engine.fit(samples, global_fallback=True)

    # ── Print results ──────────────────────────────────────────────────────
    report = text_report(results, samples)
    print("\n" + report)

    # Actionable recommendation
    print(f"\n{BOLD('Recommendations:')}")
    for node_id, r in sorted(results.items()):
        q_col = GREEN if r.quality in ("excellent","good") else (YELLOW if r.quality=="acceptable" else RED)
        print(f"  {q_col(node_id)} [{q_col(r.quality)}]  "
              f"n={r.fitted_n:.3f}  RSSI_ref={r.fitted_rssi_ref:.1f}dBm  "
              f"→ spoof threshold ≥ {int(r.percentile_95_m * 1.5)}m recommended")

    snippet = generate_patch_snippet(results)
    print(f"\n{DIM(snippet)}")

    if args.dry_run:
        print(f"\n{YELLOW('Dry-run mode — no files written.')}")
        return

    # ── Write calibration YAML ────────────────────────────────────────────
    out_path = Path(args.output) if args.output else None
    yaml_path = write_calibration_yaml(results, out_path, notes=args.notes)
    print(f"\n{GREEN('✓')} Calibration YAML written: {yaml_path}")

    # ── Optionally patch trilateration.py ─────────────────────────────────
    if args.patch_py:
        # Use the result with the most samples and best R² for the global patch
        best = max(results.values(), key=lambda r: r.sample_count * r.r_squared)
        try:
            diff = patch_trilateration_py(best)
            print(f"{GREEN('✓')} trilateration.py patched:\n{diff}")
        except Exception as e:
            print(f"{RED('✗')} Could not patch trilateration.py: {e}")
            print(DIM("  Use the YAML file instead (it takes precedence at runtime)."))

    # ── Write text report ─────────────────────────────────────────────────
    report_path = (out_path or yaml_path).parent / "cal_report.txt"
    report_path.write_text(report)
    print(f"{GREEN('✓')} Text report written: {report_path}")

    # ── Generate plots ────────────────────────────────────────────────────
    if args.plots:
        print(f"\n{DIM('Generating diagnostic plots...')}")
        plot_dir = (out_path or yaml_path).parent / "cal_plots"
        saved    = generate_plots(results, samples, out_dir=plot_dir)
        if saved:
            print(f"{GREEN('✓')} {len(saved)} plots saved to {plot_dir}/")
            for p in saved:
                print(f"    {p.name}")
        else:
            print(YELLOW("  Install matplotlib to generate plots: pip install matplotlib"))

    print(f"\n{GREEN('✓')} Calibration complete.\n")
    print(DIM("  Restart the server to load the new calibration:"))
    print(DIM("  cd server/docker && docker compose restart aegis-server\n"))


if __name__ == "__main__":
    main()
