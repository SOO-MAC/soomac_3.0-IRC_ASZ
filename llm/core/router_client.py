#!/usr/bin/env python3

import json
import os
import urllib.error
import urllib.request
from typing import Any

from router_prompt import (
    build_router_system_prompt,
    build_router_user_payload,
)
from router_schema import RouterOutput
from router_normalizer import (
    normalize_router_output,
    safe_clarify_output,
)
from router_safety_guard import apply_router_safety_guard
from router_deterministic_resolver import resolve_router_deterministic
from router_recommendation_canonicalizer import (
    canonicalize_recommendation_output,
)


class RouterClientError(RuntimeError):
    pass


def get_router_url() -> str:
    """
    Example:
        http://100.102.73.2:8000/v1
    """

    value = os.getenv(
        "SOOMAC_ROUTER_URL",
        os.getenv(
            "SOOMAC_LLM_URL",
            "http://100.102.73.2:8000/v1",
        ),
    )

    return value.rstrip("/")


def get_router_model() -> str:
    """
    Model name must match the name exposed by vLLM.
    """

    value = os.getenv(
        "SOOMAC_ROUTER_MODEL",
        "",
    ).strip()

    if not value:
        raise RouterClientError(
            "SOOMAC_ROUTER_MODEL is not set"
        )

    return value


# ADAPTIVE THINKING ROUTER V1
#
# Safety principle:
# - default: thinking ON
# - thinking OFF only for extremely simple, stateless,
#   single-product explicit orders
#
# Override:
#   SOOMAC_ROUTER_THINKING=on
#   SOOMAC_ROUTER_THINKING=off
#   SOOMAC_ROUTER_THINKING=adaptive
def _should_enable_thinking(
    utterance: str,
    *,
    history: list[dict[str, Any]] | None = None,
    pending: dict[str, Any] | None = None,
    order_state: dict[str, Any] | None = None,
) -> bool:

    mode = os.getenv(
        "SOOMAC_ROUTER_THINKING",
        "adaptive",
    ).strip().lower()

    if mode in {
        "on",
        "true",
        "1",
        "yes",
    }:
        return True

    if mode in {
        "off",
        "false",
        "0",
        "no",
    }:
        return False

    # Unknown mode -> safest behavior.
    if mode != "adaptive":
        return True

    text = (utterance or "").strip()

    if not text:
        return True

    # --------------------------------------------------------
    # Anything contextual stays on.
    # --------------------------------------------------------

    if pending:
        return True

    if history:
        return True

    if isinstance(order_state, dict):
        items = order_state.get("items") or []

        if items:
            return True

    # --------------------------------------------------------
    # Risky language: always reason.
    # --------------------------------------------------------

    risky_markers = (
        # context / reference
        "그거",
        "그걸",
        "그 메뉴",
        "그메뉴",
        "아까",
        "이전",
        "방금",
        "첫 번째",
        "첫번째",
        "두 번째",
        "두번째",
        "세 번째",
        "세번째",

        # modify / remove / correction
        "바꿔",
        "바꾸",
        "변경",
        "수정",
        "교체",
        "취소",
        "제거",
        "빼",
        "빼고",
        "제외",
        "없이",
        "말고",
        "대신",

        # conditions / possibility
        "가능",
        "있으면",
        "된다면",
        "되면",
        "재고",

        # recommendation / ambiguity
        "추천",
        "뭐가",
        "어떤",
        "괜찮",
        "맞아",
        "맞죠",

        # multi-act / continuation
        "주시고",
        "해주시고",
        "그리고",
        "또 ",
        "뿐만",
        "동시에",

        # set orders are handled by dedicated fastpath or
        # require richer slot interpretation.
        "세트",
        "셋트",
    )

    if any(
        marker in text
        for marker in risky_markers
    ):
        return True

    # Questions stay ON in conservative V1.
    if "?" in text:
        return True

    question_markers = (
        "얼마",
        "뭐예요",
        "뭔가요",
        "인가요",
        "있어요",
        "돼요",
        "되나요",
        "가능해",
    )

    if any(
        marker in text
        for marker in question_markers
    ):
        return True

    # --------------------------------------------------------
    # OFF is allowed only when a known single menu product
    # is explicitly being ordered.
    # --------------------------------------------------------

    product_markers = (
        "불고기버거",
        "치킨버거",
        "치즈버거",
        "새우버거",
        "제로콜라",
        "콜라",
        "사이다",
        "스프라이트",
        "환타",
        "아이스커피",
        "감자튀김",
        "감튀",
        "치즈스틱",
    )

    normalized = text.replace("제로콜라", "")

    product_hits = 0

    if "제로콜라" in text:
        product_hits += 1

    for product in product_markers:
        if product == "제로콜라":
            continue

        if product == "콜라":
            if "콜라" in normalized:
                product_hits += 1
            continue

        if product in text:
            product_hits += 1

    if product_hits != 1:
        return True

    explicit_order_markers = (
        "주세요",
        "줘요",
        "줘",
        "주문",
        "시킬게",
        "시켜주세요",
        "할게요",
    )

    if not any(
        marker in text
        for marker in explicit_order_markers
    ):
        return True

    # Very long utterances are not treated as simple.
    if len(text) > 45:
        return True

    # Only this narrow case gets fast no-thinking inference.
    return False


def build_request_body(
    utterance: str,
    *,
    history: list[dict[str, Any]] | None = None,
    pending: dict[str, Any] | None = None,
    order_state: dict[str, Any] | None = None,
    model: str | None = None,
) -> dict[str, Any]:

    if model is None:
        model = get_router_model()

    schema = RouterOutput.model_json_schema()

    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content":
                    build_router_system_prompt(),
            },
            {
                "role": "user",
                "content":
                    build_router_user_payload(
                        utterance,
                        history=history,
                        pending=pending,
                        order_state=order_state,
                    ),
            },
        ],

        # Router는 분류/구조화 작업이므로
        # 최대한 결정적으로 동작시킨다.
        "temperature": 0.0,
        "max_tokens": 768,

        # vLLM structured outputs
        # 서버 backend는 XGrammar 사용.
        "structured_outputs": {
            "json": schema,
        },

        # Router는 분류기이므로 reasoning token을 생성하지 않는다.
        # urllib로 raw JSON을 직접 전송하므로
        # chat_template_kwargs는 request body 최상위에 둔다.
        "chat_template_kwargs": {
            "enable_thinking":
                _should_enable_thinking(
                    utterance,
                    history=history,
                    pending=pending,
                    order_state=order_state,
                ),
        },
    }


def _post_json(
    url: str,
    body: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:

    raw = json.dumps(
        body,
        ensure_ascii=False,
    ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=raw,
        headers={
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
        ) as response:

            response_raw = response.read()

    except urllib.error.HTTPError as e:

        detail = e.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RouterClientError(
            f"vLLM HTTP {e.code}: {detail}"
        ) from e

    except urllib.error.URLError as e:

        raise RouterClientError(
            f"vLLM connection failed: {e}"
        ) from e

    except TimeoutError as e:

        raise RouterClientError(
            "vLLM request timed out"
        ) from e

    try:
        parsed = json.loads(
            response_raw.decode("utf-8")
        )

        if os.getenv(
            "SOOMAC_ROUTER_TELEMETRY",
            "0",
        ) == "1":

            usage = parsed.get("usage") or {}

            message = (
                parsed.get("choices", [{}])[0]
                .get("message", {})
            )

            content = message.get("content")
            reasoning = (
                message.get("reasoning_content")
                or message.get("reasoning")
            )

            raw_acts = None

            if isinstance(content, str):
                try:
                    content_json = json.loads(content)

                    if isinstance(content_json, dict):
                        acts = content_json.get("acts")

                        if isinstance(acts, list):
                            raw_acts = len(acts)

                except Exception:
                    pass

            print(
                "[ROUTER TELEMETRY] "
                f"prompt={usage.get('prompt_tokens')} "
                f"completion={usage.get('completion_tokens')} "
                f"total={usage.get('total_tokens')} "
                f"raw_acts={raw_acts} "
                f"content_chars="
                f"{len(content) if isinstance(content, str) else None} "
                f"reasoning_chars="
                f"{len(reasoning) if isinstance(reasoning, str) else 0}"
            )

            if isinstance(content, str):
                print(
                    "[ROUTER RAW] "
                    + content.replace("\n", " ")
                )

        return parsed

    except Exception as e:

        raise RouterClientError(
            "vLLM returned invalid JSON response"
        ) from e


def _extract_content(
    response: dict[str, Any],
) -> str:

    try:
        message = (
            response["choices"][0]["message"]
        )

        content = message["content"]

    except (
        KeyError,
        IndexError,
        TypeError,
    ) as e:

        raise RouterClientError(
            "unexpected vLLM response shape: "
            + json.dumps(
                response,
                ensure_ascii=False,
            )
        ) from e

    if isinstance(content, str):
        return content

    # 일부 OpenAI-compatible 응답에서
    # content part 배열 형태가 올 수도 있으므로 대응.
    if isinstance(content, list):

        parts = []

        for item in content:

            if not isinstance(item, dict):
                continue

            if isinstance(
                item.get("text"),
                str,
            ):
                parts.append(
                    item["text"]
                )

        if parts:
            return "".join(parts)

    raise RouterClientError(
        "message.content is not usable"
    )




def _repair_single_act_self_dependency(
    output: RouterOutput,
) -> RouterOutput:
    """
    단일 act의 depends_on_act=0은
    자기 자신을 참조하는 불가능한 dependency이므로 제거한다.

    다중 act의 정상적인 선행 dependency는 유지한다.
    """
    if len(output.acts) != 1:
        return output

    if output.acts[0].depends_on_act != 0:
        return output

    data = output.model_dump(
        mode="json",
    )

    data["acts"][0]["depends_on_act"] = None

    return RouterOutput.model_validate(
        data
    )
def route(
    utterance: str,
    *,
    history: list[dict[str, Any]] | None = None,
    pending: dict[str, Any] | None = None,
    order_state: dict[str, Any] | None = None,
    timeout: float = 30.0,
    model: str | None = None,
) -> RouterOutput:

    body = build_request_body(
        utterance,
        history=history,
        pending=pending,
        order_state=order_state,
        model=model,
    )

    response = _post_json(
        get_router_url()
        + "/chat/completions",
        body,
        timeout,
    )

    try:
        content = _extract_content(
            response
        )
    except RouterClientError:
        return safe_clarify_output()

    try:
        parsed_json = json.loads(
            content
        )

    except json.JSONDecodeError as e:

        raise RouterClientError(
            "Router model content is not JSON: "
            + repr(content)
        ) from e

    try:
        raw_output = (
            RouterOutput.model_validate(
                parsed_json
            )
        )

    except Exception:
        # Invalid family/subtype or malformed semantic output
        # must never become an executable order mutation.
        raw_output = safe_clarify_output()

    # V14가 같은 recommendation을 여러 act로 반복해도
    # structured semantics 기준으로 하나로 병합한다.
    raw_output = canonicalize_recommendation_output(
        raw_output
    )

    normalized = normalize_router_output(
        raw_output,
        utterance=utterance,
        history=history,
        pending=pending,
        order_state=order_state,
    )

    normalized = canonicalize_recommendation_output(
        normalized
    )

    # recommendation은 이미 V14가 의미를 해석한 read-only 결과다.
    # deterministic resolver가 칼로리/재료 같은 단어를 보고
    # 다시 info_query로 재분류하지 못하게 한다.
    recommendation_only = (
        bool(normalized.acts)
        and all(
            act.family.value == "recommendation"
            for act in normalized.acts
        )
    )

    if recommendation_only:
        resolved = normalized

    else:
        resolved = resolve_router_deterministic(
            normalized,
            utterance=utterance,
            history=history,
            pending=pending,
            order_state=order_state,
        )
        resolved = _repair_single_act_self_dependency(resolved)

    guarded = apply_router_safety_guard(
        resolved,
        utterance=utterance,
        history=history,
        pending=pending,
        order_state=order_state,
    )

    return canonicalize_recommendation_output(
        guarded
    )


if __name__ == "__main__":

    result = route(
        "제로콜라 얼마예요?",
        history=[
            {
                "role": "staff",
                "text": "음료를 선택해주세요.",
            }
        ],
        pending={
            "line_id": 1,
            "field": "drink",
        },
        order_state={
            "items": [
                {
                    "line_id": 1,
                    "item_type": "burger",
                    "menu": "bulgogi_burger",
                }
            ]
        },
    )

    print(
        json.dumps(
            result.model_dump(
                mode="json",
                exclude_none=True,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
