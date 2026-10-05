#!/usr/bin/env python3

import argparse
import math
import numpy as np
import rclpy

import bag_tooldown_family_core as core

from small_bag_pose_handoff import (
    Runner,
    compute_handoff_target,
)

from small_bag_full_sequence import SPEC


# ============================================================
# SETTINGS
# ============================================================

# 실제 smallbag에서 기존 검증된 최대 전달 반경
EXEC_MAX_RADIUS_MM = 400.0

# 사람 어깨에서 원하는 거리
STANDOFF_MM = 300.0

# TURN 후 handoff까지 Cartesian waypoint 수
HANDOFF_SEGMENTS = 16


# ============================================================
# TCP/FK
# ============================================================

def tcp_from_q(chain, q_deg):

    state = core.pose_state(
        chain,
        np.deg2rad(
            np.asarray(q_deg, dtype=float)
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
        + core.AXIAL_OFFSET_MM * axis
    )

    return tcp, axis


# ============================================================
# TURN
# ============================================================

def build_turn(
    plan,
    handoff,
):

    q = (
        plan["PULL100"]["q"]
        .copy()
    )

    q[0] = float(
        handoff["angle_deg"]
    )

    q[4] = float(plan["PULL100"]["q"][4])

    lower = np.asarray(
        core.cfg.JOINT_LIMIT_LOWER_DEG,
        dtype=float,
    )

    upper = np.asarray(
        core.cfg.JOINT_LIMIT_UPPER_DEG,
        dtype=float,
    )

    if np.any(q < lower) or np.any(q > upper):
        raise RuntimeError(
            "TURN joint limit exceeded | "
            f"q={np.round(q,3).tolist()}"
        )

    ticks = np.asarray(
        core.cfg.model_deg_to_ticks(q),
        dtype=np.int64,
    )

    if int(ticks[3]) > core.J4_SOFT_MAX_TICK:
        raise RuntimeError(
            "TURN J4 soft max exceeded | "
            f"{ticks[3]} > "
            f"{core.J4_SOFT_MAX_TICK}"
        )

    return q, ticks


# ============================================================
# FULL IK HANDOFF PATH
# ============================================================

def build_fullik_path(
    plan,
    handoff,
    *,
    joint_blend=False,
    blend_alpha=0.20,
):

    chain = (
        core.spp.create_robot_chain()
    )

    turn_q, turn_ticks = build_turn(
        plan,
        handoff,
    )

    # --------------------------------------------------------
    # TURN 완료 위치 실제 TCP
    # --------------------------------------------------------

    turn_tcp, turn_axis = (
        tcp_from_q(
            chain,
            turn_q,
        )
    )

    # 반드시 tool-down
    if turn_axis[2] > -0.99:
        raise RuntimeError(
            "TURN tool axis is not down | "
            f"{turn_axis.tolist()}"
        )

    # --------------------------------------------------------
    # 기존 r400 smallbag의 검증된 최종 Z
    # --------------------------------------------------------

    old_final_q = (
        plan["EXTEND"][-1]["q"]
    )

    old_tcp, old_axis = (
        tcp_from_q(
            chain,
            old_final_q,
        )
    )

    target_z = float(
        old_tcp[2]
    )

    # --------------------------------------------------------
    # 최종 사람 방향 / 거리
    # --------------------------------------------------------

    az = math.radians(
        float(
            handoff["angle_deg"]
        )
    )

    final_r = float(
        handoff["handoff_radius"]
    )

    if (
        final_r
        > EXEC_MAX_RADIUS_MM
        + 1.0e-6
    ):
        raise RuntimeError(
            "execution radius exceeds "
            "validated maximum | "
            f"{final_r:.1f} > "
            f"{EXEC_MAX_RADIUS_MM:.1f} mm"
        )

    final_target = np.array([
        final_r * math.cos(az),
        final_r * math.sin(az),
        target_z,
    ])

    # --------------------------------------------------------
    # TURN TCP -> HANDOFF TCP 를 직선 보간
    #
    # 모든 waypoint마다 solve_tcp(..., tilt=90)
    # 따라서 J2/J3/J4는 달라질 수 있지만
    # tool-down은 유지된다.
    # --------------------------------------------------------

    points = []

    seed_q = turn_q.copy()

    lower = np.asarray(
        core.cfg.JOINT_LIMIT_LOWER_DEG,
        dtype=float,
    )

    upper = np.asarray(
        core.cfg.JOINT_LIMIT_UPPER_DEG,
        dtype=float,
    )

    # ========================================================
    # PAPER BAG synchronized EXTEND/HANDOFF
    #
    # Cartesian waypoint마다 IK를 다시 풀지 않고,
    # 최종 자세를 한 번 계산한 뒤
    # TURN -> FINAL을 J2/J3/J4 동시 보간한다.
    # ========================================================
    if joint_blend:

        alpha = float(blend_alpha)

        if not 0.0 <= alpha <= 1.0:
            raise ValueError(
                f"blend_alpha must be 0~1 | {alpha}"
            )

        expected_j1 = float(
            handoff["angle_deg"]
        )

        # 최종 자세만 IK로 계산
        final_solved = core.solve_tcp(
            chain,
            final_target,
            90.0,
            turn_q,
        )

        final_q = np.asarray(
            final_solved["q"],
            dtype=float,
        ).copy()

        # 방향과 J5는 현재 TURN 상태 유지
        final_q[0] = expected_j1
        final_q[4] = float(turn_q[4])

        final_ticks = np.asarray(
            core.cfg.model_deg_to_ticks(final_q),
            dtype=np.int64,
        )

        if (
            np.any(final_q < lower)
            or np.any(final_q > upper)
        ):
            raise RuntimeError(
                "SYNC HANDOFF final joint limit exceeded | "
                f"q={np.round(final_q,3).tolist()}"
            )

        if (
            int(final_ticks[3])
            > core.J4_SOFT_MAX_TICK
        ):
            raise RuntimeError(
                "SYNC HANDOFF final J4 soft max exceeded | "
                f"{final_ticks[3]} > "
                f"{core.J4_SOFT_MAX_TICK}"
            )

        final_total = (
            final_q[1]
            + final_q[2]
            + final_q[3]
        )

        if abs(final_total - 180.0) > 0.2:
            raise RuntimeError(
                "SYNC HANDOFF final tool-down mismatch | "
                f"{final_total:.3f}"
            )

        final_tcp, final_axis = tcp_from_q(
            chain,
            final_q,
        )

        # J5를 paper_bag의 0deg로 유지해도
        # TCP 위치가 변하지 않는지 여기서 검증.
        final_tcp_error = float(
            np.linalg.norm(
                final_tcp - final_target
            )
        )

        if final_tcp_error > 2.0:
            raise RuntimeError(
                "SYNC HANDOFF final TCP changed after J5 hold | "
                f"error={final_tcp_error:.3f} mm"
            )

        if final_axis[2] > -0.99:
            raise RuntimeError(
                "SYNC HANDOFF final tool axis lost | "
                f"{final_axis.tolist()}"
            )

        cart_seed_q = turn_q.copy()

        for i in range(
            1,
            HANDOFF_SEGMENTS + 1,
        ):

            u = (
                i
                / HANDOFF_SEGMENTS
            )

            # 목표 Cartesian 직선 위치
            target = (
                (1.0 - u) * turn_tcp
                + u * final_target
            )

            # 원래 Cartesian IK 경로
            cart_solved = core.solve_tcp(
                chain,
                target,
                90.0,
                cart_seed_q,
            )

            cart_q = np.asarray(
                cart_solved["q"],
                dtype=float,
            ).copy()

            cart_q[0] = expected_j1
            cart_q[4] = float(turn_q[4])

            # TURN -> FINAL joint-space 경로
            joint_q = (
                (1.0 - u) * turn_q
                + u * final_q
            )

            joint_q[0] = expected_j1
            joint_q[4] = float(turn_q[4])

            # Cartesian 경로 80% + joint 동시전개 20%
            q = (
                (1.0 - alpha) * cart_q
                + alpha * joint_q
            )

            q[0] = expected_j1
            q[4] = float(turn_q[4])

            cart_seed_q = cart_q

            ticks = np.asarray(
                core.cfg.model_deg_to_ticks(q),
                dtype=np.int64,
            )

            if (
                np.any(q < lower)
                or np.any(q > upper)
            ):
                raise RuntimeError(
                    "SYNC HANDOFF joint limit exceeded | "
                    f"i={i} "
                    f"q={np.round(q,3).tolist()}"
                )

            if (
                int(ticks[3])
                > core.J4_SOFT_MAX_TICK
            ):
                raise RuntimeError(
                    "SYNC HANDOFF J4 soft max exceeded | "
                    f"i={i} "
                    f"{ticks[3]} > "
                    f"{core.J4_SOFT_MAX_TICK}"
                )

            total = (
                q[1]
                + q[2]
                + q[3]
            )

            if abs(total - 180.0) > 0.2:
                raise RuntimeError(
                    "SYNC HANDOFF tool-down mismatch | "
                    f"i={i} "
                    f"{total:.3f}"
                )

            tcp, axis = tcp_from_q(
                chain,
                q,
            )

            if axis[2] > -0.99:
                raise RuntimeError(
                    "SYNC HANDOFF tool axis lost | "
                    f"i={i} "
                    f"{axis.tolist()}"
                )

            # 비교용 직선 Cartesian 위치.
            # 실제 joint interpolation 경로는 약간 곡선일 수 있다.
            target = (
                (1.0 - u) * turn_tcp
                + u * final_target
            )

            path_deviation = float(
                np.linalg.norm(
                    tcp - target
                )
            )

            points.append({
                "name":
                    f"SYNC_HANDOFF_{i}",

                "target": target,
                "q": q,
                "ticks": ticks,
                "tcp": tcp,
                "axis": axis,
                "error": path_deviation,
            })

        return {
            "turn_q": turn_q,
            "turn_ticks": turn_ticks,

            "turn_tcp": turn_tcp,
            "turn_axis": turn_axis,

            "target_z": target_z,
            "final_target": final_target,

            "points": points,

            "joint_blend": True,
            "blend_alpha": alpha,
            "max_path_deviation":
                max(
                    p["error"]
                    for p in points
                ),
        }

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
            90.0,          # TOOL DOWN
            seed_q,
        )

        q = (
            solved["q"]
            .copy()
        )

        ticks = (
            solved["ticks"]
            .copy()
        )

        # 사람 방향 J1 고정
        expected_j1 = float(
            handoff["angle_deg"]
        )

        if (
            abs(q[0] - expected_j1)
            > 0.2
        ):
            raise RuntimeError(
                "J1 direction mismatch | "
                f"{q[0]:.3f} vs "
                f"{expected_j1:.3f}"
            )

        # 모든 joint limit
        if (
            np.any(q < lower)
            or np.any(q > upper)
        ):
            raise RuntimeError(
                "HANDOFF joint limit exceeded | "
                f"q={np.round(q,3).tolist()}"
            )

        # 실물 J4 제한
        if (
            int(ticks[3])
            > core.J4_SOFT_MAX_TICK
        ):
            raise RuntimeError(
                "HANDOFF J4 soft max exceeded | "
                f"{ticks[3]} > "
                f"{core.J4_SOFT_MAX_TICK}"
            )

        # tool-down 조건
        total = (
            q[1]
            + q[2]
            + q[3]
        )

        if abs(total - 180.0) > 0.2:
            raise RuntimeError(
                "tool-down sum mismatch | "
                f"{total:.3f}"
            )

        tcp, axis = tcp_from_q(
            chain,
            q,
        )

        if axis[2] > -0.99:
            raise RuntimeError(
                "tool axis lost | "
                f"{axis.tolist()}"
            )

        points.append({
            "name":
                f"FULLIK_HANDOFF_{i}",

            "target": target,
            "q": q,
            "ticks": ticks,
            "tcp": tcp,
            "axis": axis,
            "error":
                float(
                    solved["tcp_error"]
                ),
        })

        seed_q = q

    return {
        "turn_q": turn_q,
        "turn_ticks": turn_ticks,

        "turn_tcp": turn_tcp,
        "turn_axis": turn_axis,

        "target_z": target_z,
        "final_target": final_target,

        "points": points,
    }


# ============================================================
# OUTPUT
# ============================================================

def print_plan(
    person,
    handoff,
    motion,
):

    final = (
        motion["points"][-1]
    )

    q = final["q"]
    tcp = final["tcp"]
    axis = final["axis"]

    print()
    print("=" * 80)

    print(
        "[PERSON]"
        f" X={person[0]:+.1f}"
        f" Y={person[1]:+.1f}"
        f" Z={person[2]:+.1f} mm"
    )

    print(
        f"distance       = "
        f"{handoff['person_r']:.1f} mm"
    )

    print(
        f"direction      = "
        f"{handoff['angle_deg']:+.2f} deg"
    )

    print(
        f"wanted radius  = "
        f"{handoff['wanted_radius']:.1f} mm"
    )

    print(
        f"used radius    = "
        f"{handoff['handoff_radius']:.1f} mm"
    )

    print(
        f"final standoff = "
        f"{handoff['actual_standoff']:.1f} mm"
    )

    print()

    print(
        "[TURN]"
        f" J1={motion['turn_q'][0]:+.2f} deg"
    )

    print(
        "[FINAL TCP]"
        f" X={tcp[0]:+.1f}"
        f" Y={tcp[1]:+.1f}"
        f" Z={tcp[2]:+.1f} mm"
    )

    print(
        "[TOOL AXIS] ",
        np.round(
            axis,
            5,
        ).tolist(),
    )

    print(
        "[FINAL Q] ",
        np.round(
            q,
            3,
        ).tolist(),
    )

    print(
        "[FINAL ticks] ",
        final["ticks"]
        .astype(int)
        .tolist(),
    )

    print(
        f"J2+J3+J4 = "
        f"{q[1]+q[2]+q[3]:.4f} deg"
    )

    print(
        f"J4 tick = "
        f"{int(final['ticks'][3])} / "
        f"{core.J4_SOFT_MAX_TICK}"
    )

    print(
        f"TCP error = "
        f"{final['error']:.6f} mm"
    )

    print(
        f"path points = "
        f"{len(motion['points'])}"
    )

    print("=" * 80)


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--execute",
        action="store_true",
    )

    parser.add_argument(
        "--step",
        action="store_true",
    )

    args = parser.parse_args()

    if args.execute and not args.step:
        raise SystemExit(
            "실제 실행은 반드시 "
            "--execute --step 으로 실행하세요."
        )

    plan = core.build_plan(
        SPEC
    )

    rclpy.init()

    node = Runner(
        use_motor=args.execute
    )

    try:

        # ====================================================
        # DRY RUN
        # ====================================================

        if not args.execute:

            print(
                "\n[FULL IK SMALLBAG PREVIEW]"
                "\nMOTOR COMMAND = NONE"
            )

            person = (
                node.collect_pose()
            )

            handoff = (
                compute_handoff_target(
                    person,
                    EXEC_MAX_RADIUS_MM,
                )
            )

            motion = (
                build_fullik_path(
                    plan,
                    handoff,
                )
            )

            print_plan(
                person,
                handoff,
                motion,
            )

            return

        # ====================================================
        # REAL MOTOR EXECUTION
        # ====================================================

        node.wait_services()

        current = (
            node.get_current()
        )

        error = (
            current
            - core.START_TICKS
        )

        print(
            "current =",
            current.tolist(),
        )

        if np.any(
            np.abs(error)
            > core.START_TOL_TICKS
        ):
            raise RuntimeError(
                "START pose check failed | "
                f"error={error.tolist()}"
            )

        # ----------------------------------------------------
        # OPEN
        # ----------------------------------------------------

        input(
            "\n[1] GRIPPER OPEN"
            "\nEnter > "
        )

        node.trigger(
            node.open,
            "smallbag/OPEN",
        )

        # ----------------------------------------------------
        # PICK
        # ----------------------------------------------------

        input(
            "\n[2] READY -> GRASP"
            "\nEnter > "
        )

        node.move(
            "smallbag/PICK",
            [
                plan["READY"]["ticks"],
                plan["GRASP"]["ticks"],
            ],
        )

        # ----------------------------------------------------
        # CLOSE
        # ----------------------------------------------------

        input(
            "\n[3] GRIPPER CLOSE/HOLD"
            "\nEnter > "
        )

        node.trigger(
            node.close,
            "smallbag/CLOSE",
        )

        # ----------------------------------------------------
        # LIFT + PULL
        # ----------------------------------------------------

        input(
            "\n[4] LIFT -> PULL100"
            "\nEnter > "
        )

        node.move(
            "smallbag/LIFT_PULL",
            [
                plan["LIFT"]["ticks"],
                plan["PULL100"]["ticks"],
            ],
        )

        # ----------------------------------------------------
        # FRESH DRIVER POSE
        # ----------------------------------------------------

        print(
            "\n[POSE] fresh driver pose collecting..."
        )

        person = (
            node.collect_pose()
        )

        handoff = (
            compute_handoff_target(
                person,
                EXEC_MAX_RADIUS_MM,
            )
        )

        motion = (
            build_fullik_path(
                plan,
                handoff,
            )
        )

        print_plan(
            person,
            handoff,
            motion,
        )

        # ----------------------------------------------------
        # TURN
        # ----------------------------------------------------

        input(
            "\n[5] DYNAMIC TURN"
            "\n※ 첫 테스트에서는 사람이 "
            "팔 작업영역 밖에 있는지 확인"
            "\nEnter > "
        )

        node.move(
            "smallbag/FULLIK_TURN",
            [
                motion["turn_ticks"],
            ],
        )

        # ----------------------------------------------------
        # FULL IK HANDOFF
        # ----------------------------------------------------

        input(
            "\n[6] FULL IK HANDOFF"
            "\n※ tool-down 유지 / 최대 400 mm"
            "\nEnter > "
        )

        node.move(
            "smallbag/FULLIK_HANDOFF",
            [
                p["ticks"]
                for p
                in motion["points"]
            ],
        )

        # ----------------------------------------------------
        # OPEN HANDOVER
        # ----------------------------------------------------

        input(
            "\n[7] HANDOFF OPEN"
            "\nEnter > "
        )

        node.trigger(
            node.open,
            "smallbag/OPEN_HANDOVER",
        )

        # ----------------------------------------------------
        # HOME
        # ----------------------------------------------------

        input(
            "\n[8] RETURN START"
            "\nEnter > "
        )

        node.move(
            "smallbag/RETURN_START",
            [
                core.START_TICKS,
            ],
        )

        print(
            "\n[DONE] SMALLBAG FULL-IK HANDOFF"
        )

    finally:

        node.destroy_node()

        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
