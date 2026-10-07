#!/usr/bin/env python3
"""
IRC drive-through Arm Control.

Architecture:
  new Main
      -> /arm/order
      -> /arm/pay
      -> /arm/deliver

  Detection1 (UNCHANGED)
      -> original Detection1 bridge logic, embedded in /arm_control
      -> /drive_thru/detected_item

  Detection2 (UNCHANGED)
      -> original driver_pose_base_bridge logic, embedded in /arm_control
      -> /driver_pose/target_base

  Arm Control
      -> owns order/detection communication and high-level sequence
      -> INHERITS the frozen motion implementation from motion_logic_immutable.py

  Motor Control
      -> executes only already-calculated tick paths / gripper commands

IMPORTANT:
  motion_logic_immutable.py is copied byte-for-byte from the last verified
  drive_thru_manager/control_node.py. This file does not rewrite PICK,
  HANDOFF, RETURN_HOME, CUP, bag, payment, IK, or path logic.
"""

from __future__ import annotations

import json
import math
import threading
import sys
import time
from collections import Counter, deque
from pathlib import Path
from ament_index_python.packages import get_package_share_directory

import numpy as np
import rclpy
from geometry_msgs.msg import PointStamped
from rclpy.executors import MultiThreadedExecutor
from std_msgs.msg import String
from std_srvs.srv import Trigger

from soomac_interfaces.msg import DetectedItem
from soomac_interfaces.srv import ArmCommand, MoveTickPath

from arm_control.motion_logic_immutable import DriveThruControlNode
_STUDY_ARM = str(
    Path(
        get_package_share_directory("arm_control")
    )
    / "study_arm"
)
if _STUDY_ARM not in sys.path:
    sys.path.insert(0, _STUDY_ARM)

from arm_control import handoff_runner_logic as _handoff_runner_logic
from arm_control.bridge_logic import (
    Detection1BridgeLogic,
    DriverPoseBaseBridgeLogic,
)


VALID_OBJECTS = ("paper_bag", "L_paper_bag", "cup")


class ArmControlNode(DriveThruControlNode):
    """
    High-level control coordinator.

    Motion/IK implementation comes from DriveThruControlNode unchanged.
    This subclass only adds:
      - new Main interface
      - Detection1/2 filtering/locking
      - sequence orchestration
    """

    def __init__(self) -> None:
        # Yesterday's motion source stays byte-identical. Only the legacy
        # helper Node ownership is redirected into this /arm_control node.
        _handoff_runner_logic.install_parent_node(self)
        sys.modules['small_bag_pose_handoff'] = _handoff_runner_logic

        # This initializes the exact existing high-level arm motion code.
        super().__init__()

        # ---------------- new communication parameters ----------------
        self.declare_parameter("driver_window", 5)
        self.declare_parameter("driver_min_samples", 3)
        self.declare_parameter("driver_inlier_radius_mm", 150.0)
        self.declare_parameter("driver_max_spread_mm", 120.0)

        self.declare_parameter("item_window", 5)
        self.declare_parameter("item_min_samples", 3)
        self.declare_parameter("item_max_spread_mm", 35.0)
        self.declare_parameter("item_min_confidence", 0.50)

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

        # ---------------- runtime state ----------------
        self.state_lock = threading.RLock()
        self.sequence_lock = threading.Lock()

        self.current_order = None

        self.driver_samples = deque(maxlen=self.driver_window)
        self.item_samples = deque(maxlen=self.item_window)

        self.locked_driver_pose_mm = None
        self.locked_item = None

        self.driver_notified = False
        self.driver_notify_pending = False

        # ---------------- Main interface ----------------
        self.create_subscription(
            String,
            "/arm/order",
            self.order_cb,
            10,
        )

        self.pay_service = self.create_service(
            Trigger,
            "/arm/pay",
            self.pay_cb,
        )

        self.deliver_service = self.create_service(
            Trigger,
            "/arm/deliver",
            self.deliver_cb,
        )

        self.arm_done_client = self.create_client(
            Trigger,
            "/arm/done",
        )

        self.driver_detected_client = self.create_client(
            Trigger,
            "/driver_detected",
        )

        # ---------------- Detection interface ----------------
        # Detection source code itself is NOT modified.
        # These are the exact outputs of the old verified bridges.
        self.create_subscription(
            DetectedItem,
            "/drive_thru/detected_item",
            self.item_cb,
            10,
        )

        self.create_subscription(
            PointStamped,
            "/driver_pose/target_base",
            self.driver_pose_cb,
            10,
        )

        # D1/D2 bridge logic is embedded in THIS ROS node.
        # Original bridge algorithms are preserved; only their
        # separate rclpy Node construction is removed.
        self._detection1_bridge = Detection1BridgeLogic(self)
        self._driver_pose_base_bridge = DriverPoseBaseBridgeLogic(self)

        self.notify_timer = self.create_timer(
            0.20,
            self.maybe_notify_driver,
        )

        self.get_logger().info(
            "Arm Control coordinator ready | "
            "Main=/arm/order,/arm/pay,/arm/deliver,/arm/done | "
            "D1=/drive_thru/detected_item | "
            "D2=/driver_pose/target_base"
        )

        self.get_logger().info(
            "Motion implementation is inherited unchanged from "
            "motion_logic_immutable.py"
        )

    # ------------------------------------------------------------------
    # Embedded Runner service wait
    #
    # The inherited motion code used spin_until_future_complete(self.runner),
    # but runner is now a proxy owned by this already-spinning ROS node.
    # Keep the exact MoveTickPath request/path behavior and only replace
    # the Future waiting plumbing.
    # ------------------------------------------------------------------

    def move_path(
        self,
        label,
        ticks_list,
        profile_velocity=0,
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
        req.point_count = int(len(arr))
        req.label = str(label)
        req.profile_velocity = int(profile_velocity)
        req.timeout_sec = float(
            self.core.PATH_TIMEOUT_SEC
        )

        future = self.runner.path.call_async(req)

        # Parent /arm_control is already running inside
        # MultiThreadedExecutor. Do NOT spin it again here.
        self.runner._wait_future(future)

        res = future.result()

        if res is None or not res.success:
            raise RuntimeError(
                f"{label} failed: "
                f"{getattr(res, 'message', 'no response')}"
            )

        self.get_logger().info(
            f"{label} reached | "
            f"ticks={list(res.reached_ticks)}"
        )

    # ------------------------------------------------------------------
    # Main order
    # ------------------------------------------------------------------

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
            self.get_logger().error(
                f"/arm/order rejected | {msg.data!r} | {exc!r}"
            )
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
            "ORDER LOCKED | "
            f"order={order['order_no']} | "
            f"mcorder={order['is_mcorder']} | "
            f"menu={order['menu']!r}"
        )

    # ------------------------------------------------------------------
    # Detection2 - old verified filtering/lock policy
    # ------------------------------------------------------------------

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
                    + ", ".join(f"{v:.1f}" for v in locked)
                    + "] mm"
                )

    def try_lock_driver(self):
        if len(self.driver_samples) < self.driver_min_samples:
            return None

        a = np.asarray(self.driver_samples, dtype=float)

        median = np.median(a, axis=0)

        distances = np.linalg.norm(
            a[:, :2] - median[:2],
            axis=1,
        )

        inliers = a[
            distances <= self.driver_inlier_radius_mm
        ]

        if len(inliers) < self.driver_min_samples:
            return None

        filtered = np.median(inliers, axis=0)

        spread = float(
            np.max(
                np.linalg.norm(
                    inliers[:, :2] - filtered[:2],
                    axis=1,
                )
            )
        )

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

        future = self.driver_detected_client.call_async(
            Trigger.Request()
        )
        future.add_done_callback(
            self.driver_detected_response_cb
        )

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
            self.get_logger().info(
                "Main /driver_detected accepted"
            )
        else:
            # Main may not yet be in WAIT_CAR. Timer retries later.
            self.get_logger().debug(
                f"Main /driver_detected not accepted yet: {message}"
            )

    # ------------------------------------------------------------------
    # Detection1 - old verified filtering/lock policy
    # ------------------------------------------------------------------

    def item_cb(self, msg: DetectedItem) -> None:
        with self.state_lock:
            if self.current_order is None:
                return

            expected = str(
                self.current_order["order_no"]
            )

            order_number = str(
                msg.order_number
            ).strip()

            if order_number != expected:
                return

            confidence = float(msg.confidence)

            if (
                not math.isfinite(confidence)
                or confidence < self.item_min_confidence
            ):
                return

            object_type = str(
                msg.object_type
            ).strip()

            if object_type not in VALID_OBJECTS:
                return

            x_mm = float(msg.x_mm)
            y_mm = float(msg.y_mm)

            if not np.isfinite(
                [x_mm, y_mm]
            ).all():
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
                    "ITEM LOCKED | "
                    + json.dumps(
                        locked,
                        ensure_ascii=False,
                    )
                )

    def try_lock_item(self):
        if len(self.item_samples) < self.item_min_samples:
            return None

        samples = list(self.item_samples)

        type_counts = Counter(
            s["object_type"]
            for s in samples
        )

        object_type, _ = (
            type_counts.most_common(1)[0]
        )

        same_type = [
            s
            for s in samples
            if s["object_type"] == object_type
        ]

        if len(same_type) < self.item_min_samples:
            return None

        xy = np.array(
            [
                [s["x_mm"], s["y_mm"]]
                for s in same_type
            ],
            dtype=float,
        )

        median_xy = np.median(
            xy,
            axis=0,
        )

        distances = np.linalg.norm(
            xy - median_xy,
            axis=1,
        )

        inlier_samples = [
            s
            for s, ok
            in zip(
                same_type,
                distances <= self.item_max_spread_mm,
            )
            if ok
        ]

        if len(inlier_samples) < self.item_min_samples:
            return None

        inlier_xy = np.array(
            [
                [s["x_mm"], s["y_mm"]]
                for s in inlier_samples
            ],
            dtype=float,
        )

        final_xy = np.median(
            inlier_xy,
            axis=0,
        )

        spread = float(
            np.max(
                np.linalg.norm(
                    inlier_xy - final_xy,
                    axis=1,
                )
            )
        )

        if spread > self.item_max_spread_mm:
            return None

        confidence = float(
            np.median(
                [
                    s["confidence"]
                    for s in inlier_samples
                ]
            )
        )

        return {
            "object_type": object_type,
            "x_mm": float(final_xy[0]),
            "y_mm": float(final_xy[1]),
            "confidence": confidence,
            "spread_mm": spread,
        }

    # ------------------------------------------------------------------
    # Exact legacy motion dispatcher
    # ------------------------------------------------------------------

    def run_legacy_command(
        self,
        command: str,
        object_type: str = "",
        x_mm: float = 0.0,
        y_mm: float = 0.0,
        z_mm: float = 0.0,
    ) -> None:
        """
        Reuse the frozen command_cb directly.

        This is intentional: all object-specific motion sequencing, IK,
        path validation, CUP synchronization, payment motion and HOME
        behavior remain in motion_logic_immutable.py.
        """
        request = ArmCommand.Request()
        request.command = str(command)
        request.object_type = str(object_type)
        request.x_mm = float(x_mm)
        request.y_mm = float(y_mm)
        request.z_mm = float(z_mm)

        response = ArmCommand.Response()

        result = self.command_cb(
            request,
            response,
        )

        if not result.success:
            raise RuntimeError(
                f"{command} failed | "
                f"{result.code} | "
                f"{result.message}"
            )

    # ------------------------------------------------------------------
    # New Main services
    # ------------------------------------------------------------------

    def pay_cb(
        self,
        request: Trigger.Request,
        response: Trigger.Response,
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

        if not self.sequence_lock.acquire(
            blocking=False
        ):
            response.success = False
            response.message = "arm sequence busy"
            return response

        threading.Thread(
            target=self.run_pay_sequence,
            daemon=True,
        ).start()

        response.success = True
        response.message = "MOVE_PAYMENT accepted"
        return response

    def deliver_cb(
        self,
        request: Trigger.Request,
        response: Trigger.Response,
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

        if not self.sequence_lock.acquire(
            blocking=False
        ):
            response.success = False
            response.message = "arm sequence busy"
            return response

        threading.Thread(
            target=self.run_deliver_sequence,
            daemon=True,
        ).start()

        response.success = True
        response.message = "delivery sequence accepted"
        return response

    def run_pay_sequence(self) -> None:
        try:
            with self.state_lock:
                driver = np.asarray(
                    self.locked_driver_pose_mm,
                    dtype=float,
                ).copy()

            # Existing verified payment motion. No rewrite.
            self.run_legacy_command(
                "MOVE_PAYMENT",
                "",
                driver[0],
                driver[1],
                driver[2],
            )

            self.notify_arm_done(
                "payment pose complete"
            )

        except Exception as exc:
            self.get_logger().error(
                f"PAY sequence failed: {type(exc).__name__}: {exc}"
            )
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
                and time.monotonic() - start
                > self.item_wait_timeout_sec
            ):
                raise RuntimeError(
                    "timed out waiting for Detection1 item lock"
                )

            time.sleep(0.05)

        raise RuntimeError(
            "ROS shutdown while waiting for item"
        )

    def run_deliver_sequence(self) -> None:
        try:
            item = self.wait_for_locked_item()

            with self.state_lock:
                driver = np.asarray(
                    self.locked_driver_pose_mm,
                    dtype=float,
                ).copy()

            # ------------------------------------------------------
            # Payment ends away from START.
            # PICK implementations require START pose, so return
            # home before beginning the existing delivery sequence.
            #
            # Existing object motion remains unchanged:
            #   PICK -> HANDOFF -> RETURN_HOME
            # ------------------------------------------------------
            self.run_legacy_command(
                "RETURN_HOME",
                "",
                0.0,
                0.0,
                0.0,
            )

            self.run_legacy_command(
                "PICK",
                item["object_type"],
                item["x_mm"],
                item["y_mm"],
                0.0,
            )

            self.run_legacy_command(
                "HANDOFF",
                item["object_type"],
                driver[0],
                driver[1],
                driver[2],
            )

            self.run_legacy_command(
                "RETURN_HOME",
                "",
                0.0,
                0.0,
                0.0,
            )

            self.notify_arm_done(
                "delivery complete"
            )

        except Exception as exc:
            self.get_logger().error(
                f"DELIVER sequence failed: {type(exc).__name__}: {exc}"
            )
        finally:
            self.sequence_lock.release()

    # ------------------------------------------------------------------
    # Main completion
    # ------------------------------------------------------------------

    def notify_arm_done(
        self,
        note: str,
    ) -> None:
        if not self.arm_done_client.wait_for_service(
            timeout_sec=self.done_service_wait_sec
        ):
            raise RuntimeError(
                "/arm/done service unavailable"
            )

        future = self.arm_done_client.call_async(
            Trigger.Request()
        )

        def done_cb(f):
            try:
                result = f.result()
                if result.success:
                    self.get_logger().info(
                        f"/arm/done accepted | {note}"
                    )
                else:
                    self.get_logger().error(
                        f"/arm/done rejected | {result.message}"
                    )
            except Exception as exc:
                self.get_logger().error(
                    f"/arm/done call failed: {exc!r}"
                )

        future.add_done_callback(done_cb)

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
