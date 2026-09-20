#!/usr/bin/env python3
import sys
import types

# 테스트 중에는 실제 OpenAI/vLLM 호출을 하지 않는다.
if "openai" not in sys.modules:
    fake = types.ModuleType("openai")
    class OpenAI:
        def __init__(self, *args, **kwargs):
            pass
    fake.OpenAI = OpenAI
    sys.modules["openai"] = fake

from order_update_schema import OrderUpdate
from order_runtime_final import repair_add_items, verify_semantics


def main():
    # 1) 독립 음료에 잘못 붙은 side 제거
    raw = {
        "intent": "order",
        "actions": [{
            "operation": "add",
            "item": {
                "item_type": "drink",
                "quantity": 1,
                "drink": "iced_coffee",
                "drink_size": "medium",
                "side": "french_fries",
            }
        }]
    }
    repaired, warnings = repair_add_items(raw)
    item = repaired["actions"][0]["item"]
    assert "side" not in item, repaired

    # 2) size 미지정이면 hallucinated medium도 semantic verifier가 제거
    update = OrderUpdate.model_validate(repaired)
    verified, warnings2 = verify_semantics(
        "아이스 아메리카노 한 잔 넣어주세요",
        {"intent": "unknown", "order_id": None, "items": []},
        None,
        update,
    )
    out = verified.model_dump(mode="json", exclude_none=True)
    item = out["actions"][0]["item"]
    assert "drink_size" not in item, out

    # 3) 사용자가 size를 직접 말하면 보존
    raw2 = {
        "intent": "order",
        "actions": [{
            "operation": "add",
            "item": {
                "item_type": "drink",
                "quantity": 1,
                "drink": "iced_coffee",
                "drink_size": "large",
            }
        }]
    }
    repaired2, _ = repair_add_items(raw2)
    update2 = OrderUpdate.model_validate(repaired2)
    verified2, _ = verify_semantics(
        "아이스 아메리카노 라지 한 잔 넣어주세요",
        {"intent": "unknown", "order_id": None, "items": []},
        None,
        update2,
    )
    out2 = verified2.model_dump(mode="json", exclude_none=True)
    assert out2["actions"][0]["item"]["drink_size"] == "large", out2

    print("V14 runtime sanitizer + grounding: 3/3 PASS")


if __name__ == "__main__":
    main()
