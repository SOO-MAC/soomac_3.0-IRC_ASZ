#!/usr/bin/env python3

import numpy as np
from scipy.optimize import least_squares

from control_config.robot_config import (
    create_robot_chain,
    get_start_q_model_rad,
    model_q_to_ikpy_vector,
    model_deg_to_ticks,
    JOINT_LIMIT_LOWER_DEG,
    JOINT_LIMIT_UPPER_DEG,
    calculate_tool_yaw_state,
)


# ============================================================
# Pick & Place 설정
# ============================================================

SIDE_MOVE_MM = 100.0
APPROACH_MM = 50.0

PICK_XYZ_MM = np.array([
    200.0,
    0.0,
    120.0,
])

TARGET_YAW_DEG = 0.0

POSITION_TOLERANCE_MM = 2.0
TOOL_TOLERANCE_DEG = 5.0
YAW_TOLERANCE_DEG = 5.0


# ============================================================
# Waypoint
# ============================================================

def make_waypoints(pick_xyz):

    pick_xyz = np.asarray(
        pick_xyz,
        dtype=float
    )

    pick_above = (
        pick_xyz
        + np.array([
            0.0,
            0.0,
            APPROACH_MM,
        ])
    )

    place_xyz = (
        pick_xyz
        + np.array([
            0.0,
            SIDE_MOVE_MM,
            0.0,
        ])
    )

    place_above = (
        place_xyz
        + np.array([
            0.0,
            0.0,
            APPROACH_MM,
        ])
    )

    return [
        ("PICK_ABOVE", pick_above),
        ("PICK", pick_xyz),

        ("PICK_ABOVE_RETURN", pick_above),

        ("PLACE_ABOVE", place_above),
        ("PLACE", place_xyz),

        ("PLACE_ABOVE_RETURN", place_above),
    ]


# ============================================================
# Tool 값 읽기
# ============================================================

def read_tool_values(tool_state):

    tool_error = None
    yaw = None

    for key in (
        "tool_error",
        "tool_error_deg",
        "tool_down_error_deg",
    ):
        if key in tool_state:
            tool_error = float(
                tool_state[key]
            )
            break

    for key in (
        "yaw",
        "yaw_deg",
    ):
        if key in tool_state:
            yaw = float(
                tool_state[key]
            )
            break

    if tool_error is None or yaw is None:

        raise RuntimeError(
            "\nTool state key 확인 필요\n"
            f"keys = {list(tool_state.keys())}\n"
            f"value = {tool_state}"
        )

    return tool_error, yaw


# ============================================================
# FK
# ============================================================

def fk_state(chain, q_model_rad):

    ikpy_q = model_q_to_ikpy_vector(
        q_model_rad
    )

    transform = np.asarray(
        chain.forward_kinematics(
            ikpy_q
        ),
        dtype=float
    )

    xyz_mm = (
        transform[:3, 3]
        * 1000.0
    )

    rotation = transform[:3, :3]

    raw_tool_state = (
        calculate_tool_yaw_state(
            rotation
        )
    )

    tool_error, yaw = (
        read_tool_values(
            raw_tool_state
        )
    )

    return {
        "xyz": xyz_mm,
        "tool_error": tool_error,
        "yaw": yaw,
    }


# ============================================================
# 각도 차이
# ============================================================

def angle_difference_deg(
    actual_deg,
    target_deg
):

    return (
        actual_deg
        - target_deg
        + 180.0
    ) % 360.0 - 180.0


# ============================================================
# IK
# ============================================================

def solve_ik(
    chain,
    target_xyz,
    target_yaw_deg,
    seed_q
):

    target_xyz = np.asarray(
        target_xyz,
        dtype=float
    )

    seed_q = np.asarray(
        seed_q,
        dtype=float
    )

    lower_rad = np.deg2rad(
        np.asarray(
            JOINT_LIMIT_LOWER_DEG,
            dtype=float
        )
    )

    upper_rad = np.deg2rad(
        np.asarray(
            JOINT_LIMIT_UPPER_DEG,
            dtype=float
        )
    )

    seed_q = np.clip(
        seed_q,
        lower_rad + 1e-6,
        upper_rad - 1e-6,
    )


    # ========================================================
    # Residual
    #
    # XYZ + Tool Down + Yaw
    # ========================================================

    def residual(q_rad):

        state = fk_state(
            chain,
            q_rad
        )

        position_residual = (
            state["xyz"]
            - target_xyz
        ) / 10.0

        tool_residual = np.array([
            state["tool_error"]
            / 5.0
        ])

        yaw_error = (
            angle_difference_deg(
                state["yaw"],
                target_yaw_deg
            )
        )

        yaw_residual = np.array([
            yaw_error / 5.0
        ])

        return np.concatenate([
            position_residual,
            tool_residual,
            yaw_residual,
        ])


    result = least_squares(
        residual,
        seed_q,

        bounds=(
            lower_rad,
            upper_rad,
        ),

        method="trf",

        max_nfev=1000,

        ftol=1e-10,
        xtol=1e-10,
        gtol=1e-10,
    )


    q_rad = result.x

    q_deg = np.rad2deg(
        q_rad
    )

    state = fk_state(
        chain,
        q_rad
    )


    position_error = float(
        np.linalg.norm(
            state["xyz"]
            - target_xyz
        )
    )

    tool_error = float(
        state["tool_error"]
    )

    yaw_error = abs(
        angle_difference_deg(
            state["yaw"],
            target_yaw_deg
        )
    )


    inside_limit = bool(
        np.all(
            q_deg >=
            np.asarray(
                JOINT_LIMIT_LOWER_DEG
            )
        )
        and
        np.all(
            q_deg <=
            np.asarray(
                JOINT_LIMIT_UPPER_DEG
            )
        )
    )


    success = bool(

        position_error
        <= POSITION_TOLERANCE_MM

        and

        tool_error
        <= TOOL_TOLERANCE_DEG

        and

        yaw_error
        <= YAW_TOLERANCE_DEG

        and

        inside_limit
    )


    # ========================================================
    # ★ Joint angle → Dynamixel tick
    # ========================================================

    if success:

        goal_ticks = model_deg_to_ticks(
            q_deg
        )

    else:

        goal_ticks = None


    return {
        "success": success,

        "q_rad": q_rad,
        "q_deg": q_deg,

        "goal_ticks": goal_ticks,

        "target_xyz": target_xyz,
        "fk_xyz": state["xyz"],

        "position_error": position_error,

        "tool_error": tool_error,

        "target_yaw": target_yaw_deg,
        "actual_yaw": state["yaw"],
        "yaw_error": yaw_error,

        "inside_limit": inside_limit,
    }


# ============================================================
# 출력
# ============================================================

def print_result(name, result):

    print("--------------------------------------")
    print(name)
    print("--------------------------------------")

    print(
        "Target XYZ :",
        np.round(
            result["target_xyz"],
            2
        )
    )

    print(
        "Joint q    :",
        np.round(
            result["q_deg"],
            2
        )
    )

    print(
        "Goal ticks :",
        result["goal_ticks"]
    )

    print(
        "FK XYZ     :",
        np.round(
            result["fk_xyz"],
            2
        )
    )

    print(
        f'Position error : '
        f'{result["position_error"]:.3f} mm'
    )

    print(
        f'Tool down error : '
        f'{result["tool_error"]:.3f} deg'
    )

    print(
        f'Yaw error : '
        f'{result["yaw_error"]:.3f} deg'
    )

    print(
        "Joint limit :",
        "OK"
        if result["inside_limit"]
        else "FAIL"
    )

    print(
        "IK RESULT :",
        "SUCCESS"
        if result["success"]
        else "FAIL"
    )

    print()


# ============================================================
# Main
# ============================================================

def main():

    chain = create_robot_chain()

    seed_q = (
        get_start_q_model_rad()
    )

    waypoints = make_waypoints(
        PICK_XYZ_MM
    )


    print()
    print("======================================")
    print(" SIMPLE PICK & PLACE")
    print(" IK -> JOINT -> DYNAMIXEL TICK")
    print(" DRY RUN : MOTOR DOES NOT MOVE")
    print("======================================")
    print()


    all_success = True


    for name, target_xyz in waypoints:

        result = solve_ik(
            chain=chain,

            target_xyz=target_xyz,

            target_yaw_deg=TARGET_YAW_DEG,

            seed_q=seed_q,
        )


        print_result(
            name,
            result
        )


        if not result["success"]:

            all_success = False

            print(
                "IK 실패 - 여기서 중단"
            )

            break


        seed_q = result["q_rad"]


    print("======================================")


    if all_success:

        print(
            "ALL WAYPOINTS READY"
        )

        print(
            "아직 실제 모터에는 명령을 보내지 않았음."
        )

    else:

        print(
            "DRY RUN FAILED"
        )


    print("======================================")
    print()


if __name__ == "__main__":
    main()
