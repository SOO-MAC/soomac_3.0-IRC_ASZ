#!/usr/bin/env python3
"""
V14 repair dataset builder.

Base:
    dataset_v13

Adds to TRAIN only:
    - v14_mobile_pickup_replay        231
    - v14_multi_only_single_repair     36
    - v14_sparse_modify_repair         24
    - v14_reference_contrast_repair    48
                                      ----
                                       339

VAL/TEST:
    copied unchanged from V13.

Also creates:
    dataset_v14/real_blind_v14_test.jsonl  (12 fresh blind cases)

Purpose:
    Keep V13's balanced reference ability while repairing the four observed
    regression classes without another large reference-data expansion.

Run:
    cd ~/drive_thru_llm
    python3 make_dataset_v14.py
"""

import argparse
import copy
import json
import random
import shutil
import tempfile
from collections import Counter
from pathlib import Path

SEED = 114

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

EXPECTED_REPAIR_COUNTS = {
    "v14_mobile_pickup_replay": 231,
    "v14_multi_only_single_repair": 36,
    "v14_sparse_modify_repair": 24,
    "v14_reference_contrast_repair": 48,
}


def read_jsonl(path: Path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def message(row, role):
    return next(m["content"] for m in row["messages"] if m["role"] == role)


def get_utterance(row):
    return message(row, "user").split("현재 사용자 발화:\n", 1)[-1].strip()


def norm(text):
    return " ".join(str(text).split())


def state(items=None, recent=None):
    out = {
        "intent": "order" if items else "unknown",
        "order_id": None,
        "items": copy.deepcopy(items or []),
    }
    if recent is not None:
        out["recent_selection"] = list(recent)
    return out


def burger_item(
    line_id,
    menu,
    *,
    quantity=1,
    typ="single",
    exclude=None,
    toppings=None,
):
    return {
        "line_id": int(line_id),
        "item_type": "burger",
        "quantity": int(quantity),
        "menu": menu,
        "type": typ,
        "drink": None,
        "drink_size": None,
        "side": None,
        "exclude": list(exclude or []),
        "add_toppings": list(toppings or []),
    }


def add_burger(menu, quantity, typ="single"):
    return {
        "operation": "add",
        "item": {
            "item_type": "burger",
            "quantity": int(quantity),
            "menu": menu,
            "type": typ,
        },
    }


def modify(line_id, *, item=None, toppings_add=None):
    out = {
        "operation": "modify",
        "target": {"line_id": int(line_id)},
    }
    if item:
        out["item"] = copy.deepcopy(item)
    if toppings_add:
        out["toppings_add"] = list(toppings_add)
    return out


def update(*actions):
    return {
        "intent": "order",
        "actions": list(actions),
    }


def unknown():
    return {
        "intent": "unknown",
        "actions": [],
    }


def korean_number(n: int) -> str:
    """Sino-Korean cardinal without spaces for positive integers < 10000."""
    if not (0 < n < 10000):
        raise ValueError(n)

    digits = [
        (1000, "천"),
        (100, "백"),
        (10, "십"),
        (1, ""),
    ]
    nums = ["", "일", "이", "삼", "사", "오", "육", "칠", "팔", "구"]

    remain = n
    parts = []

    for unit, unit_word in digits:
        d, remain = divmod(remain, unit)
        if not d:
            continue

        if unit == 1:
            parts.append(nums[d])
        else:
            if d != 1:
                parts.append(nums[d])
            parts.append(unit_word)

    return "".join(parts)


def make_row(category, s, utterance, expected, system, OrderUpdate):
    user_content = (
        "현재 주문 상태:\n"
        + json.dumps(s, ensure_ascii=False, separators=(",", ":"))
        + "\n\n현재 확인 중인 항목:\n없음"
        + "\n\n현재 사용자 발화:\n"
        + norm(utterance)
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


def build_repairs(system, OrderUpdate):
    rows = []

    # ------------------------------------------------------------------
    # 1) mobile_pickup replay: exactly 231 = 77 order ids * 3 templates
    # ------------------------------------------------------------------
    for order_id in range(401, 478):
        kor = korean_number(order_id)

        texts = [
            f"픽업 주문번호 {kor}번이에요",
            f"모바일로 미리 주문한 {order_id}번 찾으러 왔어요",
            f"주문번호 {order_id}번 픽업하러 왔습니다",
        ]

        for text in texts:
            rows.append(
                make_row(
                    "v14_mobile_pickup_replay",
                    state(),
                    text,
                    {
                        "intent": "mobile_pickup",
                        "order_id": order_id,
                    },
                    system,
                    OrderUpdate,
                )
            )

    # ------------------------------------------------------------------
    # 2) "버거만" multi-order => each burger is explicit single
    # exactly 36 = C(4,2)=6 pairs * 3 qty pairs * 2 phrasings
    # ------------------------------------------------------------------
    menus = list(BURGER_NAMES)
    qty_pairs = [(2, 1), (1, 2), (3, 1)]

    for i in range(len(menus)):
        for j in range(i + 1, len(menus)):
            m1, m2 = menus[i], menus[j]
            w1, w2 = BURGER_NAMES[m1], BURGER_NAMES[m2]

            for q1, q2 in qty_pairs:
                texts = [
                    f"{w1}만 {q1}개, {w2}만 {q2}개 주문 목록에 넣어주세요",
                    f"{w1}만 {q1}개하고 {w2}만 {q2}개 주세요",
                ]

                expected = update(
                    add_burger(m1, q1, "single"),
                    add_burger(m2, q2, "single"),
                )

                for text in texts:
                    rows.append(
                        make_row(
                            "v14_multi_only_single_repair",
                            state(),
                            text,
                            expected,
                            system,
                            OrderUpdate,
                        )
                    )

    # ------------------------------------------------------------------
    # 3) sparse modify: menu+type only. DO NOT repeat item_type/quantity.
    # exactly 24 = 12 directed menu pairs * 2 phrasings
    # ------------------------------------------------------------------
    for src in menus:
        for dst in menus:
            if src == dst:
                continue

            src_word = BURGER_NAMES[src]
            dst_word = BURGER_NAMES[dst]

            s = state(
                [
                    burger_item(
                        1,
                        src,
                        quantity=2,
                        typ="single",
                    )
                ],
                [1],
            )

            expected = update(
                modify(
                    1,
                    item={
                        "menu": dst,
                        "type": "single",
                    },
                )
            )

            texts = [
                f"{src_word}를 {dst_word} 단품으로 바꿔주세요. 수량은 그대로 두세요",
                f"{src_word} 대신 {dst_word} 단품으로 교환하고 개수는 유지해주세요",
            ]

            for text in texts:
                rows.append(
                    make_row(
                        "v14_sparse_modify_repair",
                        s,
                        text,
                        expected,
                        system,
                        OrderUpdate,
                    )
                )

    # ------------------------------------------------------------------
    # 4) same utterance, contrasting state:
    #    attribute exists -> modify target
    #    attribute absent -> unknown
    # exactly 48 = 4 menus * 3 excludes * 2 toppings * 2 states
    # ------------------------------------------------------------------
    for menu, menu_word in BURGER_NAMES.items():
        for ex, ex_word in EXCLUDE_NAMES.items():
            for top, top_word in TOPPING_NAMES.items():
                text = f"{ex_word} 제외해 둔 {menu_word}에 {top_word} 추가해주세요"

                # Positive: matching item exists at line 3.
                positive_state = state(
                    [
                        burger_item(1, menu),
                        burger_item(2, menu),
                        burger_item(3, menu, exclude=[ex]),
                        burger_item(4, menu),
                    ],
                    [1, 2],
                )

                rows.append(
                    make_row(
                        "v14_reference_contrast_repair",
                        positive_state,
                        text,
                        update(modify(3, toppings_add=[top])),
                        system,
                        OrderUpdate,
                    )
                )

                # Negative: no item has the described excluded ingredient.
                negative_state = state(
                    [
                        burger_item(1, menu),
                        burger_item(2, menu),
                        burger_item(3, menu),
                        burger_item(4, menu),
                    ],
                    [1, 2],
                )

                rows.append(
                    make_row(
                        "v14_reference_contrast_repair",
                        negative_state,
                        text,
                        unknown(),
                        system,
                        OrderUpdate,
                    )
                )

    return rows


def build_blind(system, OrderUpdate):
    """12 fresh repair-only blind cases."""
    rows = []

    # mobile_pickup x3
    mobile_cases = [
        ("앱 주문번호 오백십삼번 픽업하러 왔어요", 513),
        ("미리 주문한 624번 찾으러 왔습니다", 624),
        ("모바일 주문 735번입니다", 735),
    ]
    for text, oid in mobile_cases:
        rows.append(
            make_row(
                "v14_blind_mobile_pickup",
                state(),
                text,
                {"intent": "mobile_pickup", "order_id": oid},
                system,
                OrderUpdate,
            )
        )

    # multi-only single x3
    multi_cases = [
        (
            "불고기버거만 두 개하고 새우버거만 한 개 주세요",
            [
                add_burger("bulgogi_burger", 2),
                add_burger("shrimp_burger", 1),
            ],
        ),
        (
            "치킨버거만 한 개, 치즈버거만 두 개 올려주세요",
            [
                add_burger("chicken_burger", 1),
                add_burger("cheese_burger", 2),
            ],
        ),
        (
            "새우버거만 세 개하고 불고기버거만 한 개 주문할게요",
            [
                add_burger("shrimp_burger", 3),
                add_burger("bulgogi_burger", 1),
            ],
        ),
    ]
    for text, actions in multi_cases:
        rows.append(
            make_row(
                "v14_blind_multi_only",
                state(),
                text,
                update(*actions),
                system,
                OrderUpdate,
            )
        )

    # sparse modify x3
    sparse_specs = [
        ("shrimp_burger", "cheese_burger"),
        ("bulgogi_burger", "chicken_burger"),
        ("cheese_burger", "shrimp_burger"),
    ]
    for src, dst in sparse_specs:
        s = state([burger_item(1, src, quantity=3)], [1])
        text = (
            f"{BURGER_NAMES[src]}는 {BURGER_NAMES[dst]} 단품으로만 바꾸고 "
            "개수는 건드리지 마세요"
        )
        rows.append(
            make_row(
                "v14_blind_sparse_modify",
                s,
                text,
                update(
                    modify(
                        1,
                        item={
                            "menu": dst,
                            "type": "single",
                        },
                    )
                ),
                system,
                OrderUpdate,
            )
        )

    # reference contrast x3: one positive, two negative
    positive = state(
        [
            burger_item(1, "chicken_burger"),
            burger_item(2, "chicken_burger", exclude=["pickle"]),
            burger_item(3, "chicken_burger"),
        ],
        [1, 3],
    )
    rows.append(
        make_row(
            "v14_blind_reference_contrast",
            positive,
            "피클 제외해 놓은 치킨버거에 토마토 추가해주세요",
            update(modify(2, toppings_add=["tomato"])),
            system,
            OrderUpdate,
        )
    )

    negative1 = state(
        [
            burger_item(1, "bulgogi_burger"),
            burger_item(2, "bulgogi_burger"),
        ],
        [1, 2],
    )
    rows.append(
        make_row(
            "v14_blind_reference_contrast",
            negative1,
            "양파 빼놓은 불고기버거에 베이컨 넣어주세요",
            unknown(),
            system,
            OrderUpdate,
        )
    )

    negative2 = state(
        [
            burger_item(1, "shrimp_burger"),
            burger_item(2, "shrimp_burger"),
            burger_item(3, "shrimp_burger"),
        ],
        [2, 3],
    )
    rows.append(
        make_row(
            "v14_blind_reference_contrast",
            negative2,
            "양상추 빼놓은 새우버거만 골라서 토마토 넣어주세요",
            unknown(),
            system,
            OrderUpdate,
        )
    )

    if len(rows) != 12:
        raise AssertionError(len(rows))

    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
    )
    args = parser.parse_args()

    root = args.base_dir.resolve()
    src_dir = root / "dataset_v13"
    out_dir = root / "dataset_v14"

    if out_dir.exists():
        raise SystemExit(
            "dataset_v14이 이미 있습니다. 안전을 위해 덮어쓰지 않습니다."
        )

    from order_update_schema import OrderUpdate

    required = {
        split: src_dir / f"{split}.jsonl"
        for split in ("train", "val", "test")
    }
    required["v13_blind"] = src_dir / "real_blind_v13_test.jsonl"

    for name, path in required.items():
        if not path.is_file():
            raise FileNotFoundError(f"{name} 없음: {path}")

    base = {
        split: read_jsonl(required[split])
        for split in ("train", "val", "test")
    }

    system = message(base["train"][0], "system")

    repair_rows = build_repairs(system, OrderUpdate)
    blind_rows = build_blind(system, OrderUpdate)

    # Exact category counts must match the planned repair budget.
    actual_counts = Counter(row["category"] for row in repair_rows)
    if dict(actual_counts) != EXPECTED_REPAIR_COUNTS:
        raise ValueError(
            f"repair count mismatch\nexpected={EXPECTED_REPAIR_COUNTS}\n"
            f"actual={dict(actual_counts)}"
        )

    if len(repair_rows) != 339:
        raise ValueError(f"repair total != 339: {len(repair_rows)}")

    # Protect all known blind utterances from exact leakage.
    blind_paths = [
        root / "dataset/real_blind_test.jsonl",
        root / "dataset/real_blind_v2_test.jsonl",
        root / "dataset/real_blind_v3_test.jsonl",
        root / "dataset_v7/real_blind_v7_test.jsonl",
        root / "dataset_v8/real_blind_v8_test.jsonl",
        root / "dataset_v9/real_blind_v9_test.jsonl",
        root / "dataset_v11/real_blind_v11_test.jsonl",
        root / "dataset_v12/real_blind_v12_test.jsonl",
        root / "dataset_v13/real_blind_v13_test.jsonl",
    ]

    protected_utterances = set()
    for path in blind_paths:
        if path.is_file():
            protected_utterances.update(
                norm(get_utterance(row))
                for row in read_jsonl(path)
            )

    protected_utterances.update(
        norm(get_utterance(row))
        for row in blind_rows
    )

    leaked = [
        get_utterance(row)
        for row in repair_rows
        if norm(get_utterance(row)) in protected_utterances
    ]
    if leaked:
        raise ValueError(
            "blind utterance exact leakage detected:\n"
            + "\n".join(leaked[:20])
        )

    # Validate every new target through schema.
    for row in repair_rows + blind_rows:
        OrderUpdate.model_validate_json(message(row, "assistant"))

    # Full user input must remain unique. Same utterance with different state is
    # intentionally allowed for the positive/negative contrast pairs.
    all_existing_inputs = {
        message(row, "user")
        for split in ("train", "val", "test")
        for row in base[split]
    }

    seen_new_inputs = set()
    for row in repair_rows:
        user_input = message(row, "user")
        if user_input in all_existing_inputs:
            raise ValueError(
                "V13과 동일한 full user input 발견: "
                + get_utterance(row)
            )
        if user_input in seen_new_inputs:
            raise ValueError(
                "repair 내부 동일 full user input 발견: "
                + get_utterance(row)
            )
        seen_new_inputs.add(user_input)

    rng = random.Random(SEED)

    train = [copy.deepcopy(row) for row in base["train"]]
    train.extend(repair_rows)
    rng.shuffle(train)

    val = [copy.deepcopy(row) for row in base["val"]]
    test = [copy.deepcopy(row) for row in base["test"]]

    tmp = Path(tempfile.mkdtemp(prefix=".dataset_v14_", dir=root))

    try:
        for split, rows in (
            ("train", train),
            ("val", val),
            ("test", test),
        ):
            with (tmp / f"{split}.jsonl").open(
                "w", encoding="utf-8"
            ) as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")

        with (tmp / "real_blind_v14_test.jsonl").open(
            "w", encoding="utf-8"
        ) as f:
            for row in blind_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

        report = {
            "base": "dataset_v13",
            "seed": SEED,
            "base_counts": {
                split: len(base[split])
                for split in ("train", "val", "test")
            },
            "final_counts": {
                "train": len(train),
                "val": len(val),
                "test": len(test),
            },
            "repair_total": len(repair_rows),
            "repair_counts": dict(actual_counts),
            "blind_v14": len(blind_rows),
            "notes": [
                "V13 reference augmentation kept intact.",
                "339 repair rows added to TRAIN only.",
                "VAL/TEST copied unchanged from V13.",
                "Known blind utterances protected from exact leakage.",
                "Positive/negative reference contrast intentionally uses same utterance with different state.",
            ],
        }

        (tmp / "build_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        tmp.rename(out_dir)

    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    print("=" * 76)
    print("V14 REPAIR 데이터 생성 완료 (학습은 실행하지 않음)")
    print("=" * 76)
    print(
        f"train: {len(train)} "
        f"(V13 {len(base['train'])} + repair {len(repair_rows)})"
    )
    print(f"val  : {len(val)} (V13 그대로)")
    print(f"test : {len(test)} (V13 그대로)")

    print("\n[repair category]")
    for category, expected_n in EXPECTED_REPAIR_COUNTS.items():
        print(f"- {category}: {actual_counts[category]}")

    print(f"\nV14 신규 blind: {len(blind_rows)}")
    print("출력:", out_dir)
    print("\n다음: python3 train_qlora_v14.py --inspect-only")


if __name__ == "__main__":
    main()
