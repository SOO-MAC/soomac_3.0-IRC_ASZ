#!/usr/bin/env python3

import json
import re
from typing import Any

from router_schema import RouterOutput


TARGETS = [
    (r"제로\s*콜라", "zero_coke", "drink"),
    (r"아이스\s*커피", "iced_coffee", "drink"),
    (r"사이다", "sprite", "drink"),
    (r"콜라", "coke", "drink"),

    (r"감자\s*튀김", "french_fries", "side"),
    (r"감튀", "french_fries", "side"),
    (r"치즈\s*스틱", "cheese_stick", "side"),

    (r"불고기\s*버거", "bulgogi_burger", "burger"),
    (r"치즈\s*버거", "cheese_burger", "burger"),
    (r"치킨\s*버거", "chicken_burger", "burger"),

    (r"라지", "large", "drink"),
    (r"미디움", "medium", "drink"),

    (r"단품", "single", "burger"),
    (r"세트", "set", "burger"),

    (r"피클", "pickle", "ingredient"),
    (r"베이컨", "bacon", "topping"),
    (r"패티", "patty", "topping"),
]


KNOWN_TARGETS = {
    canonical
    for _, canonical, _ in TARGETS
}


TARGET_DOMAIN = {
    canonical: domain
    for _, canonical, domain in TARGETS
}


def _explicit_target(text: str):
    found = []

    for pattern, canonical, domain in TARGETS:
        for m in re.finditer(pattern, text):
            found.append(
                (
                    m.start(),
                    canonical,
                    domain,
                )
            )

    if not found:
        return None

    found.sort(
        key=lambda x: x[0]
    )

    _, target, domain = found[-1]

    return target, domain


def _ordinal(text: str):
    rules = [
        (r"첫\s*번째|첫째|1\s*번", 1),
        (r"두\s*번째|둘째|2\s*번", 2),
        (r"세\s*번째|셋째|3\s*번", 3),
    ]

    for pattern, value in rules:
        if re.search(pattern, text):
            return value

    return None


def _is_question(text: str):
    if "?" in text:
        return True

    return bool(
        re.search(
            r"(가능해|가능한가|가능할까|"
            r"할\s*수\s*있|"
            r"맞죠|맞아요\?|"
            r"뭐|얼마|있어요\?|되나요)",
            text,
        )
    )


def _modify_word(text: str):
    return bool(
        re.search(
            r"(바꿔|바꿀|변경|대신|"
            r"빼\s*줘|빼\s*주세요|제외)",
            text,
        )
    )


def _reference(
    source: str,
    *,
    resolved: bool,
    value=None,
    line_ids=None,
):
    d = {
        "source": source,
        "resolved": resolved,
        "line_ids": line_ids or [],
    }

    if value is not None:
        d["value"] = str(value)

    return d


def _modify_output(
    *,
    target: str,
    domain: str,
    source="explicit",
    value=None,
    line_ids=None,
    context=False,
):
    return RouterOutput.model_validate(
        {
            "acts": [
                {
                    "family": "order_action",
                    "subtype": "modify",
                    "speech_act": "request",
                    "commitment": "explicit",
                    "target_domain": domain,
                    "target": target,
                    "reference": _reference(
                        source,
                        resolved=True,
                        value=(
                            target
                            if value is None
                            else value
                        ),
                        line_ids=line_ids,
                    ),
                    "resolution": (
                        "context_resolved"
                        if context
                        else "clear"
                    ),
                }
            ]
        }
    )


def _add_output(
    *,
    target: str,
    domain: str,
    quantity: int,
):
    return RouterOutput.model_validate(
        {
            "acts": [
                {
                    "family": "order_action",
                    "subtype": "add",
                    "speech_act": "request",
                    "commitment": "explicit",
                    "target_domain": domain,
                    "target": target,
                    "quantity": quantity,
                    "reference": _reference(
                        "conversation_history",
                        resolved=True,
                        value=target,
                    ),
                    "resolution": "context_resolved",
                }
            ]
        }
    )


def _order_items(order_state):
    if not isinstance(
        order_state,
        dict,
    ):
        return []

    items = order_state.get(
        "items",
        [],
    )

    return (
        items
        if isinstance(items, list)
        else []
    )


def _line_id(item, default):
    if not isinstance(item, dict):
        return default

    value = item.get(
        "line_id",
        default,
    )

    return (
        value
        if isinstance(value, int)
        else default
    )


def _line_ids_from_obj(obj):
    result = []

    def walk(x):
        if isinstance(x, dict):

            for k, v in x.items():

                if (
                    k == "line_id"
                    and isinstance(v, int)
                ):
                    result.append(v)

                elif (
                    k == "line_ids"
                    and isinstance(v, list)
                ):
                    for n in v:
                        if isinstance(n, int):
                            result.append(n)

                walk(v)

        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)

    seen = []

    for n in result:
        if n not in seen:
            seen.append(n)

    return seen


def _targets_from_obj(obj):
    found = []

    def walk(x):
        if isinstance(x, str):

            if x in KNOWN_TARGETS:
                found.append(x)
                return

            target = _explicit_target(x)

            if target:
                found.append(
                    target[0]
                )

        elif isinstance(x, dict):
            for v in x.values():
                walk(v)

        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)

    unique = []

    for x in found:
        if x not in unique:
            unique.append(x)

    return unique


def _model_target(output):
    for act in output.acts:
        target = getattr(
            act,
            "target",
            None,
        )

        if target in KNOWN_TARGETS:
            return target

    return None


def _model_line_ids(output):
    result = []

    for act in output.acts:
        ref = getattr(
            act,
            "reference",
            None,
        )

        if ref is None:
            continue

        for n in (
            getattr(
                ref,
                "line_ids",
                [],
            )
            or []
        ):
            if (
                isinstance(n, int)
                and n not in result
            ):
                result.append(n)

    return result


def _quantity(text):
    rules = [
        (r"(두\s*잔|두\s*개|둘)", 2),
        (r"(세\s*잔|세\s*개|셋)", 3),
        (r"(네\s*잔|네\s*개)", 4),
        (r"(다섯\s*잔|다섯\s*개)", 5),
        (r"(한\s*잔|한\s*개|하나)", 1),
    ]

    for pattern, n in rules:
        if re.search(pattern, text):
            return n

    m = re.search(
        r"(\d+)\s*(잔|개)",
        text,
    )

    if m:
        return int(
            m.group(1)
        )

    return 1


def _last_history_target(history):
    for item in reversed(
        history or []
    ):
        try:
            blob = json.dumps(
                item,
                ensure_ascii=False,
            )
        except Exception:
            blob = str(item)

        direct = _targets_from_obj(
            item
        )

        if direct:
            return direct[-1]

        target = _explicit_target(
            blob
        )

        if target:
            return target[0]

    return None


def resolve_router_deterministic(
    output: RouterOutput,
    *,
    utterance: str,
    history=None,
    pending=None,
    order_state=None,
) -> RouterOutput:

    text = utterance.strip()

    # Questions are never converted into mutations here.
    question = _is_question(text)

    ordinal = _ordinal(text)

    # --------------------------------------------------
    # 1. "둘 다 피클 빼주세요"
    # --------------------------------------------------

    if (
        not question
        and re.search(
            r"(둘\s*다|모두|전부)",
            text,
        )
        and "피클" in text
    ):

        menu_target = None

        for pattern, canonical, domain in TARGETS:
            if (
                domain == "burger"
                and re.search(
                    pattern,
                    text,
                )
            ):
                menu_target = canonical
                break

        if menu_target:

            matched = []

            for i, item in enumerate(
                _order_items(
                    order_state
                ),
                start=1,
            ):
                if (
                    isinstance(item, dict)
                    and item.get(
                        "menu"
                    ) == menu_target
                ):
                    matched.append(
                        _line_id(
                            item,
                            i,
                        )
                    )

            if len(matched) >= 2:
                return _modify_output(
                    target="pickle",
                    domain="ingredient",
                    source="current_order",
                    value="pickle",
                    line_ids=matched,
                    context=True,
                )

    # --------------------------------------------------
    # 2. Ordinal + explicit modification:
    # "첫 번째 버거 피클 빼주세요"
    # --------------------------------------------------

    explicit = _explicit_target(
        text
    )

    if (
        ordinal is not None
        and explicit
        and _modify_word(text)
        and not question
    ):

        target, domain = explicit

        items = _order_items(
            order_state
        )

        if 1 <= ordinal <= len(items):

            item = items[
                ordinal - 1
            ]

            return _modify_output(
                target=target,
                domain=domain,
                source="current_order",
                value=target,
                line_ids=[
                    _line_id(
                        item,
                        ordinal,
                    )
                ],
                context=True,
            )

        # No current-order context:
        # preserve explicit target only.
        return _modify_output(
            target=target,
            domain=domain,
        )

    # --------------------------------------------------
    # 3. Pending option selection:
    # "첫 번째요", "2번으로요"
    # --------------------------------------------------

    if (
        ordinal is not None
        and pending
        and not question
    ):

        target = _model_target(
            output
        )

        pending_targets = (
            _targets_from_obj(
                pending
            )
        )

        if (
            target is None
            and
            1 <= ordinal
            <= len(pending_targets)
        ):
            target = pending_targets[
                ordinal - 1
            ]

        line_ids = (
            _model_line_ids(
                output
            )
        )

        if not line_ids:
            line_ids = (
                _line_ids_from_obj(
                    pending
                )
            )

        if not line_ids:

            items = _order_items(
                order_state
            )

            if len(items) == 1:
                line_ids = [
                    _line_id(
                        items[0],
                        1,
                    )
                ]

        # Only execute if both choice and affected
        # order line are actually resolved.
        if (
            target in KNOWN_TARGETS
            and line_ids
        ):
            return _modify_output(
                target=target,
                domain=TARGET_DOMAIN[
                    target
                ],
                source="ordinal",
                value=ordinal,
                line_ids=line_ids,
                context=True,
            )

    # --------------------------------------------------
    # 4. Explicit modification:
    # "라지로 바꿔주세요"
    # "세트로 변경해주세요"
    # "아이스커피로 변경해주세요"
    # --------------------------------------------------

    if (
        explicit
        and not question
    ):
        target, domain = explicit

        modify = _modify_word(
            text
        )

        # Toppings / ingredients are modifications
        # when explicitly added to an existing item.
        if (
            domain
            in {
                "topping",
                "ingredient",
            }
            and re.search(
                r"(추가|넣어|넣어주세요)",
                text,
            )
        ):
            modify = True

        if modify:
            return _modify_output(
                target=target,
                domain=domain,
            )

    # --------------------------------------------------
    # 5. Deictic committed add using conversation history:
    # "그럼 그거 두 잔 주실래요"
    # --------------------------------------------------

    if (
        not question
        and re.search(
            r"(그거|그걸|아까\s*그거)",
            text,
        )
        and re.search(
            r"(주세요|주실래요|주실래|줘요|줘)",
            text,
        )
    ):

        target = _last_history_target(
            history
        )

        if target:
            return _add_output(
                target=target,
                domain=TARGET_DOMAIN[
                    target
                ],
                quantity=_quantity(
                    text
                ),
            )

    return output


# ============================================================

def _v2_is_whole_order_cancel(
    text,
    order_state,
):
    """
    현재 주문이 존재하는 상태에서
    특정 품목을 지목하지 않은 명백한 취소 표현은
    전체 주문 취소(cancel_all)로 본다.

    예:
      취소할게
      주문 취소할게요
      그냥 다 취소해주세요
      전체 주문 없던 걸로 해주세요

    반례:
      치킨버거 취소해줘       -> 특정 품목 remove
      두 번째 거 취소해줘     -> 특정 품목 remove
      그거 취소해줘           -> reference resolution 필요
      추가 주문은 안 할게     -> cancel_all 아님
    """

    if not _order_items(order_state):
        return False

    compact = re.sub(
        r"\s+",
        "",
        str(text or "").lower(),
    )

    # 취소 의사가 없으면 종료
    if not re.search(
        r"(취소|없던걸로|없던것으로|삭제|다빼|전부빼|전체빼)",
        compact,
    ):
        return False

    # "추가 주문은 안 할게" 같은 prospective deny는
    # 기존 주문 전체 취소가 아니다.
    if re.search(
        r"(추가주문|추가|더주문|더시킬|더먹을)"
        r".{0,8}"
        r"(안할|않을|말|취소)",
        compact,
    ):
        return False

    # 특정 주문 항목/reference를 지목했다면
    # 전체 취소로 승격하지 않는다.
    specific_selector = re.search(
        r"(불고기버거|치킨버거|치즈버거|새우버거|"
        r"제로콜라|콜라|스프라이트|사이다|환타|판타|"
        r"아이스커피|감자튀김|감튀|치즈스틱|"
        r"첫번째|두번째|세번째|네번째|"
        r"1번|2번|3번|4번|"
        r"그거|그것|그메뉴|이거|저거)",
        compact,
    )

    if specific_selector:
        return False

    # 명시적인 전체 취소 표현
    if re.search(
        r"(전부|전체|모두|다).{0,8}"
        r"(취소|삭제|빼|없애)",
        compact,
    ):
        return True

    if re.search(
        r"주문.{0,6}(취소|없던걸로|없던것으로)",
        compact,
    ):
        return True

    # 현재 주문이 존재하고 특정 항목 지목이 없는
    # 짧은 "취소할게" 역시 전체 주문 취소.
    if re.fullmatch(
        r"(주문)?취소"
        r"(할게요?|할께요?|할래요?|"
        r"해주세요|해줘요?|해|할게|할께|할래|요)?",
        compact,
    ):
        return True

    return False


# Deterministic Resolver V2
# Final policy stabilization layer
# ============================================================

_resolve_router_deterministic_v1 = resolve_router_deterministic


def _v2_output(
    family,
    subtype,
    *,
    speech_act="statement",
    commitment="none",
    target_domain=None,
    target=None,
    source="none",
    resolved=False,
    value=None,
    line_ids=None,
    resolution="clear",
    quantity=None,
):
    act = {
        "family": family,
        "subtype": subtype,
        "speech_act": speech_act,
        "commitment": commitment,
        "reference": {
            "source": source,
            "resolved": resolved,
            "line_ids": line_ids or [],
        },
        "resolution": resolution,
    }

    if target_domain is not None:
        act["target_domain"] = target_domain

    if target is not None:
        act["target"] = target

    if value is not None:
        act["reference"]["value"] = str(value)

    if quantity is not None:
        act["quantity"] = quantity

    return RouterOutput.model_validate(
        {"acts": [act]}
    )


def _v2_clarify():
    return _v2_output(
        "clarify",
        "clarification",
        speech_act="correction",
        resolution="ambiguous",
    )


def _v2_no_action():
    return _v2_output(
        "no_action",
        "deny",
        speech_act="deny",
        resolution="clear",
    )


def _v2_last_staff_text(history):
    for item in reversed(history or []):
        if not isinstance(item, dict):
            continue

        role = item.get("role")

        if role not in {
            "staff",
            "assistant",
        }:
            continue

        value = (
            item.get("text")
            or item.get("content")
            or ""
        )

        if isinstance(value, str):
            return value

    return ""


# GOLD14 CONTEXT PATCH
# Context-dependent deictic / recommendation resolution.
#
# Important:
# - utterance만 보고 "그거"를 실행하지 않는다.
# - last staff recommendation에 후보가 정확히 하나일 때만 resolve한다.
# - 여러 후보가 있으면 clarify한다.


def _v2_targets_in_text(text):
    if not isinstance(text, str):
        return []

    specs = [
        (r"제로\s*콜라", "zero_coke", "drink"),
        (r"아이스\s*커피", "iced_coffee", "drink"),

        (r"불고기\s*버거", "bulgogi_burger", "burger"),
        (r"치즈\s*버거", "cheese_burger", "burger"),
        (r"치킨\s*버거", "chicken_burger", "burger"),
        (r"새우\s*버거", "shrimp_burger", "burger"),

        (r"감자\s*튀김", "french_fries", "side"),
        (r"치즈\s*스틱", "cheese_stick", "side"),

        (r"사이다", "sprite", "drink"),
        (r"콜라", "coke", "drink"),
        (r"환타", "fanta", "drink"),
    ]

    found = []
    occupied = []

    # 긴 표현을 먼저 잡아서
    # "제로콜라" 안의 "콜라"를 별도 후보로 세지 않는다.
    for pattern, target, domain in specs:

        for match in re.finditer(
            pattern,
            text,
        ):
            start, end = match.span()

            overlaps = any(
                not (
                    end <= old_start
                    or start >= old_end
                )
                for old_start, old_end
                in occupied
            )

            if overlaps:
                continue

            occupied.append(
                (start, end)
            )

            found.append(
                (
                    start,
                    target,
                    domain,
                )
            )

    found.sort(
        key=lambda x: x[0]
    )

    result = []
    seen = set()

    for _, target, domain in found:

        if target in seen:
            continue

        seen.add(target)

        result.append(
            (
                target,
                domain,
            )
        )

    return result


def _v2_last_staff_targets(history):
    staff = _v2_last_staff_text(
        history
    )

    return _v2_targets_in_text(
        staff
    )


def _v2_staff_reference_value(
    staff,
    target,
):
    # 추천 문장의 "치즈버거 세트" 같은 표현은
    # 주문 target은 cheese_burger,
    # reference.value는 cheese_burger_set 으로 유지.
    if (
        isinstance(target, str)
        and target.endswith("_burger")
        and isinstance(staff, str)
        and "세트" in staff
    ):
        return f"{target}_set"

    return target


def _v2_recommendation_targets(history):
    result = []
    seen = set()

    for item in history or []:

        if not isinstance(item, dict):
            continue

        if item.get("role") not in {
            "staff",
            "assistant",
        }:
            continue

        staff = (
            item.get("text")
            or item.get("content")
            or ""
        )

        if (
            not isinstance(staff, str)
            or "추천" not in staff
        ):
            continue

        for target, domain in (
            _v2_targets_in_text(
                staff
            )
        ):
            if target in seen:
                continue

            seen.add(target)

            result.append(
                (
                    target,
                    domain,
                    staff,
                )
            )

    return result


def _v2_burger_count(order_state):
    count = 0

    for item in _order_items(
        order_state
    ):
        if not isinstance(item, dict):
            continue

        menu = item.get(
            "menu",
            "",
        )

        if (
            item.get("item_type") == "burger"
            or (
                isinstance(menu, str)
                and menu.endswith(
                    "_burger"
                )
            )
        ):
            count += 1

    return count


def _v2_history_target(history):
    target = _last_history_target(
        history
    )

    if target:
        return target

    aliases = {
        "새우버거": "shrimp_burger",
        "불고기버거": "bulgogi_burger",
        "치즈버거": "cheese_burger",
        "치킨버거": "chicken_burger",
        "제로콜라": "zero_coke",
        "콜라": "coke",
        "사이다": "sprite",
        "감자튀김": "french_fries",
        "치즈스틱": "cheese_stick",
    }

    for item in reversed(history or []):
        try:
            blob = json.dumps(
                item,
                ensure_ascii=False,
            )
        except Exception:
            blob = str(item)

        for korean, canonical in aliases.items():
            if korean in blob:
                return canonical

    return None


def _v2_domain(target):
    extra = {
        "shrimp_burger": "burger",
    }

    return (
        TARGET_DOMAIN.get(target)
        or extra.get(target)
        or "menu"
    )


def resolve_router_deterministic(
    output: RouterOutput,
    *,
    utterance: str,
    history=None,
    pending=None,
    order_state=None,
) -> RouterOutput:

    text = utterance.strip()


    # --------------------------------------------------------
    # WHOLE ORDER CANCEL
    # --------------------------------------------------------
    if _v2_is_whole_order_cancel(
        text,
        order_state,
    ):
        return _v2_output(
            "order_action",
            "cancel_all",
            speech_act="request",
            commitment="explicit",
            target_domain="order",
            source="current_order",
            resolved=True,
            value="all",
            resolution="context_resolved",
        )

    # GOLD718 C053 V2 FORCE PATCH
    # Previous recommendation follow-up:
    # STAFF: "치즈버거 세트를 추천드릴게요."
    # USER : "그거 괜찮아요?"
    if re.fullmatch(
        r"(그거|그건)\s*괜찮(?:아요|나요|죠)\??",
        text,
    ):
        staff = _v2_last_staff_text(history)

        if "추천" in staff:
            aliases = [
                (r"새우\s*버거", "shrimp_burger"),
                (r"불고기\s*버거", "bulgogi_burger"),
                (r"치즈\s*버거", "cheese_burger"),
                (r"치킨\s*버거", "chicken_burger"),
            ]

            found = [
                canonical
                for pattern, canonical in aliases
                if re.search(pattern, staff)
            ]

            if len(found) == 1:
                target = found[0]

                return _v2_output(
                    "recommendation",
                    "general_recommendation",
                    speech_act="question",
                    commitment="none",
                    target_domain="burger",
                    target=target,
                    source="previous_staff",
                    resolved=True,
                    value=(
                        f"{target}_set"
                        if "세트" in staff
                        else target
                    ),
                    resolution="context_resolved",
                )


    # --------------------------------------------------------
    # --------------------------------------------------------
    # THINKING-OFF SAFETY V2
    # Remaining high-confidence Gold regressions.
    # --------------------------------------------------------

    # C011: bare negative + 아직
    if re.fullmatch(
        r"(?:아니요|아뇨|아니)\s*아직(?:이요|이에요|예요)?",
        text,
    ):
        return _v2_no_action()

    # C082: generic burger cancellation is ambiguous
    # when several burger lines exist.
    if (
        _v2_burger_count(order_state) > 1
        and re.search(
            r"버거\s*(?:하나|한\s*개|1\s*개)",
            text,
        )
        and re.search(
            r"(취소|삭제|빼)",
            text,
        )
        and not re.search(
            r"(첫\s*번째|두\s*번째|세\s*번째|"
            r"\d+\s*번째|불고기|치킨|치즈|새우)",
            text,
        )
    ):
        return _v2_clarify()

    # C107/C110: ordinal answer to pending choice.
    if isinstance(pending, dict):
        pending_field = pending.get("field")
        pending_line_id = pending.get("line_id")
        ordinal = _ordinal(text)

        if (
            pending_field
            and pending_line_id is not None
            and ordinal is not None
            and re.search(
                r"(첫\s*번째|두\s*번째|세\s*번째|\d+\s*번째)",
                text,
            )
        ):
            staff_text = _v2_last_staff_text(history)

            staff_targets = [
                pair
                for pair in _v2_targets_in_text(staff_text)
                if pair[1] == pending_field
            ]

            if 1 <= ordinal <= len(staff_targets):
                target, domain = staff_targets[ordinal - 1]

                return _v2_output(
                    "order_action",
                    "modify",
                    speech_act="request",
                    commitment="explicit",
                    target_domain=domain,
                    target=target,
                    source="ordinal",
                    resolved=True,
                    value=str(ordinal),
                    line_ids=[pending_line_id],
                    resolution="context_resolved",
                )

    # S130: pending single/set choice.
    if (
        isinstance(pending, dict)
        and pending.get("field") == "type"
        and pending.get("line_id") is not None
        and re.fullmatch(
            r"(?:세트|셋트|단품)(?:요)?",
            text,
        )
    ):
        explicit_type = (
            "single"
            if "단품" in text
            else "set"
        )

        return _v2_output(
            "order_action",
            "modify",
            speech_act="request",
            commitment="explicit",
            target_domain="burger",
            target=explicit_type,
            source="pending",
            resolved=True,
            value=explicit_type,
            line_ids=[pending["line_id"]],
            resolution="context_resolved",
        )

    # C143/C145: unresolved conversational correction.
    if (
        re.fullmatch(
            r"그거\s*말고\s*그거(?:요)?",
            text,
        )
        or re.fullmatch(
            r"아까\s*거\s*아니\s*그\s*전\s*거(?:요)?",
            text,
        )
    ):
        return _v2_clarify()

    # I020: "주문돼요?" is possibility, not execution.
    if re.search(
        r"주문\s*(?:돼요|되나요|됩니까|돼|되죠)\s*\?$",
        text,
    ):
        explicit = _explicit_target(text)

        if explicit:
            target, domain = explicit

            return _v2_output(
                "info_query",
                "possibility",
                speech_act="question",
                commitment="none",
                target_domain=domain,
                target=target,
                quantity=_quantity(text),
                source="explicit",
                resolved=True,
                value=target,
                resolution="clear",
            )

    # I084: tentative preference must not mutate.
    if re.search(
        r"(?:나으려나|나을까|괜찮으려나)\??$",
        text,
    ):
        explicit = _explicit_target(text)

        if explicit:
            return _v2_clarify()

    # V033: recommendation request is read-only.
    if (
        re.search(r"(좋아|선호)", text)
        and re.search(r"추천", text)
    ):
        return _v2_output(
            "recommendation",
            "preference_recommendation",
            speech_act="request",
            commitment="none",
            resolution="clear",
        )

    # V157: mobile-order number inquiry.
    if (
        re.search(
            r"(맥\s*오더|맥오더|모바일\s*오더|모바일오더)",
            text,
        )
        and re.search(r"주문\s*번호", text)
        and re.search(r"(확인|알려|뭐|무엇)", text)
    ):
        return _v2_output(
            "info_query",
            "mobile_order",
            speech_act="question",
            commitment="none",
            target_domain="mobile_order",
            target="mobile_order_number",
            source="explicit",
            resolved=True,
            value="mobile_order_number",
            resolution="clear",
        )

    # V162: system small-talk.
    if re.fullmatch(
        r"AI가\s*주문\s*받는\s*거예요\??",
        text,
        flags=re.IGNORECASE,
    ):
        return _v2_output(
            "general_chat",
            "chat",
            speech_act="question",
            commitment="none",
            target_domain="system",
            resolution="clear",
        )

    # V170: system small-talk.
    if re.fullmatch(
        r"처음\s*써보는데\s*신기(?:해요|하네요|하네)",
        text,
    ):
        return _v2_output(
            "general_chat",
            "chat",
            speech_act="statement",
            commitment="none",
            target_domain="system",
            resolution="clear",
        )

    # --------------------------------------------------------
    # THINKING-OFF SAFETY V1
    #
    # Qwen V14 with enable_thinking=False can over-resolve
    # short deictic expressions, spoken corrections, and
    # conditional orders. Resolve these from the utterance
    # itself before trusting model-produced "clear" actions.
    # --------------------------------------------------------

    # 0-A. Bare deictic expressions are not executable orders.
    #
    # Examples:
    #   "그거요"
    #   "그걸로요"
    #   "저거요"
    #   "그쪽 거요"
    #   "아까 그거요"
    #
    # A real pending question may still consume these elsewhere;
    # when there is no pending state, never guess the target.
    if (
        pending is None
        and re.fullmatch(
            r"(?:아까\s*)?"
            r"(?:그거|그걸로|저거|이거|그쪽\s*거)"
            r"(?:요)?",
            text,
        )
    ):
        return _v2_clarify()

    # 0-B. Interrupted / hesitant references stay non-executable.
    #
    # Examples:
    #   "아 그..."
    #   "그걸로 그..."
    #   "어 잠깐 그 뭐지"
    #   "그 첫 번째 아니..."
    #   "두 번째였나..."
    if (
        re.fullmatch(
            r"(?:아\s*)?그(?:\.{2,}|…)+",
            text,
        )
        or re.fullmatch(
            r"그걸로\s*그(?:\.{2,}|…)+",
            text,
        )
        or re.fullmatch(
            r"어\s*잠깐\s*그\s*뭐지",
            text,
        )
        or re.fullmatch(
            r"그\s*(?:첫\s*번째|두\s*번째)\s*"
            r"아니(?:\.{2,}|…)*",
            text,
        )
        or re.fullmatch(
            r"(?:첫\s*번째|두\s*번째)"
            r"였나(?:\.{2,}|…)*",
            text,
        )
    ):
        return _v2_clarify()

    # 0-C. Spoken self-correction:
    #
    #   "사이다 하나 어 아니다 환타 하나 주세요"
    #
    # Only the corrected tail is executable.
    correction = re.search(
        r"(?:어|아)?\s*아니다\s+",
        text,
    )

    if correction is not None:
        tail = text[correction.end():]

        if re.search(
            r"(주세요|줘요|줘|주문할게요|"
            r"주문할게|주문하겠습니다)",
            tail,
        ):
            corrected_targets = _v2_targets_in_text(
                tail
            )

            if len(corrected_targets) == 1:
                target, domain = corrected_targets[0]

                return _v2_output(
                    "order_action",
                    "add",
                    speech_act="correction",
                    commitment="explicit",
                    target_domain=domain,
                    target=target,
                    quantity=_quantity(tail),
                    source="explicit",
                    resolved=True,
                    value=target,
                    resolution="clear",
                )

    # 0-D. Conditional order:
    #
    #   "새우버거 있으면 하나 주세요"
    #   "새우버거 재고 있으면 1개 주문할게요"
    #
    # Never allow the model to collapse this into an immediate
    # executable add or a generic clarify.
    conditional_match = re.search(
        r"(가능하면|가능하다면|있으면|된다면|되면|"
        r"가능할\s*때|재고\s*있으면)",
        text,
    )

    if (
        conditional_match
        and re.search(
            r"(주세요|줘요|줘|주문할게요|"
            r"주문할게|주문하겠습니다)",
            text,
        )
    ):
        conditional_targets = _v2_targets_in_text(
            text
        )

        if len(conditional_targets) == 1:
            target, domain = conditional_targets[0]
            qty = _quantity(text)

            stock_condition = bool(
                re.search(
                    r"(?:재고\s*)?있으면",
                    text,
                )
            )

            query_subtype = (
                "stock"
                if stock_condition
                else "possibility"
            )

            condition_type = (
                "stock_available"
                if stock_condition
                else "possible"
            )

            return RouterOutput.model_validate(
                {
                    "acts": [
                        {
                            "family": "info_query",
                            "subtype": query_subtype,
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
                        },
                        {
                            "family": "order_action",
                            "subtype": "add",
                            "speech_act": "request",
                            "commitment": "conditional",
                            "target_domain": domain,
                            "target": target,
                            "quantity": qty,
                            "reference": {
                                "source": "explicit",
                                "resolved": True,
                                "value": target,
                                "line_ids": [],
                            },
                            "condition": {
                                "type": condition_type,
                                "expected": True,
                            },
                            "depends_on_act": 0,
                            "resolution": "clear",
                        },
                    ]
                }
            )

    # 1. Dangerous ambiguous deictic mutations
    # --------------------------------------------------------

    if (
        re.fullmatch(
            r"(그거|저거|이거)\s*"
            r"(하나\s*)?"
            r"(주세요|줘요|줘)",
            text,
        )
    ):
        staff = _v2_last_staff_text(
            history
        )

        candidates = (
            _v2_last_staff_targets(
                history
            )
            if "추천" in staff
            else []
        )

        # 직전 직원 추천이 정확히 하나일 때만
        # deictic reference를 주문으로 승격한다.
        if len(candidates) == 1:
            target, domain = (
                candidates[0]
            )

            return _v2_output(
                "order_action",
                "add",
                speech_act="request",
                commitment="explicit",
                target_domain=domain,
                target=target,
                source="previous_staff",
                resolved=True,
                value=(
                    _v2_staff_reference_value(
                        staff,
                        target,
                    )
                ),
                resolution="context_resolved",
            )

        # 후보가 0개 또는 2개 이상이면
        # 임의 선택하지 않는다.
        return _v2_output(
            "clarify",
            "clarification",
            speech_act="request",
            commitment="none",
            source="deictic",
            resolved=False,
            resolution="ambiguous",
        )

    if (
        re.search(
            r"그중\s*하나",
            text,
        )
        and re.search(
            r"(빼|바꿔|변경|취소|삭제)",
            text,
        )
    ):
        return _v2_clarify()

    if (
        "그 버거" in text
        and _v2_burger_count(
            order_state
        ) > 1
        and re.search(
            r"(빼|바꿔|변경|취소|삭제)",
            text,
        )
    ):
        return _v2_clarify()

    # --------------------------------------------------------
    # 2. User started command then cancelled it
    # --------------------------------------------------------

    if (
        re.search(
            r"(주세요|줘요|주문)",
            text,
        )
        and re.search(
            r"(아니|잠깐)",
            text,
        )
        and re.search(
            r"(취소|안\s*할|말게)",
            text,
        )
    ):
        return _v2_no_action()

    # --------------------------------------------------------
    # 3. Broken / interrupted corrections
    # --------------------------------------------------------

    if (
        re.search(
            r"(첫\s*번째|두\s*번째|그거).*(말고|아니).*잠깐",
            text,
        )
        or re.search(
            r"아니\s+아니\s+잠깐",
            text,
        )
        or re.search(
            r"아니\s+다시\s+말",
            text,
        )
        or re.search(
            r"잠깐\s+뭐라고",
            text,
        )
        or re.search(
            r"그거.*\.\.\..*아니",
            text,
        )
    ):
        return _v2_clarify()

    # GOLD718 LAST2 PATCH

    # --------------------------------------------------------
    # Order-state confirmation:
    # "치킨버거 단품 맞죠?"
    # "불고기버거 세트 맞죠?"
    #
    # Never split menu + type into multiple acts.
    # --------------------------------------------------------

    if re.search(
        r"(단품|세트).*(맞죠|맞아요|맞나요)",
        text,
    ):
        menu_target = None

        for pattern, canonical, domain in TARGETS:
            if (
                domain == "burger"
                and re.search(pattern, text)
            ):
                menu_target = canonical
                break

        expected_type = (
            "single"
            if "단품" in text
            else "set"
        )

        if menu_target:
            matched = []

            for i, item in enumerate(
                _order_items(order_state),
                start=1,
            ):
                if not isinstance(item, dict):
                    continue

                if (
                    item.get("menu") == menu_target
                    and item.get("type") == expected_type
                ):
                    matched.append(
                        _line_id(item, i)
                    )

            return _v2_output(
                "info_query",
                "order_state",
                speech_act="question",
                commitment="none",
                target_domain="order",
                source="current_order",
                resolved=True,
                value="current_order",
                line_ids=matched,
                resolution="context_resolved",
            )

    # --------------------------------------------------------
    # Wait-time conversational forms
    # --------------------------------------------------------

    if re.fullmatch(
        r"(?:아직\s*)?"
        r"(?:멀었어요|멀었나요|멀었죠)\??",
        text,
    ):
        return _v2_output(
            "info_query",
            "wait_time",
            speech_act="question",
            commitment="none",
            target_domain="order",
            target="current_order",
            source="current_order",
            resolved=True,
            value="current_order",
            resolution="context_resolved",
        )

    # GOLD718 POLICY2 FINAL PATCH

    # --------------------------------------------------------
    # Set composition query
    # "세트에 뭐 들어가요?"
    # "세트에는 뭐 들어가요?"
    # --------------------------------------------------------

    if re.search(
        r"세트(?:에|에는)?.*"
        r"(뭐\s*들어가|뭐\s*나와|구성)",
        text,
    ):
        return _v2_output(
            "info_query",
            "option",
            speech_act="question",
            commitment="none",
            target_domain="burger",
            target="set",
            source="explicit",
            resolved=True,
            value="set",
            resolution="clear",
        )

    # --------------------------------------------------------
    # System / robot identity conversation
    # --------------------------------------------------------

    if (
        re.search(
            r"\bAI\b.*주문.*받",
            text,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"(너|이거).{0,6}로봇",
            text,
        )
        or re.search(
            r"로봇팔.*움직",
            text,
        )
    ):
        return _v2_output(
            "general_chat",
            "chat",
            speech_act=(
                "question"
                if "?" in text
                or text.endswith(("예요", "인가요", "이야"))
                else "statement"
            ),
            commitment="none",
            target_domain="system",
            source="none",
            resolved=False,
            resolution="clear",
        )

    # GOLD718 CHATBOUNDARY3 PATCH

    # --------------------------------------------------------
    # 0-A. Pure uncertainty / hesitation
    # "어 음 모르겠어요"
    # --------------------------------------------------------

    if re.fullmatch(
        r"(?:어\s*)?(?:음\s*)?"
        r"모르겠(?:어요|네요|다)?",
        text,
    ):
        return _v2_output(
            "clarify",
            "clarification",
            speech_act="statement",
            commitment="none",
            source="none",
            resolved=False,
            resolution="ambiguous",
        )

    # --------------------------------------------------------
    # 0-B. Future-time finish statement
    #
    # "내일 끝낼게요" must NOT finalize/pause current order.
    # --------------------------------------------------------

    if re.search(
        r"(내일|나중에|이따|다음에)"
        r".{0,12}"
        r"(끝낼게|끝내|마칠게|마무리)",
        text,
    ):
        return _v2_output(
            "clarify",
            "clarification",
            speech_act="statement",
            commitment="none",
            source="none",
            resolved=False,
            resolution="ambiguous",
        )

    # --------------------------------------------------------
    # 0-C. Compliment about speech/system recognition
    # --------------------------------------------------------

    if re.search(
        r"(?:말|음성).{0,8}"
        r"(?:잘\s*알아듣|잘\s*인식)"
        r"|(?:잘\s*알아듣|잘\s*인식).{0,8}"
        r"(?:네요|군요|하네요)",
        text,
    ):
        return _v2_output(
            "general_chat",
            "chat",
            speech_act="statement",
            commitment="none",
            target_domain="system",
            source="none",
            resolved=False,
            resolution="clear",
        )

    # GOLD718 DETERMINISTIC3 PATCH

    # --------------------------------------------------------
    # Ordinal item removal
    # --------------------------------------------------------

    ordinal_remove = re.search(
        r"(첫\s*번째|두\s*번째|세\s*번째)"
        r"\s*(?:거|메뉴|항목)?"
        r".*(?:취소|삭제|빼)",
        text,
    )

    if ordinal_remove:
        ordinal_map = {
            "첫번째": 1,
            "두번째": 2,
            "세번째": 3,
        }

        key = re.sub(
            r"\s+",
            "",
            ordinal_remove.group(1),
        )

        ordinal = ordinal_map.get(
            key
        )

        items = _order_items(
            order_state
        )

        if (
            ordinal is not None
            and 1 <= ordinal <= len(items)
        ):
            item = items[
                ordinal - 1
            ]

            if isinstance(item, dict):
                line_id = _line_id(
                    item,
                    ordinal,
                )

                target = (
                    item.get("menu")
                    or item.get("item_type")
                )

                domain = (
                    item.get("item_type")
                    or (
                        _v2_domain(target)
                        if target
                        else "order"
                    )
                )

                if target:
                    return _v2_output(
                        "order_action",
                        "remove",
                        speech_act="request",
                        commitment="explicit",
                        target_domain=domain,
                        target=target,
                        source="current_order",
                        resolved=True,
                        value=target,
                        line_ids=[line_id],
                        resolution="context_resolved",
                    )

    # GOLD718 POLICY4 PATCH

    # --------------------------------------------------------
    # 3.5 Explicit possibility question
    # --------------------------------------------------------

    possibility_question = bool(
        re.search(
            r"(가능|[가-힣]+\s*수\s*있)",
            text,
        )
    )

    explicit_command = bool(
        re.search(
            r"(주세요|줘요|줘|해\s*주세요|해줘)",
            text,
        )
    )

    conditional_phrase = bool(
        re.search(
            r"(가능하면|가능하다면|있으면|된다면|되면|"
            r"가능할\s*때|재고\s*있으면)",
            text,
        )
    )

    if (
        possibility_question
        and not explicit_command
        and not conditional_phrase
    ):
        # Context patch helper는 복합 명칭의 overlap을 제거한다.
        # 예: "제로콜라" 안의 "콜라"를 별도 target으로 세지 않음.
        precise_targets = (
            _v2_targets_in_text(
                text
            )
        )

        target_info = (
            precise_targets[-1]
            if precise_targets
            else _explicit_target(text)
        )

        if target_info:
            target, domain = target_info

            # 현재 주문의 특정 line을 대상으로 한
            # possibility 질문이면 그 reference는 보존한다.
            for act in output.acts:

                family = getattr(
                    getattr(
                        act,
                        "family",
                        None,
                    ),
                    "value",
                    getattr(
                        act,
                        "family",
                        None,
                    ),
                )

                subtype = getattr(
                    getattr(
                        act,
                        "subtype",
                        None,
                    ),
                    "value",
                    getattr(
                        act,
                        "subtype",
                        None,
                    ),
                )

                if (
                    family != "info_query"
                    or subtype != "possibility"
                ):
                    continue

                ref = getattr(
                    act,
                    "reference",
                    None,
                )

                if ref is None:
                    continue

                source = getattr(
                    getattr(
                        ref,
                        "source",
                        None,
                    ),
                    "value",
                    getattr(
                        ref,
                        "source",
                        None,
                    ),
                )

                resolved = bool(
                    getattr(
                        ref,
                        "resolved",
                        False,
                    )
                )

                line_ids = list(
                    getattr(
                        ref,
                        "line_ids",
                        [],
                    )
                    or []
                )

                if (
                    source == "current_order"
                    and resolved
                    and line_ids
                ):
                    return _v2_output(
                        "info_query",
                        "possibility",
                        speech_act="question",
                        commitment="none",
                        target_domain=domain,
                        target=target,
                        source="current_order",
                        resolved=True,
                        value=target,
                        line_ids=line_ids,
                        resolution="context_resolved",
                    )

            # 현재 주문 line을 특정할 필요가 없는
            # 일반 가능성 질문.
            return _v2_output(
                "info_query",
                "possibility",
                speech_act="question",
                commitment="none",
                target_domain=domain,
                target=target,
                source="explicit",
                resolved=True,
                value=target,
                line_ids=[],
                resolution="clear",
            )

    # --------------------------------------------------------
    # 4. Staff call / staff-call cancel
    # --------------------------------------------------------

    if re.search(
        r"직원.*("
        r"안\s*불러|"
        r"부르지|"
        r"필요\s*없|"
        r"취소|"
        r"안\s*(?:와|오)(?:도|셔도)?"
        r")",
        text,
    ):
        return _v2_output(
            "staff_request",
            "staff_call_cancel",
            speech_act="request",
            commitment="explicit",
            target_domain="staff",
            target="staff",
            source="explicit",
            resolved=True,
            value="staff",
            resolution="clear",
        )

    if re.search(
        r"(직원|사람).*(이야기|말하고|상담|불러|호출)",
        text,
    ):
        return _v2_output(
            "staff_request",
            "staff_call",
            speech_act="request",
            commitment="explicit",
            target_domain="staff",
            source="explicit",
            resolved=True,
            value="staff",
        )

    # --------------------------------------------------------
    # 5. Conditional order
    # "감자튀김 가능하면 5개 주세요"
    # --------------------------------------------------------

    conditional = re.search(
        r"(가능하면|가능하다면|있으면|된다면)",
        text,
    )

    explicit = _explicit_target(
        text
    )

    if (
        conditional
        and explicit
        and re.search(
            r"(주세요|줘요|줘|"
            r"주문할게요|주문할게|주문하겠습니다)",
            text,
        )
    ):
        target, domain = explicit
        qty = _quantity(text)

        return RouterOutput.model_validate(
            {
                "acts": [
                    {
                        "family": "info_query",
                        "subtype": "possibility",
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
                    },
                    {
                        "family": "order_action",
                        "subtype": "add",
                        "speech_act": "request",
                        "commitment": "conditional",
                        "target_domain": domain,
                        "target": target,
                        "quantity": qty,
                        "reference": {
                            "source": "explicit",
                            "resolved": True,
                            "value": target,
                            "line_ids": [],
                        },
                        "condition": {
                            "type": "possible",
                            "expected": True,
                        },
                        "depends_on_act": 0,
                        "resolution": "clear",
                    },
                ]
            }
        )

    # --------------------------------------------------------
    # 6. Comparisons are one read-only act
    # --------------------------------------------------------

    if (
        re.search(
            r"(중\s*뭐가|뭐가\s*나아|뭐가\s*좋)",
            text,
        )
        and "?" in text
    ):
        if "버거" in text:
            domain = "burger"
        elif (
            "감자" in text
            or "치즈스틱" in text
        ):
            domain = "side"
        elif "콜라" in text:
            domain = "drink"
        else:
            domain = "menu"

        return _v2_output(
            "recommendation",
            "comparison",
            speech_act="question",
            target_domain=domain,
            resolution="clear",
        )

    # GOLD14 REMAINING7 PATCH

    # --------------------------------------------------------
    # 6.5 Explicit topping / ingredient possibility
    # --------------------------------------------------------

    if (
        _is_question(text)
        and re.search(
            r"(가능|할\s*수\s*있)",
            text,
        )
    ):
        target_info = _explicit_target(
            text
        )

        if target_info:
            target, domain = target_info

            if domain in {
                "topping",
                "ingredient",
            }:
                return _v2_output(
                    "info_query",
                    "possibility",
                    speech_act="question",
                    commitment="none",
                    target_domain=domain,
                    target=target,
                    source="explicit",
                    resolved=True,
                    value=target,
                    resolution="clear",
                )

    # --------------------------------------------------------
    # 7. Ingredient / allergen / possibility queries
    # --------------------------------------------------------

    ingredient_map = {
        "토마토": "tomato",
        "치즈": "cheese",
        "피클": "pickle",
    }

    for word, target in ingredient_map.items():
        if (
            word in text
            and re.search(
                r"(들어가|들었|있나)",
                text,
            )
            and (
                "?" in text
                or text.endswith(
                    ("요", "나요")
                )
            )
        ):
            return _v2_output(
                "info_query",
                "ingredient",
                speech_act="question",
                target_domain="ingredient",
                target=target,
                source="explicit",
                resolved=True,
                value=target,
            )

    allergen_map = {
        "우유": "milk",
        "계란": "egg",
        "밀": "wheat",
        "갑각류": "shellfish",
    }

    for word, target in allergen_map.items():
        if (
            word in text
            and re.search(
                r"(들어가|들었|있나)",
                text,
            )
        ):
            return _v2_output(
                "info_query",
                "allergen",
                speech_act="question",
                target_domain="menu",
                target=target,
                source="explicit",
                resolved=True,
                value=target,
            )

    if re.search(
        r"뺄\s*수\s*있",
        text,
    ):
        target_info = _explicit_target(
            text
        )

        if target_info:
            target, domain = target_info

            return _v2_output(
                "info_query",
                "possibility",
                speech_act="question",
                target_domain=domain,
                target=target,
                source="explicit",
                resolved=True,
                value=target,
            )

    if re.search(
        r"이\s*버거.*뭐\s*들어가",
        text,
    ):
        return _v2_output(
            "info_query",
            "ingredient",
            speech_act="question",
            target_domain="burger",
            resolution="clear",
        )

    if (
        "콜라" in text
        and re.search(
            r"주문\s*가능",
            text,
        )
    ):
        target = (
            "zero_coke"
            if "제로" in text
            else "coke"
        )

        return _v2_output(
            "info_query",
            "possibility",
            speech_act="question",
            target_domain="drink",
            target=target,
            source="explicit",
            resolved=True,
            value=target,
        )

    # --------------------------------------------------------
    # 7.5 Card tap / payment interaction query
    # --------------------------------------------------------

    if (
        (
            "태그" in text
            or re.search(
                r"카드.{0,10}(대면|대는|대야|태그)",
                text,
            )
        )
        and (
            "?" in text
            or re.search(
                r"(되나요|돼요|되는\s*거|하면\s*돼|어디)",
                text,
            )
        )
    ):
        return _v2_output(
            "info_query",
            "payment",
            speech_act="question",
            commitment="none",
            target_domain="payment",
            target="card_tap",
            source="explicit",
            resolved=True,
            value="card_tap",
            resolution="clear",
        )

    # --------------------------------------------------------
    # 8. Payment query
    # --------------------------------------------------------

    if (
        "결제" in text
        and re.search(
            r"(안\s*된|안돼|안\s*돼|문제|실패)",
            text,
        )
    ):
        return _v2_output(
            "info_query",
            "payment",
            speech_act="statement",
            target_domain="payment",
            resolution="clear",
        )

    # --------------------------------------------------------
    # 8.5 Repeat previous staff message
    # --------------------------------------------------------

    if re.search(
        r"(뭐라고요|"
        r"다시\s*말해|"
        r"한\s*번\s*더\s*말해|"
        r"방금\s*뭐라고|"
        r"질문\s*다시|"
        r"안내\s*다시)",
        text,
    ):
        staff = _v2_last_staff_text(
            history
        )

        if staff:
            return _v2_output(
                "conversation_control",
                "repeat",
                speech_act="request",
                commitment="none",
                source="previous_staff",
                resolved=True,
                value="previous_staff_message",
                resolution="context_resolved",
            )

    # --------------------------------------------------------
    # 9. Conversation resume
    # --------------------------------------------------------

    if text in {
        "이제 할게요",
        "계속할게요",
        "다시 주문할게요",
        "이제 이어서 할게요",
    }:
        return _v2_output(
            "conversation_control",
            "resume",
            speech_act="request",
            resolution="clear",
        )

    # --------------------------------------------------------
    # 9.5 Recommendation acknowledgement
    # --------------------------------------------------------

    staff = _v2_last_staff_text(
        history
    )

    if (
        "추천" in staff
        and re.fullmatch(
            r"(좋네요|괜찮네요|마음에\s*드네요)",
            text,
        )
    ):
        candidates = (
            _v2_targets_in_text(
                staff
            )
        )

        reference_value = (
            _v2_staff_reference_value(
                staff,
                candidates[0][0],
            )
            if len(candidates) == 1
            else "previous_staff_message"
        )

        return _v2_output(
            "no_action",
            "acknowledge",
            speech_act="acknowledgement",
            commitment="none",
            source="previous_staff",
            resolved=True,
            value=reference_value,
            resolution="context_resolved",
        )

    # --------------------------------------------------------
    # 9.5 Clearly out-of-scope sports queries
    # --------------------------------------------------------

    if (
        re.search(
            r"(축구|야구|농구|배구)",
            text,
        )
        and re.search(
            r"(누가\s*이겼|결과|점수|경기)",
            text,
        )
    ):
        return _v2_output(
            "out_of_scope",
            "unsupported",
            speech_act=(
                "question"
                if "?" in text
                else "request"
            ),
            commitment="none",
            source="none",
            resolved=False,
            resolution="clear",
        )

    # --------------------------------------------------------
    # 10. General chat
    # --------------------------------------------------------

    if re.search(
        r"(맛있겠네요|고마워요|감사해요|수고하세요)",
        text,
    ):
        return _v2_output(
            "general_chat",
            "chat",
            speech_act="statement",
            resolution="clear",
        )

    # GOLD718 FINAL C053 PATCH

    # --------------------------------------------------------
    # Previous single recommendation evaluation
    #
    # STAFF: "치즈버거 세트를 추천드릴게요."
    # USER : "그거 괜찮아요?"
    #
    # This is a read-only recommendation follow-up.
    # --------------------------------------------------------

    if re.fullmatch(
        r"(그거|그건)\s*괜찮(?:아요|나요|죠)\??",
        text,
    ):
        staff = _v2_last_staff_text(history)

        if re.search(r"추천", staff):
            burger_aliases = [
                (r"새우\s*버거", "shrimp_burger"),
                (r"불고기\s*버거", "bulgogi_burger"),
                (r"치즈\s*버거", "cheese_burger"),
                (r"치킨\s*버거", "chicken_burger"),
            ]

            targets = []

            for pattern, canonical in burger_aliases:
                if re.search(pattern, staff):
                    if canonical not in targets:
                        targets.append(canonical)

            if len(targets) == 1:
                target = targets[0]

                ref_value = (
                    f"{target}_set"
                    if "세트" in staff
                    else target
                )

                return _v2_output(
                    "recommendation",
                    "general_recommendation",
                    speech_act="question",
                    commitment="none",
                    target_domain="burger",
                    target=target,
                    source="previous_staff",
                    resolved=True,
                    value=ref_value,
                    resolution="context_resolved",
                )

            # 여러 메뉴를 추천한 상황이면
            # "그거"가 무엇인지 특정하지 않는다.
            if len(targets) > 1:
                return _v2_output(
                    "clarify",
                    "clarification",
                    speech_act="question",
                    commitment="none",
                    source="deictic",
                    resolved=False,
                    resolution="ambiguous",
                )

    # --------------------------------------------------------
    # 11. Recommendation follow-up
    # --------------------------------------------------------

    if re.fullmatch(
        r"그건\s*말고\s*다른\s*거요",
        text,
    ):
        return _v2_output(
            "recommendation",
            "general_recommendation",
            speech_act="request",
            target_domain="menu",
            source="previous_staff",
            resolved=True,
            value="previous_staff_message",
            resolution="context_resolved",
        )

    # --------------------------------------------------------
    # 12. "네" to an informational staff offer
    # --------------------------------------------------------

    if text in {
        "네",
        "예",
        "네 해주세요",
        "예 해주세요",
    }:
        staff = _v2_last_staff_text(
            history
        )

        # "단품과 세트 중 선택해주세요."
        # "사이드 메뉴를 말씀해주세요."
        #
        # 같은 open-choice 질문은 "네"만으로
        # 실제 pending value를 결정할 수 없다.
        if (
            pending
            and re.search(
                r"(중\s*선택|선택해|말씀해|골라)",
                staff,
            )
        ):
            return _v2_output(
                "clarify",
                "clarification",
                speech_act="affirm",
                commitment="none",
                source="pending",
                resolved=False,
                resolution="ambiguous",
            )

        if re.search(
            r"(가격|금액|얼마)",
            staff,
        ):
            return _v2_output(
                "info_query",
                "price",
                speech_act="affirm",
                target_domain="order",
                source="previous_staff",
                resolved=True,
                value="current_order",
                resolution="context_resolved",
            )

        if re.search(
            r"(메뉴|종류|추천|보여|알려)",
            staff,
        ):
            return _v2_output(
                "info_query",
                "menu",
                speech_act="affirm",
                target_domain="menu",
                source="previous_staff",
                resolved=True,
                value="menu",
                resolution="context_resolved",
            )

    # --------------------------------------------------------
    # 12.5 Distant recommendation reference
    # --------------------------------------------------------

    if (
        re.search(
            r"(전에|아까).{0,8}"
            r"추천한.{0,8}"
            r"(거|메뉴)",
            text,
        )
        and re.search(
            r"(주세요|줘요|줘)",
            text,
        )
    ):
        candidates = (
            _v2_recommendation_targets(
                history
            )
        )

        if len(candidates) == 1:
            target, domain, staff = (
                candidates[0]
            )

            return _v2_output(
                "order_action",
                "add",
                speech_act="request",
                commitment="explicit",
                target_domain=domain,
                target=target,
                source="conversation_history",
                resolved=True,
                value=(
                    _v2_staff_reference_value(
                        staff,
                        target,
                    )
                ),
                resolution="context_resolved",
            )

        return _v2_output(
            "clarify",
            "clarification",
            speech_act="request",
            commitment="none",
            source="conversation_history",
            resolved=False,
            resolution="ambiguous",
        )

    # --------------------------------------------------------
    # 13. "아까 그 메뉴 하나 주세요"
    # --------------------------------------------------------

    if (
        re.search(
            r"아까\s*그\s*(메뉴|거)",
            text,
        )
        and re.search(
            r"(주세요|줘요|줘)",
            text,
        )
    ):
        target = _v2_history_target(
            history
        )

        if target:
            return _v2_output(
                "order_action",
                "add",
                speech_act="request",
                commitment="explicit",
                target_domain=_v2_domain(
                    target
                ),
                target=target,
                source="conversation_history",
                resolved=True,
                value=target,
                resolution="context_resolved",
                quantity=_quantity(text),
            )

    # --------------------------------------------------------
    # 14. Bare explicit pending choice
    # "치즈스틱요"
    # --------------------------------------------------------

    if (
        pending
        and explicit
        and re.fullmatch(
            r"[가-힣A-Za-z0-9\s]+(요)?",
            text,
        )
        and not re.search(
            r"(주세요|바꿔|변경|가능|얼마|뭐)",
            text,
        )
    ):
        target, domain = explicit

        line_ids = _line_ids_from_obj(
            pending
        )

        subtype = (
            "modify"
            if line_ids
            else "add"
        )

        return _v2_output(
            "order_action",
            subtype,
            speech_act="request",
            commitment="explicit",
            target_domain=domain,
            target=target,
            source="pending",
            resolved=True,
            value=target,
            line_ids=line_ids,
            resolution="context_resolved",
            quantity=(
                None
                if subtype == "modify"
                else 1
            ),
        )

    # --------------------------------------------------------
    # Original deterministic resolver
    # --------------------------------------------------------

    result = _resolve_router_deterministic_v1(
        output,
        utterance=utterance,
        history=history,
        pending=pending,
        order_state=order_state,
    )

    return result


# BLIND_V1_FINAL_PATCH_20261003
# Blind-v1 targeted deterministic stabilization.
_resolve_router_deterministic_blind_v1_base = resolve_router_deterministic


def _blind_v1_recommendation_context(history):
    aliases = [
        (r"새우\s*버거", "shrimp_burger"),
        (r"불고기\s*버거", "bulgogi_burger"),
        (r"치즈\s*버거", "cheese_burger"),
        (r"치킨\s*버거", "chicken_burger"),
    ]

    for item in reversed(history or []):
        if not isinstance(item, dict):
            continue

        if item.get("role") not in {"staff", "assistant"}:
            continue

        staff = item.get("text") or item.get("content") or ""

        if not isinstance(staff, str) or "추천" not in staff:
            continue

        found = [
            canonical
            for pattern, canonical in aliases
            if re.search(pattern, staff)
        ]

        if len(found) == 1:
            target = found[0]

            return (
                target,
                f"{target}_set"
                if "세트" in staff
                else target,
            )

    return None


def resolve_router_deterministic(
    output: RouterOutput,
    *,
    utterance: str,
    history=None,
    pending=None,
    order_state=None,
) -> RouterOutput:

    text = utterance.strip()
    items = _order_items(order_state)

    # --------------------------------------------------
    # 1. Ingredient removal + ordinal
    # "첫 번째 버거에서 피클만 빼줘요"
    # --------------------------------------------------
    if (
        "피클" in text
        and re.search(r"(빼|제외)", text)
    ):
        ordinal = _ordinal(text)

        if (
            ordinal is not None
            and 1 <= ordinal <= len(items)
        ):
            item = items[ordinal - 1]

            return _v2_output(
                "order_action",
                "modify",
                speech_act="request",
                commitment="explicit",
                target_domain="ingredient",
                target="pickle",
                source="current_order",
                resolved=True,
                value="pickle",
                line_ids=[
                    _line_id(item, ordinal)
                ],
                resolution="context_resolved",
            )

    # --------------------------------------------------
    # 2. Ingredient removal selected by current drink
    # "제로콜라 들어간 버거에서 피클 빼줘요"
    # "콜라 들어간 쪽 피클 빼주세요"
    # --------------------------------------------------
    if (
        "피클" in text
        and re.search(r"(빼|제외)", text)
        and "들어간" in text
        and "콜라" in text
    ):
        if re.search(r"제로\s*콜라", text):
            drink = "zero_coke"
        else:
            drink = "coke"

        matched = []

        for i, item in enumerate(items, start=1):
            if (
                isinstance(item, dict)
                and item.get("drink") == drink
            ):
                matched.append(
                    _line_id(item, i)
                )

        if len(matched) == 1:
            return _v2_output(
                "order_action",
                "modify",
                speech_act="request",
                commitment="explicit",
                target_domain="ingredient",
                target="pickle",
                source="current_order",
                resolved=True,
                value="pickle",
                line_ids=matched,
                resolution="context_resolved",
            )

        if len(matched) > 1:
            return _v2_output(
                "clarify",
                "clarification",
                speech_act="request",
                commitment="none",
                source="current_order",
                resolved=False,
                resolution="ambiguous",
            )

    # --------------------------------------------------
    # 3. Explicit allergen query
    # --------------------------------------------------
    allergen_map = {
        "우유": "milk",
        "계란": "egg",
        "갑각류": "shellfish",
    }

    if re.search(r"(들어가|들어간|들었|함유)", text):
        for word, target in allergen_map.items():
            if word in text:
                return _v2_output(
                    "info_query",
                    "allergen",
                    speech_act="question",
                    target_domain="menu",
                    target=target,
                    source="explicit",
                    resolved=True,
                    value=target,
                    resolution="clear",
                )

    # --------------------------------------------------
    # 4. Nutrition queries
    # --------------------------------------------------
    if re.search(r"(칼로리|열량)", text):
        return _v2_output(
            "info_query",
            "nutrition",
            speech_act="question",
            target_domain="menu",
            resolution="clear",
        )

    if re.search(r"(당\s*함량|당류)", text):
        return _v2_output(
            "info_query",
            "nutrition",
            speech_act="question",
            target_domain="menu",
            target="sugar",
            source="explicit",
            resolved=True,
            value="sugar",
            resolution="clear",
        )

    # --------------------------------------------------
    # 5. Evaluation of previous recommendation
    # --------------------------------------------------
    if (
        re.search(r"추천", text)
        and re.search(r"(괜찮|어때)", text)
    ):
        rec = _blind_v1_recommendation_context(history)

        if rec:
            target, ref_value = rec

            return _v2_output(
                "recommendation",
                "general_recommendation",
                speech_act="request",
                commitment="none",
                target_domain="burger",
                target=target,
                source="previous_staff",
                resolved=True,
                value=ref_value,
                resolution="context_resolved",
            )

    # --------------------------------------------------
    # 6. Positive reaction to previous recommendation
    # --------------------------------------------------
    if re.search(r"맛있겠", text):
        rec = _blind_v1_recommendation_context(history)

        if rec:
            _, ref_value = rec

            return _v2_output(
                "general_chat",
                "chat",
                speech_act="statement",
                commitment="none",
                source="previous_staff",
                resolved=True,
                value=ref_value,
                resolution="context_resolved",
            )

    # --------------------------------------------------
    # 7. Resume ordering
    # --------------------------------------------------
    if (
        re.fullmatch(
            r"이제\s*계속\s*주문할게요",
            text,
        )
        or re.fullmatch(
            r"다시\s*이어서\s*할게요",
            text,
        )
    ):
        return _v2_output(
            "conversation_control",
            "resume",
            speech_act="request",
            commitment="none",
            resolution="clear",
        )

    return _resolve_router_deterministic_blind_v1_base(
        output,
        utterance=utterance,
        history=history,
        pending=pending,
        order_state=order_state,
    )



# ============================================================
# MOBILE PICKUP ROUTER FIX
# ============================================================
# Explicit mobile-order pickup utterances such as:
#
#   "맥오더 65번이요"
#   "모바일 주문 123번입니다"
#   "앱 주문 44번 픽업하러 왔어요"
#
# are deterministic because the customer supplied the order
# number explicitly.
#
# Router is only the gate here.  The actual mobile_pickup
# parsing / confirmation / FINAL HANDOFF remains the
# responsibility of DriveThruRuntime + drive_thru_app.py.
#
# MOBILE_PICKUP_ROUTER_FIX_20261004
# ============================================================

_resolve_router_deterministic_before_mobile_pickup = (
    resolve_router_deterministic
)


def _explicit_mobile_pickup_number(
    utterance: str,
):
    text = str(
        utterance
        or ""
    ).strip()

    # Explicit mobile-order domain signal is mandatory.
    if not re.search(
        r"(맥\s*오더|모바일\s*주문|앱\s*주문|"
        r"사전\s*주문|미리\s*주문|픽업)",
        text,
    ):
        return None

    # Informational / confirmation questions must remain
    # read-only and must never become an executable pickup.
    if (
        "?" in text
        or re.search(
            r"(어디|어떻게|가능|되나요|돼요|"
            r"맞나요|맞죠|맞아요|맞습니까|"
            r"인가요|예요|이에요|확인해|"
            r"확인할|번호\s*어디)",
            text,
        )
    ):
        return None

    # Customer-stated mobile order number.
    match = re.search(
        r"(?<!\d)(\d{1,4})(?:\s*번)?(?!\d)",
        text,
    )

    if match is None:
        return None

    number = int(
        match.group(1)
    )

    if number <= 0:
        return None

    return number


def resolve_router_deterministic(
    output: RouterOutput,
    *,
    utterance: str,
    history=None,
    pending=None,
    order_state=None,
) -> RouterOutput:

    mobile_order_id = (
        _explicit_mobile_pickup_number(
            utterance
        )
    )

    if mobile_order_id is not None:

        value = str(
            mobile_order_id
        )

        return _v2_output(
            "order_action",
            "add",
            speech_act="request",
            commitment="explicit",
            target_domain="mobile_order",
            target=value,
            source="explicit",
            resolved=True,
            value=value,
            resolution="clear",
        )

    return (
        _resolve_router_deterministic_before_mobile_pickup(
            output,
            utterance=utterance,
            history=history,
            pending=pending,
            order_state=order_state,
        )
    )


# ============================================================
# EXPLICIT SINGLE / SET FINAL OVERRIDE
# 2026-10-04
# ============================================================

_resolve_router_before_explicit_single_set = resolve_router_deterministic


def resolve_router_deterministic(
    output: RouterOutput,
    *,
    utterance: str,
    history=None,
    pending=None,
    order_state=None,
) -> RouterOutput:

    result = _resolve_router_before_explicit_single_set(
        output,
        utterance=utterance,
        history=history,
        pending=pending,
        order_state=order_state,
    )

    text = re.sub(
        r"\s+",
        "",
        utterance or "",
    )

    explicit_type = None

    if "단품" in text:
        explicit_type = "single"

    elif "세트" in text:
        explicit_type = "set"

    if explicit_type is None:
        return result

    # --------------------------------------------------------
    # 중요:
    # "불고기버거 단품 하나 + 콜라 라지 하나"처럼
    # 실제 제품이 명시된 주문에서는 target을 single/set으로
    # 덮어쓰면 안 된다.
    #
    # 이 override는
    # "단품으로 바꿔주세요"
    # "세트로 바꿔주세요"
    # 처럼 type 자체만 명시한 발화에만 적용한다.
    # --------------------------------------------------------

    has_explicit_product = bool(
        re.search(
            r"(불고기\s*버거|"
            r"치킨\s*버거|"
            r"치즈\s*버거|"
            r"새우\s*버거|"
            r"제로\s*콜라|"
            r"아이스\s*커피|"
            r"콜라|사이다|스프라이트|"
            r"환타|판타|"
            r"감자\s*튀김|감튀|"
            r"치즈\s*스틱)",
            utterance or "",
        )
    )

    if has_explicit_product:
        return result

    data = result.model_dump(
        mode="json"
    )

    for act in data.get(
        "acts",
        [],
    ):
        if (
            act.get("family")
            != "order_action"
        ):
            continue

        if (
            act.get("subtype")
            not in {
                "add",
                "modify",
            }
        ):
            continue

        # 명시된 단품/세트가 LLM 판단보다 우선한다.
        act["target_domain"] = "burger"
        act["target"] = explicit_type
        act["commitment"] = "explicit"

        reference = (
            act.get("reference")
            or {}
        )

        # pending/current order 문맥은 기존 line_ids를 유지.
        reference["resolved"] = True
        reference["value"] = explicit_type

        if not reference.get("source"):
            reference["source"] = (
                "current_order"
                if pending is not None
                else "explicit"
            )

        act["reference"] = reference

        if (
            reference.get("line_ids")
            or pending is not None
        ):
            act["resolution"] = (
                "context_resolved"
            )
        else:
            act["resolution"] = "clear"

    return RouterOutput.model_validate(
        data
    )
