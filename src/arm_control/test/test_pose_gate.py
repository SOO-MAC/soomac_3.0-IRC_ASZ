"""Check Main's driver-input switch without creating a ROS node or motor clients."""

import threading
import unittest
from types import SimpleNamespace

from geometry_msgs.msg import PointStamped
from std_srvs.srv import SetBool

from arm_control.arm_control_node import ArmControlNode


class PoseInputHarness:
    pose_enable_cb = ArmControlNode.pose_enable_cb
    driver_camera_pose_cb = ArmControlNode.driver_camera_pose_cb

    def __init__(self):
        self.state_lock = threading.RLock()
        self.pose_estimation_enabled = True
        self.lock_inputs = []
        self.runner_inputs = []
        self.published = []
        self.runner = SimpleNamespace(pose_cb=self.runner_inputs.append)
        self._driver_pose_base_pub = SimpleNamespace(publish=self.published.append)

    def get_logger(self):
        return SimpleNamespace(info=lambda _: None)

    def driver_pose_cb(self, msg):
        self.lock_inputs.append(msg)


class PoseGateTests(unittest.TestCase):
    def setUp(self):
        self.harness = PoseInputHarness()
        self.pose = PointStamped()
        self.pose.point.x = 0.1
        self.pose.point.y = -0.2
        self.pose.point.z = 0.7

    def switch(self, enabled):
        return self.harness.pose_enable_cb(
            SetBool.Request(data=enabled), SetBool.Response()
        )

    def test_off_blocks_runner_lock_and_base_output(self):
        self.assertTrue(self.switch(False).success)
        self.harness.driver_camera_pose_cb(self.pose)
        self.assertEqual(self.harness.lock_inputs, [])
        self.assertEqual(self.harness.runner_inputs, [])
        self.assertEqual(self.harness.published, [])

    def test_rearm_accepts_and_transforms_once(self):
        self.switch(False)
        self.assertTrue(self.switch(True).success)
        self.harness.driver_camera_pose_cb(self.pose)
        self.assertEqual(len(self.harness.lock_inputs), 1)
        self.assertEqual(len(self.harness.runner_inputs), 1)
        self.assertEqual(len(self.harness.published), 1)
        out = self.harness.published[0]
        self.assertAlmostEqual(out.point.x, -0.787)
        self.assertAlmostEqual(out.point.y, 0.1)
        self.assertAlmostEqual(out.point.z, 0.2185)
        self.assertEqual(out.header.frame_id, "arm_base")


if __name__ == "__main__":
    unittest.main()
