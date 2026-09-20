import copy

import order_runtime_final as rt


def make_runtime_with_fake_llm(fake_update):
    runtime = rt.DriveThruRuntime()

    def fake_parse(state, pending, utterance):
        return rt.OrderUpdate.model_validate(copy.deepcopy(fake_update))

    runtime.parser.parse = fake_parse
    return runtime


def test_negative_reference():
    """
    Known model failure #1

    State:
      line 1 = bulgogi burger
      onion은 제외되어 있지 않음

    User:
      "양파 빼놓은 불고기버거에 베이컨 넣어주세요"

    Bad LLM:
      line 1에 onion exclusion + bacon을 임의로 추가

    Expected Runtime:
      존재하지 않는 '양파 뺀 버거' 참조이므로
      주문 상태를 변경하지 않아야 한다.
    """

    bad_llm = {
        "intent": "order",
        "actions": [
            {
                "operation": "modify",
                "target": {
                    "line_id": 1
                },
                "exclude_add": ["onion"],
                "toppings_add": ["bacon"]
            }
        ]
    }

    runtime = make_runtime_with_fake_llm(bad_llm)

    runtime.manager.state = {
        "intent": "order",
        "order_id": None,
        "items": [
            {
                "line_id": 1,
                "item_type": "burger",
                "quantity": 1,
                "menu": "bulgogi_burger",
                "type": "single",
                "drink": None,
                "drink_size": None,
                "side": None,
                "exclude": [],
                "add_toppings": []
            }
        ]
    }

    runtime.manager.pending = runtime.manager.compute_pending()

    before = copy.deepcopy(runtime.manager.state)

    runtime.process(
        "양파 빼놓은 불고기버거에 베이컨 넣어주세요"
    )

    after = runtime.manager.state

    assert after == before, (
        "\n[FAIL] nonexistent reference modified state"
        f"\nBEFORE={before}"
        f"\nAFTER ={after}"
    )


def test_hallucinated_drink_size():
    """
    Known model failure #2

    User:
      "아이스 아메리카노도 한 잔 넣어주세요"

    Bad LLM:
      사용자가 말하지 않은 medium을 생성

    Expected Runtime:
      medium 제거
      drink_size = None
      pending = (line_id, 'drink_size')
    """

    bad_llm = {
        "intent": "order",
        "actions": [
            {
                "operation": "add",
                "item": {
                    "item_type": "drink",
                    "quantity": 1,
                    "drink": "iced_coffee",
                    "drink_size": "medium"
                }
            }
        ]
    }

    runtime = make_runtime_with_fake_llm(bad_llm)

    runtime.process(
        "아이스 아메리카노도 한 잔 넣어주세요"
    )

    items = runtime.manager.state["items"]

    assert len(items) == 1, (
        f"expected 1 item, got {len(items)}"
    )

    item = items[0]

    assert item["drink"] == "iced_coffee"

    assert item.get("drink_size") is None, (
        f"hallucinated size survived runtime: "
        f"{item.get('drink_size')}"
    )

    pending = runtime.manager.pending

    assert pending is not None, (
        "drink_size should remain pending"
    )

    assert pending[1] == "drink_size", (
        f"expected drink_size pending, got {pending}"
    )


if __name__ == "__main__":
    tests = [
        ("negative reference grounding", test_negative_reference),
        ("hallucinated drink size", test_hallucinated_drink_size),
    ]

    passed = 0

    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"PASS: {name}")
        except Exception as e:
            print(f"FAIL: {name}")
            print(e)

    print()
    print(
        f"V14 known model failures runtime guard: "
        f"{passed}/{len(tests)} PASS"
    )

    if passed != len(tests):
        raise SystemExit(1)
