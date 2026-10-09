#!/usr/bin/env python3

from checkout_manager import (
    BURGER_BASE_PRICE,
    SET_UPCHARGE,
    STANDALONE_DRINK_PRICE,
    DRINK_SIZE_UPCHARGE,
    STANDALONE_SIDE_PRICE,
)


# ============================================================
# MENU DATABASE
# ============================================================

BURGERS = {
    "bulgogi_burger": {
        "name": "불고기버거",
        "calories": 520,
        "spicy": False,
        "ingredients": [
            "patty",
            "lettuce",
            "tomato",
            "onion",
            "pickle",
            "sauce",
            "bulgogi_sauce",
        ],
        "description": "맵지 않고 무난하게 먹기 좋은 편",
    },
    "chicken_burger": {
        "name": "치킨버거",
        "calories": 560,
        "spicy": True,
        "ingredients": [
            "chicken_patty",
            "lettuce",
            "tomato",
            "onion",
            "pickle",
            "sauce",
        ],
        "description": "살짝 매콤한 맛이 특징",
    },
    "cheese_burger": {
        "name": "치즈버거",
        "calories": 600,
        "spicy": False,
        "ingredients": [
            "patty",
            "lettuce",
            "tomato",
            "onion",
            "pickle",
            "sauce",
            "cheese",
        ],
        "description": "치즈가 들어가고 맵지 않은 메뉴",
    },
    "shrimp_burger": {
        "name": "새우버거",
        "calories": 540,
        "spicy": False,
        "ingredients": [
            "shrimp_patty",
            "lettuce",
            "tomato",
            "onion",
            "pickle",
            "sauce",
        ],
        "description": "맵지 않고 비교적 가볍게 먹기 좋은 편",
    },
}


DRINKS = {
    "coke": {
        "name": "콜라",
        "calories": {
            "small": 140,
            "medium": 190,
            "large": 250,
        },
    },
    "zero_coke": {
        "name": "제로콜라",
        "calories": {
            "small": 0,
            "medium": 0,
            "large": 0,
        },
    },
    "sprite": {
        "name": "스프라이트",
        "calories": {
            "small": 135,
            "medium": 185,
            "large": 245,
        },
    },
    "fanta": {
        "name": "환타",
        "calories": {
            "small": 150,
            "medium": 205,
            "large": 270,
        },
    },
    "iced_coffee": {
        "name": "아이스커피",
        "calories": {
            "small": 90,
            "medium": 120,
            "large": 160,
        },
    },
}


SIDES = {
    "french_fries": {
        "name": "감자튀김",
        "calories": 320,
    },
    "cheese_stick": {
        "name": "치즈스틱",
        "calories": 290,
    },
}


SAUCES = [
    "케첩",
    "머스타드",
]


INGREDIENT_NAMES = {
    "patty": "패티",
    "chicken_patty": "치킨패티",
    "shrimp_patty": "새우패티",
    "onion": "양파",
    "sauce": "소스",
    "bulgogi_sauce": "불고기 소스",
    "pickle": "피클",
    "lettuce": "양상추",
    "tomato": "토마토",
    "cheese": "치즈",
}


BURGER_ALIASES = {
    "bulgogi_burger": (
        "불고기버거",
        "불고기 버거",
    ),
    "chicken_burger": (
        "치킨버거",
        "치킨 버거",
    ),
    "cheese_burger": (
        "치즈버거",
        "치즈 버거",
    ),
    "shrimp_burger": (
        "새우버거",
        "새우 버거",
    ),
}


DRINK_ALIASES = {
    "zero_coke": (
        "제로콜라",
        "제로 콜라",
        "콜라제로",
        "콜라 제로",
    ),
    "coke": (
        "콜라",
        "코카콜라",
    ),
    "sprite": (
        "스프라이트",
        "사이다",
    ),
    "fanta": (
        "환타",
        "판타",
    ),
    "iced_coffee": (
        "아이스커피",
        "아이스 커피",
        "아아",
    ),
}


SIDE_ALIASES = {
    "french_fries": (
        "감자튀김",
        "감자 튀김",
        "감튀",
    ),
    "cheese_stick": (
        "치즈스틱",
        "치즈 스틱",
    ),
}


INGREDIENT_ALIASES = {
    "pickle": (
        "피클",
    ),
    "lettuce": (
        "양상추",
        "상추",
    ),
    "tomato": (
        "토마토",
    ),
    "cheese": (
        "치즈",
    ),
}


SIZE_NAMES = {
    "small": "스몰",
    "medium": "미디엄",
    "large": "라지",
}


SIZE_ALIASES = {
    "small": (
        "스몰",
        "작은 사이즈",
        "작은사이즈",
        "작은 걸",
        "작은거",
        "작은 거",
    ),
    "medium": (
        "미디엄",
        "미디움",
        "중간 사이즈",
        "중간사이즈",
    ),
    "large": (
        "라지",
        "큰 사이즈",
        "큰사이즈",
        "큰 걸",
        "큰거",
        "큰 거",
    ),
}


GENERAL_BURGER_ORDER = [
    "bulgogi_burger",
    "shrimp_burger",
    "chicken_burger",
    "cheese_burger",
]


# ============================================================
# BASIC HELPERS
# ============================================================

def _value(value):
    return getattr(
        value,
        "value",
        value,
    )


def _criteria_value(
    criteria,
    key,
    default=None,
):

    if criteria is None:
        return default

    if isinstance(criteria, dict):
        return criteria.get(
            key,
            default,
        )

    return getattr(
        criteria,
        key,
        default,
    )


def _criteria_list(
    criteria,
    key,
):

    value = _criteria_value(
        criteria,
        key,
        [],
    )

    if not value:
        return []

    return [
        str(_value(x))
        for x in value
    ]


def _contains_any(
    text,
    phrases,
):
    return any(
        phrase in text
        for phrase in phrases
    )


def _detect_from_aliases(
    text,
    aliases,
):

    results = []

    for key, words in aliases.items():
        if any(
            word in text
            for word in words
        ):
            results.append(key)

    return results


def _detect_burgers(text):
    return _detect_from_aliases(
        text,
        BURGER_ALIASES,
    )


def _detect_drinks(text):
    # zero_coke가 coke보다 먼저 잡히도록
    # 선언 순서를 유지한다.
    return _detect_from_aliases(
        text,
        DRINK_ALIASES,
    )


def _detect_sides(text):
    return _detect_from_aliases(
        text,
        SIDE_ALIASES,
    )


def _detect_ingredient(text):

    for key, aliases in (
        INGREDIENT_ALIASES.items()
    ):
        if any(
            alias in text
            for alias in aliases
        ):
            return key

    return None


def _detect_size(text):

    for key, aliases in (
        SIZE_ALIASES.items()
    ):
        if any(
            alias in text
            for alias in aliases
        ):
            return key

    return None


def _resolve_target(
    target,
    text,
):

    target = _value(target)

    if (
        target in BURGERS
        or target in DRINKS
        or target in SIDES
        or target in INGREDIENT_NAMES
    ):
        return target

    burgers = _detect_burgers(text)

    if len(burgers) == 1:
        return burgers[0]

    drinks = _detect_drinks(text)

    if len(drinks) == 1:
        return drinks[0]

    sides = _detect_sides(text)

    if len(sides) == 1:
        return sides[0]

    ingredient = _detect_ingredient(
        text
    )

    if ingredient is not None:
        return ingredient

    return None


def _infer_domain(
    target_domain,
    target,
    text,
):

    domain = _value(
        target_domain
    )

    if domain:
        return str(domain)

    if target in BURGERS:
        return "burger"

    if target in DRINKS:
        return "drink"

    if target in SIDES:
        return "side"

    if target in INGREDIENT_NAMES:
        return "ingredient"

    if (
        "음료" in text
        or "음료수" in text
        or _detect_drinks(text)
    ):
        return "drink"

    if (
        "사이드" in text
        or _detect_sides(text)
    ):
        return "side"

    if (
        "버거" in text
        or "햄버거" in text
        or _detect_burgers(text)
    ):
        return "burger"

    return "menu"


def _burger_price(
    key,
    burger_prices=None,
):

    if (
        burger_prices
        and key in burger_prices
    ):
        return int(
            burger_prices[key]
        )

    return int(
        BURGER_BASE_PRICE[key]
    )


def _drink_price(
    key,
    size,
):
    return int(
        STANDALONE_DRINK_PRICE[key]
        + DRINK_SIZE_UPCHARGE[size]
    )


def _natural_names(names):

    if not names:
        return ""

    if len(names) == 1:
        return names[0]

    if len(names) == 2:
        return (
            f"{names[0]}와 "
            f"{names[1]}"
        )

    return (
        ", ".join(names[:-1])
        + f", {names[-1]}"
    )


# ============================================================
# UNKNOWN / UNSUPPORTED FACT GUARD
# ============================================================

def _unknown_fact_reply(
    subtype,
    text,
):

    if subtype == "allergen":
        return (
            "현재 확정된 알레르기 정보가 "
            "등록되어 있지 않아서 "
            "정확하게 안내드리기 어려워요."
        )

    if subtype == "stock":
        return (
            "현재 실시간 재고 정보는 "
            "연동되어 있지 않아요."
        )

    if _contains_any(
        text,
        (
            "나트륨",
            "단백질",
            "탄수화물",
            "당류",
            "지방",
            "콜레스테롤",
            "카페인",
        ),
    ):
        return (
            "현재 등록된 영양정보는 "
            "칼로리뿐이라서 "
            "그 수치는 정확하게 "
            "안내드리기 어려워요."
        )

    if _contains_any(
        text,
        (
            "제일 많이 팔",
            "가장 많이 팔",
            "판매량",
            "판매 순위",
            "인기 순위",
        ),
    ):
        return (
            "현재 판매량 정보는 "
            "연동되어 있지 않아서 "
            "가장 많이 팔린 메뉴는 "
            "정확하게 안내드리기 어려워요."
        )

    if _contains_any(
        text,
        (
            "원산지",
            "누가 만들",
            "만든 사람",
        ),
    ):
        return (
            "그 정보는 현재 메뉴 데이터에 "
            "등록되어 있지 않아요."
        )

    return None


# ============================================================
# BURGER FACTS
# ============================================================

def _burger_overview(
    key,
    burger_prices,
    text,
):

    data = BURGERS[key]

    name = data["name"]

    price = _burger_price(
        key,
        burger_prices,
    )

    kcal = data["calories"]

    if _contains_any(
        text,
        (
            "맛있",
            "어때",
            "괜찮",
            "추천할 만",
        ),
    ):

        if key == "chicken_burger":
            return (
                "취향에 따라 다르지만, "
                "살짝 매콤한 맛을 좋아하시면 "
                f"{name}가 괜찮아요. "
                f"{price:,}원이고 "
                f"{kcal} kcal예요."
            )

        if key == "cheese_burger":
            return (
                "치즈 맛을 좋아하시면 "
                f"{name}가 잘 맞을 거예요. "
                "맵지는 않고 "
                f"{price:,}원, "
                f"{kcal} kcal예요."
            )

        if key == "shrimp_burger":
            return (
                "취향에 따라 다르지만, "
                "맵지 않고 비교적 가볍게 "
                f"드시려면 {name}도 괜찮아요. "
                f"{price:,}원이고 "
                f"{kcal} kcal예요."
            )

        return (
            "맵지 않고 무난한 메뉴를 "
            f"원하시면 {name}가 괜찮아요. "
            f"{price:,}원이고 "
            f"{kcal} kcal예요."
        )

    spicy_text = (
        "살짝 매콤한 편"
        if data["spicy"]
        else "맵지 않은 메뉴"
    )

    return (
        f"{name}는 "
        f"{data['description']}이에요. "
        f"{price:,}원이고 "
        f"{kcal} kcal, "
        f"{spicy_text}이에요."
    )


def _burger_ingredient_reply(
    key,
    text,
):

    data = BURGERS[key]

    name = data["name"]

    ingredient = _detect_ingredient(
        text
    )

    if ingredient is not None:

        label = INGREDIENT_NAMES[
            ingredient
        ]

        if ingredient in data[
            "ingredients"
        ]:
            return (
                f"네, {name}에는 "
                f"{label}가 들어가요."
            )

        return (
            f"아니요, {name}에는 "
            f"{label}가 들어가지 않아요."
        )

    labels = [
        INGREDIENT_NAMES[x]
        for x in data[
            "ingredients"
        ]
    ]

    return (
        f"{name}에는 "
        f"{_natural_names(labels)}가 "
        "기본으로 들어가요."
    )


def _burger_spicy_reply(
    key,
    text,
):

    data = BURGERS[key]

    name = data["name"]

    if data["spicy"]:

        if _contains_any(
            text,
            (
                "많이",
                "엄청",
                "혀",
                "매우",
            ),
        ):
            return (
                "그 정도로 맵진 않고요, "
                f"{name}는 살짝 "
                "매콤한 편이에요."
            )

        return (
            f"{name}는 살짝 "
            "매콤한 편이에요."
        )

    return (
        f"아니요, {name}는 "
        "맵지 않아요."
    )


# ============================================================
# NUTRITION
# ============================================================

def _nutrition_reply(
    target,
    domain,
    text,
):

    burgers = _detect_burgers(
        text
    )

    drinks = _detect_drinks(
        text
    )

    sides = _detect_sides(
        text
    )

    size = _detect_size(
        text
    )

    # --------------------------------------------------------
    # Burger comparison
    # --------------------------------------------------------

    if len(burgers) >= 2:

        rows = [
            (
                key,
                BURGERS[key][
                    "calories"
                ],
            )
            for key in burgers
        ]

        lowest = min(
            rows,
            key=lambda x: x[1],
        )

        highest = max(
            rows,
            key=lambda x: x[1],
        )

        if _contains_any(
            text,
            (
                "낮",
                "적",
                "가벼",
            ),
        ):
            return (
                f"{BURGERS[lowest[0]]['name']}가 "
                f"{lowest[1]} kcal로 더 낮아요."
            )

        if _contains_any(
            text,
            (
                "높",
                "많",
                "헤비",
            ),
        ):
            return (
                f"{BURGERS[highest[0]]['name']}가 "
                f"{highest[1]} kcal로 더 높아요."
            )

        return (
            " / ".join(
                f"{BURGERS[key]['name']} "
                f"{kcal} kcal"
                for key, kcal in rows
            )
            + "예요."
        )

    # --------------------------------------------------------
    # Drink comparison
    # --------------------------------------------------------

    if len(drinks) >= 2:

        compare_size = (
            size or "medium"
        )

        rows = [
            (
                key,
                DRINKS[key][
                    "calories"
                ][compare_size],
            )
            for key in drinks
        ]

        lowest = min(
            rows,
            key=lambda x: x[1],
        )

        highest = max(
            rows,
            key=lambda x: x[1],
        )

        size_name = SIZE_NAMES[
            compare_size
        ]

        if _contains_any(
            text,
            (
                "낮",
                "적",
                "가벼",
            ),
        ):
            return (
                f"{size_name} 기준으로 "
                f"{DRINKS[lowest[0]]['name']}가 "
                f"{lowest[1]} kcal로 더 낮아요."
            )

        if _contains_any(
            text,
            (
                "높",
                "많",
            ),
        ):
            return (
                f"{size_name} 기준으로 "
                f"{DRINKS[highest[0]]['name']}가 "
                f"{highest[1]} kcal로 더 높아요."
            )

    # --------------------------------------------------------
    # Side comparison
    # --------------------------------------------------------

    if len(sides) >= 2:

        rows = [
            (
                key,
                SIDES[key][
                    "calories"
                ],
            )
            for key in sides
        ]

        lowest = min(
            rows,
            key=lambda x: x[1],
        )

        highest = max(
            rows,
            key=lambda x: x[1],
        )

        if _contains_any(
            text,
            (
                "낮",
                "적",
                "가벼",
            ),
        ):
            return (
                f"{SIDES[lowest[0]]['name']}이 "
                f"{lowest[1]} kcal로 더 낮아요."
            )

        if _contains_any(
            text,
            (
                "높",
                "많",
            ),
        ):
            return (
                f"{SIDES[highest[0]]['name']}이 "
                f"{highest[1]} kcal로 더 높아요."
            )

    # --------------------------------------------------------
    # Specific burger
    # --------------------------------------------------------

    if target in BURGERS:

        data = BURGERS[target]

        return (
            f"{data['name']}는 "
            f"{data['calories']} kcal예요."
        )

    # --------------------------------------------------------
    # Specific drink
    # --------------------------------------------------------

    if target in DRINKS:

        data = DRINKS[target]

        if size is not None:
            return (
                f"{data['name']} "
                f"{SIZE_NAMES[size]}은 "
                f"{data['calories'][size]} kcal예요."
            )

        return (
            f"{data['name']}은 "
            f"스몰 {data['calories']['small']} kcal, "
            f"미디엄 {data['calories']['medium']} kcal, "
            f"라지 {data['calories']['large']} kcal예요."
        )

    # --------------------------------------------------------
    # Specific side
    # --------------------------------------------------------

    if target in SIDES:

        data = SIDES[target]

        return (
            f"{data['name']}은 "
            f"{data['calories']} kcal예요."
        )

    # --------------------------------------------------------
    # Whole burger menu
    # --------------------------------------------------------

    if domain == "burger":

        rows = sorted(
            BURGERS.items(),
            key=lambda x: (
                x[1]["calories"]
            ),
        )

        if _contains_any(
            text,
            (
                "제일 낮",
                "가장 낮",
                "제일 적",
                "가장 적",
                "제일 가벼",
                "가장 가벼",
            ),
        ):
            key, data = rows[0]

            return (
                f"{data['name']}가 "
                "가장 칼로리가 낮아요. "
                f"{data['calories']} kcal예요."
            )

        if _contains_any(
            text,
            (
                "제일 높",
                "가장 높",
                "제일 많",
                "가장 많",
            ),
        ):
            key, data = rows[-1]

            return (
                f"{data['name']}가 "
                "가장 칼로리가 높아요. "
                f"{data['calories']} kcal예요."
            )

        return (
            "버거 칼로리는 "
            + ", ".join(
                f"{data['name']} "
                f"{data['calories']} kcal"
                for _, data in rows
            )
            + "예요."
        )

    # --------------------------------------------------------
    # Whole drink menu
    # --------------------------------------------------------

    if domain == "drink":

        if _contains_any(
            text,
            (
                "제일 낮",
                "가장 낮",
                "제일 적",
                "가장 적",
            ),
        ):
            return (
                "제로콜라가 가장 낮아요. "
                "모든 사이즈가 0 kcal예요."
            )

        return (
            "음료 칼로리는 사이즈에 따라 달라요. "
            "콜라 140~250 kcal, "
            "제로콜라 0 kcal, "
            "스프라이트 135~245 kcal, "
            "환타 150~270 kcal, "
            "아이스커피 90~160 kcal예요."
        )

    # --------------------------------------------------------
    # Whole side menu
    # --------------------------------------------------------

    if domain == "side":

        if _contains_any(
            text,
            (
                "제일 낮",
                "가장 낮",
                "제일 적",
                "가장 적",
                "가벼",
            ),
        ):
            return (
                "치즈스틱이 290 kcal로 "
                "감자튀김보다 조금 낮아요."
            )

        return (
            "감자튀김은 320 kcal, "
            "치즈스틱은 290 kcal예요."
        )

    return None


# ============================================================
# PRICE
# ============================================================

def _price_reply(
    target,
    domain,
    text,
    burger_prices,
):

    size = _detect_size(
        text
    )

    burgers = _detect_burgers(
        text
    )

    drinks = _detect_drinks(
        text
    )

    sides = _detect_sides(
        text
    )

    # --------------------------------------------------------
    # Burger comparison
    # --------------------------------------------------------

    if len(burgers) >= 2:

        rows = [
            (
                key,
                _burger_price(
                    key,
                    burger_prices,
                ),
            )
            for key in burgers
        ]

        low = min(
            rows,
            key=lambda x: x[1],
        )

        high = max(
            rows,
            key=lambda x: x[1],
        )

        if _contains_any(
            text,
            (
                "싸",
                "저렴",
                "낮",
            ),
        ):
            return (
                f"{BURGERS[low[0]]['name']}가 "
                f"{low[1]:,}원으로 더 저렴해요."
            )

        if _contains_any(
            text,
            (
                "비싸",
                "높",
            ),
        ):
            return (
                f"{BURGERS[high[0]]['name']}가 "
                f"{high[1]:,}원으로 더 비싸요."
            )

    # --------------------------------------------------------
    # Specific burger
    # --------------------------------------------------------

    if target in BURGERS:

        name = BURGERS[target][
            "name"
        ]

        base = _burger_price(
            target,
            burger_prices,
        )

        if "세트" in text:

            basic_set = (
                base
                + SET_UPCHARGE
            )

            return (
                f"{name} 기본 세트는 "
                f"{basic_set:,}원부터예요. "
                "음료 사이즈나 사이드 변경에 따라 "
                "추가금이 붙을 수 있어요."
            )

        return (
            f"{name} 단품은 "
            f"{base:,}원이에요."
        )

    # --------------------------------------------------------
    # Specific drink
    # --------------------------------------------------------

    if target in DRINKS:

        name = DRINKS[target][
            "name"
        ]

        if size is not None:

            return (
                f"{name} "
                f"{SIZE_NAMES[size]}은 "
                f"{_drink_price(target, size):,}원이에요."
            )

        return (
            f"{name}은 "
            f"스몰 {_drink_price(target, 'small'):,}원, "
            f"미디엄 {_drink_price(target, 'medium'):,}원, "
            f"라지 {_drink_price(target, 'large'):,}원이에요."
        )

    # --------------------------------------------------------
    # Specific side
    # --------------------------------------------------------

    if target in SIDES:

        return (
            f"{SIDES[target]['name']}은 "
            f"{STANDALONE_SIDE_PRICE[target]:,}원이에요."
        )

    # --------------------------------------------------------
    # Whole menus
    # --------------------------------------------------------

    if domain == "burger":
        return (
            "버거 단품 가격은 "
            + ", ".join(
                f"{BURGERS[key]['name']} "
                f"{_burger_price(key, burger_prices):,}원"
                for key in BURGERS
            )
            + "이에요."
        )

    if domain == "side":
        return (
            "감자튀김은 2,000원, "
            "치즈스틱은 2,500원이에요."
        )

    if domain == "drink":
        return (
            "일반 탄산음료는 "
            "스몰 2,000원, 미디엄 2,300원, "
            "라지 2,700원이에요. "
            "아이스커피는 각 사이즈에서 "
            "500원씩 더 비싸요."
        )

    return None


# ============================================================
# RECOMMENDATION
# ============================================================

def _burger_candidates(
    criteria,
    burger_prices,
):

    candidates = list(
        BURGERS.keys()
    )

    budget = _criteria_value(
        criteria,
        "budget_max",
    )

    spicy = _value(
        _criteria_value(
            criteria,
            "spicy_preference",
        )
    )

    calorie_pref = _value(
        _criteria_value(
            criteria,
            "calorie_preference",
        )
    )

    calorie_max = _criteria_value(
        criteria,
        "calorie_max",
    )

    includes = _criteria_list(
        criteria,
        "include_ingredients",
    )

    excludes = _criteria_list(
        criteria,
        "exclude_ingredients",
    )

    if budget is not None:
        candidates = [
            key
            for key in candidates
            if _burger_price(
                key,
                burger_prices,
            ) <= int(budget)
        ]

    if spicy == "not_spicy":
        candidates = [
            key
            for key in candidates
            if not BURGERS[key][
                "spicy"
            ]
        ]

    elif spicy == "spicy":
        candidates = [
            key
            for key in candidates
            if BURGERS[key][
                "spicy"
            ]
        ]

    if calorie_max is not None:
        candidates = [
            key
            for key in candidates
            if BURGERS[key][
                "calories"
            ] <= int(
                calorie_max
            )
        ]

    for ingredient in includes:
        candidates = [
            key
            for key in candidates
            if ingredient
            in BURGERS[key][
                "ingredients"
            ]
        ]

    for ingredient in excludes:
        candidates = [
            key
            for key in candidates
            if ingredient
            not in BURGERS[key][
                "ingredients"
            ]
        ]

    if (
        calorie_pref == "low"
        or calorie_max is not None
    ):
        candidates.sort(
            key=lambda key: (
                BURGERS[key][
                    "calories"
                ],
                _burger_price(
                    key,
                    burger_prices,
                ),
            )
        )

    elif budget is not None:
        candidates.sort(
            key=lambda key: (
                _burger_price(
                    key,
                    burger_prices,
                ),
                BURGERS[key][
                    "calories"
                ],
            )
        )

    else:
        rank = {
            key: index
            for index, key in enumerate(
                GENERAL_BURGER_ORDER
            )
        }

        candidates.sort(
            key=lambda key: rank[
                key
            ]
        )

    return candidates


def _recommendation_reply(
    subtype,
    domain,
    target,
    text,
    criteria,
    burger_prices,
):

    # --------------------------------------------------------
    # Drink
    # --------------------------------------------------------

    if (
        domain == "drink"
        or (
            "음료" in text
            and "버거" not in text
            and "조합" not in text
        )
    ):

        if _contains_any(
            text,
            (
                "제로 말고",
                "제로말고",
                "제로 빼고",
            ),
        ):
            return (
                "제로콜라를 제외하면 "
                "아이스커피가 가장 가벼워요. "
                "스몰 기준 90 kcal예요."
            )

        if _contains_any(
            text,
            (
                "칼로리",
                "가볍",
                "부담",
                "낮",
            ),
        ):
            return (
                "칼로리를 신경 쓰시면 "
                "제로콜라가 가장 좋아요. "
                "사이즈와 상관없이 0 kcal예요."
            )

        return (
            "무난하게는 제로콜라를 추천드릴게요. "
            "칼로리가 0이라 부담도 적어요."
        )

    # --------------------------------------------------------
    # Side
    # --------------------------------------------------------

    if (
        domain == "side"
        or (
            "사이드" in text
            and "버거" not in text
            and "조합" not in text
        )
    ):

        if _contains_any(
            text,
            (
                "칼로리",
                "가볍",
                "부담",
                "낮",
            ),
        ):
            return (
                "칼로리만 보면 "
                "치즈스틱이 290 kcal로 "
                "감자튀김 320 kcal보다 조금 낮아요."
            )

        return (
            "무난하게 드시려면 감자튀김이 괜찮고, "
            "칼로리만 보면 치즈스틱이 조금 더 낮아요."
        )

    # --------------------------------------------------------
    # Burger / mixed
    # --------------------------------------------------------

    # "다른 거 추천해줘"처럼 직전 추천을 거절한 경우,
    # Router가 conversation history로 잡아준 직전 target은
    # 다시 추천하지 않는다.
    other_requested = _contains_any(
        text,
        (
            "다른 거",
            "다른거",
            "다른 걸",
            "다른걸",
            "다른 메뉴",
            "다른 걸로",
            "다른걸로",
            "말고 다른",
        ),
    )

    previous_recommendation = (
        target
        if (
            other_requested
            and target in BURGERS
        )
        else None
    )

    # "새우버거는 어때?"처럼 사용자가 특정 burger를
    # 직접 물어본 경우에는 그 burger를 설명한다.
    #
    # 단, "그거 말고 다른 거 추천해줘"는
    # 직전 target을 설명하면 안 된다.
    if (
        target in BURGERS
        and not other_requested
    ):

        data = BURGERS[target]

        price = _burger_price(
            target,
            burger_prices,
        )

        description = (
            data.get("description")
            or "현재 시연 메뉴"
        )

        calories = data.get(
            "calories"
        )

        details = []

        if price is not None:
            details.append(
                f"{price:,}원"
            )

        if calories is not None:
            details.append(
                f"{calories} kcal"
            )

        suffix = (
            " "
            + "이고 ".join(details)
            + "예요."
            if details
            else ""
        )

        return (
            f"{data['name']}는 "
            f"{description}이에요."
            + suffix
        )

    candidates = _burger_candidates(
        criteria,
        burger_prices,
    )

    if previous_recommendation is not None:
        candidates = [
            key
            for key in candidates
            if key != previous_recommendation
        ]

    if not candidates:
        return (
            "말씀하신 조건에 딱 맞는 "
            "버거는 없어요. "
            "조건을 하나만 바꿔주시면 "
            "다시 골라드릴게요."
        )

    top = candidates[0]

    data = BURGERS[top]

    price = _burger_price(
        top,
        burger_prices,
    )

    # 버거 + 음료 조합
    if (
        "버거" in text
        and (
            "음료" in text
            or "음료수" in text
        )
    ):
        return (
            f"{data['name']}에 "
            "제로콜라 스몰 조합을 추천드릴게요. "
            f"버거는 {data['calories']} kcal, "
            "제로콜라는 0 kcal예요."
        )

    # 버거 + 음료 + 사이드 조합
    if (
        _contains_any(
            text,
            (
                "조합",
                "사이드까지",
                "사이드랑 음료",
                "음료랑 사이드",
            ),
        )
    ):
        return (
            f"가볍게 드시려면 "
            f"{data['name']}, 제로콜라, "
            "치즈스틱 조합이 괜찮아요. "
            f"각각 {data['calories']} kcal, "
            "0 kcal, 290 kcal예요."
        )

    wants_list = _contains_any(
        text,
        (
            "뭐뭐",
            "어떤 거",
            "어떤게",
            "선택지",
            "뭐 있어",
            "뭐가 있어",
            "먹을 수 있어",
            "가능한 메뉴",
        ),
    )

    if wants_list and len(
        candidates
    ) > 1:

        names = [
            BURGERS[key][
                "name"
            ]
            for key in candidates
        ]

        return (
            f"조건에 맞는 메뉴는 "
            f"{_natural_names(names)}예요. "
            f"그중에서는 {data['name']}를 "
            "먼저 추천드릴게요."
        )

    reasons = []

    spicy = _value(
        _criteria_value(
            criteria,
            "spicy_preference",
        )
    )

    calorie_pref = _value(
        _criteria_value(
            criteria,
            "calorie_preference",
        )
    )

    calorie_max = _criteria_value(
        criteria,
        "calorie_max",
    )

    budget = _criteria_value(
        criteria,
        "budget_max",
    )

    excludes = _criteria_list(
        criteria,
        "exclude_ingredients",
    )

    if (
        calorie_pref == "low"
        or calorie_max is not None
    ):
        reasons.append(
            f"{data['calories']} kcal로 "
            "비교적 가벼운 편"
        )

    if spicy == "not_spicy":
        reasons.append(
            "맵지 않은 메뉴"
        )

    if budget is not None:
        reasons.append(
            f"{price:,}원으로 "
            "예산 안에 들어오는 메뉴"
        )

    if "cheese" in excludes:
        reasons.append(
            "치즈가 들어가지 않는 메뉴"
        )

    if not reasons:

        reasons.append(
            data[
                "description"
            ]
        )

    return (
        f"{data['name']} 추천드릴게요. "
        f"{reasons[0]}이라 괜찮아요."
    )


# ============================================================
# MAIN RESOLVER
# ============================================================

def answer_menu_query(
    *,
    family,
    subtype,
    target=None,
    target_domain=None,
    utterance="",
    burger_prices=None,
    criteria=None,
):

    family = str(
        _value(family)
        or ""
    )

    subtype = str(
        _value(subtype)
        or ""
    )

    text = str(
        utterance or ""
    ).strip()

    target = _resolve_target(
        target,
        text,
    )

    domain = _infer_domain(
        target_domain,
        target,
        text,
    )

    # ========================================================
    # Only menu knowledge requests
    # ========================================================

    if family not in {
        "info_query",
        "recommendation",
    }:
        return None

    # ========================================================
    # Never hallucinate unknown facts
    # ========================================================

    unknown = _unknown_fact_reply(
        subtype,
        text,
    )

    if unknown is not None:
        return unknown

    # ========================================================
    # Recommendation
    # ========================================================

    if family == "recommendation":

        return _recommendation_reply(
            subtype,
            domain,
            target,
            text,
            criteria,
            burger_prices,
        )

    # ========================================================
    # INFO QUERY
    # ========================================================

    # --------------------------------------------------------
    # Nutrition
    # --------------------------------------------------------

    if (
        subtype == "nutrition"
        or _contains_any(
            text,
            (
                "칼로리",
                "열량",
                "kcal",
            ),
        )
    ):
        reply = _nutrition_reply(
            target,
            domain,
            text,
        )

        if reply is not None:
            return reply

    # --------------------------------------------------------
    # Price
    # --------------------------------------------------------

    if (
        subtype == "price"
        or _contains_any(
            text,
            (
                "가격",
                "얼마",
                "몇 원",
                "비싸",
                "싸",
            ),
        )
    ):
        reply = _price_reply(
            target,
            domain,
            text,
            burger_prices,
        )

        if reply is not None:
            return reply

    # --------------------------------------------------------
    # Spiciness
    # --------------------------------------------------------

    if (
        target in BURGERS
        and _contains_any(
            text,
            (
                "맵",
                "매워",
                "매운",
                "매콤",
            ),
        )
    ):
        return _burger_spicy_reply(
            target,
            text,
        )

    # --------------------------------------------------------
    # Sauce
    # --------------------------------------------------------

    if _contains_any(
        text,
        (
            "소스",
            "케첩",
            "머스타드",
        ),
    ):

        if (
            subtype == "possibility"
            and _contains_any(
                text,
                (
                    "빼",
                    "제외",
                    "없이",
                ),
            )
        ):
            return (
                "현재 시연 주문 옵션에는 "
                "소스 제외가 별도 항목으로 "
                "등록되어 있지 않아요."
            )

        return (
            "현재 등록된 버거 소스는 "
            "케첩과 머스타드예요."
        )

    # --------------------------------------------------------
    # Ingredient
    # --------------------------------------------------------

    if (
        subtype == "ingredient"
        or (
            target in BURGERS
            and _detect_ingredient(
                text
            )
            is not None
        )
    ):

        if target in BURGERS:
            return _burger_ingredient_reply(
                target,
                text,
            )

        if _contains_any(
            text,
            (
                "기본 재료",
                "기본재료",
                "야채",
                "뭐 들어",
                "재료",
            ),
        ):
            return (
                "모든 버거에는 기본으로 "
                "피클, 양상추, 토마토가 들어가고, "
                "치즈는 치즈버거에만 들어가요."
            )

    # --------------------------------------------------------
    # Possibility / removal
    # --------------------------------------------------------

    if subtype == "possibility":

        ingredient = (
            target
            if target
            in INGREDIENT_NAMES
            else _detect_ingredient(
                text
            )
        )

        if (
            ingredient
            in INGREDIENT_NAMES
            and _contains_any(
                text,
                (
                    "빼",
                    "제외",
                    "없이",
                ),
            )
        ):
            return (
                f"네, "
                f"{INGREDIENT_NAMES[ingredient]} "
                "빼고 주문하실 수 있어요."
            )

        if target in BURGERS:
            return (
                f"네, "
                f"{BURGERS[target]['name']} "
                "주문 가능해요."
            )

        if target in DRINKS:
            return (
                f"네, "
                f"{DRINKS[target]['name']} "
                "주문 가능해요."
            )

        if target in SIDES:
            return (
                f"네, "
                f"{SIDES[target]['name']} "
                "주문 가능해요."
            )

    # --------------------------------------------------------
    # Options
    # --------------------------------------------------------

    if subtype == "option":

        if domain == "drink":
            return (
                "음료는 콜라, 제로콜라, "
                "스프라이트, 환타, "
                "아이스커피 중에서 고를 수 있어요."
            )

        if domain == "side":
            return (
                "사이드는 감자튀김과 "
                "치즈스틱 중에서 고를 수 있어요."
            )

        if _contains_any(
            text,
            (
                "사이즈",
                "크기",
            ),
        ):
            return (
                "음료 사이즈는 "
                "스몰, 미디엄, 라지가 있어요."
            )

        return (
            "버거는 단품 또는 세트로 "
            "선택할 수 있어요."
        )

    # --------------------------------------------------------
    # Specific menu overview / subjective question
    # --------------------------------------------------------

    if subtype == "menu":

        if target in BURGERS:
            return _burger_overview(
                target,
                burger_prices,
                text,
            )

        if target in DRINKS:

            data = DRINKS[target]

            return (
                f"{data['name']}은 "
                f"스몰 {data['calories']['small']} kcal, "
                f"미디엄 {data['calories']['medium']} kcal, "
                f"라지 {data['calories']['large']} kcal예요."
            )

        if target in SIDES:

            data = SIDES[target]

            return (
                f"{data['name']}은 "
                f"{data['calories']} kcal이고 "
                f"{STANDALONE_SIDE_PRICE[target]:,}원이에요."
            )

        if domain == "burger":
            return (
                "버거는 불고기버거, 치킨버거, "
                "치즈버거, 새우버거가 있어요."
            )

        if domain == "drink":
            return (
                "음료는 콜라, 제로콜라, "
                "스프라이트, 환타, "
                "아이스커피가 있어요."
            )

        if domain == "side":
            return (
                "사이드는 감자튀김과 "
                "치즈스틱이 있어요."
            )

    return None
