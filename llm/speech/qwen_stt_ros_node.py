#!/usr/bin/env python3
"""
qwen_stt_ros_node.py

어댑터는 인식만 하고, 정책(언제 들을지, 무엇을 발행할지)은 여기서 정한다.

 /stt/text 발행 ──┐
                   │ wait_tts   LLM 생각 중
                   │ speaking   TTS 말하는 중
                   │ guard      잔향 400ms
                   └─ listen    여기서 다시 수음
                   
    발행 -> TurnGate 가 닫음 -> /tts/status 의 done + 잔향 -> 다시 듣기

    워치독이 둘 있다. 이게 없으면 신호 한 번 유실에 손님 앞에서 영구히 귀머거리가 된다.
        llm_timeout_s   발행 후 TTS 가 시작되지 않으면 포기하고 다시 듣는다
        tts_timeout_s   종료 신호를 전혀 못 받았을 때의 절대 상한
    평소에는 heartbeat 가 1~2초 안에 교정하므로 워치독까지 가지 않는다.

QoS 주의
    /tts/status 구독은 tts_node 의 퍼블리셔와 같아야 한다 (RELIABLE + VOLATILE).
    TRANSIENT_LOCAL 로 요청하면 "requesting incompatible QoS. No messages will be sent"
    가 뜨고 상태를 하나도 못 받아 매 턴 워치독이 발동한다.

실행
    python3 qwen_stt_ros_node.py
    python3 qwen_stt_ros_node.py --ros-args -p device_index:=5 -p guard_ms:=600
    python3 qwen_stt_ros_node.py --ros-args -p gate_enabled:=false    # 게이트 끄고 비교
"""
from __future__ import annotations

import inspect
import json
import threading
import time

import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from std_msgs.msg import String

from qwen_stt_adapter import QwenSTTAdapter
from stt_guard import guard_stt_result
from turn_gate import LISTEN, SPEAKING, TurnGate, TurnGateConfig

STT_TEXT_TOPIC = "/stt/text"
TTS_STATUS_TOPIC = "/tts/status"
GATE_TOPIC = "/stt/gate"          # 게이트 상태를 외부에서 볼 수 있게 (디버깅용)


class QwenSTTRosNode(Node):
    def __init__(self):
        super().__init__("qwen_stt_node")

        # ---------------------------------------------------------- 파라미터
        self.declare_parameter("device_index", -1)       # -1 = 시스템 기본
        self.declare_parameter("verify_speech", True)
        self.declare_parameter("silero_threshold", 0.5)
        self.declare_parameter("end_silence_ms", 600)
        self.declare_parameter("min_confidence", 0.0)    # 0.0 = 거르지 않음

        # 게이트
        self.declare_parameter("gate_enabled", True)
        self.declare_parameter("tts_status_topic", TTS_STATUS_TOPIC)
        self.declare_parameter("guard_ms", 400)          # TTS 종료 후 잔향 여유
        self.declare_parameter("llm_timeout_s", 8.0)     # LLM 무응답 워치독
        self.declare_parameter("tts_timeout_s", 30.0)    # TTS 종료 신호 누락 워치독
        self.declare_parameter("publish_gate_state", True)

        # /stt/text 발행 QoS.
        # 구독자(ros_stt_udp_bridge)는 RELIABLE + VOLATILE 로 확인됐다.
        # TRANSIENT_LOCAL 퍼블리셔는 VOLATILE 구독자와 호환되지만, 과거 메시지를
        # 다시 주는 효과는 구독자도 TRANSIENT_LOCAL 일 때만 생긴다. 즉 지금은
        # 얻는 것도 잃는 것도 없다. 동료가 구독자를 바꾸면 그때 같이 맞추면 된다.
        # depth 1 은 혹시 바뀌어도 되살아나는 주문을 직전 한 건으로 묶어두는 보험이다.
        self.declare_parameter("text_durability", "transient_local")   # transient_local | volatile
        self.declare_parameter("text_depth", 1)

        g = lambda k: self.get_parameter(k).value        # noqa: E731
        dev = g("device_index")
        self.min_conf = g("min_confidence")
        self.publish_gate_state = bool(g("publish_gate_state"))

        # ---------------------------------------------------------- 발행
        durability = (DurabilityPolicy.VOLATILE
                      if str(g("text_durability")).lower() == "volatile"
                      else DurabilityPolicy.TRANSIENT_LOCAL)
        self.publisher = self.create_publisher(
            String, STT_TEXT_TOPIC,
            QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=int(g("text_depth")),
                       reliability=ReliabilityPolicy.RELIABLE, durability=durability))

        self.gate_pub = (self.create_publisher(String, GATE_TOPIC, 10)
                         if self.publish_gate_state else None)

        # ---------------------------------------------------------- 게이트
        self.gate_enabled = bool(g("gate_enabled"))
        self.gate = TurnGate(TurnGateConfig(
            guard_ms=int(g("guard_ms")),
            llm_timeout_s=float(g("llm_timeout_s")),
            tts_timeout_s=float(g("tts_timeout_s")),
        ))
        self._tts_status_topic = g("tts_status_topic")

        # 수음 중단용. 콜백 스레드가 set 하고 메인 스레드가 읽는다.
        # _cap_lock 은 "지금 수음 중인가" 와 cancel 초기화를 원자적으로 묶는다.
        # 묶지 않으면 수음 시작 직전에 온 accepted 를 흘려 한 턴을 오염된 채로 돌린다.
        self._cancel = threading.Event()
        self._capturing = False
        self._cap_lock = threading.Lock()

        # tts_node 의 status 퍼블리셔와 같은 QoS 여야 한다. 다르면 한 건도 안 온다.
        self.create_subscription(
            String, self._tts_status_topic, self._on_tts_status,
            QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=50,
                       reliability=ReliabilityPolicy.RELIABLE,
                       durability=DurabilityPolicy.VOLATILE))

        # TTS 노드가 없으면 기다릴 이유가 없다. 1초마다 확인한다.
        # 실행기가 백그라운드에서 도므로 수음 중에도 이 타이머가 산다.
        self._tts_seen = None
        self.create_timer(1.0, self._watch_tts)

        self.counts = {"pub": 0, "reject": 0, "empty": 0, "error": 0, "abort": 0}

        # ---------------------------------------------------------- 모델
        self.get_logger().info("Loading Qwen3-ASR...")
        self.stt = QwenSTTAdapter(
            device_index=None if dev < 0 else dev,
            verify_speech=bool(g("verify_speech")),
            silero_threshold=float(g("silero_threshold")),
            end_silence_ms=int(g("end_silence_ms")),
        )

        # 어댑터가 중단을 지원하는지 먼저 확인한다. 인자 불일치로 죽는 것도,
        # 조용히 반쪽으로 도는 것도 둘 다 싫다.
        try:
            params = inspect.signature(self.stt.transcribe_once).parameters
            self._can_cancel = "cancel" in params
        except (TypeError, ValueError):
            self._can_cancel = False
        if not self._can_cancel:
            self.get_logger().error(
                "★어댑터의 transcribe_once() 가 cancel 인자를 받지 않는다. "
                "수음 도중 TTS 가 말하면 그 소리를 그대로 받아적는다. "
                "transcribe_once(self, cancel=None) 로 고치고 "
                "segmenter.collect(cancel or threading.Event()) 로 넘겨라.")

        self.get_logger().info(
            f"READY {STT_TEXT_TOPIC} <- 게이트 "
            f"{'켬' if self.gate_enabled else '끔'} "
            f"(잔향 {g('guard_ms')}ms, LLM 워치독 {g('llm_timeout_s')}초, "
            f"TTS 워치독 {g('tts_timeout_s')}초, "
            f"수음중단 {'가능' if self._can_cancel else '불가'}) "
            f"| 상태 구독 {self._tts_status_topic}")

    # -------------------------------------------------------------- 로그
    def _flush_log(self) -> None:
        for line in self.gate.drain_log():
            self.get_logger().info(f"게이트: {line}")

    # -------------------------------------------------------------- 콜백
    def _on_tts_status(self, msg: String) -> None:
        """백그라운드 실행기 스레드에서 돈다. 메인 루프가 수음 중일 때도 산다."""
        if not self.gate_enabled:
            return
        try:
            ev = json.loads(msg.data)
        except ValueError:
            return
        if not isinstance(ev, dict):
            return

        before = self.gate.state
        self.gate.on_tts_status(ev)
        state = self.gate.state

        # 수음 중에 스피커가 울리기 시작했다. 지금 모으고 있는 오디오는 오염됐다.
        # 끝까지 모아서 추론해봐야 로봇 목소리를 받아적을 뿐이니 여기서 끊는다.
        barge = False
        if state == SPEAKING:
            with self._cap_lock:
                if self._capturing and not self._cancel.is_set():
                    self._cancel.set()
                    barge = True

        # heartbeat 는 1초마다 오므로 상태가 바뀔 때만 찍는다. 아니면 로그가 못 쓰게 된다.
        self._flush_log()
        if barge:
            self.get_logger().warning("수음 중단: TTS 가 말하기 시작했다")
        if state != before:
            self._publish_gate_state()

    def _watch_tts(self) -> None:
        """
        /tts/status 퍼블리셔가 있는지 1초마다 본다.

        count_publishers 는 로컬 그래프 캐시 조회라 비용이 거의 없다.
        실행기가 백그라운드 스레드라 수음 중에도 이 타이머가 제때 돈다.
        (예전에는 메인 루프가 spin 을 쥐고 있어 STT 를 먼저 띄우면 TTS 를 한참 못 찾았다.)
        """
        present = self.count_publishers(self._tts_status_topic) > 0
        if present == self._tts_seen:
            return
        self._tts_seen = present
        if self.gate_enabled:
            self.gate.set_tts_present(present)
        self._flush_log()
        if not present:
            self.get_logger().warning(
                f"{self._tts_status_topic} 퍼블리셔 0개 -> 게이트 비활성, 계속 듣는다. "
                f"TTS 를 나중에 띄워도 된다. 발견되면 '게이트: TTS 노드 연결' 이 뜬다")
        else:
            self.get_logger().info(
                f"{self._tts_status_topic} 퍼블리셔 발견 -> 게이트 활성")

    def _publish_gate_state(self) -> None:
        if self.gate_pub is None:
            return
        self.gate_pub.publish(String(data=json.dumps({
            "state": self.gate.state,
            "listening": self.gate.state == LISTEN,
            "tts_active": self.gate.active,
            "t": round(time.monotonic(), 3),
        }, ensure_ascii=False)))

    # -------------------------------------------------------------- 대기
    def _wait_until_listening(self) -> bool:
        """
        게이트가 열릴 때까지 기다린다. 메인 스레드에서만 부른다.

        spin 은 백그라운드 실행기가 돌리므로 여기서는 재우기만 하면 된다.
        update() 는 시간 기반 전이(잔향 만료, 워치독)를 진행시키는 역할이라
        주기적으로 불러야 한다.
        """
        if not self.gate_enabled:
            return True

        reason = self.gate.update()
        self._flush_log()
        if reason is None:
            return True

        last = reason
        self._publish_gate_state()
        while rclpy.ok():
            time.sleep(0.02)                 # 50Hz. 잔향 400ms 에 비하면 충분히 잘다.
            reason = self.gate.update()
            self._flush_log()
            if reason is None:
                self._publish_gate_state()
                return True
            if reason != last:
                last = reason
                self._publish_gate_state()
        return False

    def _begin_capture(self) -> bool:
        """
        수음을 시작해도 되는지 최종 확인하고 플래그를 세운다.

        _wait_until_listening() 이 돌려준 뒤 여기까지 오는 사이에 accepted 가 올 수 있다.
        그 창을 닫으려고 상태 재확인과 cancel 초기화를 한 락 안에서 한다.
        """
        with self._cap_lock:
            if self.gate_enabled and self.gate.state != LISTEN:
                return False
            self._cancel.clear()
            self._capturing = True
            return True

    def _end_capture(self) -> None:
        with self._cap_lock:
            self._capturing = False

    # -------------------------------------------------------------- 메인 루프
    def run(self) -> None:
        while rclpy.ok():
            if not self._wait_until_listening():
                return
            if not self._begin_capture():
                continue                      # 방금 TTS 가 끼어들었다. 다시 기다린다.

            try:
                result = (self.stt.transcribe_once(cancel=self._cancel)
                          if self._can_cancel else self.stt.transcribe_once())
            finally:
                self._end_capture()

            # 중단된 수음은 통째로 버린다. 게이트는 이미 SPEAKING 이므로
            # on_not_published() 를 부르면 안 된다. 그걸 부르면 TTS 가 말하는
            # 중간에 마이크가 다시 열린다.
            if self._cancel.is_set():
                self.counts["abort"] += 1
                self.get_logger().info("수음 폐기: TTS 재생과 겹쳤다")
                continue

            if not result.ok:
                self.counts["error"] += 1
                if self.gate_enabled:
                    self.gate.on_not_published()
                self.get_logger().warning(
                    f"STT ERROR: {result.error_code} {result.error_detail}")
                continue

            text = (result.transcript or "").strip()
            if not text:
                # 무발화·취소. 잡음은 Segmenter 가 이미 걸렀다.
                self.counts["empty"] += 1
                if self.gate_enabled:
                    self.gate.on_not_published()
                self.get_logger().debug(f"빈 결과: {result.error_detail}")
                continue

            decision = guard_stt_result(result, min_confidence=self.min_conf or None)
            if not decision.allow:
                # decision.reply 가 되물을 문장이다. 지금은 LLM 이 재질문을 맡으므로
                # 여기서 /tts/text 로 보내지 않는다. 보내려면 LLM 상태와 합의가 필요하다.
                self.counts["reject"] += 1
                if self.gate_enabled:
                    self.gate.on_not_published()
                self.get_logger().info(
                    f"가드 거부({decision.reason}): {text}"
                    + (f" | {decision.detail}" if decision.detail else ""))
                continue

            self.publisher.publish(String(data=decision.text))
            self.counts["pub"] += 1
            self.get_logger().info(f"PUB {STT_TEXT_TOPIC}: {decision.text}")

            # 여기서부터 응답이 끝날 때까지 마이크를 닫는다.
            if self.gate_enabled:
                self.gate.on_published()
                self._flush_log()
                self._publish_gate_state()

    def close(self) -> None:
        c = self.counts
        self.get_logger().info(
            f"발행 {c['pub']} | 가드거부 {c['reject']} | 빈결과 {c['empty']} | "
            f"오류 {c['error']} | 수음폐기 {c['abort']}")
        self.get_logger().info(self.gate.summary())
        try:
            self.stt.close()
        except Exception:
            pass


def main() -> None:
    rclpy.init()
    node = QwenSTTRosNode()

    # 콜백을 백그라운드로 뺀다. 메인 스레드는 수음·추론으로 몇 초씩 블로킹하는데,
    # 그 사이에도 /tts/status 와 1초 타이머가 살아 있어야 한다.
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    spin_thread = threading.Thread(target=executor.spin, name="ros-spin", daemon=True)
    spin_thread.start()

    try:
        node.run()
    except KeyboardInterrupt:
        pass
    finally:
        node.close()
        executor.shutdown()
        spin_thread.join(timeout=1.0)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()