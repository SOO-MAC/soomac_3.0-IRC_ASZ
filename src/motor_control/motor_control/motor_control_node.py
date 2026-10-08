#!/usr/bin/env python3
"""IRC Motor Control Node.

Owns the Dynamixel serial port. It does not know IK, camera coordinates, ArUco,
or object geometry. It only executes motor tick trajectories and gripper ticks.
"""
from __future__ import annotations
import threading
import time
from typing import Iterable

import numpy as np
import rclpy
from dynamixel_sdk import COMM_SUCCESS, GroupSyncWrite, PacketHandler, PortHandler
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import Int32, Int32MultiArray
from std_srvs.srv import Trigger
from arm_control.srv import ExecutePickPlace, MoveToTicks, MoveTickPath
from arm_control.robot_config import *

# Drive-through cup gripper positions
CUP_GRIPPER_OPEN_TICK = 85
CUP_GRIPPER_CLOSE_TICK = 600
BAG_GRIPPER_HOLD_TICK = 2048
BAG_GRIPPER_POSITION_THRESHOLD_TICK = 40

# Drive-through motion speed overrides
#
# LEVEL_START:
#   START 자세에서 cup-horizontal 자세를 만들 때
#   주로 J4가 움직이므로 기본 8보다 빠르게.
#
# HANDOFF TURN:
#   물체 종류와 관계없이 사람 방향으로 J1 회전할 때
#   기본 속도보다 빠르게.
CUP_LEVEL_START_PROFILE_VELOCITY = 16
HANDOFF_TURN_PROFILE_VELOCITY = 20

# NFC PAYMENT only
#
# Tool-down constraint / waypoint / stream timing은 그대로 두고
# Profile Velocity만 올린다.
PAYMENT_TOOLDOWN_START_PROFILE_VELOCITY = 24
PAYMENT_TOOLDOWN_TURN_PROFILE_VELOCITY = 40
PAYMENT_TOOLDOWN_APPROACH_PROFILE_VELOCITY = 24


def signed_int32(value: int) -> int:
    value = int(value) & 0xFFFFFFFF
    return value - 0x100000000 if value >= 0x80000000 else value

def check_write(packet: PacketHandler, comm_result: int, dxl_error: int, label: str) -> None:
    if comm_result != COMM_SUCCESS:
        raise RuntimeError(f"{label} 통신 실패: {packet.getTxRxResult(comm_result)}")
    if dxl_error != 0:
        raise RuntimeError(f"{label} 패킷 오류: {packet.getRxPacketError(dxl_error)}")

def write1(port: PortHandler, packet: PacketHandler, dxl_id: int, address: int, value: int, label: str) -> None:
    comm, error = packet.write1ByteTxRx(port, dxl_id, address, int(value))
    check_write(packet, comm, error, label)

def write4(port: PortHandler, packet: PacketHandler, dxl_id: int, address: int, value: int, label: str) -> None:
    comm, error = packet.write4ByteTxRx(port, dxl_id, address, int(value) & 0xFFFFFFFF)
    check_write(packet, comm, error, label)

def read4_retry(port: PortHandler, packet: PacketHandler, dxl_id: int, address: int, label: str) -> int:
    last_comm = COMM_SUCCESS
    last_error = 0
    for attempt in range(1, ARM_READ_RETRIES + 1):
        value, comm, error = packet.read4ByteTxRx(port, dxl_id, address)
        last_comm, last_error = comm, error
        if comm == COMM_SUCCESS and error == 0:
            return int(value)
        if attempt < ARM_READ_RETRIES:
            time.sleep(ARM_READ_RETRY_DT_SEC)

    comm_text = packet.getTxRxResult(last_comm) if last_comm != COMM_SUCCESS else "COMM_SUCCESS"
    error_text = packet.getRxPacketError(last_error) if last_error else "None"
    raise RuntimeError(f"{label} 최종 실패: comm={comm_text}, packet={error_text}")

def set_torque(port: PortHandler, packet: PacketHandler, ids: Iterable[int], enabled: bool) -> None:
    value = TORQUE_ENABLE if enabled else TORQUE_DISABLE
    failures = []
    for dxl_id in ids:
        try:
            write1(
                port,
                packet,
                int(dxl_id),
                ADDR_TORQUE_ENABLE,
                value,
                f"ID {dxl_id} Torque {'ON' if enabled else 'OFF'}",
            )
        except Exception as exc:  # best-effort shutdown must continue
            failures.append(str(exc))
    if failures:
        raise RuntimeError(" | ".join(failures))

def read_arm_ticks(port: PortHandler, packet: PacketHandler) -> np.ndarray:
    return np.array(
        [
            signed_int32(
                read4_retry(
                    port,
                    packet,
                    dxl_id,
                    ADDR_PRESENT_POSITION,
                    f"ID {dxl_id} Present Position",
                )
            )
            for dxl_id in ARM_IDS
        ],
        dtype=np.int64,
    )

def quintic_smoothstep(u: float) -> float:
    """Quintic time scaling with zero velocity/acceleration at both ends."""
    u = float(np.clip(u, 0.0, 1.0))
    return 10.0 * u**3 - 15.0 * u**4 + 6.0 * u**5


def build_phase_commands(
    ticks: list[np.ndarray],
    held_j5_tick: int | None = None,
    min_commands: int = PHASE_MIN_COMMANDS,
    max_commands: int = PHASE_MAX_COMMANDS,
) -> list[np.ndarray]:
    """
    하나의 named-waypoint motion phase 전체에 quintic을 한 번 적용한다.

    - planner가 만든 중간 joint waypoint 경로는 그대로 유지
    - 시작 속도/가속도 = 0
    - 끝 속도/가속도 = 0
    - held_j5_tick이 주어지면 phase 동안 J5를 고정
    """
    if not ticks:
        return []

    path = np.asarray(
        ticks,
        dtype=np.int64,
    ).reshape(-1, 5).copy()

    if held_j5_tick is not None:
        path[:, 4] = int(held_j5_tick)

    if len(path) == 1:
        return [path[0].copy()]

    cumulative = np.zeros(len(path), dtype=float)
    cumulative[1:] = np.cumsum(
        np.linalg.norm(np.diff(path.astype(float), axis=0), axis=1)
    )
    total_length = float(cumulative[-1])

    if total_length <= 1.0e-12:
        return [path[-1].copy()]

    # 각 관절이 phase 전체에서 실제로 이동한 누적 tick 양.
    joint_travel = np.sum(
        np.abs(np.diff(path, axis=0)),
        axis=0,
    )

    required = int(
        np.max(
            np.ceil(
                joint_travel
                / PHASE_MAX_TICK_STEP
            )
        )
    )

    # 원래 planner waypoint 개수보다 적게 만들지 않는다.
    count = max(
        int(min_commands),
        int(required),
        len(path) - 1,
    )
    count = min(
        count,
        int(max_commands),
    )

    commands: list[np.ndarray] = []

    for index in range(1, count + 1):
        u = index / count
        s = quintic_smoothstep(u)
        distance = s * total_length

        upper = min(
            int(np.searchsorted(cumulative, distance, side="right")),
            len(path) - 1,
        )
        lower = max(upper - 1, 0)
        segment_length = cumulative[upper] - cumulative[lower]

        if distance <= 0.0:
            sampled = path[0]
        elif distance >= total_length:
            sampled = path[-1]
        elif segment_length <= 1.0e-12:
            sampled = path[upper]
        else:
            alpha = (distance - cumulative[lower]) / segment_length
            sampled = (
                (1.0 - alpha) * path[lower]
                + alpha * path[upper]
            )

        command = np.rint(sampled).astype(np.int64)

        if held_j5_tick is not None:
            command[4] = int(held_j5_tick)

        commands.append(command)

    commands[-1] = path[-1].copy()
    return commands

def stream_commands(
    arm_writer: ArmSyncWriter,
    commands: list[np.ndarray],
) -> None:
    """고정 주기로 command sequence를 streaming한다."""
    next_send_time = time.monotonic()

    for command in commands:
        arm_writer.send(command)

        next_send_time += ARM_STREAM_DT_SEC
        remaining = next_send_time - time.monotonic()

        if remaining > 0.0:
            time.sleep(remaining)
        else:
            next_send_time = time.monotonic()


def rotate_j5_only(
    port: PortHandler,
    packet: PacketHandler,
    arm_writer: ArmSyncWriter,
    current_ticks: np.ndarray,
    target_j5_tick: int,
) -> np.ndarray:
    """PLACE_ABOVE 도착 후 J1~J4를 유지하고 J5만 회전한다."""
    start = np.asarray(
        current_ticks,
        dtype=np.int64,
    ).reshape(5).copy()

    delta = int(target_j5_tick) - int(start[4])

    if delta == 0:
        return start

    commands = []

    for index in range(1, TRANSPORT_YAW_COMMANDS + 1):
        blend = quintic_smoothstep(index / TRANSPORT_YAW_COMMANDS)
        command = start.copy()
        command[4] = int(round(start[4] + blend * delta))
        commands.append(command)

    stream_commands(
        arm_writer,
        commands,
    )

    goal = start.copy()
    goal[4] = int(target_j5_tick)

    yaw_thresholds = np.array(
        [
            1000000,
            1000000,
            1000000,
            1000000,
            ARM_ALIGN_THRESHOLDS[4],
        ],
        dtype=np.int64,
    )

    return wait_arm_reached(
        port,
        packet,
        goal,
        timeout_sec=ARM_WAYPOINT_TIMEOUT_SEC,
        thresholds=yaw_thresholds,
    )


class ArmSyncWriter:
    """Reuse one GroupSyncWrite object for all J1~J5 commands."""

    def __init__(self, port: PortHandler, packet: PacketHandler) -> None:
        self.packet = packet
        self.group = GroupSyncWrite(port, packet, ADDR_GOAL_POSITION, 4)

    def send(self, ticks: np.ndarray) -> None:
        ticks = np.asarray(ticks, dtype=np.int64).reshape(5)

        # Final global hardware safety gate.
        # Every J1~J5 command passes this immediately before transmission.
        validate_arm_ticks(ticks)

        try:
            for dxl_id, tick in zip(ARM_IDS, ticks):
                unsigned = int(tick) & 0xFFFFFFFF
                data = [
                    unsigned & 0xFF,
                    (unsigned >> 8) & 0xFF,
                    (unsigned >> 16) & 0xFF,
                    (unsigned >> 24) & 0xFF,
                ]
                if not self.group.addParam(
                    int(dxl_id),
                    data,
                ):
                    raise RuntimeError(
                        f"GroupSyncWrite addParam 실패: "
                        f"ID={dxl_id}, tick={int(tick)}"
                    )

            comm_result = self.group.txPacket()
            if comm_result != COMM_SUCCESS:
                raise RuntimeError(
                    "GroupSyncWrite Goal Position 통신 실패: "
                    f"{self.packet.getTxRxResult(comm_result)}"
                )
        finally:
            self.group.clearParam()

def set_arm_profile_velocity(
    port: PortHandler,
    packet: PacketHandler,
    velocity: int,
) -> None:
    base = max(1, int(velocity))

    for index, dxl_id in enumerate(ARM_IDS):
        applied = base

        if index == 1:  # J2
            applied = max(
                1,
                int(round(base * J2_PROFILE_VELOCITY_SCALE)),
            )
        elif index == 2:  # J3
            applied = max(
                1,
                int(round(base * J3_PROFILE_VELOCITY_SCALE)),
            )

        write4(
            port,
            packet,
            dxl_id,
            ADDR_PROFILE_VELOCITY,
            applied,
            f"ID {dxl_id} Profile Velocity={applied}",
        )


def set_cup_synchronized_profile_velocity(
    port: PortHandler,
    packet: PacketHandler,
    velocity: int,
    ticks_path,
) -> np.ndarray:
    """
    CUP level transport 전용.

    기존 속도 상한:
      J1 = base
      J2 = base * J2_PROFILE_VELOCITY_SCALE
      J3 = base * J3_PROFILE_VELOCITY_SCALE
      J4 = base
      J5 = base

    이 상한은 그대로 유지한다.

    대신 phase 전체 joint travel을 이용해서
    모든 moving joint가 비슷한 시간에 움직이도록
    Profile Velocity를 비례 배분한다.

    따라서 5:1 감속기 보정은 없애지 않는다.
    """

    base_velocity = max(
        1,
        int(velocity),
    )

    path = np.asarray(
        ticks_path,
        dtype=float,
    ).reshape(-1, 5)

    velocity_caps = np.array(
        [
            float(base_velocity),

            float(
                max(
                    1,
                    int(
                        round(
                            base_velocity
                            * J2_PROFILE_VELOCITY_SCALE
                        )
                    ),
                )
            ),

            float(
                max(
                    1,
                    int(
                        round(
                            base_velocity
                            * J3_PROFILE_VELOCITY_SCALE
                        )
                    ),
                )
            ),

            float(base_velocity),
            float(base_velocity),
        ],
        dtype=float,
    )

    if len(path) <= 1:
        joint_travel = np.zeros(
            5,
            dtype=float,
        )
    else:
        # 단순 시작-끝 차이가 아니라
        # 실제 phase 전체 이동량 사용.
        joint_travel = np.sum(
            np.abs(
                np.diff(
                    path,
                    axis=0,
                )
            ),
            axis=0,
        )

    moving = (
        joint_travel
        > 0.5
    )

    applied = np.ones(
        5,
        dtype=np.int64,
    )

    if np.any(moving):

        # 각 축이 자신의 기존 속도 상한을
        # 넘지 않으면서 동시에 끝나도록
        # 필요한 공통 duration 계산.
        duration = float(
            np.max(
                joint_travel[moving]
                / velocity_caps[moving]
            )
        )

        if duration <= 0.0:
            raise RuntimeError(
                "invalid CUP synchronized duration"
            )

        for index in range(5):

            if not moving[index]:
                applied[index] = 1
                continue

            value = int(
                round(
                    joint_travel[index]
                    / duration
                )
            )

            value = max(
                1,
                value,
            )

            value = min(
                value,
                int(
                    round(
                        velocity_caps[index]
                    )
                ),
            )

            applied[index] = value

    for dxl_id, value in zip(
        ARM_IDS,
        applied,
    ):

        write4(
            port,
            packet,
            dxl_id,
            ADDR_PROFILE_VELOCITY,
            int(value),
            (
                f"ID {dxl_id} "
                f"CUP Sync Profile Velocity="
                f"{int(value)}"
            ),
        )

    return applied


def set_tooldown_handoff_profile_velocity(
    port: PortHandler,
    packet: PacketHandler,
    velocity: int,
) -> None:
    """
    paper_bag tool-down EXTEND 전용 profile.

    J2/J3/J4가 동시에 tool-down 자세를 따라갈 때
    J4가 뒤처지는 현상을 줄이기 위한 velocity cap.
    """
    base = max(1, int(velocity))

    values = [
        base,       # J1
        base * 5,   # J2
        base * 5,   # J3
        base * 2,   # J4
        base,       # J5
    ]

    for dxl_id, applied in zip(
        ARM_IDS,
        values,
    ):
        write4(
            port,
            packet,
            dxl_id,
            ADDR_PROFILE_VELOCITY,
            int(applied),
            (
                f"ID {dxl_id} "
                f"ToolDown Profile Velocity={applied}"
            ),
        )


def set_stream_tracking_profile_velocity(
    port: PortHandler,
    packet: PacketHandler,
    velocity: int,
    compensate_reduction: bool = False,
) -> None:
    """
    Continuous streamed path 전용 tracking profile.

    경로 진행 속도는 stream waypoint가 결정한다.
    여기서는 J1~J4가 서로 뒤처지지 않도록
    충분한 velocity cap만 제공한다.
    """

    base = max(1, int(velocity))

    tracking = max(
        60,
        base * 5,
    )

    values = [
        tracking,  # J1
        max(1, int(round(tracking * J2_PROFILE_VELOCITY_SCALE))) if compensate_reduction else tracking,
        max(1, int(round(tracking * J3_PROFILE_VELOCITY_SCALE))) if compensate_reduction else tracking,
        tracking,  # J4
        base,      # J5
    ]

    for dxl_id, applied in zip(
        ARM_IDS,
        values,
    ):
        write4(
            port,
            packet,
            int(dxl_id),
            ADDR_PROFILE_VELOCITY,
            int(applied),
            (
                f"ID {dxl_id} "
                f"Stream Tracking Velocity={applied}"
            ),
        )


def wait_arm_reached(
    port: PortHandler,
    packet: PacketHandler,
    goal_ticks: np.ndarray,
    timeout_sec: float = ARM_WAYPOINT_TIMEOUT_SEC,
    thresholds: np.ndarray = ARM_POSITION_THRESHOLDS,
) -> np.ndarray:
    goal_ticks = np.asarray(goal_ticks, dtype=np.int64).reshape(5)
    thresholds = np.asarray(thresholds, dtype=np.int64).reshape(5)
    deadline = time.monotonic() + float(timeout_sec)
    last_ticks = read_arm_ticks(port, packet)

    while time.monotonic() < deadline:
        last_ticks = read_arm_ticks(port, packet)
        errors = goal_ticks - last_ticks
        if np.all(np.abs(errors) <= thresholds):
            return last_ticks
        time.sleep(0.05)

    errors = goal_ticks - last_ticks
    raise RuntimeError(
        "Arm waypoint 도달 시간 초과 | "
        f"goal={goal_ticks.tolist()} | present={last_ticks.tolist()} | "
        f"error={errors.tolist()} | limit={thresholds.tolist()}"
    )


def wait_cup_level_reached(
    port: PortHandler,
    packet: PacketHandler,
    goal_ticks: np.ndarray,
    timeout_sec: float = ARM_WAYPOINT_TIMEOUT_SEC,
    level_target_deg: float = 90.0,
    level_tolerance_deg: float = 5.0,
) -> np.ndarray:
    """
    CUP 전용 도착 판정.

    단순 joint tick 오차뿐 아니라 실제 현재 자세의

        J2 + J3 + J4 = 90 deg

    조건까지 만족할 때만 완료로 인정한다.

    따라서 목표점에서는 수평인데 J4 등이 아직 따라오는 중인
    상태를 다음 phase로 넘기지 않는다.
    """

    goal_ticks = np.asarray(
        goal_ticks,
        dtype=np.int64,
    ).reshape(5)

    deadline = (
        time.monotonic()
        + float(timeout_sec)
    )

    last_ticks = read_arm_ticks(
        port,
        packet,
    )

    last_level_sum_deg = float("nan")
    last_level_error_deg = float("inf")

    while time.monotonic() < deadline:

        last_ticks = read_arm_ticks(
            port,
            packet,
        )

        errors = (
            goal_ticks
            - last_ticks
        )

        q_deg = np.asarray(
            ticks_to_model_deg(
                last_ticks
            ),
            dtype=float,
        )

        last_level_sum_deg = float(
            q_deg[1]
            + q_deg[2]
            + q_deg[3]
        )

        last_level_error_deg = abs(
            last_level_sum_deg
            - float(level_target_deg)
        )

        cup_position_thresholds = (
            ARM_POSITION_THRESHOLDS.copy()
        )

        # CUP handoff TURN:
        # J1 has small steady-state/backlash residual.
        # Keep every other joint threshold unchanged.
        cup_position_thresholds[0] = max(
            int(cup_position_thresholds[0]),
            30,
        )

        position_ok = bool(
            np.all(
                np.abs(errors)
                <= cup_position_thresholds
            )
        )

        level_ok = (
            last_level_error_deg
            <= float(level_tolerance_deg)
        )

        if position_ok and level_ok:
            return last_ticks

        time.sleep(0.05)

    errors = (
        goal_ticks
        - last_ticks
    )

    raise RuntimeError(
        "CUP level settle timeout | "
        f"goal={goal_ticks.tolist()} | "
        f"present={last_ticks.tolist()} | "
        f"error={errors.tolist()} | "
        f"sum234={last_level_sum_deg:.3f} deg | "
        f"level_error={last_level_error_deg:.3f} deg | "
        f"level_limit={level_tolerance_deg:.3f} deg"
    )


def move_arm_direct(
    port: PortHandler,
    packet: PacketHandler,
    arm_writer: ArmSyncWriter,
    current_ticks: np.ndarray,
    goal_ticks: np.ndarray,
    *,
    profile_velocity: int,
    timeout_sec: float,
    min_commands: int = 1,
    skip_if_reached: bool = False,
    reset_schedule_when_late: bool = False,
) -> np.ndarray:
    """현재 tick에서 목표 tick까지 quintic direct move를 수행한다."""
    current = np.asarray(current_ticks, dtype=np.int64).reshape(5)
    goal = np.asarray(goal_ticks, dtype=np.int64).reshape(5)
    delta = goal - current
    max_delta = int(np.max(np.abs(delta)))

    if skip_if_reached and max_delta == 0:
        return current.copy()

    set_arm_profile_velocity(
        port,
        packet,
        profile_velocity,
    )

    command_count = max(
        int(min_commands),
        int(np.ceil(max_delta / DIRECT_MAX_TICK_STEP)),
    )

    next_send_time = time.monotonic()

    for index in range(1, command_count + 1):
        blend = quintic_smoothstep(index / command_count)
        command = np.rint(
            current + blend * delta
        ).astype(np.int64)

        arm_writer.send(command)

        next_send_time += DIRECT_COMMAND_DT_SEC
        remaining = next_send_time - time.monotonic()

        if remaining > 0.0:
            time.sleep(remaining)
        elif reset_schedule_when_late:
            next_send_time = time.monotonic()

    return wait_arm_reached(
        port,
        packet,
        goal,
        timeout_sec=timeout_sec,
        thresholds=ARM_POSITION_THRESHOLDS,
    )


def setup_arm(port: PortHandler, packet: PacketHandler, arm_writer: ArmSyncWriter) -> np.ndarray:
    # Torque OFF is required to change Operating Mode.
    set_torque(port, packet, ARM_IDS, False)
    for dxl_id in ARM_IDS:
        write1(
            port,
            packet,
            dxl_id,
            ADDR_OPERATING_MODE,
            OP_EXTENDED_POSITION,
            f"ID {dxl_id} Extended Position Mode",
        )

    set_arm_profile_velocity(port, packet, START_PROFILE_VELOCITY)
    present = read_arm_ticks(port, packet)

    # Prevent a jump when torque is enabled: current position becomes the goal.
    arm_writer.send(present)
    set_torque(port, packet, ARM_IDS, True)
    time.sleep(0.2)
    return present


def move_to_initial_start(
    port: PortHandler,
    packet: PacketHandler,
    arm_writer: ArmSyncWriter,
    current_ticks: np.ndarray,
) -> np.ndarray:
    """
    Motor Control Node 시작 시 현재 실제 자세에서 ASSUMED_START_TICKS까지
    기존 standalone Dynamixel demo와 동일한 방식으로 부드럽게 이동한다.

    순서:
      1. setup_arm()에서 현재 위치를 Goal Position으로 기록한 뒤 Torque ON
      2. 현재 tick과 START tick의 차이를 검사
      3. quintic time scaling으로 START까지 이동
      4. wait_arm_reached()로 실제 도착 확인
    """
    current_ticks = np.asarray(
        current_ticks,
        dtype=np.int64,
    ).reshape(5)

    goal_ticks = np.asarray(
        ASSUMED_START_TICKS,
        dtype=np.int64,
    ).reshape(5)

    delta = goal_ticks - current_ticks

    print("\n" + "=" * 88)
    print("MOTOR CONTROL INITIAL START MOVE")
    print("=" * 88)

    for index, (now, goal, difference) in enumerate(
        zip(current_ticks, goal_ticks, delta),
        start=1,
    ):
        print(
            f"J{index}: "
            f"current={int(now)}, "
            f"start={int(goal)}, "
            f"delta={int(difference):+d}"
        )

    # 기존 standalone demo의 초기 이동 안전 검사 유지.
    if np.any(
        np.abs(delta)
        > START_MAX_ABS_DELTA_TICKS
    ):
        raise RuntimeError(
            "현재 위치와 START 사이 tick 차이가 안전 한도를 초과했습니다. "
            f"delta={delta.tolist()}, "
            f"limit={START_MAX_ABS_DELTA_TICKS.tolist()}"
        )

    max_delta = int(np.max(np.abs(delta)))

    if max_delta == 0:
        print(
            "이미 초기 START 위치입니다: "
            f"{goal_ticks.tolist()}"
        )
        return current_ticks.copy()

    count = max(
        2,
        int(np.ceil(max_delta / DIRECT_MAX_TICK_STEP)),
    )

    print(
        "초기 START 이동 시작 | "
        f"goal={goal_ticks.tolist()} | "
        f"commands={count} | "
        f"profile_velocity={START_PROFILE_VELOCITY}"
    )

    reached = move_arm_direct(
        port,
        packet,
        arm_writer,
        current_ticks,
        goal_ticks,
        profile_velocity=START_PROFILE_VELOCITY,
        timeout_sec=DIRECT_TIMEOUT_SEC,
        min_commands=2,
        skip_if_reached=True,
        reset_schedule_when_late=True,
    )

    print(
        "초기 START 위치 도착: "
        f"{reached.tolist()}"
    )

    return reached

def read_gripper_position(
    port: PortHandler,
    packet: PacketHandler,
) -> int:
    raw_position = read4_retry(
        port,
        packet,
        GRIPPER_ID,
        ADDR_PRESENT_POSITION,
        "Gripper Present Position",
    )
    return signed_int32(raw_position)

def setup_gripper(
    port: PortHandler,
    packet: PacketHandler,
) -> None:
    current_position = read_gripper_position(
        port,
        packet,
    )

    set_torque(
        port,
        packet,
        (GRIPPER_ID,),
        False,
    )

    write1(
        port,
        packet,
        GRIPPER_ID,
        ADDR_OPERATING_MODE,
        OP_POSITION_CONTROL,
        "Gripper Position Control Mode",
    )

    write4(
        port,
        packet,
        GRIPPER_ID,
        ADDR_PROFILE_VELOCITY,
        GRIPPER_PROFILE_VELOCITY,
        "Gripper Profile Velocity",
    )

    safe_current_position = int(
        np.clip(
            current_position,
            GRIPPER_MIN_TICK,
            GRIPPER_MAX_TICK,
        )
    )

    write4(
        port,
        packet,
        GRIPPER_ID,
        ADDR_GOAL_POSITION,
        safe_current_position,
        "Gripper current position as goal",
    )

    set_torque(
        port,
        packet,
        (GRIPPER_ID,),
        True,
    )
    time.sleep(0.2)

def send_gripper_position(
    port: PortHandler,
    packet: PacketHandler,
    goal_tick: int,
) -> None:
    goal_tick = int(
        np.clip(
            goal_tick,
            GRIPPER_MIN_TICK,
            GRIPPER_MAX_TICK,
        )
    )

    write4(
        port,
        packet,
        GRIPPER_ID,
        ADDR_GOAL_POSITION,
        goal_tick,
        f"Gripper Goal Position={goal_tick}",
    )

def wait_gripper_reached(
    port: PortHandler,
    packet: PacketHandler,
    goal_tick: int,
    threshold_tick: int = GRIPPER_POSITION_THRESHOLD_TICK,
    timeout_sec: float = GRIPPER_MOVE_TIMEOUT_SEC,
) -> int:
    goal_tick = int(goal_tick)
    deadline = time.monotonic() + float(timeout_sec)
    last_position = read_gripper_position(
        port,
        packet,
    )

    while time.monotonic() < deadline:
        last_position = read_gripper_position(
            port,
            packet,
        )
        error = goal_tick - last_position


        if abs(error) <= int(threshold_tick):
            time.sleep(GRIPPER_SETTLE_SEC)
            return last_position

        time.sleep(GRIPPER_SAMPLE_DT_SEC)

    raise RuntimeError(
        "Gripper position 도달 시간 초과 | "
        f"goal={goal_tick}, "
        f"last={last_position}, "
        f"threshold={threshold_tick}"
    )


def run_tick_phase(
    port: PortHandler,
    packet: PacketHandler,
    arm_writer: ArmSyncWriter,
    ticks_path: list[np.ndarray],
    start_index: int,
    goal_index: int,
    actual_ticks: np.ndarray,
    *,
    thresholds: np.ndarray | None = None,
    held_j5_tick: int | None = None,
    min_commands: int | None = None,
    max_commands: int | None = None,
) -> np.ndarray:
    """한 arm phase의 path 생성 -> streaming -> 목표 도달 확인을 공통 수행한다."""
    kwargs = {}
    if held_j5_tick is not None:
        kwargs["held_j5_tick"] = int(held_j5_tick)
    if min_commands is not None:
        kwargs["min_commands"] = int(min_commands)
    if max_commands is not None:
        kwargs["max_commands"] = int(max_commands)

    phase_path = [
        np.asarray(ticks_path[index], dtype=np.int64).copy()
        for index in range(start_index, goal_index + 1)
    ]
    phase_path[0] = np.asarray(
        actual_ticks,
        dtype=np.int64,
    ).reshape(5).copy()

    commands = build_phase_commands(
        phase_path,
        **kwargs,
    )
    stream_commands(
        arm_writer,
        commands,
    )

    goal = np.asarray(
        ticks_path[goal_index],
        dtype=np.int64,
    ).copy()

    if held_j5_tick is not None:
        goal[4] = int(held_j5_tick)

    return wait_arm_reached(
        port,
        packet,
        goal,
        thresholds=(
            ARM_POSITION_THRESHOLDS
            if thresholds is None
            else thresholds
        ),
    )


def execute_tick_path(
    port: PortHandler,
    packet: PacketHandler,
    arm_writer: ArmSyncWriter,
    ticks_path: list[np.ndarray],
    waypoint_names: list[str],
    gripper_open_tick: int,
    gripper_close_tick: int,
) -> np.ndarray:
    if len(ticks_path) != len(waypoint_names):
        raise ValueError(
            "ticks_path and waypoint_names length mismatch"
        )

    index_map = {
        name: index
        for index, name in enumerate(waypoint_names)
        if name
    }

    required = (
        "START",
        "PICK_ABOVE",
        "PICK",
        "PICK_RETURN",
        "PLACE_ABOVE",
        "PLACE",
        "PLACE_RETURN",
    )

    missing = [
        name
        for name in required
        if name not in index_map
    ]

    if missing:
        raise ValueError(
            f"required waypoint missing: {missing}"
        )

    set_arm_profile_velocity(
        port,
        packet,
        ARM_PROFILE_VELOCITY,
    )

    actual_ticks = read_arm_ticks(
        port,
        packet,
    )

    start_i = index_map["START"]
    pick_above_i = index_map["PICK_ABOVE"]
    pick_i = index_map["PICK"]
    pick_return_i = index_map["PICK_RETURN"]
    place_above_i = index_map["PLACE_ABOVE"]
    place_i = index_map["PLACE"]
    place_return_i = index_map["PLACE_RETURN"]

    # PICK_ABOVE
    send_gripper_position(
        port,
        packet,
        gripper_open_tick,
    )
    wait_gripper_reached(
        port,
        packet,
        gripper_open_tick,
    )

    start_j5 = int(actual_ticks[4])
    j5_free_thresholds = ARM_ALIGN_THRESHOLDS.copy()
    j5_free_thresholds[4] = 1_000_000

    actual_ticks = run_tick_phase(
        port,
        packet,
        arm_writer,
        ticks_path,
        start_i,
        pick_above_i,
        actual_ticks,
        thresholds=j5_free_thresholds,
        held_j5_tick=start_j5,
    )

    actual_ticks = rotate_j5_only(
        port,
        packet,
        arm_writer,
        actual_ticks,
        int(ticks_path[pick_above_i][4]),
    )

    # PICK
    actual_ticks = run_tick_phase(
        port,
        packet,
        arm_writer,
        ticks_path,
        pick_above_i,
        pick_i,
        actual_ticks,
    )

    send_gripper_position(
        port,
        packet,
        gripper_close_tick,
    )
    wait_gripper_reached(
        port,
        packet,
        gripper_close_tick,
    )

    # PICK_RETURN
    actual_ticks = run_tick_phase(
        port,
        packet,
        arm_writer,
        ticks_path,
        pick_i,
        pick_return_i,
        actual_ticks,
        thresholds=ARM_ALIGN_THRESHOLDS,
    )

    # PICK_RETURN -> PLACE_ABOVE
    held_j5 = int(actual_ticks[4])

    actual_ticks = run_tick_phase(
        port,
        packet,
        arm_writer,
        ticks_path,
        pick_return_i,
        place_above_i,
        actual_ticks,
        thresholds=j5_free_thresholds,
        held_j5_tick=held_j5,
        min_commands=TRANSPORT_MIN_COMMANDS,
        max_commands=TRANSPORT_MAX_COMMANDS,
    )

    actual_ticks = rotate_j5_only(
        port,
        packet,
        arm_writer,
        actual_ticks,
        int(ticks_path[place_above_i][4]),
    )

    # PLACE
    actual_ticks = run_tick_phase(
        port,
        packet,
        arm_writer,
        ticks_path,
        place_above_i,
        place_i,
        actual_ticks,
    )

    send_gripper_position(
        port,
        packet,
        gripper_open_tick,
    )
    wait_gripper_reached(
        port,
        packet,
        gripper_open_tick,
    )

    # PLACE_RETURN
    return run_tick_phase(
        port,
        packet,
        arm_writer,
        ticks_path,
        place_i,
        place_return_i,
        actual_ticks,
        thresholds=ARM_ALIGN_THRESHOLDS,
    )


class MotorControlNode(Node):
    def __init__(self) -> None:
        super().__init__("motor_control_node")

        self.motion_lock = threading.RLock()
        self.callback_group = MutuallyExclusiveCallbackGroup()

        self.port = PortHandler(DEVICENAME)
        self.packet = PacketHandler(PROTOCOL_VERSION)

        self.arm_writer: ArmSyncWriter | None = None
        self.hardware_ready = False

        self.joint_pub = self.create_publisher(
            Int32MultiArray,
            "/motor/joint_state",
            10,
        )

        self.gripper_pub = self.create_publisher(
            Int32,
            "/motor/gripper_state",
            10,
        )

        self.move_srv = self.create_service(
            MoveToTicks,
            "/motor/move_to_ticks",
            self.handle_move_to_ticks,
            callback_group=self.callback_group,
        )

        self.execute_srv = self.create_service(
            ExecutePickPlace,
            "/arm/execute_pick_place",
            self.handle_execute,
            callback_group=self.callback_group,
        )

        self.off_srv = self.create_service(
            Trigger,
            "/motor/torque_off",
            self.handle_torque_off,
            callback_group=self.callback_group,
        )

        self.cup_gripper_open_srv = self.create_service(
            Trigger,
            "/motor/cup_gripper_open",
            self.handle_cup_gripper_open,
            callback_group=self.callback_group,
        )

        self.cup_gripper_close_srv = self.create_service(
            Trigger,
            "/motor/cup_gripper_close",
            self.handle_cup_gripper_close,
            callback_group=self.callback_group,
        )

        self.move_tick_path_srv = self.create_service(
            MoveTickPath,
            "/motor/move_tick_path",
            self.handle_move_tick_path,
            callback_group=self.callback_group,
        )

        self.bag_gripper_hold_srv = self.create_service(
            Trigger,
            "/motor/bag_gripper_hold",
            self.handle_bag_gripper_hold,
            callback_group=self.callback_group,
        )

        self.state_timer = self.create_timer(
            0.2,
            self.publish_state,
        )

        self.initialize_hardware()

        self.get_logger().info(
            "Motor Control Node ready"
        )

    def initialize_hardware(self) -> None:
        if not self.port.openPort():
            raise RuntimeError(
                f"cannot open {DEVICENAME}"
            )

        if not self.port.setBaudRate(BAUDRATE):
            raise RuntimeError(
                f"baudrate failed {BAUDRATE}"
            )

        self.arm_writer = ArmSyncWriter(
            self.port,
            self.packet,
        )

        current_ticks = setup_arm(
            self.port,
            self.packet,
            self.arm_writer,
        )

        self.get_logger().info(
            "Move to START | "
            f"current={current_ticks.tolist()} | "
            f"goal={ASSUMED_START_TICKS.tolist()}"
        )

        move_to_initial_start(
            self.port,
            self.packet,
            self.arm_writer,
            current_ticks,
        )

        setup_gripper(
            self.port,
            self.packet,
        )

        send_gripper_position(
            self.port,
            self.packet,
            DEFAULT_GRIPPER_OPEN_TICK,
        )

        wait_gripper_reached(
            self.port,
            self.packet,
            DEFAULT_GRIPPER_OPEN_TICK,
        )

        self.hardware_ready = True

    def publish_state(self) -> None:
        if not self.hardware_ready:
            return

        # Motion callback이 포트를 쓰는 동안에는 이번 publish를 건너뛴다.
        if not self.motion_lock.acquire(blocking=False):
            return

        try:
            joints = read_arm_ticks(
                self.port,
                self.packet,
            )

            gripper = read_gripper_position(
                self.port,
                self.packet,
            )

            joint_msg = Int32MultiArray()
            joint_msg.data = [
                int(value)
                for value in joints
            ]
            self.joint_pub.publish(joint_msg)

            gripper_msg = Int32()
            gripper_msg.data = int(gripper)
            self.gripper_pub.publish(gripper_msg)

        except Exception:
            # State topic은 보조 피드백이므로 일시적인 read 실패로
            # motor service 자체를 중단하지 않는다.
            pass

        finally:
            self.motion_lock.release()

    def handle_move_to_ticks(
        self,
        request: MoveToTicks.Request,
        response: MoveToTicks.Response,
    ) -> MoveToTicks.Response:
        try:
            with self.motion_lock:
                goal = np.asarray(
                    request.goal_ticks,
                    dtype=np.int64,
                ).reshape(5)

                current = read_arm_ticks(
                    self.port,
                    self.packet,
                )

                velocity = (
                    int(request.profile_velocity)
                    if int(request.profile_velocity) > 0
                    else DIRECT_PROFILE_VELOCITY
                )

                timeout = (
                    float(request.timeout_sec)
                    if float(request.timeout_sec) > 0.0
                    else DIRECT_TIMEOUT_SEC
                )

                reached = move_arm_direct(
                    self.port,
                    self.packet,
                    self.arm_writer,
                    current,
                    goal,
                    profile_velocity=velocity,
                    timeout_sec=timeout,
                )

                response.success = True
                response.message = (
                    f"{request.label} reached"
                )
                response.reached_ticks = [
                    int(value)
                    for value in reached
                ]

        except Exception as error:
            response.success = False
            response.message = str(error)
            response.reached_ticks = []

        return response

    def handle_move_tick_path(
        self,
        request: MoveTickPath.Request,
        response: MoveTickPath.Response,
    ) -> MoveTickPath.Response:
        try:
            with self.motion_lock:

                point_count = int(request.point_count)

                flat_ticks = np.asarray(
                    request.joint_ticks,
                    dtype=np.int64,
                )

                if point_count <= 0:
                    raise ValueError(
                        f"point_count must be > 0: {point_count}"
                    )

                if point_count > 200:
                    raise ValueError(
                        f"too many path points: {point_count} > 200"
                    )

                expected_size = point_count * 5

                if flat_ticks.size != expected_size:
                    raise ValueError(
                        "joint_ticks size mismatch | "
                        f"point_count={point_count} | "
                        f"expected={expected_size} | "
                        f"actual={flat_ticks.size}"
                    )

                path = [
                    row.copy()
                    for row in flat_ticks.reshape(
                        point_count,
                        5,
                    )
                ]

                # --------------------------------------------------
                # Path safety validation
                #
                # Normal motions:
                #   use the current global robot_config joint limits.
                #
                # Recovered CUP_SMOOTH path:
                #   this is the physically-tested 2026-09-17 cup path.
                #   It intentionally used:
                #     J3 up to about 142.1 deg / <= 10300 tick
                #     J4 down to about -55.5 deg
                #     J5 fixed exactly at 2048
                #
                # Do NOT widen the global limits because bag motions
                # rely on the current robot_config limits.
                # --------------------------------------------------

                is_recovered_cup_smooth = str(
                    request.label
                ).startswith("CUP_SMOOTH/")

                lower = np.asarray(
                    JOINT_LIMIT_LOWER_DEG,
                    dtype=float,
                )

                upper = np.asarray(
                    JOINT_LIMIT_UPPER_DEG,
                    dtype=float,
                )

                for index, ticks in enumerate(path):
                    ticks = np.asarray(
                        ticks,
                        dtype=np.int64,
                    )

                    q_deg = np.asarray(
                        ticks_to_model_deg(ticks),
                        dtype=float,
                    )

                    if is_recovered_cup_smooth:
                        # J1
                        if not (-185.0 <= q_deg[0] <= 185.0):
                            raise ValueError(
                                f"CUP path[{index}] J1 limit exceeded | "
                                f"q={q_deg.tolist()} | "
                                f"ticks={ticks.tolist()}"
                            )

                        # J2
                        if not (-90.0 <= q_deg[1] <= 90.0):
                            raise ValueError(
                                f"CUP path[{index}] J2 limit exceeded | "
                                f"q={q_deg.tolist()} | "
                                f"ticks={ticks.tolist()}"
                            )

                        # Historical cup-path J3 safety limit
                        # lower bound remains 0 deg;
                        # only the old tested upper range is extended.
                        if q_deg[2] < 0.0 or int(ticks[2]) > 10300:
                            raise ValueError(
                                f"CUP path[{index}] J3 historical limit exceeded | "
                                f"q={q_deg[2]:.3f} deg | "
                                f"tick={int(ticks[2])}"
                            )

                        # Historical tested J4 range + normal upper bound
                        if not (-60.0 <= q_deg[3] <= 140.0):
                            raise ValueError(
                                f"CUP path[{index}] J4 limit exceeded | "
                                f"q={q_deg.tolist()} | "
                                f"ticks={ticks.tolist()}"
                            )

                        # Cup path must never rotate J5
                        if int(ticks[4]) != 2048:
                            raise ValueError(
                                f"CUP path[{index}] J5 must stay 2048 | "
                                f"ticks={ticks.tolist()}"
                            )

                    else:
                        if (
                            np.any(q_deg < lower)
                            or np.any(q_deg > upper)
                        ):
                            raise ValueError(
                                f"path[{index}] joint limit exceeded | "
                                f"q={q_deg.tolist()} | "
                                f"ticks={ticks.tolist()}"
                            )

                # 예전 MoveTickPath 동작:
                # 현재 실제 위치를 path 맨 앞에 붙인다.
                current = read_arm_ticks(
                    self.port,
                    self.packet,
                )

                full_path = [
                    current.copy(),
                    *[
                        np.asarray(
                            ticks,
                            dtype=np.int64,
                        ).copy()
                        for ticks in path
                    ],
                ]

                velocity = (
                    int(request.profile_velocity)
                    if int(request.profile_velocity) > 0
                    else DIRECT_PROFILE_VELOCITY
                )

                timeout = (
                    float(request.timeout_sec)
                    if float(request.timeout_sec) > 0.0
                    else DIRECT_TIMEOUT_SEC
                )

                label_text = str(
                    request.label
                )

                # --------------------------------------------------
                # Drive-through phase-specific speed overrides
                # --------------------------------------------------

                # CUP 수평 준비자세:
                # START -> LEVEL_START에서 J4가 너무 느렸으므로
                # 기본 profile velocity보다 빠르게 사용한다.
                if (
                    label_text
                    == "cup/LEVEL_START"
                ):
                    velocity = max(
                        int(velocity),
                        CUP_LEVEL_START_PROFILE_VELOCITY,
                    )

                # 모든 물체 공통 HANDOFF 방향 회전.
                #
                # 현재 drive-through의 회전 phase들은
                # label에 TURN을 포함한다.
                # cup/DYNAMIC_TURN 포함.
                if (
                    "TURN"
                    in label_text.upper()
                    and not label_text.startswith("payment/")
                ):
                    velocity = max(
                        int(velocity),
                        HANDOFF_TURN_PROFILE_VELOCITY,
                    )

                # --------------------------------------------------
                # NFC PAYMENT-only speed overrides
                #
                # IMPORTANT:
                #   velocity 값만 높인다.
                #   tool-down synchronized velocity,
                #   sum234=180 검사,
                #   J5=2048 검사,
                #   settle 검사는 그대로 유지한다.
                # --------------------------------------------------

                if (
                    label_text
                    == "payment/TOOLDOWN_START"
                ):
                    velocity = max(
                        int(velocity),
                        PAYMENT_TOOLDOWN_START_PROFILE_VELOCITY,
                    )

                elif (
                    label_text
                    in (
                        "payment/TOOLDOWN_TURN",
                        "payment/TOOLDOWN_TURN_APPROACH",
                    )
                ):
                    velocity = max(
                        int(velocity),
                        PAYMENT_TOOLDOWN_TURN_PROFILE_VELOCITY,
                    )

                elif (
                    label_text
                    == "payment/TOOLDOWN_APPROACH"
                ):
                    velocity = max(
                        int(velocity),
                        PAYMENT_TOOLDOWN_APPROACH_PROFILE_VELOCITY,
                    )

                # LEVEL_START는 아직 비수평 START에서
                # 수평 자세로 전환하는 준비 동작.
                #
                # RETURN_HOME은 이미 컵을 전달한 뒤이므로
                # level lock 대상에서 제외.
                cup_level_motion = (
                    label_text.startswith("cup/")
                    and not label_text.endswith(
                        "/LEVEL_START"
                    )
                    and not label_text.endswith(
                        "/RETURN_HOME"
                    )
                )

                payment_tooldown_motion = (
                    label_text
                    in (
                        "payment/TOOLDOWN_TURN",
                        "payment/TOOLDOWN_APPROACH",
                        "payment/TOOLDOWN_TURN_APPROACH",
                    )
                )

                bag_tooldown_motion = label_text == "paper_bag/FULLIK_HANDOFF_SMOOTH"

                if cup_level_motion or payment_tooldown_motion:

                    # CUP:
                    # J2/J3/J4가 phase 동안 함께 따라오도록
                    # 계산된 synchronized Profile Velocity를 유지한다.
                    applied_velocities = (
                        set_cup_synchronized_profile_velocity(
                            self.port,
                            self.packet,
                            velocity,
                            full_path,
                        )
                    )

                else:

                    # Non-CUP streamed paths only.
                    is_stream_tracking_path = (
                        label_text
                        in (
                            "L_paper_bag/READY_APPROACH_SMOOTH",
                            "L_paper_bag/HORIZONTAL_LIFT",
                            "L_paper_bag/HORIZONTAL_PULL100",
                            "paper_bag/FULLIK_HANDOFF_SMOOTH",
                        )
                    )

                    if is_stream_tracking_path:
                        set_stream_tracking_profile_velocity(
                            self.port,
                            self.packet,
                            velocity,
                            compensate_reduction=bag_tooldown_motion,
                        )
                    else:
                        set_arm_profile_velocity(
                            self.port,
                            self.packet,
                            velocity,
                        )

                    applied_velocities = None

                # L-paperbag은 관절 tracking cap은 그대로 유지하고
                # stream command 개수만 늘려 전체 궤적을 느리게 한다.
                #
                # 따라서 J1~J4가 같이 따라오는 특성은 유지하면서
                # READY -> GRASP 진행시간만 약 30% 늘어난다.
                is_lbag_smooth_path = (
                    str(request.label)
                    in (
                        "L_paper_bag/READY_APPROACH_SMOOTH",
                        "L_paper_bag/HORIZONTAL_LIFT",
                    )
                )

                command_multiplier = (
                    4
                    if is_lbag_smooth_path
                    else 3
                )

                commands = build_phase_commands(
                    full_path,
                    min_commands=max(
                        PHASE_MIN_COMMANDS,
                        len(full_path) * command_multiplier,
                    ),
                    max_commands=TRANSPORT_MAX_COMMANDS,
                )

                # --------------------------------------------------
                # CUP command-level horizontal invariant
                #
                # CUP_TILT_DEG == 0:
                #   J2 + J3 + J4 == 90 deg
                #
                # IK waypoint뿐 아니라 실제 stream command
                # 하나하나도 수평인지 확인한다.
                # --------------------------------------------------

                if cup_level_motion:

                    for (
                        command_index,
                        command,
                    ) in enumerate(commands):

                        command_q_deg = np.asarray(
                            ticks_to_model_deg(
                                command
                            ),
                            dtype=float,
                        )

                        level_sum_deg = float(
                            command_q_deg[1]
                            + command_q_deg[2]
                            + command_q_deg[3]
                        )

                        level_error_deg = abs(
                            level_sum_deg
                            - 90.0
                        )

                        if level_error_deg > 5.0:

                            raise ValueError(
                                "CUP level-lock violation | "
                                f"label={label_text} | "
                                f"command={command_index} | "
                                f"sum234="
                                f"{level_sum_deg:.3f} deg | "
                                f"error="
                                f"{level_error_deg:.3f} deg | "
                                f"q="
                                f"{command_q_deg.tolist()}"
                            )

                # --------------------------------------------------
                # PAYMENT tool-down command invariant
                #
                # After TOOLDOWN_START:
                #   J2 + J3 + J4 ~= 180 deg
                #   J5 = 2048
                # --------------------------------------------------

                if payment_tooldown_motion or bag_tooldown_motion:

                    for (
                        command_index,
                        command,
                    ) in enumerate(commands):

                        command_q_deg = np.asarray(
                            ticks_to_model_deg(
                                command
                            ),
                            dtype=float,
                        )

                        tool_sum_deg = float(
                            command_q_deg[1]
                            + command_q_deg[2]
                            + command_q_deg[3]
                        )

                        tool_error_deg = abs(
                            tool_sum_deg
                            - 180.0
                        )

                        if tool_error_deg > 5.0:
                            raise ValueError(
                                "Tool-down violation | "
                                f"label={label_text} | "
                                f"command={command_index} | "
                                f"sum234={tool_sum_deg:.3f} deg | "
                                f"error={tool_error_deg:.3f} deg"
                            )

                        held_j5 = int(path[0][4]) if bag_tooldown_motion else 2048
                        j5_tolerance = int(ARM_POSITION_THRESHOLDS[4]) if bag_tooldown_motion else 0
                        if abs(int(command[4]) - held_j5) > j5_tolerance:
                            raise ValueError(
                                "Tool-down J5 changed | "
                                f"label={label_text} | "
                                f"command={command_index} | "
                                f"J5={int(command[4])}"
                            )

                stream_commands(
                    self.arm_writer,
                    commands,
                )

                # CUP은 단순 tick 도착만으로 완료 처리하지 않는다.
                # LEVEL_START 이후 컵 운반 phase는 실제 현재 자세가
                # 수평까지 정착한 것을 확인한 뒤 다음 phase로 넘어간다.
                cup_requires_level_settle = (
                    cup_level_motion
                    or label_text.endswith(
                        "/LEVEL_START"
                    )
                )

                payment_requires_tooldown_settle = (
                    payment_tooldown_motion
                    or bag_tooldown_motion
                    or label_text.endswith(
                        "/TOOLDOWN_START"
                    )
                )

                if cup_requires_level_settle:

                    reached = wait_cup_level_reached(
                        self.port,
                        self.packet,
                        path[-1],
                        timeout_sec=timeout,
                        level_target_deg=90.0,
                        level_tolerance_deg=5.0,
                    )

                elif payment_requires_tooldown_settle:

                    reached = wait_cup_level_reached(
                        self.port,
                        self.packet,
                        path[-1],
                        timeout_sec=timeout,
                        level_target_deg=180.0,
                        level_tolerance_deg=5.0,
                    )

                else:

                    reached = wait_arm_reached(
                        self.port,
                        self.packet,
                        path[-1],
                        timeout_sec=timeout,
                        thresholds=ARM_POSITION_THRESHOLDS,
                    )

                response.success = True
                response.message = (
                    f"{request.label} path reached | "
                    f"points={point_count} | "
                    f"commands={len(commands)} | "
                    f"velocity={velocity}"
                )

                response.reached_ticks = [
                    int(value)
                    for value in reached
                ]

        except Exception as error:
            response.success = False
            response.message = str(error)
            response.reached_ticks = []

        return response

    def handle_bag_gripper_hold(
        self,
        request: Trigger.Request,
        response: Trigger.Response,
    ) -> Trigger.Response:
        del request

        return self._handle_gripper_tick(
            BAG_GRIPPER_HOLD_TICK,
            "BAG_GRIPPER_HOLD",
            response,
            threshold_tick=BAG_GRIPPER_POSITION_THRESHOLD_TICK,
        )

    def handle_execute(
        self,
        request: ExecutePickPlace.Request,
        response: ExecutePickPlace.Response,
    ) -> ExecutePickPlace.Response:
        try:
            with self.motion_lock:
                point_count = int(request.point_count)

                flat_ticks = np.asarray(
                    request.joint_ticks,
                    dtype=np.int64,
                )

                expected_size = point_count * 5

                if (
                    point_count <= 0
                    or flat_ticks.size != expected_size
                ):
                    raise ValueError(
                        "invalid joint_ticks: "
                        f"point_count={point_count}, "
                        f"size={flat_ticks.size}"
                    )

                if len(request.waypoint_names) != point_count:
                    raise ValueError(
                        "waypoint_names length mismatch"
                    )

                ticks_path = [
                    row.copy()
                    for row in flat_ticks.reshape(
                        point_count,
                        5,
                    )
                ]

                final_ticks = execute_tick_path(
                    self.port,
                    self.packet,
                    self.arm_writer,
                    ticks_path,
                    list(request.waypoint_names),
                    int(request.gripper_open_tick),
                    int(request.gripper_close_tick),
                )

                response.success = True
                response.message = "Pick & Place execution completed"
                response.final_joint_ticks = [
                    int(value)
                    for value in final_ticks
                ]
                response.final_gripper_tick = int(
                    read_gripper_position(
                        self.port,
                        self.packet,
                    )
                )

        except Exception as error:
            response.success = False
            response.message = str(error)
            response.final_joint_ticks = []

            try:
                response.final_gripper_tick = int(
                    read_gripper_position(
                        self.port,
                        self.packet,
                    )
                )
            except Exception:
                response.final_gripper_tick = 0

        return response

    def _handle_gripper_tick(
        self,
        goal_tick: int,
        label: str,
        response: Trigger.Response,
        threshold_tick: int = GRIPPER_POSITION_THRESHOLD_TICK,
    ) -> Trigger.Response:
        try:
            with self.motion_lock:
                send_gripper_position(
                    self.port,
                    self.packet,
                    int(goal_tick),
                )

                actual = wait_gripper_reached(
                    self.port,
                    self.packet,
                    int(goal_tick),
                    threshold_tick=int(threshold_tick),
                )

                response.success = True
                response.message = (
                    f"{label} reached | "
                    f"goal={int(goal_tick)} | "
                    f"actual={int(actual)}"
                )

        except Exception as error:
            response.success = False
            response.message = str(error)

        return response

    def handle_cup_gripper_open(
        self,
        request: Trigger.Request,
        response: Trigger.Response,
    ) -> Trigger.Response:
        del request

        return self._handle_gripper_tick(
            CUP_GRIPPER_OPEN_TICK,
            "CUP_GRIPPER_OPEN",
            response,
        )

    def handle_cup_gripper_close(
        self,
        request: Trigger.Request,
        response: Trigger.Response,
    ) -> Trigger.Response:
        del request

        return self._handle_gripper_tick(
            CUP_GRIPPER_CLOSE_TICK,
            "CUP_GRIPPER_CLOSE",
            response,
        )

    def torque_off(self) -> None:
        if not self.hardware_ready:
            return

        set_torque(
            self.port,
            self.packet,
            (*ARM_IDS, GRIPPER_ID),
            False,
        )

        self.hardware_ready = False

        self.get_logger().warn(
            "J1~J5 + Gripper Torque OFF"
        )

    def handle_torque_off(
        self,
        request: Trigger.Request,
        response: Trigger.Response,
    ) -> Trigger.Response:
        del request

        try:
            with self.motion_lock:
                self.torque_off()

            response.success = True
            response.message = "all motor torque off"

        except Exception as error:
            response.success = False
            response.message = str(error)

        return response

    def destroy_node(self):
        try:
            self.torque_off()
        except Exception:
            pass

        try:
            self.port.closePort()
        except Exception:
            pass

        return super().destroy_node()


def main(args=None):
    rclpy.init(args=args)

    node = None
    executor = MultiThreadedExecutor(
        num_threads=2
    )

    try:
        node = MotorControlNode()
        executor.add_node(node)
        executor.spin()

    except KeyboardInterrupt:
        pass

    finally:
        executor.shutdown()

        if node is not None:
            node.destroy_node()

        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
