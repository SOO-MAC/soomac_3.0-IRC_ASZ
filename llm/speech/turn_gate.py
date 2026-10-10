#!/usr/bin/env python3
"""
turn_gate.py - 한 턴이 끝날 때까지 마이크를 닫아두는 상태 기계

왜 필요한가
    transcribe_once() 는 수음이 끝나면 게이트를 닫고 추론한다. 거기까지는 이미 맞다.
    문제는 발행 직후 루프가 곧바로 transcribe_once() 를 다시 불러 게이트를 다시 연다는 것.
    그 사이 LLM 이 생각하고 TTS 가 말하는 동안 마이크가 열려 있어 로봇 자기 목소리를 받아적는다.

흐름
    STT 발행 -> [LLM 추론] -> /tts/text -> [TTS 합성·재생] -> /tts/status done -> 잔향 -> 다시 듣기
                 WAIT_TTS         SPEAKING                        GUARD          LISTEN

LLM 을 끼지 않는 이유
    TTS 가 /tts/status 로 accepted/started/done/stopped/error + 1초 heartbeat{busy} 를
    이미 다 발행한다. LLM 쪽은 STT 를 UDP 로 받고 차량 단위로만 세션을 여닫으므로
    턴 단위 신호를 받을 통로가 없다. 당사자가 적을수록 실패 모드가 적다.

ROS 를 모르게 짠 이유
    상태 기계는 테스트가 가능해야 한다. 이벤트 dict 와 시계만 주면 돌아간다.

핵심 설계 두 가지

    1) accepted/done 카운터가 1차 신호, heartbeat{busy} 가 교정 신호다.
       메시지 한 개를 놓쳐도 1~2초 안에 heartbeat 가 바로잡는다.
       done 을 놓쳐서 영구히 귀머거리가 되는 사고를 이게 막는다.

    2) 타임스탬프를 비교하지 않는다. 수신 순서만 쓴다.
       t 는 time.monotonic() 이라 다른 머신과 비교할 수 없고, stamp 는 시계 동기에 의존한다.
       ROS 는 같은 퍼블리셔의 메시지를 발행 순서대로 주므로 수신 순서로 충분하다.
       TTS 쪽이 _active 를 먼저 증감한 뒤 이벤트를 발행하기 때문에,
       done 뒤에 온 busy=True 는 "정말로 다음 발화가 대기 중" 이라는 뜻이 된다.
"""
from __future__ import annotations
 
import threading
import time
from dataclasses import dataclass
 
LISTEN = "listen"          # 게이트 열어도 됨
WAIT_TTS = "wait_tts"      # 발행했고 TTS 가 시작되기를 기다린다 (= LLM 추론 중)
SPEAKING = "speaking"      # TTS 가 말하는 중
GUARD = "guard"            # 말은 끝났고 잔향이 빠지기를 기다린다
 
# TTS 가 한 발화를 끝낼 때 보내는 이벤트들
_TTS_END = ("done", "stopped", "error")
 
 
@dataclass
class TurnGateConfig:
    # TTS 종료 후 스피커 잔향이 빠질 시간. Clova 시절 선배 코드의 0.3초를 참고했고
    # 드라이브 스루 스피커 환경에서 재측정해야 한다.
    guard_ms: int = 400
 
    # 발행 후 이 시간 안에 TTS 가 시작되지 않으면 포기하고 다시 듣는다.
    # LLM 이 죽었거나, 응답할 말이 없다고 판단했거나, 네트워크가 끊긴 경우.
    # 이게 없으면 손님 앞에서 로봇이 영구히 귀머거리가 된다.
    llm_timeout_s: float = 8.0
 
    # 말이 끝났다는 신호를 전혀 못 받았을 때의 절대 상한.
    # 평소에는 heartbeat 가 1~2초 안에 교정하므로 여기까지 오지 않는다.
    tts_timeout_s: float = 30.0
 
 
class TurnGate:
    def __init__(self, cfg: TurnGateConfig | None = None, *, clock=time.monotonic):
        self.cfg = cfg or TurnGateConfig()
        self._now = clock
        self._lk = threading.RLock()
 
        self.state = LISTEN
        self.active = 0              # TTS 가 처리 중인 발화 수 (accepted - done)
        self.tts_present = True      # /tts/status 퍼블리셔가 있는가
 
        self._since = self._now()    # 현재 상태로 들어온 시각
        self._log: list[str] = []
 
        # 통계. 워치독이 몇 번 발동했는지 봐야 설정이 맞는지 안다.
        self.stats = {
            "turns": 0,
            "llm_timeout": 0,        # LLM 이 응답 안 함
            "tts_timeout": 0,        # TTS 종료 신호를 못 받음
            "hb_recovered": 0,       # heartbeat 가 놓친 done 을 교정
            "hb_reopened": 0,        # heartbeat 가 놓친 accepted 를 교정
            "tts_absent": 0,         # TTS 노드가 없어서 기다리지 않음
        }
 
    # -------------------------------------------------------------- 로그
    def drain_log(self) -> list[str]:
        """쌓인 로그를 꺼내 비운다. 노드가 자기 로거로 찍는다."""
        with self._lk:
            out, self._log = self._log, []
            return out
 
    def _to(self, state: str, why: str = "") -> None:
        if state != self.state:
            self._log.append(f"{self.state} -> {state}" + (f"  ({why})" if why else ""))
        self.state = state
        self._since = self._now()
 
    # -------------------------------------------------------------- STT 쪽에서 호출
    def on_published(self) -> None:
        """/stt/text 를 발행한 직후. 응답이 끝날 때까지 마이크를 닫아둔다."""
        with self._lk:
            self.stats["turns"] += 1
            if not self.tts_present:
                # TTS 가 없으면 아무도 말하지 않는다. 기다릴 이유가 없다.
                self.stats["tts_absent"] += 1
                self._to(LISTEN, "TTS 노드 없음")
                return
            self._to(WAIT_TTS, "응답 대기")
 
    def on_not_published(self) -> None:
        """
        빈 결과·가드 거부·수음 폐기. "우리 때문에 로봇이 말할 일은 없다" 는 뜻이다.
 
        SPEAKING/GUARD 일 때 손대지 않는 게 중요하다. 로봇이 지금 말하는 중일 수 있다
        (인사말, 직전 턴의 두 번째 문장, 다른 노드가 보낸 /tts/text).
        여기서 무조건 LISTEN 으로 되돌리면 그 소리를 그대로 받아적는다.
        """
        with self._lk:
            if self.state in (SPEAKING, GUARD):
                return
            self._to(LISTEN)
 
    def set_tts_present(self, present: bool) -> None:
        with self._lk:
            present = bool(present)
            if present == self.tts_present:
                return
            self.tts_present = present
            if not present:
                # TTS 가 죽었다. 기다려봐야 신호가 안 온다.
                self.active = 0
                self._to(LISTEN, "TTS 노드 사라짐")
            else:
                self._log.append("TTS 노드 연결")
 
    # -------------------------------------------------------------- ROS 콜백에서 호출
    def on_tts_status(self, ev: dict) -> None:
        with self._lk:
            self._on_tts_status(ev)
 
    def _on_tts_status(self, ev: dict) -> None:
        event = str(ev.get("event") or "")
 
        if event == "accepted":
            self.active += 1
            self._to(SPEAKING, f"TTS 수신 (대기 {self.active})")
 
        elif event == "started":
            # accepted 를 놓쳤을 수도 있으니 여기서도 SPEAKING 을 보장한다.
            if self.active == 0:
                self.active = 1
            if self.state != SPEAKING:
                self._to(SPEAKING, "TTS 재생 시작")
 
        elif event in _TTS_END:
            self.active = max(0, self.active - 1)
            if self.active == 0:
                self._to(GUARD, f"TTS {event}")
            # 아직 대기 중인 발화가 있으면 SPEAKING 유지
 
        elif event == "heartbeat":
            self._on_heartbeat(bool(ev.get("busy")))
 
        elif event == "ready":
            # TTS 노드가 (재)시작됐다. 재생 중인 것은 없다.
            self.active = 0
            if self.state in (WAIT_TTS, SPEAKING):
                self._to(GUARD, "TTS 노드 재시작")
 
    def _on_heartbeat(self, busy: bool) -> None:
        # 1초마다 오는 교정 신호.
        # busy=False 인데 우리가 SPEAKING -> done 을 놓쳤다. 열어준다. busy=True  인데 우리가 LISTEN/GUARD -> accepted 를 놓쳤다. 닫는다.
 
        if busy:
            self.active = max(self.active, 1)
            if self.state in (LISTEN, GUARD, WAIT_TTS):
                self.stats["hb_reopened"] += 1
                self._to(SPEAKING, "heartbeat busy (accepted 누락)")
        else:
            self.active = 0
            if self.state == SPEAKING:
                self.stats["hb_recovered"] += 1
                self._to(GUARD, "heartbeat idle (done 누락)")
            elif self.state == WAIT_TTS:
                # 여기서는 아무것도 하지 않는다.
                pass
 
    # -------------------------------------------------------------- 메인 루프에서 호출
    def update(self) -> str | None:
        """
        시간 기반 전이를 진행하고, 아직 기다려야 하면 그 이유를 돌려준다.
        None 이면 게이트를 열어도 된다.
        """
        with self._lk:
            return self._update()
 
    def _update(self) -> str | None:
        if not self.tts_present and self.state != LISTEN:
            self.active = 0
            self._to(LISTEN, "TTS 노드 없음")
            return None
 
        elapsed = self._now() - self._since
 
        if self.state == LISTEN:
            return None
 
        if self.state == WAIT_TTS:
            if elapsed < self.cfg.llm_timeout_s:
                return "llm"
            self.stats["llm_timeout"] += 1
            self._to(LISTEN, f"★LLM 응답 없음 {elapsed:.1f}초 - 워치독으로 다시 듣는다")
            return None
 
        if self.state == SPEAKING:
            if elapsed < self.cfg.tts_timeout_s:
                return "tts"
            self.stats["tts_timeout"] += 1
            self.active = 0
            self._to(LISTEN, f"★TTS 종료 신호 없음 {elapsed:.1f}초 - 워치독으로 다시 듣는다")
            return None
 
        if self.state == GUARD:
            if elapsed * 1000 < self.cfg.guard_ms:
                return "guard"
            self._to(LISTEN)
            return None
 
        return None
 
    def summary(self) -> str:
        with self._lk:
            s = dict(self.stats)
        return (f"턴 {s['turns']} | LLM 타임아웃 {s['llm_timeout']} | "
                f"TTS 타임아웃 {s['tts_timeout']} | "
                f"heartbeat 교정 {s['hb_recovered'] + s['hb_reopened']} | "
                f"TTS 부재 {s['tts_absent']}")
 