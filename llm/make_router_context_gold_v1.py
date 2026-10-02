#!/usr/bin/env python3

import json
from pathlib import Path

from router_schema import RouterOutput
from router_policy import evaluate_router_output


OUT = (
    Path(__file__).parent
    / "router_data/gold/router_gold_context_v1.jsonl"
)


# ============================================================
# HELPERS
# ============================================================

def ref(
    source="none",
    resolved=False,
    value=None,
    line_ids=None,
):
    data = {
        "source": source,
        "resolved": resolved,
        "line_ids": line_ids or [],
    }

    if value is not None:
        data["value"] = value

    return data


def act(
    family,
    subtype,
    speech_act,
    commitment,
    *,
    target_domain=None,
    target=None,
    quantity=None,
    reference=None,
    condition=None,
    depends_on_act=None,
    resolution="clear",
):
    data = {
        "family": family,
        "subtype": subtype,
        "speech_act": speech_act,
        "commitment": commitment,
        "reference": (
            reference
            if reference is not None
            else ref()
        ),
        "resolution": resolution,
    }

    if target_domain is not None:
        data["target_domain"] = target_domain

    if target is not None:
        data["target"] = target

    if quantity is not None:
        data["quantity"] = quantity

    if condition is not None:
        data["condition"] = condition

    if depends_on_act is not None:
        data["depends_on_act"] = depends_on_act

    return data


def case(
    case_id,
    utterance,
    acts,
    expected_policy,
    *,
    history=None,
    pending=None,
    items=None,
    tag,
    policy_reason,
):
    return {
        "id": case_id,
        "tag": tag,
        "utterance": utterance,
        "context": {
            "history": history or [],
            "pending": pending,
            "order_state": {
                "items": items or [],
            },
        },
        "expected": {
            "acts": acts,
        },
        "expected_policy": expected_policy,
        "policy_reason": policy_reason,
    }


cases = []
counter = 1


# ============================================================
# A. AFFIRM / DENY CONTEXT
# 20 cases
# ============================================================

affirm_deny = [
    (
        "제로콜라로 변경해드릴까요?",
        "네",
        "order_action",
        "modify",
        "affirm",
        "explicit",
        "drink",
        "zero_coke",
        "execute_order",
    ),
    (
        "제로콜라로 변경해드릴까요?",
        "아니요",
        "no_action",
        "deny",
        "deny",
        "none",
        None,
        None,
        "no_action",
    ),
    (
        "치즈스틱 하나 추가해드릴까요?",
        "네",
        "order_action",
        "add",
        "affirm",
        "explicit",
        "side",
        "cheese_stick",
        "execute_order",
    ),
    (
        "치즈스틱 하나 추가해드릴까요?",
        "괜찮아요",
        "no_action",
        "deny",
        "deny",
        "none",
        None,
        None,
        "no_action",
    ),
    (
        "이대로 주문 확정할까요?",
        "네",
        "order_action",
        "finalize",
        "affirm",
        "explicit",
        "order",
        "current_order",
        "execute_order",
    ),
    (
        "이대로 주문 확정할까요?",
        "아니요 아직이요",
        "no_action",
        "deny",
        "deny",
        "none",
        None,
        None,
        "no_action",
    ),
    (
        "가격을 알려드릴까요?",
        "네",
        "info_query",
        "price",
        "affirm",
        "none",
        "order",
        "current_order",
        "read_only",
    ),
    (
        "메뉴 목록을 알려드릴까요?",
        "네",
        "info_query",
        "menu",
        "affirm",
        "none",
        "menu",
        "menu",
        "read_only",
    ),
    (
        "직원을 호출해드릴까요?",
        "네",
        "staff_request",
        "staff_call",
        "affirm",
        "explicit",
        "staff",
        "staff",
        "event",
    ),
    (
        "직원을 호출해드릴까요?",
        "아니요",
        "no_action",
        "deny",
        "deny",
        "none",
        None,
        None,
        "no_action",
    ),
]

# 각 기본 상황을 표현 변형 2개씩 = 20
variants = {
    "네": ["네", "네 해주세요"],
    "아니요": ["아니요", "아뇨"],
    "괜찮아요": ["괜찮아요", "아니요 괜찮습니다"],
    "아니요 아직이요": ["아니요 아직이요", "아직 확정하지 마세요"],
}

for (
    staff_text,
    base_customer,
    family,
    subtype,
    speech,
    commitment,
    domain,
    target,
    policy,
) in affirm_deny:

    customer_variants = variants.get(
        base_customer,
        [base_customer, base_customer + "요"],
    )

    for customer_text in customer_variants[:2]:

        reference = (
            ref(
                source="previous_staff",
                resolved=True,
                value=target,
            )
            if target is not None
            else ref(
                source="previous_staff",
                resolved=True,
            )
        )

        # 직전 STAFF의 구체적인 변경 제안을
        # "네/아니요"로 승인 또는 거절한 경우,
        # 실행 여부와 관계없이 제안 대상 reference를 보존한다.
        if staff_text == "제로콜라로 변경해드릴까요?":
            reference = ref(
                source="previous_staff",
                resolved=True,
                value="zero_coke",
                line_ids=[1],
            )

        cases.append(
            case(
                f"C{counter:03d}",
                customer_text,
                [
                    act(
                        family,
                        subtype,
                        speech,
                        commitment,
                        target_domain=domain,
                        target=(
                            None
                            if target in {"current_order", "menu", "staff"}
                            else target
                        ),
                        reference=reference,
                        resolution="context_resolved",
                    )
                ],
                [policy],
                history=[
                    {
                        "role": "staff",
                        "text": staff_text,
                    }
                ],
                items=[
                    {
                        "line_id": 1,
                        "item_type": "drink",
                        "drink": "coke",
                    }
                ],
                tag="affirm_deny",
                policy_reason="resolve_previous_staff_proposal",
            )
        )
        counter += 1


# ============================================================
# B. PENDING INTERACTION
# 30 cases
# ============================================================

pending_specs = [
    (
        "drink",
        "음료를 선택해주세요.",
        [
            ("제로콜라로요", "order_action", "modify", "request", "explicit", "zero_coke", "execute_order"),
            ("콜라로요", "order_action", "modify", "request", "explicit", "coke", "execute_order"),
            ("사이다요", "order_action", "modify", "request", "explicit", "sprite", "execute_order"),
            ("음료 뭐 있어요?", "info_query", "menu", "question", "none", None, "read_only"),
            ("제로콜라 얼마예요?", "info_query", "price", "question", "none", "zero_coke", "read_only"),
            ("제로콜라 가능해요?", "info_query", "possibility", "question", "none", "zero_coke", "read_only"),
        ],
    ),
    (
        "side",
        "사이드를 선택해주세요.",
        [
            ("치즈스틱으로요", "order_action", "modify", "request", "explicit", "cheese_stick", "execute_order"),
            ("감자튀김으로요", "order_action", "modify", "request", "explicit", "french_fries", "execute_order"),
            ("사이드 뭐 있어요?", "info_query", "menu", "question", "none", None, "read_only"),
            ("치즈스틱 얼마예요?", "info_query", "price", "question", "none", "cheese_stick", "read_only"),
            ("치즈스틱 가능해요?", "info_query", "possibility", "question", "none", "cheese_stick", "read_only"),
            ("잠깐만요", "conversation_control", "pause", "request", "none", None, "read_only"),
        ],
    ),
    (
        "drink_size",
        "음료 사이즈를 선택해주세요.",
        [
            ("라지로요", "order_action", "modify", "request", "explicit", "large", "execute_order"),
            ("미디움으로요", "order_action", "modify", "request", "explicit", "medium", "execute_order"),
            ("라지는 얼마 추가돼요?", "info_query", "price", "question", "none", "large", "read_only"),
            ("라지 가능해요?", "info_query", "possibility", "question", "none", "large", "read_only"),
            ("사이즈 뭐 있어요?", "info_query", "option", "question", "none", None, "read_only"),
            ("잠깐 생각할게요", "conversation_control", "pause", "statement", "none", None, "read_only"),
        ],
    ),
    (
        "type",
        "단품과 세트 중 선택해주세요.",
        [
            ("세트로요", "order_action", "modify", "request", "explicit", "set", "execute_order"),
            ("단품으로요", "order_action", "modify", "request", "explicit", "single", "execute_order"),
            ("세트는 얼마 추가돼요?", "info_query", "price", "question", "none", "set", "read_only"),
            ("세트 가능해요?", "info_query", "possibility", "question", "none", "set", "read_only"),
            ("세트에 뭐 들어가요?", "info_query", "option", "question", "none", "set", "read_only"),
            ("메뉴 좀 볼게요", "conversation_control", "pause", "statement", "none", None, "read_only"),
        ],
    ),
]

# 위 4*6=24
for field, staff_text, entries in pending_specs:

    for (
        utterance,
        family,
        subtype,
        speech,
        commitment,
        target,
        policy,
    ) in entries:

        is_mutation = family == "order_action"

        reference = (
            ref(
                source="pending" if is_mutation else "explicit",
                resolved=True,
                value=target,
                line_ids=[1] if is_mutation else [],
            )
            if target is not None
            else ref()
        )

        domain = (
            "side"
            if field == "side"
            else "burger"
            if field == "type"
            else "drink"
        )

        cases.append(
            case(
                f"C{counter:03d}",
                utterance,
                [
                    act(
                        family,
                        subtype,
                        speech,
                        commitment,
                        target_domain=domain,
                        target=target,
                        reference=reference,
                        resolution=(
                            "context_resolved"
                            if is_mutation
                            else "clear"
                        ),
                    )
                ],
                [policy],
                history=[
                    {
                        "role": "staff",
                        "text": staff_text,
                    }
                ],
                pending={
                    "line_id": 1,
                    "field": field,
                },
                items=[
                    {
                        "line_id": 1,
                        "item_type": "burger",
                        "menu": "bulgogi_burger",
                        "type": (
                            None
                            if field == "type"
                            else "set"
                        ),
                    }
                ],
                tag="pending",
                policy_reason=(
                    "resolve_pending"
                    if is_mutation
                    else "query_does_not_consume_pending"
                ),
            )
        )
        counter += 1


# 6개 추가: pending인데 정보 없는 짧은 응답
for field, staff_text in [
    ("drink", "음료를 선택해주세요."),
    ("side", "사이드를 선택해주세요."),
    ("drink_size", "사이즈를 선택해주세요."),
    ("type", "단품과 세트 중 선택해주세요."),
    ("drink", "음료를 말씀해주세요."),
    ("side", "사이드 메뉴를 말씀해주세요."),
]:
    cases.append(
        case(
            f"C{counter:03d}",
            "네",
            [
                act(
                    "clarify",
                    "clarification",
                    "affirm",
                    "none",
                    reference=ref(
                        source="pending",
                        resolved=False,
                    ),
                    resolution="ambiguous",
                )
            ],
            ["clarify"],
            history=[
                {
                    "role": "staff",
                    "text": staff_text,
                }
            ],
            pending={
                "line_id": 1,
                "field": field,
            },
            items=[
                {
                    "line_id": 1,
                    "item_type": "burger",
                    "menu": "bulgogi_burger",
                }
            ],
            tag="pending",
            policy_reason="affirm_without_pending_value",
        )
    )
    counter += 1


# ============================================================
# C. PREVIOUS RECOMMENDATION / PROPOSAL
# 20 cases
# ============================================================

recommendation_contexts = [
    (
        "치즈버거 세트를 추천드릴게요.",
        "cheese_burger",
        "cheese_burger_set",
    ),
    (
        "불고기버거 세트를 추천드릴게요.",
        "bulgogi_burger",
        "bulgogi_burger_set",
    ),
]

recommendation_followups = [
    ("그걸로 주세요", "order_action", "add", "request", "explicit", "execute_order"),
    ("그거 얼마예요?", "info_query", "price", "question", "none", "read_only"),
    ("그거 괜찮아요?", "recommendation", "general_recommendation", "question", "none", "read_only"),
    ("그거 세트 맞아요?", "info_query", "option", "question", "none", "read_only"),
    ("그거 두 개 주세요", "order_action", "add", "request", "explicit", "execute_order"),
    ("다른 거 추천해주세요", "recommendation", "general_recommendation", "request", "none", "read_only"),
    ("좋네요", "no_action", "acknowledge", "acknowledgement", "none", "no_action"),
    ("맛있겠네요", "general_chat", "chat", "statement", "none", "read_only"),
    ("그건 말고 다른 거요", "recommendation", "general_recommendation", "request", "none", "read_only"),
    ("그거 주문 가능한가요?", "info_query", "possibility", "question", "none", "read_only"),
]

for staff_text, target, ref_value in recommendation_contexts:

    for (
        utterance,
        family,
        subtype,
        speech,
        commitment,
        policy,
    ) in recommendation_followups:

        order_action = family == "order_action"

        cases.append(
            case(
                f"C{counter:03d}",
                utterance,
                [
                    act(
                        family,
                        subtype,
                        speech,
                        commitment,
                        target_domain=(
                            "burger"
                            if family not in {
                                "no_action",
                                "general_chat",
                            }
                            else None
                        ),
                        target=(
                            target
                            if family not in {
                                "no_action",
                                "general_chat",
                            }
                            else None
                        ),
                        quantity=(
                            2
                            if "두 개" in utterance
                            else None
                        ),
                        reference=ref(
                            source="previous_staff",
                            resolved=True,
                            value=ref_value,
                        ),
                        resolution="context_resolved",
                    )
                ],
                [policy],
                history=[
                    {
                        "role": "staff",
                        "text": staff_text,
                    }
                ],
                tag="previous_recommendation",
                policy_reason=(
                    "resolve_previous_recommendation"
                    if order_action
                    else "read_previous_recommendation"
                ),
            )
        )
        counter += 1


# ============================================================
# D. CURRENT ORDER REFERENCES
# 30 cases
# ============================================================

order_items = [
    {
        "line_id": 1,
        "item_type": "burger",
        "menu": "bulgogi_burger",
        "type": "set",
        "drink": "coke",
        "drink_size": "medium",
        "side": "french_fries",
    },
    {
        "line_id": 2,
        "item_type": "burger",
        "menu": "bulgogi_burger",
        "type": "set",
        "drink": "zero_coke",
        "drink_size": "large",
        "side": "cheese_stick",
    },
    {
        "line_id": 3,
        "item_type": "burger",
        "menu": "chicken_burger",
        "type": "single",
    },
]

resolved_order_cases = [
    ("첫 번째 버거 피클 빼주세요", "modify", "ingredient", "pickle", [1]),
    ("두 번째 버거 피클 빼주세요", "modify", "ingredient", "pickle", [2]),
    ("세 번째 버거 세트로 바꿔주세요", "modify", "burger", "set", [3]),
    ("콜라 들어간 버거 피클 빼주세요", "modify", "ingredient", "pickle", [1]),
    ("제로콜라 들어간 버거 피클 빼주세요", "modify", "ingredient", "pickle", [2]),
    ("첫 번째 거 취소해주세요", "remove", "burger", "bulgogi_burger", [1]),
    ("두 번째 거 취소해주세요", "remove", "burger", "bulgogi_burger", [2]),
    ("세 번째 거 취소해주세요", "remove", "burger", "chicken_burger", [3]),
    ("불고기버거 둘 다 피클 빼주세요", "modify", "ingredient", "pickle", [1, 2]),
    ("제로콜라 들어간 거 라지 맞죠?", None, "order", "current_order", [2]),
]

for utterance, subtype, domain, target, line_ids in resolved_order_cases:

    if subtype is None:
        family = "info_query"
        subtype_value = "order_state"
        speech = "question"
        commitment = "none"
        policy = "read_only"
    else:
        family = "order_action"
        subtype_value = subtype
        speech = "request"
        commitment = "explicit"
        policy = "execute_order"

    cases.append(
        case(
            f"C{counter:03d}",
            utterance,
            [
                act(
                    family,
                    subtype_value,
                    speech,
                    commitment,
                    target_domain=domain,
                    target=target,
                    reference=ref(
                        source="current_order",
                        resolved=True,
                        value=target,
                        line_ids=line_ids,
                    ),
                    resolution="context_resolved",
                )
            ],
            [policy],
            items=order_items,
            tag="current_order_reference",
            policy_reason="unique_current_order_reference",
        )
    )
    counter += 1


ambiguous_order_texts = [
    "불고기버거 피클 빼주세요",
    "버거 하나 취소해주세요",
    "그 버거 피클 빼주세요",
    "불고기버거 하나만 취소해주세요",
    "세트 하나 라지로 바꿔주세요",
    "그중 하나만 피클 빼주세요",
    "하나만 취소해주세요",
    "아까 버거 수정해주세요",
    "그거 취소해주세요",
    "버거 하나 바꿔주세요",
]

for utterance in ambiguous_order_texts:

    cases.append(
        case(
            f"C{counter:03d}",
            utterance,
            [
                act(
                    "clarify",
                    "clarification",
                    "request",
                    "none",
                    target_domain="order",
                    reference=ref(
                        source="current_order",
                        resolved=False,
                    ),
                    resolution="ambiguous",
                )
            ],
            ["clarify"],
            items=order_items,
            tag="current_order_reference",
            policy_reason="multiple_current_order_candidates",
        )
    )
    counter += 1


query_order_cases = [
    ("첫 번째 거 얼마예요?", "price", [1]),
    ("두 번째 거 얼마예요?", "price", [2]),
    ("세 번째 거 얼마예요?", "price", [3]),
    ("첫 번째 거 뭐였죠?", "order_state", [1]),
    ("두 번째 거 뭐였죠?", "order_state", [2]),
    ("세 번째 거 뭐였죠?", "order_state", [3]),
    ("첫 번째랑 두 번째 가격 차이 얼마예요?", "price", [1, 2]),
    ("지금 버거 몇 개예요?", "order_state", [1, 2, 3]),
    ("제로콜라 들어간 게 몇 번째예요?", "order_state", [2]),
    ("치킨버거 단품 맞죠?", "order_state", [3]),
]

for utterance, subtype, line_ids in query_order_cases:

    cases.append(
        case(
            f"C{counter:03d}",
            utterance,
            [
                act(
                    "info_query",
                    subtype,
                    "question",
                    "none",
                    target_domain="order",
                    reference=ref(
                        source="current_order",
                        resolved=True,
                        value="current_order",
                        line_ids=line_ids,
                    ),
                    resolution="context_resolved",
                )
            ],
            ["read_only"],
            items=order_items,
            tag="current_order_reference",
            policy_reason="read_current_order_reference",
        )
    )
    counter += 1


# ============================================================
# E. ORDINAL / DEICTIC
# 20 cases
# ============================================================

choice_histories = [
    (
        "콜라와 제로콜라 중 선택해주세요.",
        "drink",
        "coke",
        "zero_coke",
    ),
    (
        "감자튀김과 치즈스틱 중 선택해주세요.",
        "side",
        "french_fries",
        "cheese_stick",
    ),
]

ordinal_followups = [
    ("첫 번째요", 1),
    ("첫 번째 걸로 주세요", 1),
    ("1번으로요", 1),
    ("두 번째요", 2),
    ("두 번째 걸로 주세요", 2),
]

for staff_text, domain, first, second in choice_histories:

    for utterance, ordinal in ordinal_followups:

        target = (
            first
            if ordinal == 1
            else second
        )

        cases.append(
            case(
                f"C{counter:03d}",
                utterance,
                [
                    act(
                        "order_action",
                        "modify",
                        "request",
                        "explicit",
                        target_domain=domain,
                        target=target,
                        reference=ref(
                            source="ordinal",
                            resolved=True,
                            value=str(ordinal),
                            line_ids=[1],
                        ),
                        resolution="context_resolved",
                    )
                ],
                ["execute_order"],
                history=[
                    {
                        "role": "staff",
                        "text": staff_text,
                    }
                ],
                pending={
                    "line_id": 1,
                    "field": (
                        "drink"
                        if domain == "drink"
                        else "side"
                    ),
                },
                tag="ordinal_deictic",
                policy_reason="ordinal_reference_resolved",
            )
        )
        counter += 1


# 10 ambiguous deictic
for staff_text in [
    "콜라와 제로콜라 중 선택해주세요.",
    "감자튀김과 치즈스틱 중 선택해주세요.",
]:
    for utterance in [
        "그거요",
        "그걸로요",
        "저거요",
        "그쪽 거요",
        "아까 그거요",
    ]:

        cases.append(
            case(
                f"C{counter:03d}",
                utterance,
                [
                    act(
                        "clarify",
                        "clarification",
                        "request",
                        "none",
                        reference=ref(
                            source="deictic",
                            resolved=False,
                        ),
                        resolution="ambiguous",
                    )
                ],
                ["clarify"],
                history=[
                    {
                        "role": "staff",
                        "text": staff_text,
                    }
                ],
                tag="ordinal_deictic",
                policy_reason="deictic_multiple_candidates",
            )
        )
        counter += 1


# ============================================================
# F. DISTANT REFERENCE / CONTEXT CONFLICT
# 20 cases
# ============================================================

distant_cases = [
    (
        [
            {"role": "staff", "text": "치즈버거 세트를 추천드릴게요."},
            {"role": "customer", "text": "가격은요?"},
            {"role": "staff", "text": "가격 안내를 드렸습니다."},
        ],
        "아까 추천한 거 주세요",
        "order_action",
        "add",
        "cheese_burger",
        "execute_order",
    ),
    (
        [
            {"role": "staff", "text": "불고기버거 세트를 추천드릴게요."},
            {"role": "customer", "text": "다른 메뉴도 있어요?"},
            {"role": "staff", "text": "치킨버거도 있습니다."},
        ],
        "아까 처음 추천한 거 주세요",
        "order_action",
        "add",
        "bulgogi_burger",
        "execute_order",
    ),
    (
        [
            {"role": "customer", "text": "치즈스틱 얼마예요?"},
            {"role": "staff", "text": "가격을 안내드렸습니다."},
        ],
        "아까 그거 하나 주세요",
        "order_action",
        "add",
        "cheese_stick",
        "execute_order",
    ),
    (
        [
            {"role": "customer", "text": "제로콜라 있나요?"},
            {"role": "staff", "text": "메뉴에 있습니다."},
        ],
        "그럼 그거 두 잔 주세요",
        "order_action",
        "add",
        "zero_coke",
        "execute_order",
    ),
    (
        [
            {"role": "staff", "text": "치즈버거와 새우버거를 추천드릴게요."},
            {"role": "customer", "text": "음..."},
            {"role": "staff", "text": "천천히 골라주세요."},
        ],
        "아까 추천한 거 주세요",
        "clarify",
        "clarification",
        None,
        "clarify",
    ),
]

# 각 4변형 = 20
distant_variants = [
    lambda x: x,
    lambda x: x.replace("주세요", "주실래요"),
    lambda x: x.replace("그거", "그 메뉴"),
    lambda x: x.replace("아까", "전에"),
]

for history, base_text, family, subtype, target, policy in distant_cases:

    for transform in distant_variants:

        utterance = transform(
            base_text
        )

        if family == "clarify":

            router_act = act(
                "clarify",
                "clarification",
                "request",
                "none",
                reference=ref(
                    source="conversation_history",
                    resolved=False,
                ),
                resolution="ambiguous",
            )

        else:

            domain = (
                "side"
                if target == "cheese_stick"
                else "drink"
                if target == "zero_coke"
                else "burger"
            )

            router_act = act(
                family,
                subtype,
                "request",
                "explicit",
                target_domain=domain,
                target=target,
                quantity=(
                    2
                    if "두 잔" in utterance
                    else 1
                    if target in {
                        "cheese_stick",
                        "zero_coke",
                    }
                    else None
                ),
                reference=ref(
                    source="conversation_history",
                    resolved=True,
                    value=target,
                ),
                resolution="context_resolved",
            )

        cases.append(
            case(
                f"C{counter:03d}",
                utterance,
                [router_act],
                [policy],
                history=history,
                tag="distant_context",
                policy_reason=(
                    "distant_reference_resolved"
                    if family != "clarify"
                    else "distant_reference_ambiguous"
                ),
            )
        )
        counter += 1


# ============================================================
# G. FINAL UNRESOLVED / CONFLICT
# 10 cases
# ============================================================

conflicts = [
    "네 아니요",
    "아니 네",
    "그거 말고 그거요",
    "첫 번째 말고... 잠깐만요",
    "아까 거 아니 그 전 거요",
    "그 메뉴요 아니 다른 거요",
    "하나 주세요 아니 잠깐만",
    "네 근데 아직 하지 마세요",
    "그걸로 할까... 모르겠어요",
    "아니요 맞아요 아니 잠깐만요",
]

for utterance in conflicts:

    cases.append(
        case(
            f"C{counter:03d}",
            utterance,
            [
                act(
                    "clarify",
                    "clarification",
                    "correction",
                    "none",
                    reference=ref(
                        source="conversation_history",
                        resolved=False,
                    ),
                    resolution="ambiguous",
                )
            ],
            ["clarify"],
            history=[
                {
                    "role": "staff",
                    "text": "치즈버거로 해드릴까요?",
                }
            ],
            tag="context_conflict",
            policy_reason="conflicting_commitment_or_reference",
        )
    )
    counter += 1


# ============================================================
# COUNT ASSERTION
# ============================================================

if len(cases) != 150:
    raise RuntimeError(
        f"expected 150 cases, got {len(cases)}"
    )


# ============================================================
# WRITE + VALIDATE
# ============================================================

OUT.parent.mkdir(
    parents=True,
    exist_ok=True,
)

seen_ids = set()
seen_inputs = {}

written = 0
skipped = 0

with OUT.open(
    "w",
    encoding="utf-8",
) as f:

    for item in cases:

        if item["id"] in seen_ids:
            raise ValueError(
                f"duplicate id: {item['id']}"
            )

        seen_ids.add(
            item["id"]
        )

        input_key = (
            item["utterance"],
            json.dumps(
                item["context"],
                ensure_ascii=False,
                sort_keys=True,
            ),
        )

        answer_signature = json.dumps(
            {
                "expected": item["expected"],
                "expected_policy": item["expected_policy"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )

        if input_key in seen_inputs:

            if (
                seen_inputs[input_key]
                != answer_signature
            ):
                raise ValueError(
                    "CONFLICTING GOLD LABEL: "
                    + item["utterance"]
                )

            print(
                "[SKIP duplicate] "
                + item["id"]
                + " "
                + item["utterance"]
            )

            skipped += 1
            continue

        seen_inputs[
            input_key
        ] = answer_signature

        parsed = RouterOutput.model_validate(
            item["expected"]
        )

        decisions = evaluate_router_output(
            parsed
        )

        actual_policy = [
            d.status.value
            for d in decisions
        ]

        if (
            actual_policy
            != item["expected_policy"]
        ):
            raise ValueError(
                f"{item['id']} policy mismatch: "
                f"{actual_policy} != "
                f"{item['expected_policy']}"
            )

        f.write(
            json.dumps(
                item,
                ensure_ascii=False,
            )
            + "\n"
        )

        written += 1


print()
print(
    f"[OK] generated={len(cases)} "
    f"unique_written={written} "
    f"skipped={skipped}"
)

print(
    f"[OK] {OUT}"
)


tags = {}

for item in cases:
    tags[item["tag"]] = (
        tags.get(
            item["tag"],
            0,
        )
        + 1
    )


print("\nCATEGORY COUNTS")

for name in sorted(tags):
    print(
        f"{name:28s} "
        f"{tags[name]:3d}"
    )
