#!/usr/bin/env python3

import re

from router_schema import RouterOutput


PRODUCTS = (
    (r"제로\s*콜라", "zero_coke", "drink"),
    (r"아이스\s*커피", "iced_coffee", "drink"),
    (r"스프라이트|사이다", "sprite", "drink"),
    (r"환타|판타", "fanta", "drink"),

    (r"불고기\s*버거", "bulgogi_burger", "burger"),
    (r"치킨\s*버거", "chicken_burger", "burger"),
    (r"치즈\s*버거", "cheese_burger", "burger"),
    (r"새우\s*버거", "shrimp_burger", "burger"),

    (r"감자\s*튀김", "french_fries", "side"),
    (r"치즈\s*스틱", "cheese_stick", "side"),

    # 반드시 zero_coke 뒤
    (r"콜라", "coke", "drink"),
)


def _compact(text):
    return re.sub(
        r"\s+",
        "",
        str(text or ""),
    )


def _product(text):
    found = []

    for pattern, target, domain in PRODUCTS:
        if re.search(pattern, text or ""):
            found.append(
                (target, domain)
            )

    # 제로콜라 내부 콜라 중복 제거
    if any(
        target == "zero_coke"
        for target, _ in found
    ):
        found = [
            x for x in found
            if x[0] != "coke"
        ]

    # 하나의 상품만 확정될 때만 사용
    unique = list(
        dict.fromkeys(found)
    )

    if len(unique) != 1:
        return None

    return unique[0]


def _quantity(text):
    compact = _compact(text)

    m = re.search(
        r"(\d{1,2})(?:개|잔|세트)?",
        compact,
    )

    if m:
        value = int(m.group(1))

        if 1 <= value <= 99:
            return value

    words = (
        ("열", 10),
        ("아홉", 9),
        ("여덟", 8),
        ("일곱", 7),
        ("여섯", 6),
        ("다섯", 5),
        ("네개", 4),
        ("넷", 4),
        ("세개", 3),
        ("셋", 3),
        ("두개", 2),
        ("둘", 2),
        ("하나", 1),
        ("한개", 1),
        ("한잔", 1),
    )

    for word, value in words:
        if word in compact:
            return value

    return 1


def _pending_parts(pending):
    if pending is None:
        return None, None

    if (
        isinstance(pending, (tuple, list))
        and len(pending) >= 2
    ):
        return pending[0], pending[1]

    if isinstance(pending, dict):
        return (
            pending.get("line_id"),
            pending.get("field"),
        )

    return None, None


def _last_staff_text(history):
    for item in reversed(history or []):
        if (
            isinstance(item, dict)
            and item.get("role") == "staff"
        ):
            return str(
                item.get("text")
                or item.get("content")
                or ""
            )

    return ""


def _unique_previous_drink(history):
    text = _last_staff_text(history)

    found = set()

    for pattern, target, domain in PRODUCTS:
        if (
            domain == "drink"
            and re.search(pattern, text)
        ):
            found.add(target)

    if "zero_coke" in found:
        found.discard("coke")

    if len(found) == 1:
        return next(iter(found))

    return None


def _output(act):
    return RouterOutput.model_validate({
        "acts": [act],
    })


def route_pre_fastpath(
    utterance,
    *,
    history=None,
    pending=None,
    order_state=None,
):
    """
    명백한 케이스만 LLM Router를 건너뛴다.

    확신할 수 없는 문장은 None을 반환해서
    기존 V14 Router로 fallback한다.
    """

    text = str(
        utterance or ""
    ).strip()

    compact = _compact(text)

    if not compact:
        return None

    line_id, pending_field = (
        _pending_parts(pending)
    )

    # ========================================================
    # 1. PRICE QUERY
    # ========================================================

    product = _product(text)

    if (
        product is not None
        and (
            "얼마" in compact
            or "가격" in compact
        )
    ):
        target, domain = product

        return _output({
            "family": "info_query",
            "subtype": "price",
            "speech_act": "question",
            "commitment": "none",
            "target_domain": domain,
            "target": target,
            "reference": {
                "source": "explicit",
                "resolved": True,
                "value": target,
                "line_ids": [],
            },
            "resolution": "clear",
        })

    # ========================================================
    # 2. PENDING TYPE
    # ========================================================

    if (
        pending_field == "type"
        and line_id is not None
    ):
        target = None

        if "단품" in compact:
            target = "single"

        elif "세트" in compact:
            target = "set"

        if target is not None:
            return _output({
                "family": "order_action",
                "subtype": "modify",
                "speech_act": "request",
                "commitment": "explicit",
                "target_domain": "burger",
                "target": target,
                "reference": {
                    "source": "pending",
                    "resolved": True,
                    "value": target,
                    "line_ids": [
                        int(line_id)
                    ],
                },
                "resolution":
                    "context_resolved",
            })

    # ========================================================
    # 3. EXPLICIT SIMPLE ADD
    # ========================================================

    order_words = (
        "주세요",
        "줘요",
        "줘",
        "추가",
        "주문",
    )

    if (
        product is not None
        and any(
            word in compact
            for word in order_words
        )
        and "얼마" not in compact
        and "가능" not in compact
    ):
        target, domain = product

        return _output({
            "family": "order_action",
            "subtype": "add",
            "speech_act": "request",
            "commitment": "explicit",
            "target_domain": domain,
            "target": target,
            "quantity": _quantity(text),
            "reference": {
                "source": "explicit",
                "resolved": True,
                "value": target,
                "line_ids": [],
            },
            "resolution": "clear",
        })

    # ========================================================
    # 4. PREVIOUS STAFF DRINK CONTEXT
    #
    # 예:
    # STAFF: 제로콜라는 스몰 2,000원...
    # USER : 스몰로 하나 주세요
    # ========================================================

    size_only = (
        "스몰" in compact
        or "미디움" in compact
        or "미디엄" in compact
        or "라지" in compact
    )

    if (
        product is None
        and size_only
        and any(
            word in compact
            for word in order_words
        )
    ):
        previous_drink = (
            _unique_previous_drink(
                history
            )
        )

        if previous_drink is not None:
            return _output({
                "family": "order_action",
                "subtype": "add",
                "speech_act": "request",
                "commitment": "explicit",
                "target_domain": "drink",
                "target": previous_drink,
                "quantity": _quantity(text),
                "reference": {
                    "source":
                        "previous_staff",
                    "resolved": True,
                    "value":
                        previous_drink,
                    "line_ids": [],
                },
                "resolution":
                    "context_resolved",
            })

    # ========================================================
    # 5. FINALIZE
    # ========================================================

    finalize_words = (
        "마무리",
        "주문끝",
        "주문끝낼",
        "이걸로할게",
        "이걸로할께",
    )

    if any(
        word in compact
        for word in finalize_words
    ):
        items = (
            (order_state or {})
            .get("items", [])
        )

        if (
            items
            and pending is None
        ):
            return _output({
                "family": "order_action",
                "subtype": "finalize",
                "speech_act": "request",
                "commitment": "explicit",
                "target_domain": "order",
                "reference": {
                    "source":
                        "current_order",
                    "resolved": True,
                    "value":
                        "current_order",
                    "line_ids": [],
                },
                "resolution":
                    "context_resolved",
            })

    return None


# ============================================================
# RECOMMENDATION PRE-ROUTER FAST PATH
# ============================================================

_route_pre_fastpath_before_recommendation = route_pre_fastpath


def _recommendation_budget(text):
    compact = re.sub(
        r"\s+",
        "",
        str(text or ""),
    )

    # 5,000원 / 5000원
    m = re.search(
        r"(\d[\d,]{2,})원",
        compact,
    )

    if m:
        try:
            value = int(
                m.group(1).replace(",", "")
            )

            if 100 <= value <= 100000:
                return value

        except ValueError:
            pass

    # 자주 쓰는 한국어 예산 표현
    korean = {
        "삼천원": 3000,
        "사천원": 4000,
        "오천원": 5000,
        "육천원": 6000,
        "칠천원": 7000,
        "팔천원": 8000,
        "구천원": 9000,
        "만원": 10000,
    }

    for word, value in korean.items():
        if word in compact:
            return value

    return None


def _has_recommendation_context(history):
    for item in reversed(
        (history or [])[-8:]
    ):
        if not isinstance(item, dict):
            continue

        value = str(
            item.get("text")
            or item.get("content")
            or ""
        )

        compact = re.sub(
            r"\s+",
            "",
            value,
        )

        if any(
            key in compact
            for key in (
                "추천",
                "취향",
                "예산",
                "선택을도와",
            )
        ):
            return True

    return False


def route_pre_fastpath(
    utterance,
    *,
    history=None,
    pending=None,
    order_state=None,
):
    text = str(
        utterance or ""
    ).strip()

    compact = re.sub(
        r"\s+",
        "",
        text,
    )

    budget = _recommendation_budget(
        text
    )

    # --------------------------------------------------------
    # 1. 직접 추천 요청
    # --------------------------------------------------------
    if (
        "추천" in compact
        and not any(
            word in compact
            for word in (
                "주세요",
                "추가해주세요",
                "주문할게",
                "주문해",
            )
        )
    ):
        subtype = (
            "budget_recommendation"
            if budget is not None
            else "general_recommendation"
        )

        return RouterOutput.model_validate({
            "acts": [
                {
                    "family": "recommendation",
                    "subtype": subtype,
                    "speech_act": "request",
                    "commitment": "none",
                    "reference": {
                        "source": "none",
                        "resolved": False,
                        "line_ids": [],
                    },
                    "resolution": "clear",
                }
            ]
        })

    # --------------------------------------------------------
    # 2. 추천 후 예산 후속 답변
    #
    # STAFF:
    #   취향이나 예산을 말씀해주세요.
    #
    # USER:
    #   5000원 정도 있어요
    # --------------------------------------------------------
    if (
        budget is not None
        and _has_recommendation_context(
            history
        )
    ):
        return RouterOutput.model_validate({
            "acts": [
                {
                    "family": "recommendation",
                    "subtype":
                        "budget_recommendation",
                    "speech_act": "statement",
                    "commitment": "none",
                    "reference": {
                        "source":
                            "conversation_history",
                        "resolved": True,
                        "value":
                            "previous_recommendation",
                        "line_ids": [],
                    },
                    "resolution":
                        "context_resolved",
                }
            ]
        })

    return (
        _route_pre_fastpath_before_recommendation(
            utterance,
            history=history,
            pending=pending,
            order_state=order_state,
        )
    )


# ============================================================
# RECOMMENDATION REQUEST FIX V2
# ============================================================

_route_pre_fastpath_before_recommendation_v2 = route_pre_fastpath


def route_pre_fastpath(
    utterance,
    *,
    history=None,
    pending=None,
    order_state=None,
):
    text = str(
        utterance or ""
    ).strip()

    compact = re.sub(
        r"\s+",
        "",
        text,
    )

    budget = _recommendation_budget(
        text
    )

    # "추천해주세요", "추천해줘", "추천 좀" 등
    # 단, "추천한 메뉴로 주세요" 같은 주문 문장은 여기서 잡지 않는다.
    recommendation_request = bool(
        re.search(
            r"(추천해주세요|추천해줘|추천해요|추천좀|추천부탁|"
            r"추천해$|추천$|뭐가좋|뭐먹)",
            compact,
        )
    )

    if recommendation_request:

        subtype = (
            "budget_recommendation"
            if budget is not None
            else "general_recommendation"
        )

        return RouterOutput.model_validate({
            "acts": [
                {
                    "family": "recommendation",
                    "subtype": subtype,
                    "speech_act": "request",
                    "commitment": "none",
                    "reference": {
                        "source": "none",
                        "resolved": False,
                        "line_ids": [],
                    },
                    "resolution": "clear",
                }
            ]
        })

    return (
        _route_pre_fastpath_before_recommendation_v2(
            utterance,
            history=history,
            pending=pending,
            order_state=order_state,
        )
    )


# ============================================================
# EXPLICIT FULL SET PRE-ROUTER FAST PATH V1
# ============================================================

def route_explicit_full_set_pre_fastpath(
    utterance,
    *,
    pending=None,
    order_state=None,
):
    """
    완전히 명시된 신규 버거 세트 주문만 deterministic하게 처리한다.

    요구 조건:
    - pending 없음
    - burger 정확히 1종
    - drink 정확히 1종
    - side 정확히 1종
    - 세트 명시
    - drink size 명시
    - 명확한 주문 의사
    - 질문/조건/수정/참조 표현 없음

    조금이라도 애매하면 None -> 기존 V14 Router.
    """

    if pending is not None:
        return None

    text = str(
        utterance or ""
    ).strip()

    compact = _compact(text)

    if not compact:
        return None

    # --------------------------------------------------------
    # 반드시 신규 "세트 주문"이어야 한다.
    # --------------------------------------------------------

    if not (
        "세트" in compact
        or "셋트" in compact
    ):
        return None

    order_words = (
        "주세요",
        "줘요",
        "줘",
        "주문",
        "시킬게",
        "시켜주세요",
    )

    if not any(
        word in compact
        for word in order_words
    ):
        return None

    # --------------------------------------------------------
    # 질문 / 조건 / 참조 / 수정은 절대 선처리하지 않는다.
    # --------------------------------------------------------

    unsafe_terms = (
        "가능",
        "얼마",
        "가격",
        "추천",
        "있으면",
        "되면",
        "가능하면",
        "아까",
        "그거",
        "그메뉴",
        "바꿔",
        "바꾸",
        "변경",
        "수정",
        "교체",
        "취소",
        "제거",
        "빼",
        "제외",
        "없이",
        "말고",
    )

    if any(
        term in compact
        for term in unsafe_terms
    ):
        return None

    # --------------------------------------------------------
    # 명시 상품 탐색
    # --------------------------------------------------------

    found = {
        "burger": [],
        "drink": [],
        "side": [],
    }

    for pattern, target, domain in PRODUCTS:

        if re.search(
            pattern,
            text,
        ):
            found[domain].append(
                target
            )

    # "제로콜라" 안의 "콜라" 중복 제거
    if "zero_coke" in found["drink"]:
        found["drink"] = [
            x
            for x in found["drink"]
            if x != "coke"
        ]

    for domain in found:
        found[domain] = list(
            dict.fromkeys(
                found[domain]
            )
        )

    if not (
        len(found["burger"]) == 1
        and len(found["drink"]) == 1
        and len(found["side"]) == 1
    ):
        return None

    burger = found["burger"][0]
    drink = found["drink"][0]
    side = found["side"][0]

    # --------------------------------------------------------
    # 음료 크기도 반드시 명시되어야 한다.
    # Runtime에는 원문이 전달되지만,
    # pre-route 발동 조건 자체를 강하게 제한한다.
    # --------------------------------------------------------

    sizes = []

    if "스몰" in compact:
        sizes.append("small")

    if (
        "미디엄" in compact
        or "미디움" in compact
    ):
        sizes.append("medium")

    if "라지" in compact:
        sizes.append("large")

    sizes = list(
        dict.fromkeys(sizes)
    )

    if len(sizes) != 1:
        return None

    # --------------------------------------------------------
    # 이미 같은 버거가 주문에 있으면
    # 신규 add / 기존 modify가 헷갈릴 수 있으므로 V14로 보낸다.
    # --------------------------------------------------------

    items = (
        (order_state or {})
        .get("items", [])
        or []
    )

    same_burger_exists = any(
        item.get("item_type") == "burger"
        and item.get("menu") == burger
        for item in items
    )

    if same_burger_exists:
        return None

    quantity = _quantity(text)

    def explicit_act(
        domain,
        target,
    ):
        return {
            "family": "order_action",
            "subtype": "add",
            "speech_act": "request",
            "commitment": "explicit",
            "target_domain": domain,
            "target": target,
            "quantity": quantity,
            "reference": {
                "source": "explicit",
                "resolved": True,
                "value": target,
                "line_ids": [],
            },
            "resolution": "clear",
        }

    return RouterOutput.model_validate({
        "acts": [
            explicit_act(
                "burger",
                burger,
            ),
            explicit_act(
                "drink",
                drink,
            ),
            explicit_act(
                "side",
                side,
            ),
        ]
    })


# ============================================================
# SPEED PRE-ROUTER FAST PATH V1
#
# 목적:
# - Thinking OFF에서 깨진 App regression 복구
# - 확실한 케이스의 첫 Router V14 호출 제거
#
# 애매하면 반드시 None -> 기존 V14
# ============================================================

def route_speed_pre_fastpath(
    utterance,
    *,
    history=None,
    pending=None,
    order_state=None,
):
    text = str(
        utterance or ""
    ).strip()

    compact = _compact(text)

    if not compact:
        return None

    # ========================================================
    # COMPOUND REFERENCE / MODIFY GLOBAL GUARD
    #
    # 한 발화 안에서 신규 주문 + 기존/순번 참조 수정이
    # 함께 등장하면 단순 burger ADD 규칙으로 축약하면 안 된다.
    #
    # 예:
    #   치킨버거 하나랑 콜라 추가하고
    #   첫 번째 버거에는 토마토 빼줘
    # ========================================================

    if (
        (
            "첫번째" in compact
            or "두번째" in compact
            or "세번째" in compact
            or "아까" in compact
            or "그거" in compact
        )
        and (
            "빼" in compact
            or "제외" in compact
            or "추가" in compact
            or "바꿔" in compact
            or "바꾸" in compact
            or "변경" in compact
            or "수정" in compact
        )
    ):
        return None

    items = []

    if isinstance(order_state, dict):
        raw_items = order_state.get(
            "items",
            [],
        )

        if isinstance(raw_items, list):
            items = [
                item
                for item in raw_items
                if isinstance(item, dict)
            ]

    line_id, pending_field = (
        _pending_parts(pending)
    )

    # ========================================================
    # 0.25 SIMPLE QUANTITY SPEED PATH
    #
    # 명백한 단일 상품 수량 ADD / REMOVE만
    # Router V14 호출 전에 deterministic하게 처리한다.
    #
    # 예:
    #   치즈스틱 5개 주세요
    #   치즈스틱 3개 빼주세요
    #   3개 빼주세요
    #
    # 대상/수량이 조금이라도 애매하면 기존 V14로 fallback.
    # ========================================================

    if pending is None:

        count_pattern = (
            r"(?:"
            r"\d{1,2}|"
            r"한|하나|한개|"
            r"두|둘|두개|"
            r"세|셋|세개|"
            r"네|넷|네개|"
            r"다섯|다섯개|"
            r"여섯|일곱|여덟|아홉|열"
            r")"
        )

        remove_match = re.search(
            rf"(?P<count>{count_pattern})"
            rf"(?:개|잔|세트)?"
            rf"(?:만)?"
            rf"(?P<action>"
            rf"빼주세요|빼줘|빼|"
            rf"취소해주세요|취소해줘|취소|"
            rf"삭제해주세요|삭제해줘|삭제|"
            rf"안먹을래|안먹을게|안먹어요"
            rf")$",
            compact,
        )

        if remove_match is not None:

            count_token = (
                remove_match.group("count")
            )

            quantity = _quantity(
                count_token + "개"
            )

            prefix = compact[
                :remove_match.start()
            ]

            explicit_product = (
                _product(prefix)
                if prefix
                else None
            )

            def _item_target(item):
                kind = item.get(
                    "item_type"
                )

                if kind == "burger":
                    return (
                        item.get("menu"),
                        "burger",
                    )

                if kind == "drink":
                    return (
                        item.get("drink"),
                        "drink",
                    )

                if kind == "side":
                    return (
                        item.get("side"),
                        "side",
                    )

                return None, None

            def _item_signature(item):
                return (
                    item.get("item_type"),
                    item.get("menu"),
                    item.get("type"),
                    item.get("drink"),
                    item.get("drink_size"),
                    item.get("side"),
                    tuple(
                        item.get(
                            "exclude",
                            [],
                        )
                        or []
                    ),
                    tuple(
                        item.get(
                            "add_toppings",
                            [],
                        )
                        or []
                    ),
                )

            candidates = []

            target = None
            domain = None

            # --------------------------------------------
            # 상품명을 직접 말한 경우
            # --------------------------------------------
            if explicit_product is not None:

                target, domain = (
                    explicit_product
                )

                candidates = [
                    item
                    for item in items
                    if _item_target(item)
                    == (
                        target,
                        domain,
                    )
                ]

                # 같은 상품이라도 옵션이 서로 다르면
                # 여기서 임의로 고르지 않는다.
                signatures = {
                    _item_signature(item)
                    for item in candidates
                }

                if len(signatures) != 1:
                    candidates = []

            # --------------------------------------------
            # "3개 빼주세요"
            #
            # 상품명을 생략했으면 현재 주문 전체가
            # 완전히 동일한 상품일 때만 처리.
            # --------------------------------------------
            elif prefix == "" and items:

                signatures = {
                    _item_signature(item)
                    for item in items
                }

                if len(signatures) == 1:
                    candidates = list(
                        items
                    )

                    target, domain = (
                        _item_target(
                            candidates[0]
                        )
                    )

            if (
                target is not None
                and domain is not None
                and quantity >= 1
                and len(candidates)
                >= quantity
            ):

                ordered = sorted(
                    candidates,
                    key=lambda item: int(
                        item.get(
                            "line_id",
                            0,
                        )
                    ),
                )

                # 뒤쪽 line부터 제거.
                selected = ordered[
                    -quantity:
                ]

                line_ids = [
                    int(
                        item["line_id"]
                    )
                    for item in selected
                ]

                return _output({
                    "family":
                        "order_action",

                    "subtype":
                        "remove",

                    "speech_act":
                        "request",

                    "commitment":
                        "explicit",

                    "target_domain":
                        domain,

                    "target":
                        target,

                    "quantity":
                        quantity,

                    "reference": {
                        "source":
                            "current_order",

                        "resolved":
                            True,

                        "value":
                            target,

                        "line_ids":
                            line_ids,
                    },

                    "resolution":
                        "context_resolved",
                })

        # ----------------------------------------------------
        # 명백한 standalone side 수량 주문
        #
        # 현재 문제였던 "치즈스틱 5개 주세요"도
        # Router V14를 호출할 필요가 없다.
        # ----------------------------------------------------

        side_add_match = re.fullmatch(
            rf"(?:감자튀김|치즈스틱)"
            rf"(?P<count>{count_pattern})"
            rf"(?:개)?"
            rf"(?:"
            rf"주세요|줘요|줘|"
            rf"주문해주세요|주문해줘|"
            rf"추가해주세요|추가해줘"
            rf")",
            compact,
        )

        if side_add_match is not None:

            product = _product(
                text
            )

            if (
                product is not None
                and product[1] == "side"
            ):
                target, domain = product

                quantity = _quantity(
                    side_add_match.group(
                        "count"
                    )
                    + "개"
                )

                return _output({
                    "family":
                        "order_action",

                    "subtype":
                        "add",

                    "speech_act":
                        "request",

                    "commitment":
                        "explicit",

                    "target_domain":
                        domain,

                    "target":
                        target,

                    "quantity":
                        quantity,

                    "reference": {
                        "source":
                            "explicit",

                        "resolved":
                            True,

                        "value":
                            target,

                        "line_ids":
                            [],
                    },

                    "resolution":
                        "clear",
                })

    # ========================================================
    # SIMPLE DRINK ADD SPEED PATH V1
    #
    # 콜라 하나 주세요
    # 제로콜라 두개 주세요
    # 사이다 주세요
    #
    # 명백한 standalone drink 주문만 처리.
    # ========================================================

    if pending is None:

        drink_product = _product(
            text
        )

        simple_drink_match = re.fullmatch(
            r"(?:"
            r"제로콜라|"
            r"아이스커피|"
            r"스프라이트|사이다|"
            r"환타|판타|"
            r"콜라"
            r")"
            r"(?:"
            r"\d{1,2}(?:개|잔)?|"
            r"한개|한잔|하나|"
            r"두개|둘|"
            r"세개|셋|"
            r"네개|넷|"
            r"다섯개|다섯|"
            r"여섯|일곱|여덟|아홉|열"
            r")?"
            r"(?:"
            r"주세요|줘요|줘|"
            r"주문해주세요|주문해줘|"
            r"추가해주세요|추가해줘"
            r")",
            compact,
        )

        if (
            simple_drink_match is not None
            and drink_product is not None
            and drink_product[1] == "drink"
        ):

            target, domain = (
                drink_product
            )

            return _output({
                "family":
                    "order_action",

                "subtype":
                    "add",

                "speech_act":
                    "request",

                "commitment":
                    "explicit",

                "target_domain":
                    domain,

                "target":
                    target,

                "quantity":
                    _quantity(text),

                "reference": {
                    "source":
                        "explicit",

                    "resolved":
                        True,

                    "value":
                        target,

                    "line_ids":
                        [],
                },

                "resolution":
                    "clear",
            })

    # ========================================================
    # 0.5 COMPOUND ORDER PARSER V2
    #
    # 독립적인 신규 ADD가 여러 개 나열된 경우
    # compound_order_fastpath가 안전하게 해석 가능하면
    # 첫 Router V14 호출을 생략한다.
    #
    # 예:
    #   치킨버거 단품 하나랑 콜라 미디엄 하나랑
    #   치즈스틱 하나 주세요
    #
    #   콜라 미디엄 하나랑 사이다 라지 하나 주세요
    #
    # 위험/복잡 문장은 parser가 None을 반환하고
    # 기존 V14로 fallback한다.
    # ========================================================

    try:
        from compound_order_fastpath import (
            build_compound_order_update,
        )

        compound_update = (
            build_compound_order_update(
                text,
                pending=pending,
                order_state=order_state,
            )
        )

    except Exception:
        compound_update = None

    if compound_update is not None:

        compound_acts = []

        for action in (
            compound_update.get(
                "actions",
                [],
            )
        ):
            item = (
                action.get("item")
                or {}
            )

            item_type = item.get(
                "item_type"
            )

            if item_type == "burger":
                domain = "burger"
                target = item.get(
                    "menu"
                )

            elif item_type == "drink":
                domain = "drink"
                target = item.get(
                    "drink"
                )

            elif item_type == "side":
                domain = "side"
                target = item.get(
                    "side"
                )

            else:
                compound_acts = []
                break

            if not target:
                compound_acts = []
                break

            compound_acts.append({
                "family":
                    "order_action",
                "subtype":
                    "add",
                "speech_act":
                    "request",
                "commitment":
                    "explicit",
                "target_domain":
                    domain,
                "target":
                    target,
                "quantity":
                    int(
                        item.get(
                            "quantity",
                            1,
                        )
                    ),
                "reference": {
                    "source":
                        "explicit",
                    "resolved":
                        True,
                    "value":
                        target,
                    "line_ids": [],
                },
                "resolution":
                    "clear",
            })

        if len(compound_acts) >= 1:
            return (
                RouterOutput
                .model_validate({
                    "acts":
                        compound_acts
                })
            )

    # ========================================================
    # 1. PENDING SIDE
    #
    # T035 / T068 계열
    #
    # Router에서 side를 standalone ADD로 오해하지 않도록
    # baseline에서 사용하던 pending closed-world 형태로 넘긴다.
    # 실제 side 선택 / self-correction / finalize 해석은
    # 기존 runtime이 원문으로 처리한다.
    # ========================================================

    if (
        pending_field == "side"
        and line_id is not None
        and re.search(
            r"(감자\s*튀김|감튀|치즈\s*스틱)",
            text,
        )
    ):
        return _output({
            "family": "order_action",
            "subtype": "modify",
            "speech_act": "request",
            "commitment": "explicit",
            "target": "__pending_closed_world__",
            "reference": {
                "source": "pending",
                "resolved": True,
                "line_ids": [],
            },
            "resolution":
                "context_resolved",
        })

    # ========================================================
    # 2. CONTEXTUAL "하나 더"
    #
    # 현재 주문이 정확히 하나이고,
    # 그 항목을 안전하게 그대로 다시 추가할 수 있을 때만 처리.
    #
    # 현재는:
    # - plain side
    # - plain single burger
    # 만 허용한다.
    # ========================================================

    one_more = bool(
        re.fullmatch(
            r"(?:하나|한개)"
            r"더"
            r"(?:추가)?"
            r"(?:해주세요|해줘요|해줘|주세요|줘요|줘)",
            compact,
        )
    )

    if (
        pending is None
        and one_more
        and len(items) == 1
    ):
        item = items[0]

        item_type = item.get(
            "item_type"
        )

        item_line_id = item.get(
            "line_id"
        )

        if item_type == "side":
            target = item.get(
                "side"
            )

            if target in {
                "french_fries",
                "cheese_stick",
            }:
                return _output({
                    "family": "order_action",
                    "subtype": "add",
                    "speech_act": "request",
                    "commitment": "explicit",
                    "target_domain": "side",
                    "target": target,
                    "quantity": 1,
                    "reference": {
                        "source":
                            "current_order",
                        "resolved": True,
                        "value": target,
                        "line_ids": (
                            [int(item_line_id)]
                            if item_line_id
                            is not None
                            else []
                        ),
                    },
                    "resolution":
                        "context_resolved",
                })

        if (
            item_type == "burger"
            and item.get("type")
            == "single"
            and not item.get("drink")
            and not item.get("side")
            and not item.get(
                "exclude"
            )
            and not item.get(
                "add_toppings"
            )
        ):
            target = item.get(
                "menu"
            )

            if target in {
                "bulgogi_burger",
                "chicken_burger",
                "cheese_burger",
                "shrimp_burger",
            }:
                return _output({
                    "family": "order_action",
                    "subtype": "add",
                    "speech_act": "request",
                    "commitment": "explicit",
                    "target_domain": "burger",
                    "target": target,
                    "quantity": 1,
                    "reference": {
                        "source":
                            "current_order",
                        "resolved": True,
                        "value": target,
                        "line_ids": (
                            [int(item_line_id)]
                            if item_line_id
                            is not None
                            else []
                        ),
                    },
                    "resolution":
                        "context_resolved",
                })

    # ========================================================
    # 3. PARTIAL SET
    #
    # 예:
    # "불고기버거 세트 콜라 미디엄 하나 주세요"
    #
    # burger + drink + size + set은 명확하지만
    # side가 아직 없을 때.
    #
    # 첫 Router V14만 생략하고 기존 runtime에 원문을 넘겨
    # 실제 set 생성 + side pending은 기존 로직에 맡긴다.
    # ========================================================

    if (
        pending is None
        and len(items) == 0
        and (
            "세트" in compact
            or "셋트" in compact
        )
    ):
        found = {
            "burger": [],
            "drink": [],
            "side": [],
        }

        for (
            pattern,
            target,
            domain,
        ) in PRODUCTS:
            if re.search(
                pattern,
                text,
            ):
                found[
                    domain
                ].append(
                    target
                )

        # 제로콜라 안의 "콜라" 중복 제거
        if "zero_coke" in found[
            "drink"
        ]:
            found["drink"] = [
                x
                for x in found[
                    "drink"
                ]
                if x != "coke"
            ]

        for domain in found:
            found[domain] = list(
                dict.fromkeys(
                    found[domain]
                )
            )

        has_size = bool(
            re.search(
                r"(스몰|미디움|미디엄|라지)",
                compact,
            )
        )

        order_intent = bool(
            re.search(
                r"(주세요|줘요|줘|주문|"
                r"시켜주세요|시킬게)",
                compact,
            )
        )

        unsafe = bool(
            re.search(
                r"(가능|얼마|가격|추천|"
                r"취소|삭제|교체|변경|"
                r"바꿔|아까|그거|\?)",
                compact,
            )
        )

        if (
            len(found["burger"]) == 1
            and len(found["drink"]) == 1
            and len(found["side"]) == 0
            and has_size
            and order_intent
            and not unsafe
        ):
            burger = found[
                "burger"
            ][0]

            drink = found[
                "drink"
            ][0]

            qty = _quantity(
                text
            )

            return RouterOutput.model_validate({
                "acts": [
                    {
                        "family":
                            "order_action",
                        "subtype": "add",
                        "speech_act":
                            "request",
                        "commitment":
                            "explicit",
                        "target_domain":
                            "burger",
                        "target":
                            burger,
                        "quantity":
                            qty,
                        "reference": {
                            "source":
                                "explicit",
                            "resolved":
                                False,
                            "line_ids": [],
                        },
                        "resolution":
                            "clear",
                    },
                    {
                        "family":
                            "order_action",
                        "subtype": "add",
                        "speech_act":
                            "request",
                        "commitment":
                            "explicit",
                        "target_domain":
                            "drink",
                        "target":
                            drink,
                        "quantity":
                            qty,
                        "reference": {
                            "source":
                                "explicit",
                            "resolved":
                                False,
                            "line_ids": [],
                        },
                        "resolution":
                            "clear",
                    },
                ]
            })

    # ========================================================
    # 4. NEW BURGER WITH EXPLICIT MODIFIERS
    #
    # T015 / T058 / T070 계열
    #
    # Router V14가 condition / depends_on_act를 잘못 붙이는 것을
    # 피한다.
    #
    # Router에서는 clean burger ADD만 만들고,
    # 기존 app의
    #   burger_add_has_explicit_modifiers
    #   preserve_original_runtime_text
    # 경로를 그대로 사용한다.
    # ========================================================

    if (
        pending is None
        and len(items) == 0
    ):
        burger_found = []

        burger_patterns = (
            (
                r"불고기\s*버거",
                "bulgogi_burger",
            ),
            (
                r"치킨\s*버거",
                "chicken_burger",
            ),
            (
                r"치즈\s*버거",
                "cheese_burger",
            ),
            (
                r"새우\s*버거",
                "shrimp_burger",
            ),
        )

        for pattern, target in (
            burger_patterns
        ):
            if re.search(
                pattern,
                text,
            ):
                burger_found.append(
                    target
                )

        burger_found = list(
            dict.fromkeys(
                burger_found
            )
        )

        has_modifier = bool(
            re.search(
                r"(피클|토마토|베이컨|치즈)"
                r".*"
                r"(빼|제외|없이|추가|넣|말고|하지)",
                compact,
            )
            or re.search(
                r"(빼|제외|없이|말고)"
                r".*"
                r"(피클|토마토|베이컨|치즈)",
                compact,
            )
        )

        order_intent = bool(
            re.search(
                r"(주세요|줘요|줘|추가해주|"
                r"주문|시켜주세요)",
                compact,
            )
        )

        unsafe = bool(
            re.search(
                r"(가능|얼마|가격|추천|"
                r"취소|삭제|\?)",
                compact,
            )
        )

        if (
            len(burger_found) == 1
            and has_modifier
            and order_intent
            and not unsafe
        ):
            burger = burger_found[0]

            return _output({
                "family":
                    "order_action",
                "subtype": "add",
                "speech_act":
                    "request",
                "commitment":
                    "explicit",
                "target_domain":
                    "burger",
                "target":
                    burger,
                "quantity":
                    _quantity(text),
                "reference": {
                    "source":
                        "explicit",
                    "resolved":
                        False,
                    "line_ids": [],
                },
                "resolution":
                    "clear",
            })

    return None
