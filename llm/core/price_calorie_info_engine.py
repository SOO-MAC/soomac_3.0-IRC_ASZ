#!/usr/bin/env python3

import re

from checkout_manager import (
    BURGER_BASE_PRICE,
    SET_UPCHARGE,
    STANDALONE_DRINK_PRICE,
    DRINK_SIZE_UPCHARGE,
    STANDALONE_SIDE_PRICE,
    TOPPING_PRICE,
)

from menu_knowledge import (
    BURGERS,
    DRINKS,
    SIDES,
)

from order_runtime_final import (
    MENU_ALIASES,
    DRINK_ALIASES,
    SIDE_ALIASES,
)


# ============================================================
# INTERNAL KCAL DELTAS
# 시연용 주문 총 kcal 계산에 사용하는 내부 기준.
# ============================================================

TOPPING_KCAL = {
    "cheese": 60,
    "bacon": 90,
    "tomato": 10,
}

BASE_INGREDIENT_KCAL = {
    "pickle": 5,
    "lettuce": 5,
    "tomato": 10,
    "cheese": 60,
}


SIZE_LABELS = {
    "small": "스몰",
    "medium": "미디엄",
    "large": "라지",
}


SIZE_PATTERNS = (
    (r"라지|large", "large"),
    (r"미디엄|미디움|medium", "medium"),
    (r"스몰|small", "small"),
)


def _compact(text):
    return re.sub(
        r"\s+",
        "",
        str(text or "").lower(),
    )


def _aliases(mapping, knowledge):
    result = []

    for canonical, aliases in mapping.items():

        values = set()

        if isinstance(aliases, str):
            values.add(aliases)

        else:
            try:
                values.update(
                    str(x)
                    for x in aliases
                    if x
                )
            except TypeError:
                pass

        info = knowledge.get(
            canonical,
            {},
        )

        name = info.get("name")

        if name:
            values.add(name)

        for alias in values:
            alias = str(alias).strip()

            if alias:
                result.append(
                    (
                        alias,
                        canonical,
                    )
                )

    return result


BURGER_TERMS = _aliases(
    MENU_ALIASES,
    BURGERS,
)

DRINK_TERMS = _aliases(
    DRINK_ALIASES,
    DRINKS,
)

SIDE_TERMS = _aliases(
    SIDE_ALIASES,
    SIDES,
)


def _extract_mentions(text):
    """
    반환:
    [
      {
        kind,
        key,
        label,
        start,
        end,
        size,
      },
      ...
    ]

    긴 alias 우선 + overlap 제거.
    """

    raw = str(text or "").lower()

    candidates = []

    groups = (
        (
            "burger",
            BURGER_TERMS,
            BURGERS,
        ),
        (
            "drink",
            DRINK_TERMS,
            DRINKS,
        ),
        (
            "side",
            SIDE_TERMS,
            SIDES,
        ),
    )

    for kind, terms, knowledge in groups:

        for alias, key in terms:

            for m in re.finditer(
                re.escape(
                    alias.lower()
                ),
                raw,
            ):
                candidates.append({
                    "kind": kind,
                    "key": key,
                    "label": (
                        knowledge
                        .get(key, {})
                        .get("name", key)
                    ),
                    "start": m.start(),
                    "end": m.end(),
                    "alias_len": (
                        m.end()
                        - m.start()
                    ),
                })

    # 같은 위치에서는 긴 표현 우선.
    candidates.sort(
        key=lambda x: (
            x["start"],
            -x["alias_len"],
        )
    )

    selected = []

    for candidate in candidates:

        overlap = any(
            not (
                candidate["end"]
                <= item["start"]
                or candidate["start"]
                >= item["end"]
            )
            for item in selected
        )

        if overlap:
            continue

        selected.append(
            candidate
        )

    selected.sort(
        key=lambda x: x["start"]
    )

    # 각 상품 뒤쪽에서 해당 상품의 size를 찾는다.
    for i, item in enumerate(selected):

        next_start = (
            selected[i + 1]["start"]
            if i + 1 < len(selected)
            else len(raw)
        )

        local = raw[
            item["end"]:
            next_start
        ]

        size = None

        for pattern, value in SIZE_PATTERNS:
            if re.search(
                pattern,
                local,
            ):
                size = value
                break

        item["size"] = size

    return selected


def _detect_metric(text):
    compact = _compact(text)

    kcal = any(
        x in compact
        for x in (
            "칼로리",
            "kcal",
            "열량",
        )
    )

    price = any(
        x in compact
        for x in (
            "가격",
            "얼마",
            "원",
            "비싸",
            "싸",
            "저렴",
            "금액",
            "가격차",
        )
    )

    # "얼마나 달라져"는 가격 문맥으로 간주.
    if (
        "얼마나달라" in compact
        or "얼마차이" in compact
    ):
        price = True

    return price, kcal


def _explicit_size(text):
    raw = str(text or "").lower()

    for pattern, value in SIZE_PATTERNS:
        if re.search(
            pattern,
            raw,
        ):
            return value

    return None


def _price_value(item):
    kind = item["kind"]
    key = item["key"]

    if kind == "burger":
        return BURGER_BASE_PRICE.get(
            key
        )

    if kind == "side":
        return STANDALONE_SIDE_PRICE.get(
            key
        )

    if kind == "drink":

        size = item.get("size")

        if size is None:
            return None

        base = STANDALONE_DRINK_PRICE.get(
            key
        )

        if base is None:
            return None

        return (
            base
            + DRINK_SIZE_UPCHARGE[
                size
            ]
        )

    return None


def _kcal_value(item):
    kind = item["kind"]
    key = item["key"]

    if kind == "burger":
        return (
            BURGERS
            .get(key, {})
            .get("calories")
        )

    if kind == "side":
        return (
            SIDES
            .get(key, {})
            .get("calories")
        )

    if kind == "drink":

        size = item.get("size")

        if size is None:
            return None

        return (
            DRINKS
            .get(key, {})
            .get(
                "calories",
                {},
            )
            .get(size)
        )

    return None


def _drink_price_text(key):
    label = (
        DRINKS
        .get(key, {})
        .get("name", key)
    )

    base = STANDALONE_DRINK_PRICE[
        key
    ]

    values = []

    for size in (
        "small",
        "medium",
        "large",
    ):
        price = (
            base
            + DRINK_SIZE_UPCHARGE[
                size
            ]
        )

        values.append(
            f"{SIZE_LABELS[size]} "
            f"{price:,}원"
        )

    return (
        f"{label}는 "
        + ", ".join(values)
        + "입니다."
    )


def _drink_kcal_text(key):
    label = (
        DRINKS
        .get(key, {})
        .get("name", key)
    )

    calories = (
        DRINKS[key][
            "calories"
        ]
    )

    values = [
        f"{SIZE_LABELS[size]} "
        f"{calories[size]}kcal"
        for size in (
            "small",
            "medium",
            "large",
        )
    ]

    return (
        f"{label}는 "
        + ", ".join(values)
        + "입니다."
    )


def _render_value(
    item,
    *,
    price=False,
    kcal=False,
):
    label = item["label"]

    if item["kind"] == "drink":

        size = item.get("size")

        if size is None:

            if price and kcal:
                return (
                    _drink_price_text(
                        item["key"]
                    )
                    + " "
                    + _drink_kcal_text(
                        item["key"]
                    )
                )

            if price:
                return _drink_price_text(
                    item["key"]
                )

            if kcal:
                return _drink_kcal_text(
                    item["key"]
                )

        size_label = (
            SIZE_LABELS[size]
        )

        label = (
            f"{label} "
            f"{size_label}"
        )

    parts = []

    if price:
        value = _price_value(item)

        if value is not None:
            parts.append(
                f"{value:,}원"
            )

    if kcal:
        value = _kcal_value(item)

        if value is not None:
            parts.append(
                f"{value}kcal"
            )

    if not parts:
        return None

    return (
        f"{label}은 "
        + ", ".join(parts)
        + "입니다."
    )


def _current_order_kcal(state):
    items = (
        state.get("items", [])
        if isinstance(state, dict)
        else []
    )

    if not items:
        return 0, False

    total = 0

    for item in items:

        quantity = int(
            item.get(
                "quantity",
                1,
            )
            or 1
        )

        item_type = item.get(
            "item_type"
        )

        kcal = 0

        if item_type == "burger":

            menu = item.get(
                "menu"
            )

            info = BURGERS.get(
                menu
            )

            if not info:
                return None, False

            kcal += int(
                info["calories"]
            )

            base_ingredients = set(
                info.get(
                    "ingredients",
                    [],
                )
            )

            for excluded in (
                item.get(
                    "exclude",
                    [],
                )
                or []
            ):
                if (
                    excluded
                    in base_ingredients
                ):
                    kcal -= (
                        BASE_INGREDIENT_KCAL
                        .get(
                            excluded,
                            0,
                        )
                    )

            for topping in (
                item.get(
                    "add_toppings",
                    [],
                )
                or []
            ):
                kcal += (
                    TOPPING_KCAL
                    .get(
                        topping,
                        0,
                    )
                )

            if (
                item.get("type")
                == "set"
            ):

                drink = item.get(
                    "drink"
                )

                size = item.get(
                    "drink_size"
                )

                side = item.get(
                    "side"
                )

                drink_kcal = (
                    DRINKS
                    .get(drink, {})
                    .get(
                        "calories",
                        {},
                    )
                    .get(size)
                )

                side_kcal = (
                    SIDES
                    .get(side, {})
                    .get(
                        "calories"
                    )
                )

                if (
                    drink_kcal is None
                    or side_kcal is None
                ):
                    return None, False

                kcal += (
                    drink_kcal
                    + side_kcal
                )

        elif item_type == "drink":

            drink = item.get(
                "drink"
            )

            size = item.get(
                "drink_size"
            )

            value = (
                DRINKS
                .get(drink, {})
                .get(
                    "calories",
                    {},
                )
                .get(size)
            )

            if value is None:
                return None, False

            kcal += value

        elif item_type == "side":

            side = item.get(
                "side"
            )

            value = (
                SIDES
                .get(side, {})
                .get(
                    "calories"
                )
            )

            if value is None:
                return None, False

            kcal += value

        else:
            return None, False

        total += kcal * quantity

    return total, True


def _answer_current_order(
    text,
    state,
    unit_price_fn,
):
    compact = _compact(text)

    current_words = (
        "내주문",
        "지금주문",
        "현재주문",
        "주문한거",
        "주문한것",
        "담은거",
        "담긴거",
        "전체주문",
    )

    if not any(
        word in compact
        for word in current_words
    ):
        return None

    price, kcal = _detect_metric(
        text
    )

    if not price and not kcal:
        return None

    items = (
        state.get("items", [])
        if isinstance(state, dict)
        else []
    )

    if not items:
        return (
            "현재 담긴 주문이 없습니다.",
            "current_order_empty",
        )

    parts = []

    if price:

        total_price = 0

        for item in items:

            value = unit_price_fn(
                item
            )

            if value is None:
                return None

            quantity = int(
                item.get(
                    "quantity",
                    1,
                )
                or 1
            )

            total_price += (
                value
                * quantity
            )

        parts.append(
            f"총 가격은 "
            f"{total_price:,}원"
        )

    if kcal:

        total_kcal, complete = (
            _current_order_kcal(
                state
            )
        )

        if not complete:
            return None

        parts.append(
            f"총 칼로리는 "
            f"{total_kcal}kcal"
        )

    return (
        "현재 주문의 "
        + ", ".join(parts)
        + "입니다.",
        "current_order_total",
    )


def _answer_set_vs_separate(text):
    compact = _compact(text)

    if "세트" not in compact:
        return None

    if not (
        "따로" in compact
        or (
            "단품" in compact
            and "음료" in compact
        )
    ):
        return None

    price, kcal = _detect_metric(
        text
    )

    if not price:
        return None

    # 기본 세트:
    # 기본 탄산 스몰 + 감자튀김을 기준으로 비교.
    regular_drinks = (
        "coke",
        "zero_coke",
        "sprite",
        "fanta",
    )

    standalone_drink = min(
        STANDALONE_DRINK_PRICE[x]
        + DRINK_SIZE_UPCHARGE[
            "small"
        ]
        for x in regular_drinks
    )

    standalone_side = (
        STANDALONE_SIDE_PRICE[
            "french_fries"
        ]
    )

    separate_extra = (
        standalone_drink
        + standalone_side
    )

    saving = (
        separate_extra
        - SET_UPCHARGE
    )

    return (
        "같은 기본 구성으로 비교하면 "
        f"세트 변경은 +{SET_UPCHARGE:,}원이고, "
        f"음료 스몰 {standalone_drink:,}원과 "
        f"감자튀김 {standalone_side:,}원을 "
        f"따로 추가하면 +{separate_extra:,}원이라 "
        f"세트가 {saving:,}원 저렴합니다. "
        "다만 단품과 음료만 따로 사는 경우에는 "
        "사이드가 포함되지 않아 동일 구성 비교는 아닙니다.",
        "set_vs_separate",
    )


def _answer_extreme(text):
    compact = _compact(text)

    if not any(
        x in compact
        for x in (
            "제일",
            "가장",
            "최저",
            "최고",
            "싼",
            "비싼",
            "낮은",
            "높은",
            "낮아",
            "높아",
        )
    ):
        return None

    price, kcal = _detect_metric(
        text
    )

    if not price and not kcal:
        return None

    if "버거" in compact:
        kind = "burger"
        keys = list(
            BURGERS
        )

    elif "음료" in compact:
        kind = "drink"
        keys = list(
            DRINKS
        )

    elif (
        "사이드" in compact
        or "사이드메뉴" in compact
    ):
        kind = "side"
        keys = list(
            SIDES
        )

    else:
        return None

    want_max = any(
        x in compact
        for x in (
            "비싼",
            "최고",
            "높은",
            "높아",
            "많은",
        )
    )

    want_min = any(
        x in compact
        for x in (
            "싼",
            "최저",
            "낮은",
            "낮아",
            "적은",
        )
    )

    if want_max == want_min:
        return None

    # --------------------------------------------
    # DRINK
    # --------------------------------------------

    if kind == "drink":

        size = _explicit_size(
            text
        )

        # size가 없으면 모든 size에서 winner가 같은 경우만 답한다.
        sizes = (
            [size]
            if size
            else [
                "small",
                "medium",
                "large",
            ]
        )

        winners = []

        for current_size in sizes:

            values = []

            for key in keys:

                if price:
                    value = (
                        STANDALONE_DRINK_PRICE[
                            key
                        ]
                        + DRINK_SIZE_UPCHARGE[
                            current_size
                        ]
                    )

                else:
                    value = (
                        DRINKS[key][
                            "calories"
                        ][current_size]
                    )

                values.append(
                    (
                        key,
                        value,
                    )
                )

            target_value = (
                max(v for _, v in values)
                if want_max
                else min(
                    v
                    for _, v in values
                )
            )

            winner_keys = tuple(
                key
                for key, value
                in values
                if value == target_value
            )

            winners.append(
                (
                    current_size,
                    winner_keys,
                    target_value,
                )
            )

        if not size:

            first_keys = winners[0][1]

            if not all(
                row[1] == first_keys
                for row in winners
            ):
                return (
                    "음료는 사이즈에 따라 순위가 달라질 수 있어요. "
                    "스몰, 미디엄, 라지 중 사이즈를 말씀해주세요.",
                    "drink_extreme_needs_size",
                )

        names = [
            DRINKS[key]["name"]
            for key in winners[0][1]
        ]

        if size:

            value = winners[0][2]

            unit = (
                "원"
                if price
                else "kcal"
            )

            return (
                f"{SIZE_LABELS[size]} 기준 "
                + ", ".join(names)
                + (
                    "이 가장 높습니다. "
                    if want_max
                    else "이 가장 낮습니다. "
                )
                + f"기준값은 {value:,}{unit}입니다.",
                "drink_extreme",
            )

        # 모든 사이즈에서 winner 동일
        detail = ", ".join(
            f"{SIZE_LABELS[s]} "
            f"{value:,}"
            + (
                "원"
                if price
                else "kcal"
            )
            for s, _, value
            in winners
        )

        return (
            ", ".join(names)
            + (
                "이 모든 사이즈에서 가장 높습니다. "
                if want_max
                else "이 모든 사이즈에서 가장 낮습니다. "
            )
            + detail
            + "입니다.",
            "drink_extreme_all_sizes",
        )

    # --------------------------------------------
    # BURGER / SIDE
    # --------------------------------------------

    values = []

    for key in keys:

        if kind == "burger":

            value = (
                BURGER_BASE_PRICE[key]
                if price
                else BURGERS[key][
                    "calories"
                ]
            )

            label = BURGERS[key][
                "name"
            ]

        else:

            value = (
                STANDALONE_SIDE_PRICE[
                    key
                ]
                if price
                else SIDES[key][
                    "calories"
                ]
            )

            label = SIDES[key][
                "name"
            ]

        values.append(
            (
                label,
                value,
            )
        )

    target_value = (
        max(v for _, v in values)
        if want_max
        else min(
            v
            for _, v in values
        )
    )

    labels = [
        label
        for label, value
        in values
        if value == target_value
    ]

    unit = (
        "원"
        if price
        else "kcal"
    )

    return (
        ", ".join(labels)
        + (
            "이 가장 높습니다. "
            if want_max
            else "이 가장 낮습니다. "
        )
        + f"기준값은 {target_value:,}{unit}입니다.",
        "menu_extreme",
    )


def _answer_mentions(
    text,
    mentions,
):
    price, kcal = _detect_metric(
        text
    )

    if not price and not kcal:
        return None

    compact = _compact(text)

    compare = any(
        x in compact
        for x in (
            "비교",
            "차이",
            "더싸",
            "더비싸",
            "낮아",
            "높아",
            "낮은",
            "높은",
            "얼마나",
            "대신",
            "중뭐",
            "중에서",
        )
    )

    # --------------------------------------------------------
    # SINGLE ENTITY
    # --------------------------------------------------------

    if len(mentions) == 1:

        reply = _render_value(
            mentions[0],
            price=price,
            kcal=kcal,
        )

        if reply is None:
            return None

        return (
            reply,
            "single_info",
        )

    # --------------------------------------------------------
    # MULTI ENTITY - size 부족 검사
    # --------------------------------------------------------

    if compare:

        for item in mentions:

            if (
                item["kind"]
                == "drink"
                and item.get(
                    "size"
                )
                is None
            ):
                return (
                    "음료끼리 정확히 비교하려면 "
                    "각 음료의 사이즈를 함께 말씀해주세요.",
                    "comparison_needs_drink_size",
                )

    # --------------------------------------------------------
    # PAIR COMPARISON
    # --------------------------------------------------------

    if (
        len(mentions) == 2
        and compare
    ):

        a, b = mentions

        sentences = []

        if price:

            av = _price_value(a)
            bv = _price_value(b)

            if (
                av is None
                or bv is None
            ):
                return None

            diff = abs(
                av - bv
            )

            if av == bv:
                sentences.append(
                    f"{a['label']}과 "
                    f"{b['label']}의 가격은 "
                    f"같이 {av:,}원입니다."
                )

            elif bv > av:
                sentences.append(
                    f"{b['label']}이 "
                    f"{a['label']}보다 "
                    f"{diff:,}원 더 비쌉니다 "
                    f"({av:,}원 vs {bv:,}원)."
                )

            else:
                sentences.append(
                    f"{a['label']}이 "
                    f"{b['label']}보다 "
                    f"{diff:,}원 더 비쌉니다 "
                    f"({av:,}원 vs {bv:,}원)."
                )

        if kcal:

            av = _kcal_value(a)
            bv = _kcal_value(b)

            if (
                av is None
                or bv is None
            ):
                return None

            diff = abs(
                av - bv
            )

            if av == bv:
                sentences.append(
                    f"칼로리는 둘 다 "
                    f"{av}kcal입니다."
                )

            elif bv > av:
                sentences.append(
                    f"{b['label']}이 "
                    f"{a['label']}보다 "
                    f"{diff}kcal 더 높습니다 "
                    f"({av}kcal vs {bv}kcal)."
                )

            else:
                sentences.append(
                    f"{a['label']}이 "
                    f"{b['label']}보다 "
                    f"{diff}kcal 더 높습니다 "
                    f"({av}kcal vs {bv}kcal)."
                )

        return (
            " ".join(sentences),
            "pair_comparison",
        )

    # --------------------------------------------------------
    # EACH / MULTI LIST
    # --------------------------------------------------------

    replies = []

    for item in mentions:

        reply = _render_value(
            item,
            price=price,
            kcal=kcal,
        )

        if reply is None:
            return None

        replies.append(
            reply
        )

    return (
        " ".join(replies),
        "multi_info",
    )


def answer_price_calorie_query(
    text,
    *,
    state=None,
    unit_price_fn=None,
):
    """
    return:
        (reply, reason)
        또는 None

    None이면 기존 Router V14로 fallback.
    """

    raw = str(
        text or ""
    ).strip()

    compact = _compact(
        raw
    )

    if not compact:
        return None

    # ========================================================
    # MUTATION SAFETY
    #
    # 실제 주문 변경 명령은 절대 이 엔진이 먹지 않는다.
    # '바꾸면 가격...' 같은 가정형 비교는 허용.
    # ========================================================

    mutation_patterns = (
        r"추가해(?:줘|주세요|줘요)",
        r"주문해(?:줘|주세요|줘요)",
        r"빼(?:줘|주세요|줘요)",
        r"제외해(?:줘|주세요|줘요)",
        r"바꿔(?:줘|주세요|줘요)",
        r"변경해(?:줘|주세요|줘요)",
        r"취소해(?:줘|주세요|줘요)",
        r"삭제해(?:줘|주세요|줘요)",
    )

    if any(
        re.search(
            pattern,
            compact,
        )
        for pattern
        in mutation_patterns
    ):
        return None

    price, kcal = _detect_metric(
        raw
    )

    if not price and not kcal:
        return None

    # 현재 주문 총액/총 kcal
    if unit_price_fn is not None:

        current = _answer_current_order(
            raw,
            state or {},
            unit_price_fn,
        )

        if current is not None:
            return current

    # 세트 vs 개별 구매
    set_compare = (
        _answer_set_vs_separate(
            raw
        )
    )

    if set_compare is not None:
        return set_compare

    # 최저 / 최고
    extreme = _answer_extreme(
        raw
    )

    if extreme is not None:
        return extreme

    mentions = _extract_mentions(
        raw
    )

    if not mentions:
        return None

    return _answer_mentions(
        raw,
        mentions,
    )
