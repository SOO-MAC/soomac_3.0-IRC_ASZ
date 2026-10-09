#!/usr/bin/env python3

import socket

import rclpy
from rclpy.node import Node
from std_msgs.msg import String


UDP_HOST = "127.0.0.1"
UDP_PORT = 5007

TTS_TOPIC = "/tts/text"


class TTSTextPublisher(Node):

    def __init__(self):

        super().__init__("tts_text_publisher")

        self.publisher_ = self.create_publisher(
            String,
            TTS_TOPIC,
            10,
        )

        self.sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
        )

        self.sock.bind(
            (UDP_HOST, UDP_PORT)
        )

        self.sock.setblocking(False)

        self.timer = self.create_timer(
            0.02,
            self.check_udp,
        )

        self.get_logger().info(
            "TTS TEXT Publisher READY"
        )

        self.get_logger().info(
            f"UDP RX  : {UDP_HOST}:{UDP_PORT}"
        )

        self.get_logger().info(
            f"ROS PUB : {TTS_TOPIC}"
        )


    def check_udp(self):

        try:
            data, _ = self.sock.recvfrom(65535)

        except BlockingIOError:
            return

        except OSError:
            return

        text = data.decode(
            "utf-8",
            errors="replace",
        ).strip()

        if not text:
            return

        msg = String()
        msg.data = text

        self.publisher_.publish(msg)

        self.get_logger().info(
            f"TX [{TTS_TOPIC}] {text}"
        )


    def destroy_node(self):

        try:
            self.sock.close()
        except Exception:
            pass

        super().destroy_node()


def main(args=None):

    rclpy.init(args=args)

    node = TTSTextPublisher()

    try:
        rclpy.spin(node)

    except KeyboardInterrupt:
        pass

    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
