#!/usr/bin/env python3

import re


TARGET_KO = {
    "bulgogi_burger": "불고기버거",
    "chicken_burger": "치킨버거",
    "cheese_burger": "치즈버거",
    "shrimp_burger": "새우버거",

    "coke": "콜라",
    "zero_coke": "제로콜라",
    "sprite": "사이다",
    "fanta": "환타",
    "iced_coffee": "아이스커피",

    "french_fries": "감자튀김",
    "cheese_stick": "치즈스틱",

    "single": "단품",
    "set": "세트",
}


def _compact(text):
    return re.sub(
        r"\s+",
        "",
        text or "",
    )


def _size_word(text):
    compact = _compact(text)

    if (
        "라지" in compact
        or "large" in compact.lower()
    ):
        return "라지"

    if (
        "미디움" in compact
        or "미디엄" in compact
        or "medium" in compact.lower()
    ):
        return "미디움"

    if (
        "스몰" in compact
        or "small" in compact.lower()
    ):
        return "스몰"

    return None


def _quantity_word(quantity):
    if quantity is None:
        return "하나"

    try:
        quantity = int(quantity)
    except Exception:
        return "하나"

    if quantity == 1:
        return "하나"

    return f"{quantity}개"


def build_router_runtime_text(
    router_output,
    original_text,
):
    """
    Router가 문맥까지 해결한 단순 주문은
    Runtime이 다시 문맥을 잃지 않도록 canonical text로 보강한다.

    아직 Runtime LLM은 유지한다.
    즉 correctness bridge이며,
    다음 단계에서 이 두 번째 LLM 호출 자체를 제거한다.
    """

    try:
        data = router_output.model_dump(
            mode="json"
        )
    except Exception:
        return original_text

    acts = data.get(
        "acts",
        [],
    )

    if len(acts) != 1:
        return original_text

    act = acts[0]

    if (
        act.get("family")
        != "order_action"
    ):
        return original_text

    subtype = act.get(
        "subtype"
    )

    target = act.get(
        "target"
    )

    domain = act.get(
        "target_domain"
    )

    quantity = act.get(
        "quantity"
    )

    ko = TARGET_KO.get(
        target
    )

    if not ko:
        return original_text

    compact = _compact(
        original_text
    )

    # --------------------------------------------------------
    # pending 단품/세트 답변
    # --------------------------------------------------------
    if (
        subtype == "modify"
        and target in {
            "single",
            "set",
        }
    ):
        return f"{ko}으로 주세요"

    # --------------------------------------------------------
    # 음료 추가
    #
    # 예:
    # 이전 대화: "제로콜라 얼마예요?"
    # 현재:      "스몰로 하나 주세요"
    # Router:    target=zero_coke
    #
    # Runtime 입력:
    # "제로콜라 스몰 하나 추가해주세요"
    # --------------------------------------------------------
    if (
        subtype == "add"
        and domain == "drink"
    ):
        size = _size_word(
            original_text
        )

        parts = [ko]

        if size:
            parts.append(size)

        parts.append(
            _quantity_word(
                quantity
            )
        )

        parts.append(
            "추가해주세요"
        )

        return " ".join(
            parts
        )

    # --------------------------------------------------------
    # 명확한 단일 버거 추가
    # --------------------------------------------------------
    if (
        subtype == "add"
        and domain == "burger"
        and target.endswith(
            "_burger"
        )
    ):
        parts = [
            ko,
            _quantity_word(
                quantity
            ),
            "주세요",
        ]

        return " ".join(
            parts
        )

    # --------------------------------------------------------
    # 사이드 추가
    # --------------------------------------------------------
    if (
        subtype == "add"
        and domain == "side"
    ):
        return (
            f"{ko} "
            f"{_quantity_word(quantity)} "
            "추가해주세요"
        )

    return original_text
