#!/usr/bin/env python3

import json
from pathlib import Path

from router_schema import RouterOutput
from router_policy import evaluate_router_output


OUT = (
    Path(__file__).parent
    / "router_data/gold/router_gold_correction_v1.jsonl"
)


def ref(source="explicit", resolved=True, value=None, line_ids=None):
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
        "reference": reference if reference is not None else ref(
            source="none",
            resolved=False,
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
            f"X{counter:03d}",
            *args,
            **kwargs,
        )
    )

    counter += 1


# ============================================================
# A. SAME-SLOT SELF CORRECTION
# 10 targets * 3 = 30
# ============================================================

corrections = [
    ("콜라", "제로콜라", "drink", "zero_coke"),
    ("제로콜라", "콜라", "drink", "coke"),
    ("사이다", "환타", "drink", "fanta"),
    ("환타", "사이다", "drink", "sprite"),
    ("감자튀김", "치즈스틱", "side", "cheese_stick"),
    ("치즈스틱", "감자튀김", "side", "french_fries"),
    ("불고기버거", "치킨버거", "burger", "chicken_burger"),
    ("치킨버거", "치즈버거", "burger", "cheese_burger"),
    ("치즈버거", "새우버거", "burger", "shrimp_burger"),
    ("새우버거", "불고기버거", "burger", "bulgogi_burger"),
]

for old, new, domain, target in corrections:

    texts = [
        f"{old} 주세요 아니 {new} 주세요",
        f"{old} 하나... 아 아니다 {new} 하나 주세요",
        f"{old} 말고 {new} 주세요",
    ]

    for text in texts:

        add_case(
            text,
            [
                act(
                    "order_action",
                    "add",
                    "correction",
                    "explicit",
                    target_domain=domain,
                    target=target,
                    quantity=1 if "하나" in text else None,
                    reference=ref(
                        value=target,
                    ),
                )
            ],
            ["execute_order"],
            tag="self_correction_target",
            reason="last_valid_same_slot_correction",
        )


# ============================================================
# B. QUANTITY CORRECTION
# 5 targets * 3 = 15
# ============================================================

quantity_specs = [
    ("치즈스틱", "side", "cheese_stick", "개"),
    ("감자튀김", "side", "french_fries", "개"),
    ("콜라", "drink", "coke", "잔"),
    ("제로콜라", "drink", "zero_coke", "잔"),
    ("불고기버거", "burger", "bulgogi_burger", "개"),
]

for label, domain, target, unit in quantity_specs:

    rows = [
        (f"{label} 두 {unit} 아니 세 {unit} 주세요", 3),
        (f"{label} 한 {unit} 말고 네 {unit} 주세요", 4),
        (f"{label} 5{unit} 주세요 아 아니다 2{unit} 주세요", 2),
    ]

    for text, qty in rows:

        add_case(
            text,
            [
                act(
                    "order_action",
                    "add",
                    "correction",
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
            tag="self_correction_quantity",
            reason="last_valid_quantity_wins",
        )


# ============================================================
# C. NEGATION / DO-NOT-ACT
# 20
# ============================================================

negative_targets = [
    ("치즈스틱", "side", "cheese_stick"),
    ("감자튀김", "side", "french_fries"),
    ("콜라", "drink", "coke"),
    ("제로콜라", "drink", "zero_coke"),
    ("불고기버거", "burger", "bulgogi_burger"),
]

for label, domain, target in negative_targets:

    for text in [
        f"{label} 추가하지 마세요",
        f"{label}는 넣지 말아주세요",
    ]:

        add_case(
            text,
            [
                act(
                    "no_action",
                    "deny",
                    "deny",
                    "none",
                    target_domain=domain,
                    target=target,
                    reference=ref(
                        value=target,
                    ),
                )
            ],
            ["no_action"],
            tag="negation",
            reason="explicit_negative_request",
        )


for label, domain, target in negative_targets:

    staff = f"{label} 추가해드릴까요?"

    for text in [
        "아니요",
        "아뇨 추가하지 마세요",
    ]:

        add_case(
            text,
            [
                act(
                    "no_action",
                    "deny",
                    "deny",
                    "none",
                    target_domain=domain,
                    target=target,
                    reference=ref(
                        source="previous_staff",
                        resolved=True,
                        value=target,
                    ),
                    resolution="context_resolved",
                )
            ],
            ["no_action"],
            history=[
                {
                    "role": "staff",
                    "text": staff,
                }
            ],
            tag="negation",
            reason="deny_previous_proposal",
        )


# ============================================================
# D. REPLACEMENT / "말고, 대신"
# 10 specs * 2 = 20
# ============================================================

replacement_specs = [
    (
        "콜라",
        "제로콜라",
        "drink",
        "zero_coke",
        {"line_id": 1, "item_type": "drink", "drink": "coke"},
    ),
    (
        "제로콜라",
        "콜라",
        "drink",
        "coke",
        {"line_id": 1, "item_type": "drink", "drink": "zero_coke"},
    ),
    (
        "감자튀김",
        "치즈스틱",
        "side",
        "cheese_stick",
        {"line_id": 1, "item_type": "side", "side": "french_fries"},
    ),
    (
        "치즈스틱",
        "감자튀김",
        "side",
        "french_fries",
        {"line_id": 1, "item_type": "side", "side": "cheese_stick"},
    ),
    (
        "불고기버거",
        "치킨버거",
        "burger",
        "chicken_burger",
        {"line_id": 1, "item_type": "burger", "menu": "bulgogi_burger"},
    ),
    (
        "치킨버거",
        "치즈버거",
        "burger",
        "cheese_burger",
        {"line_id": 1, "item_type": "burger", "menu": "chicken_burger"},
    ),
    (
        "치즈버거",
        "새우버거",
        "burger",
        "shrimp_burger",
        {"line_id": 1, "item_type": "burger", "menu": "cheese_burger"},
    ),
    (
        "새우버거",
        "불고기버거",
        "burger",
        "bulgogi_burger",
        {"line_id": 1, "item_type": "burger", "menu": "shrimp_burger"},
    ),
    (
        "사이다",
        "환타",
        "drink",
        "fanta",
        {"line_id": 1, "item_type": "drink", "drink": "sprite"},
    ),
    (
        "환타",
        "사이다",
        "drink",
        "sprite",
        {"line_id": 1, "item_type": "drink", "drink": "fanta"},
    ),
]

for old, new, domain, target, item in replacement_specs:

    for text in [
        f"{old} 말고 {new}로 바꿔주세요",
        f"{old} 대신 {new}로 변경해주세요",
    ]:

        add_case(
            text,
            [
                act(
                    "order_action",
                    "modify",
                    "correction",
                    "explicit",
                    target_domain=domain,
                    target=target,
                    reference=ref(
                        source="current_order",
                        resolved=True,
                        value=target,
                        line_ids=[1],
                    ),
                    resolution="context_resolved",
                )
            ],
            ["execute_order"],
            items=[item],
            tag="replacement",
            reason="replacement_target_explicit",
        )


# ============================================================
# E. PARALLEL MULTI-ACTION
# 10 pairs * 2 = 20
# ============================================================

parallel_specs = [
    (
        ("불고기버거", "burger", "bulgogi_burger", 1),
        ("치즈스틱", "side", "cheese_stick", 2),
    ),
    (
        ("치킨버거", "burger", "chicken_burger", 1),
        ("콜라", "drink", "coke", 1),
    ),
    (
        ("치즈버거", "burger", "cheese_burger", 1),
        ("제로콜라", "drink", "zero_coke", 2),
    ),
    (
        ("새우버거", "burger", "shrimp_burger", 2),
        ("감자튀김", "side", "french_fries", 1),
    ),
    (
        ("콜라", "drink", "coke", 1),
        ("사이다", "drink", "sprite", 1),
    ),
    (
        ("제로콜라", "drink", "zero_coke", 1),
        ("환타", "drink", "fanta", 1),
    ),
    (
        ("감자튀김", "side", "french_fries", 2),
        ("치즈스틱", "side", "cheese_stick", 2),
    ),
    (
        ("불고기버거", "burger", "bulgogi_burger", 2),
        ("치킨버거", "burger", "chicken_burger", 1),
    ),
    (
        ("치즈버거", "burger", "cheese_burger", 1),
        ("아이스커피", "drink", "iced_coffee", 1),
    ),
    (
        ("새우버거", "burger", "shrimp_burger", 1),
        ("치즈스틱", "side", "cheese_stick", 1),
    ),
]

for first, second in parallel_specs:

    l1, d1, t1, q1 = first
    l2, d2, t2, q2 = second

    for text in [
        f"{l1} {q1}개랑 {l2} {q2}개 주세요",
        f"{l1} {q1}개 주세요 그리고 {l2} {q2}개도 주세요",
    ]:

        add_case(
            text,
            [
                act(
                    "order_action",
                    "add",
                    "request",
                    "explicit",
                    target_domain=d1,
                    target=t1,
                    quantity=q1,
                    reference=ref(value=t1),
                ),
                act(
                    "order_action",
                    "add",
                    "request",
                    "explicit",
                    target_domain=d2,
                    target=t2,
                    quantity=q2,
                    reference=ref(value=t2),
                ),
            ],
            [
                "execute_order",
                "execute_order",
            ],
            tag="parallel_multi_action",
            reason="independent_requests_both_preserved",
        )


# ============================================================
# F. QUERY + ACTION BOTH REMAIN ACTIVE
# 10 targets * 2 = 20
# ============================================================

query_action_specs = [
    ("치즈스틱", "side", "cheese_stick", 2),
    ("감자튀김", "side", "french_fries", 3),
    ("콜라", "drink", "coke", 2),
    ("제로콜라", "drink", "zero_coke", 2),
    ("사이다", "drink", "sprite", 1),
    ("환타", "drink", "fanta", 1),
    ("아이스커피", "drink", "iced_coffee", 2),
    ("불고기버거", "burger", "bulgogi_burger", 2),
    ("치킨버거", "burger", "chicken_burger", 1),
    ("새우버거", "burger", "shrimp_burger", 1),
]

for label, domain, target, qty in query_action_specs:

    texts = [
        f"{label} 가격도 알려주시고 {qty}개 주세요",
        f"{label} 얼마인지 알려주세요 그리고 {qty}개 주문할게요",
    ]

    for text in texts:

        add_case(
            text,
            [
                act(
                    "info_query",
                    "price",
                    "question",
                    "none",
                    target_domain=domain,
                    target=target,
                    quantity=qty,
                    reference=ref(value=target),
                ),
                act(
                    "order_action",
                    "add",
                    "request",
                    "explicit",
                    target_domain=domain,
                    target=target,
                    quantity=qty,
                    reference=ref(value=target),
                ),
            ],
            [
                "read_only",
                "execute_order",
            ],
            tag="query_plus_action",
            reason="independent_query_and_order_both_requested",
        )


# ============================================================
# G. ACTION THEN EXPLICIT RETRACTION
# 5 scenarios * 3 = 15
# ============================================================

retractions = [
    (
        "치즈버거 주세요",
        "치즈버거",
        "burger",
        "cheese_burger",
    ),
    (
        "치즈스틱 하나 주세요",
        "치즈스틱",
        "side",
        "cheese_stick",
    ),
    (
        "제로콜라 주세요",
        "제로콜라",
        "drink",
        "zero_coke",
    ),
    (
        "불고기버거 주세요",
        "불고기버거",
        "burger",
        "bulgogi_burger",
    ),
    (
        "콜라 주세요",
        "콜라",
        "drink",
        "coke",
    ),
]

for initial, label, domain, target in retractions:

    texts = [
        f"{initial} 아 아니다 아직 주문하지 마세요",
        f"{initial} 아니 잠깐 취소할게요",
        f"{initial} 아니요 그건 넣지 마세요",
    ]

    for text in texts:

        add_case(
            text,
            [
                act(
                    "no_action",
                    "deny",
                    "correction",
                    "none",
                    target_domain=domain,
                    target=target,
                    reference=ref(
                        value=target,
                    ),
                )
            ],
            ["no_action"],
            tag="retraction",
            reason="later_explicit_retraction_cancels_prior_action",
        )


# ============================================================
# H. CONDITIONAL MULTI-ACT
# 5 targets * 2 = 10
# ============================================================

conditional_specs = [
    ("제로콜라", "drink", "zero_coke", 2, "stock"),
    ("치즈스틱", "side", "cheese_stick", 3, "stock"),
    ("새우버거", "burger", "shrimp_burger", 1, "stock"),
    ("아이스커피", "drink", "iced_coffee", 2, "possibility"),
    ("감자튀김", "side", "french_fries", 5, "possibility"),
]

for label, domain, target, qty, kind in conditional_specs:

    if kind == "stock":
        texts = [
            f"{label} 있으면 {qty}개 주세요",
            f"{label} 재고 있으면 {qty}개 주문할게요",
        ]
        subtype = "stock"
        condition_type = "stock_available"

    else:
        texts = [
            f"{label} 가능하면 {qty}개 주세요",
            f"{label} 주문 가능하면 {qty}개 주문할게요",
        ]
        subtype = "possibility"
        condition_type = "possible"

    for text in texts:

        add_case(
            text,
            [
                act(
                    "info_query",
                    subtype,
                    "question",
                    "none",
                    target_domain=domain,
                    target=target,
                    reference=ref(value=target),
                ),
                act(
                    "order_action",
                    "add",
                    "request",
                    "conditional",
                    target_domain=domain,
                    target=target,
                    quantity=qty,
                    reference=ref(value=target),
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
            tag="conditional_multi_action",
            reason="condition_must_succeed_before_order",
        )


# ============================================================
# EXACT COUNT
# ============================================================

if len(cases) != 150:
    raise RuntimeError(
        f"expected 150 cases, got {len(cases)}"
    )


# ============================================================
# WRITE / DUPLICATE / POLICY VALIDATION
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

        actual = [
            x.status.value
            for x in decisions
        ]

        if actual != item["expected_policy"]:

            raise ValueError(
                f"{item['id']} policy mismatch: "
                f"{actual} != "
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

print(f"[OK] {OUT}")


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
        f"{key:30s} {tags[key]:3d}"
    )
