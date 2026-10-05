#!/usr/bin/env python3

import argparse
import math
import numpy as np

import bag_tooldown_family_core as core


# ============================================================
# PHYSICAL / MOTION CONSTANTS
# ============================================================

# Bag body
BODY_HEIGHT_MM = 252.0
BODY_CENTER_Z_MM = BODY_HEIGHT_MM / 2.0       # 126

# Camera detects body center.
# From body center:
#   +126 mm = body top
#   + 50 mm = handle grasp point
HANDLE_GRASP_EXTRA_MM = 30.0

GRASP_Z_MM = (
    BODY_CENTER_Z_MM
    + BODY_HEIGHT_MM / 2.0
    + HANDLE_GRASP_EXTRA_MM
)                                                # 302

# Lift bag vertically after grasp.
LIFT_MM = 50.0
LIFT_Z_MM = GRASP_Z_MM + LIFT_MM               # 352

# Before horizontal insertion.
PREP_Z_MM = GRASP_Z_MM
APPROACH_MM = 120.0

# Detected body-center XY보다
# 물체 방향으로 50 mm 더 깊게 진입.
INSERT_EXTRA_MM = 50.0

# Actual finger axial offset.
TCP_OFFSET_MM = 136.0

# Horizontal tool.
HORIZONTAL_TILT_DEG = 0.0

# | | orientation during horizontal pick/handoff.
HORIZONTAL_J5_DEG = 0.0
HORIZONTAL_J5_TICK = 2048

APPROACH_SEGMENTS = 12
LIFT_SEGMENTS = 8
HANDOFF_SEGMENTS = 24

MAX_HANDOFF_RADIUS_MM = 400.0


# ============================================================
# CORE SOLVER WRAPPER
# ============================================================

def solve_horizontal(
    chain,
    name,
    target,
    seed_q,
):
    target = np.asarray(
        target,
        dtype=float,
    ).reshape(3)

    seed_q = np.asarray(
        seed_q,
        dtype=float,
    ).reshape(5).copy()

    angle_deg = math.degrees(
        math.atan2(
            float(target[1]),
            float(target[0]),
        )
    )

    seed_q[0] = angle_deg
    seed_q[4] = HORIZONTAL_J5_DEG

    old_offset = float(core.AXIAL_OFFSET_MM)
    old_j5_deg = float(core.J5_DEG)
    old_j5_tick = int(core.J5_TICK)

    try:
        core.AXIAL_OFFSET_MM = TCP_OFFSET_MM
        core.J5_DEG = HORIZONTAL_J5_DEG
        core.J5_TICK = HORIZONTAL_J5_TICK

        solved = core.solve_tcp(
            chain,
            target,
            HORIZONTAL_TILT_DEG,
            seed_q,
        )

    except Exception as exc:
        raise RuntimeError(
            f"{name}: horizontal IK failed | "
            f"target={np.round(target, 2).tolist()} | "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    finally:
        core.AXIAL_OFFSET_MM = old_offset
        core.J5_DEG = old_j5_deg
        core.J5_TICK = old_j5_tick

    q = np.asarray(
        solved["q"],
        dtype=float,
    ).copy()

    q[0] = angle_deg
    q[4] = HORIZONTAL_J5_DEG

    ticks = np.asarray(
        core.cfg.model_deg_to_ticks(q),
        dtype=np.int64,
    )

    core.cfg.validate_arm_ticks(ticks)

    if int(ticks[4]) != HORIZONTAL_J5_TICK:
        raise RuntimeError(
            f"{name}: J5 changed | "
            f"{ticks[4]} != {HORIZONTAL_J5_TICK}"
        )

    # core convention:
    # tilt=0 => J2+J3+J4 = 90 deg
    sum234 = float(
        q[1] + q[2] + q[3]
    )

    if abs(sum234 - 90.0) > 0.3:
        raise RuntimeError(
            f"{name}: horizontal posture lost | "
            f"J2+J3+J4={sum234:.3f}"
        )

    return {
        "name": str(name),
        "target": target.copy(),
        "q": q.copy(),
        "ticks": ticks.copy(),
        "tcp_error": float(
            solved["tcp_error"]
        ),
        "sum234": sum234,
    }


def tcp_from_q(
    chain,
    q_deg,
):
    state = core.pose_state(
        chain,
        np.deg2rad(
            np.asarray(
                q_deg,
                dtype=float,
            )
        ),
    )

    axis = core.unit(
        state["tool_axis"]
    )

    tcp = (
        np.asarray(
            state["xyz"],
            dtype=float,
        )
        + TCP_OFFSET_MM * axis
    )

    return tcp, axis


# ============================================================
# PICK
# ============================================================

def build_pick(
    x_mm,
    y_mm,
):
    x = float(x_mm)
    y = float(y_mm)

    radius = math.hypot(x, y)

    if radius <= APPROACH_MM + 20.0:
        raise RuntimeError(
            "L_paper_bag too close to base | "
            f"R={radius:.1f}"
        )

    angle_deg = math.degrees(
        math.atan2(y, x)
    )

    ux = x / radius
    uy = y / radius

    # Detected XY보다 같은 방사방향으로
    # 50 mm 더 깊은 곳까지 들어간다.
    grasp_x = (
        x
        + INSERT_EXTRA_MM * ux
    )
    grasp_y = (
        y
        + INSERT_EXTRA_MM * uy
    )

    ready_radius = (
        radius - APPROACH_MM
    )

    ready_x = ready_radius * ux
    ready_y = ready_radius * uy

    chain = core.spp.create_robot_chain()

    seed = np.array(
        [
            angle_deg,
            -10.0,
            80.0,
            20.0,
            HORIZONTAL_J5_DEG,
        ],
        dtype=float,
    )

    # --------------------------------------------------------
    # PREP
    # --------------------------------------------------------

    prep = solve_horizontal(
        chain,
        "PREP",
        [
            ready_x,
            ready_y,
            PREP_Z_MM,
        ],
        seed,
    )

    # --------------------------------------------------------
    # READY
    # --------------------------------------------------------

    ready = solve_horizontal(
        chain,
        "READY",
        [
            ready_x,
            ready_y,
            GRASP_Z_MM,
        ],
        prep["q"],
    )

    # --------------------------------------------------------
    # Horizontal insertion:
    # R-120 -> detected XY
    # Z stays exactly 302 mm.
    # --------------------------------------------------------

    approach = []
    last_q = ready["q"].copy()

    for i in range(
        1,
        APPROACH_SEGMENTS + 1,
    ):
        u = i / APPROACH_SEGMENTS

        target = np.array(
            [
                (1.0 - u) * ready_x
                + u * grasp_x,

                (1.0 - u) * ready_y
                + u * grasp_y,

                GRASP_Z_MM,
            ],
            dtype=float,
        )

        p = solve_horizontal(
            chain,
            f"APPROACH_{i:02d}",
            target,
            last_q,
        )

        approach.append(p)
        last_q = p["q"].copy()

    grasp = approach[-1]

    # --------------------------------------------------------
    # Vertical lift:
    # XY fixed, Z 302 -> 352
    # Horizontal | | remains.
    # --------------------------------------------------------

    lift = []
    last_q = grasp["q"].copy()

    for i in range(
        1,
        LIFT_SEGMENTS + 1,
    ):
        u = i / LIFT_SEGMENTS

        z = (
            GRASP_Z_MM
            + u * LIFT_MM
        )

        p = solve_horizontal(
            chain,
            f"LIFT_{i:02d}",
            [
                grasp_x,
                grasp_y,
                z,
            ],
            last_q,
        )

        lift.append(p)
        last_q = p["q"].copy()

    return {
        "x_mm": x,
        "y_mm": y,
        "radius_mm": radius,
        "angle_deg": angle_deg,

        "PREP": prep,
        "READY": ready,
        "APPROACH": approach,
        "GRASP": grasp,
        "LIFT": lift,
    }


# ============================================================
# HORIZONTAL HANDOFF
# ============================================================

def build_handoff(
    lift_q,
    handoff,
):
    """
    L_paper_bag handoff:

      horizontal LIFT
        -> J1 TURN
        -> horizontal Cartesian EXTEND

    J2+J3+J4 = 90 deg throughout generated path.
    J5 = 2048 throughout.
    """

    chain = core.spp.create_robot_chain()

    lift_q = np.asarray(
        lift_q,
        dtype=float,
    ).reshape(5).copy()

    # --------------------------------------------------------
    # TURN only J1.
    # Horizontal arm posture stays unchanged.
    # --------------------------------------------------------

    turn_q = lift_q.copy()
    turn_q[0] = float(
        handoff["angle_deg"]
    )
    turn_q[4] = HORIZONTAL_J5_DEG

    turn_ticks = np.asarray(
        core.cfg.model_deg_to_ticks(
            turn_q
        ),
        dtype=np.int64,
    )

    core.cfg.validate_arm_ticks(
        turn_ticks
    )

    sum234 = float(
        turn_q[1]
        + turn_q[2]
        + turn_q[3]
    )

    if abs(sum234 - 90.0) > 0.3:
        raise RuntimeError(
            "L_paper_bag TURN lost horizontal posture | "
            f"sum234={sum234:.3f}"
        )

    if int(
        turn_ticks[4]
    ) != HORIZONTAL_J5_TICK:
        raise RuntimeError(
            "L_paper_bag TURN J5 changed"
        )

    turn_tcp, _ = tcp_from_q(
        chain,
        turn_q,
    )

    final_radius = float(
        handoff["handoff_radius"]
    )

    if (
        final_radius
        > MAX_HANDOFF_RADIUS_MM
    ):
        raise RuntimeError(
            "L_paper_bag handoff radius too large | "
            f"{final_radius:.1f} > "
            f"{MAX_HANDOFF_RADIUS_MM:.1f}"
        )

    az = math.radians(
        float(
            handoff["angle_deg"]
        )
    )

    final_target = np.array(
        [
            final_radius
            * math.cos(az),

            final_radius
            * math.sin(az),

            LIFT_Z_MM,
        ],
        dtype=float,
    )

    points = []
    seed = turn_q.copy()

    for i in range(
        1,
        HANDOFF_SEGMENTS + 1,
    ):
        u = i / HANDOFF_SEGMENTS

        target = (
            (1.0 - u) * turn_tcp
            + u * final_target
        )

        p = solve_horizontal(
            chain,
            f"HANDOFF_{i:02d}",
            target,
            seed,
        )

        points.append(p)
        seed = p["q"].copy()

    return {
        "turn_q": turn_q,
        "turn_ticks": turn_ticks,
        "turn_tcp": turn_tcp,
        "final_target": final_target,
        "points": points,
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

    args = parser.parse_args()

    plan = build_pick(
        args.x,
        args.y,
    )

    print()
    print("=" * 78)
    print("L_PAPER_BAG HORIZONTAL PICK PREVIEW")
    print("=" * 78)

    print(
        f"Detected XY     = "
        f"({plan['x_mm']:+.1f}, "
        f"{plan['y_mm']:+.1f}) mm"
    )

    print(
        f"R / J1 angle    = "
        f"{plan['radius_mm']:.2f} mm / "
        f"{plan['angle_deg']:+.3f} deg"
    )

    print(
        f"Body center Z   = "
        f"{BODY_CENTER_Z_MM:.1f} mm"
    )

    print(
        f"Handle grasp Z  = "
        f"{GRASP_Z_MM:.1f} mm"
    )

    print(
        f"Lift Z          = "
        f"{LIFT_Z_MM:.1f} mm"
    )

    print(
        f"J5              = "
        f"{HORIZONTAL_J5_TICK}"
    )

    for name, point in [
        ("PREP", plan["PREP"]),
        ("READY", plan["READY"]),
        ("GRASP", plan["GRASP"]),
        ("LIFT", plan["LIFT"][-1]),
    ]:
        print(
            f"{name:<8} | "
            f"target="
            f"{np.round(point['target'],2).tolist()} | "
            f"q="
            f"{np.round(point['q'],2).tolist()} | "
            f"ticks="
            f"{point['ticks'].tolist()} | "
            f"sum234="
            f"{point['sum234']:.3f}"
        )

    print()
    print(
        f"approach points = "
        f"{len(plan['APPROACH'])}"
    )

    print(
        f"lift points     = "
        f"{len(plan['LIFT'])}"
    )

    print()
    print("MOTOR COMMAND = NONE")


if __name__ == "__main__":
    main()
