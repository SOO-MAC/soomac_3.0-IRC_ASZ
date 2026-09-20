#!/usr/bin/env python3
import sys
import types

if "openai" not in sys.modules:
    fake = types.ModuleType("openai")
    class OpenAI:
        def __init__(self, *args, **kwargs):
            pass
    fake.OpenAI = OpenAI
    sys.modules["openai"] = fake

from order_update_schema import OrderUpdate
from order_runtime_final import (
    repair_pending_explicit_field,
    repair_pending_burger_modifiers,
)


def burger_state():
    return {
        "intent": "order",
        "order_id": None,
        "items": [{
            "line_id": 1,
            "item_type": "burger",
            "quantity": 1,
            "menu": "bulgogi_burger",
            "type": None,
            "drink": None,
            "drink_size": None,
            "side": None,
            "exclude": [],
            "add_toppings": [],
        }],
    }


def main():
    pending = (1, "type")

    # 실제 발견 버그 재현:
    # LLM이 exclude_add는 냈지만 item/type을 누락한 경우
    raw = OrderUpdate.model_validate({
        "intent": "order",
        "actions": [{
            "operation": "modify",
            "target": {"line_id": 1},
            "item": None,
            "exclude_add": ["pickle"],
        }],
    })

    u, w1 = repair_pending_explicit_field(
        "단품으로 하는데 피클은 빼주세요",
        burger_state(),
        pending,
        raw,
    )
    u, w2 = repair_pending_burger_modifiers(
        "단품으로 하는데 피클은 빼주세요",
        burger_state(),
        pending,
        u,
    )

    action = u.model_dump(mode="json", exclude_none=True)["actions"][0]
    assert action["item"]["type"] == "single", action
    assert action["exclude_add"] == ["pickle"], action

    # 이미 type이 있으면 그대로
    raw2 = OrderUpdate.model_validate({
        "intent": "order",
        "actions": [{
            "operation": "modify",
            "target": {"line_id": 1},
            "item": {"type": "single"},
            "exclude_add": ["pickle"],
        }],
    })
    u2, _ = repair_pending_explicit_field(
        "단품으로 하는데 피클은 빼주세요",
        burger_state(),
        pending,
        raw2,
    )
    a2 = u2.model_dump(mode="json", exclude_none=True)["actions"][0]
    assert a2["item"]["type"] == "single", a2

    # pending 답을 명시하지 않은 reference 문장은 type을 만들어내면 안 됨
    raw3 = OrderUpdate.model_validate({
        "intent": "order",
        "actions": [{
            "operation": "modify",
            "target": {"line_id": 1},
            "item": None,
            "toppings_add": ["bacon"],
        }],
    })
    u3, _ = repair_pending_explicit_field(
        "피클 빼놓은 불고기버거에 베이컨 넣어주세요",
        burger_state(),
        pending,
        raw3,
    )
    a3 = u3.model_dump(mode="json", exclude_none=True)["actions"][0]
    assert "item" not in a3, a3

    print("V14 pending field + modifier repair: 3/3 PASS")


if __name__ == "__main__":
    main()
