#!/usr/bin/env python3
"""Drive-through arm: camera transforms, order coordination, IK and motor requests.

Only ArmControlNode owns a ROS node. Internal planner classes isolate each task's
constants and helper functions; all motion executes through motor_control services.
"""

from __future__ import annotations
import json
import math
import threading
import time
from collections import Counter, deque
from pathlib import Path
import numpy as np
import rclpy
from ament_index_python.packages import get_package_share_directory
from rclpy.node import Node
from rclpy.executors import MultiThreadedExecutor
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
from geometry_msgs.msg import PointStamped
from sensor_msgs.msg import CameraInfo
from vision_msgs.msg import Detection2DArray
from std_msgs.msg import String
from std_srvs.srv import SetBool, Trigger
from soomac_interfaces.msg import DetectedItem
from soomac_interfaces.srv import ArmCommand, MoveTickPath
from arm_control import robot_config
from arm_control.robot_config import load_config

_MOTION_PARAMETERS = load_config("motion_parameters.yaml")


def _motion_setting(section, key):
    return _MOTION_PARAMETERS[section][key]


from scipy.optimize import least_squares
import argparse
import sys
from dataclasses import dataclass
from rclpy.callback_groups import ReentrantCallbackGroup
from collections import deque
from std_msgs.msg import Int32MultiArray
from soomac_interfaces.srv import MoveTickPath

COMMAND_SERVICE = "/arm_control/execute"
CMD_PING = "PING"
CMD_MOVE_PAYMENT = "MOVE_PAYMENT"
CMD_PICK = "PICK"
CMD_HANDOFF = "HANDOFF"
CMD_RETURN_HOME = "RETURN_HOME"
OBJ_PAPER_BAG = "paper_bag"
OBJ_L_PAPER_BAG = "L_paper_bag"
OBJ_CUP = "cup"
VALID_OBJECTS = ("paper_bag", "L_paper_bag", "cup")


# Shared kinematics
class ArmKinematics:
    create_robot_chain = robot_config.create_robot_chain
    get_start_q_model_rad = robot_config.get_start_q_model_rad
    model_q_to_ikpy_vector = robot_config.model_q_to_ikpy_vector
    model_deg_to_ticks = robot_config.model_deg_to_ticks
    JOINT_LIMIT_LOWER_DEG = robot_config.JOINT_LIMIT_LOWER_DEG
    JOINT_LIMIT_UPPER_DEG = robot_config.JOINT_LIMIT_UPPER_DEG
    calculate_tool_yaw_state = robot_config.calculate_tool_yaw_state
    SIDE_MOVE_MM = _motion_setting("arm_kinematics", "SIDE_MOVE_MM")
    APPROACH_MM = _motion_setting("arm_kinematics", "APPROACH_MM")
    PICK_XYZ_MM = np.array(_motion_setting("arm_kinematics", "PICK_XYZ_MM"), dtype=None)
    TARGET_YAW_DEG = _motion_setting("arm_kinematics", "TARGET_YAW_DEG")
    POSITION_TOLERANCE_MM = _motion_setting("arm_kinematics", "POSITION_TOLERANCE_MM")
    TOOL_TOLERANCE_DEG = _motion_setting("arm_kinematics", "TOOL_TOLERANCE_DEG")
    YAW_TOLERANCE_DEG = _motion_setting("arm_kinematics", "YAW_TOLERANCE_DEG")

    @staticmethod
    def make_waypoints(pick_xyz):
        pick_xyz = np.asarray(pick_xyz, dtype=float)
        pick_above = pick_xyz + np.array([0.0, 0.0, ArmKinematics.APPROACH_MM])
        place_xyz = pick_xyz + np.array([0.0, ArmKinematics.SIDE_MOVE_MM, 0.0])
        place_above = place_xyz + np.array([0.0, 0.0, ArmKinematics.APPROACH_MM])
        return [
            ("PICK_ABOVE", pick_above),
            ("PICK", pick_xyz),
            ("PICK_ABOVE_RETURN", pick_above),
            ("PLACE_ABOVE", place_above),
            ("PLACE", place_xyz),
            ("PLACE_ABOVE_RETURN", place_above),
        ]

    @staticmethod
    def read_tool_values(tool_state):
        tool_error = None
        yaw = None
        for key in ("tool_error", "tool_error_deg", "tool_down_error_deg"):
            if key in tool_state:
                tool_error = float(tool_state[key])
                break
        for key in ("yaw", "yaw_deg"):
            if key in tool_state:
                yaw = float(tool_state[key])
                break
        if tool_error is None or yaw is None:
            raise RuntimeError(
                f"\nTool state key 확인 필요\nkeys = {list(tool_state.keys())}\nvalue = {tool_state}"
            )
        return (tool_error, yaw)

    @staticmethod
    def fk_state(chain, q_model_rad):
        ikpy_q = ArmKinematics.model_q_to_ikpy_vector(q_model_rad)
        transform = np.asarray(chain.forward_kinematics(ikpy_q), dtype=float)
        xyz_mm = transform[:3, 3] * 1000.0
        rotation = transform[:3, :3]
        raw_tool_state = ArmKinematics.calculate_tool_yaw_state(rotation)
        tool_error, yaw = ArmKinematics.read_tool_values(raw_tool_state)
        return {"xyz": xyz_mm, "tool_error": tool_error, "yaw": yaw}

    @staticmethod
    def angle_difference_deg(actual_deg, target_deg):
        return (actual_deg - target_deg + 180.0) % 360.0 - 180.0

    @staticmethod
    def solve_ik(chain, target_xyz, target_yaw_deg, seed_q):
        target_xyz = np.asarray(target_xyz, dtype=float)
        seed_q = np.asarray(seed_q, dtype=float)
        lower_rad = np.deg2rad(
            np.asarray(ArmKinematics.JOINT_LIMIT_LOWER_DEG, dtype=float)
        )
        upper_rad = np.deg2rad(
            np.asarray(ArmKinematics.JOINT_LIMIT_UPPER_DEG, dtype=float)
        )
        seed_q = np.clip(seed_q, lower_rad + 1e-06, upper_rad - 1e-06)

        def residual(q_rad):
            state = ArmKinematics.fk_state(chain, q_rad)
            position_residual = (state["xyz"] - target_xyz) / 10.0
            tool_residual = np.array([state["tool_error"] / 5.0])
            yaw_error = ArmKinematics.angle_difference_deg(state["yaw"], target_yaw_deg)
            yaw_residual = np.array([yaw_error / 5.0])
            return np.concatenate([position_residual, tool_residual, yaw_residual])

        result = least_squares(
            residual,
            seed_q,
            bounds=(lower_rad, upper_rad),
            method="trf",
            max_nfev=1000,
            ftol=1e-10,
            xtol=1e-10,
            gtol=1e-10,
        )
        q_rad = result.x
        q_deg = np.rad2deg(q_rad)
        state = ArmKinematics.fk_state(chain, q_rad)
        position_error = float(np.linalg.norm(state["xyz"] - target_xyz))
        tool_error = float(state["tool_error"])
        yaw_error = abs(
            ArmKinematics.angle_difference_deg(state["yaw"], target_yaw_deg)
        )
        inside_limit = bool(
            np.all(q_deg >= np.asarray(ArmKinematics.JOINT_LIMIT_LOWER_DEG))
            and np.all(q_deg <= np.asarray(ArmKinematics.JOINT_LIMIT_UPPER_DEG))
        )
        success = bool(
            position_error <= ArmKinematics.POSITION_TOLERANCE_MM
            and tool_error <= ArmKinematics.TOOL_TOLERANCE_DEG
            and (yaw_error <= ArmKinematics.YAW_TOLERANCE_DEG)
            and inside_limit
        )
        if success:
            goal_ticks = ArmKinematics.model_deg_to_ticks(q_deg)
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


class BagKinematics:
    spp = ArmKinematics

    @staticmethod
    def full_fk(chain, q_rad):
        """
        q_rad = [J1,J2,J3,J4,J5]
        IKPy 전체 chain transform 반환
        """
        q_rad = np.asarray(q_rad, dtype=float)
        full_q = np.zeros(len(chain.links), dtype=float)
        active = np.asarray(chain.active_links_mask, dtype=bool)
        full_q[active] = q_rad
        return chain.forward_kinematics(full_q)

    @staticmethod
    def pose_state(chain, q_rad):
        T = BagKinematics.full_fk(chain, q_rad)
        xyz_mm = T[:3, 3] * 1000.0
        R = T[:3, :3]
        tool_axis = R[:, 2]
        finger_x = R[:, 0]
        finger_y = R[:, 1]
        return {
            "xyz": xyz_mm,
            "R": R,
            "tool_axis": tool_axis,
            "finger_x": finger_x,
            "finger_y": finger_y,
        }

    @staticmethod
    def axis_angle_error_deg(a, b):
        a = np.asarray(a, dtype=float)
        b = np.asarray(b, dtype=float)
        a /= np.linalg.norm(a)
        b /= np.linalg.norm(b)
        dot = np.clip(np.dot(a, b), -1.0, 1.0)
        return float(np.rad2deg(np.arccos(dot)))

    @staticmethod
    def solve_sideways_pose(chain, target_xyz, target_axis, seed_deg):
        lower_deg = np.asarray(BagKinematics.spp.JOINT_LIMIT_LOWER_DEG, dtype=float)
        upper_deg = np.asarray(BagKinematics.spp.JOINT_LIMIT_UPPER_DEG, dtype=float)
        lower = np.deg2rad(lower_deg)
        upper = np.deg2rad(upper_deg)
        seed = np.deg2rad(np.asarray(seed_deg, dtype=float))
        seed = np.clip(seed, lower + 1e-06, upper - 1e-06)
        target_xyz = np.asarray(target_xyz, dtype=float)
        target_axis = np.asarray(target_axis, dtype=float)
        target_axis /= np.linalg.norm(target_axis)

        def residual(q):
            state = BagKinematics.pose_state(chain, q)
            pos_res = (state["xyz"] - target_xyz) / 10.0
            axis_res = (state["tool_axis"] - target_axis) * 10.0
            j5_res = np.array([np.rad2deg(q[4]) / 10.0])
            return np.concatenate([pos_res, axis_res, j5_res])

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
        state = BagKinematics.pose_state(chain, q)
        position_error = float(np.linalg.norm(state["xyz"] - target_xyz))
        axis_error = BagKinematics.axis_angle_error_deg(state["tool_axis"], target_axis)
        return {
            "q_rad": q,
            "q_deg": np.rad2deg(q),
            "state": state,
            "position_error": position_error,
            "axis_error": axis_error,
            "success": position_error <= 3.0 and axis_error <= 2.0,
        }


# Bag motion planning
class BagMotion:
    spp = ArmKinematics
    pose_state = BagKinematics.pose_state
    cfg = robot_config
    AXIAL_OFFSET_MM = _motion_setting("bag_geometry", "AXIAL_OFFSET_MM")
    J5_DEG = _motion_setting("bag_geometry", "J5_DEG")
    J5_TICK = _motion_setting("bag_geometry", "J5_TICK")
    GRIPPER_OPEN_TICK = _motion_setting("bag_geometry", "GRIPPER_OPEN_TICK")
    GRIPPER_HOLD_TICK = _motion_setting("bag_geometry", "GRIPPER_HOLD_TICK")
    PULL_MM = _motion_setting("bag_geometry", "PULL_MM")
    TURN_J1_DEG = _motion_setting("bag_geometry", "TURN_J1_DEG")
    EXTEND_RADIUS_MM = _motion_setting("bag_geometry", "EXTEND_RADIUS_MM")
    EXTEND_SEGMENTS = _motion_setting("bag_geometry", "EXTEND_SEGMENTS")
    LINK2_MM = _motion_setting("bag_geometry", "LINK2_MM")
    LINK3_MM = _motion_setting("bag_geometry", "LINK3_MM")
    TOOL_AXIAL_MM = _motion_setting("bag_geometry", "TOOL_AXIAL_MM")
    J4_SOFT_MAX_TICK = _motion_setting("bag_geometry", "J4_SOFT_MAX_TICK")
    START_TICKS = np.asarray(cfg.ASSUMED_START_TICKS, dtype=np.int64)
    START_TOL_TICKS = np.array(
        _motion_setting("bag_geometry", "START_TOL_TICKS"), dtype=np.int64
    )
    PATH_SERVICE = _motion_setting("bag_geometry", "PATH_SERVICE")
    OPEN_SERVICE = _motion_setting("bag_geometry", "OPEN_SERVICE")
    CLOSE_SERVICE = _motion_setting("bag_geometry", "CLOSE_SERVICE")
    PATH_TIMEOUT_SEC = _motion_setting("bag_geometry", "PATH_TIMEOUT_SEC")

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

    @staticmethod
    def unit(v):
        v = np.asarray(v, dtype=float)
        n = float(np.linalg.norm(v))
        if n < 1e-12:
            raise ValueError("zero vector")
        return v / n

    @staticmethod
    def solve_tcp(chain, target_tcp, tilt_deg, seed_q=None):
        target_tcp = np.asarray(target_tcp, dtype=float).reshape(3)
        lower = np.asarray(BagMotion.cfg.JOINT_LIMIT_LOWER_DEG, dtype=float)
        upper = np.asarray(BagMotion.cfg.JOINT_LIMIT_UPPER_DEG, dtype=float)
        x, y, _ = target_tcp
        j1_rad = float(np.arctan2(y, x))
        j1_deg = float(np.rad2deg(j1_rad))
        if not lower[0] <= j1_deg <= upper[0]:
            raise RuntimeError(f"J1 limit exceeded: {j1_deg:.3f}")
        e_r = np.array([np.cos(j1_rad), np.sin(j1_rad), 0.0])
        tilt_rad = float(np.deg2rad(tilt_deg))
        desired_tool = BagMotion.unit(
            np.array(
                [
                    np.cos(tilt_rad) * e_r[0],
                    np.cos(tilt_rad) * e_r[1],
                    -np.sin(tilt_rad),
                ]
            )
        )
        model_target = target_tcp - BagMotion.AXIAL_OFFSET_MM * desired_tool
        target_sum_deg = 90.0 + float(tilt_deg)
        lo = lower[1:4]
        hi = upper[1:4]
        if seed_q is None:
            seed = np.array([-20.0, 80.0, 80.0], dtype=float)
        else:
            seed = np.asarray(seed_q[1:4], dtype=float)
        seed = np.clip(seed, lo + 1e-06, hi - 1e-06)

        def residual(v):
            j2, j3, j4 = v
            q = np.array([j1_deg, j2, j3, j4, BagMotion.J5_DEG])
            state = BagMotion.pose_state(chain, np.deg2rad(q))
            p = np.asarray(state["xyz"], dtype=float)
            return np.array(
                [
                    p[0] - model_target[0],
                    p[1] - model_target[1],
                    p[2] - model_target[2],
                    (j2 + j3 + j4 - target_sum_deg) * 10.0,
                ]
            )

        result = least_squares(
            residual,
            seed,
            bounds=(lo, hi),
            max_nfev=4000,
            ftol=1e-12,
            xtol=1e-12,
            gtol=1e-12,
        )
        j2, j3, j4 = result.x
        q = np.array([j1_deg, j2, j3, j4, BagMotion.J5_DEG])
        state = BagMotion.pose_state(chain, np.deg2rad(q))
        model_xyz = np.asarray(state["xyz"], dtype=float)
        actual_tool = BagMotion.unit(state["tool_axis"])
        actual_tcp = model_xyz + BagMotion.AXIAL_OFFSET_MM * actual_tool
        tcp_error = float(np.linalg.norm(actual_tcp - target_tcp))
        margins = np.minimum(q - lower, upper - q)
        ticks = np.asarray(BagMotion.cfg.model_deg_to_ticks(q), dtype=np.int64)
        if tcp_error > 1.0:
            raise RuntimeError(f"TCP error {tcp_error:.3f} mm")
        if int(ticks[4]) != BagMotion.J5_TICK:
            raise RuntimeError(f"J5 mismatch {ticks[4]}")
        return {
            "q": q,
            "ticks": ticks,
            "target": target_tcp,
            "tcp": actual_tcp,
            "tcp_error": tcp_error,
            "margin": margins,
            "min_margin": float(np.min(margins)),
        }

    @staticmethod
    def build_pull_target(spec):
        r0 = float(np.hypot(spec.x_mm, spec.y_mm))
        az = float(np.arctan2(spec.y_mm, spec.x_mm))
        r = r0 - BagMotion.PULL_MM
        if r <= 0:
            raise RuntimeError("invalid PULL radius")
        return np.array([r * np.cos(az), r * np.sin(az), spec.lift_z_mm])

    @staticmethod
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
        start_q = np.asarray(start_q, dtype=float).reshape(5)
        if abs(start_q[0] - BagMotion.TURN_J1_DEG) > 1e-06:
            raise RuntimeError("EXTEND must start at J1=-180")
        sum23 = float(start_q[1] + start_q[2])
        total_tooldown = float(sum23 + start_q[3])
        if abs(total_tooldown - 180.0) > 0.2:
            raise RuntimeError(
                f"tool-down sum mismatch | J2+J3+J4={total_tooldown:.3f}"
            )
        rhs = (
            BagMotion.EXTEND_RADIUS_MM
            - BagMotion.LINK3_MM * math.sin(math.radians(sum23))
        ) / BagMotion.LINK2_MM
        if not -1.0 <= rhs <= 1.0:
            raise RuntimeError(f"EXTEND radius unreachable: rhs={rhs}")
        final_j2 = math.degrees(math.asin(rhs))
        final_j3 = sum23 - final_j2
        final_q = np.array(
            [BagMotion.TURN_J1_DEG, final_j2, final_j3, start_q[3], BagMotion.J5_DEG]
        )
        points = []
        for i in range(1, BagMotion.EXTEND_SEGMENTS + 1):
            u = i / BagMotion.EXTEND_SEGMENTS
            q = (1.0 - u) * start_q + u * final_q
            q[0] = BagMotion.TURN_J1_DEG
            q[3] = start_q[3]
            q[4] = BagMotion.J5_DEG
            q[2] = sum23 - q[1]
            ticks = np.asarray(BagMotion.cfg.model_deg_to_ticks(q), dtype=np.int64)
            if int(ticks[3]) > BagMotion.J4_SOFT_MAX_TICK:
                raise RuntimeError(
                    f"physical J4 soft max exceeded | {ticks[3]} > {BagMotion.J4_SOFT_MAX_TICK}"
                )
            points.append({"name": f"TOOLDOWN_EXTEND_{i}", "q": q, "ticks": ticks})
        return points

    @staticmethod
    def build_plan(spec):
        chain = BagMotion.spp.create_robot_chain()
        result = {}
        ready = BagMotion.solve_tcp(
            chain,
            np.array([spec.x_mm, spec.y_mm, spec.ready_z_mm]),
            spec.ready_tilt_deg,
        )
        result["READY"] = ready
        grasp = BagMotion.solve_tcp(
            chain,
            np.array([spec.x_mm, spec.y_mm, spec.grasp_z_mm]),
            spec.grasp_tilt_deg,
            ready["q"],
        )
        result["GRASP"] = grasp
        lift = BagMotion.solve_tcp(
            chain,
            np.array([spec.x_mm, spec.y_mm, spec.lift_z_mm]),
            spec.lift_tilt_deg,
            grasp["q"],
        )
        result["LIFT"] = lift
        pull = BagMotion.solve_tcp(
            chain, BagMotion.build_pull_target(spec), 90.0, lift["q"]
        )
        result["PULL100"] = pull
        turn_q = pull["q"].copy()
        turn_q[0] = BagMotion.TURN_J1_DEG
        turn_q[4] = BagMotion.J5_DEG
        turn_ticks = np.asarray(
            BagMotion.cfg.model_deg_to_ticks(turn_q), dtype=np.int64
        )
        if int(turn_ticks[3]) > BagMotion.J4_SOFT_MAX_TICK:
            raise RuntimeError("TURN J4 soft max exceeded")
        result["TURN_-180"] = {"q": turn_q, "ticks": turn_ticks}
        result["EXTEND"] = BagMotion.build_tooldown_extend(turn_q)
        return result


class HandoffReference:
    BagSpec = BagMotion.BagSpec
    SPEC = BagSpec(**_motion_setting("handoff_reference", "SPEC"))


class PaperBagReference:
    core = BagMotion
    SPEC = core.BagSpec(**_motion_setting("paper_bag_reference", "SPEC"))
    PICK_MIN_RADIUS_MM = _motion_setting("paper_bag_reference", "PICK_MIN_RADIUS_MM")
    PICK_MAX_RADIUS_MM = _motion_setting("paper_bag_reference", "PICK_MAX_RADIUS_MM")


# Embedded motor-service runner and handoff helpers
class HandoffMotion:
    core = BagMotion
    SPEC = core.BagSpec(**_motion_setting("handoff", "SPEC"))
    POSE_TOPIC = _motion_setting("handoff", "POSE_TOPIC")
    POSE_WINDOW = _motion_setting("handoff", "POSE_WINDOW")
    POSE_MIN_SAMPLES = _motion_setting("handoff", "POSE_MIN_SAMPLES")
    POSE_TIMEOUT_SEC = _motion_setting("handoff", "POSE_TIMEOUT_SEC")
    STANDOFF_MM = _motion_setting("handoff", "STANDOFF_MM")
    MIN_HANDOFF_RADIUS_MM = _motion_setting("handoff", "MIN_HANDOFF_RADIUS_MM")
    EXECUTION_VALIDATED_MAX_MM = _motion_setting(
        "handoff", "EXECUTION_VALIDATED_MAX_MM"
    )
    MAX_REAR_DEVIATION_DEG = _motion_setting("handoff", "MAX_REAR_DEVIATION_DEG")
    MAX_POSE_XY_SPREAD_MM = _motion_setting("handoff", "MAX_POSE_XY_SPREAD_MM")

    @staticmethod
    def rear_deviation_deg(angle_deg):
        """
        rear = +/-180 deg 기준 편차.
        +157 deg -> 23 deg
        -170 deg -> 10 deg
        """
        d = (angle_deg - 180.0 + 180.0) % 360.0 - 180.0
        return abs(d)

    @staticmethod
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
        q = np.asarray(pull_q, dtype=float).reshape(5)
        lower = np.asarray(HandoffMotion.core.cfg.JOINT_LIMIT_LOWER_DEG, dtype=float)
        upper = np.asarray(HandoffMotion.core.cfg.JOINT_LIMIT_UPPER_DEG, dtype=float)
        sum23 = float(q[1] + q[2])
        j2_min = max(float(lower[1]), sum23 - float(upper[2]))
        j2_max = min(float(upper[1]), sum23 - float(lower[2]))
        if j2_min > j2_max:
            raise RuntimeError(
                f"no feasible J2/J3 interval | J2=[{j2_min:.3f},{j2_max:.3f}]"
            )

        def radius_for_j2(j2_deg):
            return HandoffMotion.core.LINK2_MM * math.sin(
                math.radians(j2_deg)
            ) + HandoffMotion.core.LINK3_MM * math.sin(math.radians(sum23))

        candidates = [j2_min, j2_max]
        for k in range(-4, 5):
            critical = 90.0 + 180.0 * k
            if j2_min <= critical <= j2_max:
                candidates.append(critical)
        candidate_data = []
        for j2 in candidates:
            j3 = sum23 - j2
            radius = radius_for_j2(j2)
            candidate_data.append((float(radius), float(j2), float(j3)))
        min_item = min(candidate_data, key=lambda x: x[0])
        max_item = max(candidate_data, key=lambda x: x[0])
        if max_item[0] <= 0.0:
            raise RuntimeError(f"invalid maximum radial reach | {max_item[0]:.3f} mm")
        return {
            "sum23": sum23,
            "j2_min": j2_min,
            "j2_max": j2_max,
            "min_radius": float(min_item[0]),
            "max_radius": float(max_item[0]),
            "max_j2": float(max_item[1]),
            "max_j3": float(max_item[2]),
        }

    @staticmethod
    def compute_handoff_target(person_xyz_mm, max_radius_mm):
        x, y, z = [float(v) for v in person_xyz_mm]
        person_r = math.hypot(x, y)
        if person_r < 1.0:
            raise RuntimeError("invalid person distance")
        if x >= 0.0:
            raise RuntimeError(f"person is not behind robot | X={x:.1f}")
        angle_deg = math.degrees(math.atan2(y, x))
        rear_dev = HandoffMotion.rear_deviation_deg(angle_deg)
        if rear_dev > HandoffMotion.MAX_REAR_DEVIATION_DEG:
            raise RuntimeError(
                f"person direction too far from rear | angle={angle_deg:.2f} deg, rear deviation={rear_dev:.2f} deg"
            )
        wanted_radius = person_r - HandoffMotion.STANDOFF_MM
        if wanted_radius < HandoffMotion.MIN_HANDOFF_RADIUS_MM:
            raise RuntimeError(
                f"person too close for safe handoff | person_r={person_r:.1f} mm, wanted_r={wanted_radius:.1f} mm"
            )
        max_radius_mm = float(max_radius_mm)
        if max_radius_mm < HandoffMotion.MIN_HANDOFF_RADIUS_MM:
            raise RuntimeError(
                f"kinematic max reach too short | max_radius={max_radius_mm:.1f} mm"
            )
        handoff_radius = min(wanted_radius, max_radius_mm)
        ux = x / person_r
        uy = y / person_r
        target_x = ux * handoff_radius
        target_y = uy * handoff_radius
        actual_standoff = person_r - handoff_radius
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

    @staticmethod
    def build_dynamic_handoff(pull_q, handoff):
        pull_q = np.asarray(pull_q, dtype=float).reshape(5)
        target_j1 = float(handoff["angle_deg"])
        target_radius = float(handoff["handoff_radius"])
        lower = np.asarray(HandoffMotion.core.cfg.JOINT_LIMIT_LOWER_DEG, dtype=float)
        upper = np.asarray(HandoffMotion.core.cfg.JOINT_LIMIT_UPPER_DEG, dtype=float)
        if not lower[0] <= target_j1 <= upper[0]:
            raise RuntimeError(f"dynamic J1 limit exceeded: {target_j1:.2f} deg")
        turn_q = pull_q.copy()
        turn_q[0] = target_j1
        turn_q[4] = HandoffMotion.core.J5_DEG
        turn_ticks = np.asarray(
            HandoffMotion.core.cfg.model_deg_to_ticks(turn_q), dtype=np.int64
        )
        if int(turn_ticks[3]) > HandoffMotion.core.J4_SOFT_MAX_TICK:
            raise RuntimeError("dynamic TURN J4 soft max exceeded")
        sum23 = float(turn_q[1] + turn_q[2])
        total_tooldown = float(sum23 + turn_q[3])
        if abs(total_tooldown - 180.0) > 0.2:
            raise RuntimeError(f"tool-down sum mismatch | {total_tooldown:.3f}")
        rhs = (
            target_radius - HandoffMotion.core.LINK3_MM * math.sin(math.radians(sum23))
        ) / HandoffMotion.core.LINK2_MM
        if not -1.0 <= rhs <= 1.0:
            raise RuntimeError(
                f"dynamic EXTEND unreachable | rhs={rhs:.6f}, radius={target_radius:.1f}"
            )
        final_j2 = math.degrees(math.asin(rhs))
        final_j3 = sum23 - final_j2
        final_q = np.array(
            [target_j1, final_j2, final_j3, turn_q[3], HandoffMotion.core.J5_DEG]
        )
        if np.any(final_q < lower) or np.any(final_q > upper):
            raise RuntimeError(
                f"dynamic final joint limit exceeded | q={np.round(final_q, 3).tolist()}"
            )
        points = []
        for i in range(1, HandoffMotion.core.EXTEND_SEGMENTS + 1):
            u = i / HandoffMotion.core.EXTEND_SEGMENTS
            q = (1.0 - u) * turn_q + u * final_q
            q[0] = target_j1
            q[3] = turn_q[3]
            q[4] = HandoffMotion.core.J5_DEG
            q[2] = sum23 - q[1]
            ticks = np.asarray(
                HandoffMotion.core.cfg.model_deg_to_ticks(q), dtype=np.int64
            )
            if int(ticks[3]) > HandoffMotion.core.J4_SOFT_MAX_TICK:
                raise RuntimeError("dynamic EXTEND J4 soft max exceeded")
            points.append({"name": f"DYNAMIC_EXTEND_{i}", "q": q, "ticks": ticks})
        chain = HandoffMotion.core.spp.create_robot_chain()
        state = HandoffMotion.core.pose_state(chain, np.deg2rad(final_q))
        tool_axis = HandoffMotion.core.unit(state["tool_axis"])
        tcp = (
            np.asarray(state["xyz"], dtype=float)
            + HandoffMotion.core.AXIAL_OFFSET_MM * tool_axis
        )
        return {
            "turn_q": turn_q,
            "turn_ticks": turn_ticks,
            "extend": points,
            "final_q": final_q,
            "final_tcp": tcp,
        }

    class Runner:

        def __init__(self, parent_node, use_motor=False):
            self._parent_node = parent_node
            self._callback_group = ReentrantCallbackGroup()
            self.use_motor = use_motor
            self.pose_samples = deque(maxlen=HandoffMotion.POSE_WINDOW)
            self.current = None
            if use_motor:
                self.create_subscription(
                    Int32MultiArray, "/motor/joint_state", self.joint_cb, 10
                )
                self.path = self.create_client(
                    MoveTickPath, HandoffMotion.core.PATH_SERVICE
                )
                self.open = self.create_client(Trigger, HandoffMotion.core.OPEN_SERVICE)
                self.close = self.create_client(
                    Trigger, HandoffMotion.core.CLOSE_SERVICE
                )

        def __getattr__(self, name):
            return getattr(self._parent_node, name)

        def create_subscription(
            self, msg_type, topic, callback, qos_profile, *args, **kwargs
        ):
            kwargs.setdefault("callback_group", self._callback_group)
            return self._parent_node.create_subscription(
                msg_type, topic, callback, qos_profile, *args, **kwargs
            )

        def create_client(self, srv_type, srv_name, *args, **kwargs):
            kwargs.setdefault("callback_group", self._callback_group)
            return self._parent_node.create_client(srv_type, srv_name, *args, **kwargs)

        def _wait_future(self, future):
            while rclpy.ok() and (not future.done()):
                time.sleep(0.01)

        def destroy_node(self):
            return None

        def pose_cb(self, msg):
            x = float(msg.point.x) * 1000.0
            y = float(msg.point.y) * 1000.0
            z = float(msg.point.z) * 1000.0
            if not all((math.isfinite(v) for v in (x, y, z))):
                return
            self.pose_samples.append((x, y, z))

        def joint_cb(self, msg):
            if len(msg.data) == 5:
                self.current = np.asarray(msg.data, dtype=np.int64)

        def collect_pose(self):
            self.pose_samples.clear()
            deadline = time.monotonic() + HandoffMotion.POSE_TIMEOUT_SEC
            while (
                len(self.pose_samples) < HandoffMotion.POSE_WINDOW
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            if len(self.pose_samples) < HandoffMotion.POSE_MIN_SAMPLES:
                raise RuntimeError(
                    f"not enough driver pose samples | {len(self.pose_samples)}"
                )
            a = np.asarray(self.pose_samples, dtype=float)
            median = np.median(a, axis=0)
            xy_error = a[:, :2] - median[:2]
            distances = np.linalg.norm(xy_error, axis=1)
            INLIER_RADIUS_MM = 150.0
            inlier_mask = distances <= INLIER_RADIUS_MM
            inliers = a[inlier_mask]
            if len(inliers) < HandoffMotion.POSE_MIN_SAMPLES:
                raise RuntimeError(
                    f"driver pose unstable | inliers={len(inliers)}/{len(a)}, distances={np.round(distances, 1).tolist()}"
                )
            filtered_median = np.median(inliers, axis=0)
            filtered_xy_error = inliers[:, :2] - filtered_median[:2]
            filtered_spread = float(np.max(np.linalg.norm(filtered_xy_error, axis=1)))
            print(
                f"[POSE FILTER] samples={len(a)}, inliers={len(inliers)}, spread={filtered_spread:.1f} mm"
            )
            return filtered_median

        def wait_services(self):
            for c, name in (
                (self.path, HandoffMotion.core.PATH_SERVICE),
                (self.open, HandoffMotion.core.OPEN_SERVICE),
                (self.close, HandoffMotion.core.CLOSE_SERVICE),
            ):
                if not c.wait_for_service(timeout_sec=5.0):
                    raise RuntimeError(f"service unavailable: {name}")

        def get_current(self):
            for _ in range(50):
                time.sleep(0.1)
                if self.current is not None:
                    return self.current.copy()
            raise RuntimeError("no /motor/joint_state")

        def trigger(self, client, label):
            future = client.call_async(Trigger.Request())
            self._wait_future(future)
            res = future.result()
            if res is None or not res.success:
                raise RuntimeError(
                    f"{label} failed: {getattr(res, 'message', 'no response')}"
                )
            print(f"[OK] {label}: {res.message}")

        def move(self, name, ticks_list):
            arr = np.asarray(ticks_list, dtype=np.int64).reshape(-1, 5)
            req = MoveTickPath.Request()
            req.joint_ticks = [int(v) for v in arr.reshape(-1)]
            req.point_count = len(arr)
            req.label = name
            req.profile_velocity = 0
            req.timeout_sec = HandoffMotion.core.PATH_TIMEOUT_SEC
            future = self.path.call_async(req)
            self._wait_future(future)
            res = future.result()
            if res is None or not res.success:
                raise RuntimeError(
                    f"{name} failed: {getattr(res, 'message', 'no response')}"
                )
            print(f"[OK] {name} | reached={list(res.reached_ticks)}")


class FullIKHandoff:
    core = BagMotion
    Runner = HandoffMotion.Runner
    compute_handoff_target = HandoffMotion.compute_handoff_target
    SPEC = HandoffReference.SPEC
    EXEC_MAX_RADIUS_MM = _motion_setting("fullik_handoff", "EXEC_MAX_RADIUS_MM")
    STANDOFF_MM = _motion_setting("fullik_handoff", "STANDOFF_MM")
    HANDOFF_SEGMENTS = _motion_setting("fullik_handoff", "HANDOFF_SEGMENTS")

    @staticmethod
    def tcp_from_q(chain, q_deg):
        state = FullIKHandoff.core.pose_state(
            chain, np.deg2rad(np.asarray(q_deg, dtype=float))
        )
        axis = FullIKHandoff.core.unit(state["tool_axis"])
        tcp = (
            np.asarray(state["xyz"], dtype=float)
            + FullIKHandoff.core.AXIAL_OFFSET_MM * axis
        )
        return (tcp, axis)

    @staticmethod
    def build_turn(plan, handoff):
        q = plan["PULL100"]["q"].copy()
        q[0] = float(handoff["angle_deg"])
        q[4] = float(plan["PULL100"]["q"][4])
        lower = np.asarray(FullIKHandoff.core.cfg.JOINT_LIMIT_LOWER_DEG, dtype=float)
        upper = np.asarray(FullIKHandoff.core.cfg.JOINT_LIMIT_UPPER_DEG, dtype=float)
        if np.any(q < lower) or np.any(q > upper):
            raise RuntimeError(
                f"TURN joint limit exceeded | q={np.round(q, 3).tolist()}"
            )
        ticks = np.asarray(FullIKHandoff.core.cfg.model_deg_to_ticks(q), dtype=np.int64)
        if int(ticks[3]) > FullIKHandoff.core.J4_SOFT_MAX_TICK:
            raise RuntimeError(
                f"TURN J4 soft max exceeded | {ticks[3]} > {FullIKHandoff.core.J4_SOFT_MAX_TICK}"
            )
        return (q, ticks)

    @staticmethod
    def build_fullik_path(plan, handoff, *, joint_blend=False, blend_alpha=0.2):
        chain = FullIKHandoff.core.spp.create_robot_chain()
        turn_q, turn_ticks = FullIKHandoff.build_turn(plan, handoff)
        turn_tcp, turn_axis = FullIKHandoff.tcp_from_q(chain, turn_q)
        if turn_axis[2] > -0.99:
            raise RuntimeError(f"TURN tool axis is not down | {turn_axis.tolist()}")
        old_final_q = plan["EXTEND"][-1]["q"]
        old_tcp, old_axis = FullIKHandoff.tcp_from_q(chain, old_final_q)
        target_z = float(old_tcp[2])
        az = math.radians(float(handoff["angle_deg"]))
        final_r = float(handoff["handoff_radius"])
        if final_r > FullIKHandoff.EXEC_MAX_RADIUS_MM + 1e-06:
            raise RuntimeError(
                f"execution radius exceeds validated maximum | {final_r:.1f} > {FullIKHandoff.EXEC_MAX_RADIUS_MM:.1f} mm"
            )
        final_target = np.array(
            [final_r * math.cos(az), final_r * math.sin(az), target_z]
        )
        points = []
        seed_q = turn_q.copy()
        lower = np.asarray(FullIKHandoff.core.cfg.JOINT_LIMIT_LOWER_DEG, dtype=float)
        upper = np.asarray(FullIKHandoff.core.cfg.JOINT_LIMIT_UPPER_DEG, dtype=float)
        if joint_blend:
            alpha = float(blend_alpha)
            if not 0.0 <= alpha <= 1.0:
                raise ValueError(f"blend_alpha must be 0~1 | {alpha}")
            expected_j1 = float(handoff["angle_deg"])
            final_solved = FullIKHandoff.core.solve_tcp(
                chain, final_target, 90.0, turn_q
            )
            final_q = np.asarray(final_solved["q"], dtype=float).copy()
            final_q[0] = expected_j1
            final_q[4] = float(turn_q[4])
            final_ticks = np.asarray(
                FullIKHandoff.core.cfg.model_deg_to_ticks(final_q), dtype=np.int64
            )
            if np.any(final_q < lower) or np.any(final_q > upper):
                raise RuntimeError(
                    f"SYNC HANDOFF final joint limit exceeded | q={np.round(final_q, 3).tolist()}"
                )
            if int(final_ticks[3]) > FullIKHandoff.core.J4_SOFT_MAX_TICK:
                raise RuntimeError(
                    f"SYNC HANDOFF final J4 soft max exceeded | {final_ticks[3]} > {FullIKHandoff.core.J4_SOFT_MAX_TICK}"
                )
            final_total = final_q[1] + final_q[2] + final_q[3]
            if abs(final_total - 180.0) > 0.2:
                raise RuntimeError(
                    f"SYNC HANDOFF final tool-down mismatch | {final_total:.3f}"
                )
            final_tcp, final_axis = FullIKHandoff.tcp_from_q(chain, final_q)
            final_tcp_error = float(np.linalg.norm(final_tcp - final_target))
            if final_tcp_error > 2.0:
                raise RuntimeError(
                    f"SYNC HANDOFF final TCP changed after J5 hold | error={final_tcp_error:.3f} mm"
                )
            if final_axis[2] > -0.99:
                raise RuntimeError(
                    f"SYNC HANDOFF final tool axis lost | {final_axis.tolist()}"
                )
            cart_seed_q = turn_q.copy()
            for i in range(1, FullIKHandoff.HANDOFF_SEGMENTS + 1):
                u = i / FullIKHandoff.HANDOFF_SEGMENTS
                target = (1.0 - u) * turn_tcp + u * final_target
                cart_solved = FullIKHandoff.core.solve_tcp(
                    chain, target, 90.0, cart_seed_q
                )
                cart_q = np.asarray(cart_solved["q"], dtype=float).copy()
                cart_q[0] = expected_j1
                cart_q[4] = float(turn_q[4])
                joint_q = (1.0 - u) * turn_q + u * final_q
                joint_q[0] = expected_j1
                joint_q[4] = float(turn_q[4])
                q = (1.0 - alpha) * cart_q + alpha * joint_q
                q[0] = expected_j1
                q[4] = float(turn_q[4])
                cart_seed_q = cart_q
                ticks = np.asarray(
                    FullIKHandoff.core.cfg.model_deg_to_ticks(q), dtype=np.int64
                )
                if np.any(q < lower) or np.any(q > upper):
                    raise RuntimeError(
                        f"SYNC HANDOFF joint limit exceeded | i={i} q={np.round(q, 3).tolist()}"
                    )
                if int(ticks[3]) > FullIKHandoff.core.J4_SOFT_MAX_TICK:
                    raise RuntimeError(
                        f"SYNC HANDOFF J4 soft max exceeded | i={i} {ticks[3]} > {FullIKHandoff.core.J4_SOFT_MAX_TICK}"
                    )
                total = q[1] + q[2] + q[3]
                if abs(total - 180.0) > 0.2:
                    raise RuntimeError(
                        f"SYNC HANDOFF tool-down mismatch | i={i} {total:.3f}"
                    )
                tcp, axis = FullIKHandoff.tcp_from_q(chain, q)
                if axis[2] > -0.99:
                    raise RuntimeError(
                        f"SYNC HANDOFF tool axis lost | i={i} {axis.tolist()}"
                    )
                target = (1.0 - u) * turn_tcp + u * final_target
                path_deviation = float(np.linalg.norm(tcp - target))
                points.append(
                    {
                        "name": f"SYNC_HANDOFF_{i}",
                        "target": target,
                        "q": q,
                        "ticks": ticks,
                        "tcp": tcp,
                        "axis": axis,
                        "error": path_deviation,
                    }
                )
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
                "max_path_deviation": max((p["error"] for p in points)),
            }
        for i in range(1, FullIKHandoff.HANDOFF_SEGMENTS + 1):
            u = i / FullIKHandoff.HANDOFF_SEGMENTS
            target = (1.0 - u) * turn_tcp + u * final_target
            solved = FullIKHandoff.core.solve_tcp(chain, target, 90.0, seed_q)
            q = solved["q"].copy()
            ticks = solved["ticks"].copy()
            expected_j1 = float(handoff["angle_deg"])
            if abs(q[0] - expected_j1) > 0.2:
                raise RuntimeError(
                    f"J1 direction mismatch | {q[0]:.3f} vs {expected_j1:.3f}"
                )
            if np.any(q < lower) or np.any(q > upper):
                raise RuntimeError(
                    f"HANDOFF joint limit exceeded | q={np.round(q, 3).tolist()}"
                )
            if int(ticks[3]) > FullIKHandoff.core.J4_SOFT_MAX_TICK:
                raise RuntimeError(
                    f"HANDOFF J4 soft max exceeded | {ticks[3]} > {FullIKHandoff.core.J4_SOFT_MAX_TICK}"
                )
            total = q[1] + q[2] + q[3]
            if abs(total - 180.0) > 0.2:
                raise RuntimeError(f"tool-down sum mismatch | {total:.3f}")
            tcp, axis = FullIKHandoff.tcp_from_q(chain, q)
            if axis[2] > -0.99:
                raise RuntimeError(f"tool axis lost | {axis.tolist()}")
            points.append(
                {
                    "name": f"FULLIK_HANDOFF_{i}",
                    "target": target,
                    "q": q,
                    "ticks": ticks,
                    "tcp": tcp,
                    "axis": axis,
                    "error": float(solved["tcp_error"]),
                }
            )
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


# Paper bag dynamic pickup
class PaperBagMotion:
    core = BagMotion
    SPEC = PaperBagReference.SPEC
    PICK_MIN_RADIUS_MM = PaperBagReference.PICK_MIN_RADIUS_MM
    PICK_MAX_RADIUS_MM = PaperBagReference.PICK_MAX_RADIUS_MM
    Runner = HandoffMotion.Runner
    compute_handoff_target = HandoffMotion.compute_handoff_target
    build_fullik_path = FullIKHandoff.build_fullik_path
    HANDOFF_REFERENCE_SPEC = HandoffReference.SPEC
    R_CAM_TO_ARM = np.array(
        _motion_setting("paper_bag_pick", "R_CAM_TO_ARM"), dtype=float
    )
    T_CAM_TO_ARM_MM = np.array(
        _motion_setting("paper_bag_pick", "T_CAM_TO_ARM_MM"), dtype=float
    )
    TABLE_Z_MM = _motion_setting("paper_bag_pick", "TABLE_Z_MM")
    DETECTION_TOPIC = _motion_setting("paper_bag_pick", "DETECTION_TOPIC")
    CAMERA_INFO_TOPIC = _motion_setting("paper_bag_pick", "CAMERA_INFO_TOPIC")
    VISION_CLASS = _motion_setting("paper_bag_pick", "VISION_CLASS")
    MIN_CONFIDENCE = _motion_setting("paper_bag_pick", "MIN_CONFIDENCE")
    BAG_WINDOW = _motion_setting("paper_bag_pick", "BAG_WINDOW")
    BAG_MIN_SAMPLES = _motion_setting("paper_bag_pick", "BAG_MIN_SAMPLES")
    BAG_TIMEOUT_SEC = _motion_setting("paper_bag_pick", "BAG_TIMEOUT_SEC")
    MAX_BAG_XY_SPREAD_MM = _motion_setting("paper_bag_pick", "MAX_BAG_XY_SPREAD_MM")
    USE_LONG_AXIS_BOTTOM_ESTIMATE = _motion_setting(
        "paper_bag_pick", "USE_LONG_AXIS_BOTTOM_ESTIMATE"
    )
    PULL_MM = _motion_setting("paper_bag_pick", "PULL_MM")
    TCP_ERROR_MAX_MM = _motion_setting("paper_bag_pick", "TCP_ERROR_MAX_MM")
    J3_MIN_TICK = _motion_setting("paper_bag_pick", "J3_MIN_TICK")
    J3_MAX_TICK = _motion_setting("paper_bag_pick", "J3_MAX_TICK")
    J4_MIN_TICK = _motion_setting("paper_bag_pick", "J4_MIN_TICK")
    J4_MAX_TICK = _motion_setting("paper_bag_pick", "J4_MAX_TICK")
    core.cfg.JOINT_LIMIT_UPPER_DEG[2] = 150.0
    if hasattr(core, "spp") and hasattr(core.spp, "JOINT_LIMIT_UPPER_DEG"):
        core.spp.JOINT_LIMIT_UPPER_DEG[2] = 150.0
    core.J4_SOFT_MAX_TICK = J4_MAX_TICK

    @staticmethod
    def check_pick_workspace(x_mm, y_mm):
        r = float(math.hypot(x_mm, y_mm))
        if x_mm <= 0.0:
            raise RuntimeError(
                f"pick target is not in front workspace | X={x_mm:.1f} mm"
            )
        if r < PaperBagMotion.PICK_MIN_RADIUS_MM:
            raise RuntimeError(
                f"pick target too close | R={r:.1f} < {PaperBagMotion.PICK_MIN_RADIUS_MM:.1f} mm"
            )
        if r > PaperBagMotion.PICK_MAX_RADIUS_MM:
            raise RuntimeError(
                f"pick target outside safe workspace | R={r:.1f} > {PaperBagMotion.PICK_MAX_RADIUS_MM:.1f} mm"
            )
        return r

    @staticmethod
    def validate_solution(name, solved):
        q = np.asarray(solved["q"], dtype=float)
        ticks = np.asarray(solved["ticks"], dtype=np.int64)
        err = float(solved["tcp_error"])
        if err > PaperBagMotion.TCP_ERROR_MAX_MM:
            raise RuntimeError(
                f"{name}: TCP error {err:.3f} mm > {PaperBagMotion.TCP_ERROR_MAX_MM:.3f} mm"
            )
        if (
            not PaperBagMotion.J3_MIN_TICK
            <= int(ticks[2])
            <= PaperBagMotion.J3_MAX_TICK
        ):
            raise RuntimeError(
                f"{name}: J3 raw limit | {int(ticks[2])} not in [{PaperBagMotion.J3_MIN_TICK},{PaperBagMotion.J3_MAX_TICK}]"
            )
        if (
            not PaperBagMotion.J4_MIN_TICK
            <= int(ticks[3])
            <= PaperBagMotion.J4_MAX_TICK
        ):
            raise RuntimeError(
                f"{name}: J4 raw limit | {int(ticks[3])} not in [{PaperBagMotion.J4_MIN_TICK},{PaperBagMotion.J4_MAX_TICK}]"
            )
        return {"q": q, "ticks": ticks, "error": err}

    @staticmethod
    def solve_phase(chain, name, target, tilt_deg, seed_q):
        solved = PaperBagMotion.core.solve_tcp(
            chain,
            np.asarray(target, dtype=float),
            float(tilt_deg),
            np.asarray(seed_q, dtype=float),
        )
        checked = PaperBagMotion.validate_solution(name, solved)
        checked["target"] = np.asarray(target, dtype=float)
        checked["tilt"] = float(tilt_deg)
        return checked

    PICK_J5_DEG = _motion_setting("paper_bag_pick", "PICK_J5_DEG")
    PICK_J5_TICK = _motion_setting("paper_bag_pick", "PICK_J5_TICK")

    @staticmethod
    def _fix_pick_j5(plan):
        plan = dict(plan)
        q = np.asarray(plan["q"], dtype=float).copy()
        ticks = np.asarray(plan["ticks"], dtype=np.int64).copy()
        q[4] = PaperBagMotion.PICK_J5_DEG
        ticks[4] = PaperBagMotion.PICK_J5_TICK
        plan["q"] = q
        plan["ticks"] = ticks
        return plan

    @staticmethod
    def build_dynamic_pick_plan(x_mm, y_mm):
        radius = PaperBagMotion.check_pick_workspace(x_mm, y_mm)
        chain = PaperBagMotion.core.spp.create_robot_chain()
        ref_plan = PaperBagMotion.core.build_plan(PaperBagMotion.SPEC)
        seed_q = ref_plan["READY"]["q"].copy()
        out = {}
        phase_defs = [
            (
                "READY",
                np.array([x_mm, y_mm, float(PaperBagMotion.SPEC.ready_z_mm)]),
                float(PaperBagMotion.SPEC.ready_tilt_deg),
            ),
            (
                "GRASP",
                np.array([x_mm, y_mm, float(PaperBagMotion.SPEC.grasp_z_mm)]),
                float(PaperBagMotion.SPEC.grasp_tilt_deg),
            ),
            (
                "LIFT",
                np.array([x_mm, y_mm, float(PaperBagMotion.SPEC.lift_z_mm)]),
                float(PaperBagMotion.SPEC.lift_tilt_deg),
            ),
        ]
        for name, target, tilt in phase_defs:
            p = PaperBagMotion.solve_phase(chain, name, target, tilt, seed_q)
            p = PaperBagMotion._fix_pick_j5(p)
            out[name] = p
            seed_q = p["q"].copy()
        pull_radius = radius - PaperBagMotion.PULL_MM
        if pull_radius <= 0.0:
            raise RuntimeError(f"invalid pull radius | {pull_radius:.1f} mm")
        ux = float(x_mm) / radius
        uy = float(y_mm) / radius
        pull_target = np.array(
            [ux * pull_radius, uy * pull_radius, float(PaperBagMotion.SPEC.lift_z_mm)]
        )
        pull = PaperBagMotion.solve_phase(
            chain, "PULL100", pull_target, 90.0, out["LIFT"]["q"]
        )
        pull = PaperBagMotion._fix_pick_j5(pull)
        out["PULL100"] = pull
        return (radius, out)

    @staticmethod
    def make_handoff_plan(dynamic_pick):
        """
        Full-IK handoff code needs:
          - current PULL100 q as the TURN seed
          - an EXTEND reference only to preserve the already-validated handoff Z

        Keep the old successful smallbag handoff height, but use the new detected
        paper_bag PULL100 posture for TURN/HANDOFF.
        """
        reference = PaperBagMotion.core.build_plan(
            PaperBagMotion.HANDOFF_REFERENCE_SPEC
        )
        return {"PULL100": dynamic_pick["PULL100"], "EXTEND": reference["EXTEND"]}


# Handled bag reference and horizontal pickup
class HandledBagMotion:
    cfg = robot_config
    pose_state = BagKinematics.pose_state
    PATH_SERVICE = _motion_setting("handled_bag_reference", "PATH_SERVICE")
    OPEN_SERVICE = _motion_setting("handled_bag_reference", "OPEN_SERVICE")
    CLOSE_SERVICE = _motion_setting("handled_bag_reference", "CLOSE_SERVICE")
    BAG_X_MM = _motion_setting("handled_bag_reference", "BAG_X_MM")
    BAG_Y_MM = _motion_setting("handled_bag_reference", "BAG_Y_MM")
    BAG_HEIGHT_MM = _motion_setting("handled_bag_reference", "BAG_HEIGHT_MM")
    BAG_WIDTH_MM = _motion_setting("handled_bag_reference", "BAG_WIDTH_MM")
    BAG_DEPTH_MM = _motion_setting("handled_bag_reference", "BAG_DEPTH_MM")
    PICK_AXIAL_OFFSET_MM = _motion_setting(
        "handled_bag_reference", "PICK_AXIAL_OFFSET_MM"
    )
    CARRY_AXIAL_OFFSET_MM = _motion_setting(
        "handled_bag_reference", "CARRY_AXIAL_OFFSET_MM"
    )
    L_PAPER_LIMIT_LOWER_DEG = cfg.JOINT_LIMIT_LOWER_DEG
    L_PAPER_LIMIT_UPPER_DEG = cfg.JOINT_LIMIT_UPPER_DEG
    J5_DEG = _motion_setting("handled_bag_reference", "J5_DEG")
    GRIPPER_OPEN_TICK = _motion_setting("handled_bag_reference", "GRIPPER_OPEN_TICK")
    GRIPPER_CLOSE_TICK = _motion_setting("handled_bag_reference", "GRIPPER_CLOSE_TICK")
    PREP_TILT_DEG = _motion_setting("handled_bag_reference", "PREP_TILT_DEG")
    PICK_TILT_DEG = _motion_setting("handled_bag_reference", "PICK_TILT_DEG")
    GRASP_DEPTH_MM = _motion_setting("handled_bag_reference", "GRASP_DEPTH_MM")
    GRASP_Z_MM = _motion_setting("handled_bag_reference", "GRASP_Z_MM")
    READY_Z_MM = _motion_setting("handled_bag_reference", "READY_Z_MM")
    PREP_RADIUS_MM = _motion_setting("handled_bag_reference", "PREP_RADIUS_MM")
    PREP_Z_MM = _motion_setting("handled_bag_reference", "PREP_Z_MM")
    LIFT_Z_MM = _motion_setting("handled_bag_reference", "LIFT_Z_MM")
    CARRY_RADIUS_MM = _motion_setting("handled_bag_reference", "CARRY_RADIUS_MM")
    CARRY_Z_MM = _motion_setting("handled_bag_reference", "CARRY_Z_MM")
    CARRY_TILT_DEG = PICK_TILT_DEG
    FINAL_RADII_MM = _motion_setting("handled_bag_reference", "FINAL_RADII_MM")
    NORMAL_VELOCITY = _motion_setting("handled_bag_reference", "NORMAL_VELOCITY")
    TURN_VELOCITY = _motion_setting("handled_bag_reference", "TURN_VELOCITY")
    START_TOL = np.array(
        _motion_setting("handled_bag_reference", "START_TOL"), dtype=np.int64
    )
    PICK_RADIUS_MM = float(np.hypot(BAG_X_MM, BAG_Y_MM))
    PICK_AZ_DEG = float(np.rad2deg(np.arctan2(BAG_Y_MM, BAG_X_MM)))

    @staticmethod
    def unit(v):
        v = np.asarray(v, dtype=float)
        return v / np.linalg.norm(v)

    @staticmethod
    def tool_axis_for(az_deg, tilt_deg):
        """
        radial outward + downward tool axis.

        tilt=0   : horizontal
        tilt=90  : vertical down
        """
        az = np.deg2rad(float(az_deg))
        tilt = np.deg2rad(float(tilt_deg))
        return HandledBagMotion.unit(
            np.array(
                [np.cos(tilt) * np.cos(az), np.cos(tilt) * np.sin(az), -np.sin(tilt)],
                dtype=float,
            )
        )

    @staticmethod
    def tip_target(radius_mm, az_deg, z_mm):
        az = np.deg2rad(float(az_deg))
        return np.array(
            [radius_mm * np.cos(az), radius_mm * np.sin(az), z_mm], dtype=float
        )

    @staticmethod
    def solve_pose(
        chain,
        label,
        radius_mm,
        az_deg,
        tip_z_mm,
        tilt_deg,
        seed_q=None,
        axial_offset_mm=PICK_AXIAL_OFFSET_MM,
    ):
        """
        중앙 finger-tip 위치 + tool tilt 고정.

        J1 = target azimuth
        J5 = 90 deg

        J2/J3/J4를 IK로 계산.
        """
        target_tip = HandledBagMotion.tip_target(radius_mm, az_deg, tip_z_mm)
        desired_tool = HandledBagMotion.tool_axis_for(az_deg, tilt_deg)
        model_target = target_tip - float(axial_offset_mm) * desired_tool
        j1_deg = float(az_deg)
        target_sum_deg = 90.0 + float(tilt_deg)
        if seed_q is None:
            start_q = HandledBagMotion.cfg.ticks_to_model_deg(
                HandledBagMotion.cfg.ASSUMED_START_TICKS
            )
            seed = start_q[1:4].copy()
        else:
            seed = np.asarray(seed_q, dtype=float)[1:4].copy()
        lo = HandledBagMotion.L_PAPER_LIMIT_LOWER_DEG[1:4].copy()
        hi = HandledBagMotion.L_PAPER_LIMIT_UPPER_DEG[1:4].copy()
        seed = np.clip(seed, lo + 1e-05, hi - 1e-05)

        def residual(v):
            q = np.array(
                [j1_deg, v[0], v[1], v[2], HandledBagMotion.J5_DEG], dtype=float
            )
            state = HandledBagMotion.pose_state(chain, np.deg2rad(q))
            xyz_error = np.asarray(state["xyz"], dtype=float) - model_target
            sum_error = (np.sum(v) - target_sum_deg) * 10.0
            return np.concatenate([xyz_error, [sum_error]])

        result = least_squares(
            residual,
            seed,
            bounds=(lo + 1e-06, hi - 1e-06),
            max_nfev=5000,
            ftol=1e-12,
            xtol=1e-12,
            gtol=1e-12,
        )
        q = np.array(
            [j1_deg, result.x[0], result.x[1], result.x[2], HandledBagMotion.J5_DEG],
            dtype=float,
        )
        state = HandledBagMotion.pose_state(chain, np.deg2rad(q))
        actual_tool = HandledBagMotion.unit(state["tool_axis"])
        actual_tip = (
            np.asarray(state["xyz"], dtype=float) + float(axial_offset_mm) * actual_tool
        )
        tip_error = float(np.linalg.norm(actual_tip - target_tip))
        axis_dot = np.clip(np.dot(actual_tool, desired_tool), -1.0, 1.0)
        axis_error = float(np.rad2deg(np.arccos(axis_dot)))
        margin = np.minimum(
            q - HandledBagMotion.L_PAPER_LIMIT_LOWER_DEG,
            HandledBagMotion.L_PAPER_LIMIT_UPPER_DEG - q,
        )
        if tip_error > 2.0:
            raise RuntimeError(f"{label}: tip IK error {tip_error:.3f}mm")
        if axis_error > 1.0:
            raise RuntimeError(f"{label}: tool axis error {axis_error:.3f}deg")
        if np.any(margin < 0.0):
            raise RuntimeError(
                f"{label}: joint limit exceeded | q={np.round(q, 3).tolist()}"
            )
        ticks = HandledBagMotion.cfg.model_deg_to_ticks(q)
        return {
            "label": label,
            "q": q,
            "ticks": np.asarray(ticks, dtype=np.int64),
            "tip": actual_tip,
            "tool": actual_tool,
            "finger_x": np.asarray(state["finger_x"], dtype=float),
            "margin": margin,
            "tip_error": tip_error,
        }

    @staticmethod
    def interpolate(a, b, count):
        return np.linspace(float(a), float(b), int(count))

    @staticmethod
    def build_plan():
        chain = HandledBagMotion.cfg.create_robot_chain()
        prep = HandledBagMotion.solve_pose(
            chain,
            "PREP",
            HandledBagMotion.PREP_RADIUS_MM,
            HandledBagMotion.PICK_AZ_DEG,
            HandledBagMotion.PREP_Z_MM,
            HandledBagMotion.PREP_TILT_DEG,
        )
        seed = prep["q"]
        ready_path = []
        for i, alpha in enumerate(np.linspace(0.0, 1.0, 7)[1:], start=1):
            radius = HandledBagMotion.PREP_RADIUS_MM + alpha * (
                HandledBagMotion.PICK_RADIUS_MM - HandledBagMotion.PREP_RADIUS_MM
            )
            z = HandledBagMotion.PREP_Z_MM + alpha * (
                HandledBagMotion.READY_Z_MM - HandledBagMotion.PREP_Z_MM
            )
            tilt = HandledBagMotion.PREP_TILT_DEG + alpha * (
                HandledBagMotion.PICK_TILT_DEG - HandledBagMotion.PREP_TILT_DEG
            )
            point = HandledBagMotion.solve_pose(
                chain, f"READY_{i}", radius, HandledBagMotion.PICK_AZ_DEG, z, tilt, seed
            )
            ready_path.append(point)
            seed = point["q"]
        descend_path = []
        for i, z in enumerate(
            HandledBagMotion.interpolate(
                HandledBagMotion.READY_Z_MM, HandledBagMotion.GRASP_Z_MM, 7
            )[1:],
            start=1,
        ):
            point = HandledBagMotion.solve_pose(
                chain,
                f"DESCEND_{i}",
                HandledBagMotion.PICK_RADIUS_MM,
                HandledBagMotion.PICK_AZ_DEG,
                z,
                HandledBagMotion.PICK_TILT_DEG,
                seed,
            )
            descend_path.append(point)
            seed = point["q"]
        grasp = descend_path[-1]
        lift_path = []
        for i, z in enumerate(
            HandledBagMotion.interpolate(
                HandledBagMotion.GRASP_Z_MM, HandledBagMotion.LIFT_Z_MM, 7
            )[1:],
            start=1,
        ):
            point = HandledBagMotion.solve_pose(
                chain,
                f"LIFT_{i}",
                HandledBagMotion.PICK_RADIUS_MM,
                HandledBagMotion.PICK_AZ_DEG,
                z,
                HandledBagMotion.PICK_TILT_DEG,
                seed,
            )
            lift_path.append(point)
            seed = point["q"]
        carry_path = []
        for i, alpha in enumerate(np.linspace(0.0, 1.0, 7)[1:], start=1):
            radius = HandledBagMotion.PICK_RADIUS_MM + alpha * (
                HandledBagMotion.CARRY_RADIUS_MM - HandledBagMotion.PICK_RADIUS_MM
            )
            z = HandledBagMotion.LIFT_Z_MM + alpha * (
                HandledBagMotion.CARRY_Z_MM - HandledBagMotion.LIFT_Z_MM
            )
            tilt = HandledBagMotion.PICK_TILT_DEG + alpha * (
                HandledBagMotion.CARRY_TILT_DEG - HandledBagMotion.PICK_TILT_DEG
            )
            point = HandledBagMotion.solve_pose(
                chain,
                f"CARRY_{i}",
                radius,
                HandledBagMotion.PICK_AZ_DEG,
                z,
                tilt,
                seed,
                axial_offset_mm=HandledBagMotion.CARRY_AXIAL_OFFSET_MM,
            )
            carry_path.append(point)
            seed = point["q"]
        carry = carry_path[-1]
        current_j1 = float(carry["q"][0])
        target_j1 = min((-180.0, 180.0), key=lambda angle: abs(angle - current_j1))
        turn_q = carry["q"].copy()
        turn_q[0] = target_j1
        turn_ticks = HandledBagMotion.cfg.model_deg_to_ticks(turn_q)
        turn = {
            "label": "TURN",
            "q": turn_q,
            "ticks": np.asarray(turn_ticks, dtype=np.int64),
        }
        extend_path = []
        seed = turn_q.copy()
        for radius in HandledBagMotion.FINAL_RADII_MM:
            point = HandledBagMotion.solve_pose(
                chain,
                f"EXTEND_{int(radius)}",
                radius,
                target_j1,
                HandledBagMotion.CARRY_Z_MM,
                HandledBagMotion.CARRY_TILT_DEG,
                seed,
                axial_offset_mm=HandledBagMotion.CARRY_AXIAL_OFFSET_MM,
            )
            extend_path.append(point)
            seed = point["q"]
        return {
            "prep": prep,
            "ready": ready_path,
            "descend": descend_path,
            "grasp": grasp,
            "lift": lift_path,
            "carry": carry_path,
            "turn": turn,
            "extend": extend_path,
            "target_j1": target_j1,
        }


class HandledBagHorizontalMotion:
    core = BagMotion
    BODY_HEIGHT_MM = _motion_setting("handled_bag_horizontal", "BODY_HEIGHT_MM")
    BODY_CENTER_Z_MM = BODY_HEIGHT_MM / 2.0
    HANDLE_GRASP_EXTRA_MM = _motion_setting(
        "handled_bag_horizontal", "HANDLE_GRASP_EXTRA_MM"
    )
    GRASP_Z_MM = BODY_CENTER_Z_MM + BODY_HEIGHT_MM / 2.0 + HANDLE_GRASP_EXTRA_MM
    LIFT_MM = _motion_setting("handled_bag_horizontal", "LIFT_MM")
    LIFT_Z_MM = GRASP_Z_MM + LIFT_MM
    PREP_Z_MM = GRASP_Z_MM
    APPROACH_MM = _motion_setting("handled_bag_horizontal", "APPROACH_MM")
    INSERT_EXTRA_MM = _motion_setting("handled_bag_horizontal", "INSERT_EXTRA_MM")
    TCP_OFFSET_MM = _motion_setting("handled_bag_horizontal", "TCP_OFFSET_MM")
    HORIZONTAL_TILT_DEG = _motion_setting(
        "handled_bag_horizontal", "HORIZONTAL_TILT_DEG"
    )
    HORIZONTAL_J5_DEG = _motion_setting("handled_bag_horizontal", "HORIZONTAL_J5_DEG")
    HORIZONTAL_J5_TICK = _motion_setting("handled_bag_horizontal", "HORIZONTAL_J5_TICK")
    APPROACH_SEGMENTS = _motion_setting("handled_bag_horizontal", "APPROACH_SEGMENTS")
    LIFT_SEGMENTS = _motion_setting("handled_bag_horizontal", "LIFT_SEGMENTS")
    HANDOFF_SEGMENTS = _motion_setting("handled_bag_horizontal", "HANDOFF_SEGMENTS")
    MAX_HANDOFF_RADIUS_MM = _motion_setting(
        "handled_bag_horizontal", "MAX_HANDOFF_RADIUS_MM"
    )

    @staticmethod
    def solve_horizontal(chain, name, target, seed_q):
        target = np.asarray(target, dtype=float).reshape(3)
        seed_q = np.asarray(seed_q, dtype=float).reshape(5).copy()
        angle_deg = math.degrees(math.atan2(float(target[1]), float(target[0])))
        seed_q[0] = angle_deg
        seed_q[4] = HandledBagHorizontalMotion.HORIZONTAL_J5_DEG
        old_offset = float(HandledBagHorizontalMotion.core.AXIAL_OFFSET_MM)
        old_j5_deg = float(HandledBagHorizontalMotion.core.J5_DEG)
        old_j5_tick = int(HandledBagHorizontalMotion.core.J5_TICK)
        try:
            HandledBagHorizontalMotion.core.AXIAL_OFFSET_MM = (
                HandledBagHorizontalMotion.TCP_OFFSET_MM
            )
            HandledBagHorizontalMotion.core.J5_DEG = (
                HandledBagHorizontalMotion.HORIZONTAL_J5_DEG
            )
            HandledBagHorizontalMotion.core.J5_TICK = (
                HandledBagHorizontalMotion.HORIZONTAL_J5_TICK
            )
            solved = HandledBagHorizontalMotion.core.solve_tcp(
                chain, target, HandledBagHorizontalMotion.HORIZONTAL_TILT_DEG, seed_q
            )
        except Exception as exc:
            raise RuntimeError(
                f"{name}: horizontal IK failed | target={np.round(target, 2).tolist()} | {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            HandledBagHorizontalMotion.core.AXIAL_OFFSET_MM = old_offset
            HandledBagHorizontalMotion.core.J5_DEG = old_j5_deg
            HandledBagHorizontalMotion.core.J5_TICK = old_j5_tick
        q = np.asarray(solved["q"], dtype=float).copy()
        q[0] = angle_deg
        q[4] = HandledBagHorizontalMotion.HORIZONTAL_J5_DEG
        ticks = np.asarray(
            HandledBagHorizontalMotion.core.cfg.model_deg_to_ticks(q), dtype=np.int64
        )
        HandledBagHorizontalMotion.core.cfg.validate_arm_ticks(ticks)
        if int(ticks[4]) != HandledBagHorizontalMotion.HORIZONTAL_J5_TICK:
            raise RuntimeError(
                f"{name}: J5 changed | {ticks[4]} != {HandledBagHorizontalMotion.HORIZONTAL_J5_TICK}"
            )
        sum234 = float(q[1] + q[2] + q[3])
        if abs(sum234 - 90.0) > 0.3:
            raise RuntimeError(
                f"{name}: horizontal posture lost | J2+J3+J4={sum234:.3f}"
            )
        return {
            "name": str(name),
            "target": target.copy(),
            "q": q.copy(),
            "ticks": ticks.copy(),
            "tcp_error": float(solved["tcp_error"]),
            "sum234": sum234,
        }

    @staticmethod
    def tcp_from_q(chain, q_deg):
        state = HandledBagHorizontalMotion.core.pose_state(
            chain, np.deg2rad(np.asarray(q_deg, dtype=float))
        )
        axis = HandledBagHorizontalMotion.core.unit(state["tool_axis"])
        tcp = (
            np.asarray(state["xyz"], dtype=float)
            + HandledBagHorizontalMotion.TCP_OFFSET_MM * axis
        )
        return (tcp, axis)

    @staticmethod
    def build_pick(x_mm, y_mm):
        x = float(x_mm)
        y = float(y_mm)
        radius = math.hypot(x, y)
        if radius <= HandledBagHorizontalMotion.APPROACH_MM + 20.0:
            raise RuntimeError(f"L_paper_bag too close to base | R={radius:.1f}")
        angle_deg = math.degrees(math.atan2(y, x))
        ux = x / radius
        uy = y / radius
        grasp_x = x + HandledBagHorizontalMotion.INSERT_EXTRA_MM * ux
        grasp_y = y + HandledBagHorizontalMotion.INSERT_EXTRA_MM * uy
        ready_radius = radius - HandledBagHorizontalMotion.APPROACH_MM
        ready_x = ready_radius * ux
        ready_y = ready_radius * uy
        chain = HandledBagHorizontalMotion.core.spp.create_robot_chain()
        seed = np.array(
            [
                angle_deg,
                -10.0,
                80.0,
                20.0,
                HandledBagHorizontalMotion.HORIZONTAL_J5_DEG,
            ],
            dtype=float,
        )
        prep = HandledBagHorizontalMotion.solve_horizontal(
            chain,
            "PREP",
            [ready_x, ready_y, HandledBagHorizontalMotion.PREP_Z_MM],
            seed,
        )
        ready = HandledBagHorizontalMotion.solve_horizontal(
            chain,
            "READY",
            [ready_x, ready_y, HandledBagHorizontalMotion.GRASP_Z_MM],
            prep["q"],
        )
        approach = []
        last_q = ready["q"].copy()
        for i in range(1, HandledBagHorizontalMotion.APPROACH_SEGMENTS + 1):
            u = i / HandledBagHorizontalMotion.APPROACH_SEGMENTS
            target = np.array(
                [
                    (1.0 - u) * ready_x + u * grasp_x,
                    (1.0 - u) * ready_y + u * grasp_y,
                    HandledBagHorizontalMotion.GRASP_Z_MM,
                ],
                dtype=float,
            )
            p = HandledBagHorizontalMotion.solve_horizontal(
                chain, f"APPROACH_{i:02d}", target, last_q
            )
            approach.append(p)
            last_q = p["q"].copy()
        grasp = approach[-1]
        lift = []
        last_q = grasp["q"].copy()
        for i in range(1, HandledBagHorizontalMotion.LIFT_SEGMENTS + 1):
            u = i / HandledBagHorizontalMotion.LIFT_SEGMENTS
            z = (
                HandledBagHorizontalMotion.GRASP_Z_MM
                + u * HandledBagHorizontalMotion.LIFT_MM
            )
            p = HandledBagHorizontalMotion.solve_horizontal(
                chain, f"LIFT_{i:02d}", [grasp_x, grasp_y, z], last_q
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

    @staticmethod
    def build_handoff(lift_q, handoff):
        """
        L_paper_bag handoff:

          horizontal LIFT
            -> J1 TURN
            -> horizontal Cartesian EXTEND

        J2+J3+J4 = 90 deg throughout generated path.
        J5 = 2048 throughout.
        """
        chain = HandledBagHorizontalMotion.core.spp.create_robot_chain()
        lift_q = np.asarray(lift_q, dtype=float).reshape(5).copy()
        turn_q = lift_q.copy()
        turn_q[0] = float(handoff["angle_deg"])
        turn_q[4] = HandledBagHorizontalMotion.HORIZONTAL_J5_DEG
        turn_ticks = np.asarray(
            HandledBagHorizontalMotion.core.cfg.model_deg_to_ticks(turn_q),
            dtype=np.int64,
        )
        HandledBagHorizontalMotion.core.cfg.validate_arm_ticks(turn_ticks)
        sum234 = float(turn_q[1] + turn_q[2] + turn_q[3])
        if abs(sum234 - 90.0) > 0.3:
            raise RuntimeError(
                f"L_paper_bag TURN lost horizontal posture | sum234={sum234:.3f}"
            )
        if int(turn_ticks[4]) != HandledBagHorizontalMotion.HORIZONTAL_J5_TICK:
            raise RuntimeError("L_paper_bag TURN J5 changed")
        turn_tcp, _ = HandledBagHorizontalMotion.tcp_from_q(chain, turn_q)
        final_radius = float(handoff["handoff_radius"])
        if final_radius > HandledBagHorizontalMotion.MAX_HANDOFF_RADIUS_MM:
            raise RuntimeError(
                f"L_paper_bag handoff radius too large | {final_radius:.1f} > {HandledBagHorizontalMotion.MAX_HANDOFF_RADIUS_MM:.1f}"
            )
        az = math.radians(float(handoff["angle_deg"]))
        final_target = np.array(
            [
                final_radius * math.cos(az),
                final_radius * math.sin(az),
                HandledBagHorizontalMotion.LIFT_Z_MM,
            ],
            dtype=float,
        )
        points = []
        seed = turn_q.copy()
        for i in range(1, HandledBagHorizontalMotion.HANDOFF_SEGMENTS + 1):
            u = i / HandledBagHorizontalMotion.HANDOFF_SEGMENTS
            target = (1.0 - u) * turn_tcp + u * final_target
            p = HandledBagHorizontalMotion.solve_horizontal(
                chain, f"HANDOFF_{i:02d}", target, seed
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


# Cup pickup and handoff
class CupKinematics:
    core = BagMotion
    CUP_GRASP_Z_MM = _motion_setting("cup_geometry", "CUP_GRASP_Z_MM")
    CUP_LIFT_Z_MM = _motion_setting("cup_geometry", "CUP_LIFT_Z_MM")
    CUP_SAFE_Z_MM = _motion_setting("cup_geometry", "CUP_SAFE_Z_MM")
    APPROACH_OFFSET_MM = _motion_setting("cup_geometry", "APPROACH_OFFSET_MM")
    PULL_MM = _motion_setting("cup_geometry", "PULL_MM")
    CUP_TCP_OFFSET_MM = _motion_setting("cup_geometry", "CUP_TCP_OFFSET_MM")
    CUP_J5_DEG = _motion_setting("cup_geometry", "CUP_J5_DEG")
    CUP_J5_TICK = _motion_setting("cup_geometry", "CUP_J5_TICK")
    CUP_TILT_DEG = _motion_setting("cup_geometry", "CUP_TILT_DEG")
    TCP_ERROR_MAX_MM = _motion_setting("cup_geometry", "TCP_ERROR_MAX_MM")
    REFERENCE_X_MM = _motion_setting("cup_geometry", "REFERENCE_X_MM")
    REFERENCE_Y_MM = _motion_setting("cup_geometry", "REFERENCE_Y_MM")
    REFERENCE = {
        name: np.array(ticks, dtype=np.int64)
        for name, ticks in _motion_setting("cup_geometry", "REFERENCE").items()
    }

    @staticmethod
    def radial_data(x_mm, y_mm):
        x = float(x_mm)
        y = float(y_mm)
        if not (math.isfinite(x) and math.isfinite(y)):
            raise RuntimeError("CUP XY contains NaN/inf")
        radius = math.hypot(x, y)
        if x <= 0.0:
            raise RuntimeError(f"CUP must be in front workspace | X={x:.1f}")
        if radius <= CupKinematics.APPROACH_OFFSET_MM + 1.0:
            raise RuntimeError(f"CUP too close for D120 approach | R={radius:.1f} mm")
        ux = x / radius
        uy = y / radius
        angle_deg = math.degrees(math.atan2(y, x))
        return (radius, ux, uy, angle_deg)

    @staticmethod
    def target_at_d(x_mm, y_mm, d_mm, z_mm):
        radius, ux, uy, _ = CupKinematics.radial_data(x_mm, y_mm)
        d = float(d_mm)
        if d >= radius:
            raise RuntimeError(f"D={d:.1f} exceeds radius={radius:.1f}")
        return np.array([float(x_mm) - ux * d, float(y_mm) - uy * d, float(z_mm)])

    @staticmethod
    def cup_tcp_from_q(chain, q_deg):
        q_deg = np.asarray(q_deg, dtype=float).reshape(5)
        state = CupKinematics.core.pose_state(chain, np.deg2rad(q_deg))
        axis = CupKinematics.core.unit(state["tool_axis"])
        tcp = (
            np.asarray(state["xyz"], dtype=float)
            + CupKinematics.CUP_TCP_OFFSET_MM * axis
        )
        return (tcp, axis)

    @staticmethod
    def solve_cup_tcp(chain, name, target, seed_q, angle_deg):
        """
        Existing core.solve_tcp()를 그대로 사용하되
        CUP에서 검증했던 TCP offset 101 mm만 적용.

        계산 후:
          J1 = target radial direction
          J5 = 0 deg / 2048
        """
        target = np.asarray(target, dtype=float).reshape(3)
        seed_q = np.asarray(seed_q, dtype=float).reshape(5).copy()
        seed_q[0] = float(angle_deg)
        seed_q[4] = CupKinematics.CUP_J5_DEG
        original_offset = float(CupKinematics.core.AXIAL_OFFSET_MM)
        try:
            CupKinematics.core.AXIAL_OFFSET_MM = CupKinematics.CUP_TCP_OFFSET_MM
            try:
                solved = CupKinematics.core.solve_tcp(
                    chain, target, CupKinematics.CUP_TILT_DEG, seed_q
                )
            except Exception as e:
                raise RuntimeError(
                    f"{name}: IK solve failed | target={np.round(target, 2).tolist()} | seed_q={np.round(seed_q, 3).tolist()} | {type(e).__name__}: {e}"
                ) from e
        finally:
            CupKinematics.core.AXIAL_OFFSET_MM = original_offset
        q = np.asarray(solved["q"], dtype=float).copy()
        q[0] = float(angle_deg)
        q[4] = CupKinematics.CUP_J5_DEG
        ticks = np.asarray(CupKinematics.core.cfg.model_deg_to_ticks(q), dtype=np.int64)
        ticks[4] = CupKinematics.CUP_J5_TICK
        CupKinematics.core.cfg.validate_arm_ticks(ticks)
        tcp, axis = CupKinematics.cup_tcp_from_q(chain, q)
        error = float(np.linalg.norm(tcp - target))
        if error > CupKinematics.TCP_ERROR_MAX_MM:
            raise RuntimeError(
                f"{name}: TCP error {error:.3f} mm > {CupKinematics.TCP_ERROR_MAX_MM:.3f} mm"
            )
        if abs(float(axis[2])) > 0.03:
            raise RuntimeError(
                f"{name}: CUP tool not horizontal | axis={np.round(axis, 5).tolist()}"
            )
        _, ux, uy, _ = CupKinematics.radial_data(target[0], target[1])
        wanted_axis = np.array([ux, uy, 0.0], dtype=float)
        direction_dot = float(np.dot(axis, wanted_axis))
        if direction_dot < 0.98:
            raise RuntimeError(
                f"{name}: tool direction mismatch | dot={direction_dot:.4f} | axis={np.round(axis, 5).tolist()} | wanted={np.round(wanted_axis, 5).tolist()}"
            )
        return {
            "name": name,
            "target": target.copy(),
            "q": q,
            "ticks": ticks,
            "tcp": tcp,
            "tcp_error": error,
            "axis": axis,
        }

    @staticmethod
    def build_dynamic_cup_pick_plan(x_mm, y_mm):
        radius, ux, uy, angle_deg = CupKinematics.radial_data(x_mm, y_mm)
        chain = CupKinematics.core.spp.create_robot_chain()
        seed_q = np.asarray(
            CupKinematics.core.cfg.ticks_to_model_deg(CupKinematics.REFERENCE["SAFE"]),
            dtype=float,
        )
        seed_q[0] = angle_deg
        seed_q[4] = CupKinematics.CUP_J5_DEG
        plan = {}
        safe_target = CupKinematics.target_at_d(
            x_mm, y_mm, CupKinematics.APPROACH_OFFSET_MM, CupKinematics.CUP_SAFE_Z_MM
        )
        plan["SAFE"] = CupKinematics.solve_cup_tcp(
            chain, "SAFE", safe_target, seed_q, angle_deg
        )
        seed_q = plan["SAFE"]["q"].copy()
        descend = []
        for z in (
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
        ):
            target = CupKinematics.target_at_d(
                x_mm, y_mm, CupKinematics.APPROACH_OFFSET_MM, z
            )
            p = CupKinematics.solve_cup_tcp(
                chain, f"D120_Z{z:.0f}", target, seed_q, angle_deg
            )
            descend.append(p)
            seed_q = p["q"].copy()
        plan["DESCEND"] = descend
        approach = []
        for d in (
            100.0,
            80.0,
            60.0,
            40.0,
            35.0,
            30.0,
            25.0,
            20.0,
            15.0,
            10.0,
            5.0,
            0.0,
        ):
            target = CupKinematics.target_at_d(
                x_mm, y_mm, d, CupKinematics.CUP_GRASP_Z_MM
            )
            p = CupKinematics.solve_cup_tcp(
                chain, f"D{d:.0f}_Z105", target, seed_q, angle_deg
            )
            approach.append(p)
            seed_q = p["q"].copy()
        plan["APPROACH"] = approach
        lift = []
        for z in (125.0, 145.0, 165.0, 185.0):
            target = CupKinematics.target_at_d(x_mm, y_mm, 0.0, z)
            p = CupKinematics.solve_cup_tcp(
                chain, f"LIFT_Z{z:.0f}", target, seed_q, angle_deg
            )
            lift.append(p)
            seed_q = p["q"].copy()
        plan["LIFT"] = lift
        pull = []
        for d in (20.0, 40.0, 60.0, 80.0, 100.0):
            target = CupKinematics.target_at_d(
                x_mm, y_mm, d, CupKinematics.CUP_LIFT_Z_MM
            )
            p = CupKinematics.solve_cup_tcp(
                chain, f"PULL_D{d:.0f}", target, seed_q, angle_deg
            )
            pull.append(p)
            seed_q = p["q"].copy()
        plan["PULL"] = pull
        return {
            "x_mm": float(x_mm),
            "y_mm": float(y_mm),
            "radius_mm": float(radius),
            "angle_deg": float(angle_deg),
            "plan": plan,
        }


class CupPickMotion:
    base = CupKinematics
    APPROACH_CANDIDATES_MM = tuple(
        _motion_setting("cup_pick", "APPROACH_CANDIDATES_MM")
    )
    DESCEND_Z_MM = tuple(_motion_setting("cup_pick", "DESCEND_Z_MM"))
    LIFT_Z_MM = tuple(_motion_setting("cup_pick", "LIFT_Z_MM"))
    PULL_D_MM = tuple(_motion_setting("cup_pick", "PULL_D_MM"))
    CUP_INSERT_EXTRA_MM = _motion_setting("cup_pick", "CUP_INSERT_EXTRA_MM")
    CUP_INSERT_D_MM = -CUP_INSERT_EXTRA_MM
    CUP_INSERT_APPROACH_D_MM = (-20.0, -40.0, CUP_INSERT_D_MM)
    CUP_INSERT_RETURN_D_MM = tuple(
        _motion_setting("cup_pick", "CUP_INSERT_RETURN_D_MM")
    )
    EXTRA_PULL_D_MM = tuple(_motion_setting("cup_pick", "EXTRA_PULL_D_MM"))

    @staticmethod
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

    @staticmethod
    def _tick_margin(ticks):
        ticks = np.asarray(ticks, dtype=np.int64)
        j3 = int(ticks[2])
        j4 = int(ticks[3])
        j3_margin = min(
            j3 - int(CupPickMotion.base.core.cfg.J3_MIN_TICK),
            int(CupPickMotion.base.core.cfg.J3_MAX_TICK) - j3,
        )
        j4_margin = min(
            j4 - int(CupPickMotion.base.core.cfg.J4_MIN_TICK),
            int(CupPickMotion.base.core.cfg.J4_MAX_TICK) - j4,
        )
        return (int(j3_margin), int(j4_margin))

    @staticmethod
    def build_plan_for_d(x_mm, y_mm, approach_d_mm):
        """
        Build ONE complete CUP path using a specified approach D.

        IMPORTANT:
          Complete SAFE -> DESCEND -> APPROACH -> LIFT -> PULL
          is calculated before this function returns.

          No ROS.
          No motor command.
        """
        radius, _ux, _uy, angle_deg = CupPickMotion.base.radial_data(x_mm, y_mm)
        chain = CupPickMotion.base.core.spp.create_robot_chain()
        seed_q = np.asarray(
            CupPickMotion.base.core.cfg.ticks_to_model_deg(
                CupPickMotion.base.REFERENCE["SAFE"]
            ),
            dtype=float,
        )
        seed_q[0] = float(angle_deg)
        seed_q[4] = CupPickMotion.base.CUP_J5_DEG
        safe_target = CupPickMotion.base.target_at_d(
            x_mm, y_mm, approach_d_mm, CupPickMotion.base.CUP_SAFE_Z_MM
        )
        safe = CupPickMotion.base.solve_cup_tcp(
            chain, f"D{approach_d_mm:.0f}_Z300", safe_target, seed_q, angle_deg
        )
        seed_q = safe["q"].copy()
        descend = []
        for z_mm in CupPickMotion.DESCEND_Z_MM:
            target = CupPickMotion.base.target_at_d(x_mm, y_mm, approach_d_mm, z_mm)
            point = CupPickMotion.base.solve_cup_tcp(
                chain, f"D{approach_d_mm:.0f}_Z{z_mm:.0f}", target, seed_q, angle_deg
            )
            descend.append(point)
            seed_q = point["q"].copy()
        approach = []
        for d_mm in CupPickMotion.approach_values(approach_d_mm):
            target = CupPickMotion.base.target_at_d(
                x_mm, y_mm, d_mm, CupPickMotion.base.CUP_GRASP_Z_MM
            )
            point = CupPickMotion.base.solve_cup_tcp(
                chain, f"APPROACH_D{d_mm:.0f}", target, seed_q, angle_deg
            )
            approach.append(point)
            seed_q = point["q"].copy()
        for d_mm in CupPickMotion.CUP_INSERT_APPROACH_D_MM:
            target = CupPickMotion.base.target_at_d(
                x_mm, y_mm, d_mm, CupPickMotion.base.CUP_GRASP_Z_MM
            )
            point = CupPickMotion.base.solve_cup_tcp(
                chain, f"INSERT_D{d_mm:.0f}", target, seed_q, angle_deg
            )
            approach.append(point)
            seed_q = point["q"].copy()
        grasp = approach[-1]
        lift = []
        for z_mm in CupPickMotion.LIFT_Z_MM:
            target = CupPickMotion.base.target_at_d(
                x_mm, y_mm, CupPickMotion.CUP_INSERT_D_MM, z_mm
            )
            point = CupPickMotion.base.solve_cup_tcp(
                chain, f"LIFT_Z{z_mm:.0f}", target, seed_q, angle_deg
            )
            lift.append(point)
            seed_q = point["q"].copy()
        pull = []
        for d_mm in (*CupPickMotion.CUP_INSERT_RETURN_D_MM, *CupPickMotion.PULL_D_MM):
            target = CupPickMotion.base.target_at_d(
                x_mm, y_mm, d_mm, CupPickMotion.base.CUP_LIFT_Z_MM
            )
            point = CupPickMotion.base.solve_cup_tcp(
                chain, f"PULL_D{d_mm:.0f}", target, seed_q, angle_deg
            )
            pull.append(point)
            seed_q = point["q"].copy()
        pull100 = pull[-1]
        pull_distance_mm = 100.0
        for d_mm in CupPickMotion.EXTRA_PULL_D_MM:
            try:
                target = CupPickMotion.base.target_at_d(
                    x_mm, y_mm, d_mm, CupPickMotion.base.CUP_LIFT_Z_MM
                )
                point = CupPickMotion.base.solve_cup_tcp(
                    chain, f"PULL_D{d_mm:.0f}", target, seed_q, angle_deg
                )
            except Exception:
                break
            j3_margin, j4_margin = CupPickMotion._tick_margin(point["ticks"])
            if (
                j3_margin < CupPickMotion.SOFT_J3_MARGIN_TICK
                or j4_margin < CupPickMotion.SOFT_J4_MARGIN_TICK
            ):
                break
            pull.append(point)
            seed_q = point["q"].copy()
            pull_distance_mm = float(d_mm)
        pull_final = pull[-1]
        all_points = [safe] + descend + approach + lift + pull
        min_j3_margin = None
        min_j4_margin = None
        for point in all_points:
            ticks = np.asarray(point["ticks"], dtype=np.int64)
            CupPickMotion.base.core.cfg.validate_arm_ticks(ticks)
            if int(ticks[4]) != CupPickMotion.base.CUP_J5_TICK:
                raise RuntimeError(
                    f"CUP J5 changed | name={point['name']} tick={int(ticks[4])}"
                )
            j3_margin, j4_margin = CupPickMotion._tick_margin(ticks)
            min_j3_margin = (
                j3_margin if min_j3_margin is None else min(min_j3_margin, j3_margin)
            )
            min_j4_margin = (
                j4_margin if min_j4_margin is None else min(min_j4_margin, j4_margin)
            )
        return {
            "x_mm": float(x_mm),
            "y_mm": float(y_mm),
            "radius_mm": float(radius),
            "angle_deg": float(angle_deg),
            "approach_d_mm": float(approach_d_mm),
            "SAFE": safe,
            "DESCEND": descend,
            "APPROACH": approach,
            "GRASP": grasp,
            "LIFT": lift,
            "PULL": pull,
            "PULL100": pull100,
            "PULL_FINAL": pull_final,
            "pull_distance_mm": float(pull_distance_mm),
            "min_j3_margin_tick": int(min_j3_margin),
            "min_j4_margin_tick": int(min_j4_margin),
        }

    SOFT_J3_MARGIN_TICK = _motion_setting("cup_pick", "SOFT_J3_MARGIN_TICK")
    SOFT_J4_MARGIN_TICK = _motion_setting("cup_pick", "SOFT_J4_MARGIN_TICK")

    @staticmethod
    def build_dynamic_cup_pick_plan(x_mm, y_mm):
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
        for approach_d_mm in CupPickMotion.APPROACH_CANDIDATES_MM:
            try:
                plan = CupPickMotion.build_plan_for_d(
                    float(x_mm), float(y_mm), float(approach_d_mm)
                )
            except Exception as e:
                errors.append((approach_d_mm, "IK/HARD", str(e)))
                continue
            j3_margin = int(plan["min_j3_margin_tick"])
            j4_margin = int(plan["min_j4_margin_tick"])
            if (
                j3_margin < CupPickMotion.SOFT_J3_MARGIN_TICK
                or j4_margin < CupPickMotion.SOFT_J4_MARGIN_TICK
            ):
                errors.append(
                    (
                        approach_d_mm,
                        "SOFT",
                        f"margin too small | J3={j3_margin} (need {CupPickMotion.SOFT_J3_MARGIN_TICK}), J4={j4_margin} (need {CupPickMotion.SOFT_J4_MARGIN_TICK})",
                    )
                )
                continue
            plan["soft_j3_margin_tick"] = CupPickMotion.SOFT_J3_MARGIN_TICK
            plan["soft_j4_margin_tick"] = CupPickMotion.SOFT_J4_MARGIN_TICK
            return plan
        detail = " | ".join(
            (f"D{d:.0f} [{kind}]: {message}" for d, kind, message in errors)
        )
        raise RuntimeError(
            f"no safe dynamic CUP PICK path | XY=({float(x_mm):+.1f},{float(y_mm):+.1f}) | {detail}"
        )


class CupHandoffMotion:
    core = BagMotion
    CUP_PULL100_TICKS = np.array(
        _motion_setting("cup_handoff", "CUP_PULL100_TICKS"), dtype=np.int64
    )
    CUP_J5_TICK = _motion_setting("cup_handoff", "CUP_J5_TICK")
    CUP_J5_DEG = _motion_setting("cup_handoff", "CUP_J5_DEG")
    HANDOFF_SEGMENTS = _motion_setting("cup_handoff", "HANDOFF_SEGMENTS")
    MAX_HANDOFF_RADIUS_MM = _motion_setting("cup_handoff", "MAX_HANDOFF_RADIUS_MM")
    CUP_TILT_DEG = _motion_setting("cup_handoff", "CUP_TILT_DEG")

    @staticmethod
    def tcp_from_q(chain, q_deg):
        q_deg = np.asarray(q_deg, dtype=float).reshape(5)
        state = CupHandoffMotion.core.pose_state(chain, np.deg2rad(q_deg))
        axis = CupHandoffMotion.core.unit(state["tool_axis"])
        tcp = (
            np.asarray(state["xyz"], dtype=float)
            + CupHandoffMotion.core.AXIAL_OFFSET_MM * axis
        )
        return (tcp, axis)

    @staticmethod
    def radial_axis(angle_deg):
        a = math.radians(float(angle_deg))
        return np.array([math.cos(a), math.sin(a), 0.0])

    @staticmethod
    def build_cup_handoff(handoff, pull100_ticks=None, pull100_q=None):
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
        chain = CupHandoffMotion.core.spp.create_robot_chain()
        lower = np.asarray(CupHandoffMotion.core.cfg.JOINT_LIMIT_LOWER_DEG, dtype=float)
        upper = np.asarray(CupHandoffMotion.core.cfg.JOINT_LIMIT_UPPER_DEG, dtype=float)
        if pull100_ticks is None:
            pull_ticks = CupHandoffMotion.CUP_PULL100_TICKS.copy()
        else:
            pull_ticks = np.asarray(pull100_ticks, dtype=np.int64).reshape(5).copy()
        CupHandoffMotion.core.cfg.validate_arm_ticks(pull_ticks)
        if int(pull_ticks[4]) != CupHandoffMotion.CUP_J5_TICK:
            raise RuntimeError(f"CUP PULL100 J5 mismatch | ticks={pull_ticks.tolist()}")
        if pull100_q is None:
            pull_q = np.asarray(
                CupHandoffMotion.core.cfg.ticks_to_model_deg(pull_ticks), dtype=float
            )
        else:
            pull_q = np.asarray(pull100_q, dtype=float).reshape(5).copy()
            pull_q[4] = CupHandoffMotion.CUP_J5_DEG
            q_ticks = np.asarray(
                CupHandoffMotion.core.cfg.model_deg_to_ticks(pull_q), dtype=np.int64
            )
            q_ticks[4] = CupHandoffMotion.CUP_J5_TICK
            diff = np.abs(q_ticks - pull_ticks)
            if np.any(diff > 1):
                raise RuntimeError(
                    f"dynamic CUP PULL100 q/tick mismatch | ticks={pull_ticks.tolist()} | q_ticks={q_ticks.tolist()} | diff={diff.tolist()}"
                )
        target_angle = float(handoff["angle_deg"])
        turn_q = pull_q.copy()
        turn_q[0] = target_angle
        turn_q[4] = CupHandoffMotion.CUP_J5_DEG
        if np.any(turn_q < lower) or np.any(turn_q > upper):
            raise RuntimeError(
                f"CUP TURN joint limit exceeded | q={np.round(turn_q, 3).tolist()}"
            )
        turn_ticks = np.asarray(
            CupHandoffMotion.core.cfg.model_deg_to_ticks(turn_q), dtype=np.int64
        )
        turn_ticks[4] = CupHandoffMotion.CUP_J5_TICK
        turn_tcp, turn_axis = CupHandoffMotion.tcp_from_q(chain, turn_q)
        wanted_axis = CupHandoffMotion.radial_axis(target_angle)
        if abs(float(turn_axis[2])) > 0.03:
            raise RuntimeError(
                f"CUP TURN tool is not horizontal | axis={np.round(turn_axis, 5).tolist()}"
            )
        if float(np.dot(turn_axis, wanted_axis)) < 0.98:
            raise RuntimeError(
                f"CUP TURN tool direction mismatch | axis={np.round(turn_axis, 5).tolist()} | wanted={np.round(wanted_axis, 5).tolist()}"
            )
        final_r = float(handoff["handoff_radius"])
        if final_r > CupHandoffMotion.MAX_HANDOFF_RADIUS_MM:
            raise RuntimeError(
                f"CUP handoff radius exceeds limit | {final_r:.1f} > {CupHandoffMotion.MAX_HANDOFF_RADIUS_MM:.1f}"
            )
        final_target = np.array(
            [
                final_r * math.cos(math.radians(target_angle)),
                final_r * math.sin(math.radians(target_angle)),
                float(turn_tcp[2]),
            ]
        )
        points = []
        seed_q = turn_q.copy()
        for i in range(1, CupHandoffMotion.HANDOFF_SEGMENTS + 1):
            u = i / CupHandoffMotion.HANDOFF_SEGMENTS
            target = (1.0 - u) * turn_tcp + u * final_target
            solved = CupHandoffMotion.core.solve_tcp(
                chain, target, CupHandoffMotion.CUP_TILT_DEG, seed_q
            )
            q = np.asarray(solved["q"], dtype=float).copy()
            q[0] = target_angle
            q[4] = CupHandoffMotion.CUP_J5_DEG
            ticks = np.asarray(
                CupHandoffMotion.core.cfg.model_deg_to_ticks(q), dtype=np.int64
            )
            ticks[4] = CupHandoffMotion.CUP_J5_TICK
            if np.any(q < lower) or np.any(q > upper):
                raise RuntimeError(
                    f"CUP HANDOFF joint limit exceeded | i={i} q={np.round(q, 3).tolist()}"
                )
            tcp, axis = CupHandoffMotion.tcp_from_q(chain, q)
            error = float(np.linalg.norm(tcp - target))
            if error > 2.0:
                raise RuntimeError(
                    f"CUP HANDOFF TCP error | i={i} error={error:.3f} mm"
                )
            if abs(float(axis[2])) > 0.03:
                raise RuntimeError(
                    f"CUP HANDOFF lost horizontal tool | i={i} axis={np.round(axis, 5).tolist()}"
                )
            if float(np.dot(axis, wanted_axis)) < 0.98:
                raise RuntimeError(f"CUP HANDOFF direction mismatch | i={i}")
            if int(ticks[4]) != CupHandoffMotion.CUP_J5_TICK:
                raise RuntimeError("CUP HANDOFF J5 changed")
            points.append(
                {
                    "name": f"CUP_HANDOFF_{i}",
                    "target": target,
                    "q": q,
                    "ticks": ticks,
                    "tcp": tcp,
                    "axis": axis,
                    "error": error,
                }
            )
            seed_q = q
        return {
            "pull_q": pull_q,
            "turn_q": turn_q,
            "turn_ticks": turn_ticks,
            "turn_tcp": turn_tcp,
            "turn_axis": turn_axis,
            "final_target": final_target,
            "points": points,
        }


# Payment pose planning
class PaymentMotion:
    core = BagMotion
    compute_handoff_target = HandoffMotion.compute_handoff_target
    STANDOFF_MM = _motion_setting("payment", "STANDOFF_MM")
    MAX_PAYMENT_RADIUS_MM = _motion_setting("payment", "MAX_PAYMENT_RADIUS_MM")
    PAYMENT_Z_MM = _motion_setting("payment", "PAYMENT_Z_MM")
    PAYMENT_J4_MAX_TICK = _motion_setting("payment", "PAYMENT_J4_MAX_TICK")
    MIN_PAYMENT_RADIUS_MM = _motion_setting("payment", "MIN_PAYMENT_RADIUS_MM")
    PAYMENT_RADIUS_STEP_MM = _motion_setting("payment", "PAYMENT_RADIUS_STEP_MM")
    PAYMENT_TILT_DEG = _motion_setting("payment", "PAYMENT_TILT_DEG")
    PAYMENT_J5_DEG = _motion_setting("payment", "PAYMENT_J5_DEG")
    PAYMENT_J5_TICK = _motion_setting("payment", "PAYMENT_J5_TICK")
    PAYMENT_SEGMENTS = _motion_setting("payment", "PAYMENT_SEGMENTS")
    TOOLDOWN_TARGET_SUM_DEG = _motion_setting("payment", "TOOLDOWN_TARGET_SUM_DEG")
    TOOLDOWN_TOL_DEG = _motion_setting("payment", "TOOLDOWN_TOL_DEG")
    TCP_ERROR_LIMIT_MM = _motion_setting("payment", "TCP_ERROR_LIMIT_MM")

    @staticmethod
    def tcp_from_q(chain, q_deg):
        q = np.asarray(q_deg, dtype=float).reshape(5)
        state = PaymentMotion.core.pose_state(chain, np.deg2rad(q))
        axis = PaymentMotion.core.unit(state["tool_axis"])
        tcp = (
            np.asarray(state["xyz"], dtype=float)
            + PaymentMotion.core.AXIAL_OFFSET_MM * axis
        )
        return (tcp, axis)

    @staticmethod
    def solve_payment_tcp(chain, target, seed_q=None):
        old_j5_deg = float(PaymentMotion.core.J5_DEG)
        old_j5_tick = int(PaymentMotion.core.J5_TICK)
        try:
            PaymentMotion.core.J5_DEG = PaymentMotion.PAYMENT_J5_DEG
            PaymentMotion.core.J5_TICK = PaymentMotion.PAYMENT_J5_TICK
            solved = PaymentMotion.core.solve_tcp(
                chain,
                np.asarray(target, dtype=float),
                PaymentMotion.PAYMENT_TILT_DEG,
                seed_q,
            )
        finally:
            PaymentMotion.core.J5_DEG = old_j5_deg
            PaymentMotion.core.J5_TICK = old_j5_tick
        q = np.asarray(solved["q"], dtype=float).copy()
        ticks = np.asarray(solved["ticks"], dtype=np.int64).copy()
        q[4] = PaymentMotion.PAYMENT_J5_DEG
        ticks[4] = PaymentMotion.PAYMENT_J5_TICK
        PaymentMotion.core.cfg.validate_arm_ticks(ticks)
        if int(ticks[3]) > PaymentMotion.PAYMENT_J4_MAX_TICK:
            raise RuntimeError(
                f"PAYMENT J4 physical limit exceeded | {int(ticks[3])} > {PaymentMotion.PAYMENT_J4_MAX_TICK}"
            )
        total = float(q[1] + q[2] + q[3])
        if (
            abs(total - PaymentMotion.TOOLDOWN_TARGET_SUM_DEG)
            > PaymentMotion.TOOLDOWN_TOL_DEG
        ):
            raise RuntimeError(f"PAYMENT tool-down lost | sum234={total:.3f} deg")
        if int(ticks[4]) != PaymentMotion.PAYMENT_J5_TICK:
            raise RuntimeError(f"PAYMENT J5 changed | {ticks[4]}")
        tcp, axis = PaymentMotion.tcp_from_q(chain, q)
        if axis[2] > -0.99:
            raise RuntimeError(
                f"PAYMENT tool axis is not down | axis={np.round(axis, 5).tolist()}"
            )
        error = float(np.linalg.norm(tcp - np.asarray(target, dtype=float)))
        if error > PaymentMotion.TCP_ERROR_LIMIT_MM:
            raise RuntimeError(f"PAYMENT TCP error too large | {error:.3f} mm")
        return {
            "target": np.asarray(target, dtype=float),
            "q": q,
            "ticks": ticks,
            "tcp": tcp,
            "axis": axis,
            "sum234": total,
            "error": error,
        }

    @staticmethod
    def build_payment_plan(person_xyz):
        person = np.asarray(person_xyz, dtype=float).reshape(3)
        handoff = PaymentMotion.compute_handoff_target(
            person, PaymentMotion.MAX_PAYMENT_RADIUS_MM
        )
        chain = PaymentMotion.core.spp.create_robot_chain()
        start_ticks = np.asarray(
            PaymentMotion.core.cfg.ASSUMED_START_TICKS, dtype=np.int64
        )
        start_q = np.asarray(
            PaymentMotion.core.cfg.ticks_to_model_deg(start_ticks), dtype=float
        )
        tool_start_q = start_q.copy()
        tool_start_q[3] = (
            PaymentMotion.TOOLDOWN_TARGET_SUM_DEG - tool_start_q[1] - tool_start_q[2]
        )
        tool_start_q[4] = PaymentMotion.PAYMENT_J5_DEG
        tool_start_ticks = np.asarray(
            PaymentMotion.core.cfg.model_deg_to_ticks(tool_start_q), dtype=np.int64
        )
        tool_start_ticks[4] = PaymentMotion.PAYMENT_J5_TICK
        PaymentMotion.core.cfg.validate_arm_ticks(tool_start_ticks)
        if int(tool_start_ticks[3]) > PaymentMotion.PAYMENT_J4_MAX_TICK:
            raise RuntimeError(
                f"PAYMENT_TOOLDOWN_START J4 limit | {int(tool_start_ticks[3])} > {PaymentMotion.PAYMENT_J4_MAX_TICK}"
            )
        tool_start_tcp, tool_start_axis = PaymentMotion.tcp_from_q(chain, tool_start_q)
        tool_start_sum = float(tool_start_q[1] + tool_start_q[2] + tool_start_q[3])
        if (
            abs(tool_start_sum - PaymentMotion.TOOLDOWN_TARGET_SUM_DEG)
            > PaymentMotion.TOOLDOWN_TOL_DEG
        ):
            raise RuntimeError("PAYMENT_TOOLDOWN_START sum mismatch")
        if tool_start_axis[2] > -0.99:
            raise RuntimeError("PAYMENT_TOOLDOWN_START axis is not down")
        turn_q = tool_start_q.copy()
        turn_q[0] = float(handoff["angle_deg"])
        turn_q[4] = PaymentMotion.PAYMENT_J5_DEG
        turn_ticks = np.asarray(
            PaymentMotion.core.cfg.model_deg_to_ticks(turn_q), dtype=np.int64
        )
        turn_ticks[4] = PaymentMotion.PAYMENT_J5_TICK
        PaymentMotion.core.cfg.validate_arm_ticks(turn_ticks)
        if int(turn_ticks[3]) > PaymentMotion.PAYMENT_J4_MAX_TICK:
            raise RuntimeError(
                f"PAYMENT TURN J4 limit | {int(turn_ticks[3])} > {PaymentMotion.PAYMENT_J4_MAX_TICK}"
            )
        turn_tcp, turn_axis = PaymentMotion.tcp_from_q(chain, turn_q)
        turn_sum = float(turn_q[1] + turn_q[2] + turn_q[3])
        if (
            abs(turn_sum - PaymentMotion.TOOLDOWN_TARGET_SUM_DEG)
            > PaymentMotion.TOOLDOWN_TOL_DEG
        ):
            raise RuntimeError("PAYMENT TURN lost tool-down")
        if turn_axis[2] > -0.99:
            raise RuntimeError("PAYMENT TURN axis is not down")
        wanted_r = float(handoff["wanted_radius"])
        az = math.radians(float(handoff["angle_deg"]))
        first_r = min(wanted_r, PaymentMotion.MAX_PAYMENT_RADIUS_MM)
        if first_r < PaymentMotion.MIN_PAYMENT_RADIUS_MM:
            raise RuntimeError(
                f"person too close for PAYMENT | wanted_r={wanted_r:.1f}"
            )
        selected_r = None
        final_target = None
        points = None
        last_error = None
        candidate_r = first_r
        while candidate_r >= PaymentMotion.MIN_PAYMENT_RADIUS_MM - 1e-09:
            candidate_target = np.array(
                [
                    candidate_r * math.cos(az),
                    candidate_r * math.sin(az),
                    PaymentMotion.PAYMENT_Z_MM,
                ],
                dtype=float,
            )
            candidate_points = []
            seed_q = turn_q.copy()
            try:
                for i in range(1, PaymentMotion.PAYMENT_SEGMENTS + 1):
                    u = i / PaymentMotion.PAYMENT_SEGMENTS
                    target = (1.0 - u) * turn_tcp + u * candidate_target
                    p = PaymentMotion.solve_payment_tcp(chain, target, seed_q)
                    p["q"] = np.asarray(p["q"], dtype=float).copy()
                    p["q"][0] = float(turn_q[0])
                    p["q"][4] = PaymentMotion.PAYMENT_J5_DEG
                    p["ticks"] = np.asarray(
                        PaymentMotion.core.cfg.model_deg_to_ticks(p["q"]),
                        dtype=np.int64,
                    )
                    p["ticks"][4] = PaymentMotion.PAYMENT_J5_TICK
                    PaymentMotion.core.cfg.validate_arm_ticks(p["ticks"])
                    candidate_points.append(p)
                    seed_q = p["q"].copy()
                selected_r = float(candidate_r)
                final_target = candidate_target
                points = candidate_points
                break
            except Exception as exc:
                last_error = exc
                candidate_r -= PaymentMotion.PAYMENT_RADIUS_STEP_MM
        if selected_r is None:
            raise RuntimeError(
                f"no feasible PAYMENT tool-down path | last_error={last_error}"
            )
        actual_standoff = float(handoff["person_r"] - selected_r)
        return {
            "person": person,
            "person_radius": float(handoff["person_r"]),
            "wanted_radius": float(handoff["wanted_radius"]),
            "used_radius": selected_r,
            "standoff": actual_standoff,
            "angle_deg": float(handoff["angle_deg"]),
            "TOOLDOWN_START": {
                "q": tool_start_q,
                "ticks": tool_start_ticks,
                "tcp": tool_start_tcp,
                "sum234": tool_start_sum,
            },
            "TURN": {
                "q": turn_q,
                "ticks": turn_ticks,
                "tcp": turn_tcp,
                "sum234": turn_sum,
            },
            "FINAL_TARGET": final_target,
            "APPROACH": points,
        }


# Camera calibration
class DetectionGeometry:
    DETECTION_TOPIC = _motion_setting("detection1", "DETECTION_TOPIC")
    CAMERA_INFO_TOPIC = _motion_setting("detection1", "CAMERA_INFO_TOPIC")
    OUTPUT_TOPIC = _motion_setting("detection1", "OUTPUT_TOPIC")
    VALID_OBJECT_TYPES = set(_motion_setting("detection1", "VALID_OBJECT_TYPES"))
    R_CAM_TO_ARM = np.array(_motion_setting("detection1", "R_CAM_TO_ARM"), dtype=float)
    T_CAM_TO_ARM_MM = np.array(
        _motion_setting("detection1", "T_CAM_TO_ARM_MM"), dtype=float
    )
    TABLE_Z_MM = _motion_setting("detection1", "TABLE_Z_MM")
    PAPER_BAG_HEIGHT_MM = _motion_setting("detection1", "PAPER_BAG_HEIGHT_MM")
    L_PAPER_BAG_HEIGHT_MM = _motion_setting("detection1", "L_PAPER_BAG_HEIGHT_MM")
    CUP_SUPPORT_Z_MM = _motion_setting("detection1", "CUP_SUPPORT_Z_MM")
    CUP_HEIGHT_MM = _motion_setting("detection1", "CUP_HEIGHT_MM")
    OBJECT_CENTER_Z_MM = {
        "paper_bag": TABLE_Z_MM + 0.5 * PAPER_BAG_HEIGHT_MM,
        "L_paper_bag": TABLE_Z_MM + 0.5 * L_PAPER_BAG_HEIGHT_MM,
        "cup": CUP_SUPPORT_Z_MM + 0.5 * CUP_HEIGHT_MM,
    }


def camera_to_base(xc, yc, zc):
    xb = -zc - _motion_setting("detection2", "X_OFFSET_M")
    yb = +xc
    zb = -yc + _motion_setting("detection2", "Z_OFFSET_M")
    return (xb, yb, zb)


# Single ROS node: motion execution, orders and camera callbacks
class ArmControlNode(Node):
    """Order coordination, camera transforms and verified motion in one ROS node."""

    def _initialize_motion(self):
        super().__init__("arm_control")
        self.declare_parameter(
            "workspace_root", get_package_share_directory("arm_control")
        )
        self.workspace_root = Path(str(self.get_parameter("workspace_root").value))
        self.core = BagMotion
        self.compute_handoff_target = HandoffMotion.compute_handoff_target
        self.build_fullik_path = FullIKHandoff.build_fullik_path
        self.build_dynamic_pick_plan = PaperBagMotion.build_dynamic_pick_plan
        self.build_dynamic_cup_pick_plan = CupPickMotion.build_dynamic_cup_pick_plan
        self.make_handoff_plan = PaperBagMotion.make_handoff_plan
        self.build_l_paper_bag_plan = HandledBagMotion.build_plan
        self.l_paper_bag_turn_velocity = int(HandledBagMotion.TURN_VELOCITY)
        self.runner = HandoffMotion.Runner(self, use_motor=True)
        self.cup_close = self.runner.create_client(Trigger, "/motor/cup_gripper_close")
        self.command_lock = threading.Lock()
        self.active_pick = None
        self.service = self.create_service(ArmCommand, COMMAND_SERVICE, self.command_cb)
        self.get_logger().info(f"DriveThru Control ready | service={COMMAND_SERVICE}")
        self.get_logger().info(
            "implemented: PING, PICK/HANDOFF(paper_bag), PICK/HANDOFF(L_paper_bag), PICK/HANDOFF(cup), RETURN_HOME"
        )
        self.get_logger().info("MOVE_PAYMENT ready")

    @staticmethod
    def ok(response, code, message):
        response.success = True
        response.code = str(code)
        response.message = str(message)
        return response

    @staticmethod
    def fail(response, code, message):
        response.success = False
        response.code = str(code)
        response.message = str(message)
        return response

    def ensure_motor_services(self):
        self.runner.wait_services()

    def ensure_start_pose(self):
        self.runner.current = None
        current = self.runner.get_current()
        error = current - self.core.START_TICKS
        if np.any(np.abs(error) > self.core.START_TOL_TICKS):
            raise RuntimeError(
                f"START pose check failed | current={current.tolist()} error={error.tolist()}"
            )
        return current

    def _pick_paper_bag_at(self, x_mm, y_mm):
        radius, pick_plan = self.build_dynamic_pick_plan(float(x_mm), float(y_mm))
        paper_j5_tick = int(pick_plan["LIFT"]["ticks"][4])
        paper_j5_deg = float(pick_plan["LIFT"]["q"][4])
        pick_plan["PULL100"]["ticks"][4] = paper_j5_tick
        pick_plan["PULL100"]["q"][4] = paper_j5_deg
        handoff_plan = self.make_handoff_plan(pick_plan)
        self.ensure_motor_services()
        current = self.ensure_start_pose()
        self.get_logger().info(
            f"PICK paper_bag accepted | XY=({x_mm:+.1f},{y_mm:+.1f}) R={radius:.1f} current={current.tolist()}"
        )
        self.runner.trigger(self.runner.open, "paper_bag/OPEN")
        self.runner.move("paper_bag/READY", [pick_plan["READY"]["ticks"]])
        self.runner.move("paper_bag/GRASP", [pick_plan["GRASP"]["ticks"]])
        self.runner.trigger(self.runner.close, "paper_bag/CLOSE")
        self.runner.move("paper_bag/LIFT", [pick_plan["LIFT"]["ticks"]])
        self.runner.move("paper_bag/PULL100", [pick_plan["PULL100"]["ticks"]])
        self.active_pick = {
            "object_type": OBJ_PAPER_BAG,
            "radius": float(radius),
            "x_mm": float(x_mm),
            "y_mm": float(y_mm),
            "pick_plan": pick_plan,
            "handoff_plan": handoff_plan,
        }

    def build_lbag_horizontal_pull(self, start_q, pull_mm=100.0, segments=10):
        """
        L-paperbag 전용 horizontal PULL.

        - 현재 LIFT TCP에서 시작
        - Z 유지
        - 같은 방사방향 유지
        - 100 mm 로봇 베이스 쪽으로 당김
        - tilt = 0 deg 유지
        - J5 = 0 deg / 2048 유지
        """
        chain = self.core.spp.create_robot_chain()
        start_q = np.asarray(start_q, dtype=float).reshape(5).copy()
        state = self.core.pose_state(chain, np.deg2rad(start_q))
        tool_axis = self.core.unit(state["tool_axis"])
        start_tcp = (
            np.asarray(state["xyz"], dtype=float)
            + self.core.AXIAL_OFFSET_MM * tool_axis
        )
        if abs(float(tool_axis[2])) > 0.03:
            raise RuntimeError(
                f"L-paperbag PULL start is not horizontal | axis={tool_axis.tolist()}"
            )
        x0 = float(start_tcp[0])
        y0 = float(start_tcp[1])
        z0 = float(start_tcp[2])
        r0 = float(np.hypot(x0, y0))
        if r0 <= float(pull_mm):
            raise RuntimeError(
                f"L-paperbag PULL radius invalid | start_r={r0:.1f} pull={pull_mm:.1f}"
            )
        az = float(np.arctan2(y0, x0))
        old_j5_deg = self.core.J5_DEG
        old_j5_tick = self.core.J5_TICK
        points = []
        seed_q = start_q.copy()
        try:
            self.core.J5_DEG = 0.0
            self.core.J5_TICK = 2048
            for i in range(1, int(segments) + 1):
                u = i / float(segments)
                r = r0 - float(pull_mm) * u
                target = np.array([r * np.cos(az), r * np.sin(az), z0], dtype=float)
                solved = self.core.solve_tcp(chain, target, 0.0, seed_q)
                q = np.asarray(solved["q"], dtype=float).copy()
                ticks = np.asarray(solved["ticks"], dtype=np.int64).copy()
                total = float(q[1] + q[2] + q[3])
                if abs(total - 90.0) > 0.2:
                    raise RuntimeError(
                        f"L-paperbag PULL horizontal sum mismatch | {total:.3f}"
                    )
                if int(ticks[4]) != 2048:
                    raise RuntimeError(f"L-paperbag PULL J5 changed | {int(ticks[4])}")
                self.core.cfg.validate_arm_ticks(ticks)
                points.append({"target": target, "q": q, "ticks": ticks})
                seed_q = q
        finally:
            self.core.J5_DEG = old_j5_deg
            self.core.J5_TICK = old_j5_tick
        if not points:
            raise RuntimeError("L-paperbag PULL generated no points")
        return points

    def _lbag_v2_tcp(self, chain, q):
        q = np.asarray(q, dtype=float).reshape(5)
        state = self.core.pose_state(chain, np.deg2rad(q))
        xyz = np.asarray(state["xyz"], dtype=float)
        axis = self.core.unit(state["tool_axis"])
        tcp = xyz + float(self.core.AXIAL_OFFSET_MM) * axis
        return (tcp, axis)

    def _lbag_v2_solve(self, chain, target, seed_q=None):
        target = np.asarray(target, dtype=float).reshape(3)
        old_j5_deg = float(self.core.J5_DEG)
        old_j5_tick = int(self.core.J5_TICK)
        try:
            self.core.J5_DEG = 0.0
            self.core.J5_TICK = 2048
            result = self.core.solve_tcp(chain, target, 0.0, seed_q)
        finally:
            self.core.J5_DEG = old_j5_deg
            self.core.J5_TICK = old_j5_tick
        q = np.asarray(result["q"], dtype=float).copy()
        ticks = np.asarray(result["ticks"], dtype=np.int64).copy()
        total = float(q[1] + q[2] + q[3])
        if abs(total - 90.0) > 0.2:
            raise RuntimeError(
                f"Lbag horizontal orientation lost | J2+J3+J4={total:.3f}"
            )
        if int(ticks[4]) != 2048:
            raise RuntimeError(f"Lbag J5 changed | {int(ticks[4])}")
        self.core.cfg.validate_arm_ticks(ticks)
        tcp, axis = self._lbag_v2_tcp(chain, q)
        if abs(float(axis[2])) > 0.03:
            raise RuntimeError(f"Lbag tool is not horizontal | axis={axis.tolist()}")
        err = float(np.linalg.norm(tcp - target))
        if err > 2.0:
            raise RuntimeError(f"Lbag TCP error too large | {err:.3f} mm")
        return {
            "target": target,
            "q": q,
            "ticks": ticks,
            "tcp": tcp,
            "axis": axis,
            "error": err,
        }

    def _lbag_v2_line(self, chain, start_target, end_target, seed_q, segments):
        start_target = np.asarray(start_target, dtype=float)
        end_target = np.asarray(end_target, dtype=float)
        points = []
        seed = np.asarray(seed_q, dtype=float).copy()
        for i in range(1, int(segments) + 1):
            u = i / float(segments)
            target = (1.0 - u) * start_target + u * end_target
            solved = self._lbag_v2_solve(chain, target, seed)
            points.append(solved)
            seed = solved["q"]
        return points

    def _lbag_v2_joint_bridge(self, current_ticks, goal_ticks, segments=14):
        """
        START -> SAFE 전용.

        한 관절씩 가는 게 아니라
        J1~J5 모두 같은 progress u로 움직인다.

        각 joint는 start -> goal 사이에서만 움직이므로
        중간 overshoot가 생기지 않는다.
        """
        start = np.asarray(current_ticks, dtype=np.int64).reshape(5)
        goal = np.asarray(goal_ticks, dtype=np.int64).reshape(5)
        points = []
        for i in range(1, int(segments) + 1):
            u = i / float(segments)
            blend = 10.0 * u**3 - 15.0 * u**4 + 6.0 * u**5
            ticks = np.rint(start + blend * (goal - start)).astype(np.int64)
            self.core.cfg.validate_arm_ticks(ticks)
            points.append(ticks)
        points[-1] = goal.copy()
        return points

    def _lbag_v2_turn(self, start_q, target_angle_deg, segments=24):
        """
        PULL 완료 posture에서 제자리 회전.

        IMPORTANT:
          J1만 변경.
          J2/J3/J4/J5는 한 tick도 의도적으로 바꾸지 않는다.

        따라서 PULL한 radius 그대로 회전한다.
        """
        start_q = np.asarray(start_q, dtype=float).reshape(5).copy()
        target_angle_deg = float(target_angle_deg)
        points = []
        for i in range(1, int(segments) + 1):
            u = i / float(segments)
            blend = 10.0 * u**3 - 15.0 * u**4 + 6.0 * u**5
            q = start_q.copy()
            q[0] = start_q[0] + blend * (target_angle_deg - start_q[0])
            q[1] = start_q[1]
            q[2] = start_q[2]
            q[3] = start_q[3]
            q[4] = 0.0
            ticks = np.asarray(self.core.cfg.model_deg_to_ticks(q), dtype=np.int64)
            ticks[4] = 2048
            self.core.cfg.validate_arm_ticks(ticks)
            points.append({"q": q, "ticks": ticks})
        return points

    def do_pick_l_paper_bag(self, detected_x_mm, detected_y_mm):
        """
        L-paperbag V2 PICK

        START
          -> SAFE_HORIZONTAL
          -> DESCEND_HORIZONTAL
          -> APPROACH_HORIZONTAL
          -> CLOSE
          -> LIFT_HORIZONTAL
          -> PULL100_HORIZONTAL

        모든 물체 접근 / 운반 구간에서
        horizontal | | 유지.
        """
        x = float(detected_x_mm)
        y = float(detected_y_mm)
        detected_r = float(np.hypot(x, y))
        if detected_r < 1.0:
            raise RuntimeError("invalid Lbag detected radius")
        az = float(np.arctan2(y, x))
        ux = float(np.cos(az))
        uy = float(np.sin(az))
        INSERT_EXTRA_MM = _motion_setting(
            "arm_control.do_pick_l_paper_bag", "INSERT_EXTRA_MM"
        )
        APPROACH_MM = _motion_setting("arm_control.do_pick_l_paper_bag", "APPROACH_MM")
        GRASP_Z_MM = _motion_setting("arm_control.do_pick_l_paper_bag", "GRASP_Z_MM")
        LIFT_Z_MM = _motion_setting("arm_control.do_pick_l_paper_bag", "LIFT_Z_MM")
        SAFE_Z_MM = _motion_setting("arm_control.do_pick_l_paper_bag", "SAFE_Z_MM")
        PULL_MM = _motion_setting("arm_control.do_pick_l_paper_bag", "PULL_MM")
        grasp_r = detected_r + INSERT_EXTRA_MM
        ready_r = grasp_r - APPROACH_MM
        pull_r = grasp_r - PULL_MM
        if ready_r <= 150.0:
            raise RuntimeError(f"Lbag READY radius too small | {ready_r:.1f} mm")
        if pull_r <= 150.0:
            raise RuntimeError(f"Lbag PULL radius too small | {pull_r:.1f} mm")
        safe_target = np.array([ready_r * ux, ready_r * uy, SAFE_Z_MM], dtype=float)
        ready_target = np.array([ready_r * ux, ready_r * uy, GRASP_Z_MM], dtype=float)
        grasp_target = np.array([grasp_r * ux, grasp_r * uy, GRASP_Z_MM], dtype=float)
        lift_target = np.array([grasp_r * ux, grasp_r * uy, LIFT_Z_MM], dtype=float)
        pull_target = np.array([pull_r * ux, pull_r * uy, LIFT_Z_MM], dtype=float)
        chain = self.core.spp.create_robot_chain()
        safe = self._lbag_v2_solve(chain, safe_target, None)
        descend = self._lbag_v2_line(
            chain, safe_target, ready_target, safe["q"], segments=16
        )
        approach = self._lbag_v2_line(
            chain, ready_target, grasp_target, descend[-1]["q"], segments=20
        )
        lift = self._lbag_v2_line(
            chain, grasp_target, lift_target, approach[-1]["q"], segments=12
        )
        pull = self._lbag_v2_line(
            chain, lift_target, pull_target, lift[-1]["q"], segments=14
        )
        self.ensure_motor_services()
        current = self.ensure_start_pose()
        start_to_safe = self._lbag_v2_joint_bridge(current, safe["ticks"], segments=14)
        self.get_logger().info(
            f"PICK L_paper_bag V2 accepted | detected_R={detected_r:.1f} | ready_R={ready_r:.1f} | grasp_R={grasp_r:.1f} | pull_R={pull_r:.1f} | SAFE_Z={SAFE_Z_MM:.1f} | GRASP_Z={GRASP_Z_MM:.1f} | LIFT_Z={LIFT_Z_MM:.1f} | horizontal J5=2048"
        )
        self.runner.trigger(self.runner.open, "L_paper_bag/OPEN")
        self.move_path(
            "L_paper_bag/START_TO_SAFE_HORIZONTAL", start_to_safe, profile_velocity=12
        )
        self.move_path(
            "L_paper_bag/DESCEND_HORIZONTAL",
            [p["ticks"] for p in descend],
            profile_velocity=12,
        )
        self.move_path(
            "L_paper_bag/APPROACH_HORIZONTAL",
            [p["ticks"] for p in approach],
            profile_velocity=12,
        )
        self.runner.trigger(self.runner.close, "L_paper_bag/CLOSE")
        self.move_path(
            "L_paper_bag/LIFT_HORIZONTAL",
            [p["ticks"] for p in lift],
            profile_velocity=12,
        )
        self.move_path(
            "L_paper_bag/PULL150_HORIZONTAL",
            [p["ticks"] for p in pull],
            profile_velocity=12,
        )
        self.active_pick = {
            "object_type": OBJ_L_PAPER_BAG,
            "detected_x_mm": x,
            "detected_y_mm": y,
            "lbag_pull_q": pull[-1]["q"].copy(),
            "lbag_pull_ticks": pull[-1]["ticks"].copy(),
            "lbag_pull_tcp": pull[-1]["tcp"].copy(),
            "lbag_pull_radius": float(np.hypot(pull[-1]["tcp"][0], pull[-1]["tcp"][1])),
            "lbag_carry_z": float(pull[-1]["tcp"][2]),
        }

    def do_handoff_l_paper_bag(self, person_x_mm, person_y_mm, person_z_mm):
        """
        L-paperbag V2 HANDOFF

        PULL100 endpoint
          -> TURN_IN_PLACE
          -> EXTEND_HORIZONTAL
          -> OPEN

        TURN:
          J1 only changes.
          J2/J3/J4/J5 exactly held.

        Therefore the arm NEVER moves back outward before rotation.
        """
        if self.active_pick is None:
            raise RuntimeError("Lbag HANDOFF without PICK")
        if self.active_pick.get("object_type") != OBJ_L_PAPER_BAG:
            raise RuntimeError("active object is not L_paper_bag")
        person = np.array(
            [float(person_x_mm), float(person_y_mm), float(person_z_mm)], dtype=float
        )
        handoff = self.compute_handoff_target(person, 600.0)
        angle_deg = float(handoff["angle_deg"])
        final_r = float(handoff["handoff_radius"])
        pull_q = np.asarray(self.active_pick["lbag_pull_q"], dtype=float).copy()
        pull_tcp = np.asarray(self.active_pick["lbag_pull_tcp"], dtype=float).copy()
        pull_r = float(np.hypot(pull_tcp[0], pull_tcp[1]))
        carry_z = float(self.active_pick["lbag_carry_z"])
        if final_r <= pull_r + 10.0:
            raise RuntimeError(
                f"Lbag HANDOFF has no outward EXTEND room | pull_R={pull_r:.1f} target_R={final_r:.1f}. Driver position is too close for the requested pull->turn->extend motion."
            )
        turn = self._lbag_v2_turn(pull_q, angle_deg, segments=24)
        turn_q = turn[-1]["q"].copy()
        az = float(np.deg2rad(angle_deg))
        turn_tcp_target = np.array(
            [pull_r * np.cos(az), pull_r * np.sin(az), carry_z], dtype=float
        )
        final_target = np.array(
            [final_r * np.cos(az), final_r * np.sin(az), carry_z], dtype=float
        )
        chain = self.core.spp.create_robot_chain()
        extend = self._lbag_v2_line(
            chain, turn_tcp_target, final_target, turn_q, segments=20
        )
        self.get_logger().info(
            f"L_paper_bag HANDOFF V2 accepted | angle={angle_deg:+.2f} deg | pull_R={pull_r:.1f} -> extend_R={final_r:.1f} mm | extend={final_r - pull_r:.1f} mm | Z={carry_z:.1f} | horizontal | | / J5=2048"
        )
        self.ensure_motor_services()
        self.move_path(
            "L_paper_bag/TURN_IN_PLACE_HORIZONTAL",
            [p["ticks"] for p in turn],
            profile_velocity=12,
        )
        self.move_path(
            "L_paper_bag/EXTEND_HORIZONTAL",
            [p["ticks"] for p in extend],
            profile_velocity=12,
        )
        self.runner.trigger(self.runner.open, "L_paper_bag/OPEN_HANDOVER")

    def do_handoff_paper_bag(self, person_x_mm, person_y_mm, person_z_mm):
        if (
            self.active_pick is not None
            and self.active_pick.get("object_type") == OBJ_L_PAPER_BAG
        ):
            return self.do_handoff_l_paper_bag(person_x_mm, person_y_mm, person_z_mm)
        if self.active_pick is None:
            raise RuntimeError("HANDOFF requested without an active PICK")
        if self.active_pick["object_type"] not in (OBJ_PAPER_BAG, OBJ_L_PAPER_BAG):
            raise RuntimeError("active object is not a supported bag")
        bag_type = self.active_pick["object_type"]
        person = np.array(
            [float(person_x_mm), float(person_y_mm), float(person_z_mm)], dtype=float
        )
        handoff = self.compute_handoff_target(person, 400.0)
        self.ensure_motor_services()
        if bag_type == OBJ_L_PAPER_BAG:
            build_lbag_handoff = HandledBagHorizontalMotion.build_handoff
            motion = build_lbag_handoff(self.active_pick["lbag_lift_q"], handoff)
            self.core.cfg.validate_arm_ticks(motion["turn_ticks"])
            for point in motion["points"]:
                self.core.cfg.validate_arm_ticks(point["ticks"])
            self.get_logger().info(
                f"L_paper_bag HANDOFF accepted | angle={handoff['angle_deg']:+.2f} deg | radius={handoff['handoff_radius']:.1f} mm | horizontal | | maintained"
            )
            self.runner.move("L_paper_bag/HORIZONTAL_TURN", [motion["turn_ticks"]])
            self.runner.move(
                "L_paper_bag/HORIZONTAL_HANDOFF",
                [point["ticks"] for point in motion["points"]],
            )
            self.runner.trigger(self.runner.open, "L_paper_bag/OPEN_HANDOVER")
            return
        try:
            motion = self.build_fullik_path(
                self.active_pick["handoff_plan"], handoff, joint_blend=False
            )
        except TypeError:
            motion = self.build_fullik_path(self.active_pick["handoff_plan"], handoff)
        self.runner.move("paper_bag/FULLIK_TURN", [motion["turn_ticks"]])
        held_j5_tick = int(self.active_pick["pick_plan"]["PULL100"]["ticks"][4])
        held_j5_deg = float(self.active_pick["pick_plan"]["PULL100"]["q"][4])
        motion["turn_ticks"][4] = held_j5_tick
        if "turn_q" in motion:
            motion["turn_q"][4] = held_j5_deg
        for point in motion["points"]:
            point["ticks"][4] = held_j5_tick
            if "q" in point:
                point["q"][4] = held_j5_deg
            self.core.cfg.validate_arm_ticks(point["ticks"])
        self.core.cfg.validate_arm_ticks(motion["turn_ticks"])
        self.move_path(
            "paper_bag/FULLIK_HANDOFF_SMOOTH",
            [point["ticks"] for point in motion["points"]],
            profile_velocity=12,
        )
        self.runner.trigger(self.runner.open, "paper_bag/OPEN_HANDOVER")

    def do_move_payment(self, person_x_mm, person_y_mm, person_z_mm):
        """
        NFC PAYMENT pose.

        START
          -> TOOL_DOWN_START
          -> driver-direction TURN + TOOL-DOWN APPROACH
             (one continuous MoveTickPath)

        Payment planner policy:
          target standoff = 300 mm
          infeasible/far target -> farthest feasible radius
          PAYMENT Z = 100 mm
          J2 + J3 + J4 = 180 deg
          J5 = 2048
        """
        build_payment_plan = PaymentMotion.build_payment_plan
        person = np.array(
            [float(person_x_mm), float(person_y_mm), float(person_z_mm)], dtype=float
        )
        plan = build_payment_plan(person)
        self.ensure_motor_services()
        current = self.ensure_start_pose()
        self.get_logger().info(
            f"MOVE_PAYMENT accepted | person=({person[0]:+.1f},{person[1]:+.1f},{person[2]:+.1f}) | angle={plan['angle_deg']:+.2f} deg | wanted_R={plan['wanted_radius']:.1f} | used_R={plan['used_radius']:.1f} | standoff={plan['standoff']:.1f} | current={current.tolist()}"
        )
        self.runner.move("payment/TOOLDOWN_START", [plan["TOOLDOWN_START"]["ticks"]])
        turn_approach_path = [plan["TURN"]["ticks"]]
        turn_approach_path.extend((p["ticks"] for p in plan["APPROACH"]))
        self.runner.move("payment/TOOLDOWN_TURN_APPROACH", turn_approach_path)

    def do_return_home(self):
        self.ensure_motor_services()
        object_type = (
            self.active_pick["object_type"] if self.active_pick is not None else "arm"
        )
        self.get_logger().info(
            f"RETURN_HOME start | object={object_type} goal={self.core.START_TICKS.tolist()}"
        )
        self.runner.move(f"{object_type}/RETURN_HOME", [self.core.START_TICKS])
        current = self.ensure_start_pose()
        self.get_logger().info(f"RETURN_HOME verified | current={current.tolist()}")
        self.active_pick = None

    def _load_cup_verified_paths(self):
        """
        Load the archived Z105 CUP dense path.

        The archived dense path was checked against the
        current Motor Control smoother and the worst
        geometric difference was 0.169 mm.

        Fixed TURN180 is intentionally NOT returned here.
        Actual handoff uses the frozen driver pose.
        """
        import json

        path = self.workspace_root / "config" / "cup_z105_FULL_VERIFIED.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("name") != "CUP_Z105_FULL_VERIFIED":
            raise RuntimeError("unexpected CUP plan name")
        pickup = {}
        for phase in data["pickup"]["phases"]:
            name = str(phase["name"])
            commands = np.asarray(phase["commands"], dtype=np.int64).reshape(-1, 5)
            pickup[name] = commands
        required = (
            "START_TO_SAFE",
            "DESCEND_D120_Z300_TO_Z105",
            "APPROACH_Z105_D120_TO_D0",
            "LIFT_D0_Z105_TO_Z185",
        )
        missing = [name for name in required if name not in pickup]
        if missing:
            raise RuntimeError(f"CUP pickup phases missing: {missing}")
        pull = np.asarray(data["pull"]["commands"], dtype=np.int64).reshape(-1, 5)
        paths = {**pickup, "PULL20_TO_PULL100": pull}
        expected_goals = {
            "START_TO_SAFE": np.array([2871, 798, 8328, 1975, 2048], dtype=np.int64),
            "DESCEND_D120_Z300_TO_Z105": np.array(
                [2871, 2237, 10132, 1326, 2048], dtype=np.int64
            ),
            "APPROACH_Z105_D120_TO_D0": np.array(
                [2871, 3472, 8416, 1422, 2048], dtype=np.int64
            ),
            "LIFT_D0_Z105_TO_Z185": np.array(
                [2871, 2796, 8047, 1632, 2048], dtype=np.int64
            ),
            "PULL20_TO_PULL100": np.array(
                [2871, 1506, 9384, 1622, 2048], dtype=np.int64
            ),
        }
        for name, commands in paths.items():
            if len(commands) == 0:
                raise RuntimeError(f"{name}: empty CUP path")
            if not np.all(commands[:, 4] == 2048):
                raise RuntimeError(f"{name}: CUP J5 changed")
            if not np.array_equal(commands[-1], expected_goals[name]):
                raise RuntimeError(
                    f"{name}: final tick mismatch | got={commands[-1].tolist()} | expected={expected_goals[name].tolist()}"
                )
            for ticks in commands:
                self.core.cfg.validate_arm_ticks(ticks)
        return paths

    def ensure_cup_close_service(self):
        if not self.cup_close.wait_for_service(timeout_sec=5.0):
            raise RuntimeError("service unavailable: /motor/cup_gripper_close")

    def do_pick_cup(self, detected_x_mm, detected_y_mm):
        """
        Dynamic CUP PICK.

        Camera/detection supplies base-frame X/Y.
        CUP grasp Z remains fixed at 105 mm.

        Complete path is calculated and validated
        before any motor request is sent.
        """
        plan = self.build_dynamic_cup_pick_plan(
            float(detected_x_mm), float(detected_y_mm)
        )
        groups = [
            [plan["SAFE"]],
            plan["DESCEND"],
            plan["APPROACH"],
            plan["LIFT"],
            plan["PULL"],
        ]
        for group in groups:
            for point in group:
                ticks = np.asarray(point["ticks"], dtype=np.int64)
                self.core.cfg.validate_arm_ticks(ticks)
                if int(ticks[4]) != 2048:
                    raise RuntimeError(
                        f"dynamic CUP PICK J5 changed | name={point['name']} | ticks={ticks.tolist()}"
                    )
        transport_ticks = np.asarray(plan["PULL_FINAL"]["ticks"], dtype=np.int64).copy()
        transport_q = np.asarray(plan["PULL_FINAL"]["q"], dtype=float).copy()
        self.ensure_motor_services()
        self.ensure_cup_close_service()
        current = self.ensure_start_pose()
        self.get_logger().info(
            f"PICK cup accepted | XY=({detected_x_mm:+.1f},{detected_y_mm:+.1f}) | R={plan['radius_mm']:.1f} | angle={plan['angle_deg']:+.2f} | D={plan['approach_d_mm']:.0f} | PULL={plan['pull_distance_mm']:.0f} mm | J3margin={plan['min_j3_margin_tick']} | J4margin={plan['min_j4_margin_tick']} | current={current.tolist()}"
        )
        level_start_q = np.asarray(
            self.core.cfg.ticks_to_model_deg(self.core.START_TICKS), dtype=float
        )
        level_start_q[3] = 90.0 - float(level_start_q[1]) - float(level_start_q[2])
        level_start_q[4] = 0.0
        level_start_ticks = np.asarray(
            self.core.cfg.model_deg_to_ticks(level_start_q), dtype=np.int64
        )
        level_start_ticks[4] = 2048
        self.core.cfg.validate_arm_ticks(level_start_ticks)
        self.runner.trigger(self.runner.open, "cup/OPEN")
        self.runner.move("cup/LEVEL_START", [level_start_ticks])
        self.runner.move("cup/DYNAMIC_SAFE", [plan["SAFE"]["ticks"]])
        self.runner.move(
            "cup/DYNAMIC_DESCEND", [point["ticks"] for point in plan["DESCEND"]]
        )
        self.runner.move(
            "cup/DYNAMIC_APPROACH", [point["ticks"] for point in plan["APPROACH"]]
        )
        self.runner.trigger(self.cup_close, "cup/CLOSE")
        self.runner.move("cup/DYNAMIC_LIFT", [point["ticks"] for point in plan["LIFT"]])
        self.runner.move("cup/DYNAMIC_PULL", [point["ticks"] for point in plan["PULL"]])
        self.active_pick = {
            "object_type": OBJ_CUP,
            "detected_x_mm": float(detected_x_mm),
            "detected_y_mm": float(detected_y_mm),
            "approach_d_mm": float(plan["approach_d_mm"]),
            "transport_ticks": transport_ticks,
            "transport_q": transport_q,
            "pull_distance_mm": float(plan["pull_distance_mm"]),
            "pick_plan": plan,
        }

    def do_handoff_cup(self, person_x_mm, person_y_mm, person_z_mm):
        """
        Dynamic CUP HANDOFF.

        actual PICK PULL100
          -> driver direction TURN
          -> horizontal Cartesian HANDOFF
          -> OPEN

        RETURN_HOME remains Main's responsibility.
        """
        if self.active_pick is None:
            raise RuntimeError("CUP HANDOFF requested without active PICK")
        if self.active_pick["object_type"] != OBJ_CUP:
            raise RuntimeError("active object is not cup")
        MAX_HANDOFF_RADIUS_MM = CupHandoffMotion.MAX_HANDOFF_RADIUS_MM
        build_cup_handoff = CupHandoffMotion.build_cup_handoff
        person = np.array(
            [float(person_x_mm), float(person_y_mm), float(person_z_mm)], dtype=float
        )
        handoff = self.compute_handoff_target(person, float(MAX_HANDOFF_RADIUS_MM))
        transport_ticks = np.asarray(
            self.active_pick["transport_ticks"], dtype=np.int64
        ).copy()
        transport_q = np.asarray(self.active_pick["transport_q"], dtype=float).copy()
        motion = build_cup_handoff(
            handoff, pull100_ticks=transport_ticks, pull100_q=transport_q
        )
        self.core.cfg.validate_arm_ticks(motion["turn_ticks"])
        if int(motion["turn_ticks"][4]) != 2048:
            raise RuntimeError("CUP HANDOFF TURN J5 changed")
        for point in motion["points"]:
            self.core.cfg.validate_arm_ticks(point["ticks"])
            if int(point["ticks"][4]) != 2048:
                raise RuntimeError("CUP HANDOFF J5 changed")
        self.get_logger().info(
            f"CUP HANDOFF accepted | pick_XY=({self.active_pick['detected_x_mm']:+.1f},{self.active_pick['detected_y_mm']:+.1f}) | PULL={self.active_pick['pull_distance_mm']:.0f} mm | person=({person[0]:+.1f},{person[1]:+.1f},{person[2]:+.1f}) | angle={handoff['angle_deg']:+.2f} | radius={handoff['handoff_radius']:.1f}"
        )
        self.ensure_motor_services()
        self.runner.move("cup/DYNAMIC_TURN", [motion["turn_ticks"]])
        self.runner.move(
            "cup/HORIZONTAL_HANDOFF", [point["ticks"] for point in motion["points"]]
        )
        self.runner.trigger(self.runner.open, "cup/OPEN_HANDOVER")

    def command_cb(self, request, response):
        command = str(request.command).strip().upper()
        object_type = str(request.object_type).strip()
        if not self.command_lock.acquire(blocking=False):
            return self.fail(
                response, "BUSY", "Control is already executing another arm command"
            )
        try:
            self.get_logger().info(
                f"command | {command} object={object_type!r} xyz=({request.x_mm:.1f},{request.y_mm:.1f},{request.z_mm:.1f})"
            )
            if command == CMD_PING:
                return self.ok(response, "OK", "drive_thru_control alive")
            if command == CMD_MOVE_PAYMENT:
                self.do_move_payment(request.x_mm, request.y_mm, request.z_mm)
                return self.ok(
                    response, "PAYMENT_POSE_REACHED", "NFC payment pose reached"
                )
            if command == CMD_PICK:
                if object_type == OBJ_PAPER_BAG:
                    self.do_pick_paper_bag(request.x_mm, request.y_mm)
                    return self.ok(
                        response, "PICK_DONE", "paper_bag PICK + PULL100 complete"
                    )
                if object_type == OBJ_L_PAPER_BAG:
                    self.do_pick_l_paper_bag(request.x_mm, request.y_mm)
                    return self.ok(
                        response, "PICK_DONE", "L_paper_bag PICK + PULL150 complete"
                    )
                if object_type == OBJ_CUP:
                    self.do_pick_cup(request.x_mm, request.y_mm)
                    return self.ok(response, "OK", "cup PICK + PULL100 complete")
                return self.fail(
                    response,
                    "UNKNOWN_OBJECT",
                    f"unsupported object_type={object_type!r}",
                )
            if command == CMD_HANDOFF:
                if (
                    self.active_pick is not None
                    and self.active_pick.get("object_type") == OBJ_CUP
                ):
                    self.do_handoff_cup(request.x_mm, request.y_mm, request.z_mm)
                    return self.ok(response, "OK", "cup HANDOFF + open complete")
                active_type = object_type or (
                    self.active_pick["object_type"]
                    if self.active_pick is not None
                    else ""
                )
                if active_type in (OBJ_PAPER_BAG, OBJ_L_PAPER_BAG):
                    self.do_handoff_paper_bag(request.x_mm, request.y_mm, request.z_mm)
                    return self.ok(
                        response,
                        "HANDOFF_DONE",
                        f"{active_type} pose-based handoff + gripper open complete",
                    )
                return self.fail(
                    response,
                    "NOT_IMPLEMENTED",
                    f"HANDOFF for {active_type!r} not implemented",
                )
            if command == CMD_RETURN_HOME:
                self.do_return_home()
                return self.ok(response, "HOME_DONE", "arm returned to START")
            return self.fail(
                response, "UNKNOWN_COMMAND", f"unsupported command={command!r}"
            )
        except Exception as e:
            self.get_logger().error(f"{command} failed: {type(e).__name__}: {e}")
            return self.fail(response, "EXECUTION_ERROR", f"{type(e).__name__}: {e}")
        finally:
            self.command_lock.release()

    def close(self):
        try:
            self.runner.destroy_node()
        except Exception:
            pass

    def __init__(self) -> None:
        self._initialize_motion()
        self.declare_parameter("handoff_open_delay_sec", 5.0)
        self.declare_parameter("paper_bag_y_offset_mm", 25.0)
        self.handoff_open_delay_sec = float(
            self.get_parameter("handoff_open_delay_sec").value
        )
        self.paper_bag_y_offset_mm = float(
            self.get_parameter("paper_bag_y_offset_mm").value
        )
        _original_runner_trigger = self.runner.trigger

        def _trigger_with_handoff_delay(client, label):
            if str(label).endswith("/OPEN_HANDOVER"):
                self.get_logger().info(
                    f"{label} | waiting {self.handoff_open_delay_sec:.1f} sec before gripper OPEN"
                )
                time.sleep(self.handoff_open_delay_sec)
            return _original_runner_trigger(client, label)

        self.runner.trigger = _trigger_with_handoff_delay
        self.declare_parameter("driver_window", 5)
        self.declare_parameter("driver_min_samples", 3)
        self.declare_parameter("driver_inlier_radius_mm", 150.0)
        self.declare_parameter("driver_max_spread_mm", 120.0)
        self.declare_parameter("item_window", 5)
        self.declare_parameter("item_min_samples", 3)
        self.declare_parameter("item_max_spread_mm", 35.0)
        self.declare_parameter("item_min_confidence", 0.5)
        self.declare_parameter("item_wait_timeout_sec", 0.0)
        self.declare_parameter("done_service_wait_sec", 5.0)
        gp = lambda name: self.get_parameter(name).value
        self.driver_window = int(gp("driver_window"))
        self.driver_min_samples = int(gp("driver_min_samples"))
        self.driver_inlier_radius_mm = float(gp("driver_inlier_radius_mm"))
        self.driver_max_spread_mm = float(gp("driver_max_spread_mm"))
        self.item_window = int(gp("item_window"))
        self.item_min_samples = int(gp("item_min_samples"))
        self.item_max_spread_mm = float(gp("item_max_spread_mm"))
        self.item_min_confidence = float(gp("item_min_confidence"))
        self.item_wait_timeout_sec = float(gp("item_wait_timeout_sec"))
        self.done_service_wait_sec = float(gp("done_service_wait_sec"))
        self.state_lock = threading.RLock()
        self.sequence_lock = threading.Lock()
        self.current_order = None
        self.driver_samples = deque(maxlen=self.driver_window)
        self.item_samples = deque(maxlen=self.item_window)
        self.locked_driver_pose_mm = None
        self.locked_item = None
        self.driver_notified = False
        self.driver_notify_pending = False
        self.create_subscription(String, "/arm/order", self.order_cb, 10)
        self.pay_service = self.create_service(Trigger, "/arm/pay", self.pay_cb)
        self.deliver_service = self.create_service(
            Trigger, "/arm/deliver", self.deliver_cb
        )
        self.arm_done_client = self.create_client(Trigger, "/arm/done")
        self.driver_detected_client = self.create_client(Trigger, "/driver_detected")
        self._initialize_detection()
        self._initialize_driver_pose()
        self.notify_timer = self.create_timer(0.2, self.maybe_notify_driver)
        self.get_logger().info(
            "Arm Control coordinator ready | Main=/arm/order,/arm/pay,/arm/deliver,/arm/done | D1=/drive_thru/detected_item | D2=/driver_pose/target_base"
        )
        self.get_logger().info(
            "Motion planners and runner are integrated in arm_control_node.py"
        )

    def do_pick_paper_bag(self, x_mm, y_mm):
        detected_x = float(x_mm)
        detected_y = float(y_mm)
        pick_x = detected_x
        pick_y = detected_y + self.paper_bag_y_offset_mm
        self.get_logger().info(
            f"paper_bag PICK offset | detected=({detected_x:+.1f},{detected_y:+.1f}) -> command=({pick_x:+.1f},{pick_y:+.1f}) | Y_OFFSET={self.paper_bag_y_offset_mm:+.1f} mm"
        )
        return self._pick_paper_bag_at(pick_x, pick_y)

    def move_path(self, label, ticks_list, profile_velocity=0):
        arr = np.asarray(ticks_list, dtype=np.int64).reshape(-1, 5)
        req = MoveTickPath.Request()
        req.joint_ticks = [int(v) for v in arr.reshape(-1)]
        req.point_count = int(len(arr))
        req.label = str(label)
        req.profile_velocity = int(profile_velocity)
        req.timeout_sec = float(self.core.PATH_TIMEOUT_SEC)
        future = self.runner.path.call_async(req)
        self.runner._wait_future(future)
        res = future.result()
        if res is None or not res.success:
            raise RuntimeError(
                f"{label} failed: {getattr(res, 'message', 'no response')}"
            )
        self.get_logger().info(f"{label} reached | ticks={list(res.reached_ticks)}")

    def order_cb(self, msg: String) -> None:
        try:
            raw = json.loads(msg.data)
            order = {
                "order_no": int(raw["order_no"]),
                "menu": str(raw["menu"]),
                "is_mcorder": bool(raw["is_mcorder"]),
                "price": raw["price"],
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            self.get_logger().error(f"/arm/order rejected | {msg.data!r} | {exc!r}")
            return
        with self.state_lock:
            self.current_order = order
            self.driver_samples.clear()
            self.item_samples.clear()
            self.locked_driver_pose_mm = None
            self.locked_item = None
            self.driver_notified = False
            self.driver_notify_pending = False
        self.get_logger().info(
            f"ORDER LOCKED | order={order['order_no']} | mcorder={order['is_mcorder']} | menu={order['menu']!r}"
        )

    def driver_pose_cb(self, msg: PointStamped) -> None:
        p = np.array(
            [
                float(msg.point.x) * 1000.0,
                float(msg.point.y) * 1000.0,
                float(msg.point.z) * 1000.0,
            ],
            dtype=float,
        )
        if not np.isfinite(p).all():
            return
        with self.state_lock:
            if self.current_order is None:
                return
            if self.locked_driver_pose_mm is not None:
                return
            self.driver_samples.append(p)
            locked = self.try_lock_driver()
            if locked is not None:
                self.locked_driver_pose_mm = locked
                self.get_logger().info(
                    "DRIVER LOCKED | XYZ=["
                    + ", ".join((f"{v:.1f}" for v in locked))
                    + "] mm"
                )

    def try_lock_driver(self):
        if len(self.driver_samples) < self.driver_min_samples:
            return None
        a = np.asarray(self.driver_samples, dtype=float)
        median = np.median(a, axis=0)
        distances = np.linalg.norm(a[:, :2] - median[:2], axis=1)
        inliers = a[distances <= self.driver_inlier_radius_mm]
        if len(inliers) < self.driver_min_samples:
            return None
        filtered = np.median(inliers, axis=0)
        spread = float(np.max(np.linalg.norm(inliers[:, :2] - filtered[:2], axis=1)))
        if spread > self.driver_max_spread_mm:
            return None
        return filtered.copy()

    def maybe_notify_driver(self) -> None:
        with self.state_lock:
            if self.current_order is None:
                return
            if self.locked_driver_pose_mm is None:
                return
            if self.driver_notified or self.driver_notify_pending:
                return
            if not self.driver_detected_client.service_is_ready():
                return
            self.driver_notify_pending = True
        future = self.driver_detected_client.call_async(Trigger.Request())
        future.add_done_callback(self.driver_detected_response_cb)

    def driver_detected_response_cb(self, future) -> None:
        try:
            response = future.result()
            ok = bool(response.success)
            message = str(response.message)
        except Exception as exc:
            ok = False
            message = repr(exc)
        with self.state_lock:
            self.driver_notify_pending = False
            if ok:
                self.driver_notified = True
        if ok:
            self.get_logger().info("Main /driver_detected accepted")
        else:
            self.get_logger().debug(
                f"Main /driver_detected not accepted yet: {message}"
            )

    def item_cb(self, msg: DetectedItem) -> None:
        with self.state_lock:
            if self.current_order is None:
                return
            expected = str(self.current_order["order_no"])
            order_number = str(msg.order_number).strip()
            if order_number != expected:
                return
            confidence = float(msg.confidence)
            if not math.isfinite(confidence) or confidence < self.item_min_confidence:
                return
            object_type = str(msg.object_type).strip()
            if object_type not in VALID_OBJECTS:
                return
            x_mm = float(msg.x_mm)
            y_mm = float(msg.y_mm)
            if not np.isfinite([x_mm, y_mm]).all():
                return
            if self.locked_item is not None:
                return
            self.item_samples.append(
                {
                    "object_type": object_type,
                    "x_mm": x_mm,
                    "y_mm": y_mm,
                    "confidence": confidence,
                }
            )
            locked = self.try_lock_item()
            if locked is not None:
                locked["order_number"] = order_number
                self.locked_item = locked
                self.get_logger().info(
                    "ITEM LOCKED | " + json.dumps(locked, ensure_ascii=False)
                )

    def try_lock_item(self):
        if len(self.item_samples) < self.item_min_samples:
            return None
        samples = list(self.item_samples)
        type_counts = Counter((s["object_type"] for s in samples))
        object_type, _ = type_counts.most_common(1)[0]
        same_type = [s for s in samples if s["object_type"] == object_type]
        if len(same_type) < self.item_min_samples:
            return None
        xy = np.array([[s["x_mm"], s["y_mm"]] for s in same_type], dtype=float)
        median_xy = np.median(xy, axis=0)
        distances = np.linalg.norm(xy - median_xy, axis=1)
        inlier_samples = [
            s for s, ok in zip(same_type, distances <= self.item_max_spread_mm) if ok
        ]
        if len(inlier_samples) < self.item_min_samples:
            return None
        inlier_xy = np.array(
            [[s["x_mm"], s["y_mm"]] for s in inlier_samples], dtype=float
        )
        final_xy = np.median(inlier_xy, axis=0)
        spread = float(np.max(np.linalg.norm(inlier_xy - final_xy, axis=1)))
        if spread > self.item_max_spread_mm:
            return None
        confidence = float(np.median([s["confidence"] for s in inlier_samples]))
        return {
            "object_type": object_type,
            "x_mm": float(final_xy[0]),
            "y_mm": float(final_xy[1]),
            "confidence": confidence,
            "spread_mm": spread,
        }

    def run_legacy_command(
        self,
        command: str,
        object_type: str = "",
        x_mm: float = 0.0,
        y_mm: float = 0.0,
        z_mm: float = 0.0,
    ) -> None:
        """
        Run the command handler directly within this node.

        This is intentional: all object-specific motion sequencing, IK,
        path validation, CUP synchronization, payment motion and HOME
        behavior are implemented in this node.
        """
        request = ArmCommand.Request()
        request.command = str(command)
        request.object_type = str(object_type)
        request.x_mm = float(x_mm)
        request.y_mm = float(y_mm)
        request.z_mm = float(z_mm)
        response = ArmCommand.Response()
        result = self.command_cb(request, response)
        if not result.success:
            raise RuntimeError(f"{command} failed | {result.code} | {result.message}")

    def pay_cb(
        self, request: Trigger.Request, response: Trigger.Response
    ) -> Trigger.Response:
        with self.state_lock:
            if self.current_order is None:
                response.success = False
                response.message = "no current /arm/order"
                return response
            if self.locked_driver_pose_mm is None:
                response.success = False
                response.message = "driver pose not locked"
                return response
        if not self.sequence_lock.acquire(blocking=False):
            response.success = False
            response.message = "arm sequence busy"
            return response
        threading.Thread(target=self.run_pay_sequence, daemon=True).start()
        response.success = True
        response.message = "MOVE_PAYMENT accepted"
        return response

    def deliver_cb(
        self, request: Trigger.Request, response: Trigger.Response
    ) -> Trigger.Response:
        with self.state_lock:
            if self.current_order is None:
                response.success = False
                response.message = "no current /arm/order"
                return response
            if self.locked_driver_pose_mm is None:
                response.success = False
                response.message = "driver pose not locked"
                return response
        if not self.sequence_lock.acquire(blocking=False):
            response.success = False
            response.message = "arm sequence busy"
            return response
        threading.Thread(target=self.run_deliver_sequence, daemon=True).start()
        response.success = True
        response.message = "delivery sequence accepted"
        return response

    def run_pay_sequence(self) -> None:
        try:
            with self.state_lock:
                driver = np.asarray(self.locked_driver_pose_mm, dtype=float).copy()
            self.run_legacy_command("MOVE_PAYMENT", "", driver[0], driver[1], driver[2])
            self.notify_arm_done("payment pose complete")
        except Exception as exc:
            self.get_logger().error(f"PAY sequence failed: {type(exc).__name__}: {exc}")
        finally:
            self.sequence_lock.release()

    def wait_for_locked_item(self):
        start = time.monotonic()
        while rclpy.ok():
            with self.state_lock:
                if self.locked_item is not None:
                    return dict(self.locked_item)
            if (
                self.item_wait_timeout_sec > 0.0
                and time.monotonic() - start > self.item_wait_timeout_sec
            ):
                raise RuntimeError("timed out waiting for Detection1 item lock")
            time.sleep(0.05)
        raise RuntimeError("ROS shutdown while waiting for item")

    def run_deliver_sequence(self) -> None:
        try:
            item = self.wait_for_locked_item()
            with self.state_lock:
                driver = np.asarray(self.locked_driver_pose_mm, dtype=float).copy()
            self.run_legacy_command("RETURN_HOME", "", 0.0, 0.0, 0.0)
            self.run_legacy_command(
                "PICK", item["object_type"], item["x_mm"], item["y_mm"], 0.0
            )
            self.run_legacy_command(
                "HANDOFF", item["object_type"], driver[0], driver[1], driver[2]
            )
            self.run_legacy_command("RETURN_HOME", "", 0.0, 0.0, 0.0)
            self.notify_arm_done("delivery complete")
        except Exception as exc:
            self.get_logger().error(
                f"DELIVER sequence failed: {type(exc).__name__}: {exc}"
            )
        finally:
            self.sequence_lock.release()

    def notify_arm_done(self, note: str) -> None:
        if not self.arm_done_client.wait_for_service(
            timeout_sec=self.done_service_wait_sec
        ):
            raise RuntimeError("/arm/done service unavailable")
        future = self.arm_done_client.call_async(Trigger.Request())

        def done_cb(f):
            try:
                result = f.result()
                if result.success:
                    self.get_logger().info(f"/arm/done accepted | {note}")
                else:
                    self.get_logger().error(f"/arm/done rejected | {result.message}")
            except Exception as exc:
                self.get_logger().error(f"/arm/done call failed: {exc!r}")

        future.add_done_callback(done_cb)

    def _initialize_detection(self):
        self._camera_intrinsics = None
        self._detection_last_log = {}
        self._detected_item_pub = self.create_publisher(
            DetectedItem, DetectionGeometry.OUTPUT_TOPIC, 10
        )
        camera_qos = QoSProfile(
            depth=1,
            history=HistoryPolicy.KEEP_LAST,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(
            CameraInfo,
            DetectionGeometry.CAMERA_INFO_TOPIC,
            self.camera_info_cb,
            camera_qos,
        )
        self.create_subscription(
            Detection2DArray, DetectionGeometry.DETECTION_TOPIC, self.detections_cb, 10
        )
        self.get_logger().info(
            f"Detection1 OCR bridge ready | detection={DetectionGeometry.DETECTION_TOPIC} | camera_info={DetectionGeometry.CAMERA_INFO_TOPIC} | output={DetectionGeometry.OUTPUT_TOPIC}"
        )

    def camera_info_cb(self, msg):
        K = np.asarray(msg.k, dtype=float).reshape(3, 3)
        if not np.all(np.isfinite(K)):
            return
        if K[0, 0] <= 0.0 or K[1, 1] <= 0.0:
            return
        self._camera_intrinsics = K

    @staticmethod
    def parse_detection(det):
        object_type = None
        object_conf = 0.0
        order_number = None
        number_conf = 0.0
        for result in det.results:
            class_id = str(result.hypothesis.class_id)
            score = float(result.hypothesis.score)
            if class_id in DetectionGeometry.VALID_OBJECT_TYPES:
                if score >= object_conf:
                    object_type = class_id
                    object_conf = score
            elif class_id.startswith("num:"):
                number = class_id[4:].strip()
                if number and score >= number_conf:
                    order_number = number
                    number_conf = score
        if object_type is None or order_number is None:
            return None
        return (object_type, object_conf, order_number, number_conf)

    @staticmethod
    def pick_pixel(det):
        """Orientation-free detector: use published object center pixel."""
        return (float(det.bbox.center.position.x), float(det.bbox.center.position.y))

    def pixel_to_arm_xy(self, u, v, plane_z_mm=DetectionGeometry.TABLE_Z_MM):
        if self._camera_intrinsics is None:
            return None
        fx = float(self._camera_intrinsics[0, 0])
        fy = float(self._camera_intrinsics[1, 1])
        cx = float(self._camera_intrinsics[0, 2])
        cy = float(self._camera_intrinsics[1, 2])
        ray_cam = np.array(
            [(float(u) - cx) / fx, (float(v) - cy) / fy, 1.0], dtype=float
        )
        ray_arm = DetectionGeometry.R_CAM_TO_ARM @ ray_cam
        dz = float(ray_arm[2])
        if abs(dz) < 1e-09:
            return None
        scale = (float(plane_z_mm) - float(DetectionGeometry.T_CAM_TO_ARM_MM[2])) / dz
        if not math.isfinite(scale) or scale <= 0.0:
            return None
        p_arm = DetectionGeometry.T_CAM_TO_ARM_MM + scale * ray_arm
        if not np.all(np.isfinite(p_arm)):
            return None
        return (float(p_arm[0]), float(p_arm[1]))

    def detections_cb(self, msg):
        if self._camera_intrinsics is None:
            return
        now_ns = self.get_clock().now().nanoseconds
        for det in msg.detections:
            parsed = self.parse_detection(det)
            if parsed is None:
                continue
            object_type, object_conf, order_number, number_conf = parsed
            pixel = self.pick_pixel(det)
            if pixel is None:
                continue
            plane_z_mm = DetectionGeometry.OBJECT_CENTER_Z_MM.get(object_type)
            if plane_z_mm is None:
                continue
            xy = self.pixel_to_arm_xy(*pixel, plane_z_mm=plane_z_mm)
            if xy is None:
                continue
            x_mm, y_mm = xy
            out = DetectedItem()
            out.header = msg.header
            out.order_number = order_number
            out.object_type = object_type
            out.x_mm = x_mm
            out.y_mm = y_mm
            out.confidence = min(object_conf, number_conf)
            self.item_cb(out)
            self._detected_item_pub.publish(out)
            key = (str(det.id), order_number, object_type)
            last_ns = self._detection_last_log.get(key, 0)
            if now_ns - last_ns >= 1000000000:
                self._detection_last_log[key] = now_ns
                self.get_logger().info(
                    f"DetectedItem | track={det.id} order={order_number} type={object_type} center_z={plane_z_mm:.1f} mm xy=({x_mm:.1f},{y_mm:.1f}) mm obj_conf={object_conf:.3f} num_vote={number_conf:.3f}"
                )

    def _initialize_driver_pose(self):
        self.pose_estimation_enabled = True
        self.pose_enable_service = self.create_service(
            SetBool, "/pose_estimation/enable", self.pose_enable_cb
        )
        self._driver_pose_base_pub = self.create_publisher(
            PointStamped, "/driver_pose/target_base", 10
        )
        self.create_subscription(
            PointStamped, "/driver_pose/target", self.driver_camera_pose_cb, 10
        )
        self.get_logger().info(
            f"{'/driver_pose/target'} -> {'/driver_pose/target_base'}"
        )

    def pose_enable_cb(self, request, response):
        """Gate driver input for Main's OFF -> seven-second rearm -> ON policy."""
        with self.state_lock:
            self.pose_estimation_enabled = bool(request.data)
        response.success = True
        response.message = (
            "driver pose input enabled"
            if self.pose_estimation_enabled
            else "driver pose input disabled"
        )
        self.get_logger().info(response.message)
        return response

    def driver_camera_pose_cb(self, msg):
        with self.state_lock:
            if not self.pose_estimation_enabled:
                return
        xc = float(msg.point.x)
        yc = float(msg.point.y)
        zc = float(msg.point.z)
        if not all((math.isfinite(v) for v in (xc, yc, zc))):
            return
        xb, yb, zb = camera_to_base(xc, yc, zc)
        out = PointStamped()
        out.header = msg.header
        out.header.frame_id = "arm_base"
        out.point.x = xb
        out.point.y = yb
        out.point.z = zb
        self.runner.pose_cb(out)
        self.driver_pose_cb(out)
        self._driver_pose_base_pub.publish(out)


def main(args=None):
    rclpy.init(args=args)
    node = ArmControlNode()
    executor = MultiThreadedExecutor(num_threads=6)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            executor.remove_node(node)
        except Exception:
            pass
        try:
            node.runner.destroy_node()
        except Exception:
            pass
        try:
            node.destroy_node()
        except Exception:
            pass
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
