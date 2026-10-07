#!/usr/bin/env python3

import argparse
import math
import sys
from pathlib import Path

import numpy as np


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))

import bag_tooldown_family_core as core
from small_bag_pose_handoff import compute_handoff_target


# ============================================================
# PAYMENT SETTINGS
# ============================================================

# 사람 기준 30 cm 앞
STANDOFF_MM = 300.0

# PAYMENT tool-down에서 사용할 상한.
# 실제 목표가 이보다 멀면 아래에서 5 mm씩 줄이며
# 전체 경로가 가능한 가장 먼 반경을 자동 선택한다.
MAX_PAYMENT_RADIUS_MM = 400.0

# NFC payment TCP 높이
PAYMENT_Z_MM = 100.0

# 최종 실물 J4 hard/physical limit
PAYMENT_J4_MAX_TICK = 3500

# 가장 가까운 payment 반경
MIN_PAYMENT_RADIUS_MM = 300.0

# 자동 feasible search step
PAYMENT_RADIUS_STEP_MM = 5.0

# tool-down
PAYMENT_TILT_DEG = 90.0

# NFC 결제에서는 wrist roll 불필요
PAYMENT_J5_DEG = 0.0
PAYMENT_J5_TICK = 2048

# Cartesian path
PAYMENT_SEGMENTS = 24

TOOLDOWN_TARGET_SUM_DEG = 180.0
TOOLDOWN_TOL_DEG = 0.20

TCP_ERROR_LIMIT_MM = 1.0


# ============================================================
# FK
# ============================================================

def tcp_from_q(chain, q_deg):

    q = np.asarray(
        q_deg,
        dtype=float,
    ).reshape(5)

    state = core.pose_state(
        chain,
        np.deg2rad(q),
    )

    axis = core.unit(
        state["tool_axis"]
    )

    tcp = (
        np.asarray(
            state["xyz"],
            dtype=float,
        )
        + core.AXIAL_OFFSET_MM * axis
    )

    return tcp, axis


# ============================================================
# PAYMENT TOOL-DOWN SOLVER
#
# Existing core solve_tcp() is reused.
# Only during this calculation:
#   J5 = 0 deg / 2048
#
# Other code/file is NOT modified.
# ============================================================

def solve_payment_tcp(
    chain,
    target,
    seed_q=None,
):

    old_j5_deg = float(
        core.J5_DEG
    )

    old_j5_tick = int(
        core.J5_TICK
    )

    try:
        core.J5_DEG = PAYMENT_J5_DEG
        core.J5_TICK = PAYMENT_J5_TICK

        solved = core.solve_tcp(
            chain,
            np.asarray(
                target,
                dtype=float,
            ),
            PAYMENT_TILT_DEG,
            seed_q,
        )

    finally:
        core.J5_DEG = old_j5_deg
        core.J5_TICK = old_j5_tick

    q = np.asarray(
        solved["q"],
        dtype=float,
    ).copy()

    ticks = np.asarray(
        solved["ticks"],
        dtype=np.int64,
    ).copy()

    # exact payment wrist
    q[4] = PAYMENT_J5_DEG
    ticks[4] = PAYMENT_J5_TICK

    # Existing global joint/tick safety
    core.cfg.validate_arm_ticks(
        ticks
    )

    if int(ticks[3]) > PAYMENT_J4_MAX_TICK:
        raise RuntimeError(
            "PAYMENT J4 physical limit exceeded | "
            f"{int(ticks[3])} > "
            f"{PAYMENT_J4_MAX_TICK}"
        )

    total = float(
        q[1]
        + q[2]
        + q[3]
    )

    if (
        abs(
            total
            - TOOLDOWN_TARGET_SUM_DEG
        )
        > TOOLDOWN_TOL_DEG
    ):
        raise RuntimeError(
            "PAYMENT tool-down lost | "
            f"sum234={total:.3f} deg"
        )

    if int(ticks[4]) != PAYMENT_J5_TICK:
        raise RuntimeError(
            "PAYMENT J5 changed | "
            f"{ticks[4]}"
        )

    tcp, axis = tcp_from_q(
        chain,
        q,
    )

    if axis[2] > -0.99:
        raise RuntimeError(
            "PAYMENT tool axis is not down | "
            f"axis={np.round(axis,5).tolist()}"
        )

    error = float(
        np.linalg.norm(
            tcp
            - np.asarray(
                target,
                dtype=float,
            )
        )
    )

    if error > TCP_ERROR_LIMIT_MM:
        raise RuntimeError(
            "PAYMENT TCP error too large | "
            f"{error:.3f} mm"
        )

    return {
        "target":
            np.asarray(
                target,
                dtype=float,
            ),

        "q":
            q,

        "ticks":
            ticks,

        "tcp":
            tcp,

        "axis":
            axis,

        "sum234":
            total,

        "error":
            error,
    }


# ============================================================
# BUILD PAYMENT PLAN
# ============================================================

def build_payment_plan(
    person_xyz,
):

    person = np.asarray(
        person_xyz,
        dtype=float,
    ).reshape(3)

    # Same driver-distance policy used by handoff:
    #
    #   wanted radius = person radius - 300 mm
    #
    # max cap = 600 mm
    handoff = compute_handoff_target(
        person,
        MAX_PAYMENT_RADIUS_MM,
    )

    chain = (
        core.spp.create_robot_chain()
    )

    # --------------------------------------------------------
    # 1. START -> TOOL-DOWN START
    #
    # Keep J1/J2/J3.
    # Modify J4 only so:
    #
    #   J2 + J3 + J4 = 180 deg
    #
    # J5 -> 2048
    # --------------------------------------------------------

    start_ticks = np.asarray(
        core.cfg.ASSUMED_START_TICKS,
        dtype=np.int64,
    )

    start_q = np.asarray(
        core.cfg.ticks_to_model_deg(
            start_ticks
        ),
        dtype=float,
    )

    tool_start_q = (
        start_q.copy()
    )

    tool_start_q[3] = (
        TOOLDOWN_TARGET_SUM_DEG
        - tool_start_q[1]
        - tool_start_q[2]
    )

    tool_start_q[4] = (
        PAYMENT_J5_DEG
    )

    tool_start_ticks = np.asarray(
        core.cfg.model_deg_to_ticks(
            tool_start_q
        ),
        dtype=np.int64,
    )

    tool_start_ticks[4] = (
        PAYMENT_J5_TICK
    )

    core.cfg.validate_arm_ticks(
        tool_start_ticks
    )

    if int(tool_start_ticks[3]) > PAYMENT_J4_MAX_TICK:
        raise RuntimeError(
            "PAYMENT_TOOLDOWN_START J4 limit | "
            f"{int(tool_start_ticks[3])} > "
            f"{PAYMENT_J4_MAX_TICK}"
        )

    tool_start_tcp, tool_start_axis = (
        tcp_from_q(
            chain,
            tool_start_q,
        )
    )

    tool_start_sum = float(
        tool_start_q[1]
        + tool_start_q[2]
        + tool_start_q[3]
    )

    if (
        abs(
            tool_start_sum
            - TOOLDOWN_TARGET_SUM_DEG
        )
        > TOOLDOWN_TOL_DEG
    ):
        raise RuntimeError(
            "PAYMENT_TOOLDOWN_START "
            "sum mismatch"
        )

    if tool_start_axis[2] > -0.99:
        raise RuntimeError(
            "PAYMENT_TOOLDOWN_START "
            "axis is not down"
        )

    # --------------------------------------------------------
    # 2. TOOL-DOWN TURN
    #
    # J2/J3/J4/J5 fixed.
    # Only J1 follows frozen driver direction.
    # --------------------------------------------------------

    turn_q = (
        tool_start_q.copy()
    )

    turn_q[0] = float(
        handoff["angle_deg"]
    )

    turn_q[4] = (
        PAYMENT_J5_DEG
    )

    turn_ticks = np.asarray(
        core.cfg.model_deg_to_ticks(
            turn_q
        ),
        dtype=np.int64,
    )

    turn_ticks[4] = (
        PAYMENT_J5_TICK
    )

    core.cfg.validate_arm_ticks(
        turn_ticks
    )

    if int(turn_ticks[3]) > PAYMENT_J4_MAX_TICK:
        raise RuntimeError(
            "PAYMENT TURN J4 limit | "
            f"{int(turn_ticks[3])} > "
            f"{PAYMENT_J4_MAX_TICK}"
        )

    turn_tcp, turn_axis = (
        tcp_from_q(
            chain,
            turn_q,
        )
    )

    turn_sum = float(
        turn_q[1]
        + turn_q[2]
        + turn_q[3]
    )

    if (
        abs(
            turn_sum
            - TOOLDOWN_TARGET_SUM_DEG
        )
        > TOOLDOWN_TOL_DEG
    ):
        raise RuntimeError(
            "PAYMENT TURN lost tool-down"
        )

    if turn_axis[2] > -0.99:
        raise RuntimeError(
            "PAYMENT TURN axis is not down"
        )

    # --------------------------------------------------------
    # 3. FINAL NFC TARGET
    #
    # XY:
    #   person position - 300 mm
    #
    # Z:
    #   frozen driver pose Z
    #
    # NFC mounting offset can be added later if measured.
    # --------------------------------------------------------

    wanted_r = float(
        handoff["wanted_radius"]
    )

    az = math.radians(
        float(
            handoff["angle_deg"]
        )
    )

    # --------------------------------------------------------
    # Find farthest completely-feasible TOOL-DOWN path.
    #
    # Example:
    #   wanted = 599.7
    #   start candidate = 400
    #
    # Try:
    #   400 -> 395 -> 390 -> ...
    #
    # First complete path that satisfies:
    #   joint limits
    #   J4 <= 3500
    #   TCP error
    #   J2+J3+J4 = 180
    #   tool axis downward
    #
    # becomes the payment radius.
    # --------------------------------------------------------

    first_r = min(
        wanted_r,
        MAX_PAYMENT_RADIUS_MM,
    )

    if first_r < MIN_PAYMENT_RADIUS_MM:
        raise RuntimeError(
            "person too close for PAYMENT | "
            f"wanted_r={wanted_r:.1f}"
        )

    selected_r = None
    final_target = None
    points = None
    last_error = None

    candidate_r = first_r

    while (
        candidate_r
        >= MIN_PAYMENT_RADIUS_MM
        - 1.0e-9
    ):

        candidate_target = np.array(
            [
                candidate_r
                * math.cos(az),

                candidate_r
                * math.sin(az),

                PAYMENT_Z_MM,
            ],
            dtype=float,
        )

        candidate_points = []
        seed_q = turn_q.copy()

        try:
            for i in range(
                1,
                PAYMENT_SEGMENTS + 1,
            ):

                u = (
                    i
                    / PAYMENT_SEGMENTS
                )

                target = (
                    (1.0 - u) * turn_tcp
                    + u * candidate_target
                )

                p = solve_payment_tcp(
                    chain,
                    target,
                    seed_q,
                )

                # --------------------------------------------------
                # Keep PAYMENT J1 on one numeric branch.
                #
                # At exactly the rear direction atan2 may alternate
                # between +180 and -180 deg.  They are physically the
                # same direction, but converting both branches to
                # Dynamixel ticks creates a huge artificial J1 jump.
                #
                # Freeze J1 to the already-validated TURN direction.
                # --------------------------------------------------
                p["q"] = np.asarray(
                    p["q"],
                    dtype=float,
                ).copy()

                p["q"][0] = float(
                    turn_q[0]
                )

                p["q"][4] = (
                    PAYMENT_J5_DEG
                )

                p["ticks"] = np.asarray(
                    core.cfg.model_deg_to_ticks(
                        p["q"]
                    ),
                    dtype=np.int64,
                )

                p["ticks"][4] = (
                    PAYMENT_J5_TICK
                )

                core.cfg.validate_arm_ticks(
                    p["ticks"]
                )

                candidate_points.append(
                    p
                )

                seed_q = (
                    p["q"].copy()
                )

            # Entire path succeeded.
            selected_r = float(
                candidate_r
            )

            final_target = (
                candidate_target
            )

            points = (
                candidate_points
            )

            break

        except Exception as exc:
            last_error = exc

            candidate_r -= (
                PAYMENT_RADIUS_STEP_MM
            )

    if selected_r is None:
        raise RuntimeError(
            "no feasible PAYMENT tool-down path | "
            f"last_error={last_error}"
        )

    actual_standoff = float(
        handoff["person_r"]
        - selected_r
    )


    return {
        "person":
            person,

        "person_radius":
            float(
                handoff["person_r"]
            ),

        "wanted_radius":
            float(
                handoff[
                    "wanted_radius"
                ]
            ),

        "used_radius":
            selected_r,

        "standoff":
            actual_standoff,

        "angle_deg":
            float(
                handoff[
                    "angle_deg"
                ]
            ),

        "TOOLDOWN_START": {
            "q":
                tool_start_q,

            "ticks":
                tool_start_ticks,

            "tcp":
                tool_start_tcp,

            "sum234":
                tool_start_sum,
        },

        "TURN": {
            "q":
                turn_q,

            "ticks":
                turn_ticks,

            "tcp":
                turn_tcp,

            "sum234":
                turn_sum,
        },

        "FINAL_TARGET":
            final_target,

        "APPROACH":
            points,
    }


# ============================================================
# PREVIEW
# ============================================================

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

    parser.add_argument(
        "--z",
        type=float,
        required=True,
    )

    args = parser.parse_args()

    person = np.array(
        [
            args.x,
            args.y,
            args.z,
        ],
        dtype=float,
    )

    plan = build_payment_plan(
        person
    )

    final = (
        plan["APPROACH"][-1]
    )

    print()
    print("=" * 78)
    print("NFC PAYMENT TOOL-DOWN PREVIEW")
    print("=" * 78)

    print(
        "person XYZ      =",
        np.round(
            plan["person"],
            2,
        ).tolist(),
        "mm",
    )

    print(
        f"person radius   = "
        f"{plan['person_radius']:.2f} mm"
    )

    print(
        f"wanted radius   = "
        f"{plan['wanted_radius']:.2f} mm"
    )

    print(
        f"used radius     = "
        f"{plan['used_radius']:.2f} mm"
    )

    print(
        f"final standoff  = "
        f"{plan['standoff']:.2f} mm"
    )

    print(
        f"driver angle    = "
        f"{plan['angle_deg']:+.3f} deg"
    )

    print()

    for name in [
        "TOOLDOWN_START",
        "TURN",
    ]:
        p = plan[name]

        print(
            f"{name:<15} | "
            f"q="
            f"{np.round(p['q'],3).tolist()} | "
            f"ticks="
            f"{p['ticks'].astype(int).tolist()} | "
            f"sum234="
            f"{p['sum234']:.3f}"
        )

    print()
    print(
        f"approach points = "
        f"{len(plan['APPROACH'])}"
    )

    print(
        "final target    =",
        np.round(
            plan["FINAL_TARGET"],
            2,
        ).tolist(),
        "mm",
    )

    print(
        "final TCP       =",
        np.round(
            final["tcp"],
            2,
        ).tolist(),
        "mm",
    )

    print(
        "final q         =",
        np.round(
            final["q"],
            3,
        ).tolist(),
    )

    print(
        "final ticks     =",
        final["ticks"]
        .astype(int)
        .tolist(),
    )

    print(
        f"final sum234    = "
        f"{final['sum234']:.3f} deg"
    )

    print(
        "tool axis       =",
        np.round(
            final["axis"],
            5,
        ).tolist(),
    )

    print(
        f"TCP error       = "
        f"{final['error']:.6f} mm"
    )

    print()
    print("MOTOR COMMAND = NONE")
    print("=" * 78)


if __name__ == "__main__":
    main()
