#!/usr/bin/env python3

import json
from pathlib import Path

from router_schema import RouterOutput
from router_policy import evaluate_router_output
from router_gold_canonical import canonicalize_case


OUT = (
    Path(__file__).parent
    / "router_data/gold/router_gold_intent_v1.jsonl"
)


def ref(
    source="explicit",
    resolved=True,
    value=None,
    line_ids=None,
):
    return {
        "source": source,
        "resolved": resolved,
        "value": value,
        "line_ids": line_ids or [],
    }


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
            else ref(
                source="none",
                resolved=False,
            )
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
    tag=None,
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
    }


cases = []


# ============================================================
# A. ORDER ACTION vs POSSIBILITY
# ============================================================

pairs = [
    (
        "치즈스틱 10개 주세요",
        "치즈스틱 10개 가능해요?",
        "side",
        "cheese_stick",
        10,
    ),
    (
        "감자튀김 12개 주세요",
        "감자튀김 12개 주문 가능한가요?",
        "side",
        "french_fries",
        12,
    ),
    (
        "콜라 5잔 주세요",
        "콜라 5잔 주문할 수 있나요?",
        "drink",
        "coke",
        5,
    ),
    (
        "제로콜라 두 잔 주세요",
        "제로콜라 두 잔 가능할까요?",
        "drink",
        "zero_coke",
        2,
    ),
    (
        "불고기버거 10개 주세요",
        "불고기버거 10개 가능한가요?",
        "burger",
        "bulgogi_burger",
        10,
    ),
    (
        "치킨버거 두 개 주세요",
        "치킨버거 두 개 주문되나요?",
        "burger",
        "chicken_burger",
        2,
    ),
    (
        "새우버거 하나 주세요",
        "새우버거 하나 주문할 수 있어요?",
        "burger",
        "shrimp_burger",
        1,
    ),
    (
        "치즈버거 세 개 주세요",
        "치즈버거 세 개 가능해요?",
        "burger",
        "cheese_burger",
        3,
    ),
    (
        "아이스커피 세 잔 주세요",
        "아이스커피 세 잔 가능한가요?",
        "drink",
        "iced_coffee",
        3,
    ),
    (
        "사이다 네 잔 주세요",
        "사이다 네 잔 주문돼요?",
        "drink",
        "sprite",
        4,
    ),
]

counter = 1

for order_text, question_text, domain, target, qty in pairs:

    cases.append(
        case(
            f"I{counter:03d}",
            order_text,
            [
                act(
                    "order_action",
                    "add",
                    "request",
                    "explicit",
                    target_domain=domain,
                    target=target,
                    quantity=qty,
                    reference=ref(
                        value=target,
                    ),
                )
            ],
            ["execute_order"],
            tag="order_vs_possibility",
        )
    )
    counter += 1

    cases.append(
        case(
            f"I{counter:03d}",
            question_text,
            [
                act(
                    "info_query",
                    "possibility",
                    "question",
                    "none",
                    target_domain=domain,
                    target=target,
                    quantity=qty,
                    reference=ref(
                        value=target,
                    ),
                )
            ],
            ["read_only"],
            tag="order_vs_possibility",
        )
    )
    counter += 1


# ============================================================
# B. MODIFY vs POSSIBILITY
# ============================================================

modify_cases = [
    (
        "콜라를 제로콜라로 바꿔주세요",
        "콜라를 제로콜라로 바꿀 수 있어요?",
        "drink",
        "zero_coke",
    ),
    (
        "감자튀김 대신 치즈스틱으로 해주세요",
        "감자튀김 대신 치즈스틱 가능해요?",
        "side",
        "cheese_stick",
    ),
    (
        "라지로 바꿔주세요",
        "라지로 변경 가능한가요?",
        "drink",
        "large",
    ),
    (
        "세트로 바꿔주세요",
        "세트로 바꿀 수 있어요?",
        "burger",
        "set",
    ),
    (
        "단품으로 바꿔주세요",
        "단품으로 변경 가능한가요?",
        "burger",
        "single",
    ),
    (
        "아이스커피로 변경해주세요",
        "아이스커피로 변경 가능해요?",
        "drink",
        "iced_coffee",
    ),
    (
        "치즈스틱으로 변경해주세요",
        "치즈스틱으로 변경할 수 있나요?",
        "side",
        "cheese_stick",
    ),
    (
        "제로콜라 라지로 해주세요",
        "제로콜라 라지로 할 수 있나요?",
        "drink",
        "zero_coke",
    ),
    (
        "첫 번째 버거 피클 빼주세요",
        "첫 번째 버거 피클 뺄 수 있어요?",
        "ingredient",
        "pickle",
    ),
    (
        "베이컨 추가해주세요",
        "베이컨 추가 가능한가요?",
        "topping",
        "bacon",
    ),
]

for order_text, question_text, domain, target in modify_cases:

    cases.append(
        case(
            f"I{counter:03d}",
            order_text,
            [
                act(
                    "order_action",
                    "modify",
                    "request",
                    "explicit",
                    target_domain=domain,
                    target=target,
                    reference=ref(
                        value=target,
                    ),
                )
            ],
            ["execute_order"],
            tag="modify_vs_possibility",
        )
    )
    counter += 1

    cases.append(
        case(
            f"I{counter:03d}",
            question_text,
            [
                act(
                    "info_query",
                    "possibility",
                    "question",
                    "none",
                    target_domain=domain,
                    target=target,
                    reference=ref(
                        value=target,
                    ),
                )
            ],
            ["read_only"],
            tag="modify_vs_possibility",
        )
    )
    counter += 1


# ============================================================
# C. PRICE QUERY vs ORDER
# ============================================================

price_cases = [
    (
        "치즈스틱 하나 주세요",
        "치즈스틱 하나 얼마예요?",
        "side",
        "cheese_stick",
        1,
    ),
    (
        "치즈스틱 10개 주세요",
        "치즈스틱 10개면 얼마예요?",
        "side",
        "cheese_stick",
        10,
    ),
    (
        "불고기버거 하나 주세요",
        "불고기버거 얼마예요?",
        "burger",
        "bulgogi_burger",
        1,
    ),
    (
        "치즈버거 하나 주세요",
        "치즈버거 가격이 얼마예요?",
        "burger",
        "cheese_burger",
        1,
    ),
    (
        "새우버거 두 개 주세요",
        "새우버거 두 개면 얼마예요?",
        "burger",
        "shrimp_burger",
        2,
    ),
    (
        "콜라 세 잔 주세요",
        "콜라 세 잔 얼마예요?",
        "drink",
        "coke",
        3,
    ),
    (
        "제로콜라 두 잔 주세요",
        "제로콜라 두 잔 가격 알려주세요",
        "drink",
        "zero_coke",
        2,
    ),
    (
        "아이스커피 하나 주세요",
        "아이스커피 얼마예요?",
        "drink",
        "iced_coffee",
        1,
    ),
    (
        "감자튀김 하나 주세요",
        "감자튀김 가격 얼마예요?",
        "side",
        "french_fries",
        1,
    ),
    (
        "사이다 다섯 잔 주세요",
        "사이다 다섯 잔이면 얼마예요?",
        "drink",
        "sprite",
        5,
    ),
]

for order_text, price_text, domain, target, qty in price_cases:

    cases.append(
        case(
            f"I{counter:03d}",
            order_text,
            [
                act(
                    "order_action",
                    "add",
                    "request",
                    "explicit",
                    target_domain=domain,
                    target=target,
                    quantity=qty,
                    reference=ref(
                        value=target,
                    ),
                )
            ],
            ["execute_order"],
            tag="order_vs_price",
        )
    )
    counter += 1

    cases.append(
        case(
            f"I{counter:03d}",
            price_text,
            [
                act(
                    "info_query",
                    "price",
                    "question",
                    "none",
                    target_domain=domain,
                    target=target,
                    quantity=qty,
                    reference=ref(
                        value=target,
                    ),
                )
            ],
            ["read_only"],
            tag="order_vs_price",
        )
    )
    counter += 1


# ============================================================
# D. MENU / STOCK / RECOMMENDATION
# ============================================================

readonly_cases = [
    (
        "메뉴 뭐 있어요?",
        "info_query",
        "menu",
        "question",
        "none",
        None,
        None,
        "menu_query",
    ),
    (
        "버거 종류 뭐 있어요?",
        "info_query",
        "menu",
        "question",
        "none",
        "menu",
        None,
        "menu_query",
    ),
    (
        "음료 뭐 있어요?",
        "info_query",
        "menu",
        "question",
        "none",
        "drink",
        None,
        "menu_query",
    ),
    (
        "사이드 메뉴 뭐 있어요?",
        "info_query",
        "menu",
        "question",
        "none",
        "side",
        None,
        "menu_query",
    ),
    (
        "치즈스틱 지금 남아 있어요?",
        "info_query",
        "stock",
        "question",
        "none",
        "side",
        "cheese_stick",
        "stock_query",
    ),
    (
        "제로콜라 품절이에요?",
        "info_query",
        "stock",
        "question",
        "none",
        "drink",
        "zero_coke",
        "stock_query",
    ),
    (
        "새우버거 지금 주문돼요?",
        "info_query",
        "stock",
        "question",
        "none",
        "burger",
        "shrimp_burger",
        "stock_query",
    ),
    (
        "뭐 추천해줄 수 있어?",
        "recommendation",
        "general_recommendation",
        "question",
        "none",
        None,
        None,
        "recommendation",
    ),
    (
        "안 매운 거 추천해줘",
        "recommendation",
        "preference_recommendation",
        "request",
        "none",
        None,
        None,
        "recommendation",
    ),
    (
        "만원 안으로 추천해주세요",
        "recommendation",
        "budget_recommendation",
        "request",
        "none",
        None,
        None,
        "recommendation",
    ),
    (
        "치즈버거랑 불고기버거 중 뭐가 나아요?",
        "recommendation",
        "comparison",
        "question",
        "none",
        "burger",
        None,
        "comparison",
    ),
    (
        "카드 돼요?",
        "info_query",
        "payment",
        "question",
        "none",
        "payment",
        "card",
        "payment",
    ),
    (
        "계좌이체 가능해요?",
        "info_query",
        "payment",
        "question",
        "none",
        "payment",
        "bank_transfer",
        "payment",
    ),
    (
        "매장에 자리 있어요?",
        "info_query",
        "store",
        "question",
        "none",
        "store",
        "seat",
        "store",
    ),
    (
        "화장실 있어요?",
        "info_query",
        "store",
        "question",
        "none",
        "store",
        "restroom",
        "store",
    ),
    (
        "얼마나 걸려요?",
        "info_query",
        "wait_time",
        "question",
        "none",
        "order",
        None,
        "wait_time",
    ),
    (
        "이 버거에 뭐 들어가요?",
        "info_query",
        "ingredient",
        "question",
        "none",
        "burger",
        None,
        "ingredient",
    ),
    (
        "알레르기 정보 있어요?",
        "info_query",
        "allergen",
        "question",
        "none",
        "menu",
        None,
        "allergen",
    ),
    (
        "칼로리 얼마예요?",
        "info_query",
        "nutrition",
        "question",
        "none",
        "menu",
        None,
        "nutrition",
    ),
    (
        "세트에는 뭐 들어가요?",
        "info_query",
        "option",
        "question",
        "none",
        "burger",
        "set",
        "option",
    ),
]

for (
    text,
    family,
    subtype,
    speech,
    commitment,
    domain,
    target,
    tag,
) in readonly_cases:

    reference = (
        ref(
            value=target,
        )
        if target is not None
        else ref(
            source="none",
            resolved=False,
        )
    )

    cases.append(
        case(
            f"I{counter:03d}",
            text,
            [
                act(
                    family,
                    subtype,
                    speech,
                    commitment,
                    target_domain=domain,
                    target=target,
                    reference=reference,
                )
            ],
            ["read_only"],
            tag=tag,
        )
    )
    counter += 1


# ============================================================
# E. TENTATIVE / CLARIFY / NO ACTION
# ============================================================

ambiguous_cases = [
    (
        "치즈버거로 할까...",
        "burger",
        "cheese_burger",
    ),
    (
        "불고기버거 먹을까...",
        "burger",
        "bulgogi_burger",
    ),
    (
        "치즈스틱으로 할까 말까",
        "side",
        "cheese_stick",
    ),
    (
        "제로콜라가 나으려나",
        "drink",
        "zero_coke",
    ),
    (
        "라지로 할까",
        "drink",
        "large",
    ),
]

for text, domain, target in ambiguous_cases:

    cases.append(
        case(
            f"I{counter:03d}",
            text,
            [
                act(
                    "order_action",
                    "add",
                    "statement",
                    "tentative",
                    target_domain=domain,
                    target=target,
                    reference=ref(
                        value=target,
                    ),
                    resolution="ambiguous",
                )
            ],
            ["clarify"],
            tag="tentative",
        )
    )
    counter += 1


clarify_texts = [
    "그거요",
    "그걸로",
    "아까 거요",
    "그냥 그거",
    "저거 주세요",
]

for text in clarify_texts:

    cases.append(
        case(
            f"I{counter:03d}",
            text,
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
            tag="unresolved_reference",
        )
    )
    counter += 1


# ============================================================
# F. CONDITIONAL MULTI-ACT
# ============================================================

conditional_cases = [
    (
        "제로콜라 있으면 두 잔 주세요",
        "drink",
        "zero_coke",
        2,
    ),
    (
        "치즈스틱 있으면 세 개 주세요",
        "side",
        "cheese_stick",
        3,
    ),
    (
        "새우버거 있으면 하나 주세요",
        "burger",
        "shrimp_burger",
        1,
    ),
    (
        "아이스커피 가능하면 두 잔 주세요",
        "drink",
        "iced_coffee",
        2,
    ),
    (
        "감자튀김 주문 가능하면 다섯 개 주세요",
        "side",
        "french_fries",
        5,
    ),
]

for text, domain, target, qty in conditional_cases:

    condition_type = (
        "stock_available"
        if "있으면" in text
        else "possible"
    )

    query_subtype = (
        "stock"
        if condition_type == "stock_available"
        else "possibility"
    )

    cases.append(
        case(
            f"I{counter:03d}",
            text,
            [
                act(
                    "info_query",
                    query_subtype,
                    "question",
                    "none",
                    target_domain=domain,
                    target=target,
                    reference=ref(
                        value=target,
                    ),
                ),
                act(
                    "order_action",
                    "add",
                    "request",
                    "conditional",
                    target_domain=domain,
                    target=target,
                    quantity=qty,
                    reference=ref(
                        value=target,
                    ),
                    condition={
                        "type": condition_type,
                        "expected": True,
                    },
                    depends_on_act=0,
                ),
            ],
            [
                "read_only",
                "wait_condition",
            ],
            tag="conditional",
        )
    )
    counter += 1


# ============================================================
# WRITE + SELF VALIDATION
# ============================================================

OUT.parent.mkdir(
    parents=True,
    exist_ok=True,
)


ids = set()
utterance_keys = {}
written = 0

with OUT.open(
    "w",
    encoding="utf-8",
) as f:

    for item in cases:

        if item["id"] in ids:
            raise ValueError(
                f"duplicate id: {item['id']}"
            )

        ids.add(
            item["id"]
        )

        duplicate_key = (
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

        if duplicate_key in utterance_keys:

            previous_signature = (
                utterance_keys[
                    duplicate_key
                ]
            )

            if previous_signature != answer_signature:
                raise ValueError(
                    "CONFLICTING GOLD LABEL: "
                    + item["utterance"]
                )

            print(
                "[SKIP duplicate] "
                + item["utterance"]
            )

            continue

        utterance_keys[
            duplicate_key
        ] = answer_signature

        item = canonicalize_case(item)

        parsed = RouterOutput.model_validate(
            item["expected"]
        )

        decisions = evaluate_router_output(
            parsed
        )

        actual_policy = [
            x.status.value
            for x in decisions
        ]

        if actual_policy != item["expected_policy"]:

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


print(
    f"[OK] generated={len(cases)} / unique_written={written}"
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
        f"{name:24s} "
        f"{tags[name]:3d}"
    )
