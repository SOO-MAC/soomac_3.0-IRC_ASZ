#!/usr/bin/env python3

import re


PRODUCT_PATTERNS = (
    (r"제로\s*콜라", "zero_coke", "drink"),
    (r"아이스\s*커피", "iced_coffee", "drink"),
    (r"사이다|스프라이트", "sprite", "drink"),
    (r"환타|판타", "fanta", "drink"),
    (r"콜라", "coke", "drink"),

    (r"불고기\s*버거", "bulgogi_burger", "burger"),
    (r"치킨\s*버거", "chicken_burger", "burger"),
    (r"치즈\s*버거", "cheese_burger", "burger"),
    (r"새우\s*버거", "shrimp_burger", "burger"),

    (r"감자\s*튀김", "french_fries", "side"),
    (r"치즈\s*스틱", "cheese_stick", "side"),
)


# "세트"는 이제 허용한다.
# 나머지 수정/참조/질문 계열은 여전히 V14 fallback.
GLOBAL_UNSAFE = (
    "빼",
    "제외",
    "없이",
    "바꿔",
    "바꾸",
    "변경",
    "수정",
    "교체",
    "취소",
    "삭제",
    "가능",
    "추천",
    "가격",
    "얼마",
    "칼로리",
    "아까",
    "그거",
    "첫번째",
    "두번째",
    "세번째",
    "첫 번째",
    "두 번째",
    "세 번째",
    "?",
)


def _compact(text):
    return re.sub(
        r"\s+",
        "",
        str(text or "").lower(),
    )


def _quantity(text):
    compact = _compact(text)

    table = (
        (r"(?:네개|네잔|네\s*개)", 4),
        (r"(?:세개|세잔|세\s*개)", 3),
        (r"(?:두개|두잔|두\s*개)", 2),
        (
            r"(?:하나|한개|한잔|"
            r"한\s*개|한\s*잔)",
            1,
        ),
    )

    for pattern, value in table:
        if re.search(pattern, compact):
            return value

    return None


def _size(text):
    compact = _compact(text)

    if (
        "라지" in compact
        or "large" in compact
    ):
        return "large"

    if (
        "미디움" in compact
        or "미디엄" in compact
        or "medium" in compact
    ):
        return "medium"

    if (
        "스몰" in compact
        or "small" in compact
    ):
        return "small"

    return None


def _products(text):
    found = []

    for pattern, target, domain in PRODUCT_PATTERNS:
        if re.search(pattern, text or ""):
            found.append(
                (
                    target,
                    domain,
                )
            )

    # 제로콜라 내부의 일반 콜라 중복 제거
    if any(
        target == "zero_coke"
        for target, _
        in found
    ):
        found = [
            (target, domain)
            for target, domain
            in found
            if target != "coke"
        ]

    return list(
        dict.fromkeys(found)
    )


def _segment_has_modifier(
    segment,
    target,
):
    compact = _compact(segment)

    names = {
        "bulgogi_burger":
            "불고기버거",
        "chicken_burger":
            "치킨버거",
        "cheese_burger":
            "치즈버거",
        "shrimp_burger":
            "새우버거",
        "cheese_stick":
            "치즈스틱",
    }

    name = names.get(target)

    if name:
        compact = compact.replace(
            name,
            "",
        )

    modifier_terms = (
        "피클",
        "토마토",
        "베이컨",
    )

    if any(
        term in compact
        for term in modifier_terms
    ):
        return True

    if (
        "치즈" in compact
        and target != "cheese_stick"
    ):
        return True

    return False


def _parse_full_set_segment(
    segment,
    *,
    order_state=None,
):
    """
    기존 route_explicit_full_set_pre_fastpath()를
    세트 구성 검증기로 재사용한다.

    반환:
        {
          item_type: burger,
          quantity: ...,
          menu: ...,
          type: set,
          drink: ...,
          drink_size: ...,
          side: ...
        }

    또는 None.
    """

    compact = _compact(segment)

    if not (
        "세트" in compact
        or "셋트" in compact
    ):
        return None

    # 복합문장을 '랑'으로 자르면 첫 clause에
    # '주세요'가 없을 수 있다.
    # 전체 utterance의 주문 의사는 이미 바깥에서 검증하므로
    # 기존 full-set validator 호출용으로만 보완한다.
    validation_text = segment

    if not re.search(
        r"(주세요|줘요|줘|주문|시킬게|시켜주세요)",
        compact,
    ):
        validation_text = (
            segment.rstrip()
            + " 주세요"
        )

    try:
        from router_pre_fastpath import (
            route_explicit_full_set_pre_fastpath,
        )

        routed = (
            route_explicit_full_set_pre_fastpath(
                validation_text,
                pending=None,
                order_state=(
                    order_state
                    if isinstance(
                        order_state,
                        dict,
                    )
                    else {"items": []}
                ),
            )
        )

    except Exception:
        return None

    if routed is None:
        return None

    try:
        data = routed.model_dump(
            mode="json"
        )
    except Exception:
        return None

    acts = data.get("acts") or []

    burger = None
    drink = None
    side = None

    for act in acts:

        if (
            act.get("family")
            != "order_action"
            or act.get("subtype")
            != "add"
        ):
            return None

        domain = act.get(
            "target_domain"
        )

        target = act.get(
            "target"
        )

        if domain == "burger":
            if burger is not None:
                return None
            burger = target

        elif domain == "drink":
            if drink is not None:
                return None
            drink = target

        elif domain == "side":
            if side is not None:
                return None
            side = target

        else:
            return None

    if not all(
        (
            burger,
            drink,
            side,
        )
    ):
        return None

    size = _size(segment)

    if size is None:
        return None

    quantity = _quantity(
        segment
    )

    if quantity is None:
        return None

    return {
        "item_type":
            "burger",
        "quantity":
            quantity,
        "menu":
            burger,
        "type":
            "set",
        "drink":
            drink,
        "drink_size":
            size,
        "side":
            side,
    }


def _parse_simple_segment(
    segment,
):
    """
    단품 버거 / 독립 음료 / 독립 사이드
    한 clause를 하나의 item으로 변환.
    """

    products = _products(
        segment
    )

    if len(products) != 1:
        return None

    target, domain = (
        products[0]
    )

    quantity = _quantity(
        segment
    )

    if quantity is None:
        return None

    if _segment_has_modifier(
        segment,
        target,
    ):
        return None

    if domain == "burger":

        if "단품" not in _compact(
            segment
        ):
            return None

        return {
            "item_type":
                "burger",
            "quantity":
                quantity,
            "menu":
                target,
            "type":
                "single",
        }

    if domain == "drink":

        size = _size(
            segment
        )

        if size is None:
            return None

        return {
            "item_type":
                "drink",
            "quantity":
                quantity,
            "drink":
                target,
            "drink_size":
                size,
        }

    if domain == "side":

        return {
            "item_type":
                "side",
            "quantity":
                quantity,
            "side":
                target,
        }

    return None



def _parse_correction_update(text):
    """
    '아니' / '말고' 정정 처리.

    원칙:
      A 아니 B  -> 같은 domain은 B가 A를 덮어씀
      A 말고 B  -> 같은 domain은 B가 A를 덮어씀

    지원:
      - 세트 옵션 정정
      - 단일 음료 정정
      - 단일 버거 정정
      - 단일 사이드 정정

    미지원:
      - correction operator 2개 이상
      - 빼기/제외/수정/참조가 섞인 문장
    """

    matches = list(
        re.finditer(
            r"(아니|말고)",
            str(text or ""),
        )
    )

    if len(matches) != 1:
        return None

    m = matches[0]

    left = str(text or "")[:m.start()].strip()
    right = str(text or "")[m.end():].strip()

    if not left or not right:
        return None

    compact = _compact(text)

    # correction 이외의 위험한 수정 표현은 처리하지 않는다.
    extra_unsafe = (
        "빼",
        "제외",
        "없이",
        "바꿔",
        "바꾸",
        "변경",
        "수정",
        "교체",
        "취소",
        "삭제",
        "가능",
        "추천",
        "가격",
        "얼마",
        "칼로리",
        "아까",
        "그거",
        "첫번째",
        "두번째",
        "세번째",
        "첫 번째",
        "두 번째",
        "세 번째",
        "?",
    )

    if any(
        term in compact
        for term in extra_unsafe
    ):
        return None

    # 전체 문장에는 주문 의사가 있어야 한다.
    if not re.search(
        r"(주세요|줘요|줘|"
        r"주문해주세요|주문해줘|"
        r"추가해주세요|추가해줘)",
        compact,
    ):
        return None

    def domain_map(part):
        result = {}

        for target, domain in _products(part):

            # 한쪽 절 안에서 같은 domain 상품이 둘 이상이면
            # fastpath에서는 해석하지 않는다.
            if (
                domain in result
                and result[domain] != target
            ):
                return None

            result[domain] = target

        return result

    left_map = domain_map(left)
    right_map = domain_map(right)

    if (
        left_map is None
        or right_map is None
        or not right_map
    ):
        return None

    left_compact = _compact(left)
    right_compact = _compact(right)

    # ========================================================
    # BURGER / SET CONTEXT
    # ========================================================

    burger_context = (
        "burger" in left_map
        or "burger" in right_map
        or "세트" in left_compact
        or "셋트" in left_compact
        or "단품" in left_compact
        or "세트" in right_compact
        or "셋트" in right_compact
        or "단품" in right_compact
    )

    if burger_context:

        burger = (
            right_map.get("burger")
            or left_map.get("burger")
        )

        if not burger:
            return None

        if (
            "세트" in right_compact
            or "셋트" in right_compact
        ):
            burger_type = "set"

        elif "단품" in right_compact:
            burger_type = "single"

        elif (
            "세트" in left_compact
            or "셋트" in left_compact
        ):
            burger_type = "set"

        elif "단품" in left_compact:
            burger_type = "single"

        else:
            return None

        quantity = (
            _quantity(right)
            or _quantity(left)
            or 1
        )

        # ----------------------------------------------------
        # SET
        # ----------------------------------------------------

        if burger_type == "set":

            drink = (
                right_map.get("drink")
                or left_map.get("drink")
            )

            side = (
                right_map.get("side")
                or left_map.get("side")
            )

            # 뒤에서 size를 다시 말했으면 뒤 값 우선
            size = (
                _size(right)
                or _size(left)
            )

            if not all(
                (
                    drink,
                    side,
                    size,
                )
            ):
                return None

            return {
                "intent": "order",
                "actions": [
                    {
                        "operation": "add",
                        "item": {
                            "item_type": "burger",
                            "quantity": quantity,
                            "menu": burger,
                            "type": "set",
                            "drink": drink,
                            "drink_size": size,
                            "side": side,
                        },
                    }
                ],
            }

        # ----------------------------------------------------
        # SINGLE BURGER
        # ----------------------------------------------------

        # 단품 정정인데 음료/사이드까지 섞이면 의미가 복잡하므로
        # 기존 V14로 보낸다.
        if (
            "drink" in left_map
            or "drink" in right_map
            or "side" in left_map
            or "side" in right_map
        ):
            return None

        return {
            "intent": "order",
            "actions": [
                {
                    "operation": "add",
                    "item": {
                        "item_type": "burger",
                        "quantity": quantity,
                        "menu": burger,
                        "type": "single",
                    },
                }
            ],
        }

    # ========================================================
    # SIMPLE PRODUCT CORRECTION
    # ========================================================

    # 오른쪽 최종 선택은 정확히 한 domain이어야 한다.
    if len(right_map) != 1:
        return None

    domain, target = next(
        iter(right_map.items())
    )

    quantity = (
        _quantity(right)
        or _quantity(left)
        or 1
    )

    if domain == "drink":

        size = (
            _size(right)
            or _size(left)
        )

        if size is None:
            return None

        item = {
            "item_type": "drink",
            "quantity": quantity,
            "drink": target,
            "drink_size": size,
        }

    elif domain == "side":

        item = {
            "item_type": "side",
            "quantity": quantity,
            "side": target,
        }

    elif domain == "burger":

        # burger는 단품/세트 구분이 없으면 fastpath하지 않는다.
        if (
            "단품" not in left_compact
            and "단품" not in right_compact
        ):
            return None

        item = {
            "item_type": "burger",
            "quantity": quantity,
            "menu": target,
            "type": "single",
        }

    else:
        return None

    return {
        "intent": "order",
        "actions": [
            {
                "operation": "add",
                "item": item,
            }
        ],
    }


def build_compound_order_update(
    text,
    *,
    pending=None,
    order_state=None,
):
    """
    안전한 신규 복합 ADD를 OrderUpdate로 직접 변환.

    V2 지원:
    - 단품 + 음료 + 사이드
    - 서로 다른 음료/사이즈 여러 개
    - 완전 명시 세트 + 독립 단품/음료/사이드

    예:
      치킨버거 단품 하나랑
      콜라 미디엄 하나랑
      치즈스틱 하나 주세요

      콜라 미디엄 하나랑
      사이다 라지 하나 주세요

      불고기버거 세트 콜라 미디엄 감자튀김 하나랑
      새우버거 단품 하나 주세요

    수정 / self-correction / reference 등은
    기존 V14로 fallback.
    """

    if pending is not None:
        return None

    state_items = []

    if isinstance(
        order_state,
        dict,
    ):
        raw_items = order_state.get(
            "items",
            [],
        )

        if isinstance(
            raw_items,
            list,
        ):
            state_items = raw_items

    # 현재 V2는 신규 주문 시작 상태만.
    if state_items:
        return None

    compact = _compact(text)

    if not compact:
        return None

    if any(
        term in compact
        for term in GLOBAL_UNSAFE
    ):
        return None

    # 전체 발화에는 명확한 주문 의사가 있어야 한다.
    if not re.search(
        r"(주세요|줘요|줘|"
        r"추가해주세요|추가해줘|"
        r"추가해주라|주문해주세요|"
        r"주문해줘)",
        compact,
    ):
        return None

    # ========================================================
    # 아니 / 말고 SELF-CORRECTION
    # ========================================================

    if re.search(
        r"(아니|말고)",
        text,
    ):
        return _parse_correction_update(
            text
        )

    if not re.search(
        r"(?:,|이랑|랑|그리고)",
        text,
    ):
        return None

    segments = [
        seg.strip()
        for seg in re.split(
            r"\s*(?:,|이랑|랑|그리고)\s*",
            text,
        )
        if seg.strip()
    ]

    if len(segments) < 2:
        return None

    actions = []

    for segment in segments:

        seg_compact = _compact(
            segment
        )

        # ---------------------------------------------
        # FULL SET CLAUSE
        # ---------------------------------------------

        if (
            "세트" in seg_compact
            or "셋트" in seg_compact
        ):

            item = (
                _parse_full_set_segment(
                    segment,
                    order_state=order_state,
                )
            )

        # ---------------------------------------------
        # SIMPLE CLAUSE
        # ---------------------------------------------

        else:

            item = (
                _parse_simple_segment(
                    segment
                )
            )

        if item is None:
            return None

        actions.append({
            "operation":
                "add",
            "item":
                item,
        })

    if len(actions) < 2:
        return None

    return {
        "intent":
            "order",
        "actions":
            actions,
    }
