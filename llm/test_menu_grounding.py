from order_runtime_final import (
    ModelOutputError,
    OrderUpdate,
    guard_add_menu_grounding,
)


def add_burger(menu):
    return OrderUpdate.model_validate({
        "intent": "order",
        "order_id": None,
        "actions": [
            {
                "operation": "add",
                "target": None,
                "item": {
                    "item_type": "burger",
                    "quantity": 1,
                    "menu": menu,
                    "type": None,
                    "drink": None,
                    "drink_size": None,
                    "side": None,
                },
                "quantity_delta": None,
                "apply_to_all": False,
                "exclude_add": [],
                "exclude_remove": [],
                "toppings_add": [],
                "toppings_remove": [],
            }
        ],
    })


EMPTY = {
    "intent": "order",
    "order_id": None,
    "items": [],
}


# ============================================================
# 정상 메뉴
# ============================================================

valid = [
    ("새우버거 하나 주세요", "shrimp_burger"),
    ("치킨버거 하나 주세요", "chicken_burger"),
    ("불고기버거 하나 주세요", "bulgogi_burger"),

    # 짧은 alias도 독립된 주문 단위라면 허용
    ("새우 하나 주세요", "shrimp_burger"),
    ("치킨 하나 주세요", "chicken_burger"),
    ("불고기 하나 주세요", "bulgogi_burger"),
]

for text, menu in valid:

    update, _ = guard_add_menu_grounding(
        text,
        EMPTY,
        add_burger(menu),
    )

    assert update is not None


# ============================================================
# 다른 음식명 속 부분 문자열 -> 절대 버거로 매핑 금지
# ============================================================

invalid = [
    ("닭도리탕 하나 주세요", "chicken_burger"),
    ("새우볶음밥 하나 주세요", "shrimp_burger"),
    ("치즈돈까스 하나 주세요", "cheese_burger"),
    ("불고기덮밥 하나 주세요", "bulgogi_burger"),
    ("치킨마요덮밥 하나 주세요", "chicken_burger"),
]

for text, menu in invalid:

    try:
        guard_add_menu_grounding(
            text,
            EMPTY,
            add_burger(menu),
        )

    except ModelOutputError as e:
        assert e.code == "UNGROUNDED_MENU"

    else:
        raise AssertionError(
            f"차단 실패: {text} -> {menu}"
        )


# ============================================================
# 사용자가 말한 메뉴와 LLM 출력 불일치
# ============================================================

try:
    guard_add_menu_grounding(
        "불고기버거 하나 주세요",
        EMPTY,
        add_burger("chicken_burger"),
    )

except ModelOutputError:
    pass

else:
    raise AssertionError(
        "명시 메뉴와 LLM menu 불일치 차단 실패"
    )


# ============================================================
# 문맥 반복은 기존 state 메뉴에 한해서 허용
# ============================================================

STATE = {
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
            "add_toppings": [],
        }
    ],
}

guard_add_menu_grounding(
    "하나 더 주세요",
    STATE,
    add_burger("bulgogi_burger"),
)

try:
    guard_add_menu_grounding(
        "하나 더 주세요",
        STATE,
        add_burger("shrimp_burger"),
    )

except ModelOutputError:
    pass

else:
    raise AssertionError(
        "기존 state에 없는 메뉴 반복 허용됨"
    )


print("PASS 3A: explicit menu grounding")
print("PASS 3B: compound unknown foods rejected")
print("PASS 3C: wrong LLM menu rejected")
print("PASS 3D: contextual repeat limited to existing menu")
print()
print("MENU GROUNDING: ALL PASS")
