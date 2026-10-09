#!/usr/bin/env python3

import re


BURGERS = {
    "bulgogi_burger",
    "chicken_burger",
    "cheese_burger",
    "shrimp_burger",
}

DRINKS = {
    "coke",
    "zero_coke",
    "sprite",
    "fanta",
    "iced_coffee",
}

SIDES = {
    "french_fries",
    "cheese_stick",
}

SIZES = {
    "small",
    "medium",
    "large",
}

TYPES = {
    "single",
    "set",
}


PRODUCT_PATTERNS = (
    (r"제로\s*콜라", "zero_coke"),
    (r"아이스\s*커피", "iced_coffee"),
    (r"사이다|스프라이트", "sprite"),
    (r"환타|판타", "fanta"),
    (r"(?<!제로)\s*콜라", "coke"),

    (r"불고기\s*버거", "bulgogi_burger"),
    (r"치킨\s*버거", "chicken_burger"),
    (r"치즈\s*버거", "cheese_burger"),
    (r"새우\s*버거", "shrimp_burger"),

    (r"감자\s*튀김", "french_fries"),
    (r"치즈\s*스틱", "cheese_stick"),
)


def _explicit_products(text):
    found = set()

    for pattern, value in PRODUCT_PATTERNS:
        if re.search(pattern, text or ""):
            found.add(value)

    # 제로콜라 내부의 '콜라' 중복 방지
    if "zero_coke" in found:
        found.discard("coke")

    return found


def _explicit_type(text):
    compact = re.sub(r"\s+", "", text or "")

    if "단품" in compact:
        return "single"

    if "세트" in compact:
        return "set"

    return None


def _explicit_size(text):
    compact = re.sub(r"\s+", "", text or "").lower()

    if "라지" in compact or "large" in compact:
        return "large"

    if (
        "미디움" in compact
        or "미디엄" in compact
        or "medium" in compact
    ):
        return "medium"

    if "스몰" in compact or "small" in compact:
        return "small"

    return None


def _state_item(state, line_id):
    for item in (state or {}).get("items", []):
        if item.get("line_id") == line_id:
            return item

    return None



def _burger_add_has_explicit_modifiers(text):
    """
    새 버거 ADD에 재료 제외 / 토핑 추가가 같이 들어 있으면
    simple_burger_add로 축약하지 않는다.

    예:
      새우버거 피클 빼서 단품 하나
      불고기버거 베이컨 추가해서 하나
      치킨버거 토마토 없이 하나

    반면:
      치즈버거 단품 하나 추가해줘
    는 '치즈'를 topping으로 오인하지 않는다.
    """

    compact = re.sub(
        r"\s+",
        "",
        str(text or "").lower(),
    )

    # 상품명 자체에 포함된 단어를 modifier로 오인하지 않도록
    # 먼저 버거 상품명을 제거한다.
    for product_name in (
        "불고기버거",
        "치킨버거",
        "치즈버거",
        "새우버거",
    ):
        compact = compact.replace(
            product_name,
            "",
        )

    # --------------------------------------------------------
    # 재료 제외
    # --------------------------------------------------------

    exclude_terms = (
        "피클",
        "양파",
        "토마토",
        "양상추",
        "치즈",
    )

    exclude_actions = (
        "빼",
        "제외",
        "없이",
    )

    for ingredient in exclude_terms:
        pos = compact.find(ingredient)

        if pos == -1:
            continue

        tail = compact[
            pos + len(ingredient):
        ]

        if any(
            action in tail
            for action in exclude_actions
        ):
            return True

    # --------------------------------------------------------
    # 토핑 추가
    # --------------------------------------------------------

    topping_terms = (
        "치즈",
        "베이컨",
        "패티",
    )

    topping_actions = (
        "추가",
        "토핑",
        "넣어",
        "더넣",
        "올려",
    )

    for topping in topping_terms:
        pos = compact.find(topping)

        if pos == -1:
            continue

        tail = compact[
            pos + len(topping):
        ]

        if any(
            action in tail
            for action in topping_actions
        ):
            return True

    return False


def build_router_fastpath(
    router_output,
    utterance,
    state,
    pending,
):
    """
    return:
        (OrderUpdate dict, reason)
        또는
        (None, fallback reason)

    원칙:
    - 단일 act만 fast path
    - 명백한 add / pending modify / finalize만
    - 조금이라도 모순이면 기존 V14 Runtime으로 fallback
    """

    try:
        data = router_output.model_dump(
            mode="json"
        )
    except Exception:
        return None, "router_dump_failed"

    acts = data.get("acts") or []

    # ============================================================
    # CONTEXTUAL TOPPING ADD -> MODIFY NORMALIZATION
    #
    # 예:
    #   "치즈버거에 치즈 추가 가능한가요?"
    #   -> "추가해주세요"
    #
    # Router가 후속 발화를 다음처럼 해석할 수 있다.
    #
    #   subtype       = add
    #   target_domain = topping
    #   target        = cheese
    #   line_ids      = [1]
    #
    # 의미상 신규 상품 ADD가 아니라 기존 주문 line의 topping 수정이므로
    # 기존 modify fastpath가 처리할 수 있게 subtype만 modify로 정규화한다.
    #
    # 안전 조건:
    # - act 1개
    # - topping domain
    # - 허용 topping
    # - current_order reference
    # - 정확히 line_id 1개 resolved
    # ============================================================

    if len(acts) == 1:

        contextual_topping_act = acts[0]

        if isinstance(contextual_topping_act, dict):

            contextual_reference = (
                contextual_topping_act.get("reference")
                or {}
            )

            contextual_line_ids = (
                contextual_reference.get("line_ids")
                or []
            )

            if (
                contextual_topping_act.get("subtype") == "add"
                and contextual_topping_act.get("target_domain") == "topping"
                and contextual_topping_act.get("target")
                in {
                    "cheese",
                    "bacon",
                    "patty",
                }
                and contextual_reference.get("source") == "current_order"
                and contextual_reference.get("resolved") is True
                and len(contextual_line_ids) == 1
            ):
                contextual_topping_act["subtype"] = "modify"
                contextual_topping_act[
                    "_contextual_topping_add"
                ] = True


    # ========================================================
    # COMPOUND / CORRECTION DIRECT COMPILER
    #
    # pre-router compound parser가 확실하게 해석한 문장은
    # act 개수와 관계없이 동일 parser의 OrderUpdate를
    # 그대로 사용한다.
    # ========================================================

    try:
        from compound_order_fastpath import (
            build_compound_order_update,
        )

        direct_compound_update = (
            build_compound_order_update(
                utterance,
                pending=pending,
                order_state=state,
            )
        )

    except Exception:
        direct_compound_update = None

    if direct_compound_update is not None:
        return (
            direct_compound_update,
            "compound_direct",
        )


    # ========================================================
    # SAFE MULTI-ACT ADD
    #
    # 명백한 복수 ADD만 deterministic하게 compile한다.
    # 조금이라도 수정/참조/세트/self-correction이면 기존
    # Runtime V14로 fallback한다.
    # ========================================================

    if len(acts) > 1:

        # ----------------------------------------------------
        # COMPOUND ORDER PARSER
        #
        # 전용 parser가 원문 전체를 안전하게 해석할 수 있으면
        # Router multi-act를 다시 추측하지 않고,
        # parser가 만든 정확한 OrderUpdate를 그대로 사용한다.
        #
        # 특히:
        #   콜라 미디엄 + 사이다 라지
        # 처럼 같은 domain의 서로 다른 속성을 보존한다.
        # ----------------------------------------------------

        try:
            from compound_order_fastpath import (
                build_compound_order_update,
            )

            compound_update = (
                build_compound_order_update(
                    utterance,
                    pending=pending,
                    order_state=state,
                )
            )

        except Exception:
            compound_update = None

        if compound_update is not None:
            return (
                compound_update,
                "compound_multi_add",
            )

        compact = re.sub(
            r"\s+",
            "",
            str(utterance or ""),
        )

        unsafe_multi_terms = (
            "세트",
            "셋트",
            "아니",
            "빼",
            "제외",
            "없이",
            "말고",
            "바꿔",
            "바꾸",
            "변경",
            "수정",
            "교체",
            "취소",
            "삭제",
            "가능",
            "추천",
            "얼마",
            "가격",
            "칼로리",
            "아까",
            "그거",
            "첫번째",
            "두번째",
            "첫 번째",
            "두 번째",
            "?",
        )

        if any(
            term in compact
            for term in unsafe_multi_terms
        ):
            return None, (
                "multi_act_unsafe"
            )

        compiled_actions = []

        burger_acts = []
        drink_acts = []

        for multi_act in acts:

            if (
                multi_act.get("family")
                != "order_action"
                or multi_act.get("subtype")
                != "add"
                or multi_act.get(
                    "commitment"
                )
                != "explicit"
                or multi_act.get(
                    "resolution"
                )
                == "ambiguous"
            ):
                return None, (
                    "multi_act_non_simple_add"
                )

            domain = multi_act.get(
                "target_domain"
            )

            target = multi_act.get(
                "target"
            )

            quantity = (
                multi_act.get(
                    "quantity"
                )
                or 1
            )

            if domain == "burger":
                if target not in BURGERS:
                    return None, (
                        "multi_act_bad_burger"
                    )

                burger_acts.append(
                    multi_act
                )

                item = {
                    "item_type":
                        "burger",
                    "quantity":
                        int(quantity),
                    "menu":
                        target,
                }

                compiled_actions.append({
                    "operation":
                        "add",
                    "item":
                        item,
                })

            elif domain == "drink":
                if target not in DRINKS:
                    return None, (
                        "multi_act_bad_drink"
                    )

                drink_acts.append(
                    multi_act
                )

                item = {
                    "item_type":
                        "drink",
                    "quantity":
                        int(quantity),
                    "drink":
                        target,
                }

                compiled_actions.append({
                    "operation":
                        "add",
                    "item":
                        item,
                })

            elif domain == "side":
                if target not in SIDES:
                    return None, (
                        "multi_act_bad_side"
                    )

                compiled_actions.append({
                    "operation":
                        "add",
                    "item": {
                        "item_type":
                            "side",
                        "quantity":
                            int(quantity),
                        "side":
                            target,
                    },
                })

            else:
                return None, (
                    "multi_act_bad_domain"
                )

        # V1에서는 burger/drink가 각각 최대 하나일 때만
        # raw utterance에서 type/size를 안전하게 붙인다.
        if (
            len(burger_acts) > 1
            or len(drink_acts) > 1
        ):
            return None, (
                "multi_act_repeated_domain"
            )

        if burger_acts:
            burger_type = (
                _explicit_type(
                    utterance
                )
            )

            if burger_type is None:
                return None, (
                    "multi_act_burger_type_missing"
                )

            for action in (
                compiled_actions
            ):
                item = (
                    action.get("item")
                    or {}
                )

                if (
                    item.get("item_type")
                    == "burger"
                ):
                    item["type"] = (
                        burger_type
                    )

        if drink_acts:
            size = _explicit_size(
                utterance
            )

            if size is None:
                return None, (
                    "multi_act_drink_size_missing"
                )

            for action in (
                compiled_actions
            ):
                item = (
                    action.get("item")
                    or {}
                )

                if (
                    item.get("item_type")
                    == "drink"
                ):
                    item[
                        "drink_size"
                    ] = size

        explicit_products = (
            _explicit_products(
                utterance
            )
        )

        act_products = {
            act.get("target")
            for act in acts
            if act.get("target")
        }

        # 실제 발화의 상품과 Router act가 정확히 일치할 때만.
        if (
            explicit_products
            and act_products
            != explicit_products
        ):
            return None, (
                "multi_act_product_conflict"
            )

        return {
            "intent": "order",
            "actions":
                compiled_actions,
        }, "simple_multi_add"

    if len(acts) != 1:
        return None, "multi_act"

    act = acts[0]

    if act.get("family") != "order_action":
        return None, "not_order_action"

    if act.get("commitment") != "explicit":
        return None, "not_explicit"

    if act.get("resolution") == "ambiguous":
        return None, "ambiguous"

    subtype = act.get("subtype")
    domain = act.get("target_domain")
    target = act.get("target")
    quantity = act.get("quantity") or 1

    explicit_products = _explicit_products(
        utterance
    )

    # ========================================================
    # ADD
    # ========================================================
    if subtype == "add":

        # 발화에 제품이 명시돼 있는데 Router target과 다르면
        # Fast Path에서 절대 신뢰하지 않는다.
        if (
            explicit_products
            and target not in explicit_products
        ):
            return None, (
                "explicit_target_conflict:"
                f"{sorted(explicit_products)}"
                f"!={target}"
            )

        # ----------------------------------------------------
        # BURGER
        # ----------------------------------------------------
        if (
            domain == "burger"
            and target in BURGERS
        ):
            # 새 버거 주문에 재료 제외/토핑 추가가 같이 있으면
            # simple fast path가 옵션을 버릴 수 있으므로
            # Runtime V14에 넘긴다.
            if _burger_add_has_explicit_modifiers(
                utterance
            ):
                return None, (
                    "burger_add_has_explicit_modifiers"
                )

            item = {
                "item_type": "burger",
                "quantity": int(quantity),
                "menu": target,
            }

            burger_type = _explicit_type(
                utterance
            )

            if burger_type is not None:
                item["type"] = burger_type

            return {
                "intent": "order",
                "actions": [
                    {
                        "operation": "add",
                        "item": item,
                    }
                ],
            }, "simple_burger_add"

        # ----------------------------------------------------
        # DRINK
        # ----------------------------------------------------
        if (
            domain == "drink"
            and target in DRINKS
        ):
            item = {
                "item_type": "drink",
                "quantity": int(quantity),
                "drink": target,
            }

            size = _explicit_size(
                utterance
            )

            if size is not None:
                item["drink_size"] = size

            return {
                "intent": "order",
                "actions": [
                    {
                        "operation": "add",
                        "item": item,
                    }
                ],
            }, "simple_drink_add"

        # ----------------------------------------------------
        # SIDE
        # ----------------------------------------------------
        if (
            domain == "side"
            and target in SIDES
        ):
            return {
                "intent": "order",
                "actions": [
                    {
                        "operation": "add",
                        "item": {
                            "item_type": "side",
                            "quantity": int(quantity),
                            "side": target,
                        },
                    }
                ],
            }, "simple_side_add"

        return None, "unsupported_add"

    # ========================================================
    # REMOVE
    #
    # Router / pre-fastpath가 current_order의 정확한 line_ids를
    # resolve한 경우 Runtime V14를 다시 부르지 않는다.
    # ========================================================

    if subtype == "remove":

        reference = (
            act.get("reference")
            or {}
        )

        source = reference.get(
            "source"
        )

        resolved = bool(
            reference.get(
                "resolved"
            )
        )

        raw_line_ids = (
            reference.get(
                "line_ids"
            )
            or []
        )

        line_ids = []

        for value in raw_line_ids:
            try:
                line_id = int(
                    value
                )
            except Exception:
                return (
                    None,
                    "remove_invalid_line_id",
                )

            if line_id not in line_ids:
                line_ids.append(
                    line_id
                )

        if (
            source != "current_order"
            or not resolved
            or not line_ids
        ):
            return (
                None,
                "remove_unresolved_reference",
            )

        # quantity가 명시됐으면 line 수와 정확히 일치해야 한다.
        raw_quantity = act.get(
            "quantity"
        )

        if raw_quantity is not None:

            try:
                remove_quantity = int(
                    raw_quantity
                )
            except Exception:
                return (
                    None,
                    "remove_invalid_quantity",
                )

            if (
                remove_quantity
                != len(line_ids)
            ):
                return (
                    None,
                    "remove_quantity_line_mismatch",
                )

        existing_ids = {
            int(
                item.get(
                    "line_id"
                )
            )
            for item in (
                state.get(
                    "items",
                    [],
                )
                or []
            )
            if isinstance(
                item,
                dict,
            )
            and item.get(
                "line_id"
            )
            is not None
        }

        if any(
            line_id not in existing_ids
            for line_id in line_ids
        ):
            return (
                None,
                "remove_missing_line",
            )

        # 발화에 상품명이 명시됐다면
        # Router target과 충돌하는 경우 fastpath 금지.
        if (
            explicit_products
            and target not in explicit_products
        ):
            return (
                None,
                "remove_explicit_target_conflict",
            )

        actions = [
            {
                "operation":
                    "remove",

                "target": {
                    "line_id":
                        line_id,
                },
            }
            for line_id in line_ids
        ]

        return (
            {
                "intent":
                    "order",

                "actions":
                    actions,
            },
            "explicit_item_remove",
        )

    # ========================================================
    # MODIFY
    # ========================================================
    if subtype == "modify":

        reference = act.get("reference") or {}
        line_ids = reference.get("line_ids") or []

        line_id = None

        if len(line_ids) == 1:
            line_id = int(line_ids[0])

        # Router가 line_id를 못 줬더라도 현재 pending 대상이면 사용 가능.
        elif pending is not None:
            line_id = int(pending[0])

        # topping modify인데 현재 주문 burger가 정확히 하나라면
        # 그 burger를 deterministic하게 대상으로 사용한다.
        elif (
            act.get("target_domain") == "topping"
            and target in {
                "cheese",
                "bacon",
                "patty",
            }
        ):
            burger_line_ids = [
                int(item["line_id"])
                for item in (
                    (state or {}).get(
                        "items",
                        [],
                    )
                    or []
                )
                if (
                    item.get("item_type")
                    == "burger"
                    and item.get("line_id")
                    is not None
                )
            ]

            if len(burger_line_ids) == 1:
                line_id = burger_line_ids[0]

        if line_id is None:
            return None, "modify_without_single_line"

        item = _state_item(
            state,
            line_id,
        )

        if item is None:
            return None, "modify_missing_line"

        # ====================================================
        # CONTEXTUAL TOPPING ADD
        #
        # Router가 이전 capability 문맥을 이용해
        #
        #   add / topping / cheese / line_ids=[1]
        #
        # 로 해석한 후 위 normalization에서 modify로 바뀐
        # 경우만 여기서 직접 OrderUpdate로 컴파일한다.
        #
        # 일반 topping modify를 무조건 add로 해석하지 않는다.
        # ====================================================

        if act.get("_contextual_topping_add") is True:

            if act.get("target_domain") != "topping":
                return None, "contextual_topping_domain_mismatch"

            if target not in {
                "cheese",
                "bacon",
                "patty",
            }:
                return None, "unsupported_contextual_topping"

            if item.get("item_type") != "burger":
                return None, "contextual_topping_non_burger"

            return {
                "intent": "order",
                "actions": [
                    {
                        "operation": "modify",
                        "target": {
                            "line_id": line_id,
                        },
                        "item": None,
                        "quantity_delta": None,
                        "apply_to_all": False,
                        "exclude_add": [],
                        "exclude_remove": [],
                        "toppings_add": [
                            target,
                        ],
                        "toppings_remove": [],
                    }
                ],
            }, "contextual_topping_add"

        # ====================================================
        # EXPLICIT TOPPING MODIFY
        #
        # 예:
        #   "베이컨 추가해줘"
        #   "치즈 넣어줘"
        #   "패티 토핑 빼줘"
        #
        # line_id가 위에서 하나로 확정된 경우만 처리한다.
        # ====================================================

        if (
            act.get("target_domain") == "topping"
            and target in {
                "cheese",
                "bacon",
                "patty",
            }
        ):
            compact_utterance = re.sub(
                r"\\s+",
                "",
                str(utterance or "").lower(),
            )

            topping_add_requested = any(
                word in compact_utterance
                for word in (
                    "추가",
                    "넣어",
                    "넣을",
                    "더넣",
                    "올려",
                    "얹어",
                )
            )

            topping_remove_requested = any(
                word in compact_utterance
                for word in (
                    "빼",
                    "제거",
                    "삭제",
                )
            )

            # 추가/제거 의미가 동시에 있거나 둘 다 없으면
            # 기존 Runtime으로 안전하게 fallback.
            if (
                topping_add_requested
                == topping_remove_requested
            ):
                return None, "ambiguous_topping_modify"

            return {
                "intent": "order",
                "actions": [
                    {
                        "operation": "modify",
                        "target": {
                            "line_id": line_id,
                        },
                        "item": None,
                        "quantity_delta": None,
                        "apply_to_all": False,
                        "exclude_add": [],
                        "exclude_remove": [],
                        "toppings_add": (
                            [target]
                            if topping_add_requested
                            else []
                        ),
                        "toppings_remove": (
                            [target]
                            if topping_remove_requested
                            else []
                        ),
                    }
                ],
            }, (
                "explicit_topping_add"
                if topping_add_requested
                else "explicit_topping_remove"
            )

        patch = {}

        # ====================================================
        # PENDING DRINK SIZE FAST PATH V2 SAFE
        #
        # Fast path 허용:
        #   콜라 1개 pending -> "미디엄으로 주세요"
        #
        # Runtime fallback:
        #   세트 여러 개
        #   "미디엄이랑 라지"
        #   "미디엄 말고 라지"
        #   사이즈 + 주문확정 동시 요청
        # ====================================================

        pending_field = None

        if (
            isinstance(pending, (tuple, list))
            and len(pending) >= 2
        ):
            pending_field = pending[1]

        elif isinstance(pending, dict):
            pending_field = pending.get("field")

        if (
            target == "__pending_closed_world__"
            and pending_field == "drink_size"
        ):
            compact_pending = "".join(
                str(utterance or "")
                .lower()
                .split()
            )

            unresolved_size_items = [
                x
                for x in (state or {}).get("items", [])
                if (
                    x.get("drink_size") is None
                    and (
                        x.get("item_type") == "drink"
                        or (
                            x.get("item_type") == "burger"
                            and x.get("type") == "set"
                        )
                    )
                )
            ]

            size_hits = []

            if "스몰" in compact_pending:
                size_hits.append("small")

            if any(
                x in compact_pending
                for x in (
                    "미디엄",
                    "미디움",
                    "미듐",
                )
            ):
                size_hits.append("medium")

            if "라지" in compact_pending:
                size_hits.append("large")

            size_hits = list(dict.fromkeys(size_hits))

            correction_request = any(
                x in compact_pending
                for x in (
                    "말고",
                    "아니고",
                    "아니라",
                    "대신",
                    "바꿔",
                    "변경",
                )
            )

            finalization_request = any(
                x in compact_pending
                for x in (
                    "마무리",
                    "끝낼",
                    "끝내",
                    "확정",
                    "완료",
                )
            )

            single_pending_target = (
                len(unresolved_size_items) == 1
                and unresolved_size_items[0].get("line_id")
                == line_id
            )

            if (
                not single_pending_target
                or len(size_hits) != 1
                or correction_request
                or finalization_request
            ):
                return (
                    None,
                    "pending_drink_size_complex",
                )

            size = size_hits[0]

            return (
                {
                    "intent": "order",
                    "actions": [
                        {
                            "operation": "modify",
                            "target": {
                                "line_id": line_id,
                            },
                            "item": {
                                "drink_size": size,
                            },
                        }
                    ],
                },
                "pending_drink_size",
            )

        # 명시 단품/세트는 Router보다 raw 발화가 최우선
        explicit_type = _explicit_type(
            utterance
        )

        if explicit_type is not None:
            patch["type"] = explicit_type

        elif target in TYPES:
            patch["type"] = target

        elif target in DRINKS:
            patch["drink"] = target

        elif target in SIZES:
            patch["drink_size"] = target

        elif target in SIDES:
            patch["side"] = target

        else:
            return None, "unsupported_modify_target"

        # pending field와 patch가 모순이면 fallback
        if pending is not None:
            pending_line, pending_field = pending

            if int(pending_line) == line_id:
                if (
                    pending_field == "type"
                    and "type" not in patch
                ):
                    return None, "pending_type_conflict"

                if (
                    pending_field == "drink"
                    and "drink" not in patch
                ):
                    return None, "pending_drink_conflict"

                if (
                    pending_field == "drink_size"
                    and "drink_size" not in patch
                ):
                    return None, "pending_size_conflict"

                if (
                    pending_field == "side"
                    and "side" not in patch
                ):
                    return None, "pending_side_conflict"

        return {
            "intent": "order",
            "actions": [
                {
                    "operation": "modify",
                    "target": {
                        "line_id": line_id,
                    },
                    "item": patch,
                    "apply_to_all": False,
                }
            ],
        }, "simple_modify"

    # ========================================================
    # FINALIZE
    # ========================================================
    if subtype == "finalize":

        if pending is not None:
            return None, "finalize_with_pending"

        if not (state or {}).get("items"):
            return None, "finalize_empty_order"

        return {
            "intent": "confirm",
            "actions": [],
        }, "simple_finalize"

    # 나머지는 기존 Runtime에서 안전하게 처리
    return None, f"unsupported_subtype:{subtype}"
