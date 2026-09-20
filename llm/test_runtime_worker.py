import copy
import threading
import time

from runtime_worker import (
    RuntimeWorker,
    StaleRuntimeRequest,
)


class FakeManager:

    def __init__(self):
        self.state = {
            "count": 0
        }
        self.pending = None

    def reset(self):
        self.state = {
            "count": 0
        }
        self.pending = None


class FakeRuntime:

    active = 0
    max_active = 0
    lock = threading.Lock()

    def __init__(self):
        self.manager = FakeManager()

    def process(self, text):

        with self.lock:
            FakeRuntime.active += 1

            FakeRuntime.max_active = max(
                FakeRuntime.max_active,
                FakeRuntime.active,
            )

        try:
            time.sleep(0.05)

            self.manager.state["count"] += 1

            return {
                "state": copy.deepcopy(
                    self.manager.state
                ),
                "pending": None,
                "text": text,
            }

        finally:
            with self.lock:
                FakeRuntime.active -= 1


# ============================================================
# 1. 여러 요청 → 실제 실행은 반드시 1개씩
# ============================================================

worker = RuntimeWorker(
    FakeRuntime,
    max_queue_size=16,
)

futures = []

for i in range(6):

    request_id, generation, future = (
        worker.submit(
            f"request-{i}"
        )
    )

    futures.append(
        future
    )

results = [
    future.result(
        timeout=3
    )
    for future in futures
]

assert FakeRuntime.max_active == 1

assert [
    x["state"]["count"]
    for x in results
] == [
    1, 2, 3, 4, 5, 6
]

print(
    "PASS 5A: runtime.process() "
    "single-worker serialization"
)


# ============================================================
# 2. 추론 중 세션 변경 → stale result 폐기
# ============================================================

_, _, future = worker.submit(
    "old-customer-request"
)

time.sleep(
    0.01
)

reset_future = (
    worker.invalidate_and_reset(
        wait=False
    )
)

try:
    future.result(
        timeout=3
    )

except StaleRuntimeRequest:

    print(
        "PASS 5B: stale inference "
        "result discarded"
    )

else:
    raise AssertionError(
        "stale result가 폐기되지 않았습니다."
    )

reset_future.result(
    timeout=3
)

snapshot = worker.snapshot()

assert (
    snapshot["state"]["count"]
    == 0
)

print(
    "PASS 5C: reset serialized "
    "through worker"
)


# ============================================================
# 3. reset 후 새로운 세션 정상 처리
# ============================================================

result = worker.process(
    "new-customer-request",
    timeout=3,
)

assert (
    result["state"]["count"]
    == 1
)

print(
    "PASS 5D: new generation "
    "process normal"
)


# ============================================================
# 4. clean shutdown
# ============================================================

worker.stop()

print(
    "PASS 5E: graceful worker shutdown"
)

print()
print(
    "RUNTIME WORKER REGRESSION: ALL PASS"
)
