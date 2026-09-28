#!/usr/bin/env python3

import socket

import rclpy
from rclpy.node import Node
from std_msgs.msg import String


UDP_HOST = "127.0.0.1"
UDP_PORT = 5006


class RosSTTUdpBridge(Node):

    def __init__(self):
        super().__init__("ros_stt_udp_bridge")

        self.sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
        )

        self.subscription = self.create_subscription(
            String,
            "/stt/text",
            self.on_stt,
            10,
        )

        self.get_logger().info(
            f"/stt/text -> udp://{UDP_HOST}:{UDP_PORT}"
        )

    def on_stt(self, msg):

        text = (msg.data or "").strip()

        if not text:
            return

        self.sock.sendto(
            text.encode("utf-8"),
            (UDP_HOST, UDP_PORT),
        )

        self.get_logger().info(
            f"RX /stt/text: {text}"
        )

    def destroy_node(self):
        self.sock.close()
        super().destroy_node()


def main():

    rclpy.init()

    node = RosSTTUdpBridge()

    try:
        rclpy.spin(node)

    except KeyboardInterrupt:
        pass

    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
