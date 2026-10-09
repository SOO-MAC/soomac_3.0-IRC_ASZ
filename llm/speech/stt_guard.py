from dataclasses import dataclass
from typing import Callable, Optional, Any
import math


# ============================================================
# STT RESULT
# ============================================================

@dataclass(frozen=True)
class STTResult:
    """
    모든 STT provider의 출력을 앱 내부에서 동일한 형태로 표현한다.

    confidence:
        사용하는 STT가 confidence를 제공하고 threshold를 쓸 경우
        adapter 단계에서 0.0 ~ 1.0 범위로 정규화한다.
    """

    transcript: Optional[str] = None

    # STT 호출 자체가 성공했는지
    ok: bool = True

    # streaming STT인 경우 partial/final 구분
    is_final: bool = True

    # provider가 지원할 경우에만 사용
    confidence: Optional[float] = None

    # provider / network 오류 정보
    error_code: Optional[str] = None
    error_detail: Optional[str] = None


# ============================================================
# STT GUARD DECISION
# ============================================================

@dataclass(frozen=True)
class STTGuardDecision:
    allow: bool
    text: Optional[str]
    reason: Optional[str]
    reply: Optional[str]
    detail: Optional[str] = None


def _reject(
    reason,
    reply,
    detail=None,
):
    return STTGuardDecision(
        allow=False,
        text=None,
        reason=reason,
        reply=reply,
        detail=detail,
    )


# ============================================================
# SAFE PROVIDER CALL
# ============================================================

def safe_stt_call(
    transcribe_func: Callable[..., Any],
    *args,
    **kwargs,
) -> STTResult:
    """
    STT provider의 예외가 앱까지 터져 나오지 않도록 하는
    provider boundary.

    실제 Clova/Whisper/etc adapter는 최종적으로
    STTResult를 반환하도록 맞춘다.
    """

    try:
        result = transcribe_func(
            *args,
            **kwargs,
        )

    except TimeoutError as e:
        return STTResult(
            ok=False,
            error_code="TIMEOUT",
            error_detail=str(e),
        )

    except Exception as e:
        return STTResult(
            ok=False,
            error_code=type(e).__name__,
            error_detail=str(e),
        )

    # 간단한 STT 함수가 문자열만 반환하는 경우도 지원
    if isinstance(result, str):
        return STTResult(
            transcript=result,
            ok=True,
            is_final=True,
        )

    if result is None:
        return STTResult(
            transcript=None,
            ok=True,
            is_final=True,
        )

    if isinstance(result, STTResult):
        return result

    # provider adapter가 예상하지 못한 객체를 반환한 경우
    return STTResult(
        ok=False,
        error_code="INVALID_PROVIDER_RESULT",
        error_detail=(
            f"unexpected STT result type: "
            f"{type(result).__name__}"
        ),
    )


# ============================================================
# STT GUARD
# ============================================================

def guard_stt_result(
    result,
    *,
    min_confidence=None,
):
    """
    STT 결과가 기존 Input Guard / LLM으로 넘어가도 되는지 판단한다.

    이 함수는 주문 의미를 해석하지 않는다.
    음성 인식 결과 자체의 유효성만 검사한다.
    """

    # --------------------------------------------------------
    # 1. 결과 자체 없음
    # --------------------------------------------------------

    if result is None:
        return _reject(
            "no_result",
            "잘 듣지 못했습니다. 다시 말씀해주세요.",
        )

    # 테스트 및 단순 provider 호환
    if isinstance(result, str):
        result = STTResult(
            transcript=result
        )

    # --------------------------------------------------------
    # 2. 잘못된 객체
    # --------------------------------------------------------

    if not isinstance(
        result,
        STTResult,
    ):
        return _reject(
            "invalid_result_type",
            "음성 인식 중 문제가 발생했습니다. 다시 말씀해주세요.",
            detail=(
                f"unexpected result type: "
                f"{type(result).__name__}"
            ),
        )

    # --------------------------------------------------------
    # 3. STT provider 오류
    # --------------------------------------------------------

    if not result.ok:

        code = (
            result.error_code
            or "UNKNOWN_STT_ERROR"
        )

        if "TIMEOUT" in code.upper():
            return _reject(
                "stt_timeout",
                "음성 인식 시간이 초과되었습니다. 다시 말씀해주세요.",
                detail=result.error_detail,
            )

        return _reject(
            "stt_error",
            "음성을 인식하지 못했습니다. 다시 말씀해주세요.",
            detail=(
                f"{code}: "
                f"{result.error_detail or ''}"
            ).strip(),
        )

    # error_code가 있는데 ok=True인 이상한 provider 결과도 차단
    if result.error_code is not None:
        return _reject(
            "inconsistent_stt_result",
            "음성 인식 중 문제가 발생했습니다. 다시 말씀해주세요.",
            detail=result.error_code,
        )

    # --------------------------------------------------------
    # 4. Streaming partial
    # --------------------------------------------------------
    #
    # partial:
    #   "불고기"
    #   "불고기 버거"
    #   "불고기 버거 하나"
    #
    # 이런 값들을 주문으로 보내면 안 된다.
    # final transcript만 사용한다.
    #
    # partial은 뒤에 final이 들어올 예정이므로 TTS 재질문도 하지 않는다.

    if not result.is_final:
        return STTGuardDecision(
            allow=False,
            text=None,
            reason="partial_result",
            reply=None,
            detail=None,
        )

    # --------------------------------------------------------
    # 5. transcript 존재 여부
    # --------------------------------------------------------

    transcript = result.transcript

    if transcript is None:
        return _reject(
            "empty_transcript",
            "잘 듣지 못했습니다. 다시 말씀해주세요.",
        )

    if not isinstance(
        transcript,
        str,
    ):
        return _reject(
            "invalid_transcript_type",
            "음성을 정확히 인식하지 못했습니다. 다시 말씀해주세요.",
            detail=(
                f"transcript type: "
                f"{type(transcript).__name__}"
            ),
        )

    text = transcript.strip()

    if not text:
        return _reject(
            "empty_transcript",
            "잘 듣지 못했습니다. 다시 말씀해주세요.",
        )

    # --------------------------------------------------------
    # 6. confidence
    # --------------------------------------------------------

    confidence = result.confidence

    if confidence is not None:

        if (
            isinstance(confidence, bool)
            or not isinstance(
                confidence,
                (int, float),
            )
        ):
            return _reject(
                "invalid_confidence",
                "음성을 정확히 인식하지 못했습니다. 다시 말씀해주세요.",
                detail=repr(confidence),
            )

        confidence = float(confidence)

        if not math.isfinite(
            confidence
        ):
            return _reject(
                "invalid_confidence",
                "음성을 정확히 인식하지 못했습니다. 다시 말씀해주세요.",
                detail=repr(confidence),
            )

        # adapter contract:
        # confidence를 사용할 경우 0~1로 정규화
        if (
            confidence < 0.0
            or confidence > 1.0
        ):
            return _reject(
                "invalid_confidence_range",
                "음성을 정확히 인식하지 못했습니다. 다시 말씀해주세요.",
                detail=str(confidence),
            )

        if (
            min_confidence is not None
            and confidence
            < min_confidence
        ):
            return _reject(
                "low_confidence",
                "음성을 정확히 듣지 못했습니다. 다시 말씀해주세요.",
                detail=(
                    f"confidence="
                    f"{confidence:.3f}, "
                    f"threshold="
                    f"{min_confidence:.3f}"
                ),
            )

    # --------------------------------------------------------
    # 7. 정상
    # --------------------------------------------------------

    return STTGuardDecision(
        allow=True,
        text=text,
        reason=None,
        reply=None,
        detail=None,
    )
