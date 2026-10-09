#!/usr/bin/env python3

from pathlib import Path

from launch import LaunchDescription
from launch.actions import ExecuteProcess


def generate_launch_description():
    llm_dir = Path(__file__).resolve().parent

    stt_bridge = ExecuteProcess(
        cmd=[
            "python3",
            str(llm_dir / "speech/ros_stt_udp_bridge.py"),
        ],
        output="screen",
    )

    tts_publisher = ExecuteProcess(
        cmd=[
            "python3",
            str(llm_dir / "speech/tts_text_publisher.py"),
        ],
        output="screen",
    )

    order_publisher = ExecuteProcess(
        cmd=["python3", str(llm_dir / "core/order_result_publisher.py")],
        output="screen",
    )

    return LaunchDescription([
        stt_bridge,
        tts_publisher,
        order_publisher,
    ])
