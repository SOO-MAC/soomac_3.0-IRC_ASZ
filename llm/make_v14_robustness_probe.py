#!/usr/bin/env python3
"""
Build a V14 robustness-only blind probe.

Purpose:
1) state-grounded attribute reference:
   - same utterance, matching excluded attribute exists -> modify exact target
   - same utterance, matching attribute absent -> unknown
2) optional drink-size grounding:
   - no size spoken -> do NOT invent drink_size
   - explicit size spoken -> include only the spoken size

This file DOES NOT modify train/val/test and must never be merged into training data
before V14 is evaluated on it.
"""

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATASET_V14 = BASE_DIR / "dataset_v14"
OUT = DATASET_V14 / "robustness_probe_v14.jsonl"

from order_update_schema import OrderUpdate

BURGER_NAMES = {
    "bulgogi_burger": "불고기버거",
    "chicken_burger": "치킨버거",
    "cheese_burger": "치즈버거",
    "shrimp_burger": "새우버거",
}
EXCLUDE_NAMES = {
    "pickle": "피클",
    "onion": "양파",
    "lettuce": "양상추",
}
TOPPING_NAMES = {
    "bacon": "베이컨",
    "tomato": "토마토",
}


def read_jsonl(path):
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def get_system_prompt():
    train = read_jsonl(DATASET_V14 / "train.jsonl")
    if not train:
        raise RuntimeError("dataset_v14/train.jsonl is empty")
    return next(
        m["content"]
        for m in train[0]["messages"]
        if m["role"] == "system"
    )


def state(items=None, recent=None):
    out = {
        "intent": "order" if items else "unknown",
        "order_id": None,
        "items": list(items or []),
    }
    if recent is not None:
        out["recent_selection"] = list(recent)
    return out


def burger_item(line_id, menu, exclude=None):
    return {
        "line_id": int(line_id),
        "item_type": "burger",
        "quantity": 1,
        "menu": menu,
        "type": "single",
        "drink": None,
        "drink_size": None,
        "side": None,
        "exclude": list(exclude or []),
        "add_toppings": [],
    }


def order_update(*actions):
    return {"intent": "order", "actions": list(actions)}


def modify(line_id, *, toppings_add=None):
    out = {
        "operation": "modify",
        "target": {"line_id": int(line_id)},
    }
    if toppings_add:
        out["toppings_add"] = list(toppings_add)
    return out


def add_drink(drink, quantity=1, size=None):
    item = {
        "item_type": "drink",
        "quantity": int(quantity),
        "drink": drink,
    }
    if size is not None:
        item["drink_size"] = size
    return {
        "operation": "add",
        "item": item,
    }


def unknown():
    return {"intent": "unknown", "actions": []}


def make_row(category, s, utterance, expected, system):
    user_content = (
        "현재 주문 상태:\n"
        + json.dumps(s, ensure_ascii=False, separators=(",", ":"))
        + "\n\n현재 확인 중인 항목:\n없음"
        + "\n\n현재 사용자 발화:\n"
        + " ".join(utterance.split())
    )

    parsed = OrderUpdate.model_validate(expected)
    return {
        "category": category,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
            {
                "role": "assistant",
                "content": parsed.model_dump_json(
                    exclude_none=True,
                    exclude_defaults=True,
                ),
            },
        ],
    }


def build_reference_probe(system):
    """
    20 cases = 10 positive/negative contrast pairs.
    Same utterance in each pair; only state changes.
    """
    combos = [
        ("bulgogi_burger", "pickle", "bacon", 4,
         "피클 없이 해놨던 불고기버거 골라서 베이컨만 넣어주세요"),
        ("bulgogi_burger", "onion", "tomato", 3,
         "양파를 빼놨던 불고기버거 찾아서 토마토 추가해주세요"),
        ("chicken_burger", "lettuce", "bacon", 2,
         "양상추 없이 만들어 둔 치킨버거에 베이컨 넣어주세요"),
        ("chicken_burger", "pickle", "tomato", 4,
         "피클을 빼둔 치킨버거만 골라 토마토를 더해주세요"),
        ("cheese_burger", "onion", "bacon", 2,
         "양파 제외해 놨던 치즈버거 쪽에 베이컨만 추가해주세요"),
        ("cheese_burger", "lettuce", "tomato", 3,
         "양상추 없는 상태로 둔 치즈버거에 토마토 넣어주세요"),
        ("shrimp_burger", "pickle", "bacon", 3,
         "피클 빼놓고 둔 새우버거를 찾아 베이컨 추가해주세요"),
        ("shrimp_burger", "onion", "tomato", 4,
         "양파 없이 해놓은 새우버거에 토마토만 더해주세요"),
        ("bulgogi_burger", "lettuce", "bacon", 2,
         "양상추 빼서 만들어 둔 불고기버거에 베이컨 넣어주세요"),
        ("shrimp_burger", "lettuce", "tomato", 2,
         "양상추를 빼둔 새우버거만 찾아서 토마토 추가해주세요"),
    ]

    rows = []
    for menu, ex, top, target, text in combos:
        # recent_selection is deliberately a distractor and does not contain target.
        distractors = [i for i in (1, 2, 3, 4) if i != target][:2]

        positive_items = [
            burger_item(i, menu, exclude=[ex] if i == target else [])
            for i in (1, 2, 3, 4)
        ]
        negative_items = [
            burger_item(i, menu)
            for i in (1, 2, 3, 4)
        ]

        rows.append(
            make_row(
                "v14_probe_reference_positive",
                state(positive_items, distractors),
                text,
                order_update(modify(target, toppings_add=[top])),
                system,
            )
        )
        rows.append(
            make_row(
                "v14_probe_reference_negative",
                state(negative_items, distractors),
                text,
                unknown(),
                system,
            )
        )

    return rows


def build_drink_probe(system):
    """
    20 cases:
    - 10 no-size: expected output must omit drink_size
    - 10 explicit-size controls
    """
    no_size = [
        ("아이스 아메리카노 한 잔 추가해주세요", "iced_coffee", 1),
        ("아이스커피 두 잔 더 넣어주세요", "iced_coffee", 2),
        ("콜라 한 잔 주세요", "coke", 1),
        ("콜라 두 잔 추가할게요", "coke", 2),
        ("제로콜라 한 잔도 주세요", "zero_coke", 1),
        ("제로콜라 두 잔 넣어주세요", "zero_coke", 2),
        ("사이다 한 잔 추가해주세요", "sprite", 1),
        ("사이다 두 잔 부탁드려요", "sprite", 2),
        ("환타 한 잔도 넣어주세요", "fanta", 1),
        ("환타 두 잔 추가해주세요", "fanta", 2),
    ]

    explicit = [
        ("아이스 아메리카노 스몰 한 잔 추가해주세요", "iced_coffee", 1, "small"),
        ("아이스커피 라지 두 잔 넣어주세요", "iced_coffee", 2, "large"),
        ("콜라 미디엄 한 잔 주세요", "coke", 1, "medium"),
        ("콜라 라지 두 잔 추가할게요", "coke", 2, "large"),
        ("제로콜라 스몰 한 잔도 주세요", "zero_coke", 1, "small"),
        ("제로콜라 미디엄 두 잔 넣어주세요", "zero_coke", 2, "medium"),
        ("사이다 라지 한 잔 추가해주세요", "sprite", 1, "large"),
        ("사이다 스몰 두 잔 부탁드려요", "sprite", 2, "small"),
        ("환타 미디엄 한 잔도 넣어주세요", "fanta", 1, "medium"),
        ("환타 라지 두 잔 추가해주세요", "fanta", 2, "large"),
    ]

    rows = []
    for text, drink, qty in no_size:
        rows.append(
            make_row(
                "v14_probe_drink_no_size",
                state(),
                text,
                order_update(add_drink(drink, qty)),
                system,
            )
        )

    for text, drink, qty, size in explicit:
        rows.append(
            make_row(
                "v14_probe_drink_explicit_size",
                state(),
                text,
                order_update(add_drink(drink, qty, size)),
                system,
            )
        )

    return rows


def main():
    system = get_system_prompt()
    rows = build_reference_probe(system) + build_drink_probe(system)

    if len(rows) != 40:
        raise RuntimeError(f"expected 40 probe cases, got {len(rows)}")

    # Schema validation has already happened in make_row.
    OUT.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )

    from collections import Counter
    counts = Counter(r["category"] for r in rows)

    print("=" * 72)
    print("V14 ROBUSTNESS PROBE CREATED")
    print("=" * 72)
    print(f"output: {OUT}")
    print(f"total : {len(rows)}")
    for k, v in sorted(counts.items()):
        print(f"  {k:36s} {v:3d}")
    print("\nIMPORTANT: probe file is evaluation-only. Do not add it to training.")


if __name__ == "__main__":
    main()
