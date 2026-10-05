#!/usr/bin/env python3

import argparse
import math
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np
import rclpy
from rclpy.qos import (
    QoSProfile,
    ReliabilityPolicy,
    DurabilityPolicy,
    HistoryPolicy,
)
from sensor_msgs.msg import CameraInfo
from vision_msgs.msg import Detection2DArray

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bag_tooldown_family_core as core

from paper_bag_full_sequence import (
    SPEC,
    PICK_MIN_RADIUS_MM,
    PICK_MAX_RADIUS_MM,
)

from small_bag_pose_handoff import (
    Runner,
    compute_handoff_target,
)

from small_bag_fullik_handoff_execute import (
    build_fullik_path,
    print_plan as print_handoff_plan,
)

from small_bag_full_sequence import SPEC as HANDOFF_REFERENCE_SPEC


# ============================================================
# WORLD-VIEW CAMERA CALIBRATION
#
# p_arm = R_CAM_TO_ARM @ p_camera + T_CAM_TO_ARM_MM
# Units: mm
# ============================================================

R_CAM_TO_ARM = np.array([
    [ 0.99954869, -0.02825663,  0.01019687],
    [-0.02364760, -0.53079331,  0.84717133],
    [-0.01852578, -0.84703012, -0.53122196],
], dtype=float)

T_CAM_TO_ARM_MM = np.array([
     293.40638902,
    -602.84060821,
     399.16724562,
], dtype=float)

TABLE_Z_MM = 0.0


# ============================================================
# DETECTION SETTINGS
# ============================================================

DETECTION_TOPIC = "/paper_bag/detections"
CAMERA_INFO_TOPIC = "/paper_bag/camera_info"

VISION_CLASS = "paper_bag"

MIN_CONFIDENCE = 0.55

BAG_WINDOW = 5
BAG_MIN_SAMPLES = 3
BAG_TIMEOUT_SEC = 10.0

MAX_BAG_XY_SPREAD_MM = 35.0

# Current detection node publishes body-mask centroid, theta and long-axis size.
# Until the detector publishes a true bottom-center pixel directly, estimate
# the table-contact pixel from the lower endpoint of the long axis.
USE_LONG_AXIS_BOTTOM_ESTIMATE = True


# ============================================================
# PICK SETTINGS
# ============================================================

PULL_MM = 100.0

TCP_ERROR_MAX_MM = 1.0

J3_MIN_TICK = 0
J3_MAX_TICK = 10606

J4_MIN_TICK = 700
J4_MAX_TICK = 3500

# Current physical test limit override used by the existing preview/test code.
core.cfg.JOINT_LIMIT_UPPER_DEG[2] = 150.0

if hasattr(core, "spp") and hasattr(
    core.spp,
    "JOINT_LIMIT_UPPER_DEG",
):
    core.spp.JOINT_LIMIT_UPPER_DEG[2] = 150.0

core.J4_SOFT_MAX_TICK = J4_MAX_TICK


# ============================================================
# PIXEL / GEOMETRY HELPERS
# ============================================================

def pick_pixel_from_detection(det):
    cx = float(det.bbox.center.position.x)
    cy = float(det.bbox.center.position.y)

    if not USE_LONG_AXIS_BOTTOM_ESTIMATE:
        return cx, cy

    theta = float(det.bbox.center.theta)
    long_px = float(det.bbox.size_x)

    half = 0.5 * long_px

    dx = half * math.cos(theta)
    dy = half * math.sin(theta)

    p1 = np.array(
        [cx - dx, cy - dy],
        dtype=float,
    )

    p2 = np.array(
        [cx + dx, cy + dy],
        dtype=float,
    )

    # Image +v points downward.
    bottom = (
        p1
        if p1[1] >= p2[1]
        else p2
    )

    return (
        float(bottom[0]),
        float(bottom[1]),
    )


def pixel_to_table_xy(K, u, v):
    fx = float(K[0, 0])
    fy = float(K[1, 1])
    cx = float(K[0, 2])
    cy = float(K[1, 2])

    ray_cam = np.array([
        (float(u) - cx) / fx,
        (float(v) - cy) / fy,
        1.0,
    ], dtype=float)

    ray_arm = (
        R_CAM_TO_ARM
        @ ray_cam
    )

    dz = float(ray_arm[2])

    if abs(dz) < 1.0e-9:
        raise RuntimeError(
            "camera ray parallel to table"
        )

    lam = (
        TABLE_Z_MM
        - T_CAM_TO_ARM_MM[2]
    ) / dz

    if lam <= 0.0:
        raise RuntimeError(
            "table intersection behind camera"
        )

    return (
        T_CAM_TO_ARM_MM
        + lam * ray_arm
    )


def check_pick_workspace(x_mm, y_mm):
    r = float(
        math.hypot(
            x_mm,
            y_mm,
        )
    )

    if x_mm <= 0.0:
        raise RuntimeError(
            f"pick target is not in front workspace | X={x_mm:.1f} mm"
        )

    if r < PICK_MIN_RADIUS_MM:
        raise RuntimeError(
            f"pick target too close | "
            f"R={r:.1f} < "
            f"{PICK_MIN_RADIUS_MM:.1f} mm"
        )

    if r > PICK_MAX_RADIUS_MM:
        raise RuntimeError(
            f"pick target outside safe workspace | "
            f"R={r:.1f} > "
            f"{PICK_MAX_RADIUS_MM:.1f} mm"
        )

    return r


# ============================================================
# IK HELPERS
# ============================================================

def validate_solution(name, solved):
    q = np.asarray(
        solved["q"],
        dtype=float,
    )

    ticks = np.asarray(
        solved["ticks"],
        dtype=np.int64,
    )

    err = float(
        solved["tcp_error"]
    )

    if err > TCP_ERROR_MAX_MM:
        raise RuntimeError(
            f"{name}: TCP error "
            f"{err:.3f} mm > "
            f"{TCP_ERROR_MAX_MM:.3f} mm"
        )

    if not (
        J3_MIN_TICK
        <= int(ticks[2])
        <= J3_MAX_TICK
    ):
        raise RuntimeError(
            f"{name}: J3 raw limit | "
            f"{int(ticks[2])} not in "
            f"[{J3_MIN_TICK},{J3_MAX_TICK}]"
        )

    if not (
        J4_MIN_TICK
        <= int(ticks[3])
        <= J4_MAX_TICK
    ):
        raise RuntimeError(
            f"{name}: J4 raw limit | "
            f"{int(ticks[3])} not in "
            f"[{J4_MIN_TICK},{J4_MAX_TICK}]"
        )

    return {
        "q": q,
        "ticks": ticks,
        "error": err,
    }


def solve_phase(
    chain,
    name,
    target,
    tilt_deg,
    seed_q,
):
    solved = core.solve_tcp(
        chain,
        np.asarray(
            target,
            dtype=float,
        ),
        float(tilt_deg),
        np.asarray(
            seed_q,
            dtype=float,
        ),
    )

    checked = validate_solution(
        name,
        solved,
    )

    checked["target"] = np.asarray(
        target,
        dtype=float,
    )

    checked["tilt"] = float(
        tilt_deg
    )

    return checked


# ============================================================
# PAPER BAG PICK WRIST ORIENTATION
#
# READY -> GRASP -> LIFT:
#   J5는 모터 START 자세 그대로 유지 (2048 tick ~= 0 deg)
#
# PULL100 이후:
#   기존 bag core orientation 사용 (J5=90 deg / 3072 tick)
# ============================================================

PICK_J5_DEG = 0.0
PICK_J5_TICK = 2048


def _fix_pick_j5(plan):
    plan = dict(plan)

    q = np.asarray(
        plan["q"],
        dtype=float,
    ).copy()

    ticks = np.asarray(
        plan["ticks"],
        dtype=np.int64,
    ).copy()

    q[4] = PICK_J5_DEG
    ticks[4] = PICK_J5_TICK

    plan["q"] = q
    plan["ticks"] = ticks

    return plan


def build_dynamic_pick_plan(x_mm, y_mm):
    radius = check_pick_workspace(
        x_mm,
        y_mm,
    )

    chain = (
        core.spp.create_robot_chain()
    )

    # Use the fixed paper_bag reference only as the first IK seed.
    ref_plan = core.build_plan(
        SPEC
    )

    seed_q = (
        ref_plan["READY"]["q"]
        .copy()
    )

    out = {}

    phase_defs = [
        (
            "READY",
            np.array([
                x_mm,
                y_mm,
                float(SPEC.ready_z_mm),
            ]),
            float(SPEC.ready_tilt_deg),
        ),
        (
            "GRASP",
            np.array([
                x_mm,
                y_mm,
                float(SPEC.grasp_z_mm),
            ]),
            float(SPEC.grasp_tilt_deg),
        ),
        (
            "LIFT",
            np.array([
                x_mm,
                y_mm,
                float(SPEC.lift_z_mm),
            ]),
            float(SPEC.lift_tilt_deg),
        ),
    ]

    for name, target, tilt in phase_defs:
        p = solve_phase(
            chain,
            name,
            target,
            tilt,
            seed_q,
        )

        # PICK 단계에서는 손목(J5)을 START 자세로 유지한다.
        # J1~J4 IK 결과는 그대로 사용.
        p = _fix_pick_j5(p)

        out[name] = p
        seed_q = p["q"].copy()

    # Pull radially inward by 100 mm while preserving azimuth and tool-down.
    pull_radius = (
        radius
        - PULL_MM
    )

    if pull_radius <= 0.0:
        raise RuntimeError(
            f"invalid pull radius | {pull_radius:.1f} mm"
        )

    ux = float(x_mm) / radius
    uy = float(y_mm) / radius

    pull_target = np.array([
        ux * pull_radius,
        uy * pull_radius,
        float(SPEC.lift_z_mm),
    ])

    pull = solve_phase(
        chain,
        "PULL100",
        pull_target,
        90.0,
        out["LIFT"]["q"],
    )

    # paper_bag은 별도 wrist orientation을 사용하지 않는다.
    # READY/GRASP/LIFT와 동일하게 J5=0deg(2048) 유지.
    pull = _fix_pick_j5(pull)

    out["PULL100"] = pull

    return radius, out


# ============================================================
# ROS RUNNER
# ============================================================

class WorldViewPaperBagRunner(Runner):
    def __init__(self, use_motor=False):
        super().__init__(
            use_motor=use_motor
        )

        self.K = None

        self.bag_samples = deque(
            maxlen=BAG_WINDOW
        )

        latched = QoSProfile(
            depth=1,
            history=HistoryPolicy.KEEP_LAST,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )

        self.create_subscription(
            CameraInfo,
            CAMERA_INFO_TOPIC,
            self.camera_info_cb,
            latched,
        )

        self.create_subscription(
            Detection2DArray,
            DETECTION_TOPIC,
            self.detection_cb,
            10,
        )

    def camera_info_cb(self, msg):
        K = np.asarray(
            msg.k,
            dtype=float,
        ).reshape(3, 3)

        if (
            not np.isfinite(K).all()
            or K[0, 0] <= 0.0
            or K[1, 1] <= 0.0
        ):
            return

        self.K = K

    def detection_cb(self, msg):
        if self.K is None:
            return

        candidates = []

        for det in msg.detections:
            if not det.results:
                continue

            class_id = str(
                det.results[0]
                .hypothesis
                .class_id
            )

            score = float(
                det.results[0]
                .hypothesis
                .score
            )

            if (
                class_id
                != VISION_CLASS
            ):
                continue

            if (
                score
                < MIN_CONFIDENCE
            ):
                continue

            candidates.append(
                (
                    score,
                    det,
                )
            )

        if not candidates:
            return

        # One delivery request uses the highest-confidence handle-less bag.
        score, det = max(
            candidates,
            key=lambda x: x[0],
        )

        try:
            u, v = (
                pick_pixel_from_detection(
                    det
                )
            )

            p_arm = pixel_to_table_xy(
                self.K,
                u,
                v,
            )

            x_mm = float(
                p_arm[0]
            )

            y_mm = float(
                p_arm[1]
            )

            radius = (
                check_pick_workspace(
                    x_mm,
                    y_mm,
                )
            )

        except Exception:
            return

        self.bag_samples.append(
            (
                x_mm,
                y_mm,
                score,
                u,
                v,
                radius,
            )
        )

    def collect_bag_target(self):
        self.bag_samples.clear()

        deadline = (
            time.monotonic()
            + BAG_TIMEOUT_SEC
        )

        while (
            len(self.bag_samples)
            < BAG_WINDOW
            and time.monotonic()
            < deadline
        ):
            rclpy.spin_once(
                self,
                timeout_sec=0.05,
            )

        if (
            len(self.bag_samples)
            < BAG_MIN_SAMPLES
        ):
            raise RuntimeError(
                "not enough stable paper_bag detections | "
                f"{len(self.bag_samples)}"
            )

        a = np.asarray(
            self.bag_samples,
            dtype=float,
        )

        xy = a[:, :2]

        median_xy = np.median(
            xy,
            axis=0,
        )

        distances = np.linalg.norm(
            xy - median_xy,
            axis=1,
        )

        inlier_mask = (
            distances
            <= MAX_BAG_XY_SPREAD_MM
        )

        inliers = a[
            inlier_mask
        ]

        if (
            len(inliers)
            < BAG_MIN_SAMPLES
        ):
            raise RuntimeError(
                "paper_bag detection unstable | "
                f"inliers={len(inliers)}/"
                f"{len(a)}, "
                f"spread="
                f"{np.round(distances,1).tolist()}"
            )

        target = np.median(
            inliers[:, :2],
            axis=0,
        )

        score = float(
            np.median(
                inliers[:, 2]
            )
        )

        uv = np.median(
            inliers[:, 3:5],
            axis=0,
        )

        spread = float(
            np.max(
                np.linalg.norm(
                    inliers[:, :2]
                    - target,
                    axis=1,
                )
            )
        )

        radius = float(
            math.hypot(
                target[0],
                target[1],
            )
        )

        print()
        print(
            "[BAG FILTER] "
            f"samples={len(a)}, "
            f"inliers={len(inliers)}, "
            f"spread={spread:.1f} mm"
        )

        print(
            "[BAG TARGET] "
            f"X={target[0]:+.1f} "
            f"Y={target[1]:+.1f} mm | "
            f"R={radius:.1f} mm | "
            f"conf~{score:.3f} | "
            f"pixel~({uv[0]:.1f},{uv[1]:.1f})"
        )

        return (
            float(target[0]),
            float(target[1]),
        )


# ============================================================
# PLAN OUTPUT
# ============================================================

def print_pick_plan(
    x_mm,
    y_mm,
    radius,
    plan,
):
    print()
    print("=" * 84)
    print(" PAPER_BAG WORLD-VIEW PICK PLAN")
    print(" MOTOR COMMAND = NONE UNTIL --execute --step")
    print("=" * 84)

    print(
        f"target XY = "
        f"[{x_mm:+.1f}, "
        f"{y_mm:+.1f}] mm"
    )

    print(
        f"radius = "
        f"{radius:.1f} mm | "
        f"allowed "
        f"{PICK_MIN_RADIUS_MM:.0f}"
        f"~{PICK_MAX_RADIUS_MM:.0f} mm"
    )

    for name in (
        "READY",
        "GRASP",
        "LIFT",
        "PULL100",
    ):
        p = plan[name]

        print()
        print(name)

        print(
            " target =",
            np.round(
                p["target"],
                2,
            ).tolist(),
        )

        print(
            " q =",
            np.round(
                p["q"],
                3,
            ).tolist(),
        )

        print(
            " ticks =",
            p["ticks"]
            .astype(int)
            .tolist(),
        )

        print(
            f" TCP error = "
            f"{p['error']:.6f} mm"
        )

        print(
            f" J4 margin = "
            f"{J4_MAX_TICK-int(p['ticks'][3])} ticks"
        )

    print("=" * 84)


def make_handoff_plan(dynamic_pick):
    """
    Full-IK handoff code needs:
      - current PULL100 q as the TURN seed
      - an EXTEND reference only to preserve the already-validated handoff Z

    Keep the old successful smallbag handoff height, but use the new detected
    paper_bag PULL100 posture for TURN/HANDOFF.
    """
    reference = core.build_plan(
        HANDOFF_REFERENCE_SPEC
    )

    return {
        "PULL100":
            dynamic_pick["PULL100"],

        "EXTEND":
            reference["EXTEND"],
    }


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

    parser.add_argument(
        "--auto",
        action="store_true",
        help="Enter 없이 한 사이클 자동 실행",
    )

    parser.add_argument(
        "--with-handoff-preview",
        action="store_true",
        help=(
            "preview mode에서도 driver pose를 받아 "
            "handoff plan까지 계산"
        ),
    )

    args = parser.parse_args()

    if args.execute and not (args.step or args.auto):
        raise SystemExit(
            "실제 실행은 "
            "--execute --step 또는 "
            "--execute --auto 로 실행하세요."
        )

    if args.step and args.auto:
        raise SystemExit(
            "--step 과 --auto 는 동시에 사용할 수 없습니다."
        )

    def gate(text):
        if args.auto:
            print(text)
            print("[AUTO] continue")
            return

        gate(text)

    rclpy.init()

    node = WorldViewPaperBagRunner(
        use_motor=args.execute
    )

    try:
        print()
        print("=" * 84)
        print(" WORLDVIEW PAPER_BAG FULL DELIVERY")
        print("=" * 84)

        print(
            f"vision class      = {VISION_CLASS}"
        )

        print(
            f"pick workspace    = "
            f"{PICK_MIN_RADIUS_MM:.0f}"
            f"~{PICK_MAX_RADIUS_MM:.0f} mm"
        )

        print(
            "paper_bag Z       = "
            f"READY {SPEC.ready_z_mm:.0f} / "
            f"GRASP {SPEC.grasp_z_mm:.0f} / "
            f"LIFT {SPEC.lift_z_mm:.0f} mm"
        )

        print(
            "motor             = "
            + (
                "ENABLED / STEP"
                if args.execute
                else "NONE / PREVIEW"
            )
        )

        print("=" * 84)

        print(
            "\n[VISION] fresh paper_bag target collecting..."
        )

        x_mm, y_mm = (
            node.collect_bag_target()
        )

        radius, pick_plan = (
            build_dynamic_pick_plan(
                x_mm,
                y_mm,
            )
        )

        print_pick_plan(
            x_mm,
            y_mm,
            radius,
            pick_plan,
        )

        handoff_plan = (
            make_handoff_plan(
                pick_plan
            )
        )

        # ----------------------------------------------------
        # PREVIEW
        # ----------------------------------------------------

        if not args.execute:
            if (
                args.with_handoff_preview
            ):
                print(
                    "\n[POSE] fresh driver pose collecting..."
                )

                person = (
                    node.collect_pose()
                )

                handoff = (
                    compute_handoff_target(
                        person,
                        400.0,
                    )
                )

                motion = (
                    build_fullik_path(
                        handoff_plan,
                        handoff,
                    )
                )

                print_handoff_plan(
                    person,
                    handoff,
                    motion,
                )

            print()
            print(
                "[PREVIEW DONE] "
                "motor command 없음"
            )

            return

        # ----------------------------------------------------
        # REAL EXECUTION
        # ----------------------------------------------------

        node.wait_services()

        current = (
            node.get_current()
        )

        error = (
            current
            - core.START_TICKS
        )

        print(
            "\ncurrent =",
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

        gate(
            "\n[1] GRIPPER OPEN"
            "\n사람이 로봇 작업영역 밖에 있는지 확인 후 Enter > "
        )

        node.trigger(
            node.open,
            "paper_bag/OPEN",
        )

        gate(
            "\n[2] MOVE READY"
            "\n검출 위치/주변 간섭 확인 후 Enter > "
        )

        node.move(
            "paper_bag/DYNAMIC_READY",
            [
                pick_plan["READY"]["ticks"],
            ],
        )

        gate(
            "\n[3] DESCEND TO GRASP"
            "\n실제 봉투와 정렬 확인 후 Enter > "
        )

        node.move(
            "paper_bag/DYNAMIC_GRASP",
            [
                pick_plan["GRASP"]["ticks"],
            ],
        )

        gate(
            "\n[4] GRIPPER CLOSE"
            "\nEnter > "
        )

        node.trigger(
            node.close,
            "paper_bag/CLOSE",
        )

        gate(
            "\n[5] LIFT"
            "\nEnter > "
        )

        node.move(
            "paper_bag/DYNAMIC_LIFT",
            [
                pick_plan["LIFT"]["ticks"],
            ],
        )

        gate(
            "\n[6] PULL100"
            "\n주변 간섭 확인 후 Enter > "
        )

        node.move(
            "paper_bag/DYNAMIC_PULL100",
            [
                pick_plan["PULL100"]["ticks"],
            ],
        )

        # Fresh driver pose only after the bag is safely pulled inward.
        print(
            "\n[POSE] fresh driver pose collecting..."
        )

        person = (
            node.collect_pose()
        )

        handoff = (
            compute_handoff_target(
                person,
                400.0,
            )
        )

        motion = (
            build_fullik_path(
                handoff_plan,
                handoff,
            )
        )

        print_handoff_plan(
            person,
            handoff,
            motion,
        )

        gate(
            "\n[7] DYNAMIC TURN"
            "\n첫 통합 테스트에서는 사람이 팔 작업영역 밖에 있는지 "
            "확인 후 Enter > "
        )

        node.move(
            "paper_bag/FULLIK_TURN",
            [
                motion["turn_ticks"],
            ],
        )

        gate(
            "\n[8] FULL IK HANDOFF"
            "\n경로/주변 간섭 확인 후 Enter > "
        )

        node.move(
            "paper_bag/FULLIK_HANDOFF",
            [
                p["ticks"]
                for p
                in motion["points"]
            ],
        )

        gate(
            "\n[9] HANDOFF OPEN"
            "\n수령 준비 확인 후 Enter > "
        )

        node.trigger(
            node.open,
            "paper_bag/OPEN_HANDOVER",
        )

        gate(
            "\n[10] RETURN START"
            "\n작업영역이 비었는지 확인 후 Enter > "
        )

        node.move(
            "paper_bag/RETURN_START",
            [
                core.START_TICKS,
            ],
        )

        print(
            "\n[DONE] WORLDVIEW PAPER_BAG -> FULLIK HANDOFF"
        )

    finally:
        node.destroy_node()

        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
