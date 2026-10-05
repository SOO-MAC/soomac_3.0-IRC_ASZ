#!/usr/bin/env python3
"""
tts_node.py - SOOMAC TTS ROS 2 노드 (Supertonic, CPU, ROS 2 Humble / Python 3.10)

LLM 쪽 퍼블리셔(/tts/text, std_msgs/String 평문)를 그대로 받는다.
기존 템플릿의 "TTS 코드 연결 위치"에 tts_core 파이프라인을 붙인 형태다.

구독
  say_topic   (std_msgs/String, 기본 /tts/text)
        "음료는 무엇으로 드릴까요?"                                   평문 (현재 퍼블리셔 형식)
      나중에 메인 노드가 발화 id·끼어들기 등을 넘기고 싶으면 JSON 도 받는다.
        {"text": "...", "topic": "ask_drink", "id": 7, "interrupt": false}
        {"reply": "..."} / {"utterance": "..."}                       text 대신 허용하는 키
        {"cmd": "stop"}                                               중단
  stop_topic  (std_msgs/Empty, 기본 /tts/stop)

발행
  status_topic (std_msgs/String JSON, 기본 /tts/status)
      accepted / started / progress / done / stopped / error  (tts_core.py 참고)
      ready     노드 준비 완료 (음색, 샘플레이트 등)
      heartbeat heartbeat_s 마다 {busy, alive}. 메인 노드의 TTS 생존 확인용
    모든 이벤트에
      t     time.monotonic()  같은 머신의 다른 프로세스와 비교 가능(CLOCK_MONOTONIC)
      stamp ROS 시각(초)
    started 에는 duration_s, expected_end_t / expected_end_stamp 가 실린다.
    메인 노드는 이 값으로 STT 게이트를 여는 시각(= 종료 + 잔향 여유)을 계획하면 된다.

실행 (패키지로 만들기 전, 스크립트로 바로)
  source /opt/ros/humble/setup.bash
  source ~/venvs/soomac-tts/bin/activate
  python3 tts_node.py --ros-args -p voice:=F1 -p total_steps:=8

테스트
  ros2 topic echo /tts/status
  ros2 topic pub --once /tts/text std_msgs/msg/String "{data: '음료는 무엇으로 드릴까요?'}"
  ros2 topic pub --once /tts/stop std_msgs/msg/Empty "{}"
"""
from __future__ import annotations

import itertools
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rclpy                                                     # noqa: E402
from rclpy.node import Node                                      # noqa: E402
from rclpy.qos import (                                          # noqa: E402
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from std_msgs.msg import Empty, String                           # noqa: E402

from tts_core import (                                           # noqa: E402
    PRECACHE_PHRASES,
    PhraseCache,
    TTSPipeline,
    build_engine,
    build_player,
)
from tts_text import load_lexicon, prepare                       # noqa: E402

TTS_TOPIC = "/tts/text"            # LLM 퍼블리셔와 맞춘 기본값
TTS_STOP_TOPIC = "/tts/stop"
TTS_STATUS_TOPIC = "/tts/status"

TEXT_KEYS = ("text", "reply", "utterance", "speech", "message")


def parse_say_payload(raw: str) -> dict | None:
    """
    std_msgs/String 의 data 를 해석한다. JSON 객체면 필드를 읽고, 아니면 평문으로 본다.
    읽을 텍스트가 없으면 None.
    """
    raw = (raw or "").strip()
    if not raw:
        return None

    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            if str(obj.get("cmd", "")).lower() == "stop":
                return {"cmd": "stop"}
            text = next((obj[k] for k in TEXT_KEYS if isinstance(obj.get(k), str) and obj[k].strip()), None)
            if text is None:
                return None
            return {
                "text": text.strip(),
                "topic": obj.get("topic") or obj.get("kind"),
                "id": obj.get("id"),
                "interrupt": bool(obj.get("interrupt", False)),
            }

    return {"text": raw, "topic": None, "id": None, "interrupt": False}


class TTSNode(Node):
    """
    node_name / defaults / precache_phrases 를 바꿔서 다른 용도로 재사용할 수 있다.
    (deliver_tts_node.py 가 이 방식으로 결제·전달 안내용 노드를 만든다)
    defaults 는 파라미터 기본값만 바꾼다. --ros-args -p 로 넘긴 값이 항상 우선이다.
    """

    def __init__(self, node_name: str = "tts_node", *, defaults: dict | None = None,
                 precache_phrases=PRECACHE_PHRASES):
        super().__init__(node_name)

        defaults = defaults or {}
        P = lambda name, value: self.declare_parameter(name, defaults.get(name, value))  # noqa: E731
        P("say_topic", TTS_TOPIC)
        P("stop_topic", TTS_STOP_TOPIC)
        P("status_topic", TTS_STATUS_TOPIC)
        P("say_reliability", "reliable")      # 퍼블리셔가 best_effort 면 best_effort 로
        P("engine", "supertonic")             # supertonic | dummy
        P("voice", "F1")
        P("total_steps", 8)
        P("speed", 1.05)
        P("seed", 1234)
        P("threads", 2)
        P("model_dir", "")                    # 비우면 ~/.cache/supertonic3
        P("offline", False)                   # True 면 모델 자동 다운로드 금지
        P("player", "pyaudio")                # pyaudio | null
        P("output_device", -1)                # -1 = 기본 장치
        P("volume", 1.0)
        P("lead_silence_ms", 80)
        P("sentence_gap_ms", 180)
        P("prefetch_all", True)
        P("cache_dir", "runtime_data/tts_cache")
        P("precache", True)
        P("lexicon_path", "")
        P("heartbeat_s", 1.0)
        P("print_rx", True)                   # 받은 토픽 메시지를 터미널에 출력

        g = lambda name: self.get_parameter(name).value   # noqa: E731

        status_qos = QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=50,
                                reliability=ReliabilityPolicy.RELIABLE,
                                durability=DurabilityPolicy.VOLATILE)
        self._status_pub = self.create_publisher(String, g("status_topic"), status_qos)

        # ---- /tts/text 구독: 모델 로딩보다 먼저 연다.
        # 콜백은 spin 이 시작돼야 실행되므로, 로딩·사전 합성 중에 들어온 메시지는
        # 구독자 큐(depth 20)에 쌓였다가 준비가 끝나는 즉시 순서대로 처리된다.
        reliability = (ReliabilityPolicy.BEST_EFFORT
                       if str(g("say_reliability")).lower() == "best_effort"
                       else ReliabilityPolicy.RELIABLE)
        self._say_reliable = reliability == ReliabilityPolicy.RELIABLE
        say_qos = QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=20,
                             reliability=reliability, durability=DurabilityPolicy.VOLATILE)
        self.subscription = self.create_subscription(
            String,
            g("say_topic"),          # 기본값 TTS_TOPIC = "/tts/text"
            self.tts_callback,
            say_qos,
        )
        self.create_subscription(Empty, g("stop_topic"), self._on_stop_topic, 10)
        self._say_topic = g("say_topic")
        self._stop_topic = g("stop_topic")
        self._print_rx = bool(g("print_rx"))
        self._rx_count = 0
        self.get_logger().info(f"{g('say_topic')} 구독 시작 (모델 로딩 중 들어온 발화는 준비 후 재생)")

        # 다른 PC 의 퍼블리셔와 붙지 않을 때 가장 먼저 볼 값들. 양쪽 PC·모든 터미널이 같아야 한다.
        print(
            "[ROS ENV] "
            f"ROS_DOMAIN_ID={os.environ.get('ROS_DOMAIN_ID', '(미설정=0)')}  "
            f"ROS_LOCALHOST_ONLY={os.environ.get('ROS_LOCALHOST_ONLY', '(미설정=0)')}  "
            f"RMW={os.environ.get('RMW_IMPLEMENTATION', '(기본)')}  "
            f"node={self.get_fully_qualified_name()}",
            flush=True,
        )

        # ---- 엔진 / 출력 / 캐시
        engine_kind = g("engine")
        engine_kw = {} if engine_kind == "dummy" else dict(
            voice=g("voice"), total_steps=int(g("total_steps")), speed=float(g("speed")),
            seed=int(g("seed")), threads=int(g("threads")) or None,
            model_dir=g("model_dir") or None, auto_download=not bool(g("offline")),
        )
        engine = build_engine(engine_kind, **engine_kw)

        t0 = time.perf_counter()
        engine.synthesize("안녕하세요.")      # 워밍업: 첫 손님이 ONNX 초기화 비용을 겪지 않게
        self.get_logger().info(f"워밍업 {(time.perf_counter() - t0) * 1000:.0f}ms")

        device = int(g("output_device"))
        player = build_player(g("player"), engine.sample_rate, None if device < 0 else device)
        cache = PhraseCache(engine.key, g("cache_dir"))

        lexicon = None
        self._lexicon = None
        if g("lexicon_path"):
            lexicon = load_lexicon(g("lexicon_path"))
            self._lexicon = lexicon
            self.get_logger().info(f"발음 교정 사전 {len(lexicon)}개: {g('lexicon_path')}")

        self.pipeline = TTSPipeline(
            engine, player, cache, on_event=self._publish_event, lexicon=lexicon,
            prefetch_all=bool(g("prefetch_all")), lead_silence_ms=int(g("lead_silence_ms")),
            sentence_gap_ms=int(g("sentence_gap_ms")), volume=float(g("volume")),
        )

        if g("precache"):
            self.pipeline.precache(precache_phrases)


        self._ids = itertools.count(1)
        self._ready_info = {
            "engine": engine_kind,
            "voice": getattr(engine, "voice", None),
            "sample_rate": engine.sample_rate,
            "output_rate": player.rate,
            "say_topic": g("say_topic"),
            "pid": os.getpid(),
        }
        # /tts/text 퍼블리셔 수가 바뀔 때마다 출력한다. 0 이면 LLM 쪽과 연결이 안 된 것.
        self._last_pub_count = None
        self._watch_publishers()
        self.create_timer(1.0, self._watch_publishers)

        hb = float(g("heartbeat_s"))
        if hb > 0:
            self.create_timer(hb, self._heartbeat)

        self._publish_event({"event": "ready", "t": time.monotonic(), **self._ready_info})
        self.get_logger().info(f"{node_name} READY - {g('say_topic')} 구독 중 (상태 {g('status_topic')})")

    # --------------------------------------------------------
    def _publish_event(self, ev: dict) -> None:
        # monotonic -> ROS 시각 변환. 이벤트마다 오프셋을 다시 재서 시계 보정을 반영한다.
        offset = self.get_clock().now().nanoseconds * 1e-9 - time.monotonic()
        ev = dict(ev)
        ev["stamp"] = round(ev["t"] + offset, 6)
        if "expected_end_t" in ev:
            ev["expected_end_stamp"] = round(ev["expected_end_t"] + offset, 6)
        msg = String()
        msg.data = json.dumps(ev, ensure_ascii=False)
        self._status_pub.publish(msg)

        if ev["event"] in ("started", "done", "stopped", "error"):
            detail = {k: v for k, v in ev.items() if k not in ("event", "t", "stamp", "expected_end_stamp")}
            self.get_logger().info(f"{ev['event']} {detail}")

    def _watch_publishers(self) -> None:
        """
        /tts/text 에 붙은 퍼블리셔를 1초마다 확인하고, 바뀔 때만 출력한다.
        같은 이름의 토픽 하나에 LLM 퍼블리셔(PUBLISHER)와 이 노드(SUBSCRIPTION)가 함께 붙는다.
        여기서는 PUBLISHER 쪽만 보므로, 이 노드 자신은 섞이지 않는다.
        """
        try:
            infos = self.get_publishers_info_by_topic(self._say_topic)
        except Exception:
            infos = None

        if infos is None:
            count = self.count_publishers(self._say_topic)
            names = []
        else:
            count = len(infos)
            names = []
            for info in infos:
                ns = info.node_namespace.rstrip("/")
                name = f"{ns}/{info.node_name}" if info.node_name else "(이름 없음)"
                reliability = getattr(info.qos_profile.reliability, "name", str(info.qos_profile.reliability))
                names.append((name, reliability))

        signature = (count, tuple(names))
        if signature == self._last_pub_count:
            return
        self._last_pub_count = signature

        if count == 0:
            print(f"[PUB] {self._say_topic}: 퍼블리셔 0개 "
                  "(보내는 노드가 안 떠 있거나 네트워크·ROS_DOMAIN_ID 가 다름)", flush=True)
            return

        print(f"[PUB] {self._say_topic}: 퍼블리셔 {count}개 연결", flush=True)
        for name, reliability in names:
            warn = ""
            if self._say_reliable and "BEST_EFFORT" in reliability.upper():
                warn = ("  ← QoS 불일치: 퍼블리셔가 BEST_EFFORT 라 메시지가 안 온다. "
                        "-p say_reliability:=best_effort 로 실행할 것")
            print(f"       - {name}  (Reliability: {reliability}){warn}", flush=True)

    def _heartbeat(self) -> None:
        self._publish_event({"event": "heartbeat", "t": time.monotonic(),
                             "busy": self.pipeline.busy})

    # --------------------------------------------------------
    # 수신 출력
    # --------------------------------------------------------
    def _rx_print(self, topic: str, lines: list[str]) -> None:
        if not self._print_rx:
            return
        now = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
        print(f"\n[RX {now}] {topic}  (#{self._rx_count})", flush=True)
        for line in lines:
            print(f"  {line}", flush=True)

    # --------------------------------------------------------
    def tts_callback(self, msg: String) -> None:
        self._rx_count += 1
        raw = msg.data
        payload = parse_say_payload(raw)
        lines = [f"raw   : {raw!r}"]

        if payload is None:
            lines.append("-> 무시 (읽을 텍스트 없음)")
            self._rx_print(self._say_topic, lines)
            return

        if payload.get("cmd") == "stop":
            dropped = self.pipeline.stop()
            lines.append(f"-> 중단 명령 (대기·재생 {dropped}건 폐기)")
            self._rx_print(self._say_topic, lines)
            return

        text = payload["text"]
        job_id = payload["id"] if payload["id"] is not None else f"auto-{next(self._ids)}"
        busy_before = self.pipeline.pending

        if raw.strip() != text:          # JSON 으로 들어온 경우만 추출 결과를 따로 보여준다
            lines.append(f"text  : {text!r}")
        if payload["topic"] or payload["interrupt"]:
            lines.append(f"meta  : topic={payload['topic']} interrupt={payload['interrupt']}")
        lines.append(f"읽기  : {prepare(text, self._lexicon)}")
        lines.append(f"-> 재생 대기열 등록 id={job_id} "
                     f"({'끼어들기' if payload['interrupt'] else f'앞에 {busy_before}건'})")
        self._rx_print(self._say_topic, lines)

        self.pipeline.submit(text, job_id=job_id, topic=payload["topic"],
                             interrupt=payload["interrupt"])

    def _on_stop_topic(self, _msg) -> None:
        self._rx_count += 1
        dropped = self.pipeline.stop()
        self._rx_print(self._stop_topic, [f"-> 중단 (대기·재생 {dropped}건 폐기)"])

    def close(self) -> None:
        self.pipeline.close()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = None
    try:
        node = TTSNode()
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