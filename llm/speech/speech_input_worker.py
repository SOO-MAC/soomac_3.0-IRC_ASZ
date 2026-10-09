import itertools
import queue
import threading
import time

from dataclasses import dataclass
from typing import Optional

from stt_guard import (
    STTResult,
    guard_stt_result,
    safe_stt_call,
)


@dataclass(frozen=True)
class SpeechEvent:
    event_id: int
    generation: int
    kind: str

    text: Optional[str] = None
    reason: Optional[str] = None
    reply: Optional[str] = None
    detail: Optional[str] = None


class SpeechInputWorker:
    """
    STT 전용 producer thread.

    역할:
        microphone/STT
            ↓
        safe_stt_call()
            ↓
        STT Guard
            ↓
        event queue

    이 worker는 Runtime/Order State를 절대 건드리지 않는다.
    """

    def __init__(
        self,
        transcribe_func,
        generation_provider,
        *,
        max_queue_size=8,
        min_confidence=None,
    ):
        self._transcribe_func = transcribe_func
        self._generation_provider = (
            generation_provider
        )

        self._min_confidence = (
            min_confidence
        )

        self._events = queue.Queue(
            maxsize=max_queue_size
        )

        self._counter = itertools.count(1)

        self._enabled = threading.Event()
        self._stop_event = threading.Event()

        self._thread = threading.Thread(
            target=self._run,
            name="soomac-stt-producer",
            daemon=True,
        )

        self._thread.start()

    # ========================================================
    # CONTROL
    # ========================================================

    def enable(self):
        self._enabled.set()

    def disable(self):
        self._enabled.clear()

    @property
    def enabled(self):
        return self._enabled.is_set()

    # ========================================================
    # EVENT RECEIVE
    # ========================================================

    def get(
        self,
        *,
        timeout=None,
    ):
        return self._events.get(
            timeout=timeout
        )

    def get_nowait(self):
        return self._events.get_nowait()

    def task_done(self):
        self._events.task_done()

    def empty(self):
        return self._events.empty()

    # ========================================================
    # QUEUE MANAGEMENT
    # ========================================================

    def clear(self):
        """
        세션 변경 시 이전 고객의 대기 발화를 폐기한다.
        """
        while True:
            try:
                self._events.get_nowait()
            except queue.Empty:
                break
            else:
                self._events.task_done()

    def _put_event(self, event):
        """
        STT thread가 queue 때문에 무한 block되지 않도록 한다.

        queue가 가득 찬 경우 가장 오래된 이벤트 하나를 버리고
        최신 발화를 보존한다.
        """

        try:
            self._events.put_nowait(
                event
            )
            return

        except queue.Full:
            pass

        try:
            self._events.get_nowait()
        except queue.Empty:
            pass
        else:
            self._events.task_done()

        try:
            self._events.put_nowait(
                event
            )
        except queue.Full:
            # 극히 짧은 race가 발생한 경우
            # producer 자체는 죽지 않는다.
            pass

    # ========================================================
    # STOP
    # ========================================================

    def stop(
        self,
        *,
        timeout=2.0,
    ):
        self._stop_event.set()
        self._enabled.set()

        self._thread.join(
            timeout=timeout
        )

    # ========================================================
    # PRODUCER LOOP
    # ========================================================

    def _run(self):

        while not self._stop_event.is_set():

            # ORDERING 상태가 아니면 STT를 호출하지 않는다.
            if not self._enabled.wait(
                timeout=0.1
            ):
                continue

            if self._stop_event.is_set():
                break

            generation_before = (
                self._generation_provider()
            )

            # 실제 STT provider 호출.
            # provider exception은 safe_stt_call에서 STTResult로 변환.
            result = safe_stt_call(
                self._transcribe_func
            )

            if self._stop_event.is_set():
                break

            generation_after = (
                self._generation_provider()
            )

            # 듣고 있는 사이 차량 세션이 바뀌었다면
            # 이전 고객의 음성 결과이므로 폐기.
            if (
                generation_before
                != generation_after
            ):
                continue

            decision = guard_stt_result(
                result,
                min_confidence=(
                    self._min_confidence
                ),
            )

            # streaming partial은 final이 뒤에 오므로
            # queue에도 넣지 않는다.
            if (
                not decision.allow
                and decision.reason
                == "partial_result"
            ):
                continue

            event_id = next(
                self._counter
            )

            if decision.allow:

                event = SpeechEvent(
                    event_id=event_id,
                    generation=(
                        generation_after
                    ),
                    kind="utterance",
                    text=decision.text,
                )

            else:

                event = SpeechEvent(
                    event_id=event_id,
                    generation=(
                        generation_after
                    ),
                    kind="stt_reject",
                    reason=decision.reason,
                    reply=decision.reply,
                    detail=decision.detail,
                )

            self._put_event(
                event
            )

            # provider가 즉시 실패를 반복 반환하는 경우
            # CPU/API 폭주 방지.
            if (
                isinstance(result, STTResult)
                and not result.ok
            ):
                time.sleep(0.1)
