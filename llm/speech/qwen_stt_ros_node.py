#!/usr/bin/env python3
"""
qwen_stt_ros_node.py

어댑터는 인식만 하고, 로깅·저장·필터 정책은 여기서 정한다.

바뀐 것
  - spin_once 를 루프 맨 위로. 이전엔 발행 성공 시에만 돌아서, 조용한 동안 콜백이
    처리되지 않았다. /tts_done 같은 걸 구독하면 그 메시지가 안 들어온다.
  - device_index 하드코딩(5) 제거 -> ROS 파라미터
  - Silero 잡음 검증 on/off, 신뢰도 하한, 디버그 저장을 파라미터로
"""
from __future__ import annotations

import json
import time
import wave
from pathlib import Path

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

from qwen_stt_adapter import QwenSTTAdapter

SAMPLE_RATE = 16_000


class QwenSTTRosNode(Node):
    def __init__(self):
        super().__init__("qwen_stt_node")

        self.declare_parameter("device_index", -1)      # -1 = 시스템 기본
        self.declare_parameter("verify_speech", True)
        self.declare_parameter("silero_threshold", 0.5)
        self.declare_parameter("end_silence_ms", 600)
        self.declare_parameter("scored", False)
        self.declare_parameter("min_confidence", 0.0)   # 0.0 = 거르지 않고 기록만
        self.declare_parameter("debug_dir", "")

        g = lambda k: self.get_parameter(k).value       # noqa: E731
        dev, self.min_conf = g("device_index"), g("min_confidence")
        self.debug_dir = Path(g("debug_dir")).expanduser() if g("debug_dir") else None

        self.publisher = self.create_publisher(String, "/stt/text", 10)
        self.counts = {"seg": 0, "pub": 0, "noise": 0, "lowconf": 0}

        self.get_logger().info("Loading Qwen3-ASR...")
        self.stt = QwenSTTAdapter(
            device_index=None if dev < 0 else dev,
            verify_speech=g("verify_speech"),
            silero_threshold=g("silero_threshold"),
            end_silence_ms=g("end_silence_ms"),
            # 신뢰도를 쓰려면 계산도 켜야 한다
            scored=g("scored") or self.min_conf != 0.0,
        )
        self.get_logger().info(
            f"READY -> /stt/text | 잡음검증 {'켬' if g('verify_speech') else '끔'} "
            f"| 신뢰도하한 {self.min_conf if self.min_conf else '기록만'} "
            f"| 디버그 {self.debug_dir or '없음'}")

    def _dump(self, verdict: str, text: str, ev) -> None:
        """디스크를 아는 유일한 곳. 임계값을 데이터로 정하려고 남긴다."""
        if not self.debug_dir or ev is None or not ev.pcm:
            return
        try:
            d = self.debug_dir / verdict
            d.mkdir(parents=True, exist_ok=True)
            stem = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
            with wave.open(str(d / f"{stem}.wav"), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(ev.pcm)
            (d / f"{stem}.json").write_text(json.dumps({
                "ref": "", "hyp": text, "verdict": verdict,
                "vad_reason": ev.vad_reason, "false_starts": ev.false_starts,
                "noise_rejects": ev.noise_rejects,
                "silero_prob": ev.silero_prob, "silero_speech_ms": ev.silero_speech_ms,
                "confidence": ev.confidence, "audio_sec": round(ev.audio_sec, 3),
                "infer_ms": round(ev.infer_ms, 1),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            self.get_logger().warning(f"디버그 저장 실패: {e}")

    def run(self):
        while rclpy.ok():
            # 반드시 매 반복 맨 앞에서. 결과가 비어도 콜백은 처리돼야 한다.
            rclpy.spin_once(self, timeout_sec=0.0)

            result = self.stt.transcribe_once()
            ev = self.stt.last_evidence

            if not result.ok:
                self.get_logger().warning(f"STT ERROR: {result.error_code} {result.error_detail}")
                continue

            if ev:
                self.counts["noise"] += ev.noise_rejects
                if ev.pcm:
                    self.counts["seg"] += 1

            text = (result.transcript or "").strip()
            if not text:
                self.get_logger().debug(f"빈 결과: {result.error_detail}")
                continue

            conf = result.confidence
            if self.min_conf != 0.0 and conf is not None and conf < self.min_conf:
                self.counts["lowconf"] += 1
                self.get_logger().info(f"신뢰도 미달({conf:.2f}) 버림: {text}")
                self._dump("rejected_conf", text, ev)
                continue

            self.publisher.publish(String(data=text))
            self.counts["pub"] += 1
            self._dump("accepted", text, ev)

            extra = ""
            if ev and ev.silero_prob is not None:
                extra += f" silero={ev.silero_prob:.2f}"
            if conf is not None:
                extra += f" conf={conf:.2f}"
            self.get_logger().info(f"PUB /stt/text: {text}{extra}")

    def close(self):
        c = self.counts
        self.get_logger().info(
            f"분절 {c['seg']} | 발행 {c['pub']} | 잡음거부 {c['noise']} | 신뢰도거부 {c['lowconf']}")
        try:
            self.stt.close()
        except Exception:
            pass


def main():
    rclpy.init()
    node = QwenSTTRosNode()
    try:
        node.run()
    except KeyboardInterrupt:
        pass
    finally:
        node.close()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()