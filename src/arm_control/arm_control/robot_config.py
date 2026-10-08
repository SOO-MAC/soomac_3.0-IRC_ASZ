"""Shared arm/motor geometry and mapping; configurable values are in config/robot_config.yaml."""

from __future__ import annotations
import numpy as np
from ikpy.chain import Chain
from ikpy.link import DHLink, Link, OriginLink

from pathlib import Path
import yaml
from ament_index_python.packages import get_package_share_directory


def load_config(filename):
    try:
        folder = Path(get_package_share_directory("arm_control")) / "config"
    except LookupError:
        folder = Path(__file__).resolve().parents[1] / "config"
    with (folder / filename).open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


_ROBOT_PARAMETERS = load_config("robot_config.yaml")
DEG = np.pi / 180.0
TICKS_PER_REV = _ROBOT_PARAMETERS["TICKS_PER_REV"]
ASSUMED_START_TICKS = np.array(_ROBOT_PARAMETERS["ASSUMED_START_TICKS"], dtype=np.int64)
TASK2_START_TICKS = np.array(_ROBOT_PARAMETERS["TASK2_START_TICKS"], dtype=np.int64)
START_MAX_ABS_DELTA_TICKS = np.array(
    _ROBOT_PARAMETERS["START_MAX_ABS_DELTA_TICKS"], dtype=np.int64
)
DXL_CENTER_TICK = np.array(_ROBOT_PARAMETERS["DXL_CENTER_TICK"], dtype=float)
DXL_DIRECTION = np.ones(5, dtype=float)
GEAR_RATIO = np.array(_ROBOT_PARAMETERS["GEAR_RATIO"], dtype=float)
JOINT_ZERO_OFFSET_DEG = np.array(
    _ROBOT_PARAMETERS["JOINT_ZERO_OFFSET_DEG"], dtype=float
)
JOINT_ZERO_OFFSET_RAD = np.deg2rad(JOINT_ZERO_OFFSET_DEG)
JOINT_LIMIT_LOWER_DEG = np.array(
    _ROBOT_PARAMETERS["JOINT_LIMIT_LOWER_DEG"], dtype=float
)
JOINT_LIMIT_UPPER_DEG = np.array(
    _ROBOT_PARAMETERS["JOINT_LIMIT_UPPER_DEG"], dtype=float
)
JOINT_LIMIT_LOWER_RAD = np.deg2rad(JOINT_LIMIT_LOWER_DEG)
JOINT_LIMIT_UPPER_RAD = np.deg2rad(JOINT_LIMIT_UPPER_DEG)
J3_MIN_TICK = _ROBOT_PARAMETERS["J3_MIN_TICK"]
J3_MAX_TICK = _ROBOT_PARAMETERS["J3_MAX_TICK"]
J4_MIN_TICK = _ROBOT_PARAMETERS["J4_MIN_TICK"]
J4_MAX_TICK = _ROBOT_PARAMETERS["J4_MAX_TICK"]
WORLD_TO_MODEL_ROTATION = np.eye(3, dtype=float)
MODEL_TO_WORLD_ROTATION = WORLD_TO_MODEL_ROTATION.T
WORLD_DOWN = np.array(_ROBOT_PARAMETERS["WORLD_DOWN"], dtype=float)
TOOL_HORN_TO_GRASP_M = _ROBOT_PARAMETERS["TOOL_HORN_TO_GRASP_M"]
TOOL_HORN_TO_GRASP_MM = TOOL_HORN_TO_GRASP_M * 1000.0
ARM_IDS = tuple(_ROBOT_PARAMETERS["ARM_IDS"])
GRIPPER_ID = _ROBOT_PARAMETERS["GRIPPER_ID"]
DEVICENAME = _ROBOT_PARAMETERS["DEVICENAME"]
BAUDRATE = _ROBOT_PARAMETERS["BAUDRATE"]
PROTOCOL_VERSION = _ROBOT_PARAMETERS["PROTOCOL_VERSION"]
ADDR_OPERATING_MODE = _ROBOT_PARAMETERS["ADDR_OPERATING_MODE"]
ADDR_TORQUE_ENABLE = _ROBOT_PARAMETERS["ADDR_TORQUE_ENABLE"]
ADDR_PROFILE_VELOCITY = _ROBOT_PARAMETERS["ADDR_PROFILE_VELOCITY"]
ADDR_GOAL_POSITION = _ROBOT_PARAMETERS["ADDR_GOAL_POSITION"]
ADDR_PRESENT_POSITION = _ROBOT_PARAMETERS["ADDR_PRESENT_POSITION"]
TORQUE_DISABLE = _ROBOT_PARAMETERS["TORQUE_DISABLE"]
TORQUE_ENABLE = _ROBOT_PARAMETERS["TORQUE_ENABLE"]
OP_POSITION_CONTROL = _ROBOT_PARAMETERS["OP_POSITION_CONTROL"]
OP_EXTENDED_POSITION = _ROBOT_PARAMETERS["OP_EXTENDED_POSITION"]
ARM_PROFILE_VELOCITY = _ROBOT_PARAMETERS["ARM_PROFILE_VELOCITY"]
J2_PROFILE_VELOCITY_SCALE = _ROBOT_PARAMETERS["J2_PROFILE_VELOCITY_SCALE"]
J3_PROFILE_VELOCITY_SCALE = _ROBOT_PARAMETERS["J3_PROFILE_VELOCITY_SCALE"]
ARM_STREAM_DT_SEC = _ROBOT_PARAMETERS["ARM_STREAM_DT_SEC"]
ARM_WAYPOINT_TIMEOUT_SEC = _ROBOT_PARAMETERS["ARM_WAYPOINT_TIMEOUT_SEC"]
ARM_POSITION_THRESHOLDS = np.array(
    _ROBOT_PARAMETERS["ARM_POSITION_THRESHOLDS"], dtype=np.int64
)
ARM_ALIGN_THRESHOLDS = np.array(
    _ROBOT_PARAMETERS["ARM_ALIGN_THRESHOLDS"], dtype=np.int64
)
PHASE_MIN_COMMANDS = _ROBOT_PARAMETERS["PHASE_MIN_COMMANDS"]
PHASE_MAX_COMMANDS = _ROBOT_PARAMETERS["PHASE_MAX_COMMANDS"]
PHASE_MAX_TICK_STEP = np.array(_ROBOT_PARAMETERS["PHASE_MAX_TICK_STEP"], dtype=np.int64)
TRANSPORT_MIN_COMMANDS = _ROBOT_PARAMETERS["TRANSPORT_MIN_COMMANDS"]
TRANSPORT_MAX_COMMANDS = _ROBOT_PARAMETERS["TRANSPORT_MAX_COMMANDS"]
TRANSPORT_YAW_COMMANDS = _ROBOT_PARAMETERS["TRANSPORT_YAW_COMMANDS"]
ARM_READ_RETRIES = _ROBOT_PARAMETERS["ARM_READ_RETRIES"]
ARM_READ_RETRY_DT_SEC = _ROBOT_PARAMETERS["ARM_READ_RETRY_DT_SEC"]
START_PROFILE_VELOCITY = _ROBOT_PARAMETERS["START_PROFILE_VELOCITY"]
DIRECT_PROFILE_VELOCITY = _ROBOT_PARAMETERS["DIRECT_PROFILE_VELOCITY"]
DIRECT_MAX_TICK_STEP = _ROBOT_PARAMETERS["DIRECT_MAX_TICK_STEP"]
DIRECT_COMMAND_DT_SEC = _ROBOT_PARAMETERS["DIRECT_COMMAND_DT_SEC"]
DIRECT_TIMEOUT_SEC = _ROBOT_PARAMETERS["DIRECT_TIMEOUT_SEC"]
GRIPPER_PROFILE_VELOCITY = _ROBOT_PARAMETERS["GRIPPER_PROFILE_VELOCITY"]
GRIPPER_POSITION_THRESHOLD_TICK = _ROBOT_PARAMETERS["GRIPPER_POSITION_THRESHOLD_TICK"]
GRIPPER_MOVE_TIMEOUT_SEC = _ROBOT_PARAMETERS["GRIPPER_MOVE_TIMEOUT_SEC"]
GRIPPER_SAMPLE_DT_SEC = _ROBOT_PARAMETERS["GRIPPER_SAMPLE_DT_SEC"]
GRIPPER_SETTLE_SEC = _ROBOT_PARAMETERS["GRIPPER_SETTLE_SEC"]
GRIPPER_MIN_TICK = _ROBOT_PARAMETERS["GRIPPER_MIN_TICK"]
GRIPPER_MAX_TICK = _ROBOT_PARAMETERS["GRIPPER_MAX_TICK"]
DEFAULT_GRIPPER_OPEN_TICK = _ROBOT_PARAMETERS["DEFAULT_GRIPPER_OPEN_TICK"]


class FixedDHLink(DHLink):
    """Standard-DH link with explicit name and bounds initialization."""

    def __init__(
        self, name, d=0.0, a=0.0, alpha=0.0, theta=0.0, bounds=None, length=0.0
    ):
        Link.__init__(self, name=str(name), length=float(length), bounds=bounds)
        self.d = float(d)
        self.a = float(a)
        self.alpha = float(alpha)
        self.theta = float(theta)
        self.has_rotation = True
        self.has_translation = False
        self.joint_type = "revolute"


def create_robot_chain() -> Chain:
    return Chain(
        name="soomac_5dof_standard_dh",
        links=[
            OriginLink(),
            FixedDHLink(
                "J1",
                d=0.06125,
                alpha=-90.0 * DEG,
                bounds=(JOINT_LIMIT_LOWER_RAD[0], JOINT_LIMIT_UPPER_RAD[0]),
            ),
            FixedDHLink(
                "J2",
                a=0.25,
                theta=-90.0 * DEG,
                bounds=(JOINT_LIMIT_LOWER_RAD[1], JOINT_LIMIT_UPPER_RAD[1]),
            ),
            FixedDHLink(
                "J3",
                a=0.25,
                bounds=(JOINT_LIMIT_LOWER_RAD[2], JOINT_LIMIT_UPPER_RAD[2]),
            ),
            FixedDHLink(
                "J4",
                alpha=90.0 * DEG,
                theta=90.0 * DEG,
                bounds=(JOINT_LIMIT_LOWER_RAD[3], JOINT_LIMIT_UPPER_RAD[3]),
            ),
            FixedDHLink(
                "J5",
                d=0.129,
                bounds=(JOINT_LIMIT_LOWER_RAD[4], JOINT_LIMIT_UPPER_RAD[4]),
            ),
            FixedDHLink("terminal", bounds=(0.0, 0.0)),
        ],
        active_links_mask=[False, True, True, True, True, True, False],
    )


def model_q_to_ikpy_vector(q_model_rad: np.ndarray) -> np.ndarray:
    q_model_rad = np.asarray(q_model_rad, dtype=float).reshape(-1)
    if q_model_rad.size != 5:
        raise ValueError(f"q_model_rad needs 5 joints, got {q_model_rad.size}")
    return np.array([0.0, *q_model_rad.tolist(), 0.0], dtype=float)


def ikpy_vector_to_model_q(ikpy_angles: np.ndarray) -> np.ndarray:
    ikpy_angles = np.asarray(ikpy_angles, dtype=float).reshape(-1)
    if ikpy_angles.size != 7:
        raise ValueError(f"IKPy vector length must be 7, got {ikpy_angles.size}")
    return ikpy_angles[1:6].copy()


def ticks_to_model_rad(ticks: np.ndarray) -> np.ndarray:
    ticks = np.asarray(ticks, dtype=float).reshape(5)
    motor_rad = (
        (ticks - DXL_CENTER_TICK) * (2.0 * np.pi / TICKS_PER_REV) / DXL_DIRECTION
    )
    return motor_rad / GEAR_RATIO + JOINT_ZERO_OFFSET_RAD


def ticks_to_model_deg(ticks: np.ndarray) -> np.ndarray:
    return np.rad2deg(ticks_to_model_rad(ticks))


def validate_arm_ticks(ticks: np.ndarray) -> np.ndarray:
    """Validate one J1~J5 command against final global hardware limits."""
    ticks = np.asarray(ticks, dtype=np.int64).reshape(5)
    q_deg = np.asarray(ticks_to_model_deg(ticks), dtype=float)
    if np.any(q_deg < JOINT_LIMIT_LOWER_DEG - 1e-06) or np.any(
        q_deg > JOINT_LIMIT_UPPER_DEG + 1e-06
    ):
        raise ValueError(
            f"global joint limit exceeded | q={np.round(q_deg, 3).tolist()} | ticks={ticks.tolist()}"
        )
    if not J3_MIN_TICK <= int(ticks[2]) <= J3_MAX_TICK:
        raise ValueError(
            f"J3 raw tick limit exceeded | tick={int(ticks[2])} | allowed={J3_MIN_TICK}..{J3_MAX_TICK}"
        )
    if not J4_MIN_TICK <= int(ticks[3]) <= J4_MAX_TICK:
        raise ValueError(
            f"J4 raw tick limit exceeded | tick={int(ticks[3])} | allowed={J4_MIN_TICK}..{J4_MAX_TICK}"
        )
    return ticks


def model_deg_to_ticks(q_model_deg: np.ndarray) -> np.ndarray:
    q_model_deg = np.asarray(q_model_deg, dtype=float).reshape(5)
    if not np.all(np.isfinite(q_model_deg)):
        raise ValueError("joint angle contains NaN/inf")
    outside_limits = np.any(q_model_deg < JOINT_LIMIT_LOWER_DEG - 1e-06) or np.any(
        q_model_deg > JOINT_LIMIT_UPPER_DEG + 1e-06
    )
    if outside_limits:
        raise ValueError(f"joint limit exceeded: {np.round(q_model_deg, 3)}")
    motor_deg = (q_model_deg - JOINT_ZERO_OFFSET_DEG) * GEAR_RATIO
    ticks = DXL_CENTER_TICK + DXL_DIRECTION * motor_deg * TICKS_PER_REV / 360.0
    ticks = np.rint(ticks).astype(np.int64)
    validate_arm_ticks(ticks)
    return ticks


def get_start_q_model_rad() -> np.ndarray:
    return ticks_to_model_rad(ASSUMED_START_TICKS)


def get_start_q_model_deg() -> np.ndarray:
    return ticks_to_model_deg(ASSUMED_START_TICKS)


def world_xyz_to_model_xyz(world_xyz: np.ndarray) -> np.ndarray:
    xyz = np.asarray(world_xyz, dtype=float).reshape(3)
    return WORLD_TO_MODEL_ROTATION @ xyz


def model_xyz_to_world_xyz(model_xyz: np.ndarray) -> np.ndarray:
    xyz = np.asarray(model_xyz, dtype=float).reshape(3)
    return MODEL_TO_WORLD_ROTATION @ xyz


def normalize_vector(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=float).reshape(3)
    norm = float(np.linalg.norm(vector))
    if norm < 1e-12:
        raise ValueError("zero vector")
    return vector / norm


def wrap_to_180_deg(angle_deg: float) -> float:
    return float((float(angle_deg) + 180.0) % 360.0 - 180.0)


def calculate_vector_angle_deg(vector_a: np.ndarray, vector_b: np.ndarray) -> float:
    vector_a = normalize_vector(vector_a)
    vector_b = normalize_vector(vector_b)
    cosine = float(np.clip(np.dot(vector_a, vector_b), -1.0, 1.0))
    return float(np.rad2deg(np.arccos(cosine)))


def get_tool_axis(rotation: np.ndarray) -> np.ndarray:
    rotation = np.asarray(rotation, dtype=float).reshape(3, 3)
    return normalize_vector(rotation[:, 2])


def get_gripper_heading_axis(rotation: np.ndarray) -> np.ndarray:
    rotation = np.asarray(rotation, dtype=float).reshape(3, 3)
    return normalize_vector(-rotation[:, 0])


def projected_yaw_deg(heading_axis: np.ndarray) -> float:
    heading_axis = np.asarray(heading_axis, dtype=float).reshape(3)
    if np.linalg.norm(heading_axis[:2]) < 1e-10:
        return float("nan")
    return wrap_to_180_deg(np.rad2deg(np.arctan2(heading_axis[1], heading_axis[0])))


def calculate_tool_yaw_state(rotation: np.ndarray) -> dict:
    tool_axis = get_tool_axis(rotation)
    heading_axis = get_gripper_heading_axis(rotation)
    return {
        "tool_axis": tool_axis,
        "heading_axis": heading_axis,
        "tool_down_error_deg": calculate_vector_angle_deg(tool_axis, WORLD_DOWN),
        "yaw_deg": projected_yaw_deg(heading_axis),
    }


def calculate_yaw_error_deg(current_yaw_deg: float, target_yaw_deg: float) -> float:
    if not np.isfinite(current_yaw_deg):
        return float("inf")
    return wrap_to_180_deg(float(current_yaw_deg) - float(target_yaw_deg))


def yaw_consistent_q5_deg(q1_deg: float, target_yaw_deg: float) -> float:
    return wrap_to_180_deg(float(q1_deg) - float(target_yaw_deg))


def calculate_tool_down_yaw_from_q_deg(q_model_deg: np.ndarray) -> float:
    q_model_deg = np.asarray(q_model_deg, dtype=float).reshape(5)
    return wrap_to_180_deg(q_model_deg[0] - q_model_deg[4])


def world_yaw_to_model_yaw(world_yaw_deg: float) -> float:
    yaw_rad = np.deg2rad(float(world_yaw_deg))
    heading_world = np.array([np.cos(yaw_rad), np.sin(yaw_rad), 0.0])
    heading_model = WORLD_TO_MODEL_ROTATION @ heading_world
    return projected_yaw_deg(heading_model)


def model_yaw_to_world_yaw(model_yaw_deg: float) -> float:
    yaw_rad = np.deg2rad(float(model_yaw_deg))
    heading_model = np.array([np.cos(yaw_rad), np.sin(yaw_rad), 0.0])
    heading_world = MODEL_TO_WORLD_ROTATION @ heading_model
    return projected_yaw_deg(heading_world)


def create_target_yaw_axis(target_yaw_deg: float) -> np.ndarray:
    yaw_rad = np.deg2rad(float(target_yaw_deg))
    return np.array([np.cos(yaw_rad), np.sin(yaw_rad), 0.0], dtype=float)
