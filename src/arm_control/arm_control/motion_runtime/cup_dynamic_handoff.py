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

import bag_tooldown_family_core as core


# ============================================================
# CUP verified transport state
#
# cup_z105_FULL_VERIFIED.json
# PULL20_TO_PULL100 final pose
# ============================================================

CUP_PULL100_TICKS = np.array(
    [2871, 1506, 9384, 1622, 2048],
    dtype=np.int64,
)

CUP_J5_TICK = 2048
CUP_J5_DEG = 0.0

HANDOFF_SEGMENTS = 16
MAX_HANDOFF_RADIUS_MM = 600.0

# Horizontal tool orientation
CUP_TILT_DEG = 0.0


def tcp_from_q(chain, q_deg):
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
        + core.AXIAL_OFFSET_MM * axis
    )

    return tcp, axis


def radial_axis(angle_deg):
    a = math.radians(
        float(angle_deg)
    )

    return np.array([
        math.cos(a),
        math.sin(a),
        0.0,
    ])


def build_cup_handoff(
    handoff,
    pull100_ticks=None,
    pull100_q=None,
):
    """
    Same handoff concept as paper_bag:

      verified CUP PULL100
        -> dynamic J1 TURN
        -> straight Cartesian HANDOFF
        -> J5 stays 0 deg / 2048

    Difference:
      paper_bag : tool-down
      cup       : horizontal radial tool axis
    """

    chain = (
        core.spp.create_robot_chain()
    )

    lower = np.asarray(
        core.cfg.JOINT_LIMIT_LOWER_DEG,
        dtype=float,
    )

    upper = np.asarray(
        core.cfg.JOINT_LIMIT_UPPER_DEG,
        dtype=float,
    )

    # --------------------------------------------------------
    # PICK -> HANDOFF continuity
    #
    # Default keeps old verified PULL100 for standalone
    # backwards compatibility.
    #
    # Control should pass the actual dynamic CUP PULL100.
    # --------------------------------------------------------

    if pull100_ticks is None:
        pull_ticks = (
            CUP_PULL100_TICKS.copy()
        )
    else:
        pull_ticks = np.asarray(
            pull100_ticks,
            dtype=np.int64,
        ).reshape(5).copy()

    core.cfg.validate_arm_ticks(
        pull_ticks
    )

    if int(pull_ticks[4]) != CUP_J5_TICK:
        raise RuntimeError(
            "CUP PULL100 J5 mismatch | "
            f"ticks={pull_ticks.tolist()}"
        )

    if pull100_q is None:
        pull_q = np.asarray(
            core.cfg.ticks_to_model_deg(
                pull_ticks
            ),
            dtype=float,
        )
    else:
        pull_q = np.asarray(
            pull100_q,
            dtype=float,
        ).reshape(5).copy()

        pull_q[4] = CUP_J5_DEG

        q_ticks = np.asarray(
            core.cfg.model_deg_to_ticks(
                pull_q
            ),
            dtype=np.int64,
        )

        q_ticks[4] = CUP_J5_TICK

        diff = np.abs(
            q_ticks - pull_ticks
        )

        if np.any(diff > 1):
            raise RuntimeError(
                "dynamic CUP PULL100 q/tick mismatch | "
                f"ticks={pull_ticks.tolist()} | "
                f"q_ticks={q_ticks.tolist()} | "
                f"diff={diff.tolist()}"
            )

    # --------------------------------------------------------
    # Dynamic TURN
    #
    # J2/J3/J4/J5 stay exactly as verified.
    # Only J1 follows driver direction.
    # --------------------------------------------------------

    target_angle = float(
        handoff["angle_deg"]
    )

    turn_q = pull_q.copy()
    turn_q[0] = target_angle
    turn_q[4] = CUP_J5_DEG

    if (
        np.any(turn_q < lower)
        or np.any(turn_q > upper)
    ):
        raise RuntimeError(
            "CUP TURN joint limit exceeded | "
            f"q={np.round(turn_q,3).tolist()}"
        )

    turn_ticks = np.asarray(
        core.cfg.model_deg_to_ticks(
            turn_q
        ),
        dtype=np.int64,
    )

    turn_ticks[4] = CUP_J5_TICK

    turn_tcp, turn_axis = tcp_from_q(
        chain,
        turn_q,
    )

    wanted_axis = radial_axis(
        target_angle
    )

    # Cup handoff keeps the gripper horizontal.
    if abs(float(turn_axis[2])) > 0.03:
        raise RuntimeError(
            "CUP TURN tool is not horizontal | "
            f"axis={np.round(turn_axis,5).tolist()}"
        )

    if float(
        np.dot(
            turn_axis,
            wanted_axis,
        )
    ) < 0.98:
        raise RuntimeError(
            "CUP TURN tool direction mismatch | "
            f"axis={np.round(turn_axis,5).tolist()} | "
            f"wanted={np.round(wanted_axis,5).tolist()}"
        )

    # --------------------------------------------------------
    # Same handoff target radius as bag
    #
    # Z is held at verified cup transport height.
    # --------------------------------------------------------

    final_r = float(
        handoff["handoff_radius"]
    )

    if final_r > MAX_HANDOFF_RADIUS_MM:
        raise RuntimeError(
            "CUP handoff radius exceeds limit | "
            f"{final_r:.1f} > "
            f"{MAX_HANDOFF_RADIUS_MM:.1f}"
        )

    final_target = np.array([
        final_r
        * math.cos(
            math.radians(target_angle)
        ),

        final_r
        * math.sin(
            math.radians(target_angle)
        ),

        float(turn_tcp[2]),
    ])

    # --------------------------------------------------------
    # Cartesian straight handoff
    #
    # Every point:
    #   tilt=0 deg
    #   J5=0 deg
    # --------------------------------------------------------

    points = []

    seed_q = turn_q.copy()

    for i in range(
        1,
        HANDOFF_SEGMENTS + 1,
    ):
        u = (
            i
            / HANDOFF_SEGMENTS
        )

        target = (
            (1.0 - u) * turn_tcp
            + u * final_target
        )

        solved = core.solve_tcp(
            chain,
            target,
            CUP_TILT_DEG,
            seed_q,
        )

        q = np.asarray(
            solved["q"],
            dtype=float,
        ).copy()

        # Exact driver direction + cup wrist orientation
        q[0] = target_angle
        q[4] = CUP_J5_DEG

        ticks = np.asarray(
            core.cfg.model_deg_to_ticks(q),
            dtype=np.int64,
        )

        ticks[4] = CUP_J5_TICK

        if (
            np.any(q < lower)
            or np.any(q > upper)
        ):
            raise RuntimeError(
                "CUP HANDOFF joint limit exceeded | "
                f"i={i} "
                f"q={np.round(q,3).tolist()}"
            )

        tcp, axis = tcp_from_q(
            chain,
            q,
        )

        error = float(
            np.linalg.norm(
                tcp - target
            )
        )

        if error > 2.0:
            raise RuntimeError(
                "CUP HANDOFF TCP error | "
                f"i={i} "
                f"error={error:.3f} mm"
            )

        if abs(float(axis[2])) > 0.03:
            raise RuntimeError(
                "CUP HANDOFF lost horizontal tool | "
                f"i={i} "
                f"axis={np.round(axis,5).tolist()}"
            )

        if float(
            np.dot(
                axis,
                wanted_axis,
            )
        ) < 0.98:
            raise RuntimeError(
                "CUP HANDOFF direction mismatch | "
                f"i={i}"
            )

        if int(ticks[4]) != CUP_J5_TICK:
            raise RuntimeError(
                "CUP HANDOFF J5 changed"
            )

        points.append({
            "name":
                f"CUP_HANDOFF_{i}",

            "target":
                target,

            "q":
                q,

            "ticks":
                ticks,

            "tcp":
                tcp,

            "axis":
                axis,

            "error":
                error,
        })

        seed_q = q

    return {
        "pull_q":
            pull_q,

        "turn_q":
            turn_q,

        "turn_ticks":
            turn_ticks,

        "turn_tcp":
            turn_tcp,

        "turn_axis":
            turn_axis,

        "final_target":
            final_target,

        "points":
            points,
    }


def main():
    # Calculation-only sample.
    # Same handoff radius rule used by integrated system.
    handoff = {
        "angle_deg": -150.0,
        "handoff_radius": 400.0,
    }

    motion = build_cup_handoff(
        handoff
    )

    print()
    print("=" * 78)
    print(" CUP DYNAMIC HANDOFF - DRY RUN")
    print("=" * 78)

    print(
        "PULL100 q =",
        np.round(
            motion["pull_q"],
            3,
        ).tolist(),
    )

    print(
        "TURN q    =",
        np.round(
            motion["turn_q"],
            3,
        ).tolist(),
    )

    print(
        "TURN ticks=",
        motion["turn_ticks"]
        .astype(int)
        .tolist(),
    )

    print(
        "TURN TCP  =",
        np.round(
            motion["turn_tcp"],
            2,
        ).tolist(),
    )

    print(
        "TURN axis =",
        np.round(
            motion["turn_axis"],
            5,
        ).tolist(),
    )

    first = motion["points"][0]
    final = motion["points"][-1]

    print()
    print(
        "FIRST q   =",
        np.round(
            first["q"],
            3,
        ).tolist(),
    )

    print(
        "FINAL q   =",
        np.round(
            final["q"],
            3,
        ).tolist(),
    )

    print(
        "FINAL TCP =",
        np.round(
            final["tcp"],
            2,
        ).tolist(),
    )

    print(
        "TARGET TCP=",
        np.round(
            motion["final_target"],
            2,
        ).tolist(),
    )

    print()
    print(
        "max TCP error =",
        max(
            p["error"]
            for p in motion["points"]
        ),
        "mm",
    )

    print(
        "J5 unique =",
        sorted({
            int(p["ticks"][4])
            for p in motion["points"]
        }),
    )

    print(
        "axis Z max =",
        max(
            abs(
                float(
                    p["axis"][2]
                )
            )
            for p in motion["points"]
        ),
    )

    print(
        "points =",
        len(
            motion["points"]
        ),
    )

    print()
    print(
        "[OK] calculation only - "
        "no ROS service / motor command"
    )


if __name__ == "__main__":
    main()
