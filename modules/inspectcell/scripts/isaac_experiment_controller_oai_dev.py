import time
import json
import random
import traceback
from collections import defaultdict, Counter

import omni.usd
import omni.timeline
import omni.kit.app
import omni.kit.commands

import os
from pathlib import Path

from pxr import UsdGeom, Gf, Usd

# ---------------- ROS2 ----------------
import rclpy
from rclpy.node import Node
from std_msgs.msg import String


# =========================================================
# AUTOMATION FILE PATHS
# =========================================================
BASE_DIR = Path("/home/elahe/user/Co_design_codes")

EXPERIMENT_CONFIG_PATH = Path(
    os.getenv(
        "EXPERIMENT_CONFIG_PATH",
        str(BASE_DIR / "current_experiment_config.json"),
    )
)

ISAAC_COMMAND_PATH = Path(
    os.getenv(
        "ISAAC_COMMAND_PATH",
        str(BASE_DIR / "isaac_command.json"),
    )
)

ISAAC_STATUS_PATH = Path(
    os.getenv(
        "ISAAC_STATUS_PATH",
        str(BASE_DIR / "isaac_status.json"),
    )
)


def read_json_safely(path: Path):
    try:
        if not path.exists():
            return None
        with path.open("r") as f:
            return json.load(f)
    except Exception:
        return None


def write_status(run_id: str, status: str, message: str = ""):
    data = {
        "run_id": run_id,
        "status": status,
        "message": message,
        "time": time.time(),
    }
    ISAAC_STATUS_PATH.write_text(json.dumps(data, indent=2))
    print(f"[IsaacStatus] {status} run_id={run_id} message={message}")


def should_cancel_run(run_id: str) -> tuple[bool, str]:
    command = read_json_safely(ISAAC_COMMAND_PATH)
    if not command:
        return False, ""
    if command.get("command") != "cancel" or command.get("run_id") != run_id:
        return False, ""
    return True, command.get("reason", "cancel requested")


def load_experiment_config():
    if not EXPERIMENT_CONFIG_PATH.exists():
        print(f"[ExperimentConfig] No config file found at {EXPERIMENT_CONFIG_PATH}; using defaults.")
        return {}

    try:
        with EXPERIMENT_CONFIG_PATH.open("r") as f:
            cfg = json.load(f)
        print(f"[ExperimentConfig] Loaded config from {EXPERIMENT_CONFIG_PATH}")
        print(json.dumps(cfg, indent=2))
        return cfg
    except Exception as e:
        print(f"[ExperimentConfig] Failed to read config: {e}; using defaults.")
        return {}


# =========================================================
# CONFIG
# =========================================================
MODE = "inspect"
DATASET_TAG = "gc10_dataset_oai_dev"

GC10_CLASSES = [
    "punching_hole",
    "welding_line",
    "crescent_gap",
    "water_spot",
    "oil_spot",
    "silk_spot",
    "inclusion",
    "rolled_pit",
    "crease",
    "waist_folding",
]

DEFAULT_SPAWN_COUNT_PER_OBJECT = 1
MAX_REPLAY_PER_SOURCE_OBJECT = 20

SPAWN_PARENT = "/World/SpawnedCoco"
SPAWN_REF_PATH = "/World/Conveyor/SpawnStart"

GRAPH_PATHS = [
    "/World/Conveyor/ConveyorTrack/ConveyorBeltGraph",
    "/World/Conveyor/ConveyorTrack_01/ConveyorBeltGraph",
]

# These are updated before each run from current_experiment_config.json
RUN_SPEED = float(os.getenv("RUN_SPEED", "1.0"))
CAMERA_WIDTH = int(os.getenv("CAMERA_WIDTH", "1280"))
CAMERA_HEIGHT = int(os.getenv("CAMERA_HEIGHT", "720"))

RENDER_PRODUCT_NODE = os.getenv(
    "RENDER_PRODUCT_NODE",
    "/World/ActionGraph_01/isaac_create_render_product",
)

TARGET_SPACING_M = 1.0
MAX_ACTIVE_OBJECTS = 0
CAPTURE_LEAD_TIME_SEC = 0.0
MAX_OBJECTS = 0

CAPTURE_POINTS = {
    2: {"name": "p2", "x": -1.1, "ymin": -0.20, "ymax": 0.20},
}

ENABLED_CAPTURE_POINT_IDS = [2]

DELETE_X_MIN = 1.0
SORT_X_MIN = -0.1

REQUEST_TOPIC = "/fortuna_inspection/request"
RESULT_TOPIC = "/fortuna_inspection/result"
ACK_TOPIC = "/fortuna_inspection/ack"


# =========================================================
# GLOBALS
# =========================================================
stage = omni.usd.get_context().get_stage()
timeline = omni.timeline.get_timeline_interface()
app = omni.kit.app.get_app()

vel_attrs = []
ros_node = None
RUNNING = False

spawn_plan = []
spawn_index = 0
spawned_counts = Counter()
replay_counts = Counter()

obj_counter = 0
active_objects = []
last_spawn_time = None


# =========================================================
# SOURCE OBJECT DISCOVERY
# =========================================================
def build_gc10_scene_objects():
    objects = []

    world_prim = stage.GetPrimAtPath("/World")
    if not world_prim or not world_prim.IsValid():
        raise RuntimeError("'/World' prim not found.")

    for child in world_prim.GetChildren():
        name = child.GetName().lower()

        for cls_name in GC10_CLASSES:
            prefix = cls_name + "_"

            if name.startswith(prefix):
                suffix = name[len(prefix):]
                if suffix.isdigit():
                    objects.append(child.GetPath().pathString)
                break

    if not objects:
        raise RuntimeError(
            "No GC10 source objects found under /World. "
            "Expected names like /World/crease_1, /World/oil_spot_3, "
            "/World/punching_hole_5, etc."
        )

    def sort_key(path):
        name = path.split("/")[-1].lower()

        for class_idx, cls_name in enumerate(GC10_CLASSES):
            prefix = cls_name + "_"
            if name.startswith(prefix):
                suffix = name[len(prefix):]
                number = int(suffix) if suffix.isdigit() else 9999
                return class_idx, number

        return 9999, name

    objects.sort(key=sort_key)

    print(f"[Scene] Found {len(objects)} GC10 source objects:")
    for p in objects:
        print("   ", p)

    return objects


SCENE_OBJECTS = build_gc10_scene_objects()

SPAWN_COUNTS = {
    obj_path: DEFAULT_SPAWN_COUNT_PER_OBJECT
    for obj_path in SCENE_OBJECTS
}


def get_label_for_source(source_path: str) -> str:
    source_name = source_path.split("/")[-1].lower()

    for cls_name in GC10_CLASSES:
        if source_name == cls_name:
            return cls_name

        prefix = cls_name + "_"
        if source_name.startswith(prefix):
            suffix = source_name[len(prefix):]

            if suffix.isdigit():
                return cls_name

    return "unknown"


# =========================================================
# ROS NODE
# =========================================================
class IsaacGc10YoloControllerNode(Node):
    def __init__(self):
        super().__init__("isaac_gc10_yolo_controller")

        self.req_pub = self.create_publisher(String, REQUEST_TOPIC, 10)
        self.result_sub = self.create_subscription(String, RESULT_TOPIC, self.on_result, 10)
        self.ack_sub = self.create_subscription(String, ACK_TOPIC, self.on_ack, 10)

        self.results = defaultdict(dict)
        self.acks = defaultdict(dict)

    def publish_request(self, payload: dict):
        msg = String()
        msg.data = json.dumps(payload)
        self.req_pub.publish(msg)
        print(f"[ROS] Published request: {msg.data}")

    def on_result(self, msg: String):
        try:
            data = json.loads(msg.data)
            obj_id = data["obj_id"]
            point_id = int(data["point_id"])
            self.results[obj_id][point_id] = data
            print(f"[ROS] Result received: {data}")
        except Exception as e:
            print(f"[ROS] Failed to parse result: {e}")

    def on_ack(self, msg: String):
        try:
            data = json.loads(msg.data)
            obj_id = data["obj_id"]
            point_id = int(data["point_id"])
            self.acks[obj_id][point_id] = data
            print(f"[ROS] Ack received: {data}")
        except Exception as e:
            print(f"[ROS] Failed to parse ack: {e}")


# =========================================================
# OBJECT STATE
# =========================================================
class Gc10State:
    def __init__(self, prim_path: str, label: str, source_path: str, dataset_tag: str):
        self.prim_path = prim_path
        self.obj_id = prim_path.split("/")[-1]
        self.label = label
        self.source_path = source_path
        self.dataset_tag = dataset_tag

        self.spawn_time = time.time()
        self.spawn_x = None
        self.last_x = None
        self.last_y = None

        self.pending_points = list(ENABLED_CAPTURE_POINT_IDS)
        self.sent_requests = {}
        self.completed_points = {}

        self.final_decision = None
        self.decision_locked = False
        self.deleted = False

        self.capture_started = set()

    def all_points_done(self):
        return len(self.completed_points) == len(ENABLED_CAPTURE_POINT_IDS)


# =========================================================
# HELPERS
# =========================================================
def cleanup_existing_node():
    global ros_node
    try:
        if ros_node is not None:
            ros_node.destroy_node()
            ros_node = None
            print("[ROS] Old node destroyed.")
    except Exception as e:
        print(f"[ROS] cleanup warning: {e}")


def spin_ros_once():
    global ros_node
    try:
        if ros_node is not None:
            rclpy.spin_once(ros_node, timeout_sec=0.0)
    except Exception as e:
        print(f"[ROS] spin warning: {e}")


def ensure_xform(path: str):
    prim = stage.GetPrimAtPath(path)
    if prim and prim.IsValid():
        return prim

    stage.DefinePrim(path, "Xform")
    return stage.GetPrimAtPath(path)


def get_unique_child_path(parent_path: str, base_name: str) -> str:
    idx = 0
    while True:
        path = f"{parent_path}/{base_name}_{idx:04d}"
        prim = stage.GetPrimAtPath(path)
        if not prim or not prim.IsValid():
            return path
        idx += 1


def get_world_translation(prim_path: str):
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return None

    xformable = UsdGeom.Xformable(prim)
    world_mtx = xformable.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
    return world_mtx.ExtractTranslation()


def get_spawn_world_position():
    prim = stage.GetPrimAtPath(SPAWN_REF_PATH)
    if not prim or not prim.IsValid():
        raise RuntimeError(f"Spawn reference prim not found: {SPAWN_REF_PATH}")

    xformable = UsdGeom.Xformable(prim)
    world_mtx = xformable.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
    return world_mtx.ExtractTranslation()


def is_in_delete_zone(pos: Gf.Vec3d) -> bool:
    return pos[0] >= DELETE_X_MIN


def is_in_sort_zone(pos: Gf.Vec3d) -> bool:
    return pos[0] >= SORT_X_MIN


def remove_prim(path: str):
    prim = stage.GetPrimAtPath(path)
    if prim and prim.IsValid():
        omni.kit.commands.execute("DeletePrims", paths=[path])


def cleanup_spawned_parent():
    parent = stage.GetPrimAtPath(SPAWN_PARENT)
    if parent and parent.IsValid():
        print(f"[Cleanup] Removing old spawned parent: {SPAWN_PARENT}")
        omni.kit.commands.execute("DeletePrims", paths=[SPAWN_PARENT])
        for _ in range(3):
            app.update()


def verify_scene_objects():
    missing = []

    for path in SCENE_OBJECTS:
        prim = stage.GetPrimAtPath(path)
        if not prim or not prim.IsValid():
            missing.append(path)

    if missing:
        raise RuntimeError("These scene objects are missing:\n" + "\n".join(missing))

    print(f"[Verify] All {len(SCENE_OBJECTS)} source objects exist.")


def build_spawn_plan(shuffle_plan=True):
    global spawn_plan, MAX_OBJECTS

    plan = []

    for source_path, count in SPAWN_COUNTS.items():
        plan.extend([source_path] * count)

    if shuffle_plan:
        random.shuffle(plan)

    spawn_plan = plan
    MAX_OBJECTS = len(plan)

    print("[Plan] Spawn plan:")
    for p in spawn_plan:
        print("   ", p)

    print(f"[Plan] Total objects: {MAX_OBJECTS}")


def choose_next_source():
    global spawn_index

    if spawn_index >= len(spawn_plan):
        return None, None

    source_path = spawn_plan[spawn_index]
    spawn_index += 1

    label = get_label_for_source(source_path)

    return source_path, label


def has_crossed_target(prev_x: float, curr_x: float, target_x: float, moving_positive=True):
    if moving_positive:
        return prev_x < target_x <= curr_x

    return prev_x > target_x >= curr_x


# =========================================================
# CONVEYOR CONTROL
# =========================================================
def init_conveyors():
    global vel_attrs

    vel_attrs = []

    for graph_path in GRAPH_PATHS:
        prim = stage.GetPrimAtPath(graph_path)

        if not prim or not prim.IsValid():
            print(f"[Conveyor] Graph not found: {graph_path}")
            continue

        vel_attr = prim.GetAttribute("graph:variable:Velocity")

        if not vel_attr or not vel_attr.IsValid():
            print(f"[Conveyor] Velocity attr missing: {graph_path}")
            continue

        vel_attrs.append(vel_attr)
        print(f"[Conveyor] Found {graph_path} current velocity={vel_attr.Get()}")

    if not vel_attrs:
        raise RuntimeError("No valid conveyor velocity attributes found.")


def set_all_conveyors(speed: float):
    for attr in vel_attrs:
        attr.Set(speed)

    print(f"[Conveyor] Set all conveyors to {speed}")


def stop_all_conveyors():
    set_all_conveyors(0.0)


def start_all_conveyors():
    set_all_conveyors(RUN_SPEED)


# =========================================================
# SPAWNING
# =========================================================
def duplicate_gc10_to_spawn(source_path: str) -> str:
    ensure_xform(SPAWN_PARENT)

    src_prim = stage.GetPrimAtPath(source_path)
    if not src_prim or not src_prim.IsValid():
        raise RuntimeError(f"Source prim not found: {source_path}")

    source_name = source_path.split("/")[-1]
    spawned_counts[source_name] += 1
    copy_idx = spawned_counts[source_name]

    dst_name = f"{DATASET_TAG}_{source_name}_{copy_idx:04d}"
    dst_path = f"{SPAWN_PARENT}/{dst_name}"

    if stage.GetPrimAtPath(dst_path).IsValid():
        dst_path = get_unique_child_path(SPAWN_PARENT, f"{DATASET_TAG}_{source_name}")

    omni.kit.commands.execute(
        "CopyPrim",
        path_from=source_path,
        path_to=dst_path,
    )

    spawn_pos = get_spawn_world_position()

    prim = stage.GetPrimAtPath(dst_path)
    xform = UsdGeom.Xformable(prim)

    translate_op = None

    for op in xform.GetOrderedXformOps():
        if op.GetOpType() == UsdGeom.XformOp.TypeTranslate:
            translate_op = op
            break

    if translate_op is None:
        translate_op = xform.AddTranslateOp()

    translate_op.Set(spawn_pos)

    return dst_path


def spawn_next_object():
    global obj_counter, active_objects

    source_path, label = choose_next_source()

    if source_path is None:
        return False

    new_path = duplicate_gc10_to_spawn(source_path)
    obj_state = Gc10State(new_path, label, source_path, DATASET_TAG)

    obj_counter += 1

    pos = get_world_translation(new_path)

    if pos is not None:
        obj_state.spawn_x = float(pos[0])
        obj_state.last_x = float(pos[0])
        obj_state.last_y = float(pos[1])
    else:
        spawn_pos = get_spawn_world_position()
        obj_state.spawn_x = float(spawn_pos[0])
        obj_state.last_x = float(spawn_pos[0])
        obj_state.last_y = float(spawn_pos[1])

    active_objects.append(obj_state)

    print(
        f"[Spawn] {new_path} expected_class={label} "
        f"obj_counter={obj_counter} spawn_x={obj_state.spawn_x:.4f}"
    )

    return True


def maybe_spawn_object():
    global last_spawn_time

    if spawn_index >= len(spawn_plan):
        return

    if MAX_ACTIVE_OBJECTS > 0 and len(active_objects) >= MAX_ACTIVE_OBJECTS:
        return

    if len(active_objects) == 0:
        ok = spawn_next_object()

        if ok:
            last_spawn_time = time.time()

        return

    last_obj = active_objects[-1]
    pos = get_world_translation(last_obj.prim_path)

    if pos is None:
        return

    current_x = float(pos[0])
    spawn_x = float(last_obj.spawn_x)

    distance_from_spawn = abs(current_x - spawn_x)

    if distance_from_spawn >= TARGET_SPACING_M:
        ok = spawn_next_object()

        if ok:
            last_spawn_time = time.time()

            print(
                f"[DistanceSpawn] Spawned next object because previous object moved "
                f"{distance_from_spawn:.3f} m >= {TARGET_SPACING_M:.3f} m"
            )


# =========================================================
# REQUEST / DECISION
# =========================================================
def publish_capture_request(obj_state: Gc10State, point_id: int, extra_payload: dict = None):
    payload = {
        "mode": MODE,
        "dataset_tag": obj_state.dataset_tag,
        "obj_id": obj_state.obj_id,
        "label": obj_state.label,
        "source_path": obj_state.source_path,
        "part_name": obj_state.prim_path.split("/")[-1],
        "point_id": point_id,
        "point_name": CAPTURE_POINTS[point_id]["name"],
        "timestamp": time.time(),
    }

    if extra_payload:
        payload.update(extra_payload)

    ros_node.publish_request(payload)
    obj_state.sent_requests[point_id] = time.time()


def request_replay_for_object(obj_state: Gc10State, reason: str = ""):
    global spawn_plan, spawn_index, replay_counts

    replay_key = obj_state.source_path
    replay_counts[replay_key] += 1

    if replay_counts[replay_key] > MAX_REPLAY_PER_SOURCE_OBJECT:
        print(
            f"[ReplaySkip] Max replay reached for source={obj_state.source_path}. "
            f"obj={obj_state.obj_id} reason={reason}"
        )

        remove_prim(obj_state.prim_path)
        obj_state.deleted = True
        obj_state.final_decision = "replay_skipped_max_limit"
        obj_state.decision_locked = True

        return

    spawn_plan.insert(spawn_index, obj_state.source_path)

    print(
        f"[Replay] Requeued obj={obj_state.obj_id} "
        f"source={obj_state.source_path} label={obj_state.label} "
        f"replay_count={replay_counts[replay_key]} "
        f"reason={reason}"
    )

    remove_prim(obj_state.prim_path)
    obj_state.deleted = True
    obj_state.final_decision = "replay_required"
    obj_state.decision_locked = True


def try_update_results_from_ros(obj_state: Gc10State):
    obj_id = obj_state.obj_id

    if ros_node is None:
        return

    if obj_id not in ros_node.results:
        return

    for point_id, data in ros_node.results[obj_id].items():
        if point_id in obj_state.completed_points:
            continue

        status = data.get("status", "")
        failure_type = data.get("failure_type", "")

        if (
            status == "replay_required_partial_visibility"
            or failure_type == "partial_or_not_fully_visible"
            or status == "replay_required_no_fresh_image"
            or failure_type == "capture_no_fresh_image"
        ):
            print(
                f"[ReplayRequest] obj={obj_id} point={point_id} "
                f"status={status} failure_type={failure_type} "
                f"roi_status={data.get('roi_status', '')} "
                f"frame_pick_mode={data.get('frame_pick_mode', '')}"
            )

            request_replay_for_object(
                obj_state,
                reason=(
                    f"{status}|{failure_type}|"
                    f"{data.get('roi_status', '')}|"
                    f"{data.get('frame_pick_mode', '')}"
                ),
            )

            return

        obj_state.completed_points[point_id] = data

        print(
            f"[Result] obj={obj_id} point={point_id} "
            f"decision={data.get('decision', 'unknown')} "
            f"predicted={data.get('predicted_class', '')} "
            f"conf={data.get('predicted_confidence', 0.0)}"
        )


def combine_decisions(obj_state: Gc10State):
    decisions = [
        d.get("decision", "unknown")
        for d in obj_state.completed_points.values()
    ]

    if any(d == "defective" for d in decisions):
        return "defective"

    if len(decisions) == len(ENABLED_CAPTURE_POINT_IDS) and all(d == "healthy" for d in decisions):
        return "healthy"

    if MODE == "collect" and obj_state.all_points_done():
        return "collected"

    return "unknown"


def lock_final_decision_if_ready(obj_state: Gc10State, pos):
    if obj_state.decision_locked:
        return

    try_update_results_from_ros(obj_state)

    if obj_state.deleted:
        return

    if MODE == "collect":
        if obj_state.all_points_done():
            obj_state.final_decision = "collected"
            obj_state.decision_locked = True
            print(f"[Final] obj={obj_state.obj_id} => collected")

        return

    current_combined = combine_decisions(obj_state)

    if current_combined == "defective":
        obj_state.final_decision = "defective"
        obj_state.decision_locked = True
        print(f"[Final] obj={obj_state.obj_id} early defective")
        return

    if obj_state.all_points_done():
        obj_state.final_decision = current_combined
        obj_state.decision_locked = True
        print(f"[Final] obj={obj_state.obj_id} all points done => {obj_state.final_decision}")
        return

    if is_in_sort_zone(pos):
        obj_state.final_decision = "unknown"
        obj_state.decision_locked = True
        print(f"[Final] obj={obj_state.obj_id} timeout before sort => unknown")


# =========================================================
# MAIN OBJECT HANDLER
# =========================================================
def handle_object(obj_state: Gc10State):
    pos = get_world_translation(obj_state.prim_path)

    if pos is None:
        print(f"[Warn] Object disappeared: {obj_state.obj_id}")
        obj_state.deleted = True
        return

    x = float(pos[0])
    y = float(pos[1])

    try_update_results_from_ros(obj_state)

    if obj_state.deleted:
        return

    for point_id in list(obj_state.pending_points):
        if point_id in obj_state.capture_started:
            continue

        cfg = CAPTURE_POINTS[point_id]

        trigger_x = float(cfg["x"]) - (RUN_SPEED * CAPTURE_LEAD_TIME_SEC)

        prev_x = obj_state.last_x if obj_state.last_x is not None else x
        moving_positive = True

        in_y_band = cfg["ymin"] <= y <= cfg["ymax"]
        crossed = has_crossed_target(prev_x, x, trigger_x, moving_positive=moving_positive)

        if in_y_band and crossed:
            obj_state.capture_started.add(point_id)

            distance_to_sort = max(0.0, float(SORT_X_MIN) - x)
            time_to_sort_sec = distance_to_sort / float(RUN_SPEED) if RUN_SPEED > 0 else 0.0

            capture_payload_extra = {
                "actual_trigger_time": time.time(),
                "capture_x": x,
                "capture_y": y,
                "capture_target_x": float(cfg["x"]),
                "stable_ok": False,
                "moving_capture": True,
                "sort_distance_x": float(distance_to_sort),
                "time_to_sort_sec": float(time_to_sort_sec),
            }

            print(
                f"[CaptureTrigger] obj={obj_state.obj_id} point={point_id} "
                f"x={x:.4f} y={y:.4f} target_x={cfg['x']:.4f}"
            )

            publish_capture_request(obj_state, point_id, extra_payload=capture_payload_extra)
            obj_state.pending_points.remove(point_id)

    lock_final_decision_if_ready(obj_state, pos)

    if is_in_delete_zone(pos):
        print(f"[Delete] Removing {obj_state.prim_path} final_decision={obj_state.final_decision}")

        for point_id, data in obj_state.completed_points.items():
            print(
                f"[Summary] obj={obj_state.obj_id} point={point_id} "
                f"expected={obj_state.label} "
                f"predicted={data.get('predicted_class', '')} "
                f"conf={data.get('predicted_confidence', 0.0)} "
                f"inference_ms={data.get('inference_ms', None)} "
                f"decision={data.get('decision', 'unknown')}"
            )

        remove_prim(obj_state.prim_path)
        obj_state.deleted = True

    obj_state.last_x = x
    obj_state.last_y = y


def handle_all_objects():
    global active_objects

    for obj_state in list(active_objects):
        handle_object(obj_state)

    active_objects = [o for o in active_objects if not o.deleted]


# =========================================================
# RUN / STOP
# =========================================================
def reset_pipeline_state():
    global obj_counter
    global spawn_index
    global spawned_counts
    global replay_counts
    global active_objects
    global last_spawn_time

    obj_counter = 0
    spawn_index = 0
    spawned_counts = Counter()
    replay_counts = Counter()
    active_objects = []
    last_spawn_time = None

    cleanup_spawned_parent()


def set_camera_resolution(width: int, height: int):
    prim = stage.GetPrimAtPath(RENDER_PRODUCT_NODE)

    if not prim or not prim.IsValid():
        raise RuntimeError(f"Render product node not found: {RENDER_PRODUCT_NODE}")

    w_attr = prim.GetAttribute("inputs:width")
    h_attr = prim.GetAttribute("inputs:height")

    if not w_attr or not w_attr.IsValid():
        raise RuntimeError(f"inputs:width not found on {RENDER_PRODUCT_NODE}")

    if not h_attr or not h_attr.IsValid():
        raise RuntimeError(f"inputs:height not found on {RENDER_PRODUCT_NODE}")

    old_w = w_attr.Get()
    old_h = h_attr.Get()

    w_attr.Set(int(width))
    h_attr.Set(int(height))

    for _ in range(5):
        app.update()

    print(
        f"[CameraConfig] Resolution changed at {RENDER_PRODUCT_NODE}: "
        f"{old_w}x{old_h} -> {width}x{height}"
    )


def apply_experiment_config(config: dict):
    global RUN_SPEED, CAMERA_WIDTH, CAMERA_HEIGHT, RENDER_PRODUCT_NODE

    RUN_SPEED = float(config.get("run_speed", RUN_SPEED))
    CAMERA_WIDTH = int(config.get("camera_width", CAMERA_WIDTH))
    CAMERA_HEIGHT = int(config.get("camera_height", CAMERA_HEIGHT))

    if config.get("render_product_node"):
        RENDER_PRODUCT_NODE = config["render_product_node"]

    print("[ExperimentConfig] Applied:")
    print(f"  RUN_SPEED={RUN_SPEED}")
    print(f"  CAMERA_WIDTH={CAMERA_WIDTH}")
    print(f"  CAMERA_HEIGHT={CAMERA_HEIGHT}")
    print(f"  RENDER_PRODUCT_NODE={RENDER_PRODUCT_NODE}")


def run_pipeline(config: dict):
    global ros_node, RUNNING

    if RUNNING:
        print("[Warn] Pipeline is already running.")
        return

    RUNNING = True

    apply_experiment_config(config)

    cleanup_existing_node()
    reset_pipeline_state()
    set_camera_resolution(CAMERA_WIDTH, CAMERA_HEIGHT)
    verify_scene_objects()
    build_spawn_plan(shuffle_plan=True)

    ros_node = IsaacGc10YoloControllerNode()

    ensure_xform(SPAWN_PARENT)
    init_conveyors()

    if not timeline.is_playing():
        timeline.play()

    start_all_conveyors()

    try:
        while spawn_index < len(spawn_plan) or len(active_objects) > 0:
            app.update()
            spin_ros_once()

            cancel, reason = should_cancel_run(config.get("run_id", ""))
            if cancel:
                raise RuntimeError(f"Experiment cancelled by runner: {reason}")

            maybe_spawn_object()
            handle_all_objects()

    except Exception as e:
        print(f"[Error] {e}")
        raise

    finally:
        stop_all_conveyors()
        cleanup_existing_node()
        RUNNING = False
        print("[Done] Pipeline finished.")


# =========================================================
# AUTOMATION CONTROLLER LOOP
# =========================================================
def wait_for_command_loop():
    controller_start_time = time.time()

    print("=" * 80)
    print("[IsaacController] Started.")
    print(f"[IsaacController] Watching command file: {ISAAC_COMMAND_PATH}")
    print(f"[IsaacController] Reading config file:   {EXPERIMENT_CONFIG_PATH}")
    print(f"[IsaacController] Writing status file:   {ISAAC_STATUS_PATH}")
    print("=" * 80)

    last_command_key = None

    while True:
        app.update()

        command = read_json_safely(ISAAC_COMMAND_PATH)

        if not command:
            time.sleep(0.5)
            continue

        cmd = command.get("command")
        run_id = command.get("run_id")
        created_time = float(command.get("created_time", 0.0) or 0.0)

        if created_time and created_time < controller_start_time:
            time.sleep(0.5)
            continue

        if cmd != "start" or not run_id:
            time.sleep(0.5)
            continue

        command_key = (run_id, created_time)
        if command_key == last_command_key:
            time.sleep(0.5)
            continue

        last_command_key = command_key

        print("=" * 80)
        print(f"[IsaacController] New experiment requested: {run_id}")

        try:
            config = load_experiment_config()

            if not config:
                raise RuntimeError("Experiment config is empty or unreadable.")

            if config.get("run_id") != run_id:
                raise RuntimeError(
                    f"run_id mismatch: command={run_id}, config={config.get('run_id')}"
                )

            write_status(run_id, "running", "Isaac pipeline started.")
            run_pipeline(config)
            write_status(run_id, "done", "Isaac pipeline completed.")

        except Exception:
            err = traceback.format_exc()
            print(err)
            write_status(run_id, "error", err)

        print("=" * 80)


wait_for_command_loop()
