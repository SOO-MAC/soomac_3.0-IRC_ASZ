from pathlib import Path
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    models = Path(get_package_share_directory("drive_thru_detection")) / "models"
    return LaunchDescription(
        [
            Node(
                package="drive_thru_detection",
                executable="paper_bag_detector",
                output="screen",
                parameters=[
                    {"weights_path": str(models / "paper_bag_best.pt"), "device": "0"}
                ],
            ),
            Node(
                package="drive_thru_detection",
                executable="pose_node",
                name="driver_pose",
                output="screen",
                parameters=[{"weights": str(models / "yolo11m-pose.pt")}],
            ),
        ]
    )
