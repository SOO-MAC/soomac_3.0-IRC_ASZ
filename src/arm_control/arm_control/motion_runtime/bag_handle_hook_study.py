#!/usr/bin/env python3

import argparse
import sys
import numpy as np
from scipy.optimize import least_squares

sys.path.insert(0, "study_arm")
import simple_pick_place as spp


def full_fk(chain, q_rad):
    """
    q_rad = [J1,J2,J3,J4,J5]
    IKPy 전체 chain transform 반환
    """
    q_rad = np.asarray(q_rad, dtype=float)

    full_q = np.zeros(
        len(chain.links),
        dtype=float,
    )

    active = np.asarray(
        chain.active_links_mask,
        dtype=bool,
    )

    full_q[active] = q_rad

    return chain.forward_kinematics(
        full_q
    )


def pose_state(chain, q_rad):

    T = full_fk(
        chain,
        q_rad,
    )

    xyz_mm = (
        T[:3, 3]
        * 1000.0
    )

    R = T[:3, :3]

    # 이전 FK에서 확인했듯
    # local +Z = 그리퍼 길이 방향(tool axis)
    tool_axis = R[:, 2]

    # J5 회전 시 같이 도는
    # 그리퍼 내부 X/Y 방향도 출력
    finger_x = R[:, 0]
    finger_y = R[:, 1]

    return {
        "xyz": xyz_mm,
        "R": R,
        "tool_axis": tool_axis,
        "finger_x": finger_x,
        "finger_y": finger_y,
    }


def axis_angle_error_deg(a, b):

    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)

    a /= np.linalg.norm(a)
    b /= np.linalg.norm(b)

    dot = np.clip(
        np.dot(a, b),
        -1.0,
        1.0,
    )

    return float(
        np.rad2deg(
            np.arccos(dot)
        )
    )


def solve_sideways_pose(
    chain,
    target_xyz,
    target_axis,
    seed_deg,
):

    lower_deg = np.asarray(
        spp.JOINT_LIMIT_LOWER_DEG,
        dtype=float,
    )

    upper_deg = np.asarray(
        spp.JOINT_LIMIT_UPPER_DEG,
        dtype=float,
    )

    lower = np.deg2rad(
        lower_deg
    )

    upper = np.deg2rad(
        upper_deg
    )

    seed = np.deg2rad(
        np.asarray(
            seed_deg,
            dtype=float,
        )
    )

    seed = np.clip(
        seed,
        lower + 1e-6,
        upper - 1e-6,
    )

    target_xyz = np.asarray(
        target_xyz,
        dtype=float,
    )

    target_axis = np.asarray(
        target_axis,
        dtype=float,
    )

    target_axis /= np.linalg.norm(
        target_axis
    )

    def residual(q):

        state = pose_state(
            chain,
            q,
        )

        # XYZ
        pos_res = (
            state["xyz"]
            - target_xyz
        ) / 10.0

        # tool axis 방향
        axis_res = (
            state["tool_axis"]
            - target_axis
        ) * 10.0

        # J5는 우선 0도로 두고
        # 나중에 따로 회전시킨다.
        j5_res = np.array([
            np.rad2deg(q[4])
            / 10.0
        ])

        return np.concatenate([
            pos_res,
            axis_res,
            j5_res,
        ])

    result = least_squares(
        residual,
        seed,
        bounds=(lower, upper),
        max_nfev=3000,
        ftol=1e-11,
        xtol=1e-11,
        gtol=1e-11,
    )

    q = result.x

    state = pose_state(
        chain,
        q,
    )

    position_error = float(
        np.linalg.norm(
            state["xyz"]
            - target_xyz
        )
    )

    axis_error = axis_angle_error_deg(
        state["tool_axis"],
        target_axis,
    )

    return {
        "q_rad": q,
        "q_deg": np.rad2deg(q),
        "state": state,
        "position_error": position_error,
        "axis_error": axis_error,
        "success": (
            position_error <= 3.0
            and axis_error <= 2.0
        ),
    }


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--side",
        choices=[
            "deck",
            "vehicle",
        ],
        default="deck",
    )

    parser.add_argument(
        "--reach",
        type=float,
        default=350.0,
        help="Base에서 목표까지 수평거리 [mm]",
    )

    parser.add_argument(
        "--z",
        type=float,
        default=180.0,
        help="목표 높이 [mm]",
    )

    args = parser.parse_args()

    chain = spp.create_robot_chain()

    if args.side == "deck":

        target_xyz = np.array([
            args.reach,
            0.0,
            args.z,
        ])

        # 손가락 길이방향을 +X로 눕힘
        target_axis = np.array([
            1.0,
            0.0,
            0.0,
        ])

        seeds = [
            [0, 30, 70, 20, 0],
            [0, 50, 40, 10, 0],
            [0, 70, 20, 5, 0],
            [0, 85, 5, 5, 0],
        ]

    else:

        target_xyz = np.array([
            -args.reach,
            0.0,
            args.z,
        ])

        # 차량 방향 -X로 눕힘
        target_axis = np.array([
            -1.0,
            0.0,
            0.0,
        ])

        seeds = [
            [179, 30, 70, 20, 0],
            [179, 50, 40, 10, 0],
            [179, 70, 20, 5, 0],
            [179, 85, 5, 5, 0],
        ]

    results = []

    for seed in seeds:

        result = solve_sideways_pose(
            chain,
            target_xyz,
            target_axis,
            seed,
        )

        results.append(
            result
        )

    results.sort(
        key=lambda r: (
            not r["success"],
            r["position_error"]
            + r["axis_error"],
        )
    )

    best = results[0]

    print()
    print("====================================================")
    print(" BAG HANDLE SIDEWAYS HOOK STUDY")
    print("====================================================")
    print()
    print("※ 계산만 수행")
    print("※ 모터 명령 없음")
    print()

    print(
        "Target XYZ      =",
        target_xyz.tolist(),
        "mm",
    )

    print(
        "Target tool axis=",
        target_axis.tolist(),
    )

    print()
    print("BEST SIDEWAYS POSE")
    print("-------------------------------")

    q = best["q_deg"]

    print(
        "success =",
        best["success"],
    )

    print(
        "q =",
        np.round(
            q,
            2,
        ).tolist(),
        "deg",
    )

    print(
        "ticks =",
        np.asarray(
            spp.model_deg_to_ticks(q),
            dtype=int,
        ).tolist(),
    )

    print(
        "XYZ =",
        np.round(
            best["state"]["xyz"],
            2,
        ).tolist(),
        "mm",
    )

    print(
        "tool axis =",
        np.round(
            best["state"]["tool_axis"],
            4,
        ).tolist(),
    )

    print(
        f"position error = "
        f"{best['position_error']:.3f} mm"
    )

    print(
        f"axis error     = "
        f"{best['axis_error']:.3f} deg"
    )

    # ========================================================
    # J5만 회전시켜서
    # position / tool axis 유지 여부 확인
    # ========================================================

    print()
    print("====================================================")
    print(" J5 GRIPPER ROTATION")
    print("====================================================")
    print()
    print(
        "같은 팔 자세에서 J5만 돌려서"
    )
    print(
        "손가락 방향을 바꿀 수 있는지 확인"
    )
    print()

    for j5 in [
        0.0,
        45.0,
        90.0,
        135.0,
        180.0,
    ]:

        q_test = q.copy()
        q_test[4] = j5

        state = pose_state(
            chain,
            np.deg2rad(q_test),
        )

        print(
            f"J5={j5:6.1f} deg"
        )

        print(
            " XYZ       =",
            np.round(
                state["xyz"],
                2,
            ).tolist(),
        )

        print(
            " tool axis =",
            np.round(
                state["tool_axis"],
                3,
            ).tolist(),
        )

        print(
            " finger X  =",
            np.round(
                state["finger_x"],
                3,
            ).tolist(),
        )

        print()

    print("====================================================")
    print("해석")
    print("====================================================")
    print()
    print("tool axis가 거의 수평이면 그리퍼가 옆으로 누운 상태.")
    print()
    print("J5를 돌렸는데 XYZ와 tool axis가 그대로이고")
    print("finger X 방향만 바뀐다면:")
    print()
    print("  1. 손잡이 앞까지 접근")
    print("  2. J5로 한쪽 finger 방향 맞춤")
    print("  3. 손잡이 내부로 접근")
    print("  4. 들어올려 걸기")
    print()
    print("방식이 기구학적으로 가능하다는 뜻.")
    print()
    print("실제 삽입 거리/높이는 새 그리퍼 치수 확정 후 결정.")
    print()


if __name__ == "__main__":
    main()
