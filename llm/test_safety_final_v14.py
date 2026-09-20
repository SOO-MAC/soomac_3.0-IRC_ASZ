import copy

from order_runtime_final import (
    DriveThruRuntime,
    RecoverableOrderError,
    ModelOutputError,
    InternalInvariantError,
)

from drive_thru_app import (
    guard_customer_input,
)


# ============================================================
# 1. RecoverableOrderError
# ============================================================

runtime = DriveThruRuntime()
before = copy.deepcopy(
    runtime.manager.__dict__
)

def recoverable_failure(_):
    raise RecoverableOrderError(
        "FORCED_RECOVERABLE"
    )

runtime._process = recoverable_failure

result = runtime.process("테스트")

assert result["error"]["kind"] == "recoverable"
assert runtime.manager.__dict__ == before

print(
    "PASS 1A: RecoverableOrderError "
    "→ handled + rollback"
)


# ============================================================
# 2. ModelOutputError
# ============================================================

runtime = DriveThruRuntime()
before = copy.deepcopy(
    runtime.manager.__dict__
)

def model_failure(_):
    raise ModelOutputError(
        "FORCED_MODEL_ERROR"
    )

runtime._process = model_failure

result = runtime.process("테스트")

assert result["error"]["kind"] == "model_output"
assert runtime.manager.__dict__ == before

print(
    "PASS 2A: ModelOutputError "
    "→ handled + rollback"
)


# ============================================================
# 3. InternalInvariantError
#    절대로 주문 오류로 숨겨지면 안 됨
# ============================================================

runtime = DriveThruRuntime()
before = copy.deepcopy(
    runtime.manager.__dict__
)

def invariant_failure(_):
    raise InternalInvariantError(
        "FORCED_INVARIANT_ERROR"
    )

runtime._process = invariant_failure

try:
    runtime.process("테스트")

except InternalInvariantError:
    assert runtime.manager.__dict__ == before
    print(
        "PASS 2B: InternalInvariantError "
        "→ rollback + propagated"
    )

else:
    raise AssertionError(
        "InternalInvariantError가 숨겨졌습니다."
    )


# ============================================================
# 4. 예상 못 한 Python 오류
# ============================================================

runtime = DriveThruRuntime()
before = copy.deepcopy(
    runtime.manager.__dict__
)

def python_failure(_):
    raise ValueError(
        "FORCED_PROGRAMMING_ERROR"
    )

runtime._process = python_failure

try:
    runtime.process("테스트")

except ValueError:
    assert runtime.manager.__dict__ == before
    print(
        "PASS 1B: unexpected Python error "
        "→ rollback + propagated"
    )

else:
    raise AssertionError(
        "예상하지 못한 Python 오류가 숨겨졌습니다."
    )


# ============================================================
# 5. Input Guard
# ============================================================

blocked = [
    None,
    "",
    "   ",
    "!!!",
    "음",
    "음...",
    "아아아아",
    "ㅋㅋㅋㅋ",
    "햄버거",
    "햄버거요",
    "햄버거 주세요",
    "음료",
    "음료요",
    "사이드",
]

for text in blocked:

    result = guard_customer_input(
        text,
        pending=None,
        mobile_confirmation_pending=False,
    )

    assert result["allow"] is False, (
        text,
        result,
    )

print(
    "PASS 3A: malformed/incomplete "
    "inputs blocked"
)


allowed = [
    (
        "불고기버거 하나 주세요",
        None,
        False,
    ),
    (
        "단품",
        (1, "type"),
        False,
    ),
    (
        "콜라",
        (1, "drink"),
        False,
    ),
    (
        "라지",
        (1, "drink_size"),
        False,
    ),
    (
        "감자튀김",
        (1, "side"),
        False,
    ),
    (
        "아니요 64번이에요",
        None,
        True,
    ),
]

for text, pending, mobile in allowed:

    result = guard_customer_input(
        text,
        pending=pending,
        mobile_confirmation_pending=mobile,
    )

    assert result["allow"] is True, (
        text,
        result,
    )

print(
    "PASS 3B: valid short/contextual "
    "inputs preserved"
)

print()
print(
    "SAFETY FINAL REGRESSION: ALL PASS"
)
