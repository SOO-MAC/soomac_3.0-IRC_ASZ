import json
import re

from openai import OpenAI
from pydantic import ValidationError

from order_schema import (
    Burger,
    Drink,
    DrinkSize,
    Exclude,
    Intent,
    ItemType,
    OrderCommand,
    OrderItem,
    OrderType,
    Side,
    Topping,
)

from order_update_schema import (
    ItemPatch,
    ItemSelector,
    Operation,
    OrderAction,
    OrderUpdate,
)


# =========================
# QWEN CLIENT
# =========================

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="EMPTY",
)


# =========================
# SYSTEM PROMPT
# =========================

SYSTEM_PROMPT = """
너는 드라이브스루 주문의 현재 사용자 발화를
구조화된 OrderAction으로 변환하는 AI다.

최종 주문 전체를 다시 생성하지 말고,
사용자가 현재 발화에서 요청한 동작만 추출한다.


=========================
절대 규칙
=========================

1. 사용자가 직접 말하지 않은 값은 추측하지 않는다.

2. 현재 주문 상태의 값을 item에 복사하지 않는다.

3. 한 문장에 여러 동작이 있으면 actions에 여러 개 넣는다.

4. 기존 주문을 수정하거나 삭제할 때 대상이 명확하면
   현재 주문 상태의 line_id를 target.line_id에 넣는다.

5. 대상이 모호하면 임의의 line_id를 선택하지 않는다.

6. "불고기버거 하나 주세요"에서
   단품이나 세트를 말하지 않았으면 type을 넣지 않는다.

7. 세트를 주문했다고 해서
   음료, 음료 사이즈, 사이드를 임의로 정하지 않는다.


=========================
INTENT
=========================

일반 주문 / 추가 / 수정 / 삭제
→ order

모바일 주문 픽업
→ mobile_pickup

주문 확정
→ confirm

전체 주문 취소
→ cancel

판단할 수 없음
→ unknown


=========================
OPERATION
=========================

add
→ 새로운 상품을 주문에 추가

modify
→ 기존 상품의 값 변경

remove
→ 기존 주문 라인 전체 삭제

adjust_quantity
→ 기존 주문 라인의 수량을 증가 또는 감소

reset
→ 현재 주문 전체 초기화


=========================
ADD 수량
=========================

새 상품의 개수는 item.quantity에 넣는다.

add에서는 quantity_delta를 사용하지 않는다.

예:

"감자튀김 5개 추가"

operation=add
item.item_type=side
item.side=french_fries
item.quantity=5


"불고기버거 단품 4개 추가"

operation=add
item.item_type=burger
item.menu=bulgogi_burger
item.type=single
item.quantity=4


=========================
기존 수량 변경
=========================

"기존 불고기버거 하나 더"

operation=adjust_quantity
quantity_delta=1


"기존 불고기버거 하나 빼"

operation=adjust_quantity
quantity_delta=-1


"불고기버거 두 개로 바꿔주세요"

operation=modify
item.quantity=2


=========================
세트 구성과 독립 상품
=========================

세트 음료를 묻는 중:

"사이다요"

→ 기존 burger line modify
→ drink=sprite


하지만:

"사이다 한 잔 추가해주세요"

→ 새로운 drink line add
→ drink=sprite


세트 사이드를 묻는 중:

"감자튀김이요"

→ 기존 burger line modify
→ side=french_fries


하지만:

"감자튀김 5개 추가해주세요"

→ 새로운 side line add
→ side=french_fries
→ quantity=5


=========================
수정
=========================

"콜라 말고 사이다로 바꿔주세요"

→ modify
→ drink=sprite


"감자튀김 말고 치즈스틱으로"

→ modify
→ side=cheese_stick


=========================
재료 제외 / 토핑
=========================

"피클 빼주세요"

→ modify
→ exclude_add=["pickle"]


"피클 다시 넣어주세요"

→ modify
→ exclude_remove=["pickle"]


"치즈 추가해주세요"

→ modify
→ toppings_add=["cheese"]


"추가한 치즈 빼주세요"

→ modify
→ toppings_remove=["cheese"]


=========================
PENDING
=========================

현재 시스템이 특정 line_id의 값을 질문한 상태라면
짧은 답변은 해당 line을 수정하는 것으로 처리한다.

예:

pending:
line_id=1
field=drink

사용자:
"사이다요"

→ operation=modify
→ target.line_id=1
→ item.drink=sprite


pending:
line_id=1
field=side

사용자:
"감자튀김이요"

→ operation=modify
→ target.line_id=1
→ item.side=french_fries


=========================
BURGER
=========================

불고기버거 / 불고기 버거 / 불고기
→ bulgogi_burger

치킨버거 / 치킨 버거 / 치킨
→ chicken_burger

치즈버거 / 치즈 버거
→ cheese_burger

새우버거 / 새우 버거 / 새우
→ shrimp_burger


=========================
TYPE
=========================

단품 / 버거만
→ single

세트 / 세트메뉴 / 세트 메뉴
→ set


=========================
DRINK
=========================

콜라 / 코카콜라
→ coke

제로콜라 / 제로 콜라 / 콜라제로
→ zero_coke

사이다 / 스프라이트
→ sprite

환타 / 판타
→ fanta

아이스커피 / 아이스 커피 / 아아
→ iced_coffee


=========================
DRINK SIZE
=========================

스몰 / 작은거
→ small

미디움 / 중간 / 중간 사이즈
→ medium

라지 / 큰거
→ large


=========================
SIDE
=========================

감자튀김 / 감자 튀김 / 감튀 / 프렌치프라이
→ french_fries

치즈스틱 / 치즈 스틱
→ cheese_stick


=========================
MOBILE PICKUP
=========================

"맥오더 35번이요"
"모바일 주문 35번이요"
"35번 찾으러 왔어요"

→ intent=mobile_pickup
→ order_id=35
→ actions=[]


=========================
CONFIRM
=========================

"네"
"넵"
"맞아요"
"그대로 주세요"
"확정할게요"

→ confirm


=========================
CANCEL
=========================

"주문 취소할게요"
"전체 취소"

→ cancel


=========================
RESET
=========================

"처음부터 다시"
"다시 주문할게요"

→ intent=order
→ operation=reset
"""


# =========================
# ALIAS
# =========================

WORDS = {
    "menu": {
        "bulgogi_burger": [
            "불고기버거",
            "불고기 버거",
            "불고기",
        ],
        "chicken_burger": [
            "치킨버거",
            "치킨 버거",
            "치킨",
        ],
        "cheese_burger": [
            "치즈버거",
            "치즈 버거",
        ],
        "shrimp_burger": [
            "새우버거",
            "새우 버거",
            "새우",
        ],
    },

    "type": {
        "single": [
            "단품",
            "버거만",
        ],
        "set": [
            "세트메뉴",
            "세트 메뉴",
            "세트",
        ],
    },

    "drink": {
        # 긴 표현을 먼저 둔다.
        "zero_coke": [
            "제로콜라",
            "제로 콜라",
            "콜라제로",
            "콜라 제로",
        ],
        "coke": [
            "코카콜라",
            "코카 콜라",
            "콜라",
        ],
        "sprite": [
            "사이다",
            "스프라이트",
        ],
        "fanta": [
            "환타",
            "판타",
        ],
        "iced_coffee": [
            "아이스커피",
            "아이스 커피",
            "아아",
        ],
    },

    "drink_size": {
        "small": [
            "스몰",
            "작은거",
            "작은 걸로",
        ],
        "medium": [
            "미디움",
            "중간 사이즈",
            "중간",
        ],
        "large": [
            "라지",
            "큰거",
            "큰 걸로",
        ],
    },

    "side": {
        "french_fries": [
            "감자튀김",
            "감자 튀김",
            "감튀",
            "프렌치프라이",
            "프렌치 프라이",
        ],
        "cheese_stick": [
            "치즈스틱",
            "치즈 스틱",
        ],
    },

    "exclude": {
        "onion": ["양파"],
        "pickle": ["피클"],
        "tomato": ["토마토"],
        "cheese": ["치즈"],
        "lettuce": [
            "양상추",
            "상추",
        ],
    },

    "topping": {
        "cheese": ["치즈"],
        "bacon": ["베이컨"],
        "tomato": ["토마토"],
    },
}


FIELD_ENUM = {
    "menu": Burger,
    "type": OrderType,
    "drink": Drink,
    "drink_size": DrinkSize,
    "side": Side,
}


DISPLAY_NAMES = {
    "bulgogi_burger": "불고기버거",
    "chicken_burger": "치킨버거",
    "cheese_burger": "치즈버거",
    "shrimp_burger": "새우버거",

    "coke": "콜라",
    "zero_coke": "제로콜라",
    "sprite": "사이다",
    "fanta": "환타",
    "iced_coffee": "아이스커피",

    "french_fries": "감자튀김",
    "cheese_stick": "치즈스틱",
}


# =========================
# TEXT
# =========================

def compact(text: str) -> str:
    return (
        text
        .lower()
        .replace(" ", "")
    )


def explicit_matches(
    text: str,
    field: str,
) -> list[tuple[int, int, str]]:

    text = compact(text)

    candidates: list[
        tuple[int, int, int, str]
    ] = []

    for value, aliases in WORDS[field].items():

        for alias in aliases:

            alias = compact(alias)

            start = 0

            while True:

                index = text.find(
                    alias,
                    start,
                )

                if index < 0:
                    break

                candidates.append(
                    (
                        index,
                        index + len(alias),
                        len(alias),
                        value,
                    )
                )

                start = index + 1

    # 긴 문자열 우선.
    # "제로콜라" 안의 "콜라" 중복 검출 방지.
    candidates.sort(
        key=lambda x: (
            -x[2],
            x[0],
        )
    )

    selected: list[
        tuple[int, int, str]
    ] = []

    occupied: list[
        tuple[int, int]
    ] = []

    for start, end, _, value in candidates:

        overlap = any(
            not (
                end <= used_start
                or start >= used_end
            )
            for used_start, used_end
            in occupied
        )

        if overlap:
            continue

        selected.append(
            (
                start,
                end,
                value,
            )
        )

        occupied.append(
            (
                start,
                end,
            )
        )

    selected.sort(
        key=lambda x: x[0]
    )

    return selected


def mentioned(
    text: str,
    field: str,
    value: str,
) -> bool:

    return any(
        match_value == value
        for _, _, match_value
        in explicit_matches(
            text,
            field,
        )
    )


def extract_last_explicit_value(
    text: str,
    field: str,
) -> str | None:

    matches = explicit_matches(
        text,
        field,
    )

    if not matches:
        return None

    # "콜라 말고 사이다"
    # 마지막 명시값인 sprite 사용
    return matches[-1][2]


# =========================
# QUANTITY
# =========================

def extract_quantity(
    text: str,
) -> int | None:

    text = compact(text)

    match = re.search(
        r"(\d+)(?:개|잔|세트)",
        text,
    )

    if match:
        return int(
            match.group(1)
        )

    korean_numbers = {
        "한세트": 1,
        "한개": 1,
        "한잔": 1,
        "하나": 1,

        "두세트": 2,
        "두개": 2,
        "두잔": 2,
        "둘": 2,

        "세세트": 3,
        "세개": 3,
        "세잔": 3,
        "셋": 3,

        "네세트": 4,
        "네개": 4,
        "네잔": 4,
        "넷": 4,

        "다섯세트": 5,
        "다섯개": 5,
        "다섯잔": 5,

        "여섯세트": 6,
        "여섯개": 6,
        "여섯잔": 6,

        "일곱세트": 7,
        "일곱개": 7,
        "일곱잔": 7,

        "여덟세트": 8,
        "여덟개": 8,
        "여덟잔": 8,

        "아홉세트": 9,
        "아홉개": 9,
        "아홉잔": 9,

        "열세트": 10,
        "열개": 10,
        "열잔": 10,
    }

    for word in sorted(
        korean_numbers,
        key=len,
        reverse=True,
    ):

        if word in text:
            return korean_numbers[
                word
            ]

    return None


def has_quantity(
    text: str,
) -> bool:

    return (
        extract_quantity(text)
        is not None
    )


def extract_quantity_delta(
    text: str,
) -> int | None:

    text = compact(text)

    quantity = (
        extract_quantity(text)
        or 1
    )

    if any(
        word in text
        for word in [
            "빼",
            "줄여",
            "감소",
        ]
    ):
        return -quantity

    if any(
        word in text
        for word in [
            "더",
            "늘려",
            "증가",
        ]
    ):
        return quantity

    return None


# =========================
# ORDER ID
# =========================

def extract_order_id(
    text: str,
) -> int | None:

    matches = re.findall(
        r"(\d+)번",
        compact(text),
    )

    if not matches:
        return None

    # "35번 아니고 38번"
    # 마지막 값을 사용
    return int(
        matches[-1]
    )


# =========================
# WORD CHECKS
# =========================

def has_explicit_add_word(
    text: str,
) -> bool:

    text = compact(text)

    return any(
        word in text
        for word in [
            "추가",
            "더주세요",
            "더줘",
            "하나더",
            "한개더",
            "두개더",
            "세개더",
        ]
    )


# =========================
# OPTIONS SAFETY
# =========================

def alias_present(
    text: str,
    field: str,
    value: str,
) -> bool:

    text = compact(text)

    return any(
        compact(alias) in text
        for alias
        in WORDS[field].get(
            value,
            [],
        )
    )


def keep_exclude_add(
    text: str,
    value: str,
) -> bool:

    text = compact(text)

    if not alias_present(
        text,
        "exclude",
        value,
    ):
        return False

    # "치즈버거 빼주세요"를
    # 치즈 재료 제외로 오해하지 않음.
    if (
        value == "cheese"
        and "치즈버거" in text
        and not any(
            word in text
            for word in [
                "치즈빼",
                "치즈없이",
                "치즈제외",
            ]
        )
    ):
        return False

    return any(
        word in text
        for word in [
            "빼",
            "없이",
            "제외",
        ]
    )


def keep_exclude_remove(
    text: str,
    value: str,
) -> bool:

    text = compact(text)

    return (
        alias_present(
            text,
            "exclude",
            value,
        )
        and any(
            word in text
            for word in [
                "다시넣",
                "넣어",
                "빼지마",
                "제외취소",
            ]
        )
    )


def keep_topping_add(
    text: str,
    value: str,
) -> bool:

    text = compact(text)

    for alias in WORDS[
        "topping"
    ].get(
        value,
        [],
    ):

        alias = compact(alias)

        if any(
            pattern in text
            for pattern in [
                f"{alias}추가",
                f"{alias}토핑",
                f"{alias}더넣",
            ]
        ):
            return True

    return False


def keep_topping_remove(
    text: str,
    value: str,
) -> bool:

    text = compact(text)

    for alias in WORDS[
        "topping"
    ].get(
        value,
        [],
    ):

        alias = compact(alias)

        if any(
            pattern in text
            for pattern in [
                f"{alias}빼",
                f"{alias}제거",
                f"{alias}토핑취소",
            ]
        ):
            return True

    return False

def recover_explicit_options(
    user_text: str,
    update: OrderUpdate,
) -> OrderUpdate:

    text = compact(user_text)

    # 수정 action만 대상으로 함
    modify_actions = [
        action
        for action in update.actions
        if action.operation == Operation.MODIFY
    ]

    # 대상이 하나로 명확할 때만 Python이 복구
    if len(modify_actions) != 1:
        return update

    action = modify_actions[0]

    # =========================
    # EXCLUDE ADD
    # 피클 빼줘 / 양파 없이
    # =========================

    exclude_add_mode = (
        any(
            word in text
            for word in [
                "빼",
                "없이",
                "제외",
            ]
        )
        and not any(
            word in text
            for word in [
                "빼지마",
                "다시넣",
                "제외취소",
            ]
        )
    )

    if exclude_add_mode:

        for value, aliases in WORDS["exclude"].items():

            mentioned_value = any(
                compact(alias) in text
                for alias in aliases
            )

            if not mentioned_value:
                continue

            # "치즈버거 빼주세요"를
            # 치즈 재료 제외로 잘못 처리하지 않음
            if (
                value == "cheese"
                and "치즈버거" in text
                and not any(
                    pattern in text
                    for pattern in [
                        "치즈빼",
                        "치즈없이",
                        "치즈제외",
                    ]
                )
            ):
                continue

            enum_value = Exclude(value)

            if enum_value not in action.exclude_add:
                action.exclude_add.append(
                    enum_value
                )

    # =========================
    # EXCLUDE REMOVE
    # 피클 다시 넣어줘
    # =========================

    exclude_remove_mode = any(
        word in text
        for word in [
            "다시넣",
            "넣어",
            "빼지마",
            "제외취소",
        ]
    )

    if exclude_remove_mode:

        for value, aliases in WORDS["exclude"].items():

            if not any(
                compact(alias) in text
                for alias in aliases
            ):
                continue

            enum_value = Exclude(value)

            if enum_value not in action.exclude_remove:
                action.exclude_remove.append(
                    enum_value
                )

    # =========================
    # TOPPING ADD
    # 치즈 추가 / 베이컨 토핑
    # =========================

    for value, aliases in WORDS["topping"].items():

        for alias in aliases:

            alias = compact(alias)

            topping_add = any(
                pattern in text
                for pattern in [
                    f"{alias}추가",
                    f"{alias}토핑",
                    f"{alias}더넣",
                ]
            )

            if topping_add:

                enum_value = Topping(value)

                if enum_value not in action.toppings_add:
                    action.toppings_add.append(
                        enum_value
                    )

                break

    # =========================
    # TOPPING REMOVE
    # 추가한 치즈 빼줘
    # =========================

    for value, aliases in WORDS["topping"].items():

        for alias in aliases:

            alias = compact(alias)

            topping_remove = any(
                pattern in text
                for pattern in [
                    f"추가한{alias}빼",
                    f"{alias}토핑빼",
                    f"{alias}토핑제거",
                    f"{alias}토핑취소",
                ]
            )

            if topping_remove:

                enum_value = Topping(value)

                if enum_value not in action.toppings_remove:
                    action.toppings_remove.append(
                        enum_value
                    )

                break

    return update

# =========================
# MULTIPLE BURGER RECOVERY
# =========================

def recover_multiple_burger_adds(
    user_text: str,
    update: OrderUpdate,
) -> OrderUpdate:

    menu_matches = explicit_matches(
        user_text,
        "menu",
    )

    # 버거 종류가 2개 미만이면
    # 일반 처리 사용
    if len(menu_matches) < 2:
        return update

    text = compact(
        user_text
    )

    # 수정/삭제 문장에는
    # 강제로 ADD 복구하지 않는다.
    modify_words = [
        "바꿔",
        "변경",
        "수정",
        "삭제",
        "제거",
        "빼줘",
        "빼주세요",
    ]

    if (
        not has_explicit_add_word(
            user_text
        )
        and any(
            word in text
            for word in modify_words
        )
    ):
        return update

    new_actions: list[
        OrderAction
    ] = []

    for index, (
        start,
        _,
        menu_value,
    ) in enumerate(
        menu_matches
    ):

        if (
            index + 1
            < len(menu_matches)
        ):
            end = (
                menu_matches[
                    index + 1
                ][0]
            )
        else:
            end = len(text)

        segment = text[
            start:end
        ]

        order_type = (
            extract_last_explicit_value(
                segment,
                "type",
            )
        )

        quantity = (
            extract_quantity(
                segment
            )
        )

        if quantity is None:
            quantity = 1

        drink = (
            extract_last_explicit_value(
                segment,
                "drink",
            )
        )

        drink_size = (
            extract_last_explicit_value(
                segment,
                "drink_size",
            )
        )

        side = (
            extract_last_explicit_value(
                segment,
                "side",
            )
        )

        new_actions.append(
            OrderAction(
                operation=Operation.ADD,

                item=ItemPatch(
                    item_type=(
                        ItemType.BURGER
                    ),
                    menu=menu_value,
                    type=order_type,
                    quantity=quantity,
                    drink=drink,
                    drink_size=drink_size,
                    side=side,
                ),
            )
        )

    # Qwen이 만든 ADD들은 제거하고
    # Python이 복구한 다중 ADD 사용
    non_add_actions = [
        action
        for action
        in update.actions
        if (
            action.operation
            != Operation.ADD
        )
    ]

    update.actions = (
        non_add_actions
        + new_actions
    )

    update.intent = (
        Intent.ORDER
    )

    return update


# =========================
# STATE
# =========================

def create_state() -> dict:

    return {
        "intent": "unknown",
        "order_id": None,
        "items": [],
    }


def next_line_id(
    state: dict,
) -> int:

    return (
        max(
            (
                item["line_id"]
                for item
                in state["items"]
            ),
            default=0,
        )
        + 1
    )


# =========================
# QWEN
# =========================

def ask_qwen(
    user_text: str,
    state: dict,
    pending,
) -> OrderUpdate:

    if pending is None:
        pending_text = "없음"

    else:
        pending_text = (
            f"line_id={pending[0]}, "
            f"field={pending[1]}"
        )

    prompt = f"""
현재 주문 상태:

{json.dumps(
    state,
    ensure_ascii=False,
    indent=2,
)}

현재 확인 중인 항목:

{pending_text}

현재 사용자 발화:

{user_text}

현재 사용자 발화에서 발생한 동작만 추출해라.
"""

    response = (
        client
        .chat
        .completions
        .create(
            model=(
                "Qwen/Qwen3.5-9B"
            ),

            messages=[
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],

            response_format={
                "type": "json_schema",

                "json_schema": {
                    "name":
                        "order_update",

                    "schema":
                        OrderUpdate
                        .model_json_schema(),
                },
            },

            extra_body={
                "chat_template_kwargs": {
                    "enable_thinking":
                        False,
                }
            },

            temperature=0.0,

            max_tokens=600,
        )
    )

    content = (
        response
        .choices[0]
        .message
        .content
    )

    if content is None:
        raise RuntimeError(
            "Qwen 응답이 없습니다."
        )

    print(
        "\n[Qwen 추출]"
    )

    print(
        content
    )

    return (
        OrderUpdate
        .model_validate_json(
            content
        )
    )


# =========================
# SANITIZE
# =========================

def sanitize_update(
    user_text: str,
    update: OrderUpdate,
    pending,
) -> OrderUpdate:

    pending_line = (
        pending[0]
        if pending
        else None
    )

    pending_field = (
        pending[1]
        if pending
        else None
    )

    text = compact(
        user_text
    )

    # =========================
    # 복수 버거 누락 복구
    # =========================

    update = (
        recover_multiple_burger_adds(
            user_text,
            update,
        )
    )

    # =========================
    # MOBILE ORDER ID
    # =========================

    if (
        update.intent
        == Intent.MOBILE_PICKUP
        and update.order_id
        is None
    ):

        update.order_id = (
            extract_order_id(
                user_text
            )
        )

    if (
        pending_field
        == "order_id"
        and update.order_id
        is None
    ):

        order_id = (
            extract_order_id(
                user_text
            )
        )

        if order_id is not None:

            update.intent = (
                Intent.MOBILE_PICKUP
            )

            update.order_id = (
                order_id
            )

    add_actions = [
        action
        for action
        in update.actions
        if (
            action.operation
            == Operation.ADD
        )
    ]

    total_actions = len(
        update.actions
    )

    # =========================
    # ACTION LOOP
    # =========================

    for action in update.actions:

        # -------------------------
        # PENDING 답변
        # -------------------------

        if (
            pending_line is not None
            and action.operation
            == Operation.ADD
            and not has_explicit_add_word(
                user_text
            )
        ):

            action.operation = (
                Operation.MODIFY
            )

            action.target = (
                ItemSelector(
                    line_id=pending_line,
                )
            )

        if (
            pending_line is not None
            and action.operation
            == Operation.MODIFY
        ):

            target_data = (
                action.target
                .model_dump(
                    exclude_none=True,
                )
                if action.target
                is not None
                else {}
            )

            if not target_data:

                action.target = (
                    ItemSelector(
                        line_id=pending_line,
                    )
                )

        item = action.item

        # -------------------------
        # ADD QUANTITY
        # -------------------------

        if (
            action.operation
            == Operation.ADD
            and item is not None
        ):

            # Qwen이 ADD 수량을
            # quantity_delta에 넣은 경우
            if (
                item.quantity
                is None
                and action.quantity_delta
                is not None
                and action.quantity_delta > 0
            ):

                item.quantity = (
                    action.quantity_delta
                )

            # ADD action 하나인 경우
            # 원문에서 수량 직접 복구
            if (
                item.quantity
                is None
                and len(
                    add_actions
                ) == 1
            ):

                quantity = (
                    extract_quantity(
                        user_text
                    )
                )

                if quantity is not None:

                    item.quantity = (
                        quantity
                    )

            action.quantity_delta = (
                None
            )

        # -------------------------
        # ADJUST QUANTITY
        # -------------------------

        if (
            action.operation
            == Operation.ADJUST_QUANTITY
            and (
                action.quantity_delta
                is None
                or action.quantity_delta
                == 0
            )
        ):

            action.quantity_delta = (
                extract_quantity_delta(
                    user_text
                )
            )

        # =========================
        # ITEM SAFETY
        # =========================

        if item is not None:

            # Qwen이 말하지 않은 값을
            # 만들어냈으면 제거
            for field in [
                "menu",
                "type",
                "drink",
                "drink_size",
                "side",
            ]:

                current_value = getattr(
                    item,
                    field,
                )

                if (
                    current_value is not None
                    and not alias_present(
                        user_text,
                        field,
                        current_value.value,
                    )
                ):
                    setattr(
                        item,
                        field,
                        None,
                    )

            # 단일 action인 경우는
            # 실제 사용자 발화를 최우선으로 복구
            #
            # 여러 action일 때 전역 값을 넣으면
            # 서로 다른 상품끼리 섞일 수 있으므로
            # 단일 action에서만 수행.
            if total_actions == 1:

                for field in [
                    "menu",
                    "type",
                    "drink",
                    "drink_size",
                    "side",
                ]:

                    explicit_value = (
                        extract_last_explicit_value(
                            user_text,
                            field,
                        )
                    )

                    if (
                        explicit_value
                        is None
                    ):
                        continue

                    enum_class = (
                        FIELD_ENUM[
                            field
                        ]
                    )

                    setattr(
                        item,
                        field,
                        enum_class(
                            explicit_value
                        ),
                    )

            # 수량이 Qwen hallucination이면 제거
            if (
                item.quantity
                is not None
                and not has_quantity(
                    user_text
                )
            ):

                item.quantity = None

            # 단일 ADD에서 Qwen이
            # 수량을 빼먹은 경우 다시 복구
            if (
                action.operation
                == Operation.ADD
                and item.quantity
                is None
                and len(
                    add_actions
                ) == 1
            ):

                quantity = (
                    extract_quantity(
                        user_text
                    )
                )

                if quantity is not None:

                    item.quantity = (
                        quantity
                    )

            # -------------------------
            # ITEM TYPE
            # -------------------------

            if (
                action.operation
                == Operation.ADD
            ):

                if (
                    item.menu
                    is not None
                ):

                    item.item_type = (
                        ItemType.BURGER
                    )

                elif (
                    item.drink
                    is not None
                ):

                    item.item_type = (
                        ItemType.DRINK
                    )

                elif (
                    item.side
                    is not None
                ):

                    item.item_type = (
                        ItemType.SIDE
                    )

        # =========================
        # APPLY TO ALL
        # =========================

        if (
            action.apply_to_all
            and not any(
                word in text
                for word in [
                    "둘다",
                    "전부",
                    "모두",
                    "전체",
                ]
            )
        ):

            action.apply_to_all = (
                False
            )

        # =========================
        # EXCLUDE
        # =========================

        action.exclude_add = [
            value
            for value
            in action.exclude_add
            if keep_exclude_add(
                user_text,
                value.value,
            )
        ]

        action.exclude_remove = [
            value
            for value
            in action.exclude_remove
            if keep_exclude_remove(
                user_text,
                value.value,
            )
        ]

        # =========================
        # TOPPING
        # =========================

        action.toppings_add = [
            value
            for value
            in action.toppings_add
            if keep_topping_add(
                user_text,
                value.value,
            )
        ]

        action.toppings_remove = [
            value
            for value
            in action.toppings_remove
            if keep_topping_remove(
                user_text,
                value.value,
            )
        ]

    # =========================
    # PENDING 누락값 복구
    # =========================

    if (
        pending_line
        is not None
    ):

        for field in [
            "menu",
            "type",
            "drink",
            "drink_size",
            "side",
        ]:

            explicit_value = (
                extract_last_explicit_value(
                    user_text,
                    field,
                )
            )

            if explicit_value is None:
                continue

            already_exists = False

            for action in (
                update.actions
            ):

                if (
                    action.item
                    is None
                ):
                    continue

                current_value = getattr(
                    action.item,
                    field,
                )

                if (
                    current_value
                    is None
                ):
                    continue

                target_line = (
                    action.target.line_id
                    if (
                        action.target
                        is not None
                    )
                    else None
                )

                if (
                    target_line
                    in (
                        None,
                        pending_line,
                    )
                ):

                    already_exists = True
                    break

            if already_exists:
                continue

            enum_class = (
                FIELD_ENUM[
                    field
                ]
            )

            update.actions.append(
                OrderAction(
                    operation=(
                        Operation.MODIFY
                    ),

                    target=ItemSelector(
                        line_id=pending_line,
                    ),

                    item=ItemPatch(
                        **{
                            field:
                                enum_class(
                                    explicit_value
                                )
                        }
                    ),
                )
            )
    # =========================
    # 명시적 제외/토핑 누락 복구
    # =========================

    update = recover_explicit_options(
        user_text,
        update,
    )

    return update


# =========================
# CREATE LINE
# =========================

def infer_item_type(
    item: ItemPatch,
) -> ItemType | None:

    if (
        item.item_type
        is not None
    ):
        return item.item_type

    if item.menu is not None:
        return ItemType.BURGER

    if item.drink is not None:
        return ItemType.DRINK

    if item.side is not None:
        return ItemType.SIDE

    return None


def make_line(
    state: dict,
    item: ItemPatch,
) -> dict:

    item_type = (
        infer_item_type(
            item
        )
    )

    if item_type is None:

        raise ValueError(
            "추가할 상품 종류를 "
            "판단할 수 없습니다."
        )

    data = {
        "line_id":
            next_line_id(
                state
            ),

        "item_type":
            item_type.value,

        "quantity":
            (
                item.quantity
                if (
                    item.quantity
                    is not None
                )
                else 1
            ),

        "menu": None,
        "type": None,

        "drink": None,
        "drink_size": None,

        "side": None,

        "exclude": [],
        "add_toppings": [],
    }

    patch = (
        item
        .model_dump(
            mode="json",
            exclude_none=True,
        )
    )

    patch.pop(
        "item_type",
        None,
    )

    data.update(
        patch
    )

    return (
        OrderItem
        .model_validate(
            data
        )
        .model_dump(
            mode="json"
        )
    )


# =========================
# TARGET RESOLUTION
# =========================

def selector_matches(
    line: dict,
    selector: ItemSelector,
) -> bool:

    selector_values = (
        selector
        .model_dump(
            mode="json",
            exclude_none=True,
        )
    )

    return all(
        line.get(key)
        == value
        for key, value
        in selector_values.items()
    )


def resolve_targets(
    state: dict,
    selector: ItemSelector | None,
    apply_to_all: bool,
    pending,
):

    if not state["items"]:

        return (
            [],
            "현재 수정할 주문 항목이 없습니다.",
        )

    selector_values = (
        selector
        .model_dump(
            mode="json",
            exclude_none=True,
        )
        if (
            selector
            is not None
        )
        else {}
    )

    # 대상 정보가 없음
    if not selector_values:

        if apply_to_all:

            return (
                [
                    item["line_id"]
                    for item
                    in state["items"]
                ],
                None,
            )

        if (
            pending is not None
            and pending[0]
            is not None
        ):

            return (
                [
                    pending[0]
                ],
                None,
            )

        if (
            len(
                state["items"]
            )
            == 1
        ):

            return (
                [
                    state[
                        "items"
                    ][0][
                        "line_id"
                    ]
                ],
                None,
            )

        return (
            [],
            "어느 주문 항목을 말씀하시는지 "
            "확인해 주세요.",
        )

    matches = [
        line["line_id"]
        for line
        in state["items"]
        if selector_matches(
            line,
            selector,
        )
    ]

    if not matches:

        return (
            [],
            "조건에 맞는 주문 항목을 "
            "찾지 못했습니다.",
        )

    if (
        len(matches) > 1
        and not apply_to_all
    ):

        return (
            [],
            "조건에 맞는 항목이 여러 개입니다. "
            "몇 번째 항목인지 말씀해 주세요.",
        )

    return (
        matches,
        None,
    )


def get_line(
    state: dict,
    line_id: int,
):

    return next(
        (
            item
            for item
            in state["items"]
            if (
                item["line_id"]
                == line_id
            )
        ),
        None,
    )


# =========================
# MODIFY LINE
# =========================

def modify_line(
    line: dict,
    action: OrderAction,
) -> None:

    if (
        action.item
        is not None
    ):

        patch = (
            action.item
            .model_dump(
                mode="json",
                exclude_none=True,
            )
        )

        # 기존 line의 종류는 유지
        patch.pop(
            "item_type",
            None,
        )

        line.update(
            patch
        )

        # 세트 → 단품 변경 시
        # 세트 옵션 제거
        if (
            patch.get(
                "type"
            )
            == "single"
        ):

            line["drink"] = None
            line["drink_size"] = None
            line["side"] = None

    for value in (
        action.exclude_add
    ):

        if (
            value.value
            not in line[
                "exclude"
            ]
        ):

            line[
                "exclude"
            ].append(
                value.value
            )

    for value in (
        action.exclude_remove
    ):

        if (
            value.value
            in line[
                "exclude"
            ]
        ):

            line[
                "exclude"
            ].remove(
                value.value
            )

    for value in (
        action.toppings_add
    ):

        if (
            value.value
            not in line[
                "add_toppings"
            ]
        ):

            line[
                "add_toppings"
            ].append(
                value.value
            )

    for value in (
        action.toppings_remove
    ):

        if (
            value.value
            in line[
                "add_toppings"
            ]
        ):

            line[
                "add_toppings"
            ].remove(
                value.value
            )


# =========================
# APPLY ACTION
# =========================

def apply_action(
    state: dict,
    action: OrderAction,
    pending,
) -> str | None:

    # RESET
    if (
        action.operation
        == Operation.RESET
    ):

        state.clear()

        state.update(
            create_state()
        )

        return None

    # ADD
    if (
        action.operation
        == Operation.ADD
    ):

        if (
            action.item
            is None
        ):

            return (
                "추가할 메뉴 정보가 없습니다."
            )

        line = make_line(
            state,
            action.item,
        )

        for value in (
            action.exclude_add
        ):

            if (
                value.value
                not in line[
                    "exclude"
                ]
            ):

                line[
                    "exclude"
                ].append(
                    value.value
                )

        for value in (
            action.toppings_add
        ):

            if (
                value.value
                not in line[
                    "add_toppings"
                ]
            ):

                line[
                    "add_toppings"
                ].append(
                    value.value
                )

        state[
            "items"
        ].append(
            line
        )

        state[
            "intent"
        ] = "order"

        return None

    # MODIFY / REMOVE / QUANTITY 대상
    (
        target_ids,
        error,
    ) = resolve_targets(
        state,
        action.target,
        action.apply_to_all,
        pending,
    )

    if (
        error
        is not None
    ):
        return error

    # MODIFY
    if (
        action.operation
        == Operation.MODIFY
    ):

        for line_id in target_ids:

            line = get_line(
                state,
                line_id,
            )

            if line is not None:

                modify_line(
                    line,
                    action,
                )

    # REMOVE
    elif (
        action.operation
        == Operation.REMOVE
    ):

        target_set = set(
            target_ids
        )

        state[
            "items"
        ] = [
            item
            for item
            in state["items"]
            if (
                item["line_id"]
                not in target_set
            )
        ]

    # QUANTITY +/-
    elif (
        action.operation
        == Operation.ADJUST_QUANTITY
    ):

        if (
            action.quantity_delta
            is None
            or action.quantity_delta
            == 0
        ):

            return (
                "수량을 얼마나 변경할지 "
                "확인해 주세요."
            )

        remove_ids = set()

        for line_id in target_ids:

            line = get_line(
                state,
                line_id,
            )

            if line is None:
                continue

            line[
                "quantity"
            ] += (
                action.quantity_delta
            )

            if (
                line["quantity"]
                <= 0
            ):

                remove_ids.add(
                    line_id
                )

        state[
            "items"
        ] = [
            item
            for item
            in state["items"]
            if (
                item["line_id"]
                not in remove_ids
            )
        ]

    state[
        "intent"
    ] = "order"

    return None


# =========================
# APPLY UPDATE
# =========================

def apply_update(
    state: dict,
    update: OrderUpdate,
    pending,
) -> str | None:

    # CANCEL
    if (
        update.intent
        == Intent.CANCEL
    ):

        state[
            "intent"
        ] = "cancel"

        return None

    # MOBILE PICKUP
    if (
        update.intent
        == Intent.MOBILE_PICKUP
    ):

        state.update(
            {
                "intent":
                    "mobile_pickup",

                "order_id":
                    update.order_id,

                "items":
                    [],
            }
        )

        return None

    # ACTIONS
    for action in (
        update.actions
    ):

        error = apply_action(
            state,
            action,
            pending,
        )

        if error is not None:
            return error

    return None


# =========================
# REQUIRED FIELDS
# =========================

def find_missing(
    state: dict,
):

    # MOBILE PICKUP
    if (
        state["intent"]
        == "mobile_pickup"
    ):

        if (
            state["order_id"]
            is None
        ):

            return (
                None,
                "order_id",
            )

        return None

    for line in (
        state["items"]
    ):

        line_id = (
            line["line_id"]
        )

        # BURGER
        if (
            line["item_type"]
            == "burger"
        ):

            if (
                line["menu"]
                is None
            ):

                return (
                    line_id,
                    "menu",
                )

            if (
                line["type"]
                is None
            ):

                return (
                    line_id,
                    "type",
                )

            if (
                line["type"]
                == "set"
            ):

                if (
                    line["drink"]
                    is None
                ):

                    return (
                        line_id,
                        "drink",
                    )

                if (
                    line[
                        "drink_size"
                    ]
                    is None
                ):

                    return (
                        line_id,
                        "drink_size",
                    )

                if (
                    line["side"]
                    is None
                ):

                    return (
                        line_id,
                        "side",
                    )

        # STANDALONE DRINK
        elif (
            line["item_type"]
            == "drink"
        ):

            if (
                line["drink"]
                is None
            ):

                return (
                    line_id,
                    "drink",
                )

            if (
                line[
                    "drink_size"
                ]
                is None
            ):

                return (
                    line_id,
                    "drink_size",
                )

        # STANDALONE SIDE
        elif (
            line["item_type"]
            == "side"
        ):

            if (
                line["side"]
                is None
            ):

                return (
                    line_id,
                    "side",
                )

    return None


# =========================
# DISPLAY
# =========================

def describe_line(
    state: dict,
    line_id,
) -> str:

    if line_id is None:
        return "해당 주문"

    line = get_line(
        state,
        line_id,
    )

    if line is None:
        return f"{line_id}번 항목"

    value = (
        line.get("menu")
        or line.get("drink")
        or line.get("side")
    )

    if value is None:
        return f"{line_id}번 항목"

    return DISPLAY_NAMES.get(
        value,
        value,
    )


def question_for(
    state: dict,
    pending,
) -> str:

    line_id, field = (
        pending
    )

    target = describe_line(
        state,
        line_id,
    )

    questions = {
        "menu":
            f"{target}은 어떤 버거로 하시겠습니까?",

        "type":
            f"{target}은 단품과 세트 중 어떤 걸로 하시겠습니까?",

        "drink":
            f"{target}의 음료는 무엇으로 하시겠습니까?",

        "drink_size":
            f"{target}의 음료 사이즈는 어떻게 하시겠습니까?",

        "side":
            f"{target}의 사이드는 감자튀김과 치즈스틱 중 어떤 걸로 하시겠습니까?",

        "order_id":
            "모바일 주문번호를 말씀해 주세요.",
    }

    return questions[
        field
    ]


def print_state(
    state: dict,
) -> None:

    print(
        "\n[현재 주문 상태]"
    )

    print(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
        )
    )


# =========================
# MAIN
# =========================

def main() -> None:

    state = (
        create_state()
    )

    pending = None

    print(
        "드라이브스루 주문 시스템 V2"
    )

    print(
        "종료하려면 q 입력\n"
    )

    while True:

        user_text = input(
            "사용자: "
        ).strip()

        if (
            user_text.lower()
            == "q"
        ):
            break

        if not user_text:
            continue

        try:

            # =========================
            # QWEN
            # =========================

            update = ask_qwen(
                user_text,
                state,
                pending,
            )

            before = (
                update
                .model_dump(
                    mode="json"
                )
            )

            # =========================
            # PYTHON SAFETY / RECOVERY
            # =========================

            update = (
                sanitize_update(
                    user_text,
                    update,
                    pending,
                )
            )

            after = (
                update
                .model_dump(
                    mode="json"
                )
            )

            if (
                before
                != after
            ):

                print(
                    "\n[Python 보정 후]"
                )

                print(
                    json.dumps(
                        after,
                        ensure_ascii=False,
                        indent=2,
                    )
                )

            # =========================
            # CONFIRM
            # =========================

            if (
                update.intent
                == Intent.CONFIRM
            ):

                missing = (
                    find_missing(
                        state
                    )
                )

                if (
                    missing
                    is not None
                ):

                    pending = (
                        missing
                    )

                    print(
                        "\nAI:",
                        question_for(
                            state,
                            pending,
                        ),
                    )

                    continue

                if (
                    state["intent"]
                    == "unknown"
                    or (
                        state["intent"]
                        == "order"
                        and not state[
                            "items"
                        ]
                    )
                ):

                    print(
                        "\nAI: 아직 주문 내용이 없습니다."
                    )

                    continue

                final_order = (
                    OrderCommand
                    .model_validate(
                        state
                    )
                )

                print(
                    "\n[최종 주문]"
                )

                print(
                    json.dumps(
                        final_order
                        .model_dump(
                            mode="json"
                        ),
                        ensure_ascii=False,
                        indent=2,
                    )
                )

                if (
                    state["intent"]
                    == "mobile_pickup"
                ):

                    print(
                        "\nAI: 모바일 주문을 확인했습니다. "
                        "픽업을 진행합니다."
                    )

                else:

                    print(
                        "\nAI: 주문이 확정되었습니다."
                    )

                break

            # =========================
            # UNKNOWN
            # =========================

            if (
                update.intent
                == Intent.UNKNOWN
                and not update.actions
                and update.order_id
                is None
            ):

                if (
                    pending
                    is not None
                ):

                    print(
                        "\nAI:",
                        question_for(
                            state,
                            pending,
                        ),
                    )

                else:

                    print(
                        "\nAI: 말씀을 이해하지 못했습니다. "
                        "다시 말씀해 주세요."
                    )

                continue

            # =========================
            # APPLY
            # =========================

            error = (
                apply_update(
                    state,
                    update,
                    pending,
                )
            )

            if error is not None:

                print(
                    "\nAI:",
                    error,
                )

                continue

            print_state(
                state
            )

            # =========================
            # CANCEL
            # =========================

            if (
                state["intent"]
                == "cancel"
            ):

                print(
                    "\nAI: 주문을 취소했습니다."
                )

                break

            # =========================
            # NEXT MISSING FIELD
            # =========================

            pending = (
                find_missing(
                    state
                )
            )

            if (
                pending
                is not None
            ):

                print(
                    "\nAI:",
                    question_for(
                        state,
                        pending,
                    ),
                )

                continue

            # =========================
            # READY
            # =========================

            if (
                state["intent"]
                == "mobile_pickup"
            ):

                print(
                    "\nAI: 모바일 주문번호를 확인했습니다. "
                    "픽업을 진행할까요?"
                )

            elif state["items"]:

                print(
                    "\nAI: 주문 내용을 확인했습니다. "
                    "이대로 주문을 확정할까요?"
                )

            else:

                print(
                    "\nAI: 어떤 메뉴를 주문하시겠습니까?"
                )

        except ValidationError as error:

            print(
                "\n[Pydantic 검증 실패]"
            )

            print(
                error
            )

        except Exception as error:

            print(
                "\n[오류]"
            )

            print(
                error
            )


if __name__ == "__main__":
    main()