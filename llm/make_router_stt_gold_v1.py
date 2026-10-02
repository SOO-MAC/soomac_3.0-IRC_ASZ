#!/usr/bin/env python3

import json
from pathlib import Path

from router_schema import RouterOutput
from router_policy import evaluate_router_output


OUT = (
    Path(__file__).parent
    / "router_data/gold/router_gold_stt_v1.jsonl"
)


# ============================================================
# HELPERS
# ============================================================

def ref(
    source="explicit",
    resolved=True,
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

    return data


def case(
    cid,
    utterance,
    acts,
    policies,
    *,
    tag,
    reason,
    history=None,
    pending=None,
    items=None,
):
    return {
        "id": cid,
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
        "expected_policy": policies,
        "policy_reason": reason,
    }


cases = []
counter = 1


def add_case(*args, **kwargs):
    global counter

    cases.append(
        case(
            f"S{counter:03d}",
            *args,
            **kwargs,
        )
    )

    counter += 1


PRODUCTS = [
    ("치즈스틱", "side", "cheese_stick"),
    ("감자튀김", "side", "french_fries"),
    ("콜라", "drink", "coke"),
    ("제로콜라", "drink", "zero_coke"),
    ("사이다", "drink", "sprite"),
    ("환타", "drink", "fanta"),
    ("아이스커피", "drink", "iced_coffee"),
    ("불고기버거", "burger", "bulgogi_burger"),
    ("치킨버거", "burger", "chicken_burger"),
    ("치즈버거", "burger", "cheese_burger"),
]


# ============================================================
# A. FILLER / HESITATION
# 주문 10 + 가능여부 10 + 가격 10 = 30
# ============================================================

for label, domain, target in PRODUCTS:

    # 주문
    add_case(
        f"어 음 {label} 하나 주세요",
        [
            act(
                "order_action",
                "add",
                "request",
                "explicit",
                target_domain=domain,
                target=target,
                quantity=1,
                reference=ref(
                    value=target,
                ),
            )
        ],
        ["execute_order"],
        tag="stt_filler",
        reason="filler_does_not_change_explicit_order",
    )

    # 가능 여부
    add_case(
        f"어 그 {label} 있잖아요 그거 주문 가능해요?",
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
        tag="stt_filler",
        reason="filler_question_remains_read_only",
    )

    # 가격
    add_case(
        f"음 저기 {label} 그거 얼마예요?",
        [
            act(
                "info_query",
                "price",
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
        tag="stt_filler",
        reason="filler_price_query",
    )


# ============================================================
# B. SPACING / PARTICLE LOSS / COMPACT STT
# 주문 10 + 가능여부 10 = 20
# ============================================================

for label, domain, target in PRODUCTS:

    # STT가 띄어쓰기 거의 없이 반환한 형태
    add_case(
        f"{label}하나주세요",
        [
            act(
                "order_action",
                "add",
                "request",
                "explicit",
                target_domain=domain,
                target=target,
                quantity=1,
                reference=ref(
                    value=target,
                ),
            )
        ],
        ["execute_order"],
        tag="stt_compact",
        reason="spacing_loss_order",
    )

    add_case(
        f"{label}주문가능해요",
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
        tag="stt_compact",
        reason="spacing_loss_question",
    )


# ============================================================
# C. RESTART / REPETITION
# 20
# ============================================================

for label, domain, target in PRODUCTS:

    for utterance in [
        f"{label} 하나... 하나 주세요",
        f"어 {label}... {label} 하나 주세요",
    ]:

        add_case(
            utterance,
            [
                act(
                    "order_action",
                    "add",
                    "request",
                    "explicit",
                    target_domain=domain,
                    target=target,
                    quantity=1,
                    reference=ref(
                        value=target,
                    ),
                )
            ],
            ["execute_order"],
            tag="stt_repetition",
            reason="speech_restart_same_target_not_duplicate_order",
        )


# ============================================================
# D. STT SELF CORRECTION
# 10 pairs * 2 = 20
# ============================================================

CORRECTIONS = [
    ("콜라", "제로콜라", "drink", "zero_coke"),
    ("제로콜라", "콜라", "drink", "coke"),
    ("사이다", "환타", "drink", "fanta"),
    ("환타", "사이다", "drink", "sprite"),
    ("감자튀김", "치즈스틱", "side", "cheese_stick"),
    ("치즈스틱", "감자튀김", "side", "french_fries"),
    ("불고기버거", "치킨버거", "burger", "chicken_burger"),
    ("치킨버거", "치즈버거", "burger", "cheese_burger"),
    ("치즈버거", "불고기버거", "burger", "bulgogi_burger"),
    ("불고기버거", "치즈버거", "burger", "cheese_burger"),
]


for old, new, domain, target in CORRECTIONS:

    for utterance in [
        f"{old}... 아 아니 {new} 주세요",
        f"{old} 하나 어 아니다 {new} 하나 주세요",
    ]:

        add_case(
            utterance,
            [
                act(
                    "order_action",
                    "add",
                    "correction",
                    "explicit",
                    target_domain=domain,
                    target=target,
                    quantity=(
                        1
                        if "하나" in utterance
                        else None
                    ),
                    reference=ref(
                        value=target,
                    ),
                )
            ],
            ["execute_order"],
            tag="stt_self_correction",
            reason="last_clear_correction_wins",
        )


# ============================================================
# E. CASUAL / SPOKEN KOREAN
# 20
# ============================================================

for label, domain, target in PRODUCTS:

    for utterance in [
        f"{label} 하나만 줘요",
        f"{label} 하나 부탁해요",
    ]:

        add_case(
            utterance,
            [
                act(
                    "order_action",
                    "add",
                    "request",
                    "explicit",
                    target_domain=domain,
                    target=target,
                    quantity=1,
                    reference=ref(
                        value=target,
                    ),
                )
            ],
            ["execute_order"],
            tag="stt_colloquial",
            reason="colloquial_explicit_request",
        )


# ============================================================
# F. PENDING + SHORT / CLIPPED RESPONSE
# 20
# ============================================================

PENDING_GROUPS = [
    (
        "drink",
        "drink",
        "음료를 선택해주세요.",
        [
            ("제로요", "zero_coke"),
            ("콜라요", "coke"),
            ("사이다로요", "sprite"),
            ("환타요", "fanta"),
            ("아메리카노로요", "iced_coffee"),
        ],
    ),
    (
        "side",
        "side",
        "사이드를 선택해주세요.",
        [
            ("치즈스틱요", "cheese_stick"),
            ("감튀요", "french_fries"),
            ("치즈 스틱으로요", "cheese_stick"),
            ("감자튀김으로", "french_fries"),
            ("그냥 감튀요", "french_fries"),
        ],
    ),
    (
        "drink_size",
        "drink",
        "음료 사이즈를 선택해주세요.",
        [
            ("라지요", "large"),
            ("미디움요", "medium"),
            ("큰 걸로요", "large"),
            ("보통으로요", "medium"),
            ("큰사이즈요", "large"),
        ],
    ),
    (
        "type",
        "burger",
        "단품과 세트 중 선택해주세요.",
        [
            ("세트요", "set"),
            ("단품요", "single"),
            ("세트로", "set"),
            ("단품으로", "single"),
            ("셋트요", "set"),
        ],
    ),
]


for field, domain, staff_text, responses in PENDING_GROUPS:

    for utterance, target in responses:

        add_case(
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
                        source="pending",
                        resolved=True,
                        value=target,
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
            tag="stt_pending_fragment",
            reason="short_response_resolved_by_pending",
        )


# ============================================================
# G. TOO BROKEN / AMBIGUOUS
# 20
# ============================================================

AMBIGUOUS = [
    "어 그거요",
    "아 그...",
    "그걸로 그...",
    "저거 그거요",
    "아니 그거 말고...",
    "어 잠깐 그 뭐지",
    "그거 하나... 아니",
    "콜라 아니 그...",
    "치즈 그 뭐였지",
    "버거 그거 있잖아요",
    "하나 주세요 그...",
    "아니 아니 잠깐",
    "그 첫 번째 아니...",
    "두 번째였나...",
    "네 아니 그게",
    "그거 말고 아...",
    "어 음 모르겠어요",
    "그냥 그...",
    "아니 다시 말할게요",
    "잠깐 뭐라고 했지",
]


for utterance in AMBIGUOUS:

    source = (
        "deictic"
        if "그" in utterance
        or "저거" in utterance
        else "none"
    )

    add_case(
        utterance,
        [
            act(
                "clarify",
                "clarification",
                (
                    "correction"
                    if "아니" in utterance
                    else "statement"
                ),
                "none",
                reference=ref(
                    source=source,
                    resolved=False,
                ),
                resolution="ambiguous",
            )
        ],
        ["clarify"],
        tag="stt_ambiguous",
        reason="insufficient_recoverable_stt_content",
    )


# ============================================================
# EXACT COUNT
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
                "expected":
                    item["expected"],

                "expected_policy":
                    item["expected_policy"],
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

        parsed = (
            RouterOutput.model_validate(
                item["expected"]
            )
        )

        decisions = (
            evaluate_router_output(
                parsed
            )
        )

        actual_policy = [
            x.status.value
            for x in decisions
        ]

        if (
            actual_policy
            !=
            item["expected_policy"]
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

for key in sorted(tags):

    print(
        f"{key:28s} "
        f"{tags[key]:3d}"
    )
