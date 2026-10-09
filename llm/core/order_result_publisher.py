#!/usr/bin/env python3
"""Publish completed LLM orders to the main node, using the ROS Python env."""
from __future__ import annotations

import fcntl
import json
from pathlib import Path

import rclpy
from rclpy.duration import Duration
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
from std_msgs.msg import String

from main_order_payload import OrderOutbox, validate_main_order


class OrderResultPublisher(Node):
    def __init__(self):
        super().__init__("llm_order_result_publisher")
        default_dir = Path(__file__).resolve().parents[1] / "runtime_data/handoffs/ros_outbox"
        self.declare_parameter("outbox_dir", str(default_dir))
        self.declare_parameter("order_topic", "/order")
        self.declare_parameter("main_node_name", "drive_thru_main")
        self.outbox = OrderOutbox(self.get_parameter("outbox_dir").value)
        self.target_name = self.get_parameter("main_node_name").value
        self._lock_file = (self.outbox.directory / ".publisher.lock").open("a")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_file.close()
            raise RuntimeError("This outbox already has an active order publisher")
        qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE,
                         durability=DurabilityPolicy.VOLATILE)
        self.publisher_ = self.create_publisher(
            String, self.get_parameter("order_topic").value, qos)
        self._inflight = None
        self._last_error = None
        self._waiting = False
        self.timer = self.create_timer(0.1, self.publish_pending)
        self.get_logger().info(
            f"ORDER PUB READY: {self.publisher_.topic_name}; outbox={self.outbox.directory}")

    def main_is_connected(self):
        # An echo/debug subscriber alone must not consume queued orders.
        return self.publisher_.get_subscription_count() > 0 and any(
            info.node_name == self.target_name and info.topic_type == "std_msgs/msg/String"
            for info in self.get_subscriptions_info_by_topic(self.publisher_.topic_name)
        )

    def publish_pending(self):
        try:
            if self._inflight is None:
                path = self.outbox.next_pending()
                if path is None:
                    return
                if not self.main_is_connected():
                    if not self._waiting:
                        self.get_logger().warning(
                            f"ORDER WAIT: {self.target_name} subscriber not ready; keeping pending orders")
                        self._waiting = True
                    return
                payload = validate_main_order(json.loads(path.read_text(encoding="utf-8")))
                if path.name != f"order_{payload['order_no']}.json":
                    raise ValueError("Filename and order_no do not match")
                self.publisher_.publish(String(data=json.dumps(payload, ensure_ascii=False)))
                self._inflight = path
                self._waiting = False
            # DDS receipt, not a business-level acknowledgement from on_order.
            # On timeout keep the in-flight message; do not publish it again per tick.
            if not self.publisher_.wait_for_all_acked(Duration(seconds=0.2)):
                return
            self.outbox.mark_sent(self._inflight)
            self.get_logger().info(f"ORDER SENT: {self._inflight.stem}")
            self._inflight = None
            self._last_error = None
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            error = str(exc)
            if error != self._last_error:
                self.get_logger().error(f"ORDER PUB ERROR (retained for retry): {error}")
                self._last_error = error

    def destroy_node(self):
        try:
            super().destroy_node()
        finally:
            self._lock_file.close()


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = OrderResultPublisher()
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        if node is not None:
            node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
