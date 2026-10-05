"""드라이브스루 운전자 keypoint 3D 좌표 퍼블리셔 (캘리브레이션 전).

RealSense를 직접 열고, YOLO로 사람 검출 -> 운전자 선별 -> 대상 keypoint 3D 발행.

발행: ~/target  geometry_msgs/PointStamped
      카메라 광학 좌표계(X 우, Y 하, Z 전방), 단위 m. point.z 가 곧 depth.
      대상이 안 잡히거나 depth가 무효면 발행하지 않는다.

주의: realsense2_camera 노드와 동시에 못 띄운다 (장치 점유).
"""

import cv2
import numpy as np
import pyrealsense2 as rs
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import PointStamped
from ultralytics import YOLO

W, H, FPS = 640, 480, 30
KP_CONF = 0.5
DEPTH_WIN = 2               # 5x5
DEPTH_MIN_VALID = 0.4
DEPTH_RANGE = (0.3, 4.0)

COCO17 = ["nose", "l_eye", "r_eye", "l_ear", "r_ear",
          "l_shoulder", "r_shoulder", "l_elbow", "r_elbow",
          "l_wrist", "r_wrist", "l_hip", "r_hip",
          "l_knee", "r_knee", "l_ankle", "r_ankle"]

# 사람 선별용 앵커. 어깨가 가려져도 얼굴로 사람은 잡는다.
ANCHORS = ["l_shoulder", "r_shoulder", "nose", "l_eye", "r_eye", "l_ear", "r_ear"]

EDGES = [("nose", "l_eye"), ("nose", "r_eye"), ("l_eye", "l_ear"), ("r_eye", "r_ear"),
         ("l_shoulder", "r_shoulder"),
         ("l_shoulder", "l_elbow"), ("l_elbow", "l_wrist"),
         ("r_shoulder", "r_elbow"), ("r_elbow", "r_wrist")]


def parse_yolo(result):
    """YOLO 결과 -> [{name: (u, v, conf)}]. 모델 바꿀 때 이 함수만 바꾸면 된다."""
    kp = result.keypoints
    if kp is None or len(kp) == 0:
        return []
    xy = kp.xy.cpu().numpy()
    cf = kp.conf.cpu().numpy() if kp.conf is not None else np.ones(xy.shape[:2])
    return [{nm: (float(p[i][0]), float(p[i][1]), float(c[i]))
             for i, nm in enumerate(COCO17[:11])}
            for p, c in zip(xy, cf)]


def sample_depth(depth_m, u, v):
    """(u,v) 주변 5x5 유효 depth 중앙값(m). 실패 시 None."""
    h, w = depth_m.shape
    u, v = int(round(u)), int(round(v))
    if not (0 <= u < w and 0 <= v < h):
        return None
    p = depth_m[max(v - DEPTH_WIN, 0):v + DEPTH_WIN + 1,
                max(u - DEPTH_WIN, 0):u + DEPTH_WIN + 1]
    ok = p[(p >= DEPTH_RANGE[0]) & (p <= DEPTH_RANGE[1])]
    if ok.size < DEPTH_MIN_VALID * (2 * DEPTH_WIN + 1) ** 2:
        return None
    return float(np.median(ok))


def pick_driver(persons, depth_m):
    """카메라에 가장 가까운 사람 = 운전자. confidence 최대로 고르면 조수석이 뽑힐 수 있다."""
    cands = []
    for i, p in enumerate(persons):
        for nm in ANCHORS:
            k = p[nm]
            if k[2] >= KP_CONF:
                cands.append((i, sample_depth(depth_m, k[0], k[1]), k[2]))
                break
    if not cands:
        return None
    with_d = [c for c in cands if c[1] is not None]
    if with_d:
        return min(with_d, key=lambda c: c[1])[0]
    return max(cands, key=lambda c: c[2])[0]


class PoseNode(Node):

    def __init__(self):
        super().__init__("driver_pose")
        self.declare_parameter("weights", "yolo11m-pose.pt")
        self.declare_parameter("target_kp", "l_shoulder")     # 좌핸들 = 창문 쪽
        self.declare_parameter("frame_id", "camera_color_optical_frame")
        self.declare_parameter("show", True)
        self.declare_parameter("serial_no", "048522073427")

        g = lambda n: self.get_parameter(n).value
        self.target = g("target_kp")
        self.frame_id = g("frame_id")
        self.show = g("show")
        self.serial_no = str(g("serial_no"))

        self.model = YOLO(g("weights"))
        self.pub = self.create_publisher(PointStamped, "~/target", 10)

        self.pipe = rs.pipeline()
        cfg = rs.config()

        # Camera2 전용: Detection1(Camera1)과 절대 섞이지 않게 serial 고정
        cfg.enable_device(self.serial_no)

        cfg.enable_stream(rs.stream.color, W, H, rs.format.bgr8, FPS)
        cfg.enable_stream(rs.stream.depth, W, H, rs.format.z16, FPS)
        prof = self.pipe.start(cfg)
        self.scale = prof.get_device().first_depth_sensor().get_depth_scale()
        self.align = rs.align(rs.stream.color)

        self.get_logger().info(
            f"시작  model={g('weights')}  target={self.target} "
            f"camera_serial={self.serial_no}"
        )

    def step(self):
        """한 프레임 처리. q 누르면 False."""
        f = self.align.process(self.pipe.wait_for_frames())
        c, d = f.get_color_frame(), f.get_depth_frame()
        if not c or not d:
            return True

        intr = c.profile.as_video_stream_profile().intrinsics
        img = np.asanyarray(c.get_data())
        depth = np.asanyarray(d.get_data()).astype(np.float32) * self.scale

        persons = parse_yolo(self.model(img, verbose=False)[0])
        di = pick_driver(persons, depth)

        k = persons[di][self.target] if di is not None else None
        z = None
        if k is not None and k[2] >= KP_CONF:
            z = sample_depth(depth, k[0], k[1])
            if z is not None:
                x, y, zz = rs.rs2_deproject_pixel_to_point(intr, [k[0], k[1]], z)
                msg = PointStamped()
                msg.header.stamp = self.get_clock().now().to_msg()
                msg.header.frame_id = self.frame_id
                msg.point.x, msg.point.y, msg.point.z = float(x), float(y), float(zz)
                self.pub.publish(msg)

        if self.show:
            cv2.imshow("driver_pose", self.draw(img, persons, di, k, z))
            if cv2.waitKey(1) & 0xFF == ord('q'):
                return False
        return True

    def draw(self, img, persons, di, k, z):
        img = img.copy()
        for i, p in enumerate(persons):
            col = (0, 200, 255) if i == di else (140, 140, 140)   # 운전자 주황, 나머지 회색
            ok = {nm: v for nm, v in p.items() if v[2] >= KP_CONF}
            P = lambda nm: (int(ok[nm][0]), int(ok[nm][1]))
            for a, b in EDGES:
                if a in ok and b in ok:
                    cv2.line(img, P(a), P(b), col, 2)
            if all(n in ok for n in ("l_shoulder", "r_shoulder", "nose")):
                neck = ((P("l_shoulder")[0] + P("r_shoulder")[0]) // 2,
                        (P("l_shoulder")[1] + P("r_shoulder")[1]) // 2)
                cv2.line(img, neck, P("nose"), col, 2)
            for nm in ok:
                cv2.circle(img, P(nm), 3, col, -1)

        if k is not None and k[2] >= KP_CONF:
            u, v = int(k[0]), int(k[1])
            c = (0, 255, 0) if z is not None else (0, 0, 255)
            t = f"{z:.2f}m" if z is not None else "X"
            cv2.circle(img, (u, v), 8, c, 2)
            cv2.putText(img, t, (u + 10, v - 8), cv2.FONT_HERSHEY_SIMPLEX, .5, (0, 0, 0), 3)
            cv2.putText(img, t, (u + 10, v - 8), cv2.FONT_HERSHEY_SIMPLEX, .5, c, 1)
        return img

    def close(self):
        self.pipe.stop()
        cv2.destroyAllWindows()


def main():
    rclpy.init()
    node = PoseNode()
    try:
        while rclpy.ok() and node.step():
            rclpy.spin_once(node, timeout_sec=0)
    except KeyboardInterrupt:
        pass
    finally:
        node.close()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
