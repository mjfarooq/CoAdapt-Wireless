#!/usr/bin/env python3
"""
Analyze 192-run PRB + edge CPU + resolution + speed co-design results.

This script DOES NOT modify or clean the original CSV files.
It reads each run's factory_latency.csv, filters out replay/no-fresh-image
logging rows internally, and generates corrected summary tables and plots.

Main rule:
- Remove replay/no-fresh-image/capture-artifact rows.
- Keep real inference rows, including wrong, late, unknown, no-detection, and model-miss rows.
- Use all remaining real attempted parts as the denominator.
- Do NOT remove the first row.

Expected grid:
    UL PRB:      75, 50, 25, 15
    DL PRB:      50
    Edge CPU:    8, 4, 2, 1
    Resolution:  1280x720, 640x360, 320x180
    Speed:       0.5, 1.0, 1.5, 2.0

Outputs:
    summary_all_detected_runs.csv
    summary_current_grid.csv
    grid_completion_check.csv
    top/best configuration CSVs
    PRB/CPU/resolution/speed effect plots
    Pareto plots
    joint-vs-independent tuning plots
    dynamic replay plot
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, LinearSegmentedColormap, Normalize
from matplotlib.colors import ListedColormap
from matplotlib.lines import Line2D
from matplotlib.patches import Patch


# =============================================================================
# Defaults
# =============================================================================

DEFAULT_RESULTS_ROOT = Path("/home/elahe/user/Co_design_codes/automated_experiments/results")
DEFAULT_OUT_DIR = Path("/home/elahe/user/Co_design_codes/automated_experiments/plots_grid_192_analysis_cleaned_162")

GRID_UL_LEVELS = [75, 50, 25, 15]
GRID_DL_LEVELS = [50]
GRID_CPU_LEVELS = [8.0, 4.0, 2.0, 1.0]
GRID_RESOLUTIONS = ["1280x720", "640x360", "320x180"]
GRID_SPEEDS = [0.5, 1.0, 1.5, 2.0]
DEFAULT_OPTIMUM_SHIFT_SPEED_PAIRS = [
    (0.5, 1.0),
    (1.0, 1.5),
    (1.5, 2.0),
    (1.0, 2.0),
]

UL_MAX = 75.0
CPU_MAX = 8.0
SPEED_MAX = 2.0
PART_SPACING_M = 1.0
SECONDS_PER_MINUTE = 60.0
PRODUCTION_GOODPUT_UNIT = "correct_parts_per_min"

# Preserve the previous utility trade-off after switching the resource cost
# from normalized (PRB/75)*(CPU/8) to raw PRB_pct*CPU. The raw product is 600x
# larger than the old normalized product over this grid.
LAMBDA_RESOURCE = 24.0 / (UL_MAX * CPU_MAX)
RESOURCE_COST_MODEL = "raw_prb_pct_cpu_product"

MAX_LATE_RATE = 0.10
MAX_UNKNOWN_RATE = 0.10


def resource_cost_from_raw_knobs(prb_cost, cpu_cost):
    """Return joint raw PRB/CPU resource cost.

    The inputs are the original knob values:
    - ``prb_cost``: UL PRB allocation percentage, e.g. 15, 25, 50, 75.
    - ``cpu_cost``: edge CPU quota, e.g. 1, 2, 4, 8.
    """
    return prb_cost * cpu_cost

# Common reference used for fair single-factor policy comparisons. Each
# single-factor policy may change exactly one field from this configuration.
PAPER_REFERENCE_CONFIG = {
    "slice_ul_pct": 75.0,
    "edge_cpus": 8.0,
    "resolution": "1280x720",
}

PAPER_POLICY_LABELS = {
    "fixed": "Fixed reference",
    "slice_only": "Slice only",
    "cpu_only": "CPU only",
    "resolution_only": "Resolution only",
    "joint": "Joint co-design",
}

CONTROLLABLE_POLICY_LABELS = {
    "fixed": "Fixed reference",
    "speed_only": "Speed only",
    "slice_only": "Slice only",
    "cpu_only": "CPU only",
    "resolution_only": "Resolution only",
    "joint": "Joint co-design",
}

DEFAULT_PAPER_GOODPUT_FRACTION = 0.90


# =============================================================================
# General helpers
# =============================================================================

def safe_float(x: Any, default: float = np.nan) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


def safe_int(x: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        if x is None or x == "":
            return default
        return int(float(x))
    except Exception:
        return default


def read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        print(f"[WARN] Could not parse JSON {path}: {exc}")
        return {}


def boolish_series(s: pd.Series) -> pd.Series:
    if s.dtype == bool:
        return s.fillna(False)

    numeric = pd.to_numeric(s, errors="coerce")
    x = s.astype(str).str.strip().str.lower()
    return (numeric == 1) | x.isin([
        "1", "true", "yes", "y", "t",
        "late", "unknown", "timeout", "no_ack", "no_result",
    ])


def falseish_series(s: pd.Series) -> pd.Series:
    if s.dtype == bool:
        return ~s.fillna(False)

    numeric = pd.to_numeric(s, errors="coerce")
    x = s.astype(str).str.strip().str.lower()
    return (numeric == 0) | x.isin(["0", "false", "no", "n", "f"])


def find_col(df: pd.DataFrame, candidates: Sequence[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def sanitize_float_for_name(x: Any) -> str:
    return str(x).replace(".", "p").replace("-", "m")


def ensure_out_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def format_cfg(row: pd.Series) -> str:
    return (
        f"UL{int(row['slice_ul_pct'])}_CPU{row['edge_cpus']:g}_"
        f"{row['resolution']}_v{row['run_speed']:g}"
    )


# =============================================================================
# Config extraction
# =============================================================================

def parse_from_run_name(run_name: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}

    m = re.search(r"slice_ul(\d+)_dl(\d+)", run_name)
    if m:
        out["slice_ul_pct"] = float(m.group(1))
        out["slice_dl_pct"] = float(m.group(2))
        out["slice_label"] = f"slice_ul{m.group(1)}_dl{m.group(2)}"

    m = re.search(r"edge_cpu([0-9p]+)", run_name)
    if m:
        out["edge_label"] = "edge_cpu" + m.group(1)
        out["edge_cpus"] = float(m.group(1).replace("p", "."))

    m = re.search(r"_(\d+)x(\d+)_", run_name)
    if m:
        out["camera_width"] = int(m.group(1))
        out["camera_height"] = int(m.group(2))

    m = re.search(r"speed([0-9p]+)", run_name)
    if m:
        out["run_speed"] = float(m.group(1).replace("p", "."))

    return out


def extract_config(run_dir: Path) -> Dict[str, Any]:
    cfg = read_json(run_dir / "run_config.json")
    fallback = parse_from_run_name(run_dir.name)

    def get(key: str, default: Any = "") -> Any:
        val = cfg.get(key, None)
        if val is None or val == "":
            val = fallback.get(key, default)
        return val

    slice_ul = safe_float(get("slice_ul_pct", cfg.get("industrial_ul_pct", np.nan)))
    slice_dl = safe_float(get("slice_dl_pct", cfg.get("industrial_dl_pct", np.nan)))
    edge_cpus = safe_float(get("edge_cpus", np.nan))

    w = safe_int(get("camera_width", np.nan))
    h = safe_int(get("camera_height", np.nan))
    resolution = f"{w}x{h}" if w is not None and h is not None else ""

    speed = safe_float(get("run_speed", np.nan))

    return {
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "slice_label": get("slice_label", fallback.get("slice_label", "")),
        "slice_ul_pct": slice_ul,
        "slice_dl_pct": slice_dl,
        "edge_label": get("edge_label", fallback.get("edge_label", "")),
        "edge_cpus": edge_cpus,
        "edge_compute_capacity": safe_float(get("edge_compute_capacity", np.nan)),
        "camera_width": w,
        "camera_height": h,
        "resolution": resolution,
        "run_speed": speed,
        "model_path": get("model_path", ""),
        "encoding": get("encoding", ""),
        "background_iperf_mbit": safe_float(get("background_iperf_mbit", np.nan)),
        "background_iperf_direction": get("background_iperf_direction", ""),
        "background_tun_iface": get("background_tun_iface", ""),
        "status": cfg.get("status", ""),
        "exclude_from_analysis": cfg.get("exclude_from_analysis", False),
    }


# =============================================================================
# Row filtering: remove replay/no-fresh-image artifacts only
# =============================================================================

def remove_logging_artifacts(df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, int]]:
    """
    Remove rows that are not real product-evaluation cycles.

Removed:
      - replay_required_* rows
      - capture_no_fresh_image
      - status/failure_type containing no_fresh_image
      - partial-visibility replay rows
      - explicit replay/debug/non-product flags
      - rows with no latency AND no correctness caused by capture/replay artifact

    Kept:
      - ok
      - ok|late_for_sort_edge
      - wrong detections
      - late detections
      - unknown/model-miss/no-detection rows from a real inference
    """
    df = df.copy()
    original_n = len(df)
    remove_mask = pd.Series(False, index=df.index)

    if "status" in df.columns:
        status = df["status"].astype(str).str.lower().fillna("")
        remove_mask |= status.str.contains("replay_required", na=False)
        remove_mask |= status.str.contains("replay_required_no_fresh_image", na=False)
        remove_mask |= status.str.contains("capture_no_fresh_image", na=False)
        remove_mask |= status.str.contains("no_fresh_image", na=False)

    if "failure_type" in df.columns:
        failure = df["failure_type"].astype(str).str.lower().fillna("")
        remove_mask |= failure.str.contains("capture_no_fresh_image", na=False)
        remove_mask |= failure.str.contains("replay_required_no_fresh_image", na=False)
        remove_mask |= failure.str.contains("no_fresh_image", na=False)
        remove_mask |= failure.str.contains("partial_or_not_fully_visible", na=False)

    if "frame_pick_mode" in df.columns:
        fpm = df["frame_pick_mode"].astype(str).str.lower().fillna("")
        # Only remove timeout rows if they are also no-fresh-image/replay/capture artifacts.
        artifact_context = pd.Series(False, index=df.index)
        if "status" in df.columns:
            artifact_context |= df["status"].astype(str).str.lower().str.contains(
                "replay|required|no_fresh|capture", na=False
            )
        if "failure_type" in df.columns:
            artifact_context |= df["failure_type"].astype(str).str.lower().str.contains(
                "replay|required|no_fresh|capture", na=False
            )
        remove_mask |= fpm.str.contains("timeout", na=False) & artifact_context

    for c in ["is_replay", "replay", "replayed", "is_debug", "debug_row", "non_product_row"]:
        if c in df.columns:
            remove_mask |= boolish_series(df[c])

    # Extra conservative artifact removal:
    # If a row has no roundtrip latency and no correctness label and status/failure indicates capture issue,
    # remove it. Do not remove unknown/model-miss rows solely because correct is false.
    latency_col = find_col(df, [
        "factory_roundtrip_ms", "roundtrip_ms", "factory_total_ms",
        "capture_to_result_ms", "edge_total_ms",
    ])
    correct_col = find_col(df, ["correct", "is_correct", "prediction_correct", "classification_correct"])

    if latency_col is not None and correct_col is not None:
        lat_nan = pd.to_numeric(df[latency_col], errors="coerce").isna()
        corr_nan = df[correct_col].isna()
        artifact_text = pd.Series(False, index=df.index)
        for c in ["status", "failure_type"]:
            if c in df.columns:
                artifact_text |= df[c].astype(str).str.lower().str.contains(
                    "replay|no_fresh|capture_no_fresh|timeout", na=False
                )
        remove_mask |= lat_nan & corr_nan & artifact_text

    clean = df[~remove_mask].copy()
    stats = {
        "raw_rows": int(original_n),
        "removed_artifact_rows": int(remove_mask.sum()),
        "kept_attempted_rows": int(len(clean)),
    }
    return clean, stats


# =============================================================================
# Metric inference
# =============================================================================

def infer_correct(df: pd.DataFrame) -> pd.Series:
    col = find_col(df, ["correct", "is_correct", "prediction_correct", "classification_correct"])
    if col is not None:
        return boolish_series(df[col])

    pred_col = find_col(df, ["pred_label", "predicted_label", "prediction", "class_pred", "result_label"])
    true_col = find_col(df, ["true_label", "gt_label", "ground_truth", "class_true", "target_label"])
    if pred_col is not None and true_col is not None:
        return df[pred_col].astype(str).str.strip() == df[true_col].astype(str).str.strip()

    print("[WARN] No correctness column found; treating correct as False.")
    return pd.Series(False, index=df.index)


def infer_late(df: pd.DataFrame) -> pd.Series:
    col = find_col(df, ["late_for_sort_edge", "late_for_sort", "late", "is_late"])
    if col is not None:
        return boolish_series(df[col])

    slack_col = find_col(df, ["deadline_slack_ms", "slack_ms", "sort_deadline_slack_ms"])
    if slack_col is not None:
        slack = pd.to_numeric(df[slack_col], errors="coerce")
        return (slack < 0).fillna(False)

    print("[WARN] No late/deadline column found; treating late as False.")
    return pd.Series(False, index=df.index)


def infer_unknown(df: pd.DataFrame) -> pd.Series:
    col = find_col(df, ["unknown", "is_unknown", "result_unknown", "no_ack", "no_result", "timeout"])
    if col is not None:
        return boolish_series(df[col])

    # If failure_type/status indicates a real no-detection/model miss, count as unknown/failure.
    unknown = pd.Series(False, index=df.index)
    for c in ["failure_type", "status"]:
        if c in df.columns:
            txt = df[c].astype(str).str.lower()
            unknown |= txt.str.contains("unknown|no_result|no_ack|model_miss|no_detection|timeout", na=False)

    pred_col = find_col(df, ["pred_label", "predicted_label", "prediction", "result", "edge_result", "result_label"])
    if pred_col is not None:
        x = df[pred_col].astype(str).str.strip().str.lower()
        unknown |= x.isin(["", "nan", "none", "unknown", "no_ack", "no_result", "timeout", "model_miss"])

    return unknown


def numeric_col(df: pd.DataFrame, candidates: Sequence[str]) -> Tuple[Optional[str], pd.Series]:
    col = find_col(df, candidates)
    if col is None:
        return None, pd.Series(dtype=float)
    return col, pd.to_numeric(df[col], errors="coerce")


def mean_numeric(df: pd.DataFrame, candidates: Sequence[str]) -> float:
    _, s = numeric_col(df, candidates)
    s = s.dropna()
    return float(s.mean()) if len(s) else np.nan


def q_numeric(df: pd.DataFrame, candidates: Sequence[str], quantile: float) -> float:
    _, s = numeric_col(df, candidates)
    s = s.dropna()
    return float(s.quantile(quantile)) if len(s) else np.nan


# =============================================================================
# Per-run summary
# =============================================================================

def summarize_background_iperf(run_dir: Path) -> Dict[str, Any]:
    candidates = [
        run_dir / "background_iperf_client_stdout.json",
        run_dir / "background_iperf_client.json",
    ]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        return {
            "bg_throughput_mbps": np.nan,
            "bg_lost_percent": np.nan,
            "bg_jitter_ms": np.nan,
        }

    try:
        data = json.loads(path.read_text())
    except Exception:
        return {
            "bg_throughput_mbps": np.nan,
            "bg_lost_percent": np.nan,
            "bg_jitter_ms": np.nan,
        }

    end = data.get("end", {})
    sum_obj = end.get("sum", {}) or end.get("sum_sent", {}) or end.get("sum_received", {})

    bps = safe_float(sum_obj.get("bits_per_second", np.nan))
    return {
        "bg_throughput_mbps": bps / 1e6 if not math.isnan(bps) else np.nan,
        "bg_lost_percent": safe_float(sum_obj.get("lost_percent", np.nan)),
        "bg_jitter_ms": safe_float(sum_obj.get("jitter_ms", np.nan)),
    }


def summarize_run(run_dir: Path) -> Optional[Dict[str, Any]]:
    factory_csv = run_dir / "factory_latency.csv"
    if not factory_csv.exists():
        factory_csv = run_dir / "factory.csv"
    if not factory_csv.exists():
        return None

    cfg = extract_config(run_dir)
    if str(cfg.get("exclude_from_analysis", "")).lower() == "true":
        return None

    try:
        df_raw = pd.read_csv(factory_csv)
    except Exception as exc:
        print(f"[WARN] Could not read {factory_csv}: {exc}")
        return None

    if df_raw.empty:
        return None

    df, filter_stats = remove_logging_artifacts(df_raw)
    if df.empty:
        print(f"[WARN] No attempted rows left after artifact filtering: {run_dir.name}")
        return {
            **cfg,
            **filter_stats,
            "attempted_parts": 0,
            "correct_parts": 0,
            "late_parts": 0,
            "unknown_parts": 0,
            "correct_on_time_parts": 0,
            "correct_rate": np.nan,
            "late_rate": np.nan,
            "unknown_rate": np.nan,
            "correct_on_time_rate": np.nan,
            "production_goodput": np.nan,
            "production_efficiency": np.nan,
            "resource_cost": np.nan,
            "feasible": False,
        }

    attempted_parts = int(len(df))

    correct = infer_correct(df)
    late = infer_late(df)
    unknown = infer_unknown(df)

    correct_parts = int((correct == True).sum())
    late_parts = int((late == True).sum())
    unknown_parts = int((unknown == True).sum())
    correct_on_time_parts = int(((correct == True) & (late == False) & (unknown == False)).sum())

    correct_rate = correct_parts / attempted_parts
    late_rate = late_parts / attempted_parts
    unknown_rate = unknown_parts / attempted_parts
    correct_on_time_rate = correct_on_time_parts / attempted_parts

    latency_candidates = [
        "factory_roundtrip_ms",
        "roundtrip_ms",
        "factory_total_ms",
        "capture_to_result_ms",
        "edge_total_ms",
    ]
    latency_col, _ = numeric_col(df, latency_candidates)

    slice_ul = cfg["slice_ul_pct"]
    edge_cpu = cfg["edge_cpus"]
    speed = cfg["run_speed"]

    prb_cost = slice_ul if not math.isnan(slice_ul) else np.nan
    cpu_cost = edge_cpu if not math.isnan(edge_cpu) else np.nan
    resource_cost = resource_cost_from_raw_knobs(prb_cost, cpu_cost)

    normalized_speed = speed / SPEED_MAX if not math.isnan(speed) else np.nan
    production_rate_parts_per_min = (
        SECONDS_PER_MINUTE * speed / PART_SPACING_M
        if not math.isnan(speed)
        else np.nan
    )
    production_goodput = production_rate_parts_per_min * correct_on_time_rate

    correct_per_resource = correct_rate / resource_cost if resource_cost and resource_cost > 0 else np.nan
    cot_per_resource = correct_on_time_rate / resource_cost if resource_cost and resource_cost > 0 else np.nan
    production_efficiency = production_goodput / resource_cost if resource_cost and resource_cost > 0 else np.nan
    utility = production_goodput - LAMBDA_RESOURCE * resource_cost

    feasible = (
        late_rate <= MAX_LATE_RATE
        and unknown_rate <= MAX_UNKNOWN_RATE
        and attempted_parts > 0
    )

    bg_metrics = summarize_background_iperf(run_dir)

    return {
        **cfg,
        **filter_stats,
        "attempted_parts": attempted_parts,
        "correct_parts": correct_parts,
        "late_parts": late_parts,
        "unknown_parts": unknown_parts,
        "correct_on_time_parts": correct_on_time_parts,

        "correct_rate": correct_rate,
        "late_rate": late_rate,
        "unknown_rate": unknown_rate,
        "correct_on_time_rate": correct_on_time_rate,

        "latency_col": latency_col or "",
        "mean_roundtrip_ms": mean_numeric(df, latency_candidates),
        "p50_roundtrip_ms": q_numeric(df, latency_candidates, 0.50),
        "p90_roundtrip_ms": q_numeric(df, latency_candidates, 0.90),
        "p95_roundtrip_ms": q_numeric(df, latency_candidates, 0.95),
        "p99_roundtrip_ms": q_numeric(df, latency_candidates, 0.99),

        "mean_inference_ms": mean_numeric(df, ["inference_ms"]),
        "p95_inference_ms": q_numeric(df, ["inference_ms"], 0.95),
        "mean_edge_process_ms": mean_numeric(df, ["edge_process_ms"]),
        "p95_edge_process_ms": q_numeric(df, ["edge_process_ms"], 0.95),
        "mean_edge_total_ms": mean_numeric(df, ["edge_total_ms"]),
        "p95_edge_total_ms": q_numeric(df, ["edge_total_ms"], 0.95),

        "prb_cost": prb_cost,
        "cpu_cost": cpu_cost,
        "resource_cost_model": RESOURCE_COST_MODEL,
        "resource_cost": resource_cost,
        "normalized_speed": normalized_speed,
        "part_spacing_m": PART_SPACING_M,
        "production_rate_parts_per_min": production_rate_parts_per_min,
        "production_goodput_unit": PRODUCTION_GOODPUT_UNIT,
        "production_goodput": production_goodput,
        "correct_per_resource": correct_per_resource,
        "cot_per_resource": cot_per_resource,
        "production_efficiency": production_efficiency,
        "utility": utility,
        "feasible": feasible,

        **bg_metrics,
    }


# =============================================================================
# Loading/filtering
# =============================================================================

def load_all_summaries(results_root: Path) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    run_dirs = sorted([p for p in results_root.glob("run_*") if p.is_dir()])

    for run_dir in run_dirs:
        row = summarize_run(run_dir)
        if row is not None:
            rows.append(row)

    if not rows:
        raise RuntimeError(f"No valid run summaries found under {results_root}")

    return pd.DataFrame(rows)


def filter_to_current_grid(df: pd.DataFrame, strict: bool = True) -> pd.DataFrame:
    if not strict:
        return df.copy()

    g = df.copy()
    g = g[g["slice_ul_pct"].isin(GRID_UL_LEVELS)]
    g = g[g["slice_dl_pct"].isin(GRID_DL_LEVELS)]
    g = g[g["edge_cpus"].isin(GRID_CPU_LEVELS)]
    g = g[g["resolution"].isin(GRID_RESOLUTIONS)]
    g = g[g["run_speed"].isin(GRID_SPEEDS)]
    return g


# =============================================================================
# Tables
# =============================================================================

def save_top_tables(df: pd.DataFrame, out_dir: Path) -> None:
    top_cols = [
        "production_efficiency",
        "cot_per_resource",
        "correct_per_resource",
        "production_goodput",
        "correct_on_time_rate",
        "correct_rate",
        "late_rate",
        "unknown_rate",
        "attempted_parts",
        "removed_artifact_rows",
        "resource_cost",
        "prb_cost",
        "cpu_cost",
        "slice_ul_pct",
        "slice_dl_pct",
        "edge_cpus",
        "resolution",
        "run_speed",
        "p95_roundtrip_ms",
        "p95_edge_process_ms",
        "p95_inference_ms",
        "bg_throughput_mbps",
        "bg_lost_percent",
        "run_id",
    ]
    top_cols = [c for c in top_cols if c in df.columns]

    df.sort_values("production_efficiency", ascending=False)[top_cols].to_csv(
        out_dir / "top_by_production_efficiency.csv", index=False
    )
    df.sort_values("cot_per_resource", ascending=False)[top_cols].to_csv(
        out_dir / "top_by_correct_on_time_per_resource.csv", index=False
    )
    df.sort_values("production_goodput", ascending=False)[top_cols].to_csv(
        out_dir / "top_by_production_goodput.csv", index=False
    )
    df.sort_values("correct_on_time_rate", ascending=False)[top_cols].to_csv(
        out_dir / "top_by_correct_on_time_rate.csv", index=False
    )


def best_by_group(df: pd.DataFrame, group_cols: Sequence[str], metric: str = "production_efficiency") -> pd.DataFrame:
    if not group_cols:
        return df.sort_values(metric, ascending=False).head(1)
    return (
        df.sort_values(metric, ascending=False)
        .groupby(list(group_cols), as_index=False, dropna=False)
        .first()
    )


def save_controller_tables(df: pd.DataFrame, out_dir: Path) -> None:
    network = best_by_group(df, ["edge_cpus", "resolution", "run_speed"], "production_efficiency")
    network.to_csv(out_dir / "best_network_only_by_context.csv", index=False)

    compute = best_by_group(df, ["slice_ul_pct", "resolution", "run_speed"], "production_efficiency")
    compute.to_csv(out_dir / "best_compute_only_by_context.csv", index=False)

    resolution = best_by_group(df, ["slice_ul_pct", "edge_cpus", "run_speed"], "production_efficiency")
    resolution.to_csv(out_dir / "best_resolution_only_by_context.csv", index=False)

    speed = best_by_group(df, ["slice_ul_pct", "edge_cpus", "resolution"], "production_efficiency")
    speed.to_csv(out_dir / "best_speed_only_by_context.csv", index=False)

    industry = best_by_group(df, ["slice_ul_pct", "edge_cpus"], "production_efficiency")
    industry.to_csv(out_dir / "best_industry_only_by_context.csv", index=False)

    joint_top = df.sort_values("production_efficiency", ascending=False).head(50)
    joint_top.to_csv(out_dir / "best_joint_grid_search_top50.csv", index=False)

    feasible = df[df["feasible"] == True]
    if len(feasible):
        feasible.sort_values("production_efficiency", ascending=False).head(50).to_csv(
            out_dir / "best_feasible_joint_grid_search_top50.csv", index=False
        )


# =============================================================================
# Pareto and plotting
# =============================================================================

def pareto_front(df: pd.DataFrame, x_col: str, y_col: str) -> pd.DataFrame:
    d = df[[x_col, y_col]].dropna()
    idxs = d.index.tolist()

    keep = []
    for i in idxs:
        xi, yi = df.loc[i, x_col], df.loc[i, y_col]
        dominated = False
        for j in idxs:
            if i == j:
                continue
            xj, yj = df.loc[j, x_col], df.loc[j, y_col]
            if (xj <= xi) and (yj >= yi) and ((xj < xi) or (yj > yi)):
                dominated = True
                break
        if not dominated:
            keep.append(i)

    return df.loc[keep].sort_values(x_col)


def set_plot_defaults() -> None:
    plt.rcParams.update({
        "font.size": 11,
        "axes.labelsize": 12,
        "axes.titlesize": 13,
        "legend.fontsize": 9,
        "xtick.labelsize": 10,
        "ytick.labelsize": 10,
        "figure.dpi": 110,
        "savefig.dpi": 300,
    })


def savefig(out_dir: Path, name: str) -> None:
    plt.tight_layout()
    plt.savefig(out_dir / name, bbox_inches="tight")
    plt.close()


def apply_metric_axis(ax: plt.Axes, metric: str) -> None:
    if metric.endswith("_rate"):
        ax.set_ylim(0.0, 1.0)


def add_point_count_labels(ax: plt.Axes, x: Iterable[Any], y: Iterable[Any], labels: Iterable[Any]) -> None:
    for xi, yi, label in zip(x, y, labels):
        if pd.isna(yi):
            continue
        ax.annotate(
            str(label),
            (xi, yi),
            textcoords="offset points",
            xytext=(0, 6),
            ha="center",
            fontsize=8,
            alpha=0.75,
        )


def plot_top_bar(df: pd.DataFrame, out_dir: Path, metric: str, filename: str, top_n: int = 20) -> None:
    g = df.sort_values(metric, ascending=False).head(top_n).copy()
    if g.empty:
        return

    labels = [
        f"UL{int(r.slice_ul_pct)} CPU{r.edge_cpus:g}\n{r.resolution} v{r.run_speed:g}"
        for _, r in g.iterrows()
    ]

    plt.figure(figsize=(15, 6))
    plt.bar(range(len(g)), g[metric])
    plt.xticks(range(len(g)), labels, rotation=70, ha="right")
    plt.ylabel(metric.replace("_", " "))
    plt.title(f"Top {top_n} configurations by {metric.replace('_', ' ')}")
    plt.grid(axis="y", alpha=0.25)
    savefig(out_dir, filename)


def plot_pareto(df: pd.DataFrame, out_dir: Path) -> None:
    g = df.dropna(subset=["resource_cost", "production_goodput"]).copy()
    if g.empty:
        return

    pf = pareto_front(g, "resource_cost", "production_goodput")
    pf.to_csv(out_dir / "pareto_front_resource_vs_goodput.csv", index=False)

    plt.figure(figsize=(8, 6))
    for speed, h in g.groupby("run_speed"):
        plt.scatter(h["resource_cost"], h["production_goodput"], alpha=0.7, label=f"v={speed:g}")

    if len(pf):
        plt.plot(pf["resource_cost"], pf["production_goodput"], marker="o", linewidth=2, label="Pareto front")

    plt.xlabel("Raw PRB% × CPU resource cost")
    plt.ylabel("Production goodput (correct parts/min)")
    plt.title("Resource cost vs production goodput")
    plt.grid(True, alpha=0.3)
    plt.legend()
    savefig(out_dir, "pareto_resource_cost_vs_production_goodput.png")


def plot_pareto_cot(df: pd.DataFrame, out_dir: Path) -> None:
    g = df.dropna(subset=["resource_cost", "correct_on_time_rate"]).copy()
    if g.empty:
        return

    pf = pareto_front(g, "resource_cost", "correct_on_time_rate")
    pf.to_csv(out_dir / "pareto_front_resource_vs_cot.csv", index=False)

    plt.figure(figsize=(8, 6))
    for res, h in g.groupby("resolution"):
        plt.scatter(h["resource_cost"], h["correct_on_time_rate"], alpha=0.7, label=res)

    if len(pf):
        plt.plot(pf["resource_cost"], pf["correct_on_time_rate"], marker="o", linewidth=2, label="Pareto front")

    plt.xlabel("Raw PRB% × CPU resource cost")
    plt.ylabel("Correct-on-time rate")
    plt.title("Resource cost vs correct-on-time rate")
    plt.grid(True, alpha=0.3)
    plt.legend()
    savefig(out_dir, "pareto_resource_cost_vs_correct_on_time_rate.png")


def select_policy_configuration(
    speed_df: pd.DataFrame,
    policy: str,
    metric: str,
    reference: Dict[str, Any],
) -> Optional[pd.Series]:
    """Select the measured configuration allowed by a policy.

    Speed is treated as an observed operating condition, not a tunable resource.
    The single-factor policies change exactly one design variable from the same
    reference configuration. Joint co-design may change all three variables.
    """
    tunable_by_policy = {
        "fixed": set(),
        "slice_only": {"slice_ul_pct"},
        "cpu_only": {"edge_cpus"},
        "resolution_only": {"resolution"},
        "joint": set(reference),
    }
    if policy not in tunable_by_policy:
        raise ValueError(f"Unknown policy: {policy}")

    allowed = speed_df.copy()
    tunable = tunable_by_policy[policy]
    for field, value in reference.items():
        if field not in tunable:
            allowed = allowed[allowed[field] == value]
    return select_best(allowed, metric)


def build_paper_policy_comparison(
    df: pd.DataFrame,
    metric: str,
    reference: Dict[str, Any] = PAPER_REFERENCE_CONFIG,
) -> pd.DataFrame:
    """Return policy selections and outcomes for each measured speed."""
    policies = list(PAPER_POLICY_LABELS)
    rows: List[Dict[str, Any]] = []

    for speed in sorted(df["run_speed"].dropna().unique()):
        speed_df = df[df["run_speed"] == speed]
        selected: Dict[str, pd.Series] = {}
        for policy in policies:
            row = select_policy_configuration(speed_df, policy, metric, reference)
            if row is not None:
                selected[policy] = row

        if "joint" not in selected:
            continue
        best_single_value = max(
            float(selected[p][metric])
            for p in ("slice_only", "cpu_only", "resolution_only")
            if p in selected
        )

        for policy, row in selected.items():
            value = float(row[metric])
            rows.append({
                "selection_metric": metric,
                "run_speed": float(speed),
                "policy": policy,
                "policy_label": PAPER_POLICY_LABELS[policy],
                "metric_value": value,
                "slice_ul_pct": float(row["slice_ul_pct"]),
                "edge_cpus": float(row["edge_cpus"]),
                "resolution": str(row["resolution"]),
                "configuration": format_cfg(row),
                "attempted_parts": int(row["attempted_parts"]),
                "correct_on_time_rate": float(row["correct_on_time_rate"]),
                "production_goodput": float(row["production_goodput"]),
                "production_efficiency": float(row["production_efficiency"]),
                "resource_cost": float(row["resource_cost"]),
                "best_single_factor_value": best_single_value,
                "joint_gain_over_best_single_abs": (
                    float(selected["joint"][metric]) - best_single_value
                    if policy == "joint" else np.nan
                ),
                "joint_gain_over_best_single_pct": (
                    100.0 * (float(selected["joint"][metric]) / best_single_value - 1.0)
                    if policy == "joint" and best_single_value > 0 else np.nan
                ),
                "run_id": row["run_id"],
            })

    return pd.DataFrame(rows)


def save_paper_policy_tables(df: pd.DataFrame, out_dir: Path) -> Dict[str, pd.DataFrame]:
    """Write detailed and manuscript-ready policy comparison tables."""
    comparisons: Dict[str, pd.DataFrame] = {}
    for metric in ("production_efficiency", "production_goodput"):
        comparison = build_paper_policy_comparison(df, metric)
        comparisons[metric] = comparison
        comparison.to_csv(out_dir / f"paper_policy_comparison_{metric}.csv", index=False)

        values = comparison.pivot(
            index="run_speed", columns="policy_label", values="metric_value"
        ).reset_index()
        joint_rows = comparison[comparison["policy"] == "joint"][
            [
                "run_speed", "configuration", "joint_gain_over_best_single_abs",
                "joint_gain_over_best_single_pct",
            ]
        ].rename(columns={"configuration": "joint_configuration"})
        manuscript = values.merge(joint_rows, on="run_speed", how="left")
        manuscript.to_csv(out_dir / f"paper_summary_table_{metric}.csv", index=False)

    return comparisons


def build_constrained_efficiency_comparison(
    df: pd.DataFrame,
    min_goodput_fraction: float,
    reference: Dict[str, Any] = PAPER_REFERENCE_CONFIG,
) -> pd.DataFrame:
    """Maximize efficiency subject to a speed-specific goodput requirement."""
    if not 0 < min_goodput_fraction <= 1:
        raise ValueError("min_goodput_fraction must be in (0, 1]")

    rows: List[Dict[str, Any]] = []
    for speed in sorted(df["run_speed"].dropna().unique()):
        speed_df = df[df["run_speed"] == speed]
        max_goodput = float(speed_df["production_goodput"].max())
        required_goodput = min_goodput_fraction * max_goodput
        eligible = speed_df[
            speed_df["production_goodput"] >= required_goodput - 1e-12
        ]

        selected: Dict[str, pd.Series] = {}
        for policy in PAPER_POLICY_LABELS:
            row = select_policy_configuration(
                eligible, policy, "production_efficiency", reference
            )
            if row is not None:
                selected[policy] = row

        single_values = [
            float(selected[p]["production_efficiency"])
            for p in ("slice_only", "cpu_only", "resolution_only")
            if p in selected
        ]
        best_single = max(single_values) if single_values else np.nan
        joint_value = (
            float(selected["joint"]["production_efficiency"])
            if "joint" in selected else np.nan
        )

        # Include unavailable policies explicitly so missing bars/cells cannot be
        # mistaken for measured zero performance.
        for policy, label in PAPER_POLICY_LABELS.items():
            row = selected.get(policy)
            rows.append({
                "run_speed": float(speed),
                "policy": policy,
                "policy_label": label,
                "min_goodput_fraction": min_goodput_fraction,
                "max_measured_goodput": max_goodput,
                "required_goodput": required_goodput,
                "eligible": row is not None,
                "production_efficiency": (
                    float(row["production_efficiency"]) if row is not None else np.nan
                ),
                "production_goodput": (
                    float(row["production_goodput"]) if row is not None else np.nan
                ),
                "resource_cost": float(row["resource_cost"]) if row is not None else np.nan,
                "correct_on_time_rate": (
                    float(row["correct_on_time_rate"]) if row is not None else np.nan
                ),
                "configuration": format_cfg(row) if row is not None else "Not feasible",
                "best_single_factor_efficiency": best_single,
                "joint_gain_over_best_single_pct": (
                    100.0 * (joint_value / best_single - 1.0)
                    if policy == "joint" and best_single > 0 else np.nan
                ),
                "run_id": row["run_id"] if row is not None else "",
            })
    return pd.DataFrame(rows)


def save_and_plot_constrained_efficiency(
    df: pd.DataFrame,
    out_dir: Path,
    min_goodput_fraction: float,
) -> pd.DataFrame:
    comparison = build_constrained_efficiency_comparison(
        df, min_goodput_fraction
    )
    comparison.to_csv(
        out_dir / "paper_policy_comparison_constrained_efficiency.csv", index=False
    )

    values = comparison.pivot(
        index="run_speed", columns="policy_label", values="production_efficiency"
    ).reset_index()
    joint = comparison[comparison["policy"] == "joint"][
        [
            "run_speed", "required_goodput", "configuration",
            "joint_gain_over_best_single_pct",
        ]
    ].rename(columns={"configuration": "joint_configuration"})
    values.merge(joint, on="run_speed", how="left").to_csv(
        out_dir / "paper_summary_table_constrained_efficiency.csv", index=False
    )

    plot_input = comparison.rename(columns={
        "production_efficiency": "metric_value",
    })
    plot_paper_policy_bars(
        plot_input,
        out_dir,
        "constrained_production_efficiency",
    )
    return comparison


def build_cot_constrained_efficiency_comparison(
    df: pd.DataFrame,
    min_cot_rate: float,
    reference: Dict[str, Any] = PAPER_REFERENCE_CONFIG,
) -> pd.DataFrame:
    """Maximize efficiency subject to a minimum correct-on-time rate."""
    if not 0 <= min_cot_rate <= 1:
        raise ValueError("min_cot_rate must be in [0, 1]")

    rows: List[Dict[str, Any]] = []
    for speed in sorted(df["run_speed"].dropna().unique()):
        speed_df = df[df["run_speed"] == speed]
        eligible = speed_df[
            speed_df["correct_on_time_rate"] >= min_cot_rate - 1e-12
        ]

        selected: Dict[str, pd.Series] = {}
        for policy in PAPER_POLICY_LABELS:
            row = select_policy_configuration(
                eligible, policy, "production_efficiency", reference
            )
            if row is not None:
                selected[policy] = row

        single_values = [
            float(selected[p]["production_efficiency"])
            for p in ("slice_only", "cpu_only", "resolution_only")
            if p in selected
        ]
        best_single = max(single_values) if single_values else np.nan
        joint_value = (
            float(selected["joint"]["production_efficiency"])
            if "joint" in selected else np.nan
        )

        for policy, label in PAPER_POLICY_LABELS.items():
            row = selected.get(policy)
            rows.append({
                "run_speed": float(speed),
                "policy": policy,
                "policy_label": label,
                "min_cot_rate": min_cot_rate,
                "eligible": row is not None,
                "production_efficiency": (
                    float(row["production_efficiency"]) if row is not None else np.nan
                ),
                "production_goodput": (
                    float(row["production_goodput"]) if row is not None else np.nan
                ),
                "resource_cost": float(row["resource_cost"]) if row is not None else np.nan,
                "correct_on_time_rate": (
                    float(row["correct_on_time_rate"]) if row is not None else np.nan
                ),
                "configuration": format_cfg(row) if row is not None else "Not feasible",
                "best_single_factor_efficiency": best_single,
                "joint_gain_over_best_single_pct": (
                    100.0 * (joint_value / best_single - 1.0)
                    if policy == "joint" and best_single > 0 else np.nan
                ),
                "run_id": row["run_id"] if row is not None else "",
            })
    return pd.DataFrame(rows)


def save_and_plot_cot_constrained_efficiency(
    df: pd.DataFrame,
    out_dir: Path,
    min_cot_rate: float,
) -> pd.DataFrame:
    """Save and plot policy comparison under a minimum COT-rate constraint."""
    suffix = f"cot_ge_{str(min_cot_rate).replace('.', 'p')}"
    comparison = build_cot_constrained_efficiency_comparison(df, min_cot_rate)
    comparison.to_csv(
        out_dir / f"paper_policy_comparison_efficiency_{suffix}.csv",
        index=False,
    )

    values = comparison.pivot(
        index="run_speed", columns="policy_label", values="production_efficiency"
    ).reset_index()
    joint = comparison[comparison["policy"] == "joint"][
        [
            "run_speed", "min_cot_rate", "configuration",
            "joint_gain_over_best_single_pct",
        ]
    ].rename(columns={"configuration": "joint_configuration"})
    values.merge(joint, on="run_speed", how="left").to_csv(
        out_dir / f"paper_summary_table_efficiency_{suffix}.csv", index=False
    )

    plot_input = comparison.rename(columns={
        "production_efficiency": "metric_value",
    })
    plot_paper_policy_bars(
        plot_input,
        out_dir,
        f"cot_constrained_production_efficiency_{suffix}",
    )
    return comparison


def build_controllable_speed_comparison(
    df: pd.DataFrame,
    min_goodput_fraction: float,
) -> pd.DataFrame:
    """Compare isolated and joint policies when conveyor speed is controllable."""
    if not 0 < min_goodput_fraction <= 1:
        raise ValueError("min_goodput_fraction must be in (0, 1]")

    reference = {
        "run_speed": float(df["run_speed"].max()),
        **PAPER_REFERENCE_CONFIG,
    }
    tunable_by_policy = {
        "fixed": set(),
        "speed_only": {"run_speed"},
        "slice_only": {"slice_ul_pct"},
        "cpu_only": {"edge_cpus"},
        "resolution_only": {"resolution"},
        "joint": set(reference),
    }

    max_goodput = float(df["production_goodput"].max())
    required_goodput = min_goodput_fraction * max_goodput
    eligible_grid = df[df["production_goodput"] >= required_goodput - 1e-12]
    selected: Dict[str, pd.Series] = {}

    for policy, tunable in tunable_by_policy.items():
        allowed = eligible_grid.copy()
        for field, value in reference.items():
            if field not in tunable:
                allowed = allowed[allowed[field] == value]
        row = select_best(allowed, "production_efficiency")
        if row is not None:
            selected[policy] = row

    single_values = [
        float(selected[p]["production_efficiency"])
        for p in ("speed_only", "slice_only", "cpu_only", "resolution_only")
        if p in selected
    ]
    best_single = max(single_values) if single_values else np.nan
    joint_value = (
        float(selected["joint"]["production_efficiency"])
        if "joint" in selected else np.nan
    )

    rows: List[Dict[str, Any]] = []
    for policy, label in CONTROLLABLE_POLICY_LABELS.items():
        row = selected.get(policy)
        rows.append({
            "policy": policy,
            "policy_label": label,
            "min_goodput_fraction": min_goodput_fraction,
            "max_measured_goodput": max_goodput,
            "required_goodput": required_goodput,
            "eligible": row is not None,
            "production_efficiency": (
                float(row["production_efficiency"]) if row is not None else np.nan
            ),
            "production_goodput": (
                float(row["production_goodput"]) if row is not None else np.nan
            ),
            "resource_cost": float(row["resource_cost"]) if row is not None else np.nan,
            "correct_on_time_rate": (
                float(row["correct_on_time_rate"]) if row is not None else np.nan
            ),
            "run_speed": float(row["run_speed"]) if row is not None else np.nan,
            "slice_ul_pct": float(row["slice_ul_pct"]) if row is not None else np.nan,
            "edge_cpus": float(row["edge_cpus"]) if row is not None else np.nan,
            "resolution": str(row["resolution"]) if row is not None else "",
            "configuration": format_cfg(row) if row is not None else "Not feasible",
            "best_single_factor_efficiency": best_single,
            "joint_gain_over_best_single_pct": (
                100.0 * (joint_value / best_single - 1.0)
                if policy == "joint" and best_single > 0 else np.nan
            ),
            "run_id": row["run_id"] if row is not None else "",
        })
    return pd.DataFrame(rows)


def save_and_plot_controllable_speed_comparison(
    df: pd.DataFrame,
    out_dir: Path,
    min_goodput_fraction: float,
) -> pd.DataFrame:
    comparison = build_controllable_speed_comparison(
        df, min_goodput_fraction
    )
    comparison.to_csv(
        out_dir / "paper_policy_comparison_controllable_speed.csv", index=False
    )

    colors = {
        "fixed": "#7f7f7f",
        "speed_only": "#9467bd",
        "slice_only": "#4c78a8",
        "cpu_only": "#f58518",
        "resolution_only": "#54a24b",
        "joint": "#b22222",
    }
    fig, ax = plt.subplots(figsize=(9.5, 5.6))
    y = np.arange(len(comparison))
    values = comparison["production_efficiency"]
    bars = ax.barh(
        y, values.fillna(0),
        color=[colors[p] for p in comparison["policy"]], height=0.65,
    )
    ax.set_yticks(y, comparison["policy_label"])
    ax.invert_yaxis()
    for index, (bar, row) in enumerate(zip(bars, comparison.itertuples())):
        if not row.eligible:
            ax.text(
                0.01, index, "Does not meet goodput requirement",
                va="center", ha="left", fontsize=9, color="#555555",
            )
        else:
            ax.text(
                bar.get_width() + 0.01, index,
                f"{bar.get_width():.3f}  ({row.configuration})",
                va="center", ha="left", fontsize=9,
            )
    fraction = float(comparison["min_goodput_fraction"].iloc[0])
    ax.set_xlabel("Production efficiency")
    ax.set_title(
        f"Controllable-speed policies retaining at least {fraction:.0%} "
        "of maximum goodput"
    )
    ax.set_xlim(0, max(values.max() * 1.55, 0.1))
    ax.grid(axis="x", alpha=0.25)
    savefig(out_dir, "fig_paper_controllable_speed_codesign.png")
    return comparison


def plot_paper_policy_bars(
    comparison: pd.DataFrame,
    out_dir: Path,
    metric: str,
) -> None:
    if comparison.empty:
        return

    policy_order = list(PAPER_POLICY_LABELS)
    colors = {
        "fixed": "#7f7f7f",
        "slice_only": "#4c78a8",
        "cpu_only": "#f58518",
        "resolution_only": "#54a24b",
        "joint": "#b22222",
    }
    speeds = sorted(comparison["run_speed"].unique())
    x = np.arange(len(speeds), dtype=float)
    width = 0.16

    fig, ax = plt.subplots(figsize=(10.5, 5.8))
    for index, policy in enumerate(policy_order):
        policy_df = (
            comparison[comparison["policy"] == policy]
            .set_index("run_speed")
            .reindex(speeds)
        )
        offset = (index - (len(policy_order) - 1) / 2) * width
        ax.bar(
            x + offset,
            policy_df["metric_value"],
            width=width,
            color=colors[policy],
            label=PAPER_POLICY_LABELS[policy],
            edgecolor="white",
            linewidth=0.5,
        )

    ax.set_xticks(x, [f"{speed:g}" for speed in speeds])
    ax.set_xlabel("Conveyor speed (m/s)")
    if metric == "production_efficiency":
        ax.set_ylabel("Production efficiency")
        ax.set_title("Joint co-design improves resource efficiency")
    elif metric == "constrained_production_efficiency":
        fraction = float(comparison["min_goodput_fraction"].iloc[0])
        ax.set_ylabel("Production efficiency")
        ax.set_title(
            f"Efficiency while retaining at least {fraction:.0%} of maximum goodput"
        )
    elif metric.startswith("cot_constrained_production_efficiency"):
        min_cot = float(comparison["min_cot_rate"].iloc[0])
        ax.set_ylabel("Production efficiency")
        ax.set_title(
            f"Resource efficiency with correct-on-time rate ≥ {min_cot:.1f}"
        )
    else:
        ax.set_ylabel("Production goodput (correct parts/min)")
        ax.set_title("Maximum goodput: joint versus isolated optimization")
    ax.set_ylim(bottom=0)
    ax.grid(axis="y", alpha=0.25)
    ax.legend(ncol=3, frameon=False)
    savefig(out_dir, f"fig_paper_policy_comparison_{metric}.png")


def plot_paper_efficiency_gain(comparison: pd.DataFrame, out_dir: Path) -> None:
    joint = comparison[comparison["policy"] == "joint"].sort_values("run_speed")
    if joint.empty:
        return

    fig, ax = plt.subplots(figsize=(8.2, 5.2))
    bars = ax.bar(
        joint["run_speed"].astype(str),
        joint["joint_gain_over_best_single_pct"],
        color="#b22222",
        width=0.65,
    )
    ax.bar_label(bars, fmt="%.1f%%", padding=3, fontsize=10)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_xlabel("Conveyor speed (m/s)")
    ax.set_ylabel("Efficiency gain over best single-factor policy (%)")
    ax.set_title("Measured benefit of joint co-design")
    ax.grid(axis="y", alpha=0.25)
    upper = joint["joint_gain_over_best_single_pct"].max()
    ax.set_ylim(0, upper * 1.16 if upper > 0 else 1)
    savefig(out_dir, "fig_paper_codesign_efficiency_gain.png")


def plot_paper_pareto_by_speed(df: pd.DataFrame, out_dir: Path) -> None:
    """Plot comparable resource/goodput frontiers within each speed context."""
    speeds = sorted(df["run_speed"].dropna().unique())
    if not speeds:
        return

    ncols = 2
    nrows = math.ceil(len(speeds) / ncols)
    fig, axes = plt.subplots(
        nrows, ncols, figsize=(11, 4.5 * nrows), sharex=True, sharey=False,
        squeeze=False,
    )
    resolution_colors = {
        "320x180": "#4c78a8",
        "640x360": "#f58518",
        "1280x720": "#54a24b",
    }

    for ax, speed in zip(axes.flat, speeds):
        group = df[df["run_speed"] == speed].dropna(
            subset=["resource_cost", "production_goodput"]
        )
        for resolution, points in group.groupby("resolution"):
            ax.scatter(
                points["resource_cost"], points["production_goodput"],
                s=32, alpha=0.65, color=resolution_colors.get(resolution),
                label=resolution,
            )
        front = pareto_front(group, "resource_cost", "production_goodput")
        ax.plot(
            front["resource_cost"], front["production_goodput"],
            color="black", marker="o", markersize=4, linewidth=1.5,
            label="Pareto frontier",
        )
        efficient = select_best(group, "production_efficiency")
        if efficient is not None:
            ax.scatter(
                [efficient["resource_cost"]], [efficient["production_goodput"]],
                marker="*", s=220, color="#b22222", edgecolor="black",
                linewidth=0.7, zorder=5, label="Efficiency optimum",
            )
        ax.set_title(f"Speed = {speed:g} m/s")
        ax.grid(alpha=0.25)

    for ax in axes.flat[len(speeds):]:
        ax.set_visible(False)
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.suptitle("Measured production trade-offs within each workload", y=0.99)
    fig.supxlabel("Raw PRB% × CPU resource cost", y=0.015)
    fig.supylabel(
        "Production goodput (correct parts/min)", x=0.015
    )
    fig.legend(
        handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.955),
        ncol=5, frameon=False,
    )
    plt.tight_layout(rect=(0.035, 0.035, 1, 0.89))
    plt.savefig(
        out_dir / "fig_paper_pareto_resource_goodput_by_speed.png",
        bbox_inches="tight", dpi=300,
    )
    plt.close(fig)


def _surface_grid(
    group: pd.DataFrame,
    metric: str,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return a measured UL-by-CPU grid without interpolating missing cells."""
    table = group.pivot_table(
        index="edge_cpus", columns="slice_ul_pct", values=metric, aggfunc="mean"
    ).sort_index().sort_index(axis=1)
    x, y = np.meshgrid(
        table.columns.to_numpy(dtype=float), table.index.to_numpy(dtype=float)
    )
    return x, y, table.to_numpy(dtype=float)


def _efficiency_contour_levels(values: pd.Series) -> np.ndarray:
    """Return common, nonlinear levels that retain detail near low efficiency."""
    finite = values.replace([np.inf, -np.inf], np.nan).dropna().to_numpy(float)
    if finite.size == 0:
        return np.asarray([], dtype=float)
    vmax = float(np.max(finite))
    if vmax <= 0:
        return np.asarray([0.0, 1.0], dtype=float)
    # Product-normalized efficiency is right-skewed. Powers of two preserve
    # resolution near zero while keeping one interpretable scale in all panels.
    return np.asarray([0.0, *[vmax / (2.0**power) for power in range(6, -1, -1)]])


def _format_efficiency_contour_label(value: float) -> str:
    """Format efficiency contour labels without rounding sub-unit values to 0."""
    value = float(value)
    if abs(value) >= 10:
        return f"{value:.0f}"
    if abs(value) >= 1:
        return f"{value:.1f}"
    if abs(value) >= 0.1:
        return f"{value:.2f}"
    return f"{value:.3f}"


def _draw_efficiency_contour_panel(
    ax,
    group: pd.DataFrame,
    *,
    metric: str,
    levels: np.ndarray,
    norm: BoundaryNorm,
    label_contours: bool,
) -> None:
    """Draw one interpolated surface while distinguishing measured outcomes."""
    x, y, z = _surface_grid(group, metric)
    zmin, zmax = float(np.nanmin(z)), float(np.nanmax(z))
    cmap = plt.get_cmap("viridis")
    constant_surface = math.isclose(
        zmin, zmax, rel_tol=1e-12, abs_tol=1e-12
    )

    if constant_surface:
        ax.set_facecolor(cmap(norm(zmin)))
        ax.text(
            0.5,
            0.52,
            f"Constant measured surface\n$\\eta$ = {zmin:.1f}",
            transform=ax.transAxes,
            ha="center",
            va="center",
            fontsize=9,
            bbox={"facecolor": "white", "alpha": 0.82, "edgecolor": "none"},
        )
    else:
        ax.contourf(x, y, z, levels=levels, cmap=cmap, norm=norm, extend="max")
        line_levels = levels[(levels > zmin) & (levels < zmax)]
        if line_levels.size:
            lines = ax.contour(
                x,
                y,
                z,
                levels=line_levels,
                colors="black",
                linewidths=0.45,
                alpha=0.60,
            )
            if label_contours:
                ax.clabel(
                    lines,
                    inline=True,
                    fontsize=6.5,
                    fmt=_format_efficiency_contour_label,
                )

    ax.scatter(
        group["slice_ul_pct"],
        group["edge_cpus"],
        s=30,
        c=group[metric],
        cmap=cmap,
        norm=norm,
        marker="o",
        edgecolor="black",
        linewidth=0.55,
        zorder=4,
    )
    if not constant_surface:
        # Match the 3D surface convention: the star is the measured panel
        # maximum, independent of whether it satisfies the COT constraint.
        best_value = float(group[metric].max())
        best_rows = group[np.isclose(group[metric], best_value)]
        ax.scatter(
            best_rows["slice_ul_pct"],
            best_rows["edge_cpus"],
            marker="*",
            s=170,
            color="#d62728",
            edgecolor="black",
            linewidth=0.7,
            clip_on=False,
            zorder=6,
        )


def _contour_legend_handles() -> list[Line2D]:
    return [
        Line2D([], [], marker="o", linestyle="none", markerfacecolor="#777777", markeredgecolor="black", label="Measured configuration"),
        Line2D([], [], marker="*", markersize=13, linestyle="none", markerfacecolor="#d62728", markeredgecolor="black", label="Highest measured efficiency (unconstrained)"),
    ]




def plot_codesign_efficiency_contours(
    df: pd.DataFrame,
    out_dir: Path,
) -> None:
    """Plot interpolated PRB/CPU efficiency contours for each measured speed."""
    metric = "production_efficiency"

    finite = df[metric].replace([np.inf, -np.inf], np.nan).dropna()
    if finite.empty:
        return

    levels = _efficiency_contour_levels(finite)
    norm = BoundaryNorm(levels, ncolors=plt.get_cmap("viridis").N, clip=True)

    for speed in sorted(df["run_speed"].dropna().unique()):
        fig, axes = plt.subplots(
            1, len(GRID_RESOLUTIONS),
            figsize=(15.5, 4.6),
            sharey=True,
            constrained_layout=True,
        )

        for ax, resolution in zip(axes, GRID_RESOLUTIONS):
            group = df[
                (df["run_speed"] == speed)
                & (df["resolution"] == resolution)
            ].dropna(subset=[metric])

            if group.empty:
                ax.set_visible(False)
                continue

            _draw_efficiency_contour_panel(
                ax,
                group,
                metric=metric,
                levels=levels,
                norm=norm,
                label_contours=True,
            )

            ax.set_title(resolution)
            ax.set_xlabel("UL PRB (%)")
            ax.set_xticks(sorted(df["slice_ul_pct"].unique()))
            ax.set_yticks(sorted(df["edge_cpus"].unique()))
            ax.grid(alpha=0.25)

        axes[0].set_ylabel("Edge CPUs")

        colorbar = fig.colorbar(
            plt.cm.ScalarMappable(norm=norm, cmap="viridis"),
            ax=axes,
            ticks=levels,
            shrink=0.88,
        )
        colorbar.set_label(
            "Efficiency (correct parts/min per raw PRB%–CPU cost)"
        )
        fig.legend(
            handles=_contour_legend_handles(),
            loc="lower center",
            ncol=3,
            frameon=False,
            bbox_to_anchor=(0.5, -0.15),
        )

        fig.suptitle(
            f"Co-design efficiency landscape — speed {speed:g} m/s",
            fontsize=14,
            y=1.10,
        )

        fig.text(
            0.5,
            -0.02,
            "Contours are linearly interpolated for visualization only; red stars mark the highest measured efficiency without applying the COT constraint.",
            ha="center",
            fontsize=9,
            color="#444444",
        )

        plt.savefig(
            out_dir / f"fig_contour_codesign_efficiency_speed_{speed:g}.png",
            bbox_inches="tight",
            dpi=300,
        )
        plt.close(fig)
        


def plot_multispeed_efficiency_contour_lines(
    df: pd.DataFrame,
    out_dir: Path,
) -> None:
    """Plot a comparable speed-by-resolution contour matrix."""
    metric = "production_efficiency"

    finite = df[metric].replace([np.inf, -np.inf], np.nan).dropna()
    if finite.empty:
        return

    levels = _efficiency_contour_levels(finite)
    norm = BoundaryNorm(levels, ncolors=plt.get_cmap("viridis").N, clip=True)
    speeds = sorted(df["run_speed"].dropna().unique())

    fig, axes = plt.subplots(
        len(speeds),
        len(GRID_RESOLUTIONS),
        figsize=(13.5, 13.0),
        sharex=True,
        sharey=True,
        constrained_layout=True,
        squeeze=False,
    )

    for row_index, speed in enumerate(speeds):
        for column_index, resolution in enumerate(GRID_RESOLUTIONS):
            ax = axes[row_index, column_index]
            group = df[
                (df["resolution"] == resolution)
                & (df["run_speed"] == speed)
            ].dropna(subset=[metric])

            if group.empty:
                ax.set_visible(False)
                continue

            _draw_efficiency_contour_panel(
                ax,
                group,
                metric=metric,
                levels=levels,
                norm=norm,
                label_contours=False,
            )
            if row_index == 0:
                ax.set_title(resolution)
            if column_index == 0:
                ax.set_ylabel(f"Speed {speed:g} m/s\nEdge CPU quota")
            if row_index == len(speeds) - 1:
                ax.set_xlabel("UL PRB allocation (%)")
            ax.set_xticks(sorted(df["slice_ul_pct"].unique()))
            ax.set_yticks(sorted(df["edge_cpus"].unique()))
            ax.grid(alpha=0.20)

    fig.legend(
        handles=_contour_legend_handles(),
        loc="upper center",
        ncol=3,
        frameon=False,
        bbox_to_anchor=(0.5, 1.015),
    )
    colorbar = fig.colorbar(
        plt.cm.ScalarMappable(norm=norm, cmap="viridis"),
        ax=axes,
        ticks=levels,
        shrink=0.82,
    )
    colorbar.set_label(
        "Efficiency (correct parts/min per raw PRB%–CPU cost)"
    )

    fig.suptitle(
        "Co-design efficiency across speed and resolution",
        fontsize=14,
        y=1.055,
    )

    fig.text(
        0.5,
        -0.02,
        "Contours are linearly interpolated for visualization only; circles are measurements and red stars are measured panel maxima.",
        ha="center",
        fontsize=9,
        color="#444444",
    )

    plt.savefig(
        out_dir / "fig_multispeed_efficiency_contour_lines.png",
        bbox_inches="tight",
        dpi=300,
    )
    plt.close(fig)


def _deduplicate_speed_pairs(
    speed_pairs: Iterable[Tuple[float, float]],
) -> List[Tuple[float, float]]:
    """Return speed pairs in order, removing duplicates after float coercion."""
    deduplicated: List[Tuple[float, float]] = []
    seen: set[Tuple[float, float]] = set()
    for speed_from, speed_to in speed_pairs:
        pair = (float(speed_from), float(speed_to))
        if math.isclose(pair[0], pair[1]):
            continue
        if pair not in seen:
            deduplicated.append(pair)
            seen.add(pair)
    return deduplicated


def _speed_slug(speed: float) -> str:
    """Format a speed value for stable figure filenames."""
    return f"{float(speed):.1f}".replace(".", "p")


def _optimum_shift_contexts(
    speed_from: float,
    speed_to: float,
) -> List[Tuple[float, str, LinearSegmentedColormap, str]]:
    """Return high-contrast plotting styles for a two-speed overlay."""
    orange = "#e66101"
    blue = "#1f78b4"
    orange_cmap = LinearSegmentedColormap.from_list(
        "orbit3c_shift_orange",
        ["#fff7ec", "#fdb863", orange],
    )
    blue_cmap = LinearSegmentedColormap.from_list(
        "orbit3c_shift_blue",
        ["#eff6ff", "#80b1d3", blue],
    )
    return [
        (float(speed_from), orange, orange_cmap, "-"),
        (float(speed_to), blue, blue_cmap, "--"),
    ]


def plot_optimum_shift_contours(
    df: pd.DataFrame,
    out_dir: Path,
    speed_from: float = 0.5,
    speed_to: float = 1.0,
    resolutions: Optional[Sequence[str]] = None,
) -> None:
    """Overlay two measured contexts and show their panel-optimum shift."""
    metric = "production_efficiency"
    selected_resolutions = list(resolutions or GRID_RESOLUTIONS)
    unknown_resolutions = set(selected_resolutions) - set(GRID_RESOLUTIONS)
    if unknown_resolutions:
        raise ValueError(
            f"Cannot plot unknown resolution(s): {sorted(unknown_resolutions)}"
        )
    if not selected_resolutions:
        raise ValueError("At least one resolution is required")
    pair = df[df["run_speed"].isin([speed_from, speed_to])].dropna(
        subset=[metric]
    )
    observed_speeds = set(pair["run_speed"].astype(float))
    missing = {float(speed_from), float(speed_to)} - observed_speeds
    if missing:
        raise ValueError(
            f"Cannot plot optimum shift; unmeasured speed(s): {sorted(missing)}"
        )

    levels = _efficiency_contour_levels(pair[metric])
    norm = BoundaryNorm(levels, ncolors=256, clip=True)
    line_levels = levels[1::2]
    contexts = _optimum_shift_contexts(speed_from, speed_to)

    fig, axes = plt.subplots(
        1,
        len(selected_resolutions),
        figsize=(16, 5.2) if len(selected_resolutions) > 1 else (7.2, 5.6),
        sharex=True,
        sharey=True,
        constrained_layout=True,
        squeeze=False,
    )
    axes = axes[0]
    for ax, resolution in zip(axes, selected_resolutions):
        optima: dict[float, pd.Series] = {}
        for speed, color, cmap, linestyle in contexts:
            group = pair[
                (pair["resolution"] == resolution)
                & (pair["run_speed"] == speed)
            ]
            if group.empty:
                continue
            x, y, z = _surface_grid(group, metric)
            zmin, zmax = float(np.nanmin(z)), float(np.nanmax(z))
            if not math.isclose(zmin, zmax, rel_tol=1e-12, abs_tol=1e-12):
                ax.contourf(
                    x,
                    y,
                    z,
                    levels=levels,
                    cmap=cmap,
                    norm=norm,
                    alpha=0.30,
                    extend="max",
                )
                visible_levels = line_levels[
                    (line_levels > zmin) & (line_levels < zmax)
                ]
                if visible_levels.size:
                    contours = ax.contour(
                        x,
                        y,
                        z,
                        levels=visible_levels,
                        colors=[color],
                        linestyles=linestyle,
                        linewidths=2.7,
                    )
                    ax.clabel(
                        contours,
                        inline=True,
                        fontsize=8,
                        fmt=_format_efficiency_contour_label,
                        colors=[color],
                    )

            best = group.loc[group[metric].idxmax()]
            optima[float(speed)] = best
            ax.scatter(
                [best["slice_ul_pct"]],
                [best["edge_cpus"]],
                marker="*",
                s=300,
                facecolor=color,
                edgecolor="black",
                linewidth=1.1,
                clip_on=False,
                zorder=7,
            )

        if len(optima) == 2:
            origin = optima[float(speed_from)]
            destination = optima[float(speed_to)]
            start = (float(origin["slice_ul_pct"]), float(origin["edge_cpus"]))
            end = (
                float(destination["slice_ul_pct"]),
                float(destination["edge_cpus"]),
            )
            if start != end:
                ax.annotate(
                    "",
                    xy=end,
                    xytext=start,
                    arrowprops={
                        "arrowstyle": "-|>",
                        "color": "black",
                        "linewidth": 2.0,
                        "shrinkA": 12,
                        "shrinkB": 12,
                    },
                    zorder=6,
                )
            else:
                ax.scatter(
                    [start[0]],
                    [start[1]],
                    marker="*",
                    s=270,
                    facecolor=contexts[0][1],
                    edgecolor="black",
                    linewidth=0.9,
                    clip_on=False,
                    zorder=8,
                )
                ax.scatter(
                    [start[0]],
                    [start[1]],
                    marker="*",
                    s=120,
                    facecolor=contexts[1][1],
                    edgecolor="black",
                    linewidth=0.7,
                    clip_on=False,
                    zorder=9,
                )

        ax.scatter(
            pair.loc[pair["resolution"] == resolution, "slice_ul_pct"],
            pair.loc[pair["resolution"] == resolution, "edge_cpus"],
            s=20,
            facecolor="white",
            edgecolor="#555555",
            linewidth=0.5,
            alpha=0.85,
            zorder=5,
        )
        ax.set_title(resolution)
        ax.set_xlabel("UL PRB allocation (%)")
        ax.set_xticks(sorted(df["slice_ul_pct"].unique()))
        ax.set_yticks(sorted(df["edge_cpus"].unique()))
        ax.grid(alpha=0.20)

    axes[0].set_ylabel("Edge CPU quota")
    legend = [
        Line2D(
            [],
            [],
            color=color,
            linestyle=linestyle,
            linewidth=3.0,
            marker="*",
            markersize=13,
            markeredgecolor="black",
            label=f"Speed {speed:g} m/s",
        )
        for speed, color, _, linestyle in contexts
    ]
    axes[0].legend(
        handles=legend,
        loc="upper right",
        ncol=1,
        frameon=True,
        framealpha=0.92,
    )
    speed_from_slug = _speed_slug(speed_from)
    speed_to_slug = _speed_slug(speed_to)
    resolution_suffix = (
        ""
        if len(selected_resolutions) > 1
        else f"_resolution_{selected_resolutions[0]}"
    )
    fig.savefig(
        out_dir
        / f"fig_optimum_shift_contours_speed_{speed_from_slug}_to_{speed_to_slug}{resolution_suffix}.png",
        bbox_inches="tight",
        dpi=300,
    )
    plt.close(fig)


def plot_all_optimum_shift_contours(
    df: pd.DataFrame,
    out_dir: Path,
    speed_pairs: Sequence[Tuple[float, float]],
) -> None:
    """Generate optimum-shift plots for each speed pair and resolution."""
    for speed_from, speed_to in _deduplicate_speed_pairs(speed_pairs):
        plot_optimum_shift_contours(
            df,
            out_dir,
            speed_from=speed_from,
            speed_to=speed_to,
        )
        for resolution in GRID_RESOLUTIONS:
            plot_optimum_shift_contours(
                df,
                out_dir,
                speed_from=speed_from,
                speed_to=speed_to,
                resolutions=[resolution],
            )
    
    
def plot_3d_codesign_efficiency_surfaces(
    df: pd.DataFrame,
    out_dir: Path,
) -> None:
    """Plot the measured PRB/CPU efficiency landscape for each workload."""
    metric = "production_efficiency"
    finite = df[metric].replace([np.inf, -np.inf], np.nan).dropna()
    if finite.empty:
        return

    z_min, z_max = float(finite.min()), float(finite.max())
    for speed in sorted(df["run_speed"].dropna().unique()):
        fig = plt.figure(figsize=(16, 5.2))
        for panel, resolution in enumerate(GRID_RESOLUTIONS, start=1):
            ax = fig.add_subplot(1, len(GRID_RESOLUTIONS), panel, projection="3d")
            group = df[
                (df["run_speed"] == speed) & (df["resolution"] == resolution)
            ].dropna(subset=[metric])
            if group.empty:
                ax.set_visible(False)
                continue
            x, y, z = _surface_grid(group, metric)
            ax.plot_surface(
                x, y, z, cmap="viridis", vmin=z_min, vmax=z_max,
                alpha=0.78, linewidth=0.45, edgecolor="white", antialiased=True,
            )
            ax.scatter(
                group["slice_ul_pct"], group["edge_cpus"], group[metric],
                c=group[metric], cmap="viridis", vmin=z_min, vmax=z_max,
                s=24, edgecolor="black", linewidth=0.35, depthshade=False,
            )
            best = group.loc[group[metric].idxmax()]
            ax.scatter(
                [best["slice_ul_pct"]], [best["edge_cpus"]], [best[metric]],
                marker="*", s=180, color="#d62728", edgecolor="black",
                linewidth=0.7, depthshade=False,
            )
            ax.set_title(resolution)
            ax.set_xlabel("UL PRB (%)")
            ax.set_ylabel("Edge CPUs")
            ax.set_zlabel("Production efficiency")
            ax.set_zlim(z_min, z_max * 1.04 if z_max > 0 else 1.0)
            ax.set_xticks(sorted(df["slice_ul_pct"].unique()))
            ax.set_yticks(sorted(df["edge_cpus"].unique()))
            ax.view_init(elev=25, azim=-130)
        fig.suptitle(
            f"Measured co-design efficiency landscape — speed {speed:g} m/s",
            y=0.98,
        )
        fig.text(
            0.5, 0.015,
            "Surfaces connect measured grid points; red star marks the panel optimum.",
            ha="center", fontsize=9, color="#444444",
        )
        plt.tight_layout(rect=(0, 0.035, 1, 0.94))
        plt.savefig(
            out_dir / f"fig_3d_codesign_efficiency_surface_speed_{speed:g}.png",
            bbox_inches="tight", dpi=300,
        )
        plt.close(fig)


def plot_3d_codesign_gain_surfaces(df: pd.DataFrame, out_dir: Path) -> None:
    """Plot efficiency change relative to the fixed reference at each speed."""
    gain_frames: List[pd.DataFrame] = []
    for speed in sorted(df["run_speed"].dropna().unique()):
        reference = df[
            (df["run_speed"] == speed)
            & (df["slice_ul_pct"] == PAPER_REFERENCE_CONFIG["slice_ul_pct"])
            & (df["edge_cpus"] == PAPER_REFERENCE_CONFIG["edge_cpus"])
            & (df["resolution"] == PAPER_REFERENCE_CONFIG["resolution"])
        ]
        if reference.empty:
            continue
        group = df[df["run_speed"] == speed].copy()
        group["efficiency_gain"] = (
            group["production_efficiency"]
            - float(reference.iloc[0]["production_efficiency"])
        )
        gain_frames.append(group)
    if not gain_frames:
        return

    gains = pd.concat(gain_frames, ignore_index=True)
    limit = float(gains["efficiency_gain"].abs().max())
    limit = limit if limit > 0 else 1.0
    for speed, speed_df in gains.groupby("run_speed", sort=True):
        reference_value = float(
            speed_df[
                (speed_df["slice_ul_pct"] == PAPER_REFERENCE_CONFIG["slice_ul_pct"])
                & (speed_df["edge_cpus"] == PAPER_REFERENCE_CONFIG["edge_cpus"])
                & (speed_df["resolution"] == PAPER_REFERENCE_CONFIG["resolution"])
            ].iloc[0]["production_efficiency"]
        )
        fig = plt.figure(figsize=(16, 5.2))
        for panel, resolution in enumerate(GRID_RESOLUTIONS, start=1):
            ax = fig.add_subplot(1, len(GRID_RESOLUTIONS), panel, projection="3d")
            group = speed_df[speed_df["resolution"] == resolution].dropna(
                subset=["efficiency_gain"]
            )
            x, y, z = _surface_grid(group, "efficiency_gain")
            ax.plot_surface(
                x, y, z, cmap="coolwarm", vmin=-limit, vmax=limit,
                alpha=0.82, linewidth=0.45, edgecolor="white", antialiased=True,
            )
            ax.scatter(
                group["slice_ul_pct"], group["edge_cpus"],
                group["efficiency_gain"], c=group["efficiency_gain"],
                cmap="coolwarm", vmin=-limit, vmax=limit, s=24,
                edgecolor="black", linewidth=0.35, depthshade=False,
            )
            ax.plot_surface(
                x, y, np.zeros_like(z), color="#555555", alpha=0.08,
                linewidth=0,
            )
            ax.set_title(resolution)
            ax.set_xlabel("UL PRB (%)")
            ax.set_ylabel("Edge CPUs")
            ax.set_zlabel("Efficiency gain")
            ax.set_zlim(-limit, limit)
            ax.set_xticks(sorted(df["slice_ul_pct"].unique()))
            ax.set_yticks(sorted(df["edge_cpus"].unique()))
            ax.view_init(elev=25, azim=-130)
        fig.suptitle(
            f"Efficiency gain over fixed reference — speed {speed:g} m/s",
            y=0.98,
        )
        fig.text(
            0.5, 0.015,
            f"Baseline: UL75, CPU8, 1280x720 at the same speed "
            f"(efficiency={reference_value:.3f}).",
            ha="center", fontsize=9, color="#444444",
        )
        plt.tight_layout(rect=(0, 0.035, 1, 0.94))
        plt.savefig(
            out_dir / f"fig_3d_codesign_efficiency_gain_speed_{speed:g}.png",
            bbox_inches="tight", dpi=300,
        )
        plt.close(fig)


def _pareto_3d_mask(group: pd.DataFrame) -> np.ndarray:
    """Return points non-dominated in PRB, CPU, and goodput."""
    values = group[["prb_cost", "cpu_cost", "production_goodput"]].to_numpy(float)
    keep = np.ones(len(values), dtype=bool)
    for i, (prb_i, cpu_i, goodput_i) in enumerate(values):
        dominates_i = (
            (values[:, 0] <= prb_i)
            & (values[:, 1] <= cpu_i)
            & (values[:, 2] >= goodput_i)
            & (
                (values[:, 0] < prb_i)
                | (values[:, 1] < cpu_i)
                | (values[:, 2] > goodput_i)
            )
        )
        keep[i] = not dominates_i.any()
    return keep


def plot_3d_resource_performance_pareto(df: pd.DataFrame, out_dir: Path) -> None:
    """Show network/compute/goodput trade-offs and the 3-objective Pareto set."""
    group_all = df.dropna(
        subset=["prb_cost", "cpu_cost", "production_goodput", "production_efficiency"]
    )
    if group_all.empty:
        return
    norm = Normalize(
        vmin=float(group_all["production_efficiency"].min()),
        vmax=float(group_all["production_efficiency"].max()),
    )
    markers = {"1280x720": "o", "640x360": "s", "320x180": "^"}
    fig = plt.figure(figsize=(13.5, 10))
    axes = []
    scatter_for_colorbar = None
    for panel, speed in enumerate(sorted(group_all["run_speed"].unique()), start=1):
        ax = fig.add_subplot(2, 2, panel, projection="3d")
        axes.append(ax)
        group = group_all[group_all["run_speed"] == speed].copy()
        for resolution, points in group.groupby("resolution"):
            scatter_for_colorbar = ax.scatter(
                points["prb_cost"], points["cpu_cost"],
                points["production_goodput"],
                c=points["production_efficiency"], cmap="viridis", norm=norm,
                marker=markers.get(resolution, "o"), s=42, alpha=0.82,
                linewidth=0,
            )
        pareto = group.loc[_pareto_3d_mask(group)]
        ax.scatter(
            pareto["prb_cost"], pareto["cpu_cost"], pareto["production_goodput"],
            facecolors="none", edgecolors="black", marker="o", s=105,
            linewidth=1.0, depthshade=False,
        )
        ax.set_title(f"Speed = {speed:g} m/s")
        ax.set_xlabel("UL PRB allocation (%)")
        ax.set_ylabel("Edge CPU quota")
        ax.set_zlabel("")
        ax.view_init(elev=24, azim=-135)
    if scatter_for_colorbar is not None:
        colorbar_axis = fig.add_axes([0.91, 0.18, 0.018, 0.62])
        colorbar = fig.colorbar(scatter_for_colorbar, cax=colorbar_axis)
        colorbar.set_label("Production efficiency")
    legend_items = [
        Line2D([0], [0], marker=marker, linestyle="", color="#555555",
               label=resolution, markersize=7)
        for resolution, marker in markers.items()
    ]
    legend_items.append(
        Line2D([0], [0], marker="o", linestyle="", markerfacecolor="none",
               markeredgecolor="black", label="3-objective Pareto set", markersize=9)
    )
    fig.legend(
        handles=legend_items, loc="upper center", bbox_to_anchor=(0.47, 0.94),
        ncol=4, frameon=False,
    )
    fig.suptitle("Network–compute–production trade-off", y=0.985)
    fig.text(
        0.008, 0.5, "Production goodput (correct parts/min)", rotation=90,
        va="center", ha="center", fontsize=12,
    )
    plt.subplots_adjust(left=0.02, right=0.86, bottom=0.04, top=0.89, wspace=0.02, hspace=0.1)
    plt.savefig(
        out_dir / "fig_3d_resource_performance_pareto.png",
        bbox_inches="tight", dpi=300,
    )
    plt.close(fig)


def plot_3d_constrained_policy_comparison(
    comparison: pd.DataFrame,
    out_dir: Path,
) -> None:
    """Plot constrained policy efficiency, preserving infeasible cases as such."""
    if comparison.empty:
        return
    policy_order = list(PAPER_POLICY_LABELS)
    speeds = sorted(comparison["run_speed"].unique())
    colors = {
        "fixed": "#7f7f7f", "slice_only": "#4c78a8",
        "cpu_only": "#f58518", "resolution_only": "#54a24b",
        "joint": "#b22222",
    }
    fig = plt.figure(figsize=(12, 7.5))
    ax = fig.add_subplot(111, projection="3d")
    dx, dy = 0.55, 0.58
    for row in comparison.itertuples():
        x = speeds.index(row.run_speed)
        # Put joint co-design at the front of the selected camera view so its
        # bars are not hidden by the reference and single-factor policies.
        y = len(policy_order) - 1 - policy_order.index(row.policy)
        if row.eligible and pd.notna(row.production_efficiency):
            ax.bar3d(
                x - dx / 2, y - dy / 2, 0, dx, dy,
                float(row.production_efficiency), color=colors[row.policy],
                edgecolor="white", linewidth=0.45, shade=True, alpha=0.92,
            )
        else:
            ax.scatter(
                [x], [y], [0], marker="x", s=70, color="black",
                linewidth=1.5, depthshade=False,
            )
    ax.set_xticks(range(len(speeds)), [f"{speed:g}" for speed in speeds])
    ax.set_yticks(
        range(len(policy_order)),
        [PAPER_POLICY_LABELS[policy] for policy in reversed(policy_order)],
    )
    ax.set_xlabel("Conveyor speed (m/s)")
    ax.set_ylabel("Policy")
    ax.set_zlabel("")
    fraction = float(comparison["min_goodput_fraction"].iloc[0])
    ax.set_title(
        f"Policy comparison retaining at least {fraction:.0%} of maximum goodput",
        pad=22,
    )
    ax.view_init(elev=25, azim=-125)
    ax.tick_params(axis="y", labelsize=9, pad=1)
    legend_items = [
        Patch(facecolor=colors[policy], label=PAPER_POLICY_LABELS[policy])
        for policy in policy_order
    ]
    legend_items.append(
        Line2D([0], [0], marker="x", linestyle="", color="black",
               label="Not feasible", markersize=7)
    )
    ax.legend(
        handles=legend_items, loc="upper left", bbox_to_anchor=(1.01, 0.88),
        frameon=False,
    )
    fig.text(
        0.018, 0.48, "Constrained production efficiency", rotation=90,
        va="center", ha="center", fontsize=12,
    )
    fig.subplots_adjust(left=0.05, right=0.80, bottom=0.08, top=0.88)
    plt.savefig(
        out_dir / "fig_3d_constrained_policy_comparison.png",
        bbox_inches="tight", dpi=300,
    )
    plt.close(fig)


def _reflected_grid_path(
    levels: Sequence[Sequence[Any]],
) -> List[Tuple[Any, ...]]:
    """Construct a Cartesian-grid path changing exactly one field per step."""
    if not levels:
        return [tuple()]
    tail = _reflected_grid_path(levels[1:])
    path: List[Tuple[Any, ...]] = []
    for index, value in enumerate(levels[0]):
        block = tail if index % 2 == 0 else list(reversed(tail))
        path.extend((value, *suffix) for suffix in block)
    return path


def build_full_efficiency_trajectory(df: pd.DataFrame) -> pd.DataFrame:
    """Build four auditable 48-step paths covering the complete 192-run grid."""
    path = _reflected_grid_path([
        GRID_RESOLUTIONS,
        GRID_CPU_LEVELS,
        GRID_UL_LEVELS,
    ])
    rows: List[Dict[str, Any]] = []
    key_columns = ["resolution", "edge_cpus", "slice_ul_pct"]

    for speed in sorted(df["run_speed"].dropna().unique()):
        speed_df = df[df["run_speed"] == speed]
        if speed_df.duplicated(key_columns).any():
            raise ValueError(
                f"Trajectory requires one run per configuration at speed {speed:g}."
            )
        lookup = speed_df.set_index(key_columns, drop=False)
        previous: Optional[pd.Series] = None
        running_best = -np.inf
        for step, key in enumerate(path, start=1):
            if key not in lookup.index:
                raise ValueError(
                    f"Missing trajectory configuration at speed {speed:g}: {key}"
                )
            current = lookup.loc[key]
            if isinstance(current, pd.DataFrame):
                raise ValueError(f"Duplicate trajectory configuration: {key}")
            efficiency = float(current["production_efficiency"])
            if previous is None:
                changed_variable = "start"
                gain = np.nan
            else:
                changed = [
                    column for column in key_columns
                    if current[column] != previous[column]
                ]
                if len(changed) != 1:
                    raise AssertionError(
                        f"Invalid path transition at speed {speed:g}, step {step}: "
                        f"changed {changed}"
                    )
                changed_variable = changed[0]
                gain = efficiency - float(previous["production_efficiency"])
            running_best = max(running_best, efficiency)
            rows.append({
                "run_speed": float(speed),
                "trajectory_step": step,
                "changed_variable": changed_variable,
                "efficiency_gain_from_previous": gain,
                "running_best_efficiency": running_best,
                "production_efficiency": efficiency,
                "production_goodput": float(current["production_goodput"]),
                "correct_on_time_rate": float(current["correct_on_time_rate"]),
                "resource_cost": float(current["resource_cost"]),
                "slice_ul_pct": float(current["slice_ul_pct"]),
                "edge_cpus": float(current["edge_cpus"]),
                "resolution": str(current["resolution"]),
                "configuration": format_cfg(current),
                "run_id": current["run_id"],
            })
            previous = current
    trajectory = pd.DataFrame(rows)
    if len(trajectory) != len(df):
        raise AssertionError(
            f"Trajectory covers {len(trajectory)} rows, expected {len(df)}."
        )
    return trajectory


def plot_full_efficiency_trajectory(
    df: pd.DataFrame,
    out_dir: Path,
) -> pd.DataFrame:
    """Plot all configurations with signed step gains and change provenance."""
    trajectory = build_full_efficiency_trajectory(df)
    trajectory.to_csv(out_dir / "full_192_efficiency_trajectory.csv", index=False)

    change_colors = {
        "start": "#7f7f7f",
        "slice_ul_pct": "#4c78a8",
        "edge_cpus": "#f58518",
        "resolution": "#54a24b",
    }
    change_labels = {
        "start": "Starting reference",
        "slice_ul_pct": "UL PRB changed",
        "edge_cpus": "CPU changed",
        "resolution": "Resolution changed",
    }
    change_order = list(change_colors)
    change_cmap = ListedColormap([change_colors[key] for key in change_order])

    for speed, group in trajectory.groupby("run_speed", sort=True):
        group = group.sort_values("trajectory_step")
        x = group["trajectory_step"].to_numpy()
        efficiency = group["production_efficiency"].to_numpy()
        gain = group["efficiency_gain_from_previous"].fillna(0).to_numpy()

        fig = plt.figure(figsize=(13, 7.2))
        grid = fig.add_gridspec(3, 1, height_ratios=[3.2, 1.5, 0.28], hspace=0.10)
        ax_efficiency = fig.add_subplot(grid[0])
        ax_gain = fig.add_subplot(grid[1], sharex=ax_efficiency)
        ax_change = fig.add_subplot(grid[2], sharex=ax_efficiency)

        ax_efficiency.plot(
            x, efficiency, drawstyle="steps-mid", color="#24557a",
            linewidth=1.4, label="Measured efficiency",
        )
        ax_efficiency.scatter(x, efficiency, color="#24557a", s=15, zorder=3)
        ax_efficiency.plot(
            x, group["running_best_efficiency"], color="#b22222",
            linewidth=2.0, linestyle="--", label="Running best",
        )
        best = group.loc[group["production_efficiency"].idxmax()]
        ax_efficiency.scatter(
            [best["trajectory_step"]], [best["production_efficiency"]],
            marker="*", s=190, color="#ffd92f", edgecolor="black",
            linewidth=0.7, zorder=5, label="Global optimum",
        )
        ax_efficiency.annotate(
            f"{best['configuration']}\n$\\eta$={best['production_efficiency']:.3f}",
            (best["trajectory_step"], best["production_efficiency"]),
            xytext=(10, -34), textcoords="offset points", fontsize=8,
            arrowprops={"arrowstyle": "-", "color": "#555555"},
        )
        ax_efficiency.set_ylabel("Production efficiency")
        ax_efficiency.set_ylim(bottom=0)
        ax_efficiency.grid(axis="y", alpha=0.25)
        ax_efficiency.legend(ncol=3, frameon=False, loc="upper right")
        ax_efficiency.tick_params(axis="x", labelbottom=False)

        bar_colors = np.where(gain > 0, "#2ca02c", np.where(gain < 0, "#d62728", "#aaaaaa"))
        ax_gain.bar(x, gain, color=bar_colors, width=0.82)
        ax_gain.axhline(0, color="black", linewidth=0.8)
        ax_gain.set_ylabel("Gain from\nprevious step")
        ax_gain.grid(axis="y", alpha=0.25)
        ax_gain.tick_params(axis="x", labelbottom=False)

        codes = np.array([
            change_order.index(value) for value in group["changed_variable"]
        ])[None, :]
        ax_change.imshow(
            codes, aspect="auto", interpolation="nearest", cmap=change_cmap,
            vmin=-0.5, vmax=len(change_order) - 0.5,
            extent=(x.min() - 0.5, x.max() + 0.5, 0, 1),
        )
        ax_change.set_yticks([])
        ax_change.set_xlabel("Trajectory step (one measured configuration per step)")
        ax_change.set_xlim(x.min() - 0.5, x.max() + 0.5)
        ax_change.set_xticks([1, 8, 16, 24, 32, 40, 48])
        change_legend = [
            Patch(facecolor=change_colors[key], label=change_labels[key])
            for key in change_order
        ]
        ax_change.legend(
            handles=change_legend, loc="upper center", bbox_to_anchor=(0.5, -1.35),
            ncol=4, frameon=False,
        )
        fig.suptitle(
            f"One-control-at-a-time traversal — speed {speed:g} m/s",
            y=0.98,
        )
        fig.text(
            0.5, 0.925,
            "All 48 measured configurations; incremental gains depend on this "
            "explicit reflected-grid path.",
            ha="center", fontsize=9, color="#444444",
        )
        fig.subplots_adjust(left=0.08, right=0.98, top=0.88, bottom=0.16)
        plt.savefig(
            out_dir / f"fig_full_efficiency_trajectory_speed_{speed:g}.png",
            bbox_inches="tight", dpi=300,
        )
        plt.close(fig)
    return trajectory


def _deterministic_efficiency_best(
    group: pd.DataFrame,
    incumbent: Optional[pd.Series] = None,
) -> pd.Series:
    """Choose an optimum deterministically, retaining an incumbent on a tie."""
    if group.empty:
        raise ValueError("Cannot select a trajectory step from an empty group.")
    maximum = float(group["production_efficiency"].max())
    if (
        incumbent is not None
        and math.isclose(
            float(incumbent["production_efficiency"]), maximum,
            rel_tol=0.0, abs_tol=1e-12,
        )
    ):
        # A zero-gain tuning stage should not appear to change a control merely
        # because of an arbitrary tie between measured configurations.
        return incumbent
    return group.sort_values(
        ["production_efficiency", "production_goodput", "resource_cost", "run_id"],
        ascending=[False, False, True, True],
    ).iloc[0]


def build_coordinate_descent_trajectory(df: pd.DataFrame) -> pd.DataFrame:
    """Tune PRB, then CPU, then resolution, followed by the joint optimum."""
    rows: List[Dict[str, Any]] = []
    stage_names = [
        "Fixed reference", "Tune PRB", "Tune CPU", "Tune resolution",
        "Joint optimum",
    ]
    for speed in sorted(df["run_speed"].dropna().unique()):
        speed_df = df[df["run_speed"] == speed]
        reference = speed_df[
            (speed_df["slice_ul_pct"] == PAPER_REFERENCE_CONFIG["slice_ul_pct"])
            & (speed_df["edge_cpus"] == PAPER_REFERENCE_CONFIG["edge_cpus"])
            & (speed_df["resolution"] == PAPER_REFERENCE_CONFIG["resolution"])
        ]
        if len(reference) != 1:
            raise ValueError(f"Expected one fixed reference at speed {speed:g}.")
        selected: List[pd.Series] = [reference.iloc[0]]
        selected.append(_deterministic_efficiency_best(speed_df[
            (speed_df["edge_cpus"] == selected[-1]["edge_cpus"])
            & (speed_df["resolution"] == selected[-1]["resolution"])
        ], incumbent=selected[-1]))
        selected.append(_deterministic_efficiency_best(speed_df[
            (speed_df["slice_ul_pct"] == selected[-1]["slice_ul_pct"])
            & (speed_df["resolution"] == selected[-1]["resolution"])
        ], incumbent=selected[-1]))
        selected.append(_deterministic_efficiency_best(speed_df[
            (speed_df["slice_ul_pct"] == selected[-1]["slice_ul_pct"])
            & (speed_df["edge_cpus"] == selected[-1]["edge_cpus"])
        ], incumbent=selected[-1]))
        selected.append(_deterministic_efficiency_best(speed_df))

        previous_efficiency = np.nan
        for stage, (stage_name, current) in enumerate(
            zip(stage_names, selected), start=1
        ):
            efficiency = float(current["production_efficiency"])
            rows.append({
                "run_speed": float(speed),
                "stage": stage,
                "stage_name": stage_name,
                "production_efficiency": efficiency,
                "efficiency_gain_from_previous": (
                    efficiency - previous_efficiency
                    if pd.notna(previous_efficiency) else np.nan
                ),
                "production_goodput": float(current["production_goodput"]),
                "resource_cost": float(current["resource_cost"]),
                "slice_ul_pct": float(current["slice_ul_pct"]),
                "edge_cpus": float(current["edge_cpus"]),
                "resolution": str(current["resolution"]),
                "configuration": format_cfg(current),
                "run_id": current["run_id"],
            })
            previous_efficiency = efficiency
    return pd.DataFrame(rows)


def plot_coordinate_descent_trajectory(
    df: pd.DataFrame,
    out_dir: Path,
) -> pd.DataFrame:
    """Plot the compact, interpretable stage-wise co-design trajectory."""
    trajectory = build_coordinate_descent_trajectory(df)
    trajectory.to_csv(
        out_dir / "coordinate_descent_efficiency_trajectory.csv", index=False
    )
    efficiency_upper = max(
        float(trajectory["production_efficiency"].max()) * 1.22, 0.1
    )
    fig, axes = plt.subplots(2, 2, figsize=(12, 8.2), sharey=True, squeeze=False)
    for ax, (speed, group) in zip(
        axes.flat, trajectory.groupby("run_speed", sort=True)
    ):
        group = group.sort_values("stage")
        ax.plot(
            group["stage"], group["production_efficiency"], marker="o",
            linewidth=2.0, color="#24557a",
        )
        ax.fill_between(
            group["stage"], 0, group["production_efficiency"],
            color="#4c78a8", alpha=0.10,
        )
        for row in group.itertuples():
            gain_text = "start" if pd.isna(row.efficiency_gain_from_previous) else f"{row.efficiency_gain_from_previous:+.3f}"
            ax.annotate(
                f"{row.production_efficiency:.3f}\n({gain_text})",
                (row.stage, row.production_efficiency),
                xytext=(0, 7), textcoords="offset points", ha="center",
                fontsize=8,
            )
        ax.set_xticks(group["stage"], group["stage_name"], rotation=22, ha="right")
        ax.set_title(f"Speed = {speed:g} m/s")
        ax.set_ylim(0, efficiency_upper)
        ax.grid(axis="y", alpha=0.25)
    fig.suptitle("Stepwise co-design: PRB → CPU → resolution → joint search", y=0.99)
    fig.supxlabel("Optimization stage (annotation: efficiency and incremental gain)", y=0.01)
    fig.supylabel("Production efficiency", x=0.01)
    fig.text(
        0.5, 0.945,
        "Unconstrained efficiency optimization; coordinate order is fixed and reported.",
        ha="center", fontsize=9, color="#444444",
    )
    plt.tight_layout(rect=(0.03, 0.04, 1, 0.93))
    plt.savefig(
        out_dir / "fig_coordinate_descent_efficiency_trajectory.png",
        bbox_inches="tight", dpi=300,
    )
    plt.close(fig)
    return trajectory


def plot_variable_effects(df: pd.DataFrame, out_dir: Path, metric: str) -> None:
    # PRB effect
    subdir = out_dir / f"prb_effect_{metric}"
    ensure_out_dir(subdir)
    for res in GRID_RESOLUTIONS:
        for speed in GRID_SPEEDS:
            g = df[(df["resolution"] == res) & (df["run_speed"] == speed)]
            if g.empty:
                continue
            plt.figure(figsize=(8, 5))
            for cpu, h in g.groupby("edge_cpus"):
                h = h.sort_values("slice_ul_pct")
                plt.plot(h["slice_ul_pct"], h[metric], marker="o", label=f"CPU={cpu:g}")
                if metric == "correct_on_time_rate" and "correct_on_time_parts" in h.columns:
                    add_point_count_labels(plt.gca(), h["slice_ul_pct"], h[metric], h["correct_on_time_parts"].astype(int))
            plt.xlabel("Industrial UL PRB percentage")
            plt.ylabel(metric.replace("_", " "))
            plt.title(f"PRB effect: {res}, speed={speed:g}")
            apply_metric_axis(plt.gca(), metric)
            plt.grid(True, alpha=0.3)
            plt.legend()
            savefig(subdir, f"prb_effect_{metric}_{res}_speed{sanitize_float_for_name(speed)}.png")

    # CPU effect
    subdir = out_dir / f"cpu_effect_{metric}"
    ensure_out_dir(subdir)
    for res in GRID_RESOLUTIONS:
        for speed in GRID_SPEEDS:
            g = df[(df["resolution"] == res) & (df["run_speed"] == speed)]
            if g.empty:
                continue
            plt.figure(figsize=(8, 5))
            for prb, h in g.groupby("slice_ul_pct"):
                h = h.sort_values("edge_cpus")
                plt.plot(h["edge_cpus"], h[metric], marker="o", label=f"UL={int(prb)}")
                if metric == "correct_on_time_rate" and "correct_on_time_parts" in h.columns:
                    add_point_count_labels(plt.gca(), h["edge_cpus"], h[metric], h["correct_on_time_parts"].astype(int))
            plt.xlabel("Edge CPU quota")
            plt.ylabel(metric.replace("_", " "))
            plt.title(f"CPU effect: {res}, speed={speed:g}")
            apply_metric_axis(plt.gca(), metric)
            plt.grid(True, alpha=0.3)
            plt.legend()
            savefig(subdir, f"cpu_effect_{metric}_{res}_speed{sanitize_float_for_name(speed)}.png")

    # Resolution effect
    subdir = out_dir / f"resolution_effect_{metric}"
    ensure_out_dir(subdir)
    res_x = {r: i for i, r in enumerate(GRID_RESOLUTIONS)}
    for prb in GRID_UL_LEVELS:
        for cpu in GRID_CPU_LEVELS:
            g = df[(df["slice_ul_pct"] == prb) & (df["edge_cpus"] == cpu)].copy()
            if g.empty:
                continue
            plt.figure(figsize=(8, 5))
            for speed, h in g.groupby("run_speed"):
                h = h.copy()
                h["res_x"] = h["resolution"].map(res_x)
                h = h.sort_values("res_x")
                plt.plot(h["res_x"], h[metric], marker="o", label=f"v={speed:g}")
                if metric == "correct_on_time_rate" and "correct_on_time_parts" in h.columns:
                    add_point_count_labels(plt.gca(), h["res_x"], h[metric], h["correct_on_time_parts"].astype(int))
            plt.xticks(range(len(GRID_RESOLUTIONS)), GRID_RESOLUTIONS)
            plt.xlabel("Resolution")
            plt.ylabel(metric.replace("_", " "))
            plt.title(f"Resolution effect: UL{prb}, CPU={cpu:g}")
            apply_metric_axis(plt.gca(), metric)
            plt.grid(True, alpha=0.3)
            plt.legend()
            savefig(subdir, f"resolution_effect_{metric}_ul{int(prb)}_cpu{sanitize_float_for_name(cpu)}.png")

    # Speed effect
    subdir = out_dir / f"speed_effect_{metric}"
    ensure_out_dir(subdir)
    for prb in GRID_UL_LEVELS:
        for cpu in GRID_CPU_LEVELS:
            g = df[(df["slice_ul_pct"] == prb) & (df["edge_cpus"] == cpu)]
            if g.empty:
                continue
            plt.figure(figsize=(8, 5))
            for res, h in g.groupby("resolution"):
                h = h.sort_values("run_speed")
                plt.plot(h["run_speed"], h[metric], marker="o", label=res)
                if metric == "correct_on_time_rate" and "correct_on_time_parts" in h.columns:
                    add_point_count_labels(plt.gca(), h["run_speed"], h[metric], h["correct_on_time_parts"].astype(int))
            plt.xlabel("Conveyor speed (m/s)")
            plt.ylabel(metric.replace("_", " "))
            plt.title(f"Speed effect: UL{prb}, CPU={cpu:g}")
            apply_metric_axis(plt.gca(), metric)
            plt.grid(True, alpha=0.3)
            plt.legend()
            savefig(subdir, f"speed_effect_{metric}_ul{int(prb)}_cpu{sanitize_float_for_name(cpu)}.png")

    # Heatmaps by PRB/CPU
    subdir = out_dir / f"heatmaps_{metric}"
    ensure_out_dir(subdir)
    for prb in GRID_UL_LEVELS:
        for cpu in GRID_CPU_LEVELS:
            g = df[(df["slice_ul_pct"] == prb) & (df["edge_cpus"] == cpu)]
            if g.empty:
                continue
            pivot = g.pivot_table(index="resolution", columns="run_speed", values=metric, aggfunc="mean")
            pivot = pivot.reindex([r for r in GRID_RESOLUTIONS if r in pivot.index])
            if pivot.empty:
                continue
            plt.figure(figsize=(7, 4))
            im = plt.imshow(pivot.values, aspect="auto")
            plt.colorbar(im, label=metric.replace("_", " "))
            plt.xticks(range(len(pivot.columns)), [f"{c:g}" for c in pivot.columns])
            plt.yticks(range(len(pivot.index)), pivot.index)
            plt.xlabel("Conveyor speed (m/s)")
            plt.ylabel("Resolution")
            plt.title(f"{metric.replace('_', ' ')}: UL{prb}, CPU={cpu:g}")
            savefig(subdir, f"heatmap_{metric}_ul{int(prb)}_cpu{sanitize_float_for_name(cpu)}.png")


# =============================================================================
# Joint-vs-independent and dynamic replay
# =============================================================================

def select_best(df: pd.DataFrame, metric: str) -> Optional[pd.Series]:
    g = df.dropna(subset=[metric])
    if g.empty:
        return None
    return g.sort_values(metric, ascending=False).iloc[0]


def sequential_independent_tuning(
    df: pd.DataFrame,
    speed: float,
    initial: Dict[str, Any],
    metric: str = "production_goodput",
    max_iter: int = 10,
) -> Optional[pd.Series]:
    g_speed = df[df["run_speed"] == speed].copy()
    if g_speed.empty:
        return None

    current = initial.copy()
    current["run_speed"] = speed

    def filt(g: pd.DataFrame, keys: Iterable[str]) -> pd.DataFrame:
        h = g
        for k in keys:
            h = h[h[k] == current[k]]
        return h

    all_keys = ["slice_ul_pct", "edge_cpus", "resolution", "run_speed"]
    if filt(g_speed, all_keys).empty:
        start = select_best(g_speed, metric)
        if start is None:
            return None
        current = {
            "slice_ul_pct": float(start["slice_ul_pct"]),
            "edge_cpus": float(start["edge_cpus"]),
            "resolution": start["resolution"],
            "run_speed": speed,
        }

    prev_val = -np.inf
    for _ in range(max_iter):
        # PRB best response
        b = select_best(filt(g_speed, ["edge_cpus", "resolution", "run_speed"]), metric)
        if b is not None:
            current["slice_ul_pct"] = float(b["slice_ul_pct"])

        # CPU best response
        b = select_best(filt(g_speed, ["slice_ul_pct", "resolution", "run_speed"]), metric)
        if b is not None:
            current["edge_cpus"] = float(b["edge_cpus"])

        # Resolution best response
        b = select_best(filt(g_speed, ["slice_ul_pct", "edge_cpus", "run_speed"]), metric)
        if b is not None:
            current["resolution"] = b["resolution"]

        h_cur = filt(g_speed, all_keys)
        if h_cur.empty:
            return None
        cur = select_best(h_cur, metric)
        val = float(cur[metric])
        if abs(val - prev_val) < 1e-12:
            return cur
        prev_val = val

    return select_best(filt(g_speed, all_keys), metric)


def plot_joint_vs_independent_gap(df: pd.DataFrame, out_dir: Path) -> None:
    rows = []
    fixed_base = {"slice_ul_pct": 75.0, "edge_cpus": 8.0, "resolution": "1280x720"}

    for speed in sorted(df["run_speed"].dropna().unique()):
        g_speed = df[df["run_speed"] == speed]
        joint = select_best(g_speed, "production_goodput")
        indep = sequential_independent_tuning(df, speed, fixed_base, metric="production_goodput")

        fixed = g_speed[
            (g_speed["slice_ul_pct"] == fixed_base["slice_ul_pct"])
            & (g_speed["edge_cpus"] == fixed_base["edge_cpus"])
            & (g_speed["resolution"] == fixed_base["resolution"])
        ]
        fixed_best = select_best(fixed, "production_goodput")

        if joint is None:
            continue

        rows.append({
            "run_speed": speed,
            "joint_goodput": float(joint["production_goodput"]),
            "independent_goodput": float(indep["production_goodput"]) if indep is not None else np.nan,
            "fixed_goodput": float(fixed_best["production_goodput"]) if fixed_best is not None else np.nan,
            "joint_cot": float(joint["correct_on_time_rate"]),
            "independent_cot": float(indep["correct_on_time_rate"]) if indep is not None else np.nan,
            "fixed_cot": float(fixed_best["correct_on_time_rate"]) if fixed_best is not None else np.nan,
            "joint_cfg": format_cfg(joint),
            "independent_cfg": format_cfg(indep) if indep is not None else "",
            "fixed_cfg": format_cfg(fixed_best) if fixed_best is not None else "",
        })

    summary = pd.DataFrame(rows)
    summary.to_csv(out_dir / "joint_vs_independent_by_speed.csv", index=False)
    if summary.empty:
        return

    plt.figure(figsize=(8, 6))
    plt.plot(summary["run_speed"], summary["joint_goodput"], marker="o", linewidth=2, label="Joint grid oracle")
    plt.plot(summary["run_speed"], summary["independent_goodput"], marker="o", linestyle="--", linewidth=2, label="Independent sequential tuning")
    plt.plot(summary["run_speed"], summary["fixed_goodput"], marker="o", linestyle=":", linewidth=2, label="Fixed high-resource")
    plt.fill_between(
        summary["run_speed"].values,
        summary["independent_goodput"].astype(float).values,
        summary["joint_goodput"].astype(float).values,
        alpha=0.15,
    )
    plt.xlabel("Conveyor speed (m/s)")
    plt.ylabel("Production goodput (correct parts/min)")
    plt.title("Joint optimum vs independent tuning")
    plt.grid(True, alpha=0.3)
    plt.legend()
    savefig(out_dir, "fig_joint_vs_independent_gap_goodput.png")

    plt.figure(figsize=(8, 6))
    plt.plot(summary["run_speed"], summary["joint_cot"], marker="o", linewidth=2, label="Joint grid oracle")
    plt.plot(summary["run_speed"], summary["independent_cot"], marker="o", linestyle="--", linewidth=2, label="Independent sequential tuning")
    plt.plot(summary["run_speed"], summary["fixed_cot"], marker="o", linestyle=":", linewidth=2, label="Fixed high-resource")
    plt.fill_between(
        summary["run_speed"].values,
        summary["independent_cot"].astype(float).values,
        summary["joint_cot"].astype(float).values,
        alpha=0.15,
    )
    plt.xlabel("Conveyor speed (m/s)")
    plt.ylabel("Correct-on-time rate")
    plt.title("Correct-on-time frontier by policy")
    plt.grid(True, alpha=0.3)
    plt.legend()
    savefig(out_dir, "fig_correct_on_time_frontier_by_policy.png")


def dynamic_replay(df: pd.DataFrame, out_dir: Path) -> None:
    demand_speeds = [0.5, 1.0, 1.5, 2.0, 1.0]
    windows_per_stage = 3
    fixed_base = {"slice_ul_pct": 75.0, "edge_cpus": 8.0, "resolution": "1280x720"}

    rows = []
    t = 0
    for stage, speed in enumerate(demand_speeds):
        for w in range(windows_per_stage):
            g_speed = df[df["run_speed"] == speed]
            if g_speed.empty:
                continue

            joint = select_best(g_speed, "production_goodput")
            indep = sequential_independent_tuning(df, speed, fixed_base, metric="production_goodput")

            fixed = g_speed[
                (g_speed["slice_ul_pct"] == fixed_base["slice_ul_pct"])
                & (g_speed["edge_cpus"] == fixed_base["edge_cpus"])
                & (g_speed["resolution"] == fixed_base["resolution"])
            ]
            fixed_best = select_best(fixed, "production_goodput")

            # Offline replay: first window after each demand change uses independent value,
            # subsequent windows use joint value as the adapted target.
            proposed = np.nan
            if w == 0 and indep is not None:
                proposed = float(indep["production_goodput"])
            elif joint is not None:
                proposed = float(joint["production_goodput"])

            rows.append({
                "time_window": t,
                "stage": stage,
                "demand_speed": speed,
                "joint_oracle": float(joint["production_goodput"]) if joint is not None else np.nan,
                "independent": float(indep["production_goodput"]) if indep is not None else np.nan,
                "fixed": float(fixed_best["production_goodput"]) if fixed_best is not None else np.nan,
                "proposed_coadaptation": proposed,
            })
            t += 1

    replay = pd.DataFrame(rows)
    replay.to_csv(out_dir / "dynamic_replay_over_measured_grid.csv", index=False)
    if replay.empty:
        return

    fig, axis = plt.subplots(figsize=(10, 5))
    axis.plot(replay["time_window"], replay["joint_oracle"], linewidth=2, linestyle="--", label="Oracle envelope")
    axis.plot(replay["time_window"], replay["fixed"], linewidth=2, linestyle=":", label="Best fixed")
    axis.plot(replay["time_window"], replay["independent"], linewidth=2, linestyle="-.", label="Independent tuning")
    axis.plot(replay["time_window"], replay["proposed_coadaptation"], linewidth=2, label="Proposed co-adaptation replay")
    speed_axis = axis.twinx()
    speed_axis.plot(replay["time_window"], replay["demand_speed"], color="gray", linewidth=1.5, alpha=0.7, label="Demand speed")

    axis.set_xlabel("Production window")
    axis.set_ylabel("Production goodput (correct parts/min)")
    speed_axis.set_ylabel("Demand speed (m/s)")
    axis.set_title("Offline dynamic replay over measured grid")
    axis.grid(True, alpha=0.3)
    handles, labels = axis.get_legend_handles_labels()
    speed_handles, speed_labels = speed_axis.get_legend_handles_labels()
    axis.legend(handles + speed_handles, labels + speed_labels, ncol=2)
    savefig(out_dir, "fig_dynamic_coadaptation_replay.png")


# =============================================================================
# Main
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--include-old-runs", action="store_true")
    parser.add_argument(
        "--paper-goodput-fraction",
        type=float,
        default=DEFAULT_PAPER_GOODPUT_FRACTION,
        help=(
            "Minimum fraction of the maximum measured goodput retained by the "
            "performance-constrained paper comparison (default: 0.90)."
        ),
    )
    parser.add_argument(
        "--shift-speed-from",
        type=float,
        default=0.5,
        help="Starting speed for the pairwise optimum-shift contour figure.",
    )
    parser.add_argument(
        "--shift-speed-to",
        type=float,
        default=1.0,
        help="Ending speed for the pairwise optimum-shift contour figure.",
    )
    args = parser.parse_args()
    if math.isclose(args.shift_speed_from, args.shift_speed_to):
        parser.error("--shift-speed-from and --shift-speed-to must differ")

    set_plot_defaults()
    ensure_out_dir(args.out_dir)

    print(f"[INFO] Reading results from: {args.results_root}")
    all_df = load_all_summaries(args.results_root)

    all_path = args.out_dir / "summary_all_detected_runs.csv"
    all_df.to_csv(all_path, index=False)
    print(f"[SAVED] {all_path} ({len(all_df)} rows)")

    df = filter_to_current_grid(all_df, strict=not args.include_old_runs)
    if df.empty:
        raise RuntimeError("No rows remain after filtering to the current grid.")

    summary_path = args.out_dir / "summary_current_grid.csv"
    df.to_csv(summary_path, index=False)
    print(f"[SAVED] {summary_path} ({len(df)} rows)")

    expected = len(GRID_UL_LEVELS) * len(GRID_CPU_LEVELS) * len(GRID_RESOLUTIONS) * len(GRID_SPEEDS)
    print(f"[INFO] Current-grid summaries found: {len(df)} / expected {expected}")

    completion = (
        df.groupby(["slice_ul_pct", "slice_dl_pct", "edge_cpus", "resolution", "run_speed"], dropna=False)
        .size()
        .reset_index(name="num_runs")
    )
    completion.to_csv(args.out_dir / "grid_completion_check.csv", index=False)

    filter_check_cols = [
        "run_id", "raw_rows", "removed_artifact_rows", "kept_attempted_rows",
        "attempted_parts", "correct_parts", "late_parts", "unknown_parts",
        "correct_on_time_parts", "correct_on_time_rate",
        "slice_ul_pct", "slice_dl_pct", "edge_cpus", "resolution", "run_speed",
    ]
    filter_check_cols = [c for c in filter_check_cols if c in df.columns]
    df[filter_check_cols].to_csv(args.out_dir / "artifact_filtering_check.csv", index=False)

    save_top_tables(df, args.out_dir)
    save_controller_tables(df, args.out_dir)

    plot_top_bar(df, args.out_dir, "production_efficiency", "top20_production_efficiency.png")
    plot_top_bar(df, args.out_dir, "cot_per_resource", "top20_correct_on_time_per_resource.png")
    plot_top_bar(df, args.out_dir, "production_goodput", "top20_production_goodput.png")
    plot_top_bar(df, args.out_dir, "correct_on_time_rate", "top20_correct_on_time_rate.png")

    plot_pareto(df, args.out_dir)
    plot_pareto_cot(df, args.out_dir)
    
    plot_codesign_efficiency_contours(df, args.out_dir)
    plot_multispeed_efficiency_contour_lines(df, args.out_dir)
    plot_all_optimum_shift_contours(
        df,
        args.out_dir,
        speed_pairs=[
            (args.shift_speed_from, args.shift_speed_to),
            *DEFAULT_OPTIMUM_SHIFT_SPEED_PAIRS,
        ],
    )

    paper_comparisons = save_paper_policy_tables(df, args.out_dir)
    plot_paper_policy_bars(
        paper_comparisons["production_efficiency"],
        args.out_dir,
        "production_efficiency",
    )
    plot_paper_policy_bars(
        paper_comparisons["production_goodput"],
        args.out_dir,
        "production_goodput",
    )
    plot_paper_efficiency_gain(
        paper_comparisons["production_efficiency"], args.out_dir
    )
    constrained_comparison = save_and_plot_constrained_efficiency(
        df, args.out_dir, args.paper_goodput_fraction
    )
    cot_constrained_comparisons = [
        save_and_plot_cot_constrained_efficiency(df, args.out_dir, min_cot_rate)
        for min_cot_rate in (0.3, 0.5)
    ]
    save_and_plot_controllable_speed_comparison(
        df, args.out_dir, args.paper_goodput_fraction
    )
    plot_paper_pareto_by_speed(df, args.out_dir)

    plot_3d_codesign_efficiency_surfaces(df, args.out_dir)
    plot_3d_codesign_gain_surfaces(df, args.out_dir)
    plot_3d_resource_performance_pareto(df, args.out_dir)
    plot_3d_constrained_policy_comparison(
        constrained_comparison, args.out_dir
    )
    plot_full_efficiency_trajectory(df, args.out_dir)
    plot_coordinate_descent_trajectory(df, args.out_dir)

    for metric in ["production_efficiency", "production_goodput", "correct_on_time_rate", "p95_roundtrip_ms"]:
        if metric in df.columns:
            plot_variable_effects(df, args.out_dir, metric)

    plot_joint_vs_independent_gap(df, args.out_dir)
    dynamic_replay(df, args.out_dir)

    cols = [
        "production_efficiency",
        "production_goodput",
        "correct_on_time_rate",
        "correct_rate",
        "late_rate",
        "unknown_rate",
        "attempted_parts",
        "removed_artifact_rows",
        "resource_cost",
        "slice_ul_pct",
        "edge_cpus",
        "resolution",
        "run_speed",
        "p95_roundtrip_ms",
        "run_id",
    ]
    cols = [c for c in cols if c in df.columns]
    print("\nTop 10 by production efficiency:")
    print(df.sort_values("production_efficiency", ascending=False)[cols].head(10).to_string(index=False))

    print("\n[DONE] Analysis complete.")
    print(f"Output directory: {args.out_dir}")


if __name__ == "__main__":
    main()
