#!/usr/bin/env python3
"""
deliver_tts_node.py - 결제·전달 창구 안내 TTS (ROS 2 Humble / Python 3.10)

main.py(drive_thru_main)가 'tts' 토픽으로 보내는 문장을 창구 스피커로 말한다.
언제·무엇을 말할지는 main 의 상태 기계가 정한다.

  main.say("결제 완료되었습니다. 상품 바로 전달해 드릴게요.")  ->  /tts (std_msgs/String 평문)
  arm/done (TO_PAY)  -> "{menu} 총 {price}원입니다. 카드를 단말기에 대주세요."
  payment_done       -> "결제 완료되었습니다. 상품 바로 전달해 드릴게요."
  driver_detected(맥오더) -> "맥오더 {order_no}번 준비해 드릴게요."

구현은 tts_node.TTSNode 를 그대로 쓰고 기본값만 바꾼다. 수신 출력([RX]), 퍼블리셔 감시([PUB]),
숫자 읽기, 캐시, 상태 이벤트가 tts_node 와 같다.

  구독  /tts               std_msgs/String  (main.py 의 'tts')
        /deliver_tts/stop  std_msgs/Empty
  발행  /deliver_tts/status std_msgs/String JSON (accepted/started/done/stopped/error/heartbeat)

실행
  source /opt/ros/humble/setup.bash
  python3 deliver_tts_node.py --ros-args -p output_device:=3      # 창구 스피커 번호
  (스피커 번호: python3 tts_core.py --list-devices)

테스트 (main 없이)
  ros2 topic pub --once /tts std_msgs/msg/String "{data: '빅맥 세트 총 7300원입니다. 카드를 단말기에 대주세요.'}"
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rclpy                                         # noqa: E402

from tts_node import TTSNode                         # noqa: E402

# tts_node 와 겹치지 않게 토픽·캐시 폴더를 나눈다. --ros-args -p 로 넘긴 값이 항상 우선이다.
DELIVER_DEFAULTS = {
    "say_topic": "/tts",                       # main.py: create_publisher(String, 'tts', 10)
    "stop_topic": "/deliver_tts/stop",
    "status_topic": "/deliver_tts/status",
    "cache_dir": "runtime_data/deliver_tts_cache",
}

# main.py 문장 중 빈칸이 없는 문장. 캐시는 문장 단위라서
# "{menu} 총 {price}원입니다. 카드를 단말기에 대주세요." 의 뒷문장도 미리 만들어 둘 수 있다.
# main 의 문구를 바꾸면 여기도 같이 바꾼다(안 바꿔도 동작은 하고, 처음 한 번만 합성한다).
DELIVER_PHRASES = (
    "결제 완료되었습니다. 상품 바로 전달해 드릴게요.",     # on_payment_done
    "카드를 단말기에 대주세요.",                          # on_arm_done (TO_PAY) 뒷문장
)


class DeliverTTSNode(TTSNode):
    def __init__(self):
        super().__init__(
            "deliver_tts_node",
            defaults=DELIVER_DEFAULTS,
            precache_phrases=DELIVER_PHRASES,
        )


def main(args=None) -> None:
    rclpy.init(args=args)
    node = None
    try:
        node = DeliverTTSNode()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.close()
            node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()