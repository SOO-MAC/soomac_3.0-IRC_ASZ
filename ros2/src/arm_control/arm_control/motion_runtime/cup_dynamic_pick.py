#!/usr/bin/env python3

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))

import cup_dynamic_pick_preview as base


# ============================================================
# Dynamic approach policy
# ============================================================

APPROACH_CANDIDATES_MM = (
    120.0,
    110.0,
    100.0,
    90.0,
    80.0,
    70.0,
    60.0,
    50.0,
    40.0,
)

DESCEND_Z_MM = (
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
)

LIFT_Z_MM = (
    125.0,
    145.0,
    165.0,
    185.0,
)

PULL_D_MM = (
    20.0,
    40.0,
    60.0,
    80.0,
    100.0,
)

# 검출된 CUP XY에서 반경 바깥쪽으로
# 60 mm 더 깊게 삽입한 뒤 그립한다.
#
# target_at_d():
#   D > 0 : 로봇 쪽
#   D = 0 : 검출 XY
#   D < 0 : 검출 XY보다 컵 안쪽/바깥 방향
CUP_INSERT_EXTRA_MM = 60.0
CUP_INSERT_D_MM = -CUP_INSERT_EXTRA_MM

CUP_INSERT_APPROACH_D_MM = (
    -20.0,
    -40.0,
    CUP_INSERT_D_MM,
)

# 그립 후 먼저 들어온 경로를 높은 Z에서 되돌아온 뒤
# 기존 PULL_D_MM 경로에 합류한다.
CUP_INSERT_RETURN_D_MM = (
    -40.0,
    -20.0,
    0.0,
)


EXTRA_PULL_D_MM = (
    120.0,
    140.0,
    160.0,
    180.0,
    200.0,
)


def approach_values(start_d_mm):
    """
    Horizontal approach:
      selected D -> ... -> D0

    Candidate D values are multiples of 10 mm,
    so use 10 mm Cartesian increments.
    """

    start_d_mm = float(start_d_mm)

    values = []

    d = start_d_mm - 10.0

    while d > 0.0:
        values.append(d)
        d -= 10.0

    values.append(0.0)

    return values


def _tick_margin(ticks):
    ticks = np.asarray(
        ticks,
        dtype=np.int64,
    )

    j3 = int(ticks[2])
    j4 = int(ticks[3])

    j3_margin = min(
        j3 - int(base.core.cfg.J3_MIN_TICK),
        int(base.core.cfg.J3_MAX_TICK) - j3,
    )

    j4_margin = min(
        j4 - int(base.core.cfg.J4_MIN_TICK),
        int(base.core.cfg.J4_MAX_TICK) - j4,
    )

    return (
        int(j3_margin),
        int(j4_margin),
    )


def build_plan_for_d(
    x_mm,
    y_mm,
    approach_d_mm,
):
    """
    Build ONE complete CUP path using a specified approach D.

    IMPORTANT:
      Complete SAFE -> DESCEND -> APPROACH -> LIFT -> PULL
      is calculated before this function returns.

      No ROS.
      No motor command.
    """

    (
        radius,
        _ux,
        _uy,
        angle_deg,
    ) = base.radial_data(
        x_mm,
        y_mm,
    )

    chain = (
        base.core.spp.create_robot_chain()
    )

    # Existing physically verified CUP SAFE pose
    # is used only as an IK seed.
    seed_q = np.asarray(
        base.core.cfg.ticks_to_model_deg(
            base.REFERENCE["SAFE"]
        ),
        dtype=float,
    )

    seed_q[0] = float(angle_deg)
    seed_q[4] = base.CUP_J5_DEG

    # --------------------------------------------------------
    # SAFE: selected D / Z300
    # --------------------------------------------------------

    safe_target = base.target_at_d(
        x_mm,
        y_mm,
        approach_d_mm,
        base.CUP_SAFE_Z_MM,
    )

    safe = base.solve_cup_tcp(
        chain,
        f"D{approach_d_mm:.0f}_Z300",
        safe_target,
        seed_q,
        angle_deg,
    )

    seed_q = safe["q"].copy()

    # --------------------------------------------------------
    # DESCEND: selected D / Z300 -> Z105
    # --------------------------------------------------------

    descend = []

    for z_mm in DESCEND_Z_MM:

        target = base.target_at_d(
            x_mm,
            y_mm,
            approach_d_mm,
            z_mm,
        )

        point = base.solve_cup_tcp(
            chain,
            f"D{approach_d_mm:.0f}_Z{z_mm:.0f}",
            target,
            seed_q,
            angle_deg,
        )

        descend.append(point)

        seed_q = point["q"].copy()

    # --------------------------------------------------------
    # APPROACH: selected D -> D0 at Z105
    # --------------------------------------------------------

    approach = []

    for d_mm in approach_values(
        approach_d_mm
    ):

        target = base.target_at_d(
            x_mm,
            y_mm,
            d_mm,
            base.CUP_GRASP_Z_MM,
        )

        point = base.solve_cup_tcp(
            chain,
            f"APPROACH_D{d_mm:.0f}",
            target,
            seed_q,
            angle_deg,
        )

        approach.append(point)

        seed_q = point["q"].copy()

    # --------------------------------------------------------
    # INSERT: D0 -> D-60 at Z105
    #
    # 카메라 검출 XY보다 radial 방향으로 60 mm 더 들어간다.
    # --------------------------------------------------------

    for d_mm in CUP_INSERT_APPROACH_D_MM:

        target = base.target_at_d(
            x_mm,
            y_mm,
            d_mm,
            base.CUP_GRASP_Z_MM,
        )

        point = base.solve_cup_tcp(
            chain,
            f"INSERT_D{d_mm:.0f}",
            target,
            seed_q,
            angle_deg,
        )

        approach.append(point)

        seed_q = point["q"].copy()

    grasp = approach[-1]

    # --------------------------------------------------------
    # LIFT: D-60 / Z105 -> Z185
    # 잡은 위치에서 그대로 먼저 들어 올린다.
    # --------------------------------------------------------

    lift = []

    for z_mm in LIFT_Z_MM:

        target = base.target_at_d(
            x_mm,
            y_mm,
            CUP_INSERT_D_MM,
            z_mm,
        )

        point = base.solve_cup_tcp(
            chain,
            f"LIFT_Z{z_mm:.0f}",
            target,
            seed_q,
            angle_deg,
        )

        lift.append(point)

        seed_q = point["q"].copy()

    # --------------------------------------------------------
    # PULL:
    # D-60 -> D-40 -> D-20 -> D0 -> D20 ... at Z185
    # --------------------------------------------------------

    pull = []

    for d_mm in (
        *CUP_INSERT_RETURN_D_MM,
        *PULL_D_MM,
    ):

        target = base.target_at_d(
            x_mm,
            y_mm,
            d_mm,
            base.CUP_LIFT_Z_MM,
        )

        point = base.solve_cup_tcp(
            chain,
            f"PULL_D{d_mm:.0f}",
            target,
            seed_q,
            angle_deg,
        )

        pull.append(point)

        seed_q = point["q"].copy()

    pull100 = pull[-1]

    # --------------------------------------------------------
    # Adaptive extra PULL
    #
    # PULL100 is guaranteed first.
    # Then extend by 20 mm while both J3/J4 retain the
    # configured software margin.
    #
    # Failure of an EXTRA point does NOT invalidate the PICK.
    # We simply keep the last safe transport point.
    # --------------------------------------------------------

    pull_distance_mm = 100.0

    for d_mm in EXTRA_PULL_D_MM:

        try:
            target = base.target_at_d(
                x_mm,
                y_mm,
                d_mm,
                base.CUP_LIFT_Z_MM,
            )

            point = base.solve_cup_tcp(
                chain,
                f"PULL_D{d_mm:.0f}",
                target,
                seed_q,
                angle_deg,
            )

        except Exception:
            break

        j3_margin, j4_margin = (
            _tick_margin(
                point["ticks"]
            )
        )

        if (
            j3_margin < SOFT_J3_MARGIN_TICK
            or
            j4_margin < SOFT_J4_MARGIN_TICK
        ):
            break

        pull.append(point)

        seed_q = point["q"].copy()

        pull_distance_mm = float(
            d_mm
        )

    pull_final = pull[-1]

    # --------------------------------------------------------
    # Final full-plan validation
    # --------------------------------------------------------

    all_points = (
        [safe]
        + descend
        + approach
        + lift
        + pull
    )

    min_j3_margin = None
    min_j4_margin = None

    for point in all_points:

        ticks = np.asarray(
            point["ticks"],
            dtype=np.int64,
        )

        # Latest robot_config:
        #   J3 = 0..10606
        #   J4 = 700..3500
        base.core.cfg.validate_arm_ticks(
            ticks
        )

        if int(ticks[4]) != base.CUP_J5_TICK:
            raise RuntimeError(
                "CUP J5 changed | "
                f"name={point['name']} "
                f"tick={int(ticks[4])}"
            )

        j3_margin, j4_margin = (
            _tick_margin(ticks)
        )

        min_j3_margin = (
            j3_margin
            if min_j3_margin is None
            else min(
                min_j3_margin,
                j3_margin,
            )
        )

        min_j4_margin = (
            j4_margin
            if min_j4_margin is None
            else min(
                min_j4_margin,
                j4_margin,
            )
        )

    return {
        "x_mm":
            float(x_mm),

        "y_mm":
            float(y_mm),

        "radius_mm":
            float(radius),

        "angle_deg":
            float(angle_deg),

        "approach_d_mm":
            float(approach_d_mm),

        "SAFE":
            safe,

        "DESCEND":
            descend,

        "APPROACH":
            approach,

        "GRASP":
            grasp,

        "LIFT":
            lift,

        "PULL":
            pull,

        "PULL100":
            pull100,

        "PULL_FINAL":
            pull_final,

        "pull_distance_mm":
            float(pull_distance_mm),

        "min_j3_margin_tick":
            int(min_j3_margin),

        "min_j4_margin_tick":
            int(min_j4_margin),
    }


SOFT_J3_MARGIN_TICK = 200
SOFT_J4_MARGIN_TICK = 200


def build_dynamic_cup_pick_plan(
    x_mm,
    y_mm,
):
    """
    Select the largest approach D whose COMPLETE path satisfies:

      1) IK succeeds
      2) configured hard joint/tick limits
      3) J3/J4 software margins

    Candidate order:
      D120 -> D110 -> ... -> D40

    No ROS / no motor command here.
    """

    errors = []

    for approach_d_mm in (
        APPROACH_CANDIDATES_MM
    ):
        try:
            plan = build_plan_for_d(
                float(x_mm),
                float(y_mm),
                float(approach_d_mm),
            )

        except Exception as e:
            errors.append(
                (
                    approach_d_mm,
                    "IK/HARD",
                    str(e),
                )
            )
            continue

        j3_margin = int(
            plan["min_j3_margin_tick"]
        )

        j4_margin = int(
            plan["min_j4_margin_tick"]
        )

        if (
            j3_margin
            < SOFT_J3_MARGIN_TICK
            or
            j4_margin
            < SOFT_J4_MARGIN_TICK
        ):
            errors.append(
                (
                    approach_d_mm,
                    "SOFT",
                    (
                        f"margin too small | "
                        f"J3={j3_margin} "
                        f"(need {SOFT_J3_MARGIN_TICK}), "
                        f"J4={j4_margin} "
                        f"(need {SOFT_J4_MARGIN_TICK})"
                    ),
                )
            )
            continue

        plan["soft_j3_margin_tick"] = (
            SOFT_J3_MARGIN_TICK
        )

        plan["soft_j4_margin_tick"] = (
            SOFT_J4_MARGIN_TICK
        )

        return plan

    detail = " | ".join(
        (
            f"D{d:.0f} [{kind}]: "
            f"{message}"
        )
        for d, kind, message
        in errors
    )

    raise RuntimeError(
        "no safe dynamic CUP PICK path | "
        f"XY=({float(x_mm):+.1f},"
        f"{float(y_mm):+.1f}) | "
        f"{detail}"
    )

if __name__ == "__main__":

    tests = (
        (80.0, 500.0),
        (120.0, 450.0),
        (120.0, 400.0),
        (160.0, 400.0),
        (200.0, 400.0),
    )

    print()
    print("=" * 90)
    print("DYNAMIC CUP PICK - AUTO APPROACH")
    print("NO ROS / NO SERVICE / NO MOTOR")
    print("=" * 90)

    for x_mm, y_mm in tests:

        plan = (
            build_dynamic_cup_pick_plan(
                x_mm,
                y_mm,
            )
        )

        print()
        print(
            f"XY=({x_mm:+.1f},{y_mm:+.1f}) | "
            f"R={plan['radius_mm']:.1f} | "
            f"angle={plan['angle_deg']:+.2f} | "
            f"D={plan['approach_d_mm']:.0f}"
        )

        print(
            "  GRASP   =",
            plan["GRASP"]["ticks"]
            .astype(int)
            .tolist(),
        )

        print(
            f"  PULL{plan['pull_distance_mm']:.0f} =",
            plan["PULL_FINAL"]["ticks"]
            .astype(int)
            .tolist(),
        )

        print(
            "  margins = "
            f"J3 {plan['min_j3_margin_tick']} tick, "
            f"J4 {plan['min_j4_margin_tick']} tick"
        )

    print()
    print("[OK] all calculations finished")
    print("[OK] motor request sent = 0")
