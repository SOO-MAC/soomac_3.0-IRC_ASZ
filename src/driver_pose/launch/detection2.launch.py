from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    share_dir = Path(
        get_package_share_directory("driver_pose")
    )

    weights = str(
        share_dir
        / "models"
        / "yolo11m-pose.pt"
    )

    return LaunchDescription([
        Node(
            package="driver_pose",
            executable="pose_node",
            name="driver_pose",
            output="screen",
            parameters=[
                {
                    "weights": weights,
                    "serial_no": "048522073427",
                }
            ],
        ),
    ])
