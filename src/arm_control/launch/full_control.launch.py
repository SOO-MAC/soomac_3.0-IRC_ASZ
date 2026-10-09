from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    arm_share = Path(get_package_share_directory("arm_control"))
    arm_yaml = arm_share / "config" / "arm_control.yaml"

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "with_detection",
                default_value="false",
                description="Start Detection1 and Detection2 with the control nodes.",
            ),
            DeclareLaunchArgument(
                "nfc_serial_port",
                default_value="/dev/ttyACM0",
                description="ESP32-S3 USB serial port for RFID payment.",
            ),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    str(
                        Path(get_package_share_directory("drive_thru_detection"))
                        / "launch"
                        / "detection.launch.py"
                    )
                ),
                condition=IfCondition(LaunchConfiguration("with_detection")),
            ),
            Node(
                package="irc_main",
                executable="drive_thru_main",
                name="drive_thru_main",
                output="screen",
            ),
            Node(
                package="arm_control",
                executable="arm_control",
                name="arm_control",
                output="screen",
                parameters=[
                    str(arm_yaml),
                    {"workspace_root": str(arm_share)},
                ],
            ),
            Node(
                package="motor_control",
                executable="motor_control",
                name="motor_control_node",
                output="screen",
            ),
            Node(
                package="arm_control",
                executable="nfc_payment_bridge",
                name="nfc_payment_bridge",
                output="screen",
                parameters=[
                    {
                        "serial_port": LaunchConfiguration("nfc_serial_port"),
                        "baudrate": 115200,
                        "payment_service": "/payment_done",
                    }
                ],
            ),
        ]
    )
