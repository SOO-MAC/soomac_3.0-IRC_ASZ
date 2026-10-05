#!/usr/bin/env python3

from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.optimize import least_squares

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))

import simple_pick_place as spp
from bag_handle_hook_study import pose_state
from control_config import robot_config as cfg


# ============================================================
# Recovered common bag-family values
# ============================================================

AXIAL_OFFSET_MM = 136.0

J5_DEG = 90.0
J5_TICK = 3072

GRIPPER_OPEN_TICK = 85
GRIPPER_HOLD_TICK = 2048

PULL_MM = 100.0

TURN_J1_DEG = -180.0

EXTEND_RADIUS_MM = 400.0
EXTEND_SEGMENTS = 12      # start + 12 = 13 poses

# recovered geometry used by build_tooldown_extend()
LINK2_MM = 250.0
LINK3_MM = 250.0
TOOL_AXIAL_MM = 265.0

# Physical J4 upper used in these bag tests.
J4_SOFT_MAX_TICK = 3430

START_TICKS = np.asarray(
    cfg.ASSUMED_START_TICKS,
    dtype=np.int64,
)

START_TOL_TICKS = np.array(
    [80, 120, 180, 120, 60],
    dtype=np.int64,
)

PATH_SERVICE = "/motor/move_tick_path"
OPEN_SERVICE = "/motor/cup_gripper_open"
CLOSE_SERVICE = "/motor/bag_gripper_hold"

PATH_TIMEOUT_SEC = 60.0


@dataclass(frozen=True)
class BagSpec:
    name: str

    width_mm: float
    depth_mm: float
    height_mm: float

    x_mm: float
    y_mm: float

    ready_z_mm: float
    ready_tilt_deg: float

    grasp_z_mm: float
    grasp_tilt_deg: float

    lift_z_mm: float
    lift_tilt_deg: float


def unit(v):
    v = np.asarray(v, dtype=float)
    n = float(np.linalg.norm(v))

    if n < 1.0e-12:
        raise ValueError("zero vector")

    return v / n


# ============================================================
# Recovered old bag IK
# ============================================================

def solve_tcp(
    chain,
    target_tcp,
    tilt_deg,
    seed_q=None,
):
    target_tcp = np.asarray(
        target_tcp,
        dtype=float,
    ).reshape(3)

    lower = np.asarray(
        cfg.JOINT_LIMIT_LOWER_DEG,
        dtype=float,
    )

    upper = np.asarray(
        cfg.JOINT_LIMIT_UPPER_DEG,
        dtype=float,
    )

    x, y, _ = target_tcp

    j1_rad = float(
        np.arctan2(y, x)
    )

    j1_deg = float(
        np.rad2deg(j1_rad)
    )

    if not (
        lower[0] <= j1_deg <= upper[0]
    ):
        raise RuntimeError(
            f"J1 limit exceeded: {j1_deg:.3f}"
        )

    e_r = np.array([
        np.cos(j1_rad),
        np.sin(j1_rad),
        0.0,
    ])

    tilt_rad = float(
        np.deg2rad(tilt_deg)
    )

    desired_tool = unit(
        np.array([
            np.cos(tilt_rad) * e_r[0],
            np.cos(tilt_rad) * e_r[1],
            -np.sin(tilt_rad),
        ])
    )

    # finger TCP -> model terminal
    model_target = (
        target_tcp
        - AXIAL_OFFSET_MM * desired_tool
    )

    # old orientation rule
    target_sum_deg = (
        90.0 + float(tilt_deg)
    )

    lo = lower[1:4]
    hi = upper[1:4]

    if seed_q is None:
        seed = np.array(
            [-20.0, 80.0, 80.0],
            dtype=float,
        )
    else:
        seed = np.asarray(
            seed_q[1:4],
            dtype=float,
        )

    seed = np.clip(
        seed,
        lo + 1.0e-6,
        hi - 1.0e-6,
    )

    def residual(v):
        j2, j3, j4 = v

        q = np.array([
            j1_deg,
            j2,
            j3,
            j4,
            J5_DEG,
        ])

        state = pose_state(
            chain,
            np.deg2rad(q),
        )

        p = np.asarray(
            state["xyz"],
            dtype=float,
        )

        return np.array([
            p[0] - model_target[0],
            p[1] - model_target[1],
            p[2] - model_target[2],

            (
                j2 + j3 + j4
                - target_sum_deg
            ) * 10.0,
        ])

    result = least_squares(
        residual,
        seed,
        bounds=(lo, hi),
        max_nfev=4000,
        ftol=1.0e-12,
        xtol=1.0e-12,
        gtol=1.0e-12,
    )

    j2, j3, j4 = result.x

    q = np.array([
        j1_deg,
        j2,
        j3,
        j4,
        J5_DEG,
    ])

    state = pose_state(
        chain,
        np.deg2rad(q),
    )

    model_xyz = np.asarray(
        state["xyz"],
        dtype=float,
    )

    actual_tool = unit(
        state["tool_axis"]
    )

    actual_tcp = (
        model_xyz
        + AXIAL_OFFSET_MM * actual_tool
    )

    tcp_error = float(
        np.linalg.norm(
            actual_tcp - target_tcp
        )
    )

    margins = np.minimum(
        q - lower,
        upper - q,
    )

    ticks = np.asarray(
        cfg.model_deg_to_ticks(q),
        dtype=np.int64,
    )

    if tcp_error > 1.0:
        raise RuntimeError(
            f"TCP error {tcp_error:.3f} mm"
        )

    if int(ticks[4]) != J5_TICK:
        raise RuntimeError(
            f"J5 mismatch {ticks[4]}"
        )

    return {
        "q": q,
        "ticks": ticks,
        "target": target_tcp,
        "tcp": actual_tcp,
        "tcp_error": tcp_error,
        "margin": margins,
        "min_margin": float(
            np.min(margins)
        ),
    }


# ============================================================
# PULL 100 mm toward base
# ============================================================

def build_pull_target(spec):
    r0 = float(
        np.hypot(
            spec.x_mm,
            spec.y_mm,
        )
    )

    az = float(
        np.arctan2(
            spec.y_mm,
            spec.x_mm,
        )
    )

    r = r0 - PULL_MM

    if r <= 0:
        raise RuntimeError(
            "invalid PULL radius"
        )

    return np.array([
        r * np.cos(az),
        r * np.sin(az),
        spec.lift_z_mm,
    ])


# ============================================================
# Recovered J2/J3-only tool-down EXTEND
# ============================================================

def build_tooldown_extend(start_q):
    """
    Recovered rule:

      J1 fixed
      J4 fixed
      J5 fixed
      J2/J3 only

      J2 + J3 preserved
      J2 + J3 + J4 = 180 deg preserved

      radius -> 400 mm

    12 generated points + original TURN pose = 13 poses.
    """

    start_q = np.asarray(
        start_q,
        dtype=float,
    ).reshape(5)

    if abs(start_q[0] - TURN_J1_DEG) > 1.0e-6:
        raise RuntimeError(
            "EXTEND must start at J1=-180"
        )

    sum23 = float(
        start_q[1] + start_q[2]
    )

    total_tooldown = float(
        sum23 + start_q[3]
    )

    if abs(total_tooldown - 180.0) > 0.2:
        raise RuntimeError(
            "tool-down sum mismatch | "
            f"J2+J3+J4={total_tooldown:.3f}"
        )

    # tool-down => tool axial direction has no radial component.
    # r = L2*sin(J2) + L3*sin(J2+J3)
    rhs = (
        EXTEND_RADIUS_MM
        - LINK3_MM
        * math.sin(
            math.radians(sum23)
        )
    ) / LINK2_MM

    if not -1.0 <= rhs <= 1.0:
        raise RuntimeError(
            f"EXTEND radius unreachable: rhs={rhs}"
        )

    final_j2 = math.degrees(
        math.asin(rhs)
    )

    final_j3 = (
        sum23 - final_j2
    )

    final_q = np.array([
        TURN_J1_DEG,
        final_j2,
        final_j3,
        start_q[3],
        J5_DEG,
    ])

    points = []

    for i in range(
        1,
        EXTEND_SEGMENTS + 1,
    ):
        u = (
            i / EXTEND_SEGMENTS
        )

        q = (
            (1.0 - u) * start_q
            + u * final_q
        )

        # exact fixed joints
        q[0] = TURN_J1_DEG
        q[3] = start_q[3]
        q[4] = J5_DEG

        # preserve J2 + J3 exactly
        q[2] = (
            sum23 - q[1]
        )

        ticks = np.asarray(
            cfg.model_deg_to_ticks(q),
            dtype=np.int64,
        )

        if int(ticks[3]) > J4_SOFT_MAX_TICK:
            raise RuntimeError(
                "physical J4 soft max exceeded | "
                f"{ticks[3]} > "
                f"{J4_SOFT_MAX_TICK}"
            )

        points.append({
            "name":
                f"TOOLDOWN_EXTEND_{i}",

            "q": q,
            "ticks": ticks,
        })

    return points


# ============================================================
# Complete calculation
# ============================================================

def build_plan(spec):
    chain = spp.create_robot_chain()

    result = {}

    ready = solve_tcp(
        chain,
        np.array([
            spec.x_mm,
            spec.y_mm,
            spec.ready_z_mm,
        ]),
        spec.ready_tilt_deg,
    )

    result["READY"] = ready

    grasp = solve_tcp(
        chain,
        np.array([
            spec.x_mm,
            spec.y_mm,
            spec.grasp_z_mm,
        ]),
        spec.grasp_tilt_deg,
        ready["q"],
    )

    result["GRASP"] = grasp

    lift = solve_tcp(
        chain,
        np.array([
            spec.x_mm,
            spec.y_mm,
            spec.lift_z_mm,
        ]),
        spec.lift_tilt_deg,
        grasp["q"],
    )

    result["LIFT"] = lift

    pull = solve_tcp(
        chain,
        build_pull_target(spec),
        90.0,
        lift["q"],
    )

    result["PULL100"] = pull

    # Absolute right-side turn.
    turn_q = pull["q"].copy()

    turn_q[0] = TURN_J1_DEG
    turn_q[4] = J5_DEG

    turn_ticks = np.asarray(
        cfg.model_deg_to_ticks(
            turn_q
        ),
        dtype=np.int64,
    )

    if int(turn_ticks[3]) > J4_SOFT_MAX_TICK:
        raise RuntimeError(
            "TURN J4 soft max exceeded"
        )

    result["TURN_-180"] = {
        "q": turn_q,
        "ticks": turn_ticks,
    }

    result["EXTEND"] = (
        build_tooldown_extend(
            turn_q
        )
    )

    return result


def print_plan(spec, plan):
    print()
    print("=" * 108)
    print(
        f" {spec.name.upper()} "
        "FULL SEQUENCE RECOVERY"
    )
    print("=" * 108)

    print(
        "Bag size = "
        f"{spec.width_mm:.0f} x "
        f"{spec.depth_mm:.0f} x "
        f"{spec.height_mm:.0f} mm"
    )

    print(
        f"XY       = "
        f"[{spec.x_mm:.1f}, "
        f"{spec.y_mm:.1f}] mm"
    )

    print(
        "Sequence = "
        "READY -> GRASP -> CLOSE -> "
        "LIFT -> PULL100 -> "
        "absolute J1=-180 -> "
        "J2/J3-only EXTEND r400 -> "
        "WAIT5 -> OPEN -> WAIT5 -> START"
    )

    print()

    for name in (
        "READY",
        "GRASP",
        "LIFT",
        "PULL100",
    ):
        r = plan[name]

        print(
            f"{name:<12}"
            f"q={np.round(r['q'], 3).tolist()}"
        )

        print(
            f"{'':12}"
            f"ticks="
            f"{r['ticks'].astype(int).tolist()} "
            f"err={r['tcp_error']:.6f} mm"
        )

    turn = plan["TURN_-180"]

    print(
        f"{'TURN_-180':<12}"
        f"q="
        f"{np.round(turn['q'],3).tolist()}"
    )

    print(
        f"{'':12}"
        f"ticks="
        f"{turn['ticks'].astype(int).tolist()}"
    )

    print()
    print("[TOOLDOWN EXTEND]")

    for p in plan["EXTEND"]:
        print(
            f"{p['name']:<22}"
            f"q="
            f"{np.round(p['q'],3).tolist()} "
            f"ticks="
            f"{p['ticks'].astype(int).tolist()}"
        )

    final = plan["EXTEND"][-1]

    qf = final["q"]

    print()
    print(
        f"J2+J3       = "
        f"{qf[1] + qf[2]:.3f} deg"
    )

    print(
        f"J2+J3+J4    = "
        f"{qf[1] + qf[2] + qf[3]:.3f} deg"
    )

    print(
        f"Final radius = "
        f"{EXTEND_RADIUS_MM:.1f} mm"
    )

    print(
        f"J4 tick      = "
        f"{final['ticks'][3]} "
        f"(soft max {J4_SOFT_MAX_TICK})"
    )

    print()
    print("Default = DRY RUN")
    print("=" * 108)


# ============================================================
# Existing ROS service execution
# ============================================================

def execute(spec, plan, step=False):
    import rclpy

    from rclpy.node import Node
    from std_msgs.msg import Int32MultiArray
    from std_srvs.srv import Trigger
    from soomac_interfaces.srv import MoveTickPath


    class Executor(Node):
        def __init__(self):
            super().__init__(
                f"{spec.name}_runner"
            )

            self.current = None

            self.create_subscription(
                Int32MultiArray,
                "/motor/joint_state",
                self._cb,
                10,
            )

            self.path = self.create_client(
                MoveTickPath,
                PATH_SERVICE,
            )

            self.open = self.create_client(
                Trigger,
                OPEN_SERVICE,
            )

            self.close = self.create_client(
                Trigger,
                CLOSE_SERVICE,
            )

        def _cb(self, msg):
            if len(msg.data) == 5:
                self.current = np.asarray(
                    msg.data,
                    dtype=np.int64,
                )

        def wait_services(self):
            for c, name in (
                (self.path, PATH_SERVICE),
                (self.open, OPEN_SERVICE),
                (self.close, CLOSE_SERVICE),
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

        def trigger(self, client, label):
            f = client.call_async(
                Trigger.Request()
            )

            rclpy.spin_until_future_complete(
                self,
                f,
            )

            res = f.result()

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

        def move(self, name, ticks_list):
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

            # 0 => existing motor-node default profile velocity
            req.profile_velocity = 0
            req.timeout_sec = PATH_TIMEOUT_SEC

            f = self.path.call_async(req)

            rclpy.spin_until_future_complete(
                self,
                f,
            )

            res = f.result()

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


    def pause(label):
        if step:
            input(
                f"\n{label}\n"
                "확인 후 Enter > "
            )


    rclpy.init()
    node = Executor()

    try:
        node.wait_services()

        current = node.get_current()

        error = (
            current - START_TICKS
        )

        print(
            "current =",
            current.tolist(),
        )

        if np.any(
            np.abs(error)
            > START_TOL_TICKS
        ):
            raise RuntimeError(
                "START pose check failed | "
                f"error={error.tolist()}"
            )

        pause("GRIPPER OPEN")

        node.trigger(
            node.open,
            f"{spec.name}/OPEN",
        )

        pause("READY -> GRASP")

        node.move(
            f"{spec.name}/PICK",
            [
                plan["READY"]["ticks"],
                plan["GRASP"]["ticks"],
            ],
        )

        pause("GRIPPER CLOSE/HOLD")

        node.trigger(
            node.close,
            f"{spec.name}/CLOSE",
        )

        pause("LIFT -> PULL100")

        node.move(
            f"{spec.name}/LIFT_PULL",
            [
                plan["LIFT"]["ticks"],
                plan["PULL100"]["ticks"],
            ],
        )

        pause("absolute J1 = -180")

        node.move(
            f"{spec.name}/TURN",
            [
                plan["TURN_-180"]["ticks"],
            ],
        )

        pause(
            "J2/J3-only TOOLDOWN EXTEND r400"
        )

        node.move(
            f"{spec.name}/EXTEND",
            [
                p["ticks"]
                for p
                in plan["EXTEND"]
            ],
        )

        print(
            "\n[HANDOVER_WAIT] 5 sec"
        )

        time.sleep(5.0)

        node.trigger(
            node.open,
            f"{spec.name}/OPEN_HANDOVER",
        )

        print(
            "[AFTER_OPEN_WAIT] 5 sec"
        )

        time.sleep(5.0)

        pause("RETURN START")

        node.move(
            f"{spec.name}/RETURN_START",
            [START_TICKS],
        )

        print()
        print(
            f"[DONE] {spec.name}"
        )

    finally:
        node.destroy_node()

        if rclpy.ok():
            rclpy.shutdown()


def run(spec):
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

    plan = build_plan(spec)

    print_plan(
        spec,
        plan,
    )

    if args.execute:
        execute(
            spec,
            plan,
            step=args.step,
        )
