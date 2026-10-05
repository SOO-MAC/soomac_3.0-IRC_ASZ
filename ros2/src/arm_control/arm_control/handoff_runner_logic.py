# AUTO-GENERATED from study_arm/small_bag_pose_handoff.py
# Only ROS Node ownership/wait plumbing is changed.
# Handoff/IK/path algorithms are AST-guarded unchanged.

from rclpy.callback_groups import ReentrantCallbackGroup
_EMBEDDED_PARENT_NODE = None

def install_parent_node(node):
    global _EMBEDDED_PARENT_NODE
    _EMBEDDED_PARENT_NODE = node
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
SPEC = core.BagSpec(name='smallbag_pose', width_mm=127.0, depth_mm=81.0, height_mm=232.0, x_mm=170.0, y_mm=-150.0, ready_z_mm=202.0, ready_tilt_deg=70.0, grasp_z_mm=102.0, grasp_tilt_deg=90.0, lift_z_mm=170.0, lift_tilt_deg=90.0)
POSE_TOPIC = '/driver_pose/target_base'
POSE_WINDOW = 5
POSE_MIN_SAMPLES = 3
POSE_TIMEOUT_SEC = 10.0
STANDOFF_MM = 300.0
MIN_HANDOFF_RADIUS_MM = 300.0
EXECUTION_VALIDATED_MAX_MM = 400.0
MAX_REAR_DEVIATION_DEG = 45.0
MAX_POSE_XY_SPREAD_MM = 120.0

def rear_deviation_deg(angle_deg):
    """
    rear = +/-180 deg 기준 편차.
    +157 deg -> 23 deg
    -170 deg -> 10 deg
    """
    d = (angle_deg - 180.0 + 180.0) % 360.0 - 180.0
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
    q = np.asarray(pull_q, dtype=float).reshape(5)
    lower = np.asarray(core.cfg.JOINT_LIMIT_LOWER_DEG, dtype=float)
    upper = np.asarray(core.cfg.JOINT_LIMIT_UPPER_DEG, dtype=float)
    sum23 = float(q[1] + q[2])
    j2_min = max(float(lower[1]), sum23 - float(upper[2]))
    j2_max = min(float(upper[1]), sum23 - float(lower[2]))
    if j2_min > j2_max:
        raise RuntimeError(f'no feasible J2/J3 interval | J2=[{j2_min:.3f},{j2_max:.3f}]')

    def radius_for_j2(j2_deg):
        return core.LINK2_MM * math.sin(math.radians(j2_deg)) + core.LINK3_MM * math.sin(math.radians(sum23))
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
        raise RuntimeError(f'invalid maximum radial reach | {max_item[0]:.3f} mm')
    return {'sum23': sum23, 'j2_min': j2_min, 'j2_max': j2_max, 'min_radius': float(min_item[0]), 'max_radius': float(max_item[0]), 'max_j2': float(max_item[1]), 'max_j3': float(max_item[2])}

def compute_handoff_target(person_xyz_mm, max_radius_mm):
    (x, y, z) = [float(v) for v in person_xyz_mm]
    person_r = math.hypot(x, y)
    if person_r < 1.0:
        raise RuntimeError('invalid person distance')
    if x >= 0.0:
        raise RuntimeError(f'person is not behind robot | X={x:.1f}')
    angle_deg = math.degrees(math.atan2(y, x))
    rear_dev = rear_deviation_deg(angle_deg)
    if rear_dev > MAX_REAR_DEVIATION_DEG:
        raise RuntimeError(f'person direction too far from rear | angle={angle_deg:.2f} deg, rear deviation={rear_dev:.2f} deg')
    wanted_radius = person_r - STANDOFF_MM
    if wanted_radius < MIN_HANDOFF_RADIUS_MM:
        raise RuntimeError(f'person too close for safe handoff | person_r={person_r:.1f} mm, wanted_r={wanted_radius:.1f} mm')
    max_radius_mm = float(max_radius_mm)
    if max_radius_mm < MIN_HANDOFF_RADIUS_MM:
        raise RuntimeError(f'kinematic max reach too short | max_radius={max_radius_mm:.1f} mm')
    handoff_radius = min(wanted_radius, max_radius_mm)
    ux = x / person_r
    uy = y / person_r
    target_x = ux * handoff_radius
    target_y = uy * handoff_radius
    actual_standoff = person_r - handoff_radius
    return {'person_x': x, 'person_y': y, 'person_z': z, 'person_r': person_r, 'angle_deg': angle_deg, 'rear_dev_deg': rear_dev, 'wanted_radius': wanted_radius, 'handoff_radius': handoff_radius, 'target_x': target_x, 'target_y': target_y, 'actual_standoff': actual_standoff}

def build_dynamic_handoff(pull_q, handoff):
    pull_q = np.asarray(pull_q, dtype=float).reshape(5)
    target_j1 = float(handoff['angle_deg'])
    target_radius = float(handoff['handoff_radius'])
    lower = np.asarray(core.cfg.JOINT_LIMIT_LOWER_DEG, dtype=float)
    upper = np.asarray(core.cfg.JOINT_LIMIT_UPPER_DEG, dtype=float)
    if not lower[0] <= target_j1 <= upper[0]:
        raise RuntimeError(f'dynamic J1 limit exceeded: {target_j1:.2f} deg')
    turn_q = pull_q.copy()
    turn_q[0] = target_j1
    turn_q[4] = core.J5_DEG
    turn_ticks = np.asarray(core.cfg.model_deg_to_ticks(turn_q), dtype=np.int64)
    if int(turn_ticks[3]) > core.J4_SOFT_MAX_TICK:
        raise RuntimeError('dynamic TURN J4 soft max exceeded')
    sum23 = float(turn_q[1] + turn_q[2])
    total_tooldown = float(sum23 + turn_q[3])
    if abs(total_tooldown - 180.0) > 0.2:
        raise RuntimeError(f'tool-down sum mismatch | {total_tooldown:.3f}')
    rhs = (target_radius - core.LINK3_MM * math.sin(math.radians(sum23))) / core.LINK2_MM
    if not -1.0 <= rhs <= 1.0:
        raise RuntimeError(f'dynamic EXTEND unreachable | rhs={rhs:.6f}, radius={target_radius:.1f}')
    final_j2 = math.degrees(math.asin(rhs))
    final_j3 = sum23 - final_j2
    final_q = np.array([target_j1, final_j2, final_j3, turn_q[3], core.J5_DEG])
    if np.any(final_q < lower) or np.any(final_q > upper):
        raise RuntimeError(f'dynamic final joint limit exceeded | q={np.round(final_q, 3).tolist()}')
    points = []
    for i in range(1, core.EXTEND_SEGMENTS + 1):
        u = i / core.EXTEND_SEGMENTS
        q = (1.0 - u) * turn_q + u * final_q
        q[0] = target_j1
        q[3] = turn_q[3]
        q[4] = core.J5_DEG
        q[2] = sum23 - q[1]
        ticks = np.asarray(core.cfg.model_deg_to_ticks(q), dtype=np.int64)
        if int(ticks[3]) > core.J4_SOFT_MAX_TICK:
            raise RuntimeError('dynamic EXTEND J4 soft max exceeded')
        points.append({'name': f'DYNAMIC_EXTEND_{i}', 'q': q, 'ticks': ticks})
    chain = core.spp.create_robot_chain()
    state = core.pose_state(chain, np.deg2rad(final_q))
    tool_axis = core.unit(state['tool_axis'])
    tcp = np.asarray(state['xyz'], dtype=float) + core.AXIAL_OFFSET_MM * tool_axis
    return {'turn_q': turn_q, 'turn_ticks': turn_ticks, 'extend': points, 'final_q': final_q, 'final_tcp': tcp}

class Runner:

    def __init__(self, use_motor=False):
        global _EMBEDDED_PARENT_NODE
        if _EMBEDDED_PARENT_NODE is None:
            raise RuntimeError('handoff Runner parent /arm_control is not installed')
        self._parent_node = _EMBEDDED_PARENT_NODE
        self._callback_group = ReentrantCallbackGroup()
        self.use_motor = use_motor
        self.pose_samples = deque(maxlen=POSE_WINDOW)
        self.current = None
        self.create_subscription(PointStamped, POSE_TOPIC, self.pose_cb, 10)
        if use_motor:
            self.create_subscription(Int32MultiArray, '/motor/joint_state', self.joint_cb, 10)
            self.path = self.create_client(MoveTickPath, core.PATH_SERVICE)
            self.open = self.create_client(Trigger, core.OPEN_SERVICE)
            self.close = self.create_client(Trigger, core.CLOSE_SERVICE)

    def __getattr__(self, name):
        return getattr(self._parent_node, name)

    def create_subscription(self, msg_type, topic, callback, qos_profile, *args, **kwargs):
        kwargs.setdefault('callback_group', self._callback_group)
        return self._parent_node.create_subscription(msg_type, topic, callback, qos_profile, *args, **kwargs)

    def create_client(self, srv_type, srv_name, *args, **kwargs):
        kwargs.setdefault('callback_group', self._callback_group)
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
        deadline = time.monotonic() + POSE_TIMEOUT_SEC
        while len(self.pose_samples) < POSE_WINDOW and time.monotonic() < deadline:
            time.sleep(0.05)
        if len(self.pose_samples) < POSE_MIN_SAMPLES:
            raise RuntimeError(f'not enough driver pose samples | {len(self.pose_samples)}')
        a = np.asarray(self.pose_samples, dtype=float)
        median = np.median(a, axis=0)
        xy_error = a[:, :2] - median[:2]
        distances = np.linalg.norm(xy_error, axis=1)
        INLIER_RADIUS_MM = 150.0
        inlier_mask = distances <= INLIER_RADIUS_MM
        inliers = a[inlier_mask]
        if len(inliers) < POSE_MIN_SAMPLES:
            raise RuntimeError(f'driver pose unstable | inliers={len(inliers)}/{len(a)}, distances={np.round(distances, 1).tolist()}')
        filtered_median = np.median(inliers, axis=0)
        filtered_xy_error = inliers[:, :2] - filtered_median[:2]
        filtered_spread = float(np.max(np.linalg.norm(filtered_xy_error, axis=1)))
        print(f'[POSE FILTER] samples={len(a)}, inliers={len(inliers)}, spread={filtered_spread:.1f} mm')
        return filtered_median

    def wait_services(self):
        for (c, name) in ((self.path, core.PATH_SERVICE), (self.open, core.OPEN_SERVICE), (self.close, core.CLOSE_SERVICE)):
            if not c.wait_for_service(timeout_sec=5.0):
                raise RuntimeError(f'service unavailable: {name}')

    def get_current(self):
        for _ in range(50):
            time.sleep(0.1)
            if self.current is not None:
                return self.current.copy()
        raise RuntimeError('no /motor/joint_state')

    def trigger(self, client, label):
        future = client.call_async(Trigger.Request())
        self._wait_future(future)
        res = future.result()
        if res is None or not res.success:
            raise RuntimeError(f"{label} failed: {getattr(res, 'message', 'no response')}")
        print(f'[OK] {label}: {res.message}')

    def move(self, name, ticks_list):
        arr = np.asarray(ticks_list, dtype=np.int64).reshape(-1, 5)
        req = MoveTickPath.Request()
        req.joint_ticks = [int(v) for v in arr.reshape(-1)]
        req.point_count = len(arr)
        req.label = name
        req.profile_velocity = 0
        req.timeout_sec = core.PATH_TIMEOUT_SEC
        future = self.path.call_async(req)
        self._wait_future(future)
        res = future.result()
        if res is None or not res.success:
            raise RuntimeError(f"{name} failed: {getattr(res, 'message', 'no response')}")
        print(f'[OK] {name} | reached={list(res.reached_ticks)}')

def print_dynamic_plan(person, handoff, dynamic, reach):
    tcp = dynamic['final_tcp']
    print()
    print('=' * 72)
    print(f'[PERSON] X={person[0]:+.1f} Y={person[1]:+.1f} Z={person[2]:+.1f} mm')
    print(f"distance       = {handoff['person_r']:.1f} mm")
    print(f"kinematic max  = {reach['max_radius']:.1f} mm")
    print(f"J2 feasible    = {reach['j2_min']:.2f} ~ {reach['j2_max']:.2f} deg")
    print(f"max reach pose = J2 {reach['max_j2']:.2f} deg, J3 {reach['max_j3']:.2f} deg")
    print(f"direction      = {handoff['angle_deg']:+.2f} deg")
    print(f"wanted radius  = {handoff['wanted_radius']:.1f} mm")
    print(f"used radius    = {handoff['handoff_radius']:.1f} mm")
    print(f"final standoff = {handoff['actual_standoff']:.1f} mm")
    print()
    print(f"[XY TARGET] X={handoff['target_x']:+.1f} Y={handoff['target_y']:+.1f} mm")
    print(f'[FINAL MODEL TCP] X={tcp[0]:+.1f} Y={tcp[1]:+.1f} Z={tcp[2]:+.1f} mm')
    print(f"[TURN J1] {dynamic['turn_q'][0]:+.2f} deg")
    print('=' * 72)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--step', action='store_true')
    args = parser.parse_args()
    if args.execute and (not args.step):
        raise SystemExit('현재 pose handoff 검증 단계에서는 --execute --step 으로만 실행하세요.')
    plan = core.build_plan(SPEC)
    reach = compute_tooldown_reach_limits(plan['PULL100']['q'])
    print()
    print(f"[KINEMATIC REACH] {reach['min_radius']:.1f} ~ {reach['max_radius']:.1f} mm")
    rclpy.init()
    node = Runner(use_motor=args.execute)
    try:
        if not args.execute:
            print('\n[POSE PREVIEW] motor command 없음')
            person = node.collect_pose()
            handoff = compute_handoff_target(person, reach['max_radius'])
            dynamic = build_dynamic_handoff(plan['PULL100']['q'], handoff)
            print_dynamic_plan(person, handoff, dynamic, reach)
            return
        node.wait_services()
        current = node.get_current()
        error = current - core.START_TICKS
        print('current =', current.tolist())
        if np.any(np.abs(error) > core.START_TOL_TICKS):
            raise RuntimeError(f'START pose check failed | error={error.tolist()}')
        input('\nGRIPPER OPEN\nEnter > ')
        node.trigger(node.open, f'{SPEC.name}/OPEN')
        input('\nREADY -> GRASP\nEnter > ')
        node.move(f'{SPEC.name}/PICK', [plan['READY']['ticks'], plan['GRASP']['ticks']])
        input('\nGRIPPER CLOSE\nEnter > ')
        node.trigger(node.close, f'{SPEC.name}/CLOSE')
        input('\nLIFT -> PULL100\nEnter > ')
        node.move(f'{SPEC.name}/LIFT_PULL', [plan['LIFT']['ticks'], plan['PULL100']['ticks']])
        print('\n[POSE] fresh driver pose collecting...')
        person = node.collect_pose()
        handoff = compute_handoff_target(person, reach['max_radius'])
        if handoff['handoff_radius'] > EXECUTION_VALIDATED_MAX_MM + 1e-06:
            raise RuntimeError(f"PREVIEW에서는 계산 가능하지만 실제 모터에서 아직 검증되지 않은 reach입니다 | requested={handoff['handoff_radius']:.1f} mm, validated={EXECUTION_VALIDATED_MAX_MM:.1f} mm")
        dynamic = build_dynamic_handoff(plan['PULL100']['q'], handoff)
        print_dynamic_plan(person, handoff, dynamic, reach)
        input('\n위 HANDOFF 목표 확인.\n첫 모션 검증 때는 사람을 팔 작업영역 밖으로 이동시킨 뒤\nEnter > ')
        node.move(f'{SPEC.name}/DYNAMIC_TURN', [dynamic['turn_ticks']])
        input('\nDYNAMIC EXTEND\nEnter > ')
        node.move(f'{SPEC.name}/DYNAMIC_EXTEND', [p['ticks'] for p in dynamic['extend']])
        input('\nHANDOFF 위치 도착.\n그리퍼 OPEN 할 준비가 되면 Enter > ')
        node.trigger(node.open, f'{SPEC.name}/OPEN_HANDOVER')
        input('\nRETURN START\nEnter > ')
        node.move(f'{SPEC.name}/RETURN_START', [core.START_TICKS])
        print('\n[DONE] smallbag dynamic handoff')
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
if __name__ == '__main__':
    main()
