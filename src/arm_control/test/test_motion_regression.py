"""Baseline motion checkpoints from 78d4bec; calculation only, no ROS node or motor requests."""

import unittest

import numpy as np

from arm_control import robot_config
from arm_control.arm_control_node import (
    CupPickMotion,
    HandoffMotion,
    PaperBagMotion,
    camera_to_base,
)


class MotionRegressionTests(unittest.TestCase):
    def test_paper_bag_pick_checkpoint(self):
        radius, plan = PaperBagMotion.build_dynamic_pick_plan(250.0, -125.0)
        self.assertAlmostEqual(radius, 279.5084971874737)
        np.testing.assert_array_equal(
            plan["GRASP"]["ticks"], [1648, 2434, 6840, 2969, 2048]
        )
        np.testing.assert_array_equal(
            plan["PULL100"]["ticks"], [1648, 1162, 7518, 3088, 2048]
        )

    def test_cup_pull_checkpoints(self):
        for x, y, pull, ticks in [
            (80.0, 500.0, 200.0, [2871, -308, 10364, 1789, 2048]),
            (120.0, 450.0, 160.0, [2804, -323, 10369, 1791, 2048]),
            (120.0, 400.0, 100.0, [2784, -60, 10279, 1756, 2048]),
        ]:
            with self.subTest(x=x, y=y):
                plan = CupPickMotion.build_dynamic_cup_pick_plan(x, y)
                self.assertEqual(plan["pull_distance_mm"], pull)
                np.testing.assert_array_equal(plan["PULL_FINAL"]["ticks"], ticks)

    def test_workspace_rejection(self):
        with self.assertRaisesRegex(RuntimeError, "outside safe workspace"):
            PaperBagMotion.build_dynamic_pick_plan(5000.0, 5000.0)

    def test_driver_direction_rejection(self):
        with self.assertRaisesRegex(RuntimeError, "not behind robot"):
            HandoffMotion.compute_handoff_target([700.0, 100.0, 300.0], 400.0)

    def test_camera2_calibration(self):
        np.testing.assert_allclose(
            camera_to_base(0.1, -0.2, 0.7), [-0.787, 0.1, 0.2185], rtol=0.0, atol=1e-15
        )

    def test_hardware_tick_limits(self):
        self.assertEqual(
            (robot_config.J3_MIN_TICK, robot_config.J3_MAX_TICK), (0, 10606)
        )
        self.assertEqual(
            (robot_config.J4_MIN_TICK, robot_config.J4_MAX_TICK), (700, 3500)
        )
        for ticks in [[1950, -200, 10607, 3200, 2048], [1950, -200, 7300, 3501, 2048]]:
            with self.subTest(ticks=ticks), self.assertRaises(ValueError):
                robot_config.validate_arm_ticks(ticks)


if __name__ == "__main__":
    unittest.main()
