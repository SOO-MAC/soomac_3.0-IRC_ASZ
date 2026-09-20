#!/usr/bin/env python3
"""
V14 runtime semantic-grounding smoke tests.
No vLLM server call is made.

Checks:
1) standalone drink: size not spoken -> hallucinated size removed
2) standalone drink: explicit size -> preserved
3) set drink: size not spoken -> hallucinated size removed
4) pending drink_size: explicit size can be filled back in
"""

from order_update_schema import OrderUpdate
from order_runtime_final import verify_semantics


def verified(utterance, state, pending, payload):
    update = OrderUpdate.model_validate(payload)
    out, warnings = verify_semantics(utterance, state, pending, update)
    return out.model_dump(mode="json", exclude_none=True), warnings


def item_of(result):
    return result["actions"][0]["item"]


def main():
    result, _ = verified(
        "아이스 아메리카노 한 잔 넣어주세요",
        {"intent": "unknown", "order_id": None, "items": []},
        None,
        {
            "intent": "order",
            "actions": [{
                "operation": "add",
                "item": {
                    "item_type": "drink",
                    "quantity": 1,
                    "drink": "iced_coffee",
                    "drink_size": "medium",
                },
            }],
        },
    )
    assert "drink_size" not in item_of(result), result

    result, _ = verified(
        "아이스 아메리카노 미디엄 한 잔 넣어주세요",
        {"intent": "unknown", "order_id": None, "items": []},
        None,
        {
            "intent": "order",
            "actions": [{
                "operation": "add",
                "item": {
                    "item_type": "drink",
                    "quantity": 1,
                    "drink": "iced_coffee",
                    "drink_size": "medium",
                },
            }],
        },
    )
    assert item_of(result).get("drink_size") == "medium", result

    result, _ = verified(
        "불고기버거 세트 하나에 콜라 주세요",
        {"intent": "unknown", "order_id": None, "items": []},
        None,
        {
            "intent": "order",
            "actions": [{
                "operation": "add",
                "item": {
                    "item_type": "burger",
                    "quantity": 1,
                    "menu": "bulgogi_burger",
                    "type": "set",
                    "drink": "coke",
                    "drink_size": "large",
                },
            }],
        },
    )
    assert "drink_size" not in item_of(result), result

    state = {
        "intent": "order",
        "order_id": None,
        "items": [{
            "line_id": 1,
            "item_type": "drink",
            "quantity": 1,
            "menu": None,
            "type": None,
            "drink": "coke",
            "drink_size": None,
            "side": None,
            "exclude": [],
            "add_toppings": [],
        }],
    }
    result, _ = verified(
        "라지로 해주세요",
        state,
        (1, "drink_size"),
        {
            "intent": "order",
            "actions": [{
                "operation": "modify",
                "target": {"line_id": 1},
                "item": {},
            }],
        },
    )
    assert item_of(result).get("drink_size") == "large", result

    print("V14 runtime grounding smoke: 4/4 PASS")


if __name__ == "__main__":
    main()
