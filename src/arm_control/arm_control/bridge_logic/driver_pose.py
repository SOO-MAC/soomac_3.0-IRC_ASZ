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
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import PointStamped
IN_TOPIC = '/driver_pose/target'
OUT_TOPIC = '/driver_pose/target_base'

def camera_to_base(xc, yc, zc):
    xb = -zc - 0.087
    yb = +xc
    zb = -yc + 0.0185
    return (xb, yb, zb)

class DriverPoseBaseBridge(_EmbeddedBridgeBase):

    def __init__(self, parent_node):
        _EmbeddedBridgeBase.__init__(self, parent_node)
        self.pub = self.create_publisher(PointStamped, OUT_TOPIC, 10)
        self.create_subscription(PointStamped, IN_TOPIC, self.cb, 10)
        self.get_logger().info(f'{IN_TOPIC} -> {OUT_TOPIC}')

    def cb(self, msg):
        xc = float(msg.point.x)
        yc = float(msg.point.y)
        zc = float(msg.point.z)
        if not all((math.isfinite(v) for v in (xc, yc, zc))):
            return
        (xb, yb, zb) = camera_to_base(xc, yc, zc)
        out = PointStamped()
        out.header = msg.header
        out.header.frame_id = 'arm_base'
        out.point.x = xb
        out.point.y = yb
        out.point.z = zb
        self.pub.publish(out)

def main():
    rclpy.init()
    node = DriverPoseBaseBridge()
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
