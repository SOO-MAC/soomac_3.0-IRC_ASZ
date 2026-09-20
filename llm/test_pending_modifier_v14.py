#!/usr/bin/env python3
import sys
import types

# 실제 vLLM 호출 없이 runtime 함수만 테스트
if "openai" not in sys.modules:
    fake = types.ModuleType("openai")
    class OpenAI:
        def __init__(self, *args, **kwargs):
            pass
    fake.OpenAI = OpenAI
    sys.modules["openai"] = fake

from order_update_schema import OrderUpdate
from order_runtime_final import repair_pending_burger_modifiers


def state(exclude=None):
    return {
        "intent": "order",
        "order_id": None,
        "items": [{
            "line_id": 2,
            "item_type": "burger",
            "quantity": 1,
            "menu": "bulgogi_burger",
            "type": None,
            "drink": None,
            "drink_size": None,
            "side": None,
            "exclude": exclude or [],
            "add_toppings": [],
        }],
    }


def modify_type_single(exclude_add=None):
    return OrderUpdate.model_validate({
        "intent": "order",
        "actions": [{
            "operation": "modify",
            "target": {"line_id": 2},
            "item": {"type": "single"},
            "exclude_add": exclude_add or [],
        }],
    })


def get_action(update):
    return update.model_dump(mode="json", exclude_none=True)["actions"][0]


def main():
    pending = (2, "type")

    # 핵심 재현: LLM이 type만 출력해도 피클 제외를 복원
    u, w = repair_pending_burger_modifiers(
        "단품으로 하는데 피클은 빼주세요",
        state(),
        pending,
        modify_type_single(),
    )
    assert get_action(u)["exclude_add"] == ["pickle"], get_action(u)

    # reference 표현은 새 제외 명령으로 오인하지 않음
    u, w = repair_pending_burger_modifiers(
        "피클 빼놓은 불고기버거에 베이컨 넣어주세요",
        state(["pickle"]),
        pending,
        modify_type_single(),
    )
    assert not get_action(u).get("exclude_add"), get_action(u)

    # 부정 표현도 오인하지 않음
    u, w = repair_pending_burger_modifiers(
        "단품으로 하고 피클은 빼지 말아주세요",
        state(),
        pending,
        modify_type_single(),
    )
    assert not get_action(u).get("exclude_add"), get_action(u)

    # 이미 LLM이 넣었다면 중복 없음
    u, w = repair_pending_burger_modifiers(
        "단품으로 하고 피클은 빼주세요",
        state(),
        pending,
        modify_type_single(["pickle"]),
    )
    assert get_action(u)["exclude_add"] == ["pickle"], get_action(u)

    print("V14 pending modifier repair: 4/4 PASS")


if __name__ == "__main__":
    main()
