#!/usr/bin/env python3

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    share_dir = Path(
        get_package_share_directory("drive_thru_manager")
    )

    weights = str(
        share_dir
        / "models"
        / "paper_bag_best.pt"
    )

    detector = Node(
        package="drive_thru_manager",
        executable="paper_bag_detector",
        output="screen",
        parameters=[
            {
                "weights_path": weights,
                "serial_no": "048522072487",
                "device": "cpu",
            }
        ],
    )

    return LaunchDescription([
        detector,
    ])
