#!/usr/bin/env python3

from order_runtime_final import (
    OrderStateManager,
    extract_order_id,
    korean_number,
)
from order_update_schema import OrderUpdate


def check(actual, expected, name):
    if actual != expected:
        raise AssertionError(
            f"{name} 실패\nexpected={expected}\nactual={actual}"
        )
    print(f"✅ {name}")


check(
    korean_number("사백이십칠"),
    427,
    "한국어 숫자 427",
)

check(
    extract_order_id(
        "제가 잘못 불렀네요 실제 주문번호는 사백이십칠 번입니다"
    ),
    427,
    "모바일 주문번호 427",
)

check(
    extract_order_id(
        "184번 아니고 681번이에요"
    ),
    681,
    "주문번호 정정",
)

manager = OrderStateManager()

manager.apply(
    OrderUpdate.model_validate({
        "intent": "order",
        "actions": [{
            "operation": "add",
            "item": {
                "item_type": "drink",
                "quantity": 1,
                "drink": "zero_coke",
                "drink_size": "large",
            },
        }],
    })
)

manager.apply(
    OrderUpdate.model_validate({
        "intent": "order",
        "actions": [{
            "operation": "add",
            "item": {
                "item_type": "drink",
                "quantity": 2,
                "drink": "zero_coke",
                "drink_size": "large",
            },
        }],
    })
)

check(
    len(manager.state["items"]),
    1,
    "동일 음료 line 병합",
)

check(
    manager.state["items"][0]["quantity"],
    3,
    "동일 음료 수량 합산",
)

manager.apply(
    OrderUpdate.model_validate({
        "intent": "order",
        "actions": [{
            "operation": "adjust_quantity",
            "target": {"line_id": 1},
            "quantity_delta": -1,
        }],
    })
)

check(
    manager.state["items"][0]["quantity"],
    2,
    "adjust_quantity",
)

print("\n✅ deterministic runtime 전체 테스트 PASS")
