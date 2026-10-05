#!/usr/bin/env python3
"""
Paper Bag Detection + Number OCR Node (World-view RealSense D435, pyrealsense2 직접 사용)

YOLO11-seg로 L_paper_bag / paper_bag / cup을 검출하고, 각 물체 영역에서 EasyOCR로 숫자 스티커를 읽어
물체와 짝지은 뒤 여러 프레임 다수결로 숫자를 확정한다.

[OCR 방침]
  allowlist(숫자만 허용)는 쓰지 않는다.(영어를 숫자로 읽는 문제 발생.)
  -> 전체 문자로 읽고, 결과가 전부 숫자인 것만 채택한다.

[출력] /paper_bag/detections  (vision_msgs/Detection2DArray)
  header                         : 프레임 촬영 시각 / frame_id 파라미터
  detection.id                   : 트랙 ID (같은 물체면 프레임이 바뀌어도 유지)
  results[0]                     : class_id = "L_paper_bag" | "paper_bag" | "cup", score = 검출 conf
  results[1] (숫자 확정 시에만)  : class_id = "num:<숫자>", score = 다수결 득표율(0~1)
  bbox.center.position.x / y     : 몸통 중심 픽셀 (opening으로 손잡이 제거, SDK로 왜곡 보정, float)
  bbox.center.theta, size_x/y    : 사용 안 함 (0)

  숫자 확정은 sticky: 한 번 확정되면 다른 숫자가 확정되거나 트랙이 사라질 때까지 유지
  (팔이 스티커를 잠깐 가려도 숫자가 안 날아감).

[출력] /paper_bag/camera_info (transient_local)  후단 역투영용 K. 출력 좌표가 이미 보정돼 있어 D=0.
[디버그] /paper_bag/debug_image + OpenCV 창 (show_window)
  외곽선: YOLO 마스크 / 빨간 점: 발행되는 중심 픽셀
  노랑: 채택되어 물체에 할당된 숫자 / 빨강: 숫자가 아니라서 버린 판독(로고 등) / 회색: 숫자지만 어느 물체에도 속하지 않아 버린 판독
"""

import array
import re
import threading
import time
import traceback
from collections import Counter, deque

import cv2
import numpy as np
import easyocr
import pyrealsense2 as rs
from ultralytics import YOLO

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
from rclpy.time import Time
from sensor_msgs.msg import CameraInfo, Image
from std_msgs.msg import Header
from vision_msgs.msg import Detection2D, Detection2DArray, ObjectHypothesisWithPose


EXPECTED_CLASSES = {0: "L_paper_bag", 1: "paper_bag", 2: "cup"}
CLASS_COLORS = {0: (0, 165, 255), 1: (0, 255, 0), 2: (255, 0, 255)}  # BGR


### 물체 트래킹 + count / 월드뷰 고정 카메라여서 중심 픽셀 거리 기반 greedy 매칭만 사용.
class BagTracker:

    def __init__(self, gate_px, ttl_sec, window, vote_min, vote_ratio):
        self.gate, self.ttl, self.window = gate_px, ttl_sec, window
        self.vote_min, self.vote_ratio = vote_min, vote_ratio
        self.tracks = []
        self.next_id = 0

    # 각 객체에 대응하는 트래킹 리스트 생성
    def update(self, centers, now):
        self.tracks = [t for t in self.tracks if now - t["last_seen"] <= self.ttl]
        pairs = sorted((float(np.hypot(c[0] - t["center"][0], c[1] - t["center"][1])), ti, di)
                       for ti, t in enumerate(self.tracks) for di, c in enumerate(centers))
        assigned, used = [None] * len(centers), set()
        for d, ti, di in pairs:
            if d > self.gate:
                break
            if ti in used or assigned[di] is not None:
                continue
            used.add(ti)
            assigned[di] = self.tracks[ti]

        for di, c in enumerate(centers):
            if assigned[di] is None:
                assigned[di] = {"id": self.next_id, "votes": deque(maxlen=self.window),
                                "number": None, "score": 0.0}
                self.next_id += 1
                self.tracks.append(assigned[di])
            assigned[di]["center"] = c
            assigned[di]["last_seen"] = now
        return assigned

    # 객체에서 판독한 숫자 or None. None은 계산에서 제외
    def vote(self, t, text):
        t["votes"].append(text)
        valid = [v for v in t["votes"] if v is not None]
        if not valid:
            return
        num, cnt = Counter(valid).most_common(1)[0]
        if cnt >= self.vote_min and cnt / len(valid) >= self.vote_ratio:
            t["number"], t["score"] = num, cnt / len(valid)


class PaperBagOcrNode(Node):
    def __init__(self):
        super().__init__("paper_bag_detection_n_ocr_node")

        # 카메라 / 검출
        self.declare_parameter("weights_path", "/home/ryeong/ros2_ws/src/ML/runs/segment/multi_seg/yolo11n_paper_bag_ver_1-4/weights/best.pt")
        self.declare_parameter("serial_no", "048522072487")             # RealSense 여러 대면 반드시 지정
        self.declare_parameter("width", 1920)
        self.declare_parameter("height", 1080)
        self.declare_parameter("fps", 30)
        self.declare_parameter("frame_id", "world_cam_color_optical_frame")
        self.declare_parameter("conf_thres", 0.5)
        self.declare_parameter("iou_thres", 0.5)
        self.declare_parameter("imgsz", 640)
        self.declare_parameter("device", "0")
        self.declare_parameter("agnostic_nms", True)        # 한 물체에 L/일반 동시 검출 방지
        self.declare_parameter("max_rate_hz", 15.0)
        self.declare_parameter("min_area_px", 2000.0)       # 1080p 기준
        self.declare_parameter("open_px", 29)               # 손잡이 제거 opening 커널 (1080p 기준)
        self.declare_parameter("publish_debug_image", True)
        self.declare_parameter("debug_scale", 0.5)          # 1080p 원본 그대로 보내면 메시지당 6MB
        self.declare_parameter("show_window", True)         # OpenCV 창으로 디버그 화면 표시

        # 숫자 OCR
        self.declare_parameter("ocr_min_conf", 0.5)
        self.declare_parameter("valid_numbers", "")         # 예: "1,2,3,4". 비우면 모든 숫자 허용
        self.declare_parameter("max_digits", 2)             # 이보다 긴 숫자열은 버림 (바코드 등)
        self.declare_parameter("ocr_crop_margin", 0.15)     # 봉투 bbox 대비 여유 비율
        self.declare_parameter("ocr_min_side", 400)         # 크롭 짧은 변이 이보다 작으면 확대
        self.declare_parameter("ocr_min_size", 10)          # EasyOCR 최소 텍스트 박스(px)
        self.declare_parameter("assign_tol_px", 20.0)       # 봉투 경계 밖으로 이만큼까지 허용

        # 트래킹 / 다수결
        self.declare_parameter("track_gate_px", 120.0)      # 프레임 간 같은 물체로 볼 중심 거리
        self.declare_parameter("track_ttl_sec", 3.0)        # 이 시간 안 보이면 트랙 삭제
        self.declare_parameter("vote_window", 10)
        self.declare_parameter("vote_min", 5)
        self.declare_parameter("vote_ratio", 0.6)

        gp = lambda n: self.get_parameter(n).value  # noqa: E731
        self.frame_id = str(gp("frame_id"))
        self.conf, self.iou = float(gp("conf_thres")), float(gp("iou_thres"))
        self.imgsz = int(gp("imgsz"))
        self.device = str(gp("device"))
        self.agnostic = bool(gp("agnostic_nms"))
        self.min_period = 1.0 / max(float(gp("max_rate_hz")), 1e-3)
        self.min_area = float(gp("min_area_px"))
        k = max(1, int(gp("open_px")))
        self.open_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        self.debug_scale = float(gp("debug_scale"))
        self.show_window = bool(gp("show_window"))
        self.ocr_min_conf = float(gp("ocr_min_conf"))
        self.valid_numbers = {v.strip() for v in str(gp("valid_numbers")).split(",") if v.strip()} or None
        self.num_re = re.compile(rf"[0-9]{{1,{int(gp('max_digits'))}}}")
        self.ocr_margin = float(gp("ocr_crop_margin"))
        self.ocr_min_side = int(gp("ocr_min_side"))
        self.ocr_min_size = int(gp("ocr_min_size"))
        self.assign_tol = float(gp("assign_tol_px"))
        self.tracker = BagTracker(float(gp("track_gate_px")), float(gp("track_ttl_sec")),
                                  int(gp("vote_window")), int(gp("vote_min")), float(gp("vote_ratio")))


        ## YOLO
        weights = str(gp("weights_path"))
        self.model = YOLO(weights)
        self.names = dict(self.model.names)
        for k, v in EXPECTED_CLASSES.items():
            if self.names.get(k) != v:
                self.get_logger().warn(f"클래스 불일치: model.names[{k}]={self.names.get(k)!r}, "
                                       f"기대값 {v!r}. 출력은 model.names를 따른다.")
        t0 = time.monotonic()
        self.model.predict(np.zeros((self.imgsz, self.imgsz, 3), np.uint8), imgsz=self.imgsz,
                           device=self.device, verbose=False)
        self.get_logger().info(f"YOLO 로딩/워밍업 완료: {weights} ({(time.monotonic() - t0) * 1000:.0f} ms), "
                               f"classes={self.names}")

        # EasyOCR (전체 문자로 읽음)
        t0 = time.monotonic()
        self.reader = easyocr.Reader(["en"], gpu=self.device != "cpu", verbose=False)
        self.reader.readtext(np.full((64, 64, 3), 255, np.uint8))
        self.get_logger().info(f"EasyOCR 로딩/워밍업 완료 ({(time.monotonic() - t0) * 1000:.0f} ms), "
                               f"valid={sorted(self.valid_numbers or [])}")

        # Publishers
        self.det_pub = self.create_publisher(Detection2DArray, "/paper_bag/detections", 10)
        latched = QoSProfile(depth=1, history=HistoryPolicy.KEEP_LAST,
                             reliability=ReliabilityPolicy.RELIABLE,
                             durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.info_pub = self.create_publisher(CameraInfo, "/paper_bag/camera_info", latched)
        self.dbg_pub = (self.create_publisher(Image, "/paper_bag/debug_image", 1)
                        if bool(gp("publish_debug_image")) else None)

        # RealSense
        self.pipeline = rs.pipeline()
        cfg = rs.config()
        if str(gp("serial_no")):
            cfg.enable_device(str(gp("serial_no")))
        cfg.enable_stream(rs.stream.color, int(gp("width")), int(gp("height")), rs.format.bgr8, int(gp("fps")))
        try:
            profile = self.pipeline.start(cfg)
        except RuntimeError as e:
            raise RuntimeError(f"RealSense 시작 실패: {e} (serial/해상도 확인, "
                               f"realsense2_camera 노드가 같은 카메라를 잡고 있지 않은지 확인)")
        dev = profile.get_device()
        self.intr = profile.get_stream(rs.stream.color).as_video_stream_profile().get_intrinsics()
        i = self.intr
        self.get_logger().info(
            f"RealSense {dev.get_info(rs.camera_info.name)} S/N {dev.get_info(rs.camera_info.serial_number)} | "
            f"{i.width}x{i.height} fx={i.fx:.1f} fy={i.fy:.1f} cx={i.ppx:.1f} cy={i.ppy:.1f} "
            f"model={i.model} D={np.round(i.coeffs, 5).tolist()}")

        info = CameraInfo()
        info.header.frame_id = self.frame_id
        info.header.stamp = self.get_clock().now().to_msg()
        info.width, info.height = i.width, i.height
        info.distortion_model = "plumb_bob"
        info.d = [0.0] * 5                      # 출력 좌표가 이미 보정됨 -> K만 쓰면 됨
        info.k = [i.fx, 0.0, i.ppx, 0.0, i.fy, i.ppy, 0.0, 0.0, 1.0]
        info.r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        info.p = [i.fx, 0.0, i.ppx, 0.0, 0.0, i.fy, i.ppy, 0.0, 0.0, 0.0, 1.0, 0.0]
        self.info_pub.publish(info)

        # OpenCV 창은 메인 스레드에서만 안정적이라, 처리 스레드는 최신 프레임만 넘기고
        # 표시는 메인 스레드(rclpy.spin)의 타이머가 맡는다.
        self.latest_dbg = None
        self.dbg_lock = threading.Lock()
        if self.show_window:
            self.create_timer(0.03, self._show)

        self.running = True
        self.worker = threading.Thread(target=self._loop, daemon=True)
        self.worker.start()
        self.get_logger().info("Paper Bag Detection + OCR Node 시작")

    def _loop(self):
        last_proc = 0.0
        while self.running and rclpy.ok():
            try:
                frames = self.pipeline.wait_for_frames(2000)
            except RuntimeError:
                self.get_logger().warn("2초 동안 프레임 없음 - 카메라 연결 확인", throttle_duration_sec=2.0)
                continue
            while True:                          # 추론이 밀려 쌓인 프레임은 버리고 최신 것만
                newer = self.pipeline.poll_for_frames()
                if not newer:
                    break
                frames = newer

            now = time.monotonic()
            if now - last_proc < self.min_period:
                continue
            last_proc = now

            color = frames.get_color_frame()
            if not color:
                continue
            # global/system time 도메인이면 촬영 시각, 아니면 수신 시각
            if color.get_frame_timestamp_domain() in (rs.timestamp_domain.global_time,
                                                      rs.timestamp_domain.system_time):
                stamp = Time(nanoseconds=int(color.get_timestamp() * 1e6)).to_msg()
            else:
                stamp = self.get_clock().now().to_msg()
            header = Header(stamp=stamp, frame_id=self.frame_id)
            img = np.asanyarray(color.get_data()).copy()
            h, w = img.shape[:2]

            try:
                res = self.model.predict(img, imgsz=self.imgsz, conf=self.conf, iou=self.iou,
                                         device=self.device, agnostic_nms=self.agnostic,
                                         retina_masks=False, verbose=False)[0]

                # 1. 검출: 전체 폴리곤(크롭/짝짓기용) + 손잡이 뺀 몸통 중심 ----
                objs = []
                if res.masks is not None:
                    for poly, box in zip(res.masks.xy, res.boxes):
                        poly = np.asarray(poly, dtype=np.float32)
                        if len(poly) < 3:
                            continue
                        # bbox ROI에서만 마스크를 그려서 opening (1080p 전체에 하면 느림). 1px 여백으로 경계 문제 방지
                        rect = cv2.boundingRect(poly)
                        ox, oy = rect[0] - 1, rect[1] - 1
                        mask = np.zeros((rect[3] + 2, rect[2] + 2), np.uint8)
                        cv2.fillPoly(mask, [np.round(poly - (ox, oy)).astype(np.int32)], 255)
                        opened = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.open_kernel)
                        if cv2.countNonZero(opened) >= 0.5 * cv2.countNonZero(mask):   # 작은 물체면 원본 유지
                            mask = opened
                        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
                        if not contours:
                            continue
                        m = cv2.moments(max(contours, key=cv2.contourArea))
                        if m["m00"] < self.min_area:
                            continue
                        cls = int(box.cls[0])
                        objs.append({"cls": cls, "name": self.names.get(cls, str(cls)),
                                     "score": float(box.conf[0]), "poly": poly, "rect": rect,
                                     "center": (m["m10"] / m["m00"] + ox, m["m01"] / m["m00"] + oy),
                                     "best": None})

                # 2. 트래킹
                for o, t in zip(objs, self.tracker.update([o["center"] for o in objs], now)):
                    o["track"] = t

                # 3. 물체 bbox(+여유) 크롭 -> EasyOCR -> 원본 좌표
                readings = []
                for o in objs:
                    x, y, bw, bh = o["rect"]
                    mx, my = int(bw * self.ocr_margin), int(bh * self.ocr_margin)
                    x0, y0 = max(0, x - mx), max(0, y - my)
                    crop = img[y0:min(h, y + bh + my), x0:min(w, x + bw + mx)]
                    if min(crop.shape[:2]) < 8:
                        continue
                    s = max(1.0, self.ocr_min_side / min(crop.shape[:2]))
                    if s > 1.0:
                        crop = cv2.resize(crop, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
                    try:
                        raw = self.reader.readtext(crop, detail=1, paragraph=False, min_size=self.ocr_min_size)
                    except Exception as e:
                        self.get_logger().warn(f"OCR 실패: {e}", throttle_duration_sec=2.0)
                        continue
                    for quad, text, conf in raw:
                        text = re.sub(r"\s+", "", text)
                        ok = (conf >= self.ocr_min_conf and self.num_re.fullmatch(text) is not None
                              and (self.valid_numbers is None or text in self.valid_numbers))
                        readings.append({"text": text, "conf": float(conf), "ok": ok, "assigned": False,
                                         "quad": np.asarray(quad, np.float32) / s + (x0, y0)})

                # 4. 숫자 -> 물체 짝짓기 + 다수결
                # 크롭이 겹치면 같은 스티커가 여러 번 읽히지만, 같은 물체로 할당되고 물체당 최고 conf만 쓰므로 무해.
                # pointPolygonTest: 안쪽이면 +경계까지 거리, 바깥이면 -거리 -> 가장 '안쪽'인 물체에 할당.
                for r in readings:
                    if not r["ok"]:
                        continue
                    c = tuple(float(v) for v in r["quad"].mean(axis=0))
                    d = [cv2.pointPolygonTest(o["poly"], c, True) for o in objs]
                    i = int(np.argmax(d))
                    if d[i] < -self.assign_tol:
                        continue
                    r["assigned"] = True
                    if objs[i]["best"] is None or r["conf"] > objs[i]["best"]["conf"]:
                        objs[i]["best"] = r
                for o in objs:
                    self.tracker.vote(o["track"], o["best"]["text"] if o["best"] is not None else None)

                # 5. 발행
                out = Detection2DArray(header=header)
                for o in objs:
                    t = o["track"]
                    nx, ny, _ = rs.rs2_deproject_pixel_to_point(
                        self.intr, [float(o["center"][0]), float(o["center"][1])], 1.0)
                    det = Detection2D(header=header, id=str(t["id"]))
                    det.bbox.center.position.x = nx * self.intr.fx + self.intr.ppx
                    det.bbox.center.position.y = ny * self.intr.fy + self.intr.ppy
                    hyp = ObjectHypothesisWithPose()
                    hyp.hypothesis.class_id = o["name"]
                    hyp.hypothesis.score = o["score"]
                    det.results.append(hyp)
                    if t["number"] is not None:
                        num = ObjectHypothesisWithPose()
                        num.hypothesis.class_id = f"num:{t['number']}"
                        num.hypothesis.score = t["score"]
                        det.results.append(num)
                    out.detections.append(det)
                self.det_pub.publish(out)                # 빈 배열도 발행: "이 시각엔 없음"도 정보다

                # 6. 디버그 이미지
                if self.dbg_pub is None and not self.show_window:
                    continue
                dbg = img.copy()
                for o in objs:
                    t = o["track"]
                    color = CLASS_COLORS.get(o["cls"], (255, 255, 255))
                    cv2.polylines(dbg, [o["poly"].astype(np.int32)], True, color, 3)
                    cv2.circle(dbg, (int(o["center"][0]), int(o["center"][1])), 6, (0, 0, 255), -1)
                    tx = int(o["poly"][:, 0].min())
                    ty = max(int(o["poly"][:, 1].min()) - 45, 25)   # 라벨은 물체 위 바깥 (스티커 안 가리게)
                    cv2.putText(dbg, f"#{t['id']} {o['name']} {o['score']:.2f}", (tx, ty),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.9, color, 2)
                    num = f"num:{t['number']} ({t['score']:.0%})" if t["number"] is not None else "num:?"
                    cv2.putText(dbg, num, (tx, ty + 32), cv2.FONT_HERSHEY_SIMPLEX, 0.9, color, 2)
                for r in readings:
                    color = (0, 255, 255) if r["assigned"] else ((128, 128, 128) if r["ok"] else (0, 0, 255))
                    cv2.polylines(dbg, [r["quad"].astype(np.int32)], True, color, 2)
                    qx, qy = r["quad"].min(axis=0)
                    cv2.putText(dbg, f"{r['text']} {r['conf']:.2f}", (int(qx), int(qy) - 6),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
                if self.debug_scale != 1.0:
                    dbg = cv2.resize(dbg, None, fx=self.debug_scale, fy=self.debug_scale,
                                     interpolation=cv2.INTER_AREA)
                if self.show_window:
                    with self.dbg_lock:
                        self.latest_dbg = dbg
                if self.dbg_pub is not None:
                    # cv_bridge 없이 직접 변환 (ROS Humble cv_bridge는 NumPy 버전에 민감)
                    dmsg = Image(header=header, height=dbg.shape[0], width=dbg.shape[1], encoding="bgr8",
                                 is_bigendian=0, step=dbg.shape[1] * 3)
                    dmsg.data = array.array("B", np.ascontiguousarray(dbg).tobytes())
                    self.dbg_pub.publish(dmsg)

            except Exception as e:
                self.get_logger().error(f"처리 루프 예외: {type(e).__name__}: {e}\n{traceback.format_exc()}",
                                        throttle_duration_sec=5.0)

    def _show(self):
        """메인 스레드 타이머: 새 디버그 프레임이 있으면 표시. waitKey는 창 이벤트 처리용으로 매번 호출."""
        with self.dbg_lock:
            frame, self.latest_dbg = self.latest_dbg, None
        if frame is not None:
            cv2.imshow("paper_bag_debug", frame)
        cv2.waitKey(1)

    def destroy_node(self):
        self.running = False
        if getattr(self, "worker", None) is not None:
            self.worker.join(timeout=3.0)
        try:
            self.pipeline.stop()
        except Exception:
            pass
        if self.show_window:
            cv2.destroyAllWindows()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = PaperBagOcrNode()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()