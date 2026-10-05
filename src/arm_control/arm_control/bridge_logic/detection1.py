class _EmbeddedBridgeBase:
    """Delegate ROS Node API calls to the owning ArmControlNode."""

    def __init__(self, parent_node):
        self._parent_node = parent_node

    def __getattr__(self, name):
        return getattr(self._parent_node, name)

    def destroy_node(self):
        # Embedded bridge logic does not own the parent ROS node.
        return None


import math
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
from sensor_msgs.msg import CameraInfo
from vision_msgs.msg import Detection2DArray
from soomac_interfaces.msg import DetectedItem
DETECTION_TOPIC = '/paper_bag/detections'
CAMERA_INFO_TOPIC = '/paper_bag/camera_info'
OUTPUT_TOPIC = '/drive_thru/detected_item'
VALID_OBJECT_TYPES = {'paper_bag', 'L_paper_bag', 'cup'}
R_CAM_TO_ARM = np.array([[0.99954869, -0.02825663, 0.01019687], [-0.0236476, -0.53079331, 0.84717133], [-0.01852578, -0.84703012, -0.53122196]], dtype=float)
T_CAM_TO_ARM_MM = np.array([293.40638902, -602.84060821, 399.16724562], dtype=float)
TABLE_Z_MM = 0.0
PAPER_BAG_HEIGHT_MM = 170.0
L_PAPER_BAG_HEIGHT_MM = 252.0
CUP_SUPPORT_Z_MM = 90.0
CUP_HEIGHT_MM = 140.0
OBJECT_CENTER_Z_MM = {'paper_bag': TABLE_Z_MM + 0.5 * PAPER_BAG_HEIGHT_MM, 'L_paper_bag': TABLE_Z_MM + 0.5 * L_PAPER_BAG_HEIGHT_MM, 'cup': CUP_SUPPORT_Z_MM + 0.5 * CUP_HEIGHT_MM}

class Detection1Bridge(_EmbeddedBridgeBase):
    """
    Convert the OCR-integrated /paper_bag/detections stream into DetectedItem.

    Detector contract:
      results[*].hypothesis.class_id = paper_bag | L_paper_bag
      results[*].hypothesis.class_id = num:<order_number>  (after OCR vote lock)

    The detector already performs temporal OCR voting, so this bridge does not
    wait for a separate OCR topic and does not do another N-frame lock.
    """

    def __init__(self, parent_node):
        _EmbeddedBridgeBase.__init__(self, parent_node)
        self.K = None
        self.last_log = {}
        self.pub = self.create_publisher(DetectedItem, OUTPUT_TOPIC, 10)
        camera_qos = QoSProfile(depth=1, history=HistoryPolicy.KEEP_LAST, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.create_subscription(CameraInfo, CAMERA_INFO_TOPIC, self.camera_info_cb, camera_qos)
        self.create_subscription(Detection2DArray, DETECTION_TOPIC, self.detections_cb, 10)
        self.get_logger().info(f'Detection1 OCR bridge ready | detection={DETECTION_TOPIC} | camera_info={CAMERA_INFO_TOPIC} | output={OUTPUT_TOPIC}')

    def camera_info_cb(self, msg):
        K = np.asarray(msg.k, dtype=float).reshape(3, 3)
        if not np.all(np.isfinite(K)):
            return
        if K[0, 0] <= 0.0 or K[1, 1] <= 0.0:
            return
        self.K = K

    @staticmethod
    def parse_detection(det):
        object_type = None
        object_conf = 0.0
        order_number = None
        number_conf = 0.0
        for result in det.results:
            class_id = str(result.hypothesis.class_id)
            score = float(result.hypothesis.score)
            if class_id in VALID_OBJECT_TYPES:
                if score >= object_conf:
                    object_type = class_id
                    object_conf = score
            elif class_id.startswith('num:'):
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

    def pixel_to_arm_xy(self, u, v, plane_z_mm=TABLE_Z_MM):
        if self.K is None:
            return None
        fx = float(self.K[0, 0])
        fy = float(self.K[1, 1])
        cx = float(self.K[0, 2])
        cy = float(self.K[1, 2])
        ray_cam = np.array([(float(u) - cx) / fx, (float(v) - cy) / fy, 1.0], dtype=float)
        ray_arm = R_CAM_TO_ARM @ ray_cam
        dz = float(ray_arm[2])
        if abs(dz) < 1e-09:
            return None
        scale = (float(plane_z_mm) - float(T_CAM_TO_ARM_MM[2])) / dz
        if not math.isfinite(scale) or scale <= 0.0:
            return None
        p_arm = T_CAM_TO_ARM_MM + scale * ray_arm
        if not np.all(np.isfinite(p_arm)):
            return None
        return (float(p_arm[0]), float(p_arm[1]))

    def detections_cb(self, msg):
        if self.K is None:
            return
        now_ns = self.get_clock().now().nanoseconds
        for det in msg.detections:
            parsed = self.parse_detection(det)
            if parsed is None:
                continue
            (object_type, object_conf, order_number, number_conf) = parsed
            pixel = self.pick_pixel(det)
            if pixel is None:
                continue
            plane_z_mm = OBJECT_CENTER_Z_MM.get(object_type)
            if plane_z_mm is None:
                continue
            xy = self.pixel_to_arm_xy(*pixel, plane_z_mm=plane_z_mm)
            if xy is None:
                continue
            (x_mm, y_mm) = xy
            out = DetectedItem()
            out.header = msg.header
            out.order_number = order_number
            out.object_type = object_type
            out.x_mm = x_mm
            out.y_mm = y_mm
            out.confidence = min(object_conf, number_conf)
            self.pub.publish(out)
            key = (str(det.id), order_number, object_type)
            last_ns = self.last_log.get(key, 0)
            if now_ns - last_ns >= 1000000000:
                self.last_log[key] = now_ns
                self.get_logger().info(f'DetectedItem | track={det.id} order={order_number} type={object_type} center_z={plane_z_mm:.1f} mm xy=({x_mm:.1f},{y_mm:.1f}) mm obj_conf={object_conf:.3f} num_vote={number_conf:.3f}')

def main(args=None):
    rclpy.init(args=args)
    node = Detection1Bridge()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
if __name__ == '__main__':
    main()
