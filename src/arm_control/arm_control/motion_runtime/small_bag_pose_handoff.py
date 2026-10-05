#!/usr/bin/env python3

import argparse
import math
import time
from collections import deque

import numpy as np

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import PointStamped
from std_msgs.msg import Int32MultiArray
from std_srvs.srv import Trigger

from soomac_interfaces.srv import MoveTickPath

import bag_tooldown_family_core as core


# ============================================================
# SMALL BAG SPEC
# ============================================================

SPEC = core.BagSpec(
    name="smallbag_pose",

    width_mm=127.0,
    depth_mm=81.0,
    height_mm=232.0,

    x_mm=170.0,
    y_mm=-150.0,

    ready_z_mm=202.0,
    ready_tilt_deg=70.0,

    grasp_z_mm=102.0,
    grasp_tilt_deg=90.0,

    lift_z_mm=170.0,
    lift_tilt_deg=90.0,
)


# ============================================================
# DRIVER POSE / HANDOFF SETTINGS
# ============================================================

POSE_TOPIC = "/driver_pose/target_base"

POSE_WINDOW = 5
POSE_MIN_SAMPLES = 3
POSE_TIMEOUT_SEC = 10.0

# 사람 어깨보다 이만큼 앞에서 정지
STANDOFF_MM = 300.0

# 현재 smallbag에서 이미 검증된 extension 범위 안에서 사용
MIN_HANDOFF_RADIUS_MM = 300.0
# Preview에서는 현재 자세 + joint limit으로 최대 reach 자동 계산.
# 실제 모터에서 검증된 범위는 아직 400 mm까지.
EXECUTION_VALIDATED_MAX_MM = 400.0

# 사람은 기본적으로 로봇 뒤쪽에 있어야 함
MAX_REAR_DEVIATION_DEG = 45.0

# 너무 흔들리는 pose는 사용하지 않음
MAX_POSE_XY_SPREAD_MM = 120.0


# ============================================================
# Helpers
# ============================================================

def rear_deviation_deg(angle_deg):
    """
    rear = +/-180 deg 기준 편차.
    +157 deg -> 23 deg
    -170 deg -> 10 deg
    """
    d = ((angle_deg - 180.0 + 180.0) % 360.0) - 180.0
    return abs(d)



def compute_tooldown_reach_limits(pull_q):
    """
    현재 smallbag tool-down 자세에서 가능한 radial reach 계산.

    조건:
      J1 : handoff 방향으로 별도 결정
      J4 : 현재 PULL 자세 유지
      J5 : 고정
      J2 + J3 : 유지
      joint limits 만족

    tool-down geometry:
      r = L2*sin(J2) + L3*sin(J2+J3)
    """

    q = np.asarray(
        pull_q,
        dtype=float,
    ).reshape(5)

    lower = np.asarray(
        core.cfg.JOINT_LIMIT_LOWER_DEG,
        dtype=float,
    )

    upper = np.asarray(
        core.cfg.JOINT_LIMIT_UPPER_DEG,
        dtype=float,
    )

    sum23 = float(
        q[1] + q[2]
    )

    # J3 = sum23 - J2
    #
    # J2 limit:
    #   lower2 <= J2 <= upper2
    #
    # J3 limit:
    #   lower3 <= sum23-J2 <= upper3
    #
    # 따라서 가능한 J2 구간을 교집합으로 계산.
    j2_min = max(
        float(lower[1]),
        sum23 - float(upper[2]),
    )

    j2_max = min(
        float(upper[1]),
        sum23 - float(lower[2]),
    )

    if j2_min > j2_max:
        raise RuntimeError(
            "no feasible J2/J3 interval | "
            f"J2=[{j2_min:.3f},{j2_max:.3f}]"
        )

    def radius_for_j2(j2_deg):
        return (
            core.LINK2_MM
            * math.sin(
                math.radians(j2_deg)
            )
            +
            core.LINK3_MM
            * math.sin(
                math.radians(sum23)
            )
        )

    # sin(J2)의 극값까지 포함해서 정확한 min/max 탐색.
    candidates = [
        j2_min,
        j2_max,
    ]

    for k in range(-4, 5):
        critical = 90.0 + 180.0 * k

        if (
            j2_min
            <= critical
            <= j2_max
        ):
            candidates.append(
                critical
            )

    candidate_data = []

    for j2 in candidates:

        j3 = sum23 - j2
        radius = radius_for_j2(j2)

        candidate_data.append(
            (
                float(radius),
                float(j2),
                float(j3),
            )
        )

    min_item = min(
        candidate_data,
        key=lambda x: x[0],
    )

    max_item = max(
        candidate_data,
        key=lambda x: x[0],
    )

    if max_item[0] <= 0.0:
        raise RuntimeError(
            "invalid maximum radial reach | "
            f"{max_item[0]:.3f} mm"
        )

    return {
        "sum23": sum23,

        "j2_min": j2_min,
        "j2_max": j2_max,

        "min_radius": float(min_item[0]),
        "max_radius": float(max_item[0]),

        "max_j2": float(max_item[1]),
        "max_j3": float(max_item[2]),
    }



def compute_handoff_target(person_xyz_mm, max_radius_mm):
    x, y, z = [
        float(v)
        for v in person_xyz_mm
    ]

    person_r = math.hypot(x, y)

    if person_r < 1.0:
        raise RuntimeError(
            "invalid person distance"
        )

    # 카메라는 로봇 뒤쪽을 바라보고 있음
    if x >= 0.0:
        raise RuntimeError(
            f"person is not behind robot | X={x:.1f}"
        )

    angle_deg = math.degrees(
        math.atan2(y, x)
    )

    rear_dev = rear_deviation_deg(
        angle_deg
    )

    if rear_dev > MAX_REAR_DEVIATION_DEG:
        raise RuntimeError(
            "person direction too far from rear | "
            f"angle={angle_deg:.2f} deg, "
            f"rear deviation={rear_dev:.2f} deg"
        )

    wanted_radius = (
        person_r - STANDOFF_MM
    )

    # 너무 가까우면 억지로 최소거리까지 뻗지 않고 중단
    if wanted_radius < MIN_HANDOFF_RADIUS_MM:
        raise RuntimeError(
            "person too close for safe handoff | "
            f"person_r={person_r:.1f} mm, "
            f"wanted_r={wanted_radius:.1f} mm"
        )

    max_radius_mm = float(
        max_radius_mm
    )

    if (
        max_radius_mm
        < MIN_HANDOFF_RADIUS_MM
    ):
        raise RuntimeError(
            "kinematic max reach too short | "
            f"max_radius={max_radius_mm:.1f} mm"
        )

    handoff_radius = min(
        wanted_radius,
        max_radius_mm,
    )

    ux = x / person_r
    uy = y / person_r

    target_x = ux * handoff_radius
    target_y = uy * handoff_radius

    actual_standoff = (
        person_r - handoff_radius
    )

    return {
        "person_x": x,
        "person_y": y,
        "person_z": z,

        "person_r": person_r,

        "angle_deg": angle_deg,
        "rear_dev_deg": rear_dev,

        "wanted_radius": wanted_radius,
        "handoff_radius": handoff_radius,

        "target_x": target_x,
        "target_y": target_y,

        "actual_standoff": actual_standoff,
    }


# ============================================================
# Dynamic TURN + EXTEND
# ============================================================

def build_dynamic_handoff(
    pull_q,
    handoff,
):
    pull_q = np.asarray(
        pull_q,
        dtype=float,
    ).reshape(5)

    target_j1 = float(
        handoff["angle_deg"]
    )

    target_radius = float(
        handoff["handoff_radius"]
    )

    lower = np.asarray(
        core.cfg.JOINT_LIMIT_LOWER_DEG,
        dtype=float,
    )

    upper = np.asarray(
        core.cfg.JOINT_LIMIT_UPPER_DEG,
        dtype=float,
    )

    if not (
        lower[0]
        <= target_j1
        <= upper[0]
    ):
        raise RuntimeError(
            f"dynamic J1 limit exceeded: "
            f"{target_j1:.2f} deg"
        )

    # --------------------------------------------------------
    # TURN
    # --------------------------------------------------------

    turn_q = pull_q.copy()

    turn_q[0] = target_j1
    turn_q[4] = core.J5_DEG

    turn_ticks = np.asarray(
        core.cfg.model_deg_to_ticks(
            turn_q
        ),
        dtype=np.int64,
    )

    if (
        int(turn_ticks[3])
        > core.J4_SOFT_MAX_TICK
    ):
        raise RuntimeError(
            "dynamic TURN J4 soft max exceeded"
        )

    # --------------------------------------------------------
    # Dynamic radial extension
    #
    # 기존 smallbag rule 그대로:
    #   J1 fixed
    #   J4 fixed
    #   J5 fixed
    #   J2+J3 preserved
    #   tool-down preserved
    #
    # 단, radius만 300~400mm 사이에서 동적
    # --------------------------------------------------------

    sum23 = float(
        turn_q[1] + turn_q[2]
    )

    total_tooldown = float(
        sum23 + turn_q[3]
    )

    if abs(
        total_tooldown - 180.0
    ) > 0.2:
        raise RuntimeError(
            "tool-down sum mismatch | "
            f"{total_tooldown:.3f}"
        )

    rhs = (
        target_radius
        - core.LINK3_MM
        * math.sin(
            math.radians(sum23)
        )
    ) / core.LINK2_MM

    if not -1.0 <= rhs <= 1.0:
        raise RuntimeError(
            "dynamic EXTEND unreachable | "
            f"rhs={rhs:.6f}, "
            f"radius={target_radius:.1f}"
        )

    final_j2 = math.degrees(
        math.asin(rhs)
    )

    final_j3 = (
        sum23 - final_j2
    )

    final_q = np.array([
        target_j1,
        final_j2,
        final_j3,
        turn_q[3],
        core.J5_DEG,
    ])

    if np.any(final_q < lower) or np.any(
        final_q > upper
    ):
        raise RuntimeError(
            "dynamic final joint limit exceeded | "
            f"q={np.round(final_q,3).tolist()}"
        )

    points = []

    for i in range(
        1,
        core.EXTEND_SEGMENTS + 1,
    ):
        u = (
            i
            / core.EXTEND_SEGMENTS
        )

        q = (
            (1.0 - u) * turn_q
            + u * final_q
        )

        q[0] = target_j1
        q[3] = turn_q[3]
        q[4] = core.J5_DEG

        # J2+J3 유지
        q[2] = sum23 - q[1]

        ticks = np.asarray(
            core.cfg.model_deg_to_ticks(q),
            dtype=np.int64,
        )

        if (
            int(ticks[3])
            > core.J4_SOFT_MAX_TICK
        ):
            raise RuntimeError(
                "dynamic EXTEND J4 soft max exceeded"
            )

        points.append({
            "name":
                f"DYNAMIC_EXTEND_{i}",

            "q": q,
            "ticks": ticks,
        })

    # --------------------------------------------------------
    # 실제 모델 TCP 확인
    # --------------------------------------------------------

    chain = core.spp.create_robot_chain()

    state = core.pose_state(
        chain,
        np.deg2rad(final_q),
    )

    tool_axis = core.unit(
        state["tool_axis"]
    )

    tcp = (
        np.asarray(
            state["xyz"],
            dtype=float,
        )
        + core.AXIAL_OFFSET_MM
        * tool_axis
    )

    return {
        "turn_q": turn_q,
        "turn_ticks": turn_ticks,

        "extend": points,

        "final_q": final_q,
        "final_tcp": tcp,
    }


# ============================================================
# ROS NODE
# ============================================================

class Runner(Node):

    def __init__(
        self,
        use_motor=False,
    ):
        super().__init__(
            "smallbag_pose_handoff"
        )

        self.use_motor = use_motor

        self.pose_samples = deque(
            maxlen=POSE_WINDOW
        )

        self.current = None

        self.create_subscription(
            PointStamped,
            POSE_TOPIC,
            self.pose_cb,
            10,
        )

        if use_motor:
            self.create_subscription(
                Int32MultiArray,
                "/motor/joint_state",
                self.joint_cb,
                10,
            )

            self.path = self.create_client(
                MoveTickPath,
                core.PATH_SERVICE,
            )

            self.open = self.create_client(
                Trigger,
                core.OPEN_SERVICE,
            )

            self.close = self.create_client(
                Trigger,
                core.CLOSE_SERVICE,
            )

    def pose_cb(self, msg):
        x = float(msg.point.x) * 1000.0
        y = float(msg.point.y) * 1000.0
        z = float(msg.point.z) * 1000.0

        if not all(
            math.isfinite(v)
            for v in (x, y, z)
        ):
            return

        self.pose_samples.append(
            (x, y, z)
        )

    def joint_cb(self, msg):
        if len(msg.data) == 5:
            self.current = np.asarray(
                msg.data,
                dtype=np.int64,
            )

    def collect_pose(self):
        self.pose_samples.clear()

        deadline = (
            time.monotonic()
            + POSE_TIMEOUT_SEC
        )

        while (
            len(self.pose_samples)
            < POSE_WINDOW
            and time.monotonic()
            < deadline
        ):
            rclpy.spin_once(
                self,
                timeout_sec=0.05,
            )

        if (
            len(self.pose_samples)
            < POSE_MIN_SAMPLES
        ):
            raise RuntimeError(
                "not enough driver pose samples | "
                f"{len(self.pose_samples)}"
            )

        a = np.asarray(
            self.pose_samples,
            dtype=float,
        )

        median = np.median(
            a,
            axis=0,
        )

        # ----------------------------------------------------
        # Outlier rejection
        #
        # 한 프레임의 YOLO/depth 튐 때문에 전체 실패하지 않도록
        # median 기준 150 mm 이내 샘플만 사용한다.
        # ----------------------------------------------------

        xy_error = (
            a[:, :2]
            - median[:2]
        )

        distances = np.linalg.norm(
            xy_error,
            axis=1,
        )

        INLIER_RADIUS_MM = 150.0

        inlier_mask = (
            distances
            <= INLIER_RADIUS_MM
        )

        inliers = a[inlier_mask]

        if len(inliers) < POSE_MIN_SAMPLES:
            raise RuntimeError(
                "driver pose unstable | "
                f"inliers={len(inliers)}/"
                f"{len(a)}, "
                f"distances="
                f"{np.round(distances,1).tolist()}"
            )

        filtered_median = np.median(
            inliers,
            axis=0,
        )

        filtered_xy_error = (
            inliers[:, :2]
            - filtered_median[:2]
        )

        filtered_spread = float(
            np.max(
                np.linalg.norm(
                    filtered_xy_error,
                    axis=1,
                )
            )
        )

        print(
            "[POSE FILTER] "
            f"samples={len(a)}, "
            f"inliers={len(inliers)}, "
            f"spread={filtered_spread:.1f} mm"
        )

        return filtered_median

    def wait_services(self):
        for c, name in (
            (
                self.path,
                core.PATH_SERVICE,
            ),
            (
                self.open,
                core.OPEN_SERVICE,
            ),
            (
                self.close,
                core.CLOSE_SERVICE,
            ),
        ):
            if not c.wait_for_service(
                timeout_sec=5.0
            ):
                raise RuntimeError(
                    f"service unavailable: {name}"
                )

    def get_current(self):
        for _ in range(50):
            rclpy.spin_once(
                self,
                timeout_sec=0.1,
            )

            if self.current is not None:
                return self.current.copy()

        raise RuntimeError(
            "no /motor/joint_state"
        )

    def trigger(
        self,
        client,
        label,
    ):
        future = client.call_async(
            Trigger.Request()
        )

        rclpy.spin_until_future_complete(
            self,
            future,
        )

        res = future.result()

        if (
            res is None
            or not res.success
        ):
            raise RuntimeError(
                f"{label} failed: "
                f"{getattr(res,'message','no response')}"
            )

        print(
            f"[OK] {label}: "
            f"{res.message}"
        )

    def move(
        self,
        name,
        ticks_list,
    ):
        arr = np.asarray(
            ticks_list,
            dtype=np.int64,
        ).reshape(-1, 5)

        req = MoveTickPath.Request()

        req.joint_ticks = [
            int(v)
            for v in arr.reshape(-1)
        ]

        req.point_count = len(arr)
        req.label = name

        # 기존 motor node default 사용
        req.profile_velocity = 0
        req.timeout_sec = (
            core.PATH_TIMEOUT_SEC
        )

        future = self.path.call_async(
            req
        )

        rclpy.spin_until_future_complete(
            self,
            future,
        )

        res = future.result()

        if (
            res is None
            or not res.success
        ):
            raise RuntimeError(
                f"{name} failed: "
                f"{getattr(res,'message','no response')}"
            )

        print(
            f"[OK] {name} | "
            f"reached="
            f"{list(res.reached_ticks)}"
        )


# ============================================================
# Print
# ============================================================

def print_dynamic_plan(
    person,
    handoff,
    dynamic,
    reach,
):
    tcp = dynamic["final_tcp"]

    print()
    print("=" * 72)

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
        f"kinematic max  = "
        f"{reach['max_radius']:.1f} mm"
    )

    print(
        "J2 feasible    = "
        f"{reach['j2_min']:.2f}"
        f" ~ "
        f"{reach['j2_max']:.2f} deg"
    )

    print(
        "max reach pose = "
        f"J2 {reach['max_j2']:.2f} deg, "
        f"J3 {reach['max_j3']:.2f} deg"
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
        "[XY TARGET]"
        f" X={handoff['target_x']:+.1f}"
        f" Y={handoff['target_y']:+.1f} mm"
    )

    print(
        "[FINAL MODEL TCP]"
        f" X={tcp[0]:+.1f}"
        f" Y={tcp[1]:+.1f}"
        f" Z={tcp[2]:+.1f} mm"
    )

    print(
        "[TURN J1]"
        f" {dynamic['turn_q'][0]:+.2f} deg"
    )

    print("=" * 72)


# ============================================================
# Main
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
            "현재 pose handoff 검증 단계에서는 "
            "--execute --step 으로만 실행하세요."
        )

    # 기존 smallbag PICK/LIFT/PULL 계산
    plan = core.build_plan(
        SPEC
    )

    # 현재 smallbag PULL/tool-down 자세의
    # 실제 기구학적 radial reach 계산
    reach = compute_tooldown_reach_limits(
        plan["PULL100"]["q"]
    )

    print()
    print(
        "[KINEMATIC REACH] "
        f"{reach['min_radius']:.1f} "
        f"~ {reach['max_radius']:.1f} mm"
    )

    rclpy.init()

    node = Runner(
        use_motor=args.execute
    )

    try:

        # ====================================================
        # DRY / POSE PREVIEW
        # ====================================================

        if not args.execute:

            print(
                "\n[POSE PREVIEW]"
                " motor command 없음"
            )

            person = node.collect_pose()

            handoff = compute_handoff_target(
            person,
            reach["max_radius"],
        )

            dynamic = build_dynamic_handoff(
                plan["PULL100"]["q"],
                handoff,
            )

            print_dynamic_plan(
                person,
                handoff,
                dynamic,
                reach,
            )

            return

        # ====================================================
        # REAL EXECUTION
        # ====================================================

        node.wait_services()

        current = node.get_current()

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

        input(
            "\nGRIPPER OPEN"
            "\nEnter > "
        )

        node.trigger(
            node.open,
            f"{SPEC.name}/OPEN",
        )

        input(
            "\nREADY -> GRASP"
            "\nEnter > "
        )

        node.move(
            f"{SPEC.name}/PICK",
            [
                plan["READY"]["ticks"],
                plan["GRASP"]["ticks"],
            ],
        )

        input(
            "\nGRIPPER CLOSE"
            "\nEnter > "
        )

        node.trigger(
            node.close,
            f"{SPEC.name}/CLOSE",
        )

        input(
            "\nLIFT -> PULL100"
            "\nEnter > "
        )

        node.move(
            f"{SPEC.name}/LIFT_PULL",
            [
                plan["LIFT"]["ticks"],
                plan["PULL100"]["ticks"],
            ],
        )

        # ----------------------------------------------------
        # 여기서 현재 사람 pose 새로 캡처
        # ----------------------------------------------------

        print(
            "\n[POSE] fresh driver pose collecting..."
        )

        person = node.collect_pose()

        handoff = compute_handoff_target(
            person,
            reach["max_radius"],
        )

        if (
            handoff["handoff_radius"]
            > EXECUTION_VALIDATED_MAX_MM
            + 1.0e-6
        ):
            raise RuntimeError(
                "PREVIEW에서는 계산 가능하지만 "
                "실제 모터에서 아직 검증되지 않은 reach입니다 | "
                f"requested={handoff['handoff_radius']:.1f} mm, "
                f"validated={EXECUTION_VALIDATED_MAX_MM:.1f} mm"
            )

        dynamic = build_dynamic_handoff(
            plan["PULL100"]["q"],
            handoff,
        )

        print_dynamic_plan(
                person,
                handoff,
                dynamic,
                reach,
            )

        input(
            "\n위 HANDOFF 목표 확인."
            "\n첫 모션 검증 때는 사람을 팔 작업영역 밖으로 이동시킨 뒤"
            "\nEnter > "
        )

        node.move(
            f"{SPEC.name}/DYNAMIC_TURN",
            [
                dynamic["turn_ticks"],
            ],
        )

        input(
            "\nDYNAMIC EXTEND"
            "\nEnter > "
        )

        node.move(
            f"{SPEC.name}/DYNAMIC_EXTEND",
            [
                p["ticks"]
                for p in dynamic["extend"]
            ],
        )

        input(
            "\nHANDOFF 위치 도착."
            "\n그리퍼 OPEN 할 준비가 되면 Enter > "
        )

        node.trigger(
            node.open,
            f"{SPEC.name}/OPEN_HANDOVER",
        )

        input(
            "\nRETURN START"
            "\nEnter > "
        )

        node.move(
            f"{SPEC.name}/RETURN_START",
            [
                core.START_TICKS,
            ],
        )

        print(
            "\n[DONE] smallbag dynamic handoff"
        )

    finally:
        node.destroy_node()

        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
