from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    arm_share = Path(
        get_package_share_directory("arm_control")
    )
    arm_yaml = arm_share / "config" / "arm_control.yaml"

    return LaunchDescription([
        Node(
            package="irc_main",
            executable="drive_thru_main",
            name="drive_thru_main",
            output="screen",
        ),
        Node(
            package="arm_control",
            executable="arm_control",
            arguments=[
                "--ros-args",
                "-r",
                "drive_thru_control:__node:=arm_control",
            ],
            output="screen",
            parameters=[
                str(arm_yaml),
                {
                    "workspace_root":
                    str(arm_share)
                },
            ],
        ),
    ])
