#!/usr/bin/env python3
"""
PRB/slice-based Orbit3C experiment runner.

This runner keeps the existing Isaac/edge/factory/UE-forwarder orchestration
from codesign_experiment_runner_v1.py, but changes the communication control
variable from Linux tc bandwidth shaping to the FlexRIC/OAI static slice xApp.

Important interpretation:
  * slice_ul_pct is the industrial slice PRB budget inside UL scheduling
    opportunities.
  * slice_dl_pct is the industrial slice PRB budget inside DL scheduling
    opportunities.
  * These values do not change the NR TDD pattern. UL and DL slots still come
    from the gNB radio configuration.
"""

import json
import os
import re
import atexit
import csv
import io
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime
from collections import defaultdict

#import automated_experiments.scripts.codesign_experiment_runner_v1_prev_before_cpu as base
import codesign_experiment_runner_v1 as base

SLICE_XAPP = (
    base.FLEXRIC_DIR
    / "build/examples/xApp/c/slice/xapp_orbit3c_static_slice_ctrl"
)

IPERF_PORT = int(os.environ.get("ORBIT3C_BG_IPERF_PORT", "5201"))
ULSCH_GRANT_LOG = Path(
    os.environ.get(
        "ORBIT3C_ULSCH_GRANT_CSV",
        "/home/elahe/user/ORBIT3C_OAI_DEV/logs/ulsch_grants.csv",
    )
)
OAI_LOG_DIR = Path(os.environ.get("ORBIT3C_LOG_DIR", "/home/elahe/user/ORBIT3C_OAI_DEV/logs"))
OAI_UE_LOG = Path(os.environ.get("ORBIT3C_UE_LOG", str(OAI_LOG_DIR / "ue_2ue_dev_rfsim_e2.log")))
OAI_RIC_LOG = Path(os.environ.get("ORBIT3C_RIC_LOG", str(OAI_LOG_DIR / "nearRT-RIC.log")))
CORE_CONTAINERS = [
    name.strip()
    for name in os.environ.get(
        "ORBIT3C_CORE_CONTAINERS",
        "oai-smf-dev,vpp-upf-dev,oai-amf-dev",
    ).split(",")
    if name.strip()
]
RNTI_STABILIZE_SEC = float(os.environ.get("ORBIT3C_RNTI_STABILIZE_SEC", "8"))
RNTI_DISCOVERY_TIMEOUT_SEC = float(os.environ.get("ORBIT3C_RNTI_DISCOVERY_TIMEOUT_SEC", "30"))
STACK_STABILITY_WINDOW_SEC = float(
    os.environ.get("ORBIT3C_STACK_STABILITY_WINDOW_SEC", "30")
)
CORE_MONITOR_ENABLED = os.environ.get("ORBIT3C_CORE_MONITOR", "1").lower() not in {
    "0",
    "false",
    "no",
}
STRICT_SLICE_VALIDATION = os.environ.get("ORBIT3C_STRICT_SLICE_VALIDATION", "1").lower() not in {
    "0",
    "false",
    "no",
}
STABILITY_MONITOR_INTERVAL_SEC = float(
    os.environ.get("ORBIT3C_STABILITY_MONITOR_INTERVAL_SEC", "1.0")
)
STABILITY_MONITOR_ENABLED = os.environ.get("ORBIT3C_STABILITY_MONITOR", "1").lower() not in {
    "0",
    "false",
    "no",
}
AUTO_RECOVERY_ENABLED = os.environ.get("ORBIT3C_AUTO_RECOVERY", "1").lower() not in {
    "0",
    "false",
    "no",
}
START_STACK_ENABLED = os.environ.get("ORBIT3C_START_STACK", "1").lower() not in {
    "0",
    "false",
    "no",
}
STARTUP_RECOVER_CORE = os.environ.get("ORBIT3C_STARTUP_RECOVER_CORE", "1").lower() not in {
    "0",
    "false",
    "no",
}
STARTUP_RECOVERY_ATTEMPTS = int(os.environ.get("ORBIT3C_STARTUP_RECOVERY_ATTEMPTS", "2"))
MAX_RECOVERY_RETRIES_PER_EXPERIMENT = int(
    os.environ.get("ORBIT3C_MAX_RECOVERY_RETRIES_PER_EXPERIMENT", "1")
)
SKIP_COMPLETED_RUNS = os.environ.get("ORBIT3C_SKIP_COMPLETED", "1").lower() not in {
    "0",
    "false",
    "no",
}
SUDO_KEEPALIVE_ENABLED = os.environ.get("ORBIT3C_SUDO_KEEPALIVE", "1").lower() not in {
    "0",
    "false",
    "no",
}
SUDO_KEEPALIVE_INTERVAL_SEC = int(os.environ.get("ORBIT3C_SUDO_KEEPALIVE_INTERVAL_SEC", "30"))
RNTI_TUNNEL_PROBE_ENABLED = os.environ.get("ORBIT3C_RNTI_TUNNEL_PROBE", "1").lower() not in {
    "0",
    "false",
    "no",
}
RNTI_PROBE_PORT = int(os.environ.get("ORBIT3C_RNTI_PROBE_PORT", "5210"))
RNTI_PROBE_DURATION_SEC = int(os.environ.get("ORBIT3C_RNTI_PROBE_DURATION_SEC", "4"))
RNTI_PROBE_TIMEOUT_SEC = int(os.environ.get("ORBIT3C_RNTI_PROBE_TIMEOUT_SEC", "25"))
RNTI_PROBE_MIN_TBS_BYTES = int(os.environ.get("ORBIT3C_RNTI_PROBE_MIN_TBS_BYTES", "1000"))
RNTI_PROBE_DOMINANCE_RATIO = float(os.environ.get("ORBIT3C_RNTI_PROBE_DOMINANCE_RATIO", "1.5"))
RECOVERY_SCRIPT = Path(
    os.environ.get(
        "ORBIT3C_RECOVERY_SCRIPT",
        Path(__file__).with_name("recover_oai_stack.sh"),
    )
)
ROS_PYTHON = os.environ.get("ORBIT3C_ROS_PYTHON", "/usr/bin/python3")

SUDO_KEEPALIVE_PROC = None


RNTI_HEX_RE = re.compile(r"(?:0x)?([0-9a-fA-F]{3,4})")
RNTI_AFTER_LABEL_RE = r"(?:0x)?([0-9a-fA-F]{3,4})"
UNSTABLE_LOG_PATTERNS = [
    (
        re.compile(r"find_map_e2_node_sad|E2 Node not found in the tree", re.I),
        "nearRT-RIC crashed on stale E2-node lookup",
    ),
    (
        re.compile(r"\bSCTP_SHUTDOWN_EVENT\b", re.I),
        "E2 SCTP association shut down",
    ),
    (
        re.compile(r"Aborted \(core dumped\)|assertion `.*' failed", re.I),
        "OAI/FlexRIC process aborted",
    ),
    (
        re.compile(r"malloc\(\):|double linked list corrupted|segmentation fault|segfault", re.I),
        "OAI process memory corruption or segfault",
    ),
    (re.compile(r"max RETX reached", re.I), "RLC reached max retransmissions"),
    (
        re.compile(r"RLC reached max retransmissions", re.I),
        "RRC reported RLC max retransmissions",
    ),
    (re.compile(r"\bRRCReestablishment\b", re.I), "UE RRC re-establishment"),
    (re.compile(r"Reestablishment RNTI", re.I), "gNB RRC re-establishment"),
    (re.compile(r"\bLost socket\b", re.I), "OAI/RFSIM socket was lost"),
    (re.compile(r"\bNo SRS signal\b", re.I), "gNB lost UE SRS"),
    (
        re.compile(r"Invalid timing advance offset", re.I),
        "invalid UE timing advance",
    ),
    (re.compile(r"No such device", re.I), "required tunnel device disappeared"),
    (
        re.compile(r"no_ack_from_ue_forwarder|No ACK received from UE forwarder", re.I),
        "factory did not receive UE-forwarder ACK",
    ),
]
CORE_RECOVERY_PATTERNS = [
    re.compile(r"OAI core unhealthy:", re.I),
    re.compile(r"\boaitun_ue\d+\b.*(?:does not exist|not found|disappeared)", re.I),
    re.compile(r"Cannot read IPv4 address for oaitun_ue\d+", re.I),
    re.compile(r"tunnel preflight:", re.I),
    re.compile(r"\bPDU session\b", re.I),
    re.compile(r"\bSMF\b|\bUPF\b|\bAMF\b", re.I),
]


# =========================================================
# PRB slice experiment plan
# =========================================================
# Diagnostic UL-cap sweep. The useful caps depend on measured high-load PRB
# demand: first run the 100/100 baseline under heavy offered load, inspect KPM
# p50/p95/max UL PRB usage, then keep caps below/near/above that demand.
# SLICE_POLICIES = [
#     {
#         "slice_label": "slice_ul100_dl100",
#         "slice_ul_pct": 100,
#         "slice_dl_pct": 100,
#     },
#     {
#         "slice_label": "slice_ul75_dl100",
#         "slice_ul_pct": 75,
#         "slice_dl_pct": 100,
#     },
#     {
#         "slice_label": "slice_ul50_dl100",
#         "slice_ul_pct": 50,
#         "slice_dl_pct": 100,
#     },
#     {
#         "slice_label": "slice_ul35_dl100",
#         "slice_ul_pct": 35,
#         "slice_dl_pct": 100,
#     },
#     {
#         "slice_label": "slice_ul25_dl100",
#         "slice_ul_pct": 25,
#         "slice_dl_pct": 100,
#     },
#     {
#         "slice_label": "slice_ul15_dl100",
#         "slice_ul_pct": 15,
#         "slice_dl_pct": 100,
#     },
#     {
#         "slice_label": "slice_ul100_dl20",
#         "slice_ul_pct": 100,
#         "slice_dl_pct": 20,
#     },
# ]


# SLICE_POLICIES = [
#     {"slice_label": "slice_ul99_dl99", "slice_ul_pct": 99, "slice_dl_pct": 99},
#     {"slice_label": "slice_ul75_dl99", "slice_ul_pct": 75, "slice_dl_pct": 99},
#     {"slice_label": "slice_ul50_dl99", "slice_ul_pct": 50, "slice_dl_pct": 99},
#     {"slice_label": "slice_ul35_dl99", "slice_ul_pct": 35, "slice_dl_pct": 99},
#     {"slice_label": "slice_ul25_dl99", "slice_ul_pct": 25, "slice_dl_pct": 99},
#     {"slice_label": "slice_ul15_dl99", "slice_ul_pct": 15, "slice_dl_pct": 99},
# ]

# SLICE_POLICIES = [
#     {"slice_label": "slice_ul30_dl99", "slice_ul_pct": 30, "slice_dl_pct": 99},
#     {"slice_label": "slice_ul20_dl99", "slice_ul_pct": 20, "slice_dl_pct": 99},
#     {"slice_label": "slice_ul10_dl99", "slice_ul_pct": 10, "slice_dl_pct": 99},
# ]

# SLICE_POLICIES = [
#     {"slice_label": "slice_ul18_dl99", "slice_ul_pct": 18, "slice_dl_pct": 99},
#     {"slice_label": "slice_ul16_dl99", "slice_ul_pct": 16, "slice_dl_pct": 99},
#     {"slice_label": "slice_ul14_dl99", "slice_ul_pct": 14, "slice_dl_pct": 99},
#     {"slice_label": "slice_ul12_dl99", "slice_ul_pct": 12, "slice_dl_pct": 99},
#     {"slice_label": "slice_ul8_dl99", "slice_ul_pct": 8, "slice_dl_pct": 99},
#     {"slice_label": "slice_ul6_dl99", "slice_ul_pct": 6, "slice_dl_pct": 99},
# ]

# MODELS = ["best_n.pt"]
# RESOLUTIONS = [(1280, 720)]
# SPEEDS = [0.5, 1.0]


# =========================================================
# Stage 1 edge CPU-resource pilot plan for PRB/slice setup
# Keep detector fixed. Vary container CPU allocation, resolution, and speed
# under one slice policy to isolate edge-resource sensitivity.
# =========================================================
# SLICE_POLICIES = [
#     {"slice_label": "slice_ul75_dl50", "slice_ul_pct": 75, "slice_dl_pct": 50},
# ]

# MODELS = [
#     "best_n.pt",
# ]

# RESOLUTIONS = [
#     (1280, 720),
#     (640, 360),
# ]

# SPEEDS = [1.0, 2.0]

# EDGE_RESOURCE_LEVELS = [
#     {"edge_label": "edge_cpu4", "edge_cpus": 4.0, "edge_compute_capacity": 1.0},
#     {"edge_label": "edge_cpu2", "edge_cpus": 2.0, "edge_compute_capacity": 0.5},
#     {"edge_label": "edge_cpu1", "edge_cpus": 1.0, "edge_compute_capacity": 0.25},
#     {"edge_label": "edge_cpu0p5", "edge_cpus": 0.5, "edge_compute_capacity": 0.125},
# ]

# SLICE_POLICIES = [
#     {"slice_label": "slice_ul75_dl50", "slice_ul_pct": 75, "slice_dl_pct": 50},
#     {"slice_label": "slice_ul50_dl50", "slice_ul_pct": 50, "slice_dl_pct": 50},
#     {"slice_label": "slice_ul25_dl50", "slice_ul_pct": 25, "slice_dl_pct": 50},
#     {"slice_label": "slice_ul15_dl50", "slice_ul_pct": 15, "slice_dl_pct": 50},
# ]

# EDGE_RESOURCE_LEVELS = [
#     {"edge_label": "edge_cpu8", "edge_cpus": 8.0, "edge_compute_capacity": 1.0},
#     {"edge_label": "edge_cpu4", "edge_cpus": 4.0, "edge_compute_capacity": 0.5},
#     {"edge_label": "edge_cpu2", "edge_cpus": 2.0, "edge_compute_capacity": 0.25},
#     {"edge_label": "edge_cpu1", "edge_cpus": 1.0, "edge_compute_capacity": 0.125},
# ]

# MODELS = ["best_n.pt"]

# RESOLUTIONS = [
#     (1280, 720),
#     (640, 360),
#     (320, 180),
# ]

# SPEEDS = [0.5, 1.0, 1.5, 2.0]

SLICE_POLICIES = [
    {"slice_label": "slice_ul75_dl50", "slice_ul_pct": 75, "slice_dl_pct": 50},
    {"slice_label": "slice_ul50_dl50", "slice_ul_pct": 50, "slice_dl_pct": 50},
    {"slice_label": "slice_ul25_dl50", "slice_ul_pct": 25, "slice_dl_pct": 50},
    {"slice_label": "slice_ul15_dl50", "slice_ul_pct": 15, "slice_dl_pct": 50},
]

EDGE_RESOURCE_LEVELS = [
    {"edge_label": "edge_cpu8", "edge_cpus": 8.0, "edge_compute_capacity": 1.0},
    {"edge_label": "edge_cpu4", "edge_cpus": 4.0, "edge_compute_capacity": 0.5},
    {"edge_label": "edge_cpu2", "edge_cpus": 2.0, "edge_compute_capacity": 0.25},
    {"edge_label": "edge_cpu1", "edge_cpus": 1.0, "edge_compute_capacity": 0.125},
]

MODELS = ["best_n.pt"]

RESOLUTIONS = [
    (1280, 720),
    (640, 360),
    (320, 180),
]

SPEEDS = [0.5, 1.0, 1.5, 2.0]

# Optional offered background load. With only one UE this creates load but not
# true slice isolation, because the background flow has the same RNTI/slice.
# For true isolation, run a second UE and export:
#   ORBIT3C_BG_RNTI=0x....
#   ORBIT3C_BG_TUN_IFACE=oaitun_ue2
BACKGROUND_IPERF_MBIT = os.environ.get("ORBIT3C_BG_IPERF_MBIT")
BACKGROUND_IPERF_DIRECTION = os.environ.get("ORBIT3C_BG_IPERF_DIRECTION", "ul")
BACKGROUND_TUN_IFACE = os.environ.get("ORBIT3C_BG_TUN_IFACE", base.OAI_TUN_IFACE)
BACKGROUND_DURATION_SEC = int(os.environ.get("ORBIT3C_BG_IPERF_SEC", "300"))

if BACKGROUND_IPERF_MBIT is not None:
    BACKGROUND_IPERF_MBIT = float(BACKGROUND_IPERF_MBIT)


EXPERIMENTS = []
for policy in SLICE_POLICIES:
    for edge_level in EDGE_RESOURCE_LEVELS:
        for model_path in MODELS:
            for width, height in RESOLUTIONS:
                for speed in SPEEDS:
                    EXPERIMENTS.append(
                        {
                            **policy,
                            **edge_level,
                            "model_path": model_path,
                            "bandwidth_mbit": None,
                            "camera_width": width,
                            "camera_height": height,
                            "run_speed": speed,
                            "encoding": "raw",
                            "jpeg_quality": 35,
                            "background_iperf_mbit": BACKGROUND_IPERF_MBIT,
                            "background_iperf_direction": BACKGROUND_IPERF_DIRECTION,
                            "background_tun_iface": BACKGROUND_TUN_IFACE,
                            "edge_resource_control": "docker_update_cpus",
                        }
                    )

base.MODELS = MODELS

print(f"[PRBExperimentPlan] Total experiments: {len(EXPERIMENTS)}")


def sanitize_float(x):
    return str(x).replace(".", "p")


def make_run_id(i, cfg):
    model_name = Path(cfg["model_path"]).stem
    w = cfg["camera_width"]
    h = cfg["camera_height"]
    speed = sanitize_float(cfg["run_speed"])
    enc = cfg.get("encoding", "raw")
    bg = cfg.get("background_iperf_mbit")
    bg_txt = "nobg" if bg is None else f"bg{int(bg)}m_{cfg.get('background_iperf_direction', 'ul')}"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    edge_label = cfg.get("edge_label", "edge_default")
    return (
        f"run_{i:03d}_{ts}_{cfg['slice_label']}_{edge_label}_{bg_txt}_"
        f"{model_name}_{w}x{h}_speed{speed}_{enc}"
    )


def write_current_config(cfg, run_dir):
    config = {
        "run_id": cfg["run_id"],
        "optimizer_trial_id": cfg.get("optimizer_trial_id", ""),
        "run_speed": float(cfg["run_speed"]),
        "camera_width": int(cfg["camera_width"]),
        "camera_height": int(cfg["camera_height"]),
        "render_product_node": "/World/ActionGraph_01/isaac_create_render_product",
        "resource_control": "flexric_static_slice_prb",
        "bandwidth_mbit": None,
        "slice_label": cfg["slice_label"],
        "slice_ul_pct": int(cfg["slice_ul_pct"]),
        "slice_dl_pct": int(cfg["slice_dl_pct"]),
        "background_iperf_mbit": cfg.get("background_iperf_mbit"),
        "background_iperf_direction": cfg.get("background_iperf_direction"),
        "background_tun_iface": cfg.get("background_tun_iface"),
        "ulsch_grant_log": str(ULSCH_GRANT_LOG),
        "model_path": cfg["model_path"],
        "encoding": cfg.get("encoding", "raw"),
        "jpeg_quality": cfg.get("jpeg_quality", 35),
        "run_dir": str(run_dir),
        "edge_label": cfg.get("edge_label", ""),
        "edge_cpus": cfg.get("edge_cpus", ""),
        "edge_compute_capacity": cfg.get("edge_compute_capacity", ""),
        "edge_resource_control": cfg.get("edge_resource_control", "docker_update_cpus"),
    }

    base.CURRENT_CONFIG_PATH.write_text(json.dumps(config, indent=2))
    (run_dir / "run_config.json").write_text(json.dumps(config, indent=2))
    print(f"[Config] Wrote {base.CURRENT_CONFIG_PATH}")
    print(json.dumps(config, indent=2))


def update_run_config(run_dir, updates):
    for path in [base.CURRENT_CONFIG_PATH, run_dir / "run_config.json"]:
        try:
            config = json.loads(path.read_text()) if path.exists() else {}
        except Exception:
            config = {}
        config.update(updates)
        path.write_text(json.dumps(config, indent=2))


def run_stack_recovery(run_id, reason, attempt):
    if not AUTO_RECOVERY_ENABLED:
        print("[Recovery] Automatic recovery disabled by ORBIT3C_AUTO_RECOVERY=0")
        return True

    if not RECOVERY_SCRIPT.exists():
        raise FileNotFoundError(f"Recovery script not found: {RECOVERY_SCRIPT}")

    env = os.environ.copy()
    env["ORBIT3C_RECOVERY_RUN_ID"] = run_id
    env["ORBIT3C_RECOVERY_REASON"] = reason
    env["ORBIT3C_RECOVERY_ATTEMPT"] = str(attempt)
    if should_recover_core(reason):
        env["ORBIT3C_RECOVER_CORE"] = "1"

    ensure_sudo_cache_available("before recovery")
    print(
        "[Recovery] Running OAI stack recovery "
        f"attempt {attempt}: {RECOVERY_SCRIPT}"
    )
    result = subprocess.run(
        [str(RECOVERY_SCRIPT)],
        cwd=base.BASE_DIR,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        print(
            "[Recovery WARN] OAI stack recovery script returned "
            f"{result.returncode}; the next retry will re-check preconditions."
        )
        return False
    return True


def should_recover_core(reason):
    text = str(reason)
    return any(pattern.search(text) for pattern in CORE_RECOVERY_PATTERNS)


def stop_sudo_keepalive():
    global SUDO_KEEPALIVE_PROC
    if SUDO_KEEPALIVE_PROC is None:
        return
    if SUDO_KEEPALIVE_PROC.poll() is None:
        SUDO_KEEPALIVE_PROC.terminate()
        try:
            SUDO_KEEPALIVE_PROC.wait(timeout=5)
        except subprocess.TimeoutExpired:
            SUDO_KEEPALIVE_PROC.kill()
            SUDO_KEEPALIVE_PROC.wait(timeout=5)
    SUDO_KEEPALIVE_PROC = None


def ensure_sudo_cache_available(context):
    if not SUDO_KEEPALIVE_ENABLED:
        return

    if SUDO_KEEPALIVE_PROC is not None and SUDO_KEEPALIVE_PROC.poll() is not None:
        raise RuntimeError(
            "sudo keepalive exited before it could refresh the timestamp "
            f"({context}). Run `sudo -v` again and restart the runner."
        )

    result = subprocess.run(
        ["sudo", "-n", "-v"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "sudo cache is not available "
            f"({context}). Run `sudo -v` again and restart the runner.\n"
            f"stderr={result.stderr.strip()}"
        )


def start_sudo_keepalive():
    global SUDO_KEEPALIVE_PROC
    if not SUDO_KEEPALIVE_ENABLED:
        print("[Sudo] Keepalive disabled by ORBIT3C_SUDO_KEEPALIVE=0")
        return

    result = subprocess.run(
        ["sudo", "-n", "true"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "sudo cache is not available. Run `sudo -v` in this terminal before "
            "starting the experiment runner.\n"
            f"stderr={result.stderr.strip()}"
        )

    script = (
        f"while true; do "
        f"sudo -n -v >/dev/null 2>&1 || exit 1; "
        f"sleep {max(10, SUDO_KEEPALIVE_INTERVAL_SEC)}; "
        f"done"
    )
    SUDO_KEEPALIVE_PROC = subprocess.Popen(
        ["bash", "-lc", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    atexit.register(stop_sudo_keepalive)
    print(
        "[Sudo] Keepalive started "
        f"(refresh every {max(10, SUDO_KEEPALIVE_INTERVAL_SEC)}s)."
    )


def configs_match_completed_run(cfg, run_config):
    keys = [
        "slice_label",
        "slice_ul_pct",
        "slice_dl_pct",
        "model_path",
        "bandwidth_mbit",
        "camera_width",
        "camera_height",
        "run_speed",
        "encoding",
        "jpeg_quality",
        "background_iperf_mbit",
        "background_iperf_direction",
        "background_tun_iface",
        "edge_label",
        "edge_cpus",
        "edge_compute_capacity",
    ]
    for key in keys:
        if cfg.get(key) != run_config.get(key):
            return False
    return True


def find_completed_run_for_config(cfg):
    if not SKIP_COMPLETED_RUNS or not base.RESULTS_DIR.exists():
        return None

    for run_config_path in sorted(base.RESULTS_DIR.glob("run_*/run_config.json")):
        try:
            run_config = json.loads(run_config_path.read_text())
        except Exception:
            continue
        if run_config.get("status") != "completed":
            continue
        if run_config.get("exclude_from_analysis", False):
            continue
        if configs_match_completed_run(cfg, run_config):
            return run_config_path.parent
    return None


def ensure_edge_container_running():
    out = subprocess.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{.State.Running}}",
            base.EDGE_CONTAINER_NAME,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(
            f"Docker container {base.EDGE_CONTAINER_NAME!r} was not found or "
            f"cannot be inspected.\nstdout={out.stdout}\nstderr={out.stderr}"
        )

    if out.stdout.strip().lower() == "true":
        print(f"[Startup] Docker container {base.EDGE_CONTAINER_NAME} is already running.")
        return

    print(f"[Startup] Starting Docker container {base.EDGE_CONTAINER_NAME}.")
    started = subprocess.run(
        ["docker", "start", base.EDGE_CONTAINER_NAME],
        text=True,
        capture_output=True,
        check=False,
    )
    if started.returncode != 0:
        raise RuntimeError(
            f"Failed to start Docker container {base.EDGE_CONTAINER_NAME}.\n"
            f"stdout={started.stdout}\nstderr={started.stderr}"
        )


def start_non_isaac_stack():
    if not START_STACK_ENABLED:
        print("[Startup] Automatic non-Isaac stack startup disabled by ORBIT3C_START_STACK=0")
        return

    if not RECOVERY_SCRIPT.exists():
        raise FileNotFoundError(f"Startup/recovery script not found: {RECOVERY_SCRIPT}")

    ensure_edge_container_running()

    max_attempts = max(1, STARTUP_RECOVERY_ATTEMPTS)
    for attempt in range(1, max_attempts + 1):
        env = os.environ.copy()
        env["ORBIT3C_RECOVERY_RUN_ID"] = "startup"
        env["ORBIT3C_RECOVERY_REASON"] = "startup before PRB/slice experiment sweep"
        env["ORBIT3C_RECOVERY_ATTEMPT"] = str(attempt)
        if STARTUP_RECOVER_CORE:
            env["ORBIT3C_RECOVER_CORE"] = "1"

        print(
            "[Startup] Starting/restarting OAI core, RIC, gNB, and two-UE RFsim "
            f"stack (attempt {attempt}/{max_attempts})."
        )
        ensure_sudo_cache_available("before startup recovery")
        result = subprocess.run(
            [str(RECOVERY_SCRIPT)],
            cwd=base.BASE_DIR,
            env=env,
            check=False,
        )
        if result.returncode == 0:
            return

        if attempt < max_attempts:
            print(
                "[Startup WARN] Automatic non-Isaac stack startup failed "
                f"with return code {result.returncode}; retrying full recovery."
            )
            time.sleep(10)

    raise RuntimeError(
        "Automatic non-Isaac stack startup failed after "
        f"{max_attempts} attempt(s). {RECOVERY_SCRIPT} returned {result.returncode}."
    )


def reset_tc_to_no_limit(run_dir):
    print("[TC] Ensuring no Linux tc bandwidth limit is active")
    subprocess.run(
        ["sudo", "-n", "tc", "qdisc", "del", "dev", base.OAI_TUN_IFACE, "root"],
        check=False,
    )
    base.save_tc_state(run_dir)


def get_iface_ipv4(iface):
    out = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "dev", iface],
        text=True,
        capture_output=True,
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(f"Cannot read IPv4 address for {iface}: {out.stderr}")

    match = re.search(r"\binet\s+([0-9.]+)/", out.stdout)
    if not match:
        raise RuntimeError(f"No IPv4 address found on {iface}: {out.stdout}")
    return match.group(1)


def verify_required_tunnels_for_run(cfg):
    required_ifaces = [base.OAI_TUN_IFACE]

    if cfg.get("background_iperf_mbit") is not None:
        bg_iface = cfg.get("background_tun_iface") or base.OAI_TUN_IFACE
        required_ifaces.append(bg_iface)

    seen = set()
    for iface in required_ifaces:
        if iface in seen:
            continue
        seen.add(iface)
        ip_addr = get_iface_ipv4(iface)
        print(f"[Check] Interface {iface} exists with IPv4 {ip_addr}.")


def parse_rnti(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = RNTI_HEX_RE.search(text)
    if not match:
        return None
    return int(match.group(1), 16)


def format_rnti(rnti):
    return f"0x{int(rnti):04x}"


def read_text_if_exists(path):
    try:
        return path.read_text(errors="replace") if path.exists() else ""
    except Exception:
        return ""


def get_file_size(path):
    try:
        return path.stat().st_size if path.exists() else 0
    except Exception:
        return 0


def run_capture(cmd, timeout=20):
    try:
        out = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
        return {
            "cmd": cmd,
            "returncode": out.returncode,
            "stdout": out.stdout,
            "stderr": out.stderr,
        }
    except Exception as exc:
        return {
            "cmd": cmd,
            "returncode": None,
            "stdout": "",
            "stderr": repr(exc),
        }


def docker_inspect_container(name):
    out = run_capture(["docker", "inspect", name], timeout=10)
    if out["returncode"] != 0:
        return None, out
    try:
        data = json.loads(out["stdout"])
    except Exception as exc:
        out["stderr"] = f"{out['stderr']}\nJSON parse failed: {exc}"
        return None, out
    if not data:
        return None, out
    return data[0], out


def classify_core_health():
    if not CORE_MONITOR_ENABLED:
        return None

    problems = []
    for name in CORE_CONTAINERS:
        info, out = docker_inspect_container(name)
        if info is None:
            problems.append(f"{name}: docker inspect failed: {out['stderr'].strip()}")
            continue

        state = info.get("State", {})
        running = bool(state.get("Running", False))
        oom_killed = bool(state.get("OOMKilled", False))
        exit_code = state.get("ExitCode")
        error = state.get("Error") or ""
        health = state.get("Health", {}).get("Status", "")

        if oom_killed:
            problems.append(f"{name}: OOMKilled=true exit_code={exit_code}")
        elif not running:
            problems.append(
                f"{name}: not running exit_code={exit_code} error={error!r} "
                f"finished={state.get('FinishedAt', '')}"
            )
        elif health and health != "healthy":
            problems.append(f"{name}: health={health}")

    if problems:
        return "OAI core unhealthy: " + "; ".join(problems)
    return None


def collect_core_diagnostics(run_dir, reason):
    diag_dir = run_dir / "oai_core_diagnostics"
    diag_dir.mkdir(parents=True, exist_ok=True)

    commands = {
        "free_h.txt": ["free", "-h"],
        "docker_ps.txt": ["docker", "ps", "-a", "--format", "table {{.Names}}\t{{.Status}}\t{{.RunningFor}}"],
        "docker_stats.txt": [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}",
        ],
        "kernel_recent.txt": ["journalctl", "-k", "--since", "30 minutes ago"],
    }

    summary = {
        "reason": reason,
        "created_time": datetime.now().isoformat(timespec="seconds"),
        "core_containers": CORE_CONTAINERS,
        "commands": {},
        "containers": {},
    }

    for filename, cmd in commands.items():
        out = run_capture(cmd, timeout=20)
        summary["commands"][filename] = {
            "cmd": out["cmd"],
            "returncode": out["returncode"],
            "stderr": out["stderr"],
        }
        (diag_dir / filename).write_text(
            f"$ {' '.join(cmd)}\n"
            f"returncode={out['returncode']}\n"
            f"stderr:\n{out['stderr']}\n"
            f"stdout:\n{out['stdout']}\n",
            errors="replace",
        )

    for name in CORE_CONTAINERS:
        info, inspect_out = docker_inspect_container(name)
        summary["containers"][name] = {
            "inspect_returncode": inspect_out["returncode"],
            "inspect_stderr": inspect_out["stderr"],
            "state": (info or {}).get("State", {}),
        }
        (diag_dir / f"{name}_inspect.json").write_text(
            json.dumps(info or {}, indent=2),
            errors="replace",
        )

        logs = run_capture(["docker", "logs", "--tail=400", name], timeout=20)
        (diag_dir / f"{name}_docker_logs_tail.txt").write_text(
            f"$ docker logs --tail=400 {name}\n"
            f"returncode={logs['returncode']}\n"
            f"stderr:\n{logs['stderr']}\n"
            f"stdout:\n{logs['stdout']}\n",
            errors="replace",
        )

    (diag_dir / "summary.json").write_text(json.dumps(summary, indent=2), errors="replace")
    print(f"[Diagnostics] Saved OAI core diagnostics to {diag_dir}")


def read_text_from_offset(path, offset):
    try:
        if not path.exists():
            return ""
        current_size = path.stat().st_size
        if current_size < offset:
            offset = 0
        with path.open("rb") as f:
            f.seek(offset)
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def request_isaac_cancel(run_id, run_dir, reason):
    command = {
        "run_id": run_id,
        "command": "cancel",
        "run_dir": str(run_dir),
        "reason": reason,
        "created_time": time.time(),
    }
    base.ISAAC_COMMAND_PATH.write_text(json.dumps(command, indent=2))
    print(f"[Isaac] Requested cancel for run_id={run_id}: {reason}")


def find_instability_reason(text, source):
    for pattern, reason in UNSTABLE_LOG_PATTERNS:
        match = pattern.search(text)
        if match:
            excerpt = text[max(0, match.start() - 120) : match.end() + 120]
            excerpt = " ".join(excerpt.split())
            return f"{source}: {reason}: {excerpt}"
    return None


def check_required_tunnels_still_up(cfg):
    required_ifaces = [base.OAI_TUN_IFACE]
    if cfg.get("background_iperf_mbit") is not None:
        required_ifaces.append(cfg.get("background_tun_iface") or base.OAI_TUN_IFACE)

    checked = set()
    for iface in required_ifaces:
        if iface in checked:
            continue
        checked.add(iface)
        get_iface_ipv4(iface)


def detect_run_instability(cfg, monitor_offsets, local_log_paths):
    if not STABILITY_MONITOR_ENABLED:
        return None

    core_reason = classify_core_health()
    if core_reason:
        return core_reason

    try:
        check_required_tunnels_still_up(cfg)
    except Exception as e:
        return f"tunnel preflight: {e}"

    watched_logs = [
        ("gNB log", base.GNB_LOG),
        ("two-UE log", OAI_UE_LOG),
        ("nearRT-RIC log", OAI_RIC_LOG),
    ]
    watched_logs.extend((label, path) for label, path in local_log_paths)

    for label, path in watched_logs:
        path = Path(path)
        offset = monitor_offsets.get(path, 0)
        text = read_text_from_offset(path, offset)
        monitor_offsets[path] = get_file_size(path)
        if not text:
            continue
        reason = find_instability_reason(text, label)
        if reason:
            return reason

    return None


def wait_for_clean_stack_window(cfg, monitor_offsets, window_sec):
    if not STABILITY_MONITOR_ENABLED or window_sec <= 0:
        return

    print(f"[Stability] Waiting {window_sec:.1f}s for clean OAI/RIC/tunnel state")
    start = time.time()
    while time.time() - start < window_sec:
        reason = detect_run_instability(cfg, monitor_offsets, [])
        if reason:
            raise RuntimeError(
                "Aborting unstable run during pre-measurement stability gate: "
                f"{reason}"
            )
        time.sleep(min(STABILITY_MONITOR_INTERVAL_SEC, 1.0))
    print("[Stability] Clean pre-measurement window completed")


def is_instability_failure(reason):
    text = str(reason)
    return (
        "Aborting unstable run" in text
        or "tunnel preflight:" in text
        or "OAI core unhealthy:" in text
        or any(label in text for _, label in UNSTABLE_LOG_PATTERNS)
    )


def wait_for_isaac_done_with_stability_monitor(
    cfg,
    run_dir,
    monitor_offsets,
    local_log_paths,
    timeout_sec=1800,
):
    run_id = cfg["run_id"]
    print(f"[Isaac] Waiting for Isaac to finish run_id={run_id}")

    start = time.time()
    last_monitor = 0.0
    while time.time() - start < timeout_sec:
        now = time.time()
        if now - last_monitor >= STABILITY_MONITOR_INTERVAL_SEC:
            last_monitor = now
            reason = detect_run_instability(cfg, monitor_offsets, local_log_paths)
            if reason:
                update_run_config(
                    run_dir,
                    {
                        "abort_reason": reason,
                        "abort_time": datetime.now().isoformat(timespec="seconds"),
                    },
                )
                request_isaac_cancel(run_id, run_dir, reason)
                raise RuntimeError(f"Aborting unstable run {run_id}: {reason}")

        if base.ISAAC_STATUS_PATH.exists():
            try:
                status = json.loads(base.ISAAC_STATUS_PATH.read_text())
            except Exception:
                time.sleep(1)
                continue

            if status.get("run_id") != run_id:
                time.sleep(1)
                continue

            if status.get("status") == "done":
                print(f"[Isaac] Done run_id={run_id}")
                return

            if status.get("status") == "error":
                raise RuntimeError(
                    f"Isaac reported error for run_id={run_id}: "
                    f"{status.get('message', '')}"
                )

        time.sleep(1)

    raise TimeoutError(f"Timed out waiting for Isaac run_id={run_id}")


def parse_gnb_active_rntis(log_text, seed_by_role=None):
    """
    Best-effort extraction of active RNTIs from OAI gNB logs.

    Prefer explicit CU-UE-ID/RNTI lines when present. For re-establishment logs,
    carry over an existing role assignment from the old C-RNTI to the new RNTI.
    """

    seed_by_role = seed_by_role or {}
    role_by_rnti = {
        int(rnti): role
        for role, rnti in seed_by_role.items()
        if rnti is not None
    }
    rnti_by_cu = {}
    active_rntis = set(role_by_rnti)
    removed_rntis = set()

    cu_patterns = [
        re.compile(
            r"(?:CU-UE-ID|cu_ue_id|cu ue id|ue id)\D*(\d+).*?"
            r"(?:RNTI|rnti)[^0-9a-fA-FxX]*" + RNTI_AFTER_LABEL_RE,
            re.I,
        ),
        re.compile(
            r"(?:RNTI|rnti)[^0-9a-fA-FxX]*" + RNTI_AFTER_LABEL_RE + r".*?"
            r"(?:CU-UE-ID|cu_ue_id|cu ue id|ue id)\D*(\d+)",
            re.I,
        ),
    ]
    reestab_re = re.compile(
        r"Reestablishment\s+RNTI\s+(?:0x)?([0-9a-fA-F]{3,4})\s+req\s+C-RNTI\s+(?:0x)?([0-9a-fA-F]{3,4})",
        re.I,
    )
    remove_re = re.compile(r"Remove\s+NR\s+rnti\s+(?:0x)?([0-9a-fA-F]{3,4})", re.I)
    add_re = re.compile(r"Adding\s+new\s+UE\s+context\s+with\s+RNTI\s+(?:0x)?([0-9a-fA-F]{3,4})", re.I)

    for line in log_text.splitlines():
        stripped = line.strip()

        for idx, pattern in enumerate(cu_patterns):
            match = pattern.search(stripped)
            if not match:
                continue
            if idx == 0:
                cu_id = int(match.group(1))
                rnti = int(match.group(2), 16)
            else:
                rnti = int(match.group(1), 16)
                cu_id = int(match.group(2))
            rnti_by_cu[cu_id] = rnti
            active_rntis.add(rnti)

        match = reestab_re.search(stripped)
        if match:
            new_rnti = int(match.group(1), 16)
            old_rnti = int(match.group(2), 16)
            role = role_by_rnti.pop(old_rnti, None)
            if role:
                role_by_rnti[new_rnti] = role
            for cu_id, current in list(rnti_by_cu.items()):
                if current == old_rnti:
                    rnti_by_cu[cu_id] = new_rnti
            active_rntis.discard(old_rnti)
            removed_rntis.add(old_rnti)
            active_rntis.add(new_rnti)

        match = remove_re.search(stripped)
        if match:
            removed_rnti = int(match.group(1), 16)
            active_rntis.discard(removed_rnti)
            removed_rntis.add(removed_rnti)
            role_by_rnti.pop(removed_rnti, None)

        match = add_re.search(stripped)
        if match:
            active_rntis.add(int(match.group(1), 16))

    return {
        "by_cu_id": rnti_by_cu,
        "by_role": {role: rnti for rnti, role in role_by_rnti.items()},
        "active": active_rntis,
        "removed": removed_rntis,
    }


def discover_current_rntis(run_dir, gnb_log_offset=0):
    manual_ind = parse_rnti(os.environ.get("ORBIT3C_SLICE_RNTI"))
    manual_bg = parse_rnti(os.environ.get("ORBIT3C_BG_RNTI"))
    seed_by_role = {
        "industrial": manual_ind,
        "background": manual_bg,
    }

    deadline = time.time() + RNTI_DISCOVERY_TIMEOUT_SEC
    last = None

    while time.time() < deadline:
        log_text = read_text_from_offset(base.GNB_LOG, gnb_log_offset)
        parsed = parse_gnb_active_rntis(log_text, seed_by_role=seed_by_role)
        last = parsed

        by_cu_id = parsed["by_cu_id"]
        by_role = parsed["by_role"]

        industrial = by_cu_id.get(1) or by_role.get("industrial")
        background = by_cu_id.get(2) or by_role.get("background")
        if industrial is None and manual_ind not in parsed["removed"]:
            industrial = manual_ind
        if background is None and manual_bg not in parsed["removed"]:
            background = manual_bg

        if industrial and background and industrial != background:
            discovered = {
                "industrial": industrial,
                "background": background,
                "source": "gnb_cu_ue_id" if 1 in by_cu_id and 2 in by_cu_id else "gnb_reestablishment_or_manual_seed",
                "active": sorted(parsed["active"]),
                "by_cu_id": by_cu_id,
            }
            snapshot = {
                "source": discovered["source"],
                "industrial_rnti": format_rnti(industrial),
                "background_rnti": format_rnti(background),
                "active_rntis": [format_rnti(r) for r in discovered["active"]],
                "cu_ue_id_to_rnti": {
                    str(k): format_rnti(v)
                    for k, v in sorted(by_cu_id.items())
                },
            }
            (run_dir / "discovered_rntis.json").write_text(json.dumps(snapshot, indent=2))
            print(
                "[RNTI] Active mapping selected: "
                f"industrial={format_rnti(industrial)} background={format_rnti(background)} "
                f"source={discovered['source']}"
            )
            return discovered

        time.sleep(1)

    details = {
        "manual_industrial_rnti": format_rnti(manual_ind) if manual_ind else "",
        "manual_background_rnti": format_rnti(manual_bg) if manual_bg else "",
        "last_active_rntis": [format_rnti(r) for r in sorted(last["active"])] if last else [],
        "last_removed_rntis": [format_rnti(r) for r in sorted(last["removed"])] if last else [],
        "last_cu_ue_id_to_rnti": {
            str(k): format_rnti(v)
            for k, v in sorted((last or {}).get("by_cu_id", {}).items())
        },
    }
    (run_dir / "discovered_rntis_failed.json").write_text(json.dumps(details, indent=2))
    raise RuntimeError(
        "Could not discover two distinct active RNTIs after background traffic start. "
        f"Details written to {run_dir / 'discovered_rntis_failed.json'}"
    )


def discover_single_industrial_rnti(run_dir, gnb_log_offset=0):
    manual_ind = parse_rnti(os.environ.get("ORBIT3C_SLICE_RNTI"))
    deadline = time.time() + RNTI_DISCOVERY_TIMEOUT_SEC
    last = None

    while time.time() < deadline:
        log_text = read_text_from_offset(base.GNB_LOG, gnb_log_offset)
        parsed = parse_gnb_active_rntis(
            log_text, seed_by_role={"industrial": manual_ind, "background": None}
        )
        last = parsed

        industrial = parsed["by_cu_id"].get(1) or parsed["by_role"].get("industrial")
        if industrial is None and manual_ind not in parsed["removed"]:
            industrial = manual_ind
        if industrial is None and parsed["active"]:
            industrial = sorted(parsed["active"])[0]

        if industrial:
            discovered = {
                "industrial": industrial,
                # The slice xApp interface expects a background RNTI. In no-bg
                # smoke mode there is no background flow, so keep this equal to
                # the industrial RNTI and skip strict two-slice validation.
                "background": industrial,
                "source": "single_rnti_no_background",
                "active": sorted(parsed["active"]),
                "by_cu_id": parsed["by_cu_id"],
            }
            snapshot = {
                "source": discovered["source"],
                "industrial_rnti": format_rnti(industrial),
                "background_rnti": format_rnti(industrial),
                "active_rntis": [format_rnti(r) for r in discovered["active"]],
                "cu_ue_id_to_rnti": {
                    str(k): format_rnti(v)
                    for k, v in sorted(parsed["by_cu_id"].items())
                },
                "note": "No background traffic configured; background RNTI is not used.",
            }
            (run_dir / "discovered_rntis.json").write_text(json.dumps(snapshot, indent=2))
            print(
                "[RNTI] No-background mapping selected: "
                f"industrial={format_rnti(industrial)} source={discovered['source']}"
            )
            return discovered

        time.sleep(1)

    details = {
        "manual_industrial_rnti": format_rnti(manual_ind) if manual_ind else "",
        "last_active_rntis": [format_rnti(r) for r in sorted(last["active"])] if last else [],
        "last_removed_rntis": [format_rnti(r) for r in sorted(last["removed"])] if last else [],
        "last_cu_ue_id_to_rnti": {
            str(k): format_rnti(v)
            for k, v in sorted((last or {}).get("by_cu_id", {}).items())
        },
    }
    (run_dir / "discovered_rntis_failed.json").write_text(json.dumps(details, indent=2))
    raise RuntimeError(
        "Could not discover an active industrial RNTI for no-background smoke run. "
        f"Details written to {run_dir / 'discovered_rntis_failed.json'}"
    )


def parse_ulsch_grant_rows(text):
    text = text.strip()
    if not text:
        return []
    header = (
        "host_time_us,frame,slot,sched_frame,sched_slot,rnti,slice_id,"
        "rbStart,rbSize,startSymbolIndex,nrOfSymbols,num_dmrs_symb,"
        "N_PRB_DMRS,mcs_table,mcs,Qm,R,nrOfLayers,TBS_bytes,harq_round\n"
    )
    reader = csv.DictReader(io.StringIO(header + text))
    rows = []
    for row in reader:
        if not row.get("rnti"):
            continue
        try:
            rnti = parse_rnti(row["rnti"])
            tbs_bytes = int(float(row.get("TBS_bytes", 0) or 0))
            harq_round = int(float(row.get("harq_round", 0) or 0))
        except Exception:
            continue
        rows.append(
            {
                "rnti": rnti,
                "tbs_bytes": tbs_bytes,
                "harq_round": harq_round,
            }
        )
    return rows


def summarize_probe_grants(text):
    totals = defaultdict(int)
    newtx_totals = defaultdict(int)
    grants = defaultdict(int)

    for row in parse_ulsch_grant_rows(text):
        rnti = row["rnti"]
        tbs_bytes = row["tbs_bytes"]
        totals[rnti] += tbs_bytes
        grants[rnti] += 1
        if row["harq_round"] == 0:
            newtx_totals[rnti] += tbs_bytes

    scores = newtx_totals if newtx_totals else totals
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return {
        "totals": dict(totals),
        "newtx_totals": dict(newtx_totals),
        "grants": dict(grants),
        "ranked": ranked,
    }


def cleanup_edge_probe_port(port):
    cleanup_cmd = (
        f"if command -v fuser >/dev/null 2>&1; then fuser -k {port}/tcp || true; fi; "
        "if command -v ss >/dev/null 2>&1; then "
        f"for pid in $(ss -ltnp 2>/dev/null | awk '$4 ~ /:{port}$/ {{print $NF}}' "
        "| sed -n 's/.*pid=\\([0-9][0-9]*\\).*/\\1/p' | sort -u); do "
        "kill -TERM \"$pid\" 2>/dev/null || true; "
        "done; "
        "fi"
    )
    subprocess.run(
        ["docker", "exec", base.EDGE_CONTAINER_NAME, "sh", "-lc", cleanup_cmd],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_edge_probe_server(port, timeout_sec=5.0):
    deadline = time.time() + timeout_sec
    check_cmd = (
        "if command -v ss >/dev/null 2>&1; then "
        f"ss -ltn | grep -q ':{port} '; "
        "else "
        f"python3 - <<'PY'\n"
        "import socket\n"
        "s=socket.socket()\n"
        "s.settimeout(0.5)\n"
        f"s.connect(('127.0.0.1', {port}))\n"
        "s.close()\n"
        "PY\n"
        "fi"
    )
    while time.time() < deadline:
        result = subprocess.run(
            ["docker", "exec", base.EDGE_CONTAINER_NAME, "sh", "-lc", check_cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            return True
        time.sleep(0.2)
    return False


def probe_tunnel_rnti(iface, run_dir, port):
    bind_ip = get_iface_ipv4(iface)
    server_log = f"/tmp/orbit3c_rnti_probe_{port}_{iface}.log"
    cleanup_edge_probe_port(port)

    server_cmd = f"iperf3 -s -1 -p '{port}' >'{server_log}' 2>&1"
    subprocess.run(
        ["docker", "exec", "-d", base.EDGE_CONTAINER_NAME, "sh", "-lc", server_cmd],
        check=False,
    )
    if not wait_for_edge_probe_server(port):
        server_out = run_capture(
            ["docker", "exec", base.EDGE_CONTAINER_NAME, "sh", "-lc", f"cat '{server_log}' 2>/dev/null || true"],
            timeout=10,
        )
        raise RuntimeError(
            f"RNTI probe server did not start on edge port {port}. "
            f"Server log:\n{server_out['stdout']}"
        )

    grant_offset = get_file_size(ULSCH_GRANT_LOG)
    client_cmd = [
        "timeout",
        str(RNTI_PROBE_TIMEOUT_SEC),
        "iperf3",
        "-c",
        base.EDGE_CONTAINER_IP,
        "-p",
        str(port),
        "-B",
        bind_ip,
        "--bind-dev",
        iface,
        "-t",
        str(RNTI_PROBE_DURATION_SEC),
        "-O",
        "1",
    ]
    print(
        f"[RNTIProbe] Probing {iface} ({bind_ip}) with TCP iperf "
        f"for {RNTI_PROBE_DURATION_SEC}s"
    )
    result = subprocess.run(
        client_cmd,
        text=True,
        capture_output=True,
        check=False,
    )
    time.sleep(0.5)

    server_out = run_capture(
        ["docker", "exec", base.EDGE_CONTAINER_NAME, "sh", "-lc", f"cat '{server_log}' 2>/dev/null || true"],
        timeout=10,
    )
    grant_text = read_text_from_offset(ULSCH_GRANT_LOG, grant_offset)
    summary = summarize_probe_grants(grant_text)
    ranked = summary["ranked"]

    probe_snapshot = {
        "iface": iface,
        "bind_ip": bind_ip,
        "client_cmd": client_cmd,
        "client_returncode": result.returncode,
        "client_stdout": result.stdout,
        "client_stderr": result.stderr,
        "server_stdout": server_out["stdout"],
        "grant_offset": grant_offset,
        "grant_bytes_read": len(grant_text.encode("utf-8", errors="replace")),
        "totals_by_rnti": {
            format_rnti(rnti): value for rnti, value in sorted(summary["totals"].items())
        },
        "newtx_totals_by_rnti": {
            format_rnti(rnti): value for rnti, value in sorted(summary["newtx_totals"].items())
        },
        "grants_by_rnti": {
            format_rnti(rnti): value for rnti, value in sorted(summary["grants"].items())
        },
    }
    (run_dir / f"rnti_probe_{iface}.json").write_text(
        json.dumps(probe_snapshot, indent=2),
        errors="replace",
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"RNTI probe failed for {iface}. Check {run_dir / f'rnti_probe_{iface}.json'}"
        )

    if not ranked:
        raise RuntimeError(
            f"RNTI probe for {iface} produced no ULSCH grants. "
            f"Check {run_dir / f'rnti_probe_{iface}.json'}"
        )

    best_rnti, best_bytes = ranked[0]
    second_bytes = ranked[1][1] if len(ranked) > 1 else 0
    if best_bytes < RNTI_PROBE_MIN_TBS_BYTES:
        raise RuntimeError(
            f"RNTI probe for {iface} is too weak: best={best_bytes} bytes "
            f"minimum={RNTI_PROBE_MIN_TBS_BYTES}. "
            f"Check {run_dir / f'rnti_probe_{iface}.json'}"
        )
    if second_bytes > 0 and best_bytes < second_bytes * RNTI_PROBE_DOMINANCE_RATIO:
        raise RuntimeError(
            f"RNTI probe for {iface} is ambiguous: best={best_bytes} bytes "
            f"second={second_bytes} bytes ratio_required={RNTI_PROBE_DOMINANCE_RATIO}. "
            f"Check {run_dir / f'rnti_probe_{iface}.json'}"
        )

    print(
        f"[RNTIProbe] {iface} -> {format_rnti(best_rnti)} "
        f"({best_bytes} new TX bytes)"
    )
    return best_rnti, probe_snapshot


def discover_rntis_by_tunnel_probe(cfg, run_dir):
    industrial_iface = base.OAI_TUN_IFACE
    background_iface = cfg.get("background_tun_iface") or base.OAI_TUN_IFACE

    if industrial_iface == background_iface:
        raise RuntimeError(
            "Tunnel RNTI probe requires distinct industrial/background interfaces. "
            f"Both are {industrial_iface}."
        )

    industrial_rnti, industrial_probe = probe_tunnel_rnti(
        industrial_iface,
        run_dir,
        RNTI_PROBE_PORT,
    )
    background_rnti, background_probe = probe_tunnel_rnti(
        background_iface,
        run_dir,
        RNTI_PROBE_PORT + 1,
    )

    if industrial_rnti == background_rnti:
        raise RuntimeError(
            "Tunnel RNTI probe mapped both interfaces to the same RNTI "
            f"{format_rnti(industrial_rnti)}. Do not run; tunnel/UE assignment is ambiguous."
        )

    discovered = {
        "industrial": industrial_rnti,
        "background": background_rnti,
        "source": "tunnel_ulsch_probe",
        "active": sorted({industrial_rnti, background_rnti}),
        "by_iface": {
            industrial_iface: industrial_rnti,
            background_iface: background_rnti,
        },
    }
    snapshot = {
        "source": discovered["source"],
        "industrial_iface": industrial_iface,
        "background_iface": background_iface,
        "industrial_rnti": format_rnti(industrial_rnti),
        "background_rnti": format_rnti(background_rnti),
        "iface_to_rnti": {
            iface: format_rnti(rnti)
            for iface, rnti in discovered["by_iface"].items()
        },
        "industrial_probe": industrial_probe,
        "background_probe": background_probe,
    }
    (run_dir / "discovered_rntis.json").write_text(json.dumps(snapshot, indent=2))
    print(
        "[RNTI] Tunnel probe selected: "
        f"{industrial_iface}->{format_rnti(industrial_rnti)} as industrial, "
        f"{background_iface}->{format_rnti(background_rnti)} as background"
    )
    return discovered


def cleanup_background_iperf_server():
    if BACKGROUND_IPERF_MBIT is None:
        return

    print(f"[Cleanup] Killing old background iperf3 server on port {IPERF_PORT} inside {base.EDGE_CONTAINER_NAME}...")
    subprocess.run(
        [
            "docker",
            "exec",
            base.EDGE_CONTAINER_NAME,
            "bash",
            "-lc",
            "pidof iperf3 >/dev/null 2>&1 && kill -9 $(pidof iperf3) || true",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)


def start_background_iperf(cfg, run_dir):
    rate_mbit = cfg.get("background_iperf_mbit")
    if rate_mbit is None:
        return None, None, None, None

    direction = cfg.get("background_iperf_direction", "ul").lower()
    if direction not in {"ul", "dl"}:
        raise ValueError("background_iperf_direction must be 'ul' or 'dl'")

    iface = cfg.get("background_tun_iface") or base.OAI_TUN_IFACE
    bind_ip = get_iface_ipv4(iface)

    server_log_path = run_dir / "background_iperf_server_stdout.txt"
    client_log_path = run_dir / "background_iperf_client_stdout.json"

    cleanup_background_iperf_server()

    server_cmd = [
        "docker",
        "exec",
        base.EDGE_CONTAINER_NAME,
        "iperf3",
        "-s",
        "-p",
        str(IPERF_PORT),
        "--one-off",
    ]

    server_proc, server_log = base.start_process(
        server_cmd,
        server_log_path,
        env=os.environ.copy(),
        cwd=base.BASE_DIR,
        sudo=False,
    )

    time.sleep(1)
    base.check_process_running(
        server_proc,
        "background iperf server",
        server_log_path,
    )

    client_cmd = [
        "iperf3",
        "-c",
        base.EDGE_CONTAINER_IP,
        "-p",
        str(IPERF_PORT),
        "-B",
        bind_ip,
        "-u",
        "-b",
        f"{rate_mbit}M",
        "-t",
        str(BACKGROUND_DURATION_SEC),
        "-i",
        "1",
        "--json",
    ]

    if direction == "dl":
        client_cmd.append("-R")

    client_proc, client_log = base.start_process(
        client_cmd,
        client_log_path,
        env=os.environ.copy(),
        cwd=base.BASE_DIR,
        sudo=True,
    )

    time.sleep(2)
    base.check_process_running(client_proc, "background iperf client", client_log_path)
    print(
        f"[Background] iperf {direction.upper()} load started: "
        f"{rate_mbit} Mbit/s via {iface} ({bind_ip})"
    )
    return server_proc, server_log, client_proc, client_log


def parse_slice_assignments(log_text):
    assignments = {}
    for line in log_text.splitlines():
        if "slice" not in line.lower() or "rnti" not in line.lower():
            continue
        rnti_match = re.search(
            r"(?:RNTI|rnti)[^0-9a-fA-FxX]*" + RNTI_AFTER_LABEL_RE,
            line,
        )
        if not rnti_match:
            continue
        rnti = int(rnti_match.group(1), 16)
        dl_match = re.search(r"(?:DL|dl)\s*slice\D*(\d+)", line)
        ul_match = re.search(r"(?:UL|ul)\s*slice\D*(\d+)", line)
        slice_matches = re.findall(r"slice\D*(\d+)", line, flags=re.I)
        dl_slice = int(dl_match.group(1)) if dl_match else None
        ul_slice = int(ul_match.group(1)) if ul_match else None
        if dl_slice is None and ul_slice is None and len(slice_matches) == 1:
            dl_slice = int(slice_matches[0])
            ul_slice = int(slice_matches[0])
        assignments[rnti] = {
            "line": line.strip(),
            "dl_slice": dl_slice,
            "ul_slice": ul_slice,
        }
    return assignments


def validate_slice_assignments(run_dir, industrial_rnti, background_rnti):
    if not STRICT_SLICE_VALIDATION:
        print("[Slice] Strict slice validation disabled by ORBIT3C_STRICT_SLICE_VALIDATION=0")
        return

    log_path = run_dir / "slice_xapp_stdout.txt"
    deadline = time.time() + 20
    expected = {
        int(industrial_rnti): 1,
        int(background_rnti): 2,
    }
    last_assignments = {}

    while time.time() < deadline:
        last_assignments = parse_slice_assignments(read_text_if_exists(log_path))
        ok = True
        for rnti, expected_slice in expected.items():
            assignment = last_assignments.get(rnti)
            if assignment is None:
                ok = False
                break
            dl_slice = assignment.get("dl_slice")
            ul_slice = assignment.get("ul_slice")
            if dl_slice != expected_slice or ul_slice != expected_slice:
                ok = False
                break
        if ok:
            print(
                "[Slice] Validated active RNTIs: "
                f"{format_rnti(industrial_rnti)} -> slice 1, "
                f"{format_rnti(background_rnti)} -> slice 2"
            )
            return
        time.sleep(0.5)

    snapshot = {
        format_rnti(rnti): data
        for rnti, data in sorted(last_assignments.items())
    }
    (run_dir / "slice_assignment_validation_failed.json").write_text(
        json.dumps(
            {
                "expected": {
                    format_rnti(industrial_rnti): 1,
                    format_rnti(background_rnti): 2,
                },
                "observed": snapshot,
                "log_path": str(log_path),
            },
            indent=2,
        )
    )
    raise RuntimeError(
        "Slice xApp did not prove active RNTIs are on the expected slices. "
        f"Check {run_dir / 'slice_assignment_validation_failed.json'} and {log_path}"
    )


def start_slice_xapp(cfg, run_dir, rnti_mapping):
    slice_policy_path = run_dir / "orbit3c_slice_policy.json"

    slice_env = os.environ.copy()
    slice_env["ORBIT3C_SLICE_UL_PCT"] = str(int(cfg["slice_ul_pct"]))
    slice_env["ORBIT3C_SLICE_DL_PCT"] = str(int(cfg["slice_dl_pct"]))
    slice_env["ORBIT3C_SLICE_KEEPALIVE_SEC"] = os.environ.get(
        "ORBIT3C_SLICE_KEEPALIVE_SEC", "1800"
    )
    slice_env["ORBIT3C_SLICE_POLICY_FILE"] = str(slice_policy_path)
    slice_env["ORBIT3C_SLICE_RNTI"] = format_rnti(rnti_mapping["industrial"])
    slice_env["ORBIT3C_BG_RNTI"] = format_rnti(rnti_mapping["background"])

    slice_log_path = run_dir / "slice_xapp_stdout.txt"
    proc, log = base.start_process(
        [str(SLICE_XAPP)],
        slice_log_path,
        env=slice_env,
        cwd=base.FLEXRIC_DIR,
        sudo=False,
    )

    deadline = time.time() + 20
    while time.time() < deadline:
        base.check_process_running(proc, "slice xApp", slice_log_path)
        if slice_policy_path.exists():
            print(f"[Slice] Policy file written: {slice_policy_path}")
            if cfg.get("background_iperf_mbit") is None:
                print("[Slice] Skipping two-RNTI slice validation for no-background run")
            else:
                validate_slice_assignments(
                    run_dir,
                    rnti_mapping["industrial"],
                    rnti_mapping["background"],
                )
            return proc, log
        time.sleep(0.5)

    raise RuntimeError(
        "slice xApp started but did not write orbit3c_slice_policy.json. "
        f"Check {slice_log_path}"
    )


def skip_slice_xapp_for_no_background(cfg, run_dir, rnti_mapping):
    """Record an explicit no-slice-control marker for single-flow smoke runs.

    The static Orbit3C slice xApp is a two-slice controller: it expects separate
    industrial and background RNTIs. In no-background smoke mode there is only
    one controlled flow, so launching the xApp with the same RNTI for both roles
    is invalid and can abort inside FlexRIC. We therefore skip the xApp rather
    than producing misleading "slice" results.
    """

    marker = {
        "slice_control_mode": "skipped_no_background",
        "reason": (
            "No background iperf/RNTI configured; static two-RNTI slice xApp "
            "is not launched for this smoke run."
        ),
        "requested_slice_ul_pct": int(cfg["slice_ul_pct"]),
        "requested_slice_dl_pct": int(cfg["slice_dl_pct"]),
        "industrial_rnti": format_rnti(rnti_mapping["industrial"]),
        "background_rnti": None,
        "active_rntis_at_decision": [
            format_rnti(rnti) for rnti in rnti_mapping.get("active", [])
        ],
        "written_time": datetime.now().isoformat(timespec="seconds"),
    }
    marker_path = run_dir / "slice_xapp_skipped_no_background.json"
    marker_path.write_text(json.dumps(marker, indent=2))
    update_run_config(
        run_dir,
        {
            "slice_control_mode": marker["slice_control_mode"],
            "slice_control_note": marker["reason"],
            "slice_control_marker": str(marker_path),
            "resource_control": "edge_cpu_only_no_slice_xapp_smoke",
            "background_rnti": None,
        },
    )
    print(
        "[Slice] Skipping static slice xApp for no-background smoke run. "
        f"Marker: {marker_path}"
    )
    return None, None


def verify_preconditions():
    base.verify_preconditions()
    core_reason = classify_core_health()
    if core_reason:
        raise RuntimeError(
            f"{core_reason}\n"
            "Restart the OAI core before starting experiments. For SMF/UPF state, "
            "restart both together rather than only one of them."
        )

    if not SLICE_XAPP.exists():
        raise FileNotFoundError(
            f"Slice xApp not found: {SLICE_XAPP}\n"
            "Build it from the FlexRIC directory first:\n"
            "  cmake --build build --target xapp_orbit3c_static_slice_ctrl -j2"
        )

    if BACKGROUND_IPERF_MBIT is not None:
        base.run_cmd(["iperf3", "--version"], check=True, capture=True)
        base.run_cmd(
            [
                "docker",
                "exec",
                base.EDGE_CONTAINER_NAME,
                "iperf3",
                "--version",
            ],
            check=True,
            capture=True,
        )
        base.run_cmd(
            ["ip", "addr", "show", BACKGROUND_TUN_IFACE],
            check=True,
            capture=True,
        )

    print("[Check] PRB/slice runner preconditions are ready.")


def apply_edge_container_resources(cfg, run_dir):
    """Apply Stage 1 direct edge CPU-resource control using Docker update."""
    edge_cpus = cfg.get("edge_cpus", None)

    if edge_cpus is None or edge_cpus == "":
        print("[EdgeResource] No edge_cpus set; leaving edge container unchanged.")
        return

    edge_cpus = float(edge_cpus)
    print(
        f"[EdgeResource] Applying Docker CPU limit: "
        f"container={base.EDGE_CONTAINER_NAME} --cpus={edge_cpus}"
    )

    result = subprocess.run(
        ["docker", "update", "--cpus", str(edge_cpus), base.EDGE_CONTAINER_NAME],
        text=True,
        capture_output=True,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Failed to update edge container CPU limit.\n"
            f"container={base.EDGE_CONTAINER_NAME}\n"
            f"edge_cpus={edge_cpus}\n"
            f"stdout={result.stdout}\n"
            f"stderr={result.stderr}"
        )

    inspect = subprocess.run(
        ["docker", "inspect", "-f", "{{.HostConfig.NanoCpus}}", base.EDGE_CONTAINER_NAME],
        text=True,
        capture_output=True,
        check=False,
    )
    nano_cpus = inspect.stdout.strip() if inspect.returncode == 0 else ""

    snapshot = {
        "edge_label": cfg.get("edge_label", ""),
        "edge_cpus_requested": edge_cpus,
        "edge_compute_capacity": cfg.get("edge_compute_capacity", ""),
        "edge_resource_control": cfg.get("edge_resource_control", "docker_update_cpus"),
        "docker_update_stdout": result.stdout.strip(),
        "docker_update_stderr": result.stderr.strip(),
        "docker_nano_cpus_after_update": nano_cpus,
        "updated_time": datetime.now().isoformat(timespec="seconds"),
    }
    (run_dir / "edge_container_resource_state.json").write_text(json.dumps(snapshot, indent=2))
    print(f"[EdgeResource] Docker CPU limit applied. NanoCpus={nano_cpus}")


def run_one_experiment(i, cfg):
    cfg = dict(cfg)
    cfg.setdefault("run_id", make_run_id(i, cfg))

    run_dir = base.RESULTS_DIR / cfg["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    ULSCH_GRANT_LOG.parent.mkdir(parents=True, exist_ok=True)
    ULSCH_GRANT_LOG.write_text("")

    print("\n" + "#" * 90)
    print(f"[Run] {cfg['run_id']}")
    print(json.dumps(cfg, indent=2))
    print("#" * 90)

    factory_csv = run_dir / "factory_latency.csv"
    kpm_csv = run_dir / "orbit3c_kpm_log.csv"

    kpm_proc = None
    kpm_log = None
    slice_proc = None
    slice_log = None
    edge_proc = None
    edge_log = None
    bg_server_proc = None
    bg_server_log = None
    bg_client_proc = None
    bg_client_log = None
    ue_proc = None
    ue_log = None
    factory_proc = None
    factory_log = None

    try:
        base.cleanup_old_processes()
        base.verify_no_old_host_processes()
        verify_required_tunnels_for_run(cfg)
        gnb_log_offset = get_file_size(base.GNB_LOG)
        ue_log_offset = get_file_size(OAI_UE_LOG)
        ric_log_offset = get_file_size(OAI_RIC_LOG)
        stack_monitor_offsets = {
            base.GNB_LOG: gnb_log_offset,
            OAI_UE_LOG: ue_log_offset,
            OAI_RIC_LOG: ric_log_offset,
        }

        wait_for_clean_stack_window(
            cfg,
            stack_monitor_offsets,
            STACK_STABILITY_WINDOW_SEC,
        )

        write_current_config(cfg, run_dir)

        reset_tc_to_no_limit(run_dir)

        # Stage 1: apply direct edge container CPU resource limit
        apply_edge_container_resources(cfg, run_dir)

        kpm_env = os.environ.copy()
        kpm_env["ORBIT3C_KPM_CSV"] = str(kpm_csv)

        kpm_log_path = run_dir / "kpm_logger_stdout.txt"
        kpm_proc, kpm_log = base.start_process(
            [str(base.KPM_LOGGER)],
            kpm_log_path,
            env=kpm_env,
            cwd=base.FLEXRIC_DIR,
            sudo=False,
        )

        time.sleep(2)
        base.check_process_running(kpm_proc, "KPM logger", kpm_log_path)

        edge_log_path = run_dir / "edge_stdout.txt"
        edge_cmd = base.build_edge_container_command(cfg)

        edge_proc, edge_log = base.start_process(
            edge_cmd,
            edge_log_path,
            env=os.environ.copy(),
            cwd=base.BASE_DIR,
            sudo=False,
        )

        time.sleep(2)
        base.check_process_running(edge_proc, "Edge relay in edge-dev", edge_log_path)
        base.wait_for_tcp_port(base.EDGE_CONTAINER_IP, 9100, timeout_sec=120)

        if (
            cfg.get("background_iperf_mbit") is not None
            and RNTI_TUNNEL_PROBE_ENABLED
            and (cfg.get("background_tun_iface") or base.OAI_TUN_IFACE) != base.OAI_TUN_IFACE
        ):
            rnti_mapping = discover_rntis_by_tunnel_probe(cfg, run_dir)
            bg_server_proc, bg_server_log, bg_client_proc, bg_client_log = (
                start_background_iperf(cfg, run_dir)
            )
            print(
                f"[RNTI] Waiting {RNTI_STABILIZE_SEC:.1f}s after background iperf start "
                "before slice xApp startup"
            )
            time.sleep(RNTI_STABILIZE_SEC)
        else:
            bg_server_proc, bg_server_log, bg_client_proc, bg_client_log = (
                start_background_iperf(cfg, run_dir)
            )
            if cfg.get("background_iperf_mbit") is not None:
                print(
                    f"[RNTI] Waiting {RNTI_STABILIZE_SEC:.1f}s after background iperf start "
                    "before discovering active RNTIs"
                )
                time.sleep(RNTI_STABILIZE_SEC)
            else:
                print(
                    "[RNTI] No background iperf configured; discovering current RNTIs "
                    "before slice xApp startup"
                )
            if cfg.get("background_iperf_mbit") is None:
                rnti_mapping = discover_single_industrial_rnti(
                    run_dir, gnb_log_offset=gnb_log_offset
                )
            else:
                rnti_mapping = discover_current_rntis(
                    run_dir, gnb_log_offset=gnb_log_offset
                )

        update_run_config(
            run_dir,
            {
                "rnti_discovery_source": rnti_mapping["source"],
                "industrial_rnti": format_rnti(rnti_mapping["industrial"]),
                "background_rnti": format_rnti(rnti_mapping["background"]),
                "active_rntis_at_slice_start": [
                    format_rnti(r) for r in rnti_mapping["active"]
                ],
                "iface_to_rnti": {
                    iface: format_rnti(rnti)
                    for iface, rnti in rnti_mapping.get("by_iface", {}).items()
                },
            },
        )
        if cfg.get("background_iperf_mbit") is None:
            slice_proc, slice_log = skip_slice_xapp_for_no_background(
                cfg, run_dir, rnti_mapping
            )
        else:
            slice_proc, slice_log = start_slice_xapp(cfg, run_dir, rnti_mapping)

        verify_required_tunnels_for_run(cfg)

        ue_env = os.environ.copy()
        ue_env["EDGE_IP"] = base.EDGE_CONTAINER_IP
        ue_env["EDGE_PORT"] = "9100"
        ue_env["UE_BIND_INTERFACE"] = base.OAI_TUN_IFACE
        ue_env["SOCKET_TIMEOUT_SEC"] = "120.0"

        ue_log_path = run_dir / "ue_forwarder_stdout.txt"
        ue_proc, ue_log = base.start_process(
            ["python3", str(base.UE_FWD_SCRIPT)],
            ue_log_path,
            env=ue_env,
            cwd=base.BASE_DIR,
            sudo=True,
        )

        time.sleep(1)
        base.check_process_running(ue_proc, "UE forwarder", ue_log_path)
        base.wait_for_tcp_port("127.0.0.1", 9200, timeout_sec=20)

        factory_env = os.environ.copy()
        factory_env["LATENCY_CSV_PATH"] = str(factory_csv)
        factory_env["UE_FORWARDER_IP"] = "127.0.0.1"
        factory_env["UE_FORWARDER_PORT"] = "9200"
        factory_env["SOCKET_TIMEOUT_SEC"] = "120.0"

        factory_env["MODEL_PATH"] = cfg["model_path"]
        factory_env["RUN_SPEED"] = str(cfg["run_speed"])
        factory_env["ROI_DEBUG_DIR"] = str(run_dir / "factory_roi_debug")
        factory_env["ENCODING"] = cfg.get("encoding", "raw")
        factory_env["JPEG_QUALITY"] = str(cfg.get("jpeg_quality", 35))

        factory_log_path = run_dir / "factory_stdout.txt"
        factory_proc, factory_log = base.start_process(
            [ROS_PYTHON, str(base.FACTORY_SCRIPT)],
            factory_log_path,
            env=factory_env,
            cwd=base.BASE_DIR,
            sudo=False,
        )

        time.sleep(2)
        base.check_process_running(factory_proc, "Factory relay", factory_log_path)

        base.request_isaac_run(cfg["run_id"], run_dir)
        wait_for_isaac_done_with_stability_monitor(
            cfg,
            run_dir,
            {
                **stack_monitor_offsets,
                ue_log_path: 0,
                factory_log_path: 0,
            },
            [
                ("UE forwarder log", ue_log_path),
                ("factory log", factory_log_path),
            ],
            timeout_sec=1800,
        )

        base.copy_if_exists(base.GNB_LOG, run_dir / "gnb_dev_rfsim_e2.log")
        base.copy_if_exists(OAI_UE_LOG, run_dir / "ue_2ue_dev_rfsim_e2.log")
        base.copy_if_exists(OAI_RIC_LOG, run_dir / "nearRT-RIC.log")
        base.copy_if_exists(ULSCH_GRANT_LOG, run_dir / "ulsch_grants.csv")
        update_run_config(
            run_dir,
            {
                "status": "completed",
                "exclude_from_analysis": False,
                "completed_time": datetime.now().isoformat(timespec="seconds"),
            },
        )

        print(f"[Done] Saved run to: {run_dir}")

    finally:
        base.stop_process(factory_proc, factory_log, "factory relay")
        base.stop_process(ue_proc, ue_log, "UE forwarder")
        base.stop_process(bg_client_proc, bg_client_log, "background iperf client")
        base.stop_process(bg_server_proc, bg_server_log, "background iperf server")
        base.stop_process(edge_proc, edge_log, "edge relay in edge-dev")
        base.stop_process(slice_proc, slice_log, "slice xApp")
        base.stop_process(kpm_proc, kpm_log, "KPM logger")

        reset_tc_to_no_limit(run_dir)
        cleanup_background_iperf_server()
        base.cleanup_old_processes()

        try:
            base.verify_no_old_host_processes()
        except Exception as e:
            print(f"[Warn] Post-run cleanup check issue: {e}")


def run_experiment_with_recovery(i, cfg):
    run_id = cfg.setdefault("run_id", make_run_id(i, cfg))
    run_dir = base.RESULTS_DIR / run_id
    max_attempts = 1 + max(0, MAX_RECOVERY_RETRIES_PER_EXPERIMENT)

    for attempt in range(1, max_attempts + 1):
        try:
            if attempt > 1:
                print(f"[Retry] Re-running {run_id}, attempt {attempt}/{max_attempts}")
            run_one_experiment(i, cfg)
            return True
        except Exception as e:
            reason = str(e)
            unstable = is_instability_failure(reason)
            run_dir.mkdir(parents=True, exist_ok=True)
            update_run_config(
                run_dir,
                {
                    "status": "unstable" if unstable else "failed",
                    "failure_reason": reason,
                    "failure_time": datetime.now().isoformat(timespec="seconds"),
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    # A failed run may contain a partial factory CSV. Exclude
                    # every failure, not only failures classified as unstable,
                    # so online observation cannot treat partial data as a
                    # completed optimizer trial.
                    "exclude_from_analysis": True,
                },
            )
            print(f"[Error] Experiment failed: {run_id}: {reason}")
            if unstable:
                collect_core_diagnostics(run_dir, reason)

            if attempt >= max_attempts:
                print(f"[Skip] Exhausted retries for {run_id}; continuing to next experiment.")
                return False

            recovery_ok = run_stack_recovery(run_id, reason, attempt)
            if not recovery_ok:
                update_run_config(
                    run_dir,
                    {
                        "status": "recovery_failed",
                        "recovery_failure_time": datetime.now().isoformat(timespec="seconds"),
                        "exclude_from_analysis": True,
                    },
                )
                print(
                    f"[Skip] Recovery failed for {run_id}; "
                    "not retrying this experiment on an unhealthy stack."
                )
                return False

    return False


def main():
    base.RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    print("\nBefore starting, make sure Isaac is open and the Isaac controller is running.")
    print("The runner will start/restart the non-Isaac stack by default:")
    print("  1. edge-dev container")
    print("  2. OAI AMF/SMF/UPF core services")
    print("  3. nearRT-RIC")
    print("  4. E2-enabled gNB with the modified slice-aware scheduler")
    print("  5. two-UE RFsim with oaitun_ue1 and oaitun_ue2")
    print("\nRun this script as normal user, not sudo:")
    print("  source /opt/ros/jazzy/setup.bash")
    print("  sudo -v")
    print("  export ORBIT3C_KPM_REPORT_MODE=ue")
    print("  export ORBIT3C_KPM_SUB_DELAY_MS=500")
    print(f"  export ORBIT3C_ULSCH_GRANT_CSV={ULSCH_GRANT_LOG}")
    print("  python3 automated_experiments/scripts/codesign_prb_slice_experiment_runner_v1.py")
    print("\nOptional same-UE offered background load:")
    print("  export ORBIT3C_BG_IPERF_MBIT=30")
    print("  export ORBIT3C_BG_IPERF_DIRECTION=ul   # or dl")
    print("\nOptional true second-UE slice isolation:")
    print("  export ORBIT3C_SLICE_RNTI=0xINDUSTRIAL_RNTI")
    print("  export ORBIT3C_BG_RNTI=0xBACKGROUND_RNTI")
    print("  export ORBIT3C_BG_TUN_IFACE=oaitun_ue2")
    print("\nOptional startup controls:")
    print("  export ORBIT3C_START_STACK=0              # skip automatic non-Isaac startup")
    print("  export ORBIT3C_STARTUP_RECOVER_CORE=0     # keep current AMF/SMF/UPF running")
    print("  export ORBIT3C_SUDO_KEEPALIVE=0           # disable sudo timestamp refresh")

    input("\nPress ENTER to start non-Isaac stack, verify preconditions, and start PRB/slice runs...")

    start_sudo_keepalive()

    try:
        start_non_isaac_stack()
        verify_preconditions()

        failed_runs = []
        for i, cfg in enumerate(EXPERIMENTS, start=1):
            completed_run_dir = find_completed_run_for_config(cfg)
            if completed_run_dir:
                print(
                    "[Resume] Skipping already completed config "
                    f"{i}/{len(EXPERIMENTS)}: {completed_run_dir.name}"
                )
                continue
            ok = run_experiment_with_recovery(i, cfg)
            if not ok:
                failed_runs.append(cfg.get("run_id") or make_run_id(i, cfg))

        if failed_runs:
            print("\nPRB/slice sweep finished with failed/skipped runs:")
            for run_id in failed_runs:
                print(f"  - {run_id}")
            return False
        else:
            print("\nAll requested PRB/slice runs finished.")
            return True
    finally:
        stop_sudo_keepalive()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
