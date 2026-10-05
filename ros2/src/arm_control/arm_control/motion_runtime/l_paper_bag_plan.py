#!/usr/bin/env python3

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

import numpy as np
from scipy.optimize import least_squares


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))

from control_config import robot_config as cfg
from bag_handle_hook_study import pose_state


# ============================================================
# ROS services
# ============================================================

PATH_SERVICE = "/motor/move_tick_path"
OPEN_SERVICE = "/motor/cup_gripper_open"
CLOSE_SERVICE = "/motor/bag_gripper_hold"   # 현재 2000 tick


# ============================================================
# BAG 1 - 손잡이 없는 작은 종이봉투
# ============================================================

# 봉투 중심 위치
BAG_X_MM = 170.0

# base_link 기준 +Y=왼쪽으로 사용.
# 사용자 기준 오른쪽 150mm -> Y=-150mm
BAG_Y_MM = -150.0

# 이전 실측
BAG_HEIGHT_MM = 282.0
BAG_WIDTH_MM = 150.0
BAG_DEPTH_MM = 89.0


# ============================================================
# GRIPPER / TOOL
# ============================================================

# J5축 -> finger 끝
PICK_AXIAL_OFFSET_MM = 136.0
CARRY_AXIAL_OFFSET_MM = 0.0

# L_paper_bag 전용 최종 IK limits.
# 전역 robot_config는 수정하지 않는다.
L_PAPER_LIMIT_LOWER_DEG = cfg.JOINT_LIMIT_LOWER_DEG

L_PAPER_LIMIT_UPPER_DEG = cfg.JOINT_LIMIT_UPPER_DEG

# finger 벌림 방향이 수평이 되도록
J5_DEG = 90.0

GRIPPER_OPEN_TICK = 85
GRIPPER_CLOSE_TICK = 2048


# ============================================================
# PICK
# ============================================================

# 완전 90도 tool-down보다 관절 여유가 좋은 값.
# 봉투 위에서 입구를 닫듯이 잡는 자세.
PREP_TILT_DEG = 50.0
PICK_TILT_DEG = 90.0

# 봉투 윗면에서 finger tip이 얼마나 아래까지 내려갈지
GRASP_DEPTH_MM = 80.0

GRASP_Z_MM = 202.0

# GRASP 전에 finger tip이 봉투 위 20mm
READY_Z_MM = 252.0

# 처음 자세 회전을 완성할 안전한 안쪽 위치
PREP_RADIUS_MM = 250.0
PREP_Z_MM = 320.0


# ============================================================
# AFTER GRASP
# ============================================================

# 잡고 같은 XY에서 먼저 올림
LIFT_Z_MM = 220.0

# 운반 자세로 들어가면서 안쪽으로 당김
CARRY_RADIUS_MM = 300.0
CARRY_Z_MM = 300.0

# 이후 뻗을 때는 이전 손잡이 봉투와 비슷하게
# 좀 더 눕혀서 reach 확보
CARRY_TILT_DEG = PICK_TILT_DEG  # grasp 이후 50deg 계속 유지

FINAL_RADII_MM = [
    350.0,
    375.0,
    400.0,
]


NORMAL_VELOCITY = 8
TURN_VELOCITY = 15

START_TOL = np.array(
    [80, 120, 180, 120, 60],
    dtype=np.int64,
)


# ============================================================
# Geometry
# ============================================================

PICK_RADIUS_MM = float(
    np.hypot(
        BAG_X_MM,
        BAG_Y_MM,
    )
)

PICK_AZ_DEG = float(
    np.rad2deg(
        np.arctan2(
            BAG_Y_MM,
            BAG_X_MM,
        )
    )
)


def unit(v):
    v = np.asarray(v, dtype=float)
    return v / np.linalg.norm(v)


def tool_axis_for(
    az_deg,
    tilt_deg,
):
    """
    radial outward + downward tool axis.

    tilt=0   : horizontal
    tilt=90  : vertical down
    """
    az = np.deg2rad(
        float(az_deg)
    )

    tilt = np.deg2rad(
        float(tilt_deg)
    )

    return unit(
        np.array(
            [
                np.cos(tilt) * np.cos(az),
                np.cos(tilt) * np.sin(az),
                -np.sin(tilt),
            ],
            dtype=float,
        )
    )


def tip_target(
    radius_mm,
    az_deg,
    z_mm,
):
    az = np.deg2rad(
        float(az_deg)
    )

    return np.array(
        [
            radius_mm * np.cos(az),
            radius_mm * np.sin(az),
            z_mm,
        ],
        dtype=float,
    )


# ============================================================
# IK
# ============================================================

def solve_pose(
    chain,
    label,
    radius_mm,
    az_deg,
    tip_z_mm,
    tilt_deg,
    seed_q=None,
    axial_offset_mm=PICK_AXIAL_OFFSET_MM,
):
    """
    중앙 finger-tip 위치 + tool tilt 고정.

    J1 = target azimuth
    J5 = 90 deg

    J2/J3/J4를 IK로 계산.
    """

    target_tip = tip_target(
        radius_mm,
        az_deg,
        tip_z_mm,
    )

    desired_tool = tool_axis_for(
        az_deg,
        tilt_deg,
    )

    model_target = (
        target_tip
        - float(axial_offset_mm)
        * desired_tool
    )

    j1_deg = float(
        az_deg
    )

    target_sum_deg = (
        90.0
        + float(tilt_deg)
    )

    if seed_q is None:
        start_q = cfg.ticks_to_model_deg(
            cfg.ASSUMED_START_TICKS
        )

        seed = start_q[1:4].copy()

    else:
        seed = np.asarray(
            seed_q,
            dtype=float,
        )[1:4].copy()

    lo = L_PAPER_LIMIT_LOWER_DEG[1:4].copy()
    hi = L_PAPER_LIMIT_UPPER_DEG[1:4].copy()

    seed = np.clip(
        seed,
        lo + 1e-5,
        hi - 1e-5,
    )

    def residual(v):

        q = np.array(
            [
                j1_deg,
                v[0],
                v[1],
                v[2],
                J5_DEG,
            ],
            dtype=float,
        )

        state = pose_state(
            chain,
            np.deg2rad(q),
        )

        xyz_error = (
            np.asarray(
                state["xyz"],
                dtype=float,
            )
            - model_target
        )

        sum_error = (
            np.sum(v)
            - target_sum_deg
        ) * 10.0

        return np.concatenate(
            [
                xyz_error,
                [sum_error],
            ]
        )

    result = least_squares(
        residual,
        seed,
        bounds=(
            lo + 1e-6,
            hi - 1e-6,
        ),
        max_nfev=5000,
        ftol=1e-12,
        xtol=1e-12,
        gtol=1e-12,
    )

    q = np.array(
        [
            j1_deg,
            result.x[0],
            result.x[1],
            result.x[2],
            J5_DEG,
        ],
        dtype=float,
    )

    state = pose_state(
        chain,
        np.deg2rad(q),
    )

    actual_tool = unit(
        state["tool_axis"]
    )

    actual_tip = (
        np.asarray(
            state["xyz"],
            dtype=float,
        )
        + float(axial_offset_mm)
        * actual_tool
    )

    tip_error = float(
        np.linalg.norm(
            actual_tip
            - target_tip
        )
    )

    axis_dot = np.clip(
        np.dot(
            actual_tool,
            desired_tool,
        ),
        -1.0,
        1.0,
    )

    axis_error = float(
        np.rad2deg(
            np.arccos(
                axis_dot
            )
        )
    )

    margin = np.minimum(
        q - L_PAPER_LIMIT_LOWER_DEG,
        L_PAPER_LIMIT_UPPER_DEG - q,
    )

    if tip_error > 2.0:
        raise RuntimeError(
            f"{label}: tip IK error "
            f"{tip_error:.3f}mm"
        )

    if axis_error > 1.0:
        raise RuntimeError(
            f"{label}: tool axis error "
            f"{axis_error:.3f}deg"
        )

    if np.any(margin < 0.0):
        raise RuntimeError(
            f"{label}: joint limit exceeded | "
            f"q={np.round(q, 3).tolist()}"
        )

    ticks = cfg.model_deg_to_ticks(
        q
    )

    return {
        "label": label,
        "q": q,
        "ticks": np.asarray(
            ticks,
            dtype=np.int64,
        ),
        "tip": actual_tip,
        "tool": actual_tool,
        "finger_x": np.asarray(
            state["finger_x"],
            dtype=float,
        ),
        "margin": margin,
        "tip_error": tip_error,
    }


# ============================================================
# Build full plan
# ============================================================

def interpolate(
    a,
    b,
    count,
):
    return np.linspace(
        float(a),
        float(b),
        int(count),
    )


def build_plan():

    chain = cfg.create_robot_chain()

    # --------------------------------------------------------
    # PREP
    # --------------------------------------------------------

    prep = solve_pose(
        chain,
        "PREP",
        PREP_RADIUS_MM,
        PICK_AZ_DEG,
        PREP_Z_MM,
        PREP_TILT_DEG,
    )

    seed = prep["q"]

    # --------------------------------------------------------
    # PREP -> READY
    #
    # 봉투 위로 가기 전에 이미 50deg 자세 완성.
    # finger tip은 봉투보다 위에 유지.
    # --------------------------------------------------------

    ready_path = []

    for i, alpha in enumerate(
        np.linspace(
            0.0,
            1.0,
            7,
        )[1:],
        start=1,
    ):

        radius = (
            PREP_RADIUS_MM
            + alpha
            * (
                PICK_RADIUS_MM
                - PREP_RADIUS_MM
            )
        )

        z = (
            PREP_Z_MM
            + alpha
            * (
                READY_Z_MM
                - PREP_Z_MM
            )
        )

        tilt = (
            PREP_TILT_DEG
            + alpha
            * (
                PICK_TILT_DEG
                - PREP_TILT_DEG
            )
        )

        point = solve_pose(
            chain,
            f"READY_{i}",
            radius,
            PICK_AZ_DEG,
            z,
            tilt,
            seed,
        )

        ready_path.append(
            point
        )

        seed = point["q"]


    # --------------------------------------------------------
    # READY -> GRASP
    #
    # 여기서는 XY/각도 고정.
    # 위에서 아래로만 내려간다.
    # --------------------------------------------------------

    descend_path = []

    for i, z in enumerate(
        interpolate(
            READY_Z_MM,
            GRASP_Z_MM,
            7,
        )[1:],
        start=1,
    ):

        point = solve_pose(
            chain,
            f"DESCEND_{i}",
            PICK_RADIUS_MM,
            PICK_AZ_DEG,
            z,
            PICK_TILT_DEG,
            seed,
        )

        descend_path.append(
            point
        )

        seed = point["q"]


    grasp = descend_path[-1]


    # --------------------------------------------------------
    # GRASP -> LIFT
    # --------------------------------------------------------

    lift_path = []

    for i, z in enumerate(
        interpolate(
            GRASP_Z_MM,
            LIFT_Z_MM,
            7,
        )[1:],
        start=1,
    ):

        point = solve_pose(
            chain,
            f"LIFT_{i}",
            PICK_RADIUS_MM,
            PICK_AZ_DEG,
            z,
            PICK_TILT_DEG,
            seed,
        )

        lift_path.append(
            point
        )

        seed = point["q"]


    # --------------------------------------------------------
    # LIFT -> CARRY
    #
    # 안쪽으로 당기며
    # tilt 50 -> 20 deg
    # z 300 -> 320
    # --------------------------------------------------------

    carry_path = []

    for i, alpha in enumerate(
        np.linspace(
            0.0,
            1.0,
            7,
        )[1:],
        start=1,
    ):

        radius = (
            PICK_RADIUS_MM
            + alpha
            * (
                CARRY_RADIUS_MM
                - PICK_RADIUS_MM
            )
        )

        z = (
            LIFT_Z_MM
            + alpha
            * (
                CARRY_Z_MM
                - LIFT_Z_MM
            )
        )

        tilt = (
            PICK_TILT_DEG
            + alpha
            * (
                CARRY_TILT_DEG
                - PICK_TILT_DEG
            )
        )

        point = solve_pose(
            chain,
            f"CARRY_{i}",
            radius,
            PICK_AZ_DEG,
            z,
            tilt,
            seed,
            axial_offset_mm=CARRY_AXIAL_OFFSET_MM,
        )

        carry_path.append(
            point
        )

        seed = point["q"]


    carry = carry_path[-1]


    # --------------------------------------------------------
    # TURN
    #
    # 기존 손잡이 봉투와 동일하게
    # 준비방향 기준 반대편 +/-180 중 가까운 쪽.
    # --------------------------------------------------------

    current_j1 = float(
        carry["q"][0]
    )

    target_j1 = min(
        (-180.0, 180.0),
        key=lambda angle:
            abs(angle - current_j1),
    )

    turn_q = carry["q"].copy()
    turn_q[0] = target_j1

    turn_ticks = cfg.model_deg_to_ticks(
        turn_q
    )

    turn = {
        "label": "TURN",
        "q": turn_q,
        "ticks": np.asarray(
            turn_ticks,
            dtype=np.int64,
        ),
    }


    # --------------------------------------------------------
    # EXTEND
    #
    # tool tilt=20 유지
    # Z=320 유지
    # radius -> 600
    # --------------------------------------------------------

    extend_path = []

    seed = turn_q.copy()

    for radius in FINAL_RADII_MM:

        point = solve_pose(
            chain,
            f"EXTEND_{int(radius)}",
            radius,
            target_j1,
            CARRY_Z_MM,
            CARRY_TILT_DEG,
            seed,
            axial_offset_mm=CARRY_AXIAL_OFFSET_MM,
        )

        extend_path.append(
            point
        )

        seed = point["q"]


    return {
        "prep": prep,
        "ready": ready_path,
        "descend": descend_path,
        "grasp": grasp,
        "lift": lift_path,
        "carry": carry_path,
        "turn": turn,
        "extend": extend_path,
        "target_j1": target_j1,
    }


