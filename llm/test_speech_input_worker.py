import time

from speech_input_worker import (
    SpeechInputWorker,
)


generation = 0
samples = [
    "불고기버거 하나 주세요",
    "콜라로 주세요",
    "라지로 주세요",
]


def generation_provider():
    return generation


def fake_stt():
    if samples:
        time.sleep(0.03)
        return samples.pop(0)

    time.sleep(0.03)
    return None


worker = SpeechInputWorker(
    fake_stt,
    generation_provider,
    max_queue_size=8,
)

# ============================================================
# 1. disabled 상태에서는 STT 호출/이벤트 없음
# ============================================================

time.sleep(0.1)

assert worker.empty()

print(
    "PASS 5F: disabled producer idle"
)


# ============================================================
# 2. enabled → 독립적으로 발화 생산
# ============================================================

worker.enable()

event1 = worker.get(
    timeout=1
)

event2 = worker.get(
    timeout=1
)

event3 = worker.get(
    timeout=1
)

assert event1.text == "불고기버거 하나 주세요"
assert event2.text == "콜라로 주세요"
assert event3.text == "라지로 주세요"

print(
    "PASS 5G: STT producer "
    "captures while consumer is independent"
)


# ============================================================
# 3. generation 변경 중 결과 → stale speech 폐기
# ============================================================

worker.disable()
worker.clear()

generation += 1

slow_started = False


def slow_stt():
    global slow_started

    slow_started = True
    time.sleep(0.15)

    return "이전 고객 주문"


worker.stop()

worker = SpeechInputWorker(
    slow_stt,
    generation_provider,
    max_queue_size=8,
)

worker.enable()

while not slow_started:
    time.sleep(0.01)

# STT가 듣는 중 세션 변경
generation += 1

time.sleep(0.25)

assert worker.empty()

print(
    "PASS 5H: stale STT result discarded"
)


# ============================================================
# 4. queue overflow에서도 producer 생존
# ============================================================

worker.stop()

counter = 0


def rapid_stt():
    global counter

    counter += 1

    time.sleep(0.005)

    return f"발화 {counter}"


worker = SpeechInputWorker(
    rapid_stt,
    generation_provider,
    max_queue_size=2,
)

worker.enable()

time.sleep(0.1)

assert (
    worker._thread.is_alive()
)

print(
    "PASS 5I: bounded queue overflow "
    "does not kill producer"
)


# ============================================================
# 5. shutdown
# ============================================================

worker.stop()

print(
    "PASS 5J: speech producer shutdown"
)

print()
print(
    "SPEECH INPUT WORKER REGRESSION: ALL PASS"
)
