#!/usr/bin/env python3

import sys
import threading
from pathlib import Path

import numpy as np
import rclpy
from std_srvs.srv import Trigger
from rclpy.node import Node

from soomac_interfaces.srv import ArmCommand, MoveTickPath


COMMAND_SERVICE = "/arm_control/execute"

CMD_PING = "PING"
CMD_MOVE_PAYMENT = "MOVE_PAYMENT"
CMD_PICK = "PICK"
CMD_HANDOFF = "HANDOFF"
CMD_RETURN_HOME = "RETURN_HOME"

OBJ_PAPER_BAG = "paper_bag"
OBJ_L_PAPER_BAG = "L_paper_bag"
OBJ_CUP = "cup"


def load_existing_arm_code(workspace_root: str):
    """
    Reuse the arm code that has already been tested on the real robot.

    Current implemented route:
      paper_bag dynamic PICK/PULL
      -> frozen driver pose
      -> full-IK HANDOFF
      -> RETURN_HOME

    L_paper_bag reuses the validated BAG1 tool-down sequence.
    cup / payment pose remain intentional TODOs.
    """
    root = Path(workspace_root).expanduser().resolve()
    study_arm = root / "study_arm"

    if not study_arm.is_dir():
        raise RuntimeError(
            f"study_arm not found: {study_arm}"
        )

    study_text = str(study_arm)

    if study_text not in sys.path:
        sys.path.insert(0, study_text)

    import bag_tooldown_family_core as core

    from small_bag_pose_handoff import (
        Runner,
        compute_handoff_target,
    )

    from small_bag_fullik_handoff_execute import (
        build_fullik_path,
    )

    from worldview_paper_bag_full_delivery import (
        build_dynamic_pick_plan,
        make_handoff_plan,
    )

    from l_paper_bag_plan import (
        build_plan as build_l_paper_bag_plan,
        TURN_VELOCITY as L_PAPER_BAG_TURN_VELOCITY,
    )

    from cup_dynamic_pick import (
        build_dynamic_cup_pick_plan,
    )

    return {
        "root": root,
        "core": core,
        "Runner": Runner,
        "compute_handoff_target": compute_handoff_target,
        "build_fullik_path": build_fullik_path,
        "build_dynamic_pick_plan": build_dynamic_pick_plan,
        "build_dynamic_cup_pick_plan": build_dynamic_cup_pick_plan,
        "make_handoff_plan": make_handoff_plan,
        "build_l_paper_bag_plan": build_l_paper_bag_plan,
        "l_paper_bag_turn_velocity": int(L_PAPER_BAG_TURN_VELOCITY),
    }


class DriveThruControlNode(Node):
    """
    High-level arm Control node.

    IMPORTANT:
      This node does NOT directly drive Dynamixel motors.

    It reuses the existing Runner, which talks to:
      /motor/move_tick_path
      gripper open service
      gripper close service

    So the already-working task_1_motor_control_node remains the
    low-level motor node.
    """

    def __init__(self):
        super().__init__("drive_thru_control")

        default_root = (
            Path.home()
            / "Documents"
            / "drive_thru_robot_arm"
        )

        self.declare_parameter(
            "workspace_root",
            str(default_root),
        )

        workspace_root = str(
            self.get_parameter(
                "workspace_root"
            ).value
        )

        loaded = load_existing_arm_code(
            workspace_root
        )

        self.workspace_root = Path(
            workspace_root
        )

        self.core = loaded["core"]
        self.compute_handoff_target = (
            loaded["compute_handoff_target"]
        )
        self.build_fullik_path = (
            loaded["build_fullik_path"]
        )
        self.build_dynamic_pick_plan = (
            loaded["build_dynamic_pick_plan"]
        )
        self.build_dynamic_cup_pick_plan = (
            loaded["build_dynamic_cup_pick_plan"]
        )

        self.make_handoff_plan = (
            loaded["make_handoff_plan"]
        )
        self.build_l_paper_bag_plan = (
            loaded["build_l_paper_bag_plan"]
        )
        self.l_paper_bag_turn_velocity = (
            loaded["l_paper_bag_turn_velocity"]
        )

        Runner = loaded["Runner"]

        # Separate helper Node.
        # Runner's existing blocking client helpers spin this node itself,
        # while this Control node serves /arm_control/execute.
        self.runner = Runner(
            use_motor=True
        )

        # CUP uses its own CLOSE=500 service.
        # OPEN is already runner.open.
        self.cup_close = (
            self.runner.create_client(
                Trigger,
                "/motor/cup_gripper_close",
            )
        )

        self.command_lock = threading.Lock()

        # Stored only after a successful PICK.
        self.active_pick = None

        self.service = self.create_service(
            ArmCommand,
            COMMAND_SERVICE,
            self.command_cb,
        )

        self.get_logger().info(
            "DriveThru Control ready | "
            f"service={COMMAND_SERVICE}"
        )

        self.get_logger().info(
            "implemented: PING, PICK/HANDOFF(paper_bag), "
            "PICK/HANDOFF(L_paper_bag), "
            "PICK/HANDOFF(cup), RETURN_HOME"
        )

        self.get_logger().info(
            "TODO: MOVE_PAYMENT"
        )

    # --------------------------------------------------------
    # Response helpers
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Safety / state helpers
    # --------------------------------------------------------

    def ensure_motor_services(self):
        self.runner.wait_services()

    def ensure_start_pose(self):
        # Discard cached joint state.
        #
        # Motor Control intentionally skips /motor/joint_state publishing
        # while a motion owns motion_lock.  Therefore runner.current may
        # still contain a pre-motion pose immediately after MoveTickPath
        # returns.  Force get_current() to wait for a fresh sample.
        self.runner.current = None

        current = self.runner.get_current()

        error = (
            current
            - self.core.START_TICKS
        )

        if np.any(
            np.abs(error)
            > self.core.START_TOL_TICKS
        ):
            raise RuntimeError(
                "START pose check failed | "
                f"current={current.tolist()} "
                f"error={error.tolist()}"
            )

        return current

    # --------------------------------------------------------
    # Implemented commands
    # --------------------------------------------------------

    def do_pick_paper_bag(
        self,
        x_mm,
        y_mm,
    ):
        # Calculate the complete path BEFORE sending any motion.
        radius, pick_plan = (
            self.build_dynamic_pick_plan(
                float(x_mm),
                float(y_mm),
            )
        )

        # ----------------------------------------------------
        # PAPER BAG J5
        #
        # Tool-down orientation is controlled by J2/J3/J4.
        # J5 wrist roll does not need to rotate for handoff.
        # Keep the same J5 orientation used at LIFT.
        # ----------------------------------------------------
        paper_j5_tick = int(
            pick_plan["LIFT"]["ticks"][4]
        )
        paper_j5_deg = float(
            pick_plan["LIFT"]["q"][4]
        )

        pick_plan["PULL100"]["ticks"][4] = (
            paper_j5_tick
        )
        pick_plan["PULL100"]["q"][4] = (
            paper_j5_deg
        )

        handoff_plan = (
            self.make_handoff_plan(
                pick_plan
            )
        )

        self.ensure_motor_services()
        current = self.ensure_start_pose()

        self.get_logger().info(
            "PICK paper_bag accepted | "
            f"XY=({x_mm:+.1f},{y_mm:+.1f}) "
            f"R={radius:.1f} "
            f"current={current.tolist()}"
        )

        self.runner.trigger(
            self.runner.open,
            "paper_bag/OPEN",
        )

        self.runner.move(
            "paper_bag/READY",
            [
                pick_plan["READY"]["ticks"],
            ],
        )

        self.runner.move(
            "paper_bag/GRASP",
            [
                pick_plan["GRASP"]["ticks"],
            ],
        )

        self.runner.trigger(
            self.runner.close,
            "paper_bag/CLOSE",
        )

        self.runner.move(
            "paper_bag/LIFT",
            [
                pick_plan["LIFT"]["ticks"],
            ],
        )

        # LIFT 완료 후 위치는 그대로 유지하고
        # J5만 0 deg(2048) -> 90 deg(3072) 회전.

        self.runner.move(
            "paper_bag/PULL100",
            [
                pick_plan["PULL100"]["ticks"],
            ],
        )

        self.active_pick = {
            "object_type": OBJ_PAPER_BAG,
            "radius": float(radius),
            "x_mm": float(x_mm),
            "y_mm": float(y_mm),
            "pick_plan": pick_plan,
            "handoff_plan": handoff_plan,
        }

    def move_path(
        self,
        label,
        ticks_list,
        profile_velocity=0,
    ):
        """Send a MoveTickPath request using the existing Motor Control node."""
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
        req.timeout_sec = float(self.core.PATH_TIMEOUT_SEC)

        future = self.runner.path.call_async(req)
        rclpy.spin_until_future_complete(
            self.runner,
            future,
        )

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


    def build_lbag_horizontal_pull(
        self,
        start_q,
        pull_mm=100.0,
        segments=10,
    ):
        """
        L-paperbag 전용 horizontal PULL.

        - 현재 LIFT TCP에서 시작
        - Z 유지
        - 같은 방사방향 유지
        - 100 mm 로봇 베이스 쪽으로 당김
        - tilt = 0 deg 유지
        - J5 = 0 deg / 2048 유지
        """

        chain = (
            self.core.spp.create_robot_chain()
        )

        start_q = np.asarray(
            start_q,
            dtype=float,
        ).reshape(5).copy()

        state = self.core.pose_state(
            chain,
            np.deg2rad(start_q),
        )

        tool_axis = self.core.unit(
            state["tool_axis"]
        )

        start_tcp = (
            np.asarray(
                state["xyz"],
                dtype=float,
            )
            + self.core.AXIAL_OFFSET_MM
            * tool_axis
        )

        # 시작부터 수평이 아니면 실행 금지.
        if abs(float(tool_axis[2])) > 0.03:
            raise RuntimeError(
                "L-paperbag PULL start is not horizontal | "
                f"axis={tool_axis.tolist()}"
            )

        x0 = float(start_tcp[0])
        y0 = float(start_tcp[1])
        z0 = float(start_tcp[2])

        r0 = float(
            np.hypot(x0, y0)
        )

        if r0 <= float(pull_mm):
            raise RuntimeError(
                "L-paperbag PULL radius invalid | "
                f"start_r={r0:.1f} "
                f"pull={pull_mm:.1f}"
            )

        az = float(
            np.arctan2(y0, x0)
        )

        old_j5_deg = self.core.J5_DEG
        old_j5_tick = self.core.J5_TICK

        points = []
        seed_q = start_q.copy()

        try:
            # L-paperbag은 끝까지 J5=2048.
            self.core.J5_DEG = 0.0
            self.core.J5_TICK = 2048

            for i in range(
                1,
                int(segments) + 1,
            ):
                u = (
                    i
                    / float(segments)
                )

                r = (
                    r0
                    - float(pull_mm) * u
                )

                target = np.array(
                    [
                        r * np.cos(az),
                        r * np.sin(az),
                        z0,
                    ],
                    dtype=float,
                )

                solved = self.core.solve_tcp(
                    chain,
                    target,
                    0.0,       # horizontal
                    seed_q,
                )

                q = np.asarray(
                    solved["q"],
                    dtype=float,
                ).copy()

                ticks = np.asarray(
                    solved["ticks"],
                    dtype=np.int64,
                ).copy()

                # horizontal orientation:
                # J2 + J3 + J4 = 90 deg
                total = float(
                    q[1]
                    + q[2]
                    + q[3]
                )

                if abs(total - 90.0) > 0.2:
                    raise RuntimeError(
                        "L-paperbag PULL "
                        "horizontal sum mismatch | "
                        f"{total:.3f}"
                    )

                if int(ticks[4]) != 2048:
                    raise RuntimeError(
                        "L-paperbag PULL J5 changed | "
                        f"{int(ticks[4])}"
                    )

                self.core.cfg.validate_arm_ticks(
                    ticks
                )

                points.append(
                    {
                        "target": target,
                        "q": q,
                        "ticks": ticks,
                    }
                )

                seed_q = q

        finally:
            self.core.J5_DEG = old_j5_deg
            self.core.J5_TICK = old_j5_tick

        if not points:
            raise RuntimeError(
                "L-paperbag PULL generated no points"
            )

        return points


    # ========================================================
    # L-PAPER BAG HORIZONTAL V2
    #
    # Entire L-paperbag route keeps:
    #
    #   tilt = 0 deg
    #   J2 + J3 + J4 = 90 deg
    #   J5 = 0 deg = 2048 tick
    #
    # No tool-down transition.
    # ========================================================

    def _lbag_v2_tcp(
        self,
        chain,
        q,
    ):
        q = np.asarray(
            q,
            dtype=float,
        ).reshape(5)

        state = self.core.pose_state(
            chain,
            np.deg2rad(q),
        )

        xyz = np.asarray(
            state["xyz"],
            dtype=float,
        )

        axis = self.core.unit(
            state["tool_axis"]
        )

        tcp = (
            xyz
            + float(
                self.core.AXIAL_OFFSET_MM
            )
            * axis
        )

        return tcp, axis


    def _lbag_v2_solve(
        self,
        chain,
        target,
        seed_q=None,
    ):
        target = np.asarray(
            target,
            dtype=float,
        ).reshape(3)

        old_j5_deg = float(
            self.core.J5_DEG
        )

        old_j5_tick = int(
            self.core.J5_TICK
        )

        try:
            # L-paperbag wrist roll fixed.
            self.core.J5_DEG = 0.0
            self.core.J5_TICK = 2048

            result = self.core.solve_tcp(
                chain,
                target,
                0.0,
                seed_q,
            )

        finally:
            self.core.J5_DEG = old_j5_deg
            self.core.J5_TICK = old_j5_tick

        q = np.asarray(
            result["q"],
            dtype=float,
        ).copy()

        ticks = np.asarray(
            result["ticks"],
            dtype=np.int64,
        ).copy()

        # ----------------------------------------------------
        # Hard validation
        # ----------------------------------------------------

        total = float(
            q[1]
            + q[2]
            + q[3]
        )

        if abs(total - 90.0) > 0.20:
            raise RuntimeError(
                "Lbag horizontal orientation lost | "
                f"J2+J3+J4={total:.3f}"
            )

        if int(ticks[4]) != 2048:
            raise RuntimeError(
                "Lbag J5 changed | "
                f"{int(ticks[4])}"
            )

        self.core.cfg.validate_arm_ticks(
            ticks
        )

        tcp, axis = (
            self._lbag_v2_tcp(
                chain,
                q,
            )
        )

        if abs(float(axis[2])) > 0.03:
            raise RuntimeError(
                "Lbag tool is not horizontal | "
                f"axis={axis.tolist()}"
            )

        err = float(
            np.linalg.norm(
                tcp - target
            )
        )

        if err > 2.0:
            raise RuntimeError(
                "Lbag TCP error too large | "
                f"{err:.3f} mm"
            )

        return {
            "target": target,
            "q": q,
            "ticks": ticks,
            "tcp": tcp,
            "axis": axis,
            "error": err,
        }


    def _lbag_v2_line(
        self,
        chain,
        start_target,
        end_target,
        seed_q,
        segments,
    ):
        start_target = np.asarray(
            start_target,
            dtype=float,
        )

        end_target = np.asarray(
            end_target,
            dtype=float,
        )

        points = []

        seed = np.asarray(
            seed_q,
            dtype=float,
        ).copy()

        for i in range(
            1,
            int(segments) + 1,
        ):
            u = (
                i
                / float(segments)
            )

            target = (
                (1.0 - u)
                * start_target
                + u
                * end_target
            )

            solved = (
                self._lbag_v2_solve(
                    chain,
                    target,
                    seed,
                )
            )

            points.append(
                solved
            )

            seed = solved["q"]

        return points


    def _lbag_v2_joint_bridge(
        self,
        current_ticks,
        goal_ticks,
        segments=14,
    ):
        """
        START -> SAFE 전용.

        한 관절씩 가는 게 아니라
        J1~J5 모두 같은 progress u로 움직인다.

        각 joint는 start -> goal 사이에서만 움직이므로
        중간 overshoot가 생기지 않는다.
        """

        start = np.asarray(
            current_ticks,
            dtype=np.int64,
        ).reshape(5)

        goal = np.asarray(
            goal_ticks,
            dtype=np.int64,
        ).reshape(5)

        points = []

        for i in range(
            1,
            int(segments) + 1,
        ):
            u = (
                i
                / float(segments)
            )

            # quintic progress
            blend = (
                10.0 * u**3
                - 15.0 * u**4
                + 6.0 * u**5
            )

            ticks = np.rint(
                start
                + blend
                * (goal - start)
            ).astype(
                np.int64
            )

            self.core.cfg.validate_arm_ticks(
                ticks
            )

            points.append(
                ticks
            )

        points[-1] = goal.copy()

        return points


    def _lbag_v2_turn(
        self,
        start_q,
        target_angle_deg,
        segments=24,
    ):
        """
        PULL 완료 posture에서 제자리 회전.

        IMPORTANT:
          J1만 변경.
          J2/J3/J4/J5는 한 tick도 의도적으로 바꾸지 않는다.

        따라서 PULL한 radius 그대로 회전한다.
        """

        start_q = np.asarray(
            start_q,
            dtype=float,
        ).reshape(5).copy()

        target_angle_deg = float(
            target_angle_deg
        )

        points = []

        for i in range(
            1,
            int(segments) + 1,
        ):
            u = (
                i
                / float(segments)
            )

            blend = (
                10.0 * u**3
                - 15.0 * u**4
                + 6.0 * u**5
            )

            q = start_q.copy()

            q[0] = (
                start_q[0]
                + blend
                * (
                    target_angle_deg
                    - start_q[0]
                )
            )

            # Exact hold
            q[1] = start_q[1]
            q[2] = start_q[2]
            q[3] = start_q[3]
            q[4] = 0.0

            ticks = np.asarray(
                self.core.cfg.model_deg_to_ticks(
                    q
                ),
                dtype=np.int64,
            )

            ticks[4] = 2048

            self.core.cfg.validate_arm_ticks(
                ticks
            )

            points.append(
                {
                    "q": q,
                    "ticks": ticks,
                }
            )

        return points


    def do_pick_l_paper_bag(
        self,
        detected_x_mm,
        detected_y_mm,
    ):
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

        x = float(
            detected_x_mm
        )

        y = float(
            detected_y_mm
        )

        detected_r = float(
            np.hypot(x, y)
        )

        if detected_r < 1.0:
            raise RuntimeError(
                "invalid Lbag detected radius"
            )

        az = float(
            np.arctan2(y, x)
        )

        ux = float(
            np.cos(az)
        )

        uy = float(
            np.sin(az)
        )


        # ----------------------------------------------------
        # Final geometry
        # ----------------------------------------------------

        # 요청 반영:
        # body-center XY보다 50 mm 더 깊게.
        INSERT_EXTRA_MM = 50.0

        # 손잡이 진입 전 stand-off.
        APPROACH_MM = 120.0

        # 요청 반영:
        # 기존 302 -> 282 mm.
        GRASP_Z_MM = 282.0

        # 50 mm vertical lift.
        LIFT_Z_MM = 332.0

        # 먼저 위에서 수평 자세를 완성할 높이.
        SAFE_Z_MM = 380.0

        # 다른 item처럼 lift 후 로봇 쪽으로 당김.
        PULL_MM = 150.0


        grasp_r = (
            detected_r
            + INSERT_EXTRA_MM
        )

        ready_r = (
            grasp_r
            - APPROACH_MM
        )

        pull_r = (
            grasp_r
            - PULL_MM
        )

        if ready_r <= 150.0:
            raise RuntimeError(
                "Lbag READY radius too small | "
                f"{ready_r:.1f} mm"
            )

        if pull_r <= 150.0:
            raise RuntimeError(
                "Lbag PULL radius too small | "
                f"{pull_r:.1f} mm"
            )


        safe_target = np.array(
            [
                ready_r * ux,
                ready_r * uy,
                SAFE_Z_MM,
            ],
            dtype=float,
        )

        ready_target = np.array(
            [
                ready_r * ux,
                ready_r * uy,
                GRASP_Z_MM,
            ],
            dtype=float,
        )

        grasp_target = np.array(
            [
                grasp_r * ux,
                grasp_r * uy,
                GRASP_Z_MM,
            ],
            dtype=float,
        )

        lift_target = np.array(
            [
                grasp_r * ux,
                grasp_r * uy,
                LIFT_Z_MM,
            ],
            dtype=float,
        )

        pull_target = np.array(
            [
                pull_r * ux,
                pull_r * uy,
                LIFT_Z_MM,
            ],
            dtype=float,
        )


        # ----------------------------------------------------
        # Calculate EVERYTHING before motor motion.
        # ----------------------------------------------------

        chain = (
            self.core.spp.create_robot_chain()
        )

        safe = (
            self._lbag_v2_solve(
                chain,
                safe_target,
                None,
            )
        )

        # SAFE -> READY:
        # same XY, horizontal maintained, vertical descent.
        descend = (
            self._lbag_v2_line(
                chain,
                safe_target,
                ready_target,
                safe["q"],
                segments=16,
            )
        )

        # READY -> GRASP:
        # same Z, horizontal maintained.
        approach = (
            self._lbag_v2_line(
                chain,
                ready_target,
                grasp_target,
                descend[-1]["q"],
                segments=20,
            )
        )

        # GRASP -> LIFT:
        # same XY, horizontal maintained.
        lift = (
            self._lbag_v2_line(
                chain,
                grasp_target,
                lift_target,
                approach[-1]["q"],
                segments=12,
            )
        )

        # LIFT -> PULL150:
        # same Z, same azimuth, horizontal maintained.
        pull = (
            self._lbag_v2_line(
                chain,
                lift_target,
                pull_target,
                lift[-1]["q"],
                segments=14,
            )
        )


        self.ensure_motor_services()

        current = (
            self.ensure_start_pose()
        )

        start_to_safe = (
            self._lbag_v2_joint_bridge(
                current,
                safe["ticks"],
                segments=14,
            )
        )


        self.get_logger().info(
            "PICK L_paper_bag V2 accepted | "
            f"detected_R={detected_r:.1f} | "
            f"ready_R={ready_r:.1f} | "
            f"grasp_R={grasp_r:.1f} | "
            f"pull_R={pull_r:.1f} | "
            f"SAFE_Z={SAFE_Z_MM:.1f} | "
            f"GRASP_Z={GRASP_Z_MM:.1f} | "
            f"LIFT_Z={LIFT_Z_MM:.1f} | "
            "horizontal J5=2048"
        )


        # ----------------------------------------------------
        # EXECUTION
        # ----------------------------------------------------

        self.runner.trigger(
            self.runner.open,
            "L_paper_bag/OPEN",
        )


        # 1. START -> SAFE
        #
        # all joints share the same progress.
        # No J4-only late correction.
        self.move_path(
            "L_paper_bag/START_TO_SAFE_HORIZONTAL",
            start_to_safe,
            profile_velocity=12,
        )


        # 2. SAFE -> READY
        #
        # already horizontal, now descend only in Cartesian Z.
        self.move_path(
            "L_paper_bag/DESCEND_HORIZONTAL",
            [
                p["ticks"]
                for p in descend
            ],
            profile_velocity=12,
        )


        # 3. READY -> GRASP
        #
        # literal horizontal insertion.
        self.move_path(
            "L_paper_bag/APPROACH_HORIZONTAL",
            [
                p["ticks"]
                for p in approach
            ],
            profile_velocity=12,
        )


        self.runner.trigger(
            self.runner.close,
            "L_paper_bag/CLOSE",
        )


        # 4. GRASP -> LIFT
        self.move_path(
            "L_paper_bag/LIFT_HORIZONTAL",
            [
                p["ticks"]
                for p in lift
            ],
            profile_velocity=12,
        )


        # 5. LIFT -> PULL150
        self.move_path(
            "L_paper_bag/PULL150_HORIZONTAL",
            [
                p["ticks"]
                for p in pull
            ],
            profile_velocity=12,
        )


        # ----------------------------------------------------
        # Store EXACT PULL endpoint.
        #
        # HANDOFF TURN MUST start from this exact posture.
        # No re-solving / no outward reposition before TURN.
        # ----------------------------------------------------

        self.active_pick = {
            "object_type":
                OBJ_L_PAPER_BAG,

            "detected_x_mm":
                x,

            "detected_y_mm":
                y,

            "lbag_pull_q":
                pull[-1]["q"].copy(),

            "lbag_pull_ticks":
                pull[-1]["ticks"].copy(),

            "lbag_pull_tcp":
                pull[-1]["tcp"].copy(),

            "lbag_pull_radius":
                float(
                    np.hypot(
                        pull[-1]["tcp"][0],
                        pull[-1]["tcp"][1],
                    )
                ),

            "lbag_carry_z":
                float(
                    pull[-1]["tcp"][2]
                ),
        }


    def do_handoff_l_paper_bag(
        self,
        person_x_mm,
        person_y_mm,
        person_z_mm,
    ):
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
            raise RuntimeError(
                "Lbag HANDOFF without PICK"
            )

        if (
            self.active_pick.get(
                "object_type"
            )
            != OBJ_L_PAPER_BAG
        ):
            raise RuntimeError(
                "active object is not L_paper_bag"
            )


        person = np.array(
            [
                float(person_x_mm),
                float(person_y_mm),
                float(person_z_mm),
            ],
            dtype=float,
        )


        # Desired = shoulder - 300 mm,
        # but existing validated reach remains capped at R400.
        handoff = (
            self.compute_handoff_target(
                person,
                600.0,
            )
        )

        angle_deg = float(
            handoff["angle_deg"]
        )

        final_r = float(
            handoff["handoff_radius"]
        )


        pull_q = np.asarray(
            self.active_pick[
                "lbag_pull_q"
            ],
            dtype=float,
        ).copy()

        pull_tcp = np.asarray(
            self.active_pick[
                "lbag_pull_tcp"
            ],
            dtype=float,
        ).copy()

        pull_r = float(
            np.hypot(
                pull_tcp[0],
                pull_tcp[1],
            )
        )

        carry_z = float(
            self.active_pick[
                "lbag_carry_z"
            ]
        )


        # We explicitly want an outward handoff extension.
        #
        # If person is so close that requested handoff radius
        # is inside the pulled position, stop BEFORE motor motion
        # instead of silently retracting.
        if final_r <= pull_r + 10.0:
            raise RuntimeError(
                "Lbag HANDOFF has no outward EXTEND room | "
                f"pull_R={pull_r:.1f} "
                f"target_R={final_r:.1f}. "
                "Driver position is too close for the requested "
                "pull->turn->extend motion."
            )


        # ----------------------------------------------------
        # TURN IN PLACE
        #
        # EXACT pulled posture:
        # J2/J3/J4/J5 held.
        # Only J1 changes.
        # ----------------------------------------------------

        turn = (
            self._lbag_v2_turn(
                pull_q,
                angle_deg,
                segments=24,
            )
        )

        turn_q = (
            turn[-1]["q"]
            .copy()
        )


        # ----------------------------------------------------
        # EXTEND
        #
        # After TURN is 100% complete,
        # extend along driver direction from pull_R -> final_R.
        # ----------------------------------------------------

        az = float(
            np.deg2rad(
                angle_deg
            )
        )

        turn_tcp_target = np.array(
            [
                pull_r
                * np.cos(az),

                pull_r
                * np.sin(az),

                carry_z,
            ],
            dtype=float,
        )

        final_target = np.array(
            [
                final_r
                * np.cos(az),

                final_r
                * np.sin(az),

                carry_z,
            ],
            dtype=float,
        )


        chain = (
            self.core.spp.create_robot_chain()
        )

        extend = (
            self._lbag_v2_line(
                chain,
                turn_tcp_target,
                final_target,
                turn_q,
                segments=20,
            )
        )


        self.get_logger().info(
            "L_paper_bag HANDOFF V2 accepted | "
            f"angle={angle_deg:+.2f} deg | "
            f"pull_R={pull_r:.1f} -> "
            f"extend_R={final_r:.1f} mm | "
            f"extend={final_r - pull_r:.1f} mm | "
            f"Z={carry_z:.1f} | "
            "horizontal | | / J5=2048"
        )


        self.ensure_motor_services()


        # 1. Rotate while staying at exact pulled radius.
        self.move_path(
            "L_paper_bag/TURN_IN_PLACE_HORIZONTAL",
            [
                p["ticks"]
                for p in turn
            ],
            profile_velocity=12,
        )


        # 2. Now actually extend the arm toward driver.
        self.move_path(
            "L_paper_bag/EXTEND_HORIZONTAL",
            [
                p["ticks"]
                for p in extend
            ],
            profile_velocity=12,
        )


        # 3. Give bag.
        self.runner.trigger(
            self.runner.open,
            "L_paper_bag/OPEN_HANDOVER",
        )


    def do_handoff_paper_bag(
        self,
        person_x_mm,
        person_y_mm,
        person_z_mm,
    ):
        # L-PAPERBAG V2 EARLY ROUTE
        if (
            self.active_pick is not None
            and self.active_pick.get(
                "object_type"
            )
            == OBJ_L_PAPER_BAG
        ):
            return self.do_handoff_l_paper_bag(
                person_x_mm,
                person_y_mm,
                person_z_mm,
            )

        if self.active_pick is None:
            raise RuntimeError(
                "HANDOFF requested without an active PICK"
            )

        if (
            self.active_pick["object_type"]
            not in (
                OBJ_PAPER_BAG,
                OBJ_L_PAPER_BAG,
            )
        ):
            raise RuntimeError(
                "active object is not a supported bag"
            )

        bag_type = (
            self.active_pick["object_type"]
        )

        person = np.array(
            [
                float(person_x_mm),
                float(person_y_mm),
                float(person_z_mm),
            ],
            dtype=float,
        )

        handoff = (
            self.compute_handoff_target(
                person,
                400.0,
            )
        )

        self.ensure_motor_services()

        # ====================================================
        # L PAPER BAG
        #
        # Handle pick was horizontal.
        # Keep that same horizontal | | posture:
        #   LIFT -> TURN -> HORIZONTAL HANDOFF
        # ====================================================

        if bag_type == OBJ_L_PAPER_BAG:

            from l_paper_bag_horizontal_plan import (
                build_handoff as build_lbag_handoff,
            )

            motion = build_lbag_handoff(
                self.active_pick[
                    "lbag_lift_q"
                ],
                handoff,
            )

            self.core.cfg.validate_arm_ticks(
                motion["turn_ticks"]
            )

            for point in motion["points"]:
                self.core.cfg.validate_arm_ticks(
                    point["ticks"]
                )

            self.get_logger().info(
                "L_paper_bag HANDOFF accepted | "
                f"angle="
                f"{handoff['angle_deg']:+.2f} deg | "
                f"radius="
                f"{handoff['handoff_radius']:.1f} mm | "
                "horizontal | | maintained"
            )

            self.runner.move(
                "L_paper_bag/HORIZONTAL_TURN",
                [
                    motion["turn_ticks"],
                ],
            )

            self.runner.move(
                "L_paper_bag/HORIZONTAL_HANDOFF",
                [
                    point["ticks"]
                    for point
                    in motion["points"]
                ],
            )

            self.runner.trigger(
                self.runner.open,
                "L_paper_bag/OPEN_HANDOVER",
            )

            return

        # ====================================================
        # PAPER BAG
        #
        # Existing TOOL-DOWN handoff.
        #
        # Generate exact tool-down IK path, then physically
        # settle at each small waypoint so one joint cannot
        # lag far enough to make | | look like / /.
        # ====================================================

        try:
            motion = (
                self.build_fullik_path(
                    self.active_pick[
                        "handoff_plan"
                    ],
                    handoff,
                    joint_blend=False,
                )
            )
        except TypeError:
            # Compatibility with older builder signature.
            motion = (
                self.build_fullik_path(
                    self.active_pick[
                        "handoff_plan"
                    ],
                    handoff,
                )
            )

        self.runner.move(
            "paper_bag/FULLIK_TURN",
            [
                motion["turn_ticks"],
            ],
        )

        # ----------------------------------------------------
        # Keep J5 fixed through TURN + EXTEND.
        # Tool-down itself is maintained by J2+J3+J4.
        # ----------------------------------------------------
        held_j5_tick = int(
            self.active_pick[
                "pick_plan"
            ]["PULL100"]["ticks"][4]
        )

        held_j5_deg = float(
            self.active_pick[
                "pick_plan"
            ]["PULL100"]["q"][4]
        )

        motion["turn_ticks"][4] = (
            held_j5_tick
        )

        if "turn_q" in motion:
            motion["turn_q"][4] = (
                held_j5_deg
            )

        for point in motion["points"]:
            point["ticks"][4] = (
                held_j5_tick
            )

            if "q" in point:
                point["q"][4] = (
                    held_j5_deg
                )

            self.core.cfg.validate_arm_ticks(
                point["ticks"]
            )

        self.core.cfg.validate_arm_ticks(
            motion["turn_ticks"]
        )

        # One continuous phase.
        #
        # Motor Control interpolates the full waypoint path
        # continuously and waits only at the final endpoint.
        self.move_path(
            "paper_bag/FULLIK_HANDOFF_SMOOTH",
            [
                point["ticks"]
                for point
                in motion["points"]
            ],
            profile_velocity=12,
        )

        self.runner.trigger(
            self.runner.open,
            "paper_bag/OPEN_HANDOVER",
        )


    def do_move_payment(
        self,
        person_x_mm,
        person_y_mm,
        person_z_mm,
    ):
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

        from payment_tooldown_plan import (
            build_payment_plan,
        )

        person = np.array(
            [
                float(person_x_mm),
                float(person_y_mm),
                float(person_z_mm),
            ],
            dtype=float,
        )

        # COMPLETE plan is calculated before motor motion.
        plan = build_payment_plan(
            person
        )

        self.ensure_motor_services()
        current = self.ensure_start_pose()

        self.get_logger().info(
            "MOVE_PAYMENT accepted | "
            f"person="
            f"({person[0]:+.1f},"
            f"{person[1]:+.1f},"
            f"{person[2]:+.1f}) | "
            f"angle={plan['angle_deg']:+.2f} deg | "
            f"wanted_R={plan['wanted_radius']:.1f} | "
            f"used_R={plan['used_radius']:.1f} | "
            f"standoff={plan['standoff']:.1f} | "
            f"current={current.tolist()}"
        )

        # START itself is not tool-down.
        # First make the tool-down preparation pose.
        self.runner.move(
            "payment/TOOLDOWN_START",
            [
                plan[
                    "TOOLDOWN_START"
                ][
                    "ticks"
                ],
            ],
        )

        # After this point tool-down must be preserved.
        #
        # TURN + APPROACH are intentionally sent as ONE
        # MoveTickPath request.  This prevents an unnecessary
        # settle/restart between the driver-direction turn and
        # the final payment approach.
        turn_approach_path = [
            plan["TURN"]["ticks"],
        ]

        turn_approach_path.extend(
            p["ticks"]
            for p in plan["APPROACH"]
        )

        self.runner.move(
            "payment/TOOLDOWN_TURN_APPROACH",
            turn_approach_path,
        )


    def do_return_home(self):
        self.ensure_motor_services()

        object_type = (
            self.active_pick["object_type"]
            if self.active_pick is not None
            else "arm"
        )

        self.get_logger().info(
            "RETURN_HOME start | "
            f"object={object_type} "
            f"goal={self.core.START_TICKS.tolist()}"
        )

        self.runner.move(
            f"{object_type}/RETURN_HOME",
            [
                self.core.START_TICKS,
            ],
        )

        # MoveTickPath success 후에도 실제 joint state를 다시 읽어서
        # START pose를 최종 확인한다.
        current = self.ensure_start_pose()

        self.get_logger().info(
            "RETURN_HOME verified | "
            f"current={current.tolist()}"
        )

        self.active_pick = None

    # --------------------------------------------------------
    # ROS service
    # --------------------------------------------------------


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

        path = (
            self.workspace_root
            / "study_arm"
            / "cup_z105_FULL_VERIFIED.json"
        )

        data = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

        if (
            data.get("name")
            != "CUP_Z105_FULL_VERIFIED"
        ):
            raise RuntimeError(
                "unexpected CUP plan name"
            )

        pickup = {}

        for phase in data["pickup"]["phases"]:
            name = str(
                phase["name"]
            )

            commands = np.asarray(
                phase["commands"],
                dtype=np.int64,
            ).reshape(-1, 5)

            pickup[name] = commands

        required = (
            "START_TO_SAFE",
            "DESCEND_D120_Z300_TO_Z105",
            "APPROACH_Z105_D120_TO_D0",
            "LIFT_D0_Z105_TO_Z185",
        )

        missing = [
            name
            for name in required
            if name not in pickup
        ]

        if missing:
            raise RuntimeError(
                "CUP pickup phases missing: "
                f"{missing}"
            )

        pull = np.asarray(
            data["pull"]["commands"],
            dtype=np.int64,
        ).reshape(-1, 5)

        paths = {
            **pickup,
            "PULL20_TO_PULL100":
                pull,
        }

        expected_goals = {
            "START_TO_SAFE":
                np.array(
                    [
                        2871,
                        798,
                        8328,
                        1975,
                        2048,
                    ],
                    dtype=np.int64,
                ),

            "DESCEND_D120_Z300_TO_Z105":
                np.array(
                    [
                        2871,
                        2237,
                        10132,
                        1326,
                        2048,
                    ],
                    dtype=np.int64,
                ),

            "APPROACH_Z105_D120_TO_D0":
                np.array(
                    [
                        2871,
                        3472,
                        8416,
                        1422,
                        2048,
                    ],
                    dtype=np.int64,
                ),

            "LIFT_D0_Z105_TO_Z185":
                np.array(
                    [
                        2871,
                        2796,
                        8047,
                        1632,
                        2048,
                    ],
                    dtype=np.int64,
                ),

            "PULL20_TO_PULL100":
                np.array(
                    [
                        2871,
                        1506,
                        9384,
                        1622,
                        2048,
                    ],
                    dtype=np.int64,
                ),
        }

        for name, commands in paths.items():

            if len(commands) == 0:
                raise RuntimeError(
                    f"{name}: empty CUP path"
                )

            if not np.all(
                commands[:, 4]
                == 2048
            ):
                raise RuntimeError(
                    f"{name}: CUP J5 changed"
                )

            if not np.array_equal(
                commands[-1],
                expected_goals[name],
            ):
                raise RuntimeError(
                    f"{name}: final tick mismatch | "
                    f"got="
                    f"{commands[-1].tolist()} | "
                    f"expected="
                    f"{expected_goals[name].tolist()}"
                )

            for ticks in commands:
                self.core.cfg.validate_arm_ticks(
                    ticks
                )

        return paths


    def ensure_cup_close_service(self):
        if not self.cup_close.wait_for_service(
            timeout_sec=5.0
        ):
            raise RuntimeError(
                "service unavailable: "
                "/motor/cup_gripper_close"
            )


    def do_pick_cup(
        self,
        detected_x_mm,
        detected_y_mm,
    ):
        """
        Dynamic CUP PICK.

        Camera/detection supplies base-frame X/Y.
        CUP grasp Z remains fixed at 105 mm.

        Complete path is calculated and validated
        before any motor request is sent.
        """

        # ----------------------------------------------------
        # Full calculation FIRST
        # ----------------------------------------------------

        plan = (
            self.build_dynamic_cup_pick_plan(
                float(detected_x_mm),
                float(detected_y_mm),
            )
        )

        # ----------------------------------------------------
        # Explicit whole-path validation
        # ----------------------------------------------------

        groups = [
            [plan["SAFE"]],
            plan["DESCEND"],
            plan["APPROACH"],
            plan["LIFT"],
            plan["PULL"],
        ]

        for group in groups:
            for point in group:

                ticks = np.asarray(
                    point["ticks"],
                    dtype=np.int64,
                )

                self.core.cfg.validate_arm_ticks(
                    ticks
                )

                if int(ticks[4]) != 2048:
                    raise RuntimeError(
                        "dynamic CUP PICK J5 changed | "
                        f"name={point['name']} | "
                        f"ticks={ticks.tolist()}"
                    )

        transport_ticks = np.asarray(
            plan["PULL_FINAL"]["ticks"],
            dtype=np.int64,
        ).copy()

        transport_q = np.asarray(
            plan["PULL_FINAL"]["q"],
            dtype=float,
        ).copy()

        # ----------------------------------------------------
        # Motor/services only AFTER full plan passed
        # ----------------------------------------------------

        self.ensure_motor_services()
        self.ensure_cup_close_service()

        current = self.ensure_start_pose()

        self.get_logger().info(
            "PICK cup accepted | "
            f"XY=({detected_x_mm:+.1f},"
            f"{detected_y_mm:+.1f}) | "
            f"R={plan['radius_mm']:.1f} | "
            f"angle={plan['angle_deg']:+.2f} | "
            f"D={plan['approach_d_mm']:.0f} | "
            f"PULL={plan['pull_distance_mm']:.0f} mm | "
            f"J3margin="
            f"{plan['min_j3_margin_tick']} | "
            f"J4margin="
            f"{plan['min_j4_margin_tick']} | "
            f"current={current.tolist()}"
        )

        # ----------------------------------------------------
        # CUP LEVEL START
        #
        # 기본 START pose는 CUP horizontal pose가 아니다.
        # 컵 영역으로 이동하기 전에 높은 START 위치에서
        # 먼저 J4를 보상해서:
        #
        #   J2 + J3 + J4 = 90 deg
        #
        # 를 만든다.
        #
        # 이 이후 SAFE / DESCEND / APPROACH / LIFT / PULL /
        # TURN / HANDOFF는 level-lock 대상으로 실행된다.
        # ----------------------------------------------------

        level_start_q = np.asarray(
            self.core.cfg.ticks_to_model_deg(
                self.core.START_TICKS
            ),
            dtype=float,
        )

        level_start_q[3] = (
            90.0
            - float(level_start_q[1])
            - float(level_start_q[2])
        )

        # CUP wrist roll
        level_start_q[4] = 0.0

        level_start_ticks = np.asarray(
            self.core.cfg.model_deg_to_ticks(
                level_start_q
            ),
            dtype=np.int64,
        )

        level_start_ticks[4] = 2048

        self.core.cfg.validate_arm_ticks(
            level_start_ticks
        )

        # OPEN = 85
        self.runner.trigger(
            self.runner.open,
            "cup/OPEN",
        )

        # START -> horizontal START
        self.runner.move(
            "cup/LEVEL_START",
            [
                level_start_ticks,
            ],
        )

        # horizontal START -> SAFE
        self.runner.move(
            "cup/DYNAMIC_SAFE",
            [
                plan["SAFE"]["ticks"],
            ],
        )

        # selected D / Z300 -> Z105
        self.runner.move(
            "cup/DYNAMIC_DESCEND",
            [
                point["ticks"]
                for point
                in plan["DESCEND"]
            ],
        )

        # selected D -> D0 / Z105
        self.runner.move(
            "cup/DYNAMIC_APPROACH",
            [
                point["ticks"]
                for point
                in plan["APPROACH"]
            ],
        )

        # CLOSE = 500
        self.runner.trigger(
            self.cup_close,
            "cup/CLOSE",
        )

        # Z105 -> Z185
        self.runner.move(
            "cup/DYNAMIC_LIFT",
            [
                point["ticks"]
                for point
                in plan["LIFT"]
            ],
        )

        # D0 -> D100
        self.runner.move(
            "cup/DYNAMIC_PULL",
            [
                point["ticks"]
                for point
                in plan["PULL"]
            ],
        )

        # Save ACTUAL dynamic transport state.
        self.active_pick = {
            "object_type":
                OBJ_CUP,

            "detected_x_mm":
                float(detected_x_mm),

            "detected_y_mm":
                float(detected_y_mm),

            "approach_d_mm":
                float(
                    plan["approach_d_mm"]
                ),

            "transport_ticks":
                transport_ticks,

            "transport_q":
                transport_q,

            "pull_distance_mm":
                float(
                    plan["pull_distance_mm"]
                ),

            "pick_plan":
                plan,
        }


    def do_handoff_cup(
        self,
        person_x_mm,
        person_y_mm,
        person_z_mm,
    ):
        """
        Dynamic CUP HANDOFF.

        actual PICK PULL100
          -> driver direction TURN
          -> horizontal Cartesian HANDOFF
          -> OPEN

        RETURN_HOME remains Main's responsibility.
        """

        if self.active_pick is None:
            raise RuntimeError(
                "CUP HANDOFF requested "
                "without active PICK"
            )

        if (
            self.active_pick[
                "object_type"
            ]
            != OBJ_CUP
        ):
            raise RuntimeError(
                "active object is not cup"
            )

        from cup_dynamic_handoff import (
            MAX_HANDOFF_RADIUS_MM,
            build_cup_handoff,
        )

        person = np.array(
            [
                float(person_x_mm),
                float(person_y_mm),
                float(person_z_mm),
            ],
            dtype=float,
        )

        # Same person target rule used by bag path.
        handoff = (
            self.compute_handoff_target(
                person,
                float(
                    MAX_HANDOFF_RADIUS_MM
                ),
            )
        )

        transport_ticks = np.asarray(
            self.active_pick[
                "transport_ticks"
            ],
            dtype=np.int64,
        ).copy()

        transport_q = np.asarray(
            self.active_pick[
                "transport_q"
            ],
            dtype=float,
        ).copy()

        # Complete calculation BEFORE motion.
        motion = build_cup_handoff(
            handoff,
            pull100_ticks=
                transport_ticks,
            pull100_q=
                transport_q,
        )

        # Validate TURN
        self.core.cfg.validate_arm_ticks(
            motion["turn_ticks"]
        )

        if int(
            motion["turn_ticks"][4]
        ) != 2048:
            raise RuntimeError(
                "CUP HANDOFF TURN J5 changed"
            )

        # Validate Cartesian handoff
        for point in motion["points"]:

            self.core.cfg.validate_arm_ticks(
                point["ticks"]
            )

            if (
                int(
                    point["ticks"][4]
                )
                != 2048
            ):
                raise RuntimeError(
                    "CUP HANDOFF J5 changed"
                )

        self.get_logger().info(
            "CUP HANDOFF accepted | "
            f"pick_XY="
            f"({self.active_pick['detected_x_mm']:+.1f},"
            f"{self.active_pick['detected_y_mm']:+.1f}) | "
            f"PULL="
            f"{self.active_pick['pull_distance_mm']:.0f} mm | "
            f"person="
            f"({person[0]:+.1f},"
            f"{person[1]:+.1f},"
            f"{person[2]:+.1f}) | "
            f"angle="
            f"{handoff['angle_deg']:+.2f} | "
            f"radius="
            f"{handoff['handoff_radius']:.1f}"
        )

        self.ensure_motor_services()

        # Dynamic driver-direction TURN
        self.runner.move(
            "cup/DYNAMIC_TURN",
            [
                motion[
                    "turn_ticks"
                ],
            ],
        )

        # Horizontal HANDOFF
        self.runner.move(
            "cup/HORIZONTAL_HANDOFF",
            [
                point["ticks"]
                for point
                in motion["points"]
            ],
        )

        # OPEN for customer handover
        self.runner.trigger(
            self.runner.open,
            "cup/OPEN_HANDOVER",
        )

    def command_cb(
        self,
        request,
        response,
    ):
        command = (
            str(request.command)
            .strip()
            .upper()
        )

        object_type = (
            str(request.object_type)
            .strip()
        )

        # Only one arm command may execute at a time.
        if not self.command_lock.acquire(
            blocking=False
        ):
            return self.fail(
                response,
                "BUSY",
                "Control is already executing another arm command",
            )

        try:
            self.get_logger().info(
                "command | "
                f"{command} "
                f"object={object_type!r} "
                f"xyz=({request.x_mm:.1f},"
                f"{request.y_mm:.1f},"
                f"{request.z_mm:.1f})"
            )

            if command == CMD_PING:
                return self.ok(
                    response,
                    "OK",
                    "drive_thru_control alive",
                )

            if command == CMD_MOVE_PAYMENT:

                self.do_move_payment(
                    request.x_mm,
                    request.y_mm,
                    request.z_mm,
                )

                return self.ok(
                    response,
                    "PAYMENT_POSE_REACHED",
                    "NFC payment pose reached",
                )

            if command == CMD_PICK:
                if object_type == OBJ_PAPER_BAG:
                    self.do_pick_paper_bag(
                        request.x_mm,
                        request.y_mm,
                    )

                    return self.ok(
                        response,
                        "PICK_DONE",
                        "paper_bag PICK + PULL100 complete",
                    )

                if object_type == OBJ_L_PAPER_BAG:
                    self.do_pick_l_paper_bag(
                        request.x_mm,
                        request.y_mm,
                    )

                    return self.ok(
                        response,
                        "PICK_DONE",
                        "L_paper_bag PICK + PULL150 complete",
                    )

                if object_type == OBJ_CUP:
                    self.do_pick_cup(
                        request.x_mm,
                        request.y_mm,
                    )
                    return self.ok(
                        response,
                        "OK",
                        "cup PICK + PULL100 complete",
                    )

                return self.fail(
                    response,
                    "UNKNOWN_OBJECT",
                    f"unsupported object_type={object_type!r}",
                )

            if command == CMD_HANDOFF:
                if (
                    self.active_pick is not None
                    and self.active_pick.get(
                        "object_type"
                    ) == OBJ_CUP
                ):
                    self.do_handoff_cup(
                        request.x_mm,
                        request.y_mm,
                        request.z_mm,
                    )
                    return self.ok(
                        response,
                        "OK",
                        "cup HANDOFF + open complete",
                    )

                active_type = (
                    object_type
                    or (
                        self.active_pick[
                            "object_type"
                        ]
                        if self.active_pick
                        is not None
                        else ""
                    )
                )

                if active_type in (
                    OBJ_PAPER_BAG,
                    OBJ_L_PAPER_BAG,
                ):
                    self.do_handoff_paper_bag(
                        request.x_mm,
                        request.y_mm,
                        request.z_mm,
                    )

                    return self.ok(
                        response,
                        "HANDOFF_DONE",
                        f"{active_type} "
                        "pose-based handoff "
                        "+ gripper open complete",
                    )

                return self.fail(
                    response,
                    "NOT_IMPLEMENTED",
                    f"HANDOFF for "
                    f"{active_type!r} "
                    "not implemented",
                )

            if command == CMD_RETURN_HOME:
                self.do_return_home()

                return self.ok(
                    response,
                    "HOME_DONE",
                    "arm returned to START",
                )

            return self.fail(
                response,
                "UNKNOWN_COMMAND",
                f"unsupported command={command!r}",
            )

        except Exception as e:
            self.get_logger().error(
                f"{command} failed: {type(e).__name__}: {e}"
            )

            return self.fail(
                response,
                "EXECUTION_ERROR",
                f"{type(e).__name__}: {e}",
            )

        finally:
            self.command_lock.release()

    def close(self):
        try:
            self.runner.destroy_node()
        except Exception:
            pass


def main(args=None):
    rclpy.init(args=args)

    node = None

    try:
        node = DriveThruControlNode()

        rclpy.spin(node)

    except KeyboardInterrupt:
        pass

    finally:
        if node is not None:
            node.close()
            node.destroy_node()

        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
