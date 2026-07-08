#!/usr/bin/env python3
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from datetime import datetime


# =========================================================
# Host paths
# =========================================================


BASE_DIR = Path(
    os.environ.get("ORBIT3C_CO_DESIGN_DIR", Path(__file__).resolve().parents[1])
)
RESULTS_DIR = Path(os.environ.get("ORBIT3C_RESULTS_DIR", BASE_DIR / "results"))
CURRENT_CONFIG_PATH = Path(
    os.environ.get("ORBIT3C_CURRENT_CONFIG", BASE_DIR / "current_experiment_config.json")
)

FLEXRIC_DIR = Path(
    os.environ.get(
        "ORBIT3C_FLEXRIC_DIR",
        "/home/elahe/user/ORBIT3C_OAI_DEV/oai_ran/openairinterface5g/openair2/E2AP/flexric",
    )
)


ISAAC_COMMAND_PATH = Path(
    os.environ.get("ORBIT3C_ISAAC_COMMAND", BASE_DIR / "isaac_command.json")
)
ISAAC_STATUS_PATH = Path(
    os.environ.get("ORBIT3C_ISAAC_STATUS", BASE_DIR / "isaac_status.json")
)


GNB_LOG = Path(
    os.environ.get(
        "ORBIT3C_GNB_LOG",
        "/home/elahe/user/ORBIT3C_OAI_DEV/logs/gnb_dev_rfsim_e2.log",
    )
)
UE_LOG = Path(
    os.environ.get(
        "ORBIT3C_UE_LOG",
        "/home/elahe/user/ORBIT3C_OAI_DEV/logs/ue_2ue_dev_rfsim_e2.log",
    )
)

UE_FWD_SCRIPT = BASE_DIR / "scripts" / "ue_forwarder_oai_dev.py"
FACTORY_SCRIPT = BASE_DIR / "scripts" / "factory_relay_oai_dev.py"

OAI_TUN_IFACE = "oaitun_ue1"

KPM_LOGGER = FLEXRIC_DIR / "build/examples/xApp/c/monitor/xapp_kpm_logger_orbit3c"


# =========================================================
# Edge container settings
# =========================================================
EDGE_CONTAINER_NAME = "edge-dev"
EDGE_CONTAINER_IP = "192.168.83.150"
EDGE_CONTAINER_WORKDIR = "/workspace"
EDGE_CONTAINER_SCRIPT = "/workspace/edge_relay_oai_dev.py"
EDGE_CONTAINER_RESULTS_BASE = "/workspace/automated_experiments/results"


# =========================================================
# Experiment plan: 2 runs
# =========================================================
# EXPERIMENTS = [
#     {
#         "model_path": "best_n.pt",
#         "bandwidth_mbit": 10,
#         "camera_width": 1280,
#         "camera_height": 720,
#         "run_speed": 0.5,
#         "encoding": "raw",
#         "jpeg_quality": 35,
#     },
#     {
#         "model_path": "best_n.pt",
#         "bandwidth_mbit": 50,
#         "camera_width": 640,
#         "camera_height": 360,
#         "run_speed": 1.0,
#         "encoding": "raw",
#         "jpeg_quality": 35,
#     },
# ]
# =========================================================
# Experiment plan: full factorial sweep
# 3 bandwidths × 3 models × 3 resolutions × 4 speeds = 108 runs
# =========================================================

# # =========================================================
# Stage 1 edge CPU-resource pilot plan
# Keep detector fixed. Vary container CPU allocation, resolution, speed,
# and one communication condition to isolate edge-resource sensitivity.
# =========================================================
BANDWIDTHS = [None]  # None = no bandwidth limit
MODELS = ["best_n.pt"]
RESOLUTIONS = [
    (1280, 720),
    (640, 360),
]
SPEEDS = [1.0, 2.0]

EDGE_RESOURCE_LEVELS = [
    {"edge_label": "edge_cpu4", "edge_cpus": 4.0, "edge_compute_capacity": 1.0},
    {"edge_label": "edge_cpu2", "edge_cpus": 2.0, "edge_compute_capacity": 0.5},
    {"edge_label": "edge_cpu1", "edge_cpus": 1.0, "edge_compute_capacity": 0.25},
    {"edge_label": "edge_cpu0p5", "edge_cpus": 0.5, "edge_compute_capacity": 0.125},
]

EXPERIMENTS = []

for bandwidth in BANDWIDTHS:
    for edge_level in EDGE_RESOURCE_LEVELS:
        for model_path in MODELS:
            for width, height in RESOLUTIONS:
                for speed in SPEEDS:
                    EXPERIMENTS.append(
                        {
                            **edge_level,
                            "model_path": model_path,
                            "bandwidth_mbit": bandwidth,
                            "camera_width": width,
                            "camera_height": height,
                            "run_speed": speed,
                            "encoding": "raw",
                            "jpeg_quality": 35,
                            "edge_resource_control": "docker_update_cpus",
                        }
                    )

print(f"[ExperimentPlan] Total experiments: {len(EXPERIMENTS)}")


# =========================================================
# Helpers
# =========================================================


def request_isaac_run(run_id, run_dir):
    command = {
        "run_id": run_id,
        "command": "start",
        "config_path": str(CURRENT_CONFIG_PATH),
        "run_dir": str(run_dir),
        "created_time": time.time(),
    }

    # Remove old status before starting new run
    if ISAAC_STATUS_PATH.exists():
        ISAAC_STATUS_PATH.unlink()

    ISAAC_COMMAND_PATH.write_text(json.dumps(command, indent=2))
    print(f"[Isaac] Requested run_id={run_id}")
    print(f"[Isaac] Wrote command: {ISAAC_COMMAND_PATH}")


def wait_for_isaac_done(run_id, timeout_sec=1800):
    print(f"[Isaac] Waiting for Isaac to finish run_id={run_id}")

    start = time.time()
    while time.time() - start < timeout_sec:
        if ISAAC_STATUS_PATH.exists():
            try:
                status = json.loads(ISAAC_STATUS_PATH.read_text())
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


def run_cmd(cmd, check=True, capture=False):
    print(f"\n[CMD] {' '.join(map(str, cmd))}")
    if capture:
        return subprocess.run(cmd, check=check, text=True, capture_output=True)
    return subprocess.run(cmd, check=check)


def sanitize_float(x):
    return str(x).replace(".", "p")


def make_run_id(i, cfg):
    model_name = Path(cfg["model_path"]).stem
    bw = cfg["bandwidth_mbit"]
    bw_txt = "nolimit" if bw is None else f"{bw}mbit"
    w = cfg["camera_width"]
    h = cfg["camera_height"]
    speed = sanitize_float(cfg["run_speed"])
    enc = cfg.get("encoding", "raw")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    edge_label = cfg.get("edge_label", "edge_default")
    return f"run_{i:03d}_{ts}_bw{bw_txt}_{edge_label}_{model_name}_{w}x{h}_speed{speed}_{enc}"


def wait_for_tcp_port(host, port, timeout_sec=20):
    start = time.time()
    last_error = None

    while time.time() - start < timeout_sec:
        try:
            with socket.create_connection((host, port), timeout=1):
                print(f"[Ready] {host}:{port} is accepting connections")
                return True
        except OSError as e:
            last_error = e
            time.sleep(0.5)

    raise RuntimeError(
        f"Timeout waiting for {host}:{port}. Last error: {repr(last_error)}"
    )


def check_process_running(proc, name, log_path):
    if proc.poll() is not None:
        raise RuntimeError(
            f"{name} exited early with return code {proc.returncode}. "
            f"Check log: {log_path}"
        )


def apply_bandwidth_limit(bandwidth_mbit):
    if bandwidth_mbit is None or str(bandwidth_mbit).lower() in [
        "none", "nolimit", "no_limit"
    ]:
        print("[TC] Removing bandwidth limit")
        subprocess.run(
            ["sudo", "-n", "tc", "qdisc", "del", "dev", OAI_TUN_IFACE, "root"],
            check=False,
        )
        return

    rate = f"{int(bandwidth_mbit)}mbit"
    print(f"[TC] Applying bandwidth limit: {rate}")

    run_cmd(
        [
            "sudo", "-n",
            "tc", "qdisc", "replace",
            "dev", OAI_TUN_IFACE,
            "root", "tbf",
            "rate", rate,
            "burst", "256kbit",
            "latency", "800ms",
        ]
    )


def reset_bandwidth_limit():
    print("[TC] Resetting bandwidth limit")
    subprocess.run(
        ["sudo", "-n", "tc", "qdisc", "del", "dev", OAI_TUN_IFACE, "root"],
        check=False,
    )


def save_tc_state(run_dir):
    out = run_cmd(
        ["sudo", "-n", "tc", "qdisc", "show", "dev", OAI_TUN_IFACE],
        check=False,
        capture=True,
    )
    (run_dir / "tc_qdisc.txt").write_text(out.stdout + "\n" + out.stderr)


def write_current_config(cfg, run_dir):
    config = {
        "run_id": cfg["run_id"],
        "run_speed": float(cfg["run_speed"]),
        "camera_width": int(cfg["camera_width"]),
        "camera_height": int(cfg["camera_height"]),
        "render_product_node": "/World/ActionGraph_01/isaac_create_render_product",

        # metadata
        "bandwidth_mbit": cfg["bandwidth_mbit"],
        "model_path": cfg["model_path"],
        "encoding": cfg.get("encoding", "raw"),
        "jpeg_quality": cfg.get("jpeg_quality", 35),
        "run_dir": str(run_dir),

        # Stage 1 edge-resource metadata
        "edge_label": cfg.get("edge_label", ""),
        "edge_cpus": cfg.get("edge_cpus", ""),
        "edge_compute_capacity": cfg.get("edge_compute_capacity", ""),
        "edge_resource_control": cfg.get("edge_resource_control", "docker_update_cpus"),
    }

    CURRENT_CONFIG_PATH.write_text(json.dumps(config, indent=2))
    (run_dir / "run_config.json").write_text(json.dumps(config, indent=2))
    print(f"[Config] Wrote {CURRENT_CONFIG_PATH}")
    print(json.dumps(config, indent=2))


def start_process(cmd, log_path, env=None, cwd=None, sudo=False):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(log_path, "w")

    full_cmd = cmd
    if sudo:
        full_cmd = ["sudo", "-n", "-E"] + cmd

    print(f"[Start] {' '.join(map(str, full_cmd))}")
    print(f"[Log] {log_path}")

    proc = subprocess.Popen(
        full_cmd,
        stdin=subprocess.DEVNULL,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        cwd=str(cwd) if cwd else None,
        env=env,
        # sudo processes stay in current process group so sudo cache works.
        # non-sudo processes are isolated.
        preexec_fn=None if sudo else os.setsid,
        text=True,
    )

    return proc, log_f


def stop_process(proc, log_file, name):
    if proc is None:
        return

    print(f"[Stop] {name}")

    try:
        if proc.poll() is None:
            proc_pgid = os.getpgid(proc.pid)
            current_pgid = os.getpgrp()

            if proc_pgid != current_pgid:
                os.killpg(proc_pgid, signal.SIGTERM)
            else:
                proc.terminate()

            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass

        if proc.poll() is None:
            proc_pgid = os.getpgid(proc.pid)
            current_pgid = os.getpgrp()

            if proc_pgid != current_pgid:
                os.killpg(proc_pgid, signal.SIGKILL)
            else:
                proc.kill()

            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    except BaseException as e:
        print(f"[Warn] stop_process({name}) issue: {repr(e)}")

    try:
        if log_file is not None:
            log_file.close()
    except Exception:
        pass


def copy_if_exists(src, dst):
    try:
        if src.exists():
            dst.write_bytes(src.read_bytes())
            print(f"[Copy] {src} -> {dst}")
        else:
            print(f"[Warn] Missing file: {src}")
    except Exception as e:
        print(f"[Warn] Failed to copy {src} -> {dst}: {e}")


def wait_for_user_to_run_isaac():
    print("\n" + "=" * 80)
    print("Now run the Isaac spawning script inside Isaac.")
    print("It should read:")
    print(f"  {CURRENT_CONFIG_PATH}")
    print("Wait until Isaac prints:")
    print("  [Done] Pipeline finished.")
    input("Press ENTER here after the Isaac inspection run finishes...")
    print("=" * 80 + "\n")


# =========================================================
# Cleanup and verification
# =========================================================
def kill_edge_relay_inside_container():
    """
    Kill any process inside edge-dev that currently owns TCP port 9100.
    This is safer than only killing by script name because the failure mode is
    specifically "address already in use" on 9100.
    """
    print("[Cleanup] Killing any process using port 9100 inside edge-dev...")

    edge_kill_cmd = r"""
python3 - <<'PY'
import os
import signal
import time

target_port = 9100
target_hex = format(target_port, "04X")

def find_port_owners():
    inodes = set()

    for path in ["/proc/net/tcp", "/proc/net/tcp6"]:
        try:
            lines = open(path).readlines()[1:]
        except FileNotFoundError:
            continue

        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue
            local_addr = parts[1]
            inode = parts[9]
            port_hex = local_addr.split(":")[1]

            if port_hex.upper() == target_hex:
                inodes.add(inode)

    owners = []

    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue

        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except Exception:
            continue

        for fd in fds:
            try:
                link = os.readlink(f"{fd_dir}/{fd}")
            except Exception:
                continue

            for inode in inodes:
                if link == f"socket:[{inode}]":
                    try:
                        cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="ignore")
                    except Exception:
                        cmd = ""
                    owners.append((int(pid), cmd))

    return owners

owners = find_port_owners()

if not owners:
    print("No process is using port 9100.")
else:
    for pid, cmd in owners:
        print(f"Killing PID={pid} CMD={cmd}")
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception as e:
            print(f"Failed to kill PID={pid}: {e}")

time.sleep(1)

owners_after = find_port_owners()
if owners_after:
    print("WARNING: port 9100 still has owners after kill:")
    for pid, cmd in owners_after:
        print(f"  PID={pid} CMD={cmd}")
else:
    print("Port 9100 owners cleared.")
PY
"""

    subprocess.run(
        ["docker", "exec", EDGE_CONTAINER_NAME, "bash", "-lc", edge_kill_cmd],
        check=False,
    )

    time.sleep(1)


def wait_until_edge_port_free(timeout_sec=15):
    """
    Check whether the edge listener port can be reused.
    SO_REUSEADDR is used here because the edge server also uses SO_REUSEADDR,
    so this check matches the real server behavior after rapid restarts.
    """
    print("[Check] Waiting until edge port 9100 is free inside edge-dev...")

    check_cmd = r"""
python3 - <<'PY'
import socket

target_port = 9100

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

try:
    s.bind(("0.0.0.0", target_port))
    print("free")
except OSError as e:
    print("busy", repr(e))
finally:
    s.close()
PY
"""

    start = time.time()
    last_stdout = ""
    last_stderr = ""

    while time.time() - start < timeout_sec:
        out = subprocess.run(
            ["docker", "exec", EDGE_CONTAINER_NAME, "bash", "-lc", check_cmd],
            text=True,
            capture_output=True,
            check=False,
        )

        last_stdout = out.stdout.strip()
        last_stderr = out.stderr.strip()

        if "free" in out.stdout.lower():
            print("[Check] edge port 9100 is free.")
            return

        if last_stdout:
            print(f"[Check stdout] {last_stdout}")
        if last_stderr:
            print(f"[Check stderr] {last_stderr}")

        time.sleep(1)

    raise RuntimeError(
        "edge-dev port 9100 is still busy after cleanup.\n"
        f"Last stdout: {last_stdout}\n"
        f"Last stderr: {last_stderr}"
    )


def cleanup_old_processes():
    print("[Cleanup] Killing old factory relay on host if any...")
    subprocess.run(
        ["pkill", "-f", "factory_relay_oai_dev.py"],
        check=False,
    )

    print("[Cleanup] Killing old UE forwarder on host if any...")
    subprocess.run(
        ["sudo", "-n", "pkill", "-f", "ue_forwarder_oai_dev.py"],
        check=False,
    )

    print("[Cleanup] Killing old edge relay inside edge-dev if any...")
    kill_edge_relay_inside_container()
    wait_until_edge_port_free()

    time.sleep(1)


def verify_no_old_host_processes():
    out = subprocess.run(
        [
            "bash",
            "-lc",
            "ps aux | grep -E 'factory_relay_oai_dev.py|ue_forwarder_oai_dev.py' | grep -v grep || true",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    if out.stdout.strip():
        raise RuntimeError(
            "Old host processes are still running after cleanup:\n"
            f"{out.stdout}\n"
            "Run manually:\n"
            "  sudo pkill -f ue_forwarder_oai_dev.py\n"
            "  pkill -f factory_relay_oai_dev.py"
        )

    out_port = subprocess.run(
        ["bash", "-lc", "ss -ltnp | grep ':9200' || true"],
        text=True,
        capture_output=True,
        check=False,
    )

    if out_port.stdout.strip():
        raise RuntimeError(
            "Port 9200 is still in use after cleanup:\n"
            f"{out_port.stdout}\n"
            "Run manually:\n"
            "  sudo pkill -f ue_forwarder_oai_dev.py"
        )

    print("[Check] No old host factory/UE processes and port 9200 is free.")


def verify_preconditions():
    print("[Check] Verifying required paths and interface...")

    required_files = [
        UE_FWD_SCRIPT,
        FACTORY_SCRIPT,
        KPM_LOGGER,
    ]

    for p in required_files:
        if not p.exists():
            raise FileNotFoundError(f"Required file not found: {p}")

    out = run_cmd(
        ["ip", "addr", "show", OAI_TUN_IFACE],
        check=False,
        capture=True,
    )

    if out.returncode != 0:
        raise RuntimeError(
            f"Interface {OAI_TUN_IFACE} not found. "
            f"Make sure UE is registered and oaitun_ue1 exists.\n"
            f"stdout={out.stdout}\nstderr={out.stderr}"
        )

    print(f"[Check] Interface {OAI_TUN_IFACE} exists.")

    out_sudo = run_cmd(["sudo", "-n", "true"], check=False, capture=True)
    if out_sudo.returncode != 0:
        raise RuntimeError(
            "Sudo permission is not currently cached. Run this first:\n\n"
            "  sudo -v\n\n"
            f"stderr={out_sudo.stderr}"
        )

    print("[Check] sudo permission is cached.")

    out_container = run_cmd(
        [
            "docker",
            "exec",
            EDGE_CONTAINER_NAME,
            "bash",
            "-lc",
            (
                f"test -f {EDGE_CONTAINER_SCRIPT} && "
                + " && ".join([
                    f"test -f {EDGE_CONTAINER_WORKDIR}/{Path(model_file).name}"
                    for model_file in MODELS
                ])
                + " && python3 -c 'from ultralytics import YOLO; print(\"edge container ok\")'"
            ),
        ],
        check=False,
        capture=True,
    )

    if out_container.returncode != 0:
        raise RuntimeError(
            "edge-dev container is not ready. Check that /workspace is mounted, "
            "edge_relay_oai_dev.py exists, model files exist, and ultralytics is installed.\n"
            f"stdout={out_container.stdout}\nstderr={out_container.stderr}"
        )

    print("[Check] edge-dev container is ready.")
    print(out_container.stdout)

    cleanup_old_processes()
    verify_no_old_host_processes()


# =========================================================
# Edge container command
# =========================================================
def build_edge_container_command(cfg):
    model_name = Path(cfg["model_path"]).name

    edge_container_run_dir = f"{EDGE_CONTAINER_RESULTS_BASE}/{cfg['run_id']}"
    edge_container_csv = f"{edge_container_run_dir}/edge_results.csv"
    edge_container_debug = f"{edge_container_run_dir}/edge_debug"
    edge_container_roi_debug = f"{edge_container_run_dir}/edge_roi_debug"

    edge_container_model_path = f"{EDGE_CONTAINER_WORKDIR}/{model_name}"

    container_script = f"""
set -e
cd {EDGE_CONTAINER_WORKDIR}
mkdir -p {edge_container_run_dir}

MODEL_PATH={edge_container_model_path} \\
RUN_SPEED={cfg['run_speed']} \\
EDGE_CSV_PATH={edge_container_csv} \\
EDGE_DEBUG_DIR={edge_container_debug} \\
EDGE_ROI_DEBUG_DIR={edge_container_roi_debug} \\
EDGE_LABEL={cfg.get("edge_label", "")} \\
EDGE_CPUS={cfg.get("edge_cpus", "")} \\
EDGE_COMPUTE_CAPACITY={cfg.get("edge_compute_capacity", "")} \\
EDGE_RESOURCE_CONTROL={cfg.get("edge_resource_control", "docker_update_cpus")} \\
EDGE_TORCH_THREADS={cfg.get("edge_torch_threads", os.environ.get("EDGE_TORCH_THREADS", "4"))} \\
EDGE_PRELOAD_MODEL={os.environ.get("EDGE_PRELOAD_MODEL", "1")} \\
EDGE_WARMUP_INFERENCE={os.environ.get("EDGE_WARMUP_INFERENCE", "1")} \\
EDGE_WARMUP_IMAGE_WIDTH={os.environ.get("EDGE_WARMUP_IMAGE_WIDTH", "640")} \\
EDGE_WARMUP_IMAGE_HEIGHT={os.environ.get("EDGE_WARMUP_IMAGE_HEIGHT", "640")} \\
EDGE_LISTEN_IP=0.0.0.0 \\
EDGE_LISTEN_PORT=9100 \\
python3 {EDGE_CONTAINER_SCRIPT}
"""

    return [
        "docker",
        "exec",
        EDGE_CONTAINER_NAME,
        "bash",
        "-lc",
        container_script,
    ]


def apply_edge_container_resources(cfg, run_dir):
    """Apply Stage 1 direct edge CPU-resource control using Docker update."""
    edge_cpus = cfg.get("edge_cpus", None)

    if edge_cpus is None or edge_cpus == "":
        print("[EdgeResource] No edge_cpus set; leaving edge container unchanged.")
        return

    edge_cpus = float(edge_cpus)
    print(f"[EdgeResource] Applying Docker CPU limit: {EDGE_CONTAINER_NAME} --cpus={edge_cpus}")

    result = subprocess.run(
        ["docker", "update", "--cpus", str(edge_cpus), EDGE_CONTAINER_NAME],
        text=True,
        capture_output=True,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Failed to update edge container CPU limit.\n"
            f"container={EDGE_CONTAINER_NAME}\n"
            f"edge_cpus={edge_cpus}\n"
            f"stdout={result.stdout}\n"
            f"stderr={result.stderr}"
        )

    inspect = subprocess.run(
        ["docker", "inspect", "-f", "{{.HostConfig.NanoCpus}}", EDGE_CONTAINER_NAME],
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
    print(f"[EdgeResource] Applied. Docker NanoCpus={nano_cpus}")


# =========================================================
# Main run logic
# =========================================================
def run_one_experiment(i, cfg):
    cfg = dict(cfg)
    cfg["run_id"] = make_run_id(i, cfg)

    run_dir = RESULTS_DIR / cfg["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)

    print("\n" + "#" * 90)
    print(f"[Run] {cfg['run_id']}")
    print(json.dumps(cfg, indent=2))
    print("#" * 90)

    factory_csv = run_dir / "factory_latency.csv"
    kpm_csv = run_dir / "orbit3c_kpm_log.csv"

    kpm_proc = None
    kpm_log = None
    edge_proc = None
    edge_log = None
    ue_proc = None
    ue_log = None
    factory_proc = None
    factory_log = None

    try:
        # 0. Clean before starting this run
        cleanup_old_processes()
        verify_no_old_host_processes()

        # 1. Write config for Isaac
        write_current_config(cfg, run_dir)

        # 2. Apply bandwidth
        apply_bandwidth_limit(cfg["bandwidth_mbit"])
        save_tc_state(run_dir)

        # 2b. Apply edge container CPU resource limit
        apply_edge_container_resources(cfg, run_dir)

        # 3. Start KPM logger
        kpm_env = os.environ.copy()
        kpm_env["ORBIT3C_KPM_CSV"] = str(kpm_csv)

        kpm_log_path = run_dir / "kpm_logger_stdout.txt"
        kpm_proc, kpm_log = start_process(
            [str(KPM_LOGGER)],
            kpm_log_path,
            env=kpm_env,
            cwd=FLEXRIC_DIR,
            sudo=False,
        )

        time.sleep(2)
        check_process_running(kpm_proc, "KPM logger", kpm_log_path)

        # 4. Start edge relay inside edge-dev
        edge_log_path = run_dir / "edge_stdout.txt"
        edge_cmd = build_edge_container_command(cfg)

        edge_proc, edge_log = start_process(
            edge_cmd,
            edge_log_path,
            env=os.environ.copy(),
            cwd=BASE_DIR,
            sudo=False,
        )

        time.sleep(2)
        check_process_running(edge_proc, "Edge relay in edge-dev", edge_log_path)
        wait_for_tcp_port(EDGE_CONTAINER_IP, 9100, timeout_sec=120)

        # 5. Start UE forwarder
        ue_env = os.environ.copy()
        ue_env["EDGE_IP"] = EDGE_CONTAINER_IP
        ue_env["EDGE_PORT"] = "9100"
        ue_env["UE_BIND_INTERFACE"] = OAI_TUN_IFACE
        ue_env["SOCKET_TIMEOUT_SEC"] = "120.0"

        ue_log_path = run_dir / "ue_forwarder_stdout.txt"
        ue_proc, ue_log = start_process(
            ["python3", str(UE_FWD_SCRIPT)],
            ue_log_path,
            env=ue_env,
            cwd=BASE_DIR,
            sudo=True,
        )

        time.sleep(1)
        check_process_running(ue_proc, "UE forwarder", ue_log_path)
        wait_for_tcp_port("127.0.0.1", 9200, timeout_sec=20)

        # 6. Start factory relay
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
        factory_proc, factory_log = start_process(
            ["python3", str(FACTORY_SCRIPT)],
            factory_log_path,
            env=factory_env,
            cwd=BASE_DIR,
            sudo=False,
        )

        time.sleep(2)
        check_process_running(factory_proc, "Factory relay", factory_log_path)

        # 7. Run Isaac manually for now
        #wait_for_user_to_run_isaac()
        
        request_isaac_run(cfg["run_id"], run_dir)
        wait_for_isaac_done(cfg["run_id"], timeout_sec=1800)

        # 8. Save OAI logs
        copy_if_exists(GNB_LOG, run_dir / "gnb_dev_rfsim_e2.log")
        copy_if_exists(UE_LOG, run_dir / "ue_dev_rfsim_e2.log")

        print(f"[Done] Saved run to: {run_dir}")

    finally:
        stop_process(factory_proc, factory_log, "factory relay")
        stop_process(ue_proc, ue_log, "UE forwarder")
        stop_process(edge_proc, edge_log, "edge relay in edge-dev")
        stop_process(kpm_proc, kpm_log, "KPM logger")

        reset_bandwidth_limit()

        # Strong cleanup after run
        cleanup_old_processes()

        try:
            verify_no_old_host_processes()
        except Exception as e:
            print(f"[Warn] Post-run cleanup check issue: {e}")


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    print("\nBefore starting, make sure these are already running:")
    print("  1. OAI core / edge-dev container")
    print("  2. nearRT-RIC")
    print("  3. E2-enabled gNB")
    print("  4. UE with oaitun_ue1")
    print("  5. Isaac is open")
    print("\nRun this script as normal user, not sudo:")
    print("  source /opt/ros/jazzy/setup.bash")
    print("  sudo -v")
    print("  sudo -n true && echo 'sudo cache works'")
    print("  python3 automated_experiments/scripts/codesign_experiment_runner_v1.py")

    input("\nPress ENTER to verify preconditions and start automated runs...")

    verify_preconditions()

    for i, cfg in enumerate(EXPERIMENTS, start=1):
        run_one_experiment(i, cfg)

    print("\nAll requested runs finished.")


if __name__ == "__main__":
    main()
