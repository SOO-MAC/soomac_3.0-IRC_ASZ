from stt_guard import (
    STTResult,
    guard_stt_result,
    safe_stt_call,
)


# ============================================================
# helper
# ============================================================

def blocked(
    result,
    reason,
    **kwargs,
):
    decision = guard_stt_result(
        result,
        **kwargs,
    )

    assert decision.allow is False
    assert decision.reason == reason

    print(
        f"PASS BLOCK: {reason}"
    )


# ============================================================
# 1. 결과 없음
# ============================================================

blocked(
    None,
    "no_result",
)


# ============================================================
# 2. timeout
# ============================================================

blocked(
    STTResult(
        ok=False,
        error_code="TIMEOUT",
        error_detail="request timed out",
    ),
    "stt_timeout",
)


# ============================================================
# 3. API error
# ============================================================

blocked(
    STTResult(
        ok=False,
        error_code="NETWORK_ERROR",
        error_detail="connection failed",
    ),
    "stt_error",
)


# ============================================================
# 4. None transcript
# ============================================================

blocked(
    STTResult(
        transcript=None
    ),
    "empty_transcript",
)


# ============================================================
# 5. 빈 transcript
# ============================================================

blocked(
    STTResult(
        transcript="    "
    ),
    "empty_transcript",
)


# ============================================================
# 6. partial result
# ============================================================

decision = guard_stt_result(
    STTResult(
        transcript="불고기 버거",
        is_final=False,
    )
)

assert decision.allow is False
assert decision.reason == "partial_result"
assert decision.reply is None

print(
    "PASS BLOCK: partial_result "
    "(no TTS reply)"
)


# ============================================================
# 7. low confidence
# ============================================================

blocked(
    STTResult(
        transcript="불고기버거 하나 주세요",
        confidence=0.20,
    ),
    "low_confidence",
    min_confidence=0.60,
)


# ============================================================
# 8. invalid confidence
# ============================================================

blocked(
    STTResult(
        transcript="불고기버거",
        confidence=1.5,
    ),
    "invalid_confidence_range",
)


# ============================================================
# 9. confidence 없는 provider
# ============================================================

decision = guard_stt_result(
    STTResult(
        transcript="불고기버거 하나 주세요",
        confidence=None,
    ),
    min_confidence=0.60,
)

assert decision.allow is True
assert decision.text == "불고기버거 하나 주세요"

print(
    "PASS ALLOW: provider without confidence"
)


# ============================================================
# 10. 정상 transcript
# ============================================================

decision = guard_stt_result(
    STTResult(
        transcript="  불고기버거 하나 주세요  ",
        confidence=0.95,
    ),
    min_confidence=0.60,
)

assert decision.allow is True
assert decision.text == "불고기버거 하나 주세요"

print(
    "PASS ALLOW: valid final transcript"
)


# ============================================================
# 11. safe_stt_call - 정상 문자열
# ============================================================

def fake_ok():
    return "콜라 라지로 주세요"

result = safe_stt_call(
    fake_ok
)

assert isinstance(
    result,
    STTResult,
)

assert (
    result.transcript
    == "콜라 라지로 주세요"
)

print(
    "PASS SAFE CALL: normal"
)


# ============================================================
# 12. safe_stt_call - timeout
# ============================================================

def fake_timeout():
    raise TimeoutError(
        "forced timeout"
    )

result = safe_stt_call(
    fake_timeout
)

assert result.ok is False
assert result.error_code == "TIMEOUT"

print(
    "PASS SAFE CALL: timeout converted"
)


# ============================================================
# 13. safe_stt_call - unexpected provider exception
# ============================================================

def fake_provider_error():
    raise RuntimeError(
        "forced provider failure"
    )

result = safe_stt_call(
    fake_provider_error
)

assert result.ok is False
assert (
    result.error_code
    == "RuntimeError"
)

print(
    "PASS SAFE CALL: provider exception converted"
)


print()
print(
    "STT GUARD FINAL REGRESSION: ALL PASS"
)
