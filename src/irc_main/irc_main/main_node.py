#!/usr/bin/env python3
"""
드라이브스루 main 노드 (기본 메시지만 사용: std_msgs, std_srvs)

  차량 진입(Arduino) → LLM 주문 시작 → 주문 결과(JSON 문자열)를 order_list 에 순서대로 적재
  WAIT_CAR ─운전자 인식(pose OFF)─┬─ 일반  → arm/pay → TO_PAY ─arm/done→ WAIT_PAY ─결제 완료→ arm/deliver → DELIVERING
                                  └─ 맥오더 → arm/deliver → DELIVERING
  DELIVERING ─arm/done→ order_list[0] 삭제 → WAIT_CAR, 7초 뒤 pose ON

주문 JSON (LLM 노드에서 json.dumps): {"order_no": 1, "menu": "빅맥 세트", "is_mcorder": false, "price": 7300}
arm_control 에는 주문이 맨 앞(order_list[0])이 되는 순간 arm/order 로 한 번 보내고, 이동 명령은 내용 없는 Trigger.

모든 콜백은 단일 스레드 executor 에서 논블로킹(async)으로 처리하므로 락이 필요 없다.
"""
import json
from collections import deque
from enum import Enum, auto

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from std_msgs.msg import Empty, String
from std_srvs.srv import SetBool, Trigger

POSE_REARM_SEC = 7.0


class State(Enum):
    WAIT_CAR = auto()
    TO_PAY = auto()
    WAIT_PAY = auto()
    DELIVERING = auto()


class DriveThruMain(Node):
    def __init__(self):
        super().__init__('drive_thru_main')
        self.order_list = deque()
        self.state = State.WAIT_CAR

        self.tts = self.create_publisher(String, 'tts', 10)
        self.arm_order = self.create_publisher(String, 'arm/order', 10)
        self.llm_cli = self.create_client(Trigger, 'llm/start_order')
        self.pose_cli = self.create_client(SetBool, 'pose_estimation/enable')
        self.arm_cli = {t: self.create_client(Trigger, f'arm/{t}') for t in ('pay', 'deliver')}

        self.create_subscription(Empty, 'car_entered', self.on_car_entered, 10)
        self.create_subscription(String, 'order', self.on_order, 10)
        self.create_service(Trigger, 'driver_detected', self.on_driver_detected)
        self.create_service(Trigger, 'payment_done', self.on_payment_done)
        self.create_service(Trigger, 'arm/done', self.on_arm_done)

        self.rearm_timer = self.create_timer(POSE_REARM_SEC, self.on_rearm)
        self.rearm_timer.cancel()
        self.get_logger().info('drive_thru_main ready')

    def on_car_entered(self, _):
        self.get_logger().info('차량 진입 → LLM 주문 시작')
        self.llm_cli.call_async(Trigger.Request())

    def on_order(self, msg):
        try:
            raw = json.loads(msg.data)
            order = {'order_no': int(raw['order_no']), 'menu': raw['menu'],
                     'is_mcorder': raw['is_mcorder'], 'price': raw['price']}
        except (ValueError, KeyError, TypeError) as e:
            self.get_logger().error(f'주문 파싱 실패 {msg.data!r}: {e!r}')
            return
        if any(o['order_no'] == order['order_no'] for o in self.order_list):
            self.get_logger().error(f'중복 주문번호 #{order["order_no"]} 무시')
            return
        self.order_list.append(order)
        self.get_logger().info(f'주문 #{order["order_no"]} {order["menu"]} 적재 (대기 {len(self.order_list)})')
        if len(self.order_list) == 1:
            self.send_head()

    def on_driver_detected(self, req, res):
        if self.state != State.WAIT_CAR or not self.order_list:
            return res
        order = self.order_list[0]
        if not self.command('deliver' if order['is_mcorder'] else 'pay'):
            return res
        res.success = True
        self.pose_cli.call_async(SetBool.Request(data=False))
        if order['is_mcorder']:
            self.say(f"{order['menu']} 준비해 드릴게요.")
        return res

    def on_payment_done(self, req, res):
        if self.state == State.WAIT_PAY and self.command('deliver'):
            res.success = True
            self.say('결제 완료되었습니다. 상품 바로 전달해 드릴게요.')
        return res

    def on_arm_done(self, req, res):
        if self.state == State.TO_PAY:
            self.state = State.WAIT_PAY
            order = self.order_list[0]
            self.say(f"{order['menu']} 총 {order['price']}원입니다. 카드를 단말기에 대주세요.")
        elif self.state == State.DELIVERING:
            done = self.order_list.popleft()
            self.state = State.WAIT_CAR
            self.rearm_timer.reset()
            self.send_head()
            self.get_logger().info(f'주문 #{done["order_no"]} 완료 (대기 {len(self.order_list)})')
        else:
            return res
        res.success = True
        return res

    def command(self, task):
        cli = self.arm_cli[task]
        if not cli.service_is_ready():
            self.get_logger().error(f'arm/{task} 서버 없음')
            return False
        self.state = State.TO_PAY if task == 'pay' else State.DELIVERING

        def on_response(f):
            if not f.result().success:
                self.get_logger().error(f'arm/{task} 거부: {f.result().message}')
        cli.call_async(Trigger.Request()).add_done_callback(on_response)
        return True

    def send_head(self):
        if self.order_list:
            self.arm_order.publish(String(data=json.dumps(self.order_list[0], ensure_ascii=False)))

    def on_rearm(self):
        self.rearm_timer.cancel()
        self.pose_cli.call_async(SetBool.Request(data=True))

    def say(self, text):
        self.get_logger().info(f'[TTS] {text}')
        self.tts.publish(String(data=text))


def main():
    rclpy.init()
    node = DriveThruMain()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
