from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():

    serial_port = LaunchConfiguration(
        "serial_port"
    )

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "serial_port",
                default_value="/dev/ttyACM0",
                description=(
                    "ESP32-S3 serial port "
                    "for drive-thru RFID payment"
                ),
            ),

            Node(
                package="arm_control",
                executable="drivethru_NFC",
                name="drivethru_NFC",
                output="screen",
                parameters=[
                    {
                        "serial_port":
                            serial_port,

                        "baudrate":
                            115200,

                        "payment_service":
                            "/payment_done",
                    }
                ],
            ),
        ]
    )
