#!/usr/bin/env python3

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

sys.path.insert(
    0,
    str(ROOT / "src" / "control_config"),
)
sys.path.insert(
    0,
    str(ROOT / "src"),
)
sys.path.insert(
    0,
    str(HERE),
)

import bag_tooldown_family_core as core


# ============================================================
# CUP geometry
# ============================================================

# 검증된 Z105 CUP 기준
CUP_GRASP_Z_MM = 105.0

# 잡은 뒤 올리는 높이
CUP_LIFT_Z_MM = 185.0

# 컵 접근 전에 위쪽에서 내려오는 높이
CUP_SAFE_Z_MM = 300.0

# 컵 중심에서 로봇 쪽으로 120 mm 떨어진 접근선
APPROACH_OFFSET_MM = 120.0

# 잡은 뒤 로봇 쪽으로 100 mm pull
PULL_MM = 100.0

# 예전 CUP 계산에서 사용한
# J5 model origin -> cup finger contact TCP
CUP_TCP_OFFSET_MM = 101.0

# CUP에서는 wrist roll 금지
CUP_J5_DEG = 0.0
CUP_J5_TICK = 2048

CUP_TILT_DEG = 0.0

TCP_ERROR_MAX_MM = 1.0


# ============================================================
# 기존 실물 검증 경로의 기준점
#
# reference X,Y = (80,500) mm
# ============================================================

REFERENCE_X_MM = 80.0
REFERENCE_Y_MM = 500.0

REFERENCE = {
    "SAFE":
        np.array(
            [2871, 798, 8328, 1975, 2048],
            dtype=np.int64,
        ),

    "D120_Z105":
        np.array(
            [2871, 2237, 10132, 1326, 2048],
            dtype=np.int64,
        ),

    "GRASP":
        np.array(
            [2871, 3472, 8416, 1422, 2048],
            dtype=np.int64,
        ),

    "LIFT":
        np.array(
            [2871, 2796, 8047, 1632, 2048],
            dtype=np.int64,
        ),

    "PULL100":
        np.array(
            [2871, 1506, 9384, 1622, 2048],
            dtype=np.int64,
        ),
}


# ============================================================
# Geometry
# ============================================================

def radial_data(x_mm, y_mm):
    x = float(x_mm)
    y = float(y_mm)

    if not (
        math.isfinite(x)
        and math.isfinite(y)
    ):
        raise RuntimeError(
            "CUP XY contains NaN/inf"
        )

    radius = math.hypot(
        x,
        y,
    )

    if x <= 0.0:
        raise RuntimeError(
            f"CUP must be in front workspace | X={x:.1f}"
        )

    if radius <= (
        APPROACH_OFFSET_MM
        + 1.0
    ):
        raise RuntimeError(
            "CUP too close for D120 approach | "
            f"R={radius:.1f} mm"
        )

    ux = x / radius
    uy = y / radius

    angle_deg = math.degrees(
        math.atan2(
            y,
            x,
        )
    )

    return (
        radius,
        ux,
        uy,
        angle_deg,
    )


def target_at_d(
    x_mm,
    y_mm,
    d_mm,
    z_mm,
):
    radius, ux, uy, _ = (
        radial_data(
            x_mm,
            y_mm,
        )
    )

    d = float(d_mm)

    if d >= radius:
        raise RuntimeError(
            f"D={d:.1f} exceeds radius={radius:.1f}"
        )

    return np.array([
        float(x_mm) - ux * d,
        float(y_mm) - uy * d,
        float(z_mm),
    ])


# ============================================================
# FK / TCP
# ============================================================

def cup_tcp_from_q(
    chain,
    q_deg,
):
    q_deg = np.asarray(
        q_deg,
        dtype=float,
    ).reshape(5)

    state = core.pose_state(
        chain,
        np.deg2rad(q_deg),
    )

    axis = core.unit(
        state["tool_axis"]
    )

    tcp = (
        np.asarray(
            state["xyz"],
            dtype=float,
        )
        + CUP_TCP_OFFSET_MM * axis
    )

    return (
        tcp,
        axis,
    )


# ============================================================
# CUP IK
# ============================================================

def solve_cup_tcp(
    chain,
    name,
    target,
    seed_q,
    angle_deg,
):
    """
    Existing core.solve_tcp()를 그대로 사용하되
    CUP에서 검증했던 TCP offset 101 mm만 적용.

    계산 후:
      J1 = target radial direction
      J5 = 0 deg / 2048
    """

    target = np.asarray(
        target,
        dtype=float,
    ).reshape(3)

    seed_q = np.asarray(
        seed_q,
        dtype=float,
    ).reshape(5).copy()

    seed_q[0] = float(angle_deg)
    seed_q[4] = CUP_J5_DEG

    # --------------------------------------------------------
    # core.solve_tcp()는 AXIAL_OFFSET_MM를 이용하므로
    # CUP 계산 중에만 101 mm로 교체.
    # 반드시 finally에서 원상복구.
    # --------------------------------------------------------

    original_offset = float(
        core.AXIAL_OFFSET_MM
    )

    try:
        core.AXIAL_OFFSET_MM = (
            CUP_TCP_OFFSET_MM
        )

        try:
            solved = core.solve_tcp(
                chain,
                target,
                CUP_TILT_DEG,
                seed_q,
            )
        except Exception as e:
            raise RuntimeError(
                f"{name}: IK solve failed | "
                f"target={np.round(target, 2).tolist()} | "
                f"seed_q={np.round(seed_q, 3).tolist()} | "
                f"{type(e).__name__}: {e}"
            ) from e

    finally:
        core.AXIAL_OFFSET_MM = (
            original_offset
        )

    q = np.asarray(
        solved["q"],
        dtype=float,
    ).copy()

    # radial direction 고정
    q[0] = float(angle_deg)

    # CUP wrist roll 고정
    q[4] = CUP_J5_DEG

    ticks = np.asarray(
        core.cfg.model_deg_to_ticks(q),
        dtype=np.int64,
    )

    ticks[4] = CUP_J5_TICK

    # 최신 robot_config:
    # J3 0~10606
    # J4 700~3500
    core.cfg.validate_arm_ticks(
        ticks
    )

    tcp, axis = cup_tcp_from_q(
        chain,
        q,
    )

    error = float(
        np.linalg.norm(
            tcp - target
        )
    )

    if error > TCP_ERROR_MAX_MM:
        raise RuntimeError(
            f"{name}: TCP error "
            f"{error:.3f} mm > "
            f"{TCP_ERROR_MAX_MM:.3f} mm"
        )

    # tool horizontal 확인
    if abs(float(axis[2])) > 0.03:
        raise RuntimeError(
            f"{name}: CUP tool not horizontal | "
            f"axis={np.round(axis,5).tolist()}"
        )

    _, ux, uy, _ = (
        radial_data(
            target[0],
            target[1],
        )
    )

    wanted_axis = np.array(
        [ux, uy, 0.0],
        dtype=float,
    )

    direction_dot = float(
        np.dot(
            axis,
            wanted_axis,
        )
    )

    if direction_dot < 0.98:
        raise RuntimeError(
            f"{name}: tool direction mismatch | "
            f"dot={direction_dot:.4f} | "
            f"axis={np.round(axis,5).tolist()} | "
            f"wanted={np.round(wanted_axis,5).tolist()}"
        )

    return {
        "name":
            name,

        "target":
            target.copy(),

        "q":
            q,

        "ticks":
            ticks,

        "tcp":
            tcp,

        "tcp_error":
            error,

        "axis":
            axis,
    }


# ============================================================
# Plan
# ============================================================

def build_dynamic_cup_pick_plan(
    x_mm,
    y_mm,
):
    (
        radius,
        ux,
        uy,
        angle_deg,
    ) = radial_data(
        x_mm,
        y_mm,
    )

    chain = (
        core.spp.create_robot_chain()
    )

    # 기존 검증 SAFE 자세를 첫 seed로 사용하되
    # J1은 현재 검출 방향으로 변경
    seed_q = np.asarray(
        core.cfg.ticks_to_model_deg(
            REFERENCE["SAFE"]
        ),
        dtype=float,
    )

    seed_q[0] = angle_deg
    seed_q[4] = CUP_J5_DEG

    plan = {}

    # --------------------------------------------------------
    # SAFE = D120 / Z300
    # --------------------------------------------------------

    safe_target = target_at_d(
        x_mm,
        y_mm,
        APPROACH_OFFSET_MM,
        CUP_SAFE_Z_MM,
    )

    plan["SAFE"] = solve_cup_tcp(
        chain,
        "SAFE",
        safe_target,
        seed_q,
        angle_deg,
    )

    seed_q = (
        plan["SAFE"]["q"]
        .copy()
    )

    # --------------------------------------------------------
    # DESCEND
    #
    # old successful Z sequence 그대로
    # --------------------------------------------------------

    descend = []

    for z in (
        280.0,
        260.0,
        240.0,
        220.0,
        200.0,
        180.0,
        160.0,
        140.0,
        135.0,
        125.0,
        115.0,
        105.0,
    ):
        target = target_at_d(
            x_mm,
            y_mm,
            APPROACH_OFFSET_MM,
            z,
        )

        p = solve_cup_tcp(
            chain,
            f"D120_Z{z:.0f}",
            target,
            seed_q,
            angle_deg,
        )

        descend.append(p)

        seed_q = p["q"].copy()

    plan["DESCEND"] = descend

    # --------------------------------------------------------
    # APPROACH
    #
    # Z105 유지
    # D120 -> D0
    # --------------------------------------------------------

    approach = []

    for d in (
        100.0,
        80.0,
        60.0,
        40.0,
        35.0,
        30.0,
        25.0,
        20.0,
        15.0,
        10.0,
        5.0,
        0.0,
    ):
        target = target_at_d(
            x_mm,
            y_mm,
            d,
            CUP_GRASP_Z_MM,
        )

        p = solve_cup_tcp(
            chain,
            f"D{d:.0f}_Z105",
            target,
            seed_q,
            angle_deg,
        )

        approach.append(p)

        seed_q = p["q"].copy()

    plan["APPROACH"] = approach

    # --------------------------------------------------------
    # LIFT
    #
    # D0 / Z105 -> Z185
    # --------------------------------------------------------

    lift = []

    for z in (
        125.0,
        145.0,
        165.0,
        185.0,
    ):
        target = target_at_d(
            x_mm,
            y_mm,
            0.0,
            z,
        )

        p = solve_cup_tcp(
            chain,
            f"LIFT_Z{z:.0f}",
            target,
            seed_q,
            angle_deg,
        )

        lift.append(p)

        seed_q = p["q"].copy()

    plan["LIFT"] = lift

    # --------------------------------------------------------
    # PULL
    #
    # 컵을 든 상태로 radial inward
    # D0 -> D100
    # --------------------------------------------------------

    pull = []

    for d in (
        20.0,
        40.0,
        60.0,
        80.0,
        100.0,
    ):
        target = target_at_d(
            x_mm,
            y_mm,
            d,
            CUP_LIFT_Z_MM,
        )

        p = solve_cup_tcp(
            chain,
            f"PULL_D{d:.0f}",
            target,
            seed_q,
            angle_deg,
        )

        pull.append(p)

        seed_q = p["q"].copy()

    plan["PULL"] = pull

    return {
        "x_mm":
            float(x_mm),

        "y_mm":
            float(y_mm),

        "radius_mm":
            float(radius),

        "angle_deg":
            float(angle_deg),

        "plan":
            plan,
    }


# ============================================================
# Print
# ============================================================

def show_point(
    label,
    p,
):
    print(
        f"{label:12s} | "
        f"target="
        f"{np.round(p['target'],2).tolist()} | "
        f"ticks="
        f"{p['ticks'].astype(int).tolist()} | "
        f"err="
        f"{p['tcp_error']:.4f} mm"
    )


def compare_reference(result):
    x = float(
        result["x_mm"]
    )

    y = float(
        result["y_mm"]
    )

    if (
        abs(x - REFERENCE_X_MM) > 1.0e-6
        or
        abs(y - REFERENCE_Y_MM) > 1.0e-6
    ):
        return

    plan = result["plan"]

    generated = {
        "SAFE":
            plan["SAFE"]["ticks"],

        "D120_Z105":
            plan["DESCEND"][-1]["ticks"],

        "GRASP":
            plan["APPROACH"][-1]["ticks"],

        "LIFT":
            plan["LIFT"][-1]["ticks"],

        "PULL100":
            plan["PULL"][-1]["ticks"],
    }

    print()
    print("=" * 90)
    print("REFERENCE COMPARISON")
    print(
        "old physically-verified "
        "CUP Z105 @ XY=(80,500)"
    )
    print("=" * 90)

    for name in (
        "SAFE",
        "D120_Z105",
        "GRASP",
        "LIFT",
        "PULL100",
    ):
        old = (
            REFERENCE[name]
            .astype(np.int64)
        )

        new = np.asarray(
            generated[name],
            dtype=np.int64,
        )

        diff = new - old

        print(
            f"{name:10s} "
            f"old={old.tolist()} | "
            f"new={new.tolist()} | "
            f"diff={diff.tolist()} | "
            f"max={int(np.max(np.abs(diff)))}"
        )

    print("=" * 90)


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--x",
        type=float,
        required=True,
    )

    parser.add_argument(
        "--y",
        type=float,
        required=True,
    )

    args = parser.parse_args()

    print()
    print("=" * 90)
    print("DYNAMIC CUP PICK PREVIEW")
    print("NO ROS / NO SERVICE / NO MOTOR COMMAND")
    print("=" * 90)

    print(
        "current core AXIAL_OFFSET_MM = "
        f"{float(core.AXIAL_OFFSET_MM):.1f}"
    )

    print(
        "cup TCP offset             = "
        f"{CUP_TCP_OFFSET_MM:.1f}"
    )

    print(
        "current deg limits         = "
        f"lower="
        f"{np.asarray(core.cfg.JOINT_LIMIT_LOWER_DEG).tolist()} "
        f"upper="
        f"{np.asarray(core.cfg.JOINT_LIMIT_UPPER_DEG).tolist()}"
    )

    print(
        "raw limits                 = "
        f"J3 "
        f"{core.cfg.J3_MIN_TICK}~"
        f"{core.cfg.J3_MAX_TICK}, "
        f"J4 "
        f"{core.cfg.J4_MIN_TICK}~"
        f"{core.cfg.J4_MAX_TICK}"
    )

    result = (
        build_dynamic_cup_pick_plan(
            args.x,
            args.y,
        )
    )

    print()
    print(
        f"CUP XY      = "
        f"({result['x_mm']:+.1f},"
        f"{result['y_mm']:+.1f}) mm"
    )

    print(
        f"R / angle   = "
        f"{result['radius_mm']:.2f} mm / "
        f"{result['angle_deg']:+.3f} deg"
    )

    p = result["plan"]

    print()
    show_point(
        "SAFE",
        p["SAFE"],
    )

    show_point(
        "D120_Z105",
        p["DESCEND"][-1],
    )

    show_point(
        "GRASP",
        p["APPROACH"][-1],
    )

    show_point(
        "LIFT",
        p["LIFT"][-1],
    )

    show_point(
        "PULL100",
        p["PULL"][-1],
    )

    print()
    print(
        f"points: "
        f"SAFE=1, "
        f"DESCEND={len(p['DESCEND'])}, "
        f"APPROACH={len(p['APPROACH'])}, "
        f"LIFT={len(p['LIFT'])}, "
        f"PULL={len(p['PULL'])}"
    )

    compare_reference(
        result
    )

    print()
    print("[OK] preview finished.")
    print("[OK] motor request sent = 0")


if __name__ == "__main__":
    main()
