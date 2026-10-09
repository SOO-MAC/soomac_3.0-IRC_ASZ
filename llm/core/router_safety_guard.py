#!/usr/bin/env python3

import re
from typing import Any

from router_schema import RouterOutput


def _clarify() -> RouterOutput:
    return RouterOutput.model_validate(
        {
            "acts": [
                {
                    "family": "clarify",
                    "subtype": "clarification",
                    "speech_act": "request",
                    "commitment": "none",
                    "reference": {
                        "source": "conversation_history",
                        "resolved": False,
                        "line_ids": [],
                    },
                    "resolution": "ambiguous",
                }
            ]
        }
    )


def _deny() -> RouterOutput:
    return RouterOutput.model_validate(
        {
            "acts": [
                {
                    "family": "no_action",
                    "subtype": "deny",
                    "speech_act": "deny",
                    "commitment": "none",
                    "reference": {
                        "source": "previous_staff",
                        "resolved": True,
                        "line_ids": [],
                    },
                    "resolution": "context_resolved",
                }
            ]
        }
    )


def _last_staff_text(
    history: list[dict[str, Any]],
) -> str:

    for item in reversed(history or []):
        if item.get("role") == "staff":
            text = item.get("text")
            if isinstance(text, str):
                return text

    return ""


def _items(
    order_state: dict[str, Any] | None,
) -> list[dict[str, Any]]:

    if not isinstance(order_state, dict):
        return []

    items = order_state.get("items", [])

    return items if isinstance(items, list) else []


def _has_ordinal(text: str) -> bool:
    return bool(
        re.search(
            r"(첫\s*번째|두\s*번째|세\s*번째|"
            r"1\s*번|2\s*번|3\s*번|"
            r"첫째|둘째|셋째)",
            text,
        )
    )


def _is_bare_negative(text: str) -> bool:
    t = text.strip()

    return t in {
        "아니요",
        "아뇨",
        "아니",
        "괜찮아요",
        "괜찮습니다",
        "아니요 괜찮아요",
        "아니요 괜찮습니다",
        "아뇨 괜찮아요",
    }


def _is_bare_affirm(text: str) -> bool:
    t = text.strip()

    return t in {
        "네",
        "예",
        "넵",
        "네 해주세요",
        "예 해주세요",
    }


def _explicit_staff_language(text: str) -> bool:
    return bool(
        re.search(
            r"(직원|직원분|사람).*(불러|호출|와|오|이야기|말하고|상담)"
            r"|(불러|호출|이야기|상담).*(직원|직원분|사람)"
            r"|직원\s*(안|말고|취소|필요\s*없)",
            text,
        )
    )


def _prospective_deny(text: str) -> bool:
    """
    Do NOT interpret these as cancellation of an already
    committed order.
    """

    return bool(
        re.search(
            r"(추가|넣|주문|확정|진행).{0,8}"
            r"(하지\s*마|말아|마세요|않을|안\s*할)",
            text,
        )
    )


def _conditional(text: str) -> bool:
    return bool(
        re.search(
            r"(가능하면|가능하다면|있으면|된다면|되면|"
            r"가능할\s*때|재고\s*있으면)",
            text,
        )
    )


def _contradictory(text: str) -> bool:

    has_yes = bool(
        re.search(
            r"(^|\s)(네|예|맞아요|맞습니다)(\s|$)",
            text,
        )
    )

    has_no = bool(
        re.search(
            r"(아니|아뇨|아니요|하지\s*마|잠깐)",
            text,
        )
    )

    if has_yes and has_no:
        return True

    # "하나 주세요 아니 잠깐만"
    if (
        re.search(
            r"(주세요|해줘|할게요|바꿔주세요)",
            text,
        )
        and re.search(
            r"(아니\s*잠깐|잠깐만|잠깐만요)",
            text,
        )
    ):
        return True

    return False


def _explicit_reset(text: str) -> bool:

    return bool(
        re.search(
            r"(주문|전체|전부).{0,8}"
            r"(초기화|리셋)"
            r"|처음부터\s*(다시|새로)",
            text,
        )
    )


def _explicit_cancel_all(text: str) -> bool:

    return bool(
        re.search(
            r"(전부|전체|모두).{0,8}"
            r"(취소|빼|삭제)"
            r"|(주문\s*)?(전부|전체|모두)\s*취소",
            text,
        )
    )



def _implicit_current_order_cancel_all(
    text: str,
    order_state: dict[str, Any] | None,
) -> bool:

    items = _items(order_state)

    if not items:
        return False

    compact = re.sub(
        r"\s+",
        "",
        str(text or "").lower(),
    )

    if re.search(
        r"(추가주문|추가|더주문|더시킬)"
        r".{0,8}"
        r"(안할|않을|말|취소)",
        compact,
    ):
        return False

    if re.search(
        r"(불고기버거|치킨버거|치즈버거|새우버거|"
        r"제로콜라|콜라|스프라이트|사이다|환타|"
        r"아이스커피|감자튀김|감튀|치즈스틱|"
        r"첫번째|두번째|세번째|네번째|"
        r"1번|2번|3번|4번|"
        r"그거|그것|그메뉴|이거|저거)",
        compact,
    ):
        return False

    return bool(
        re.fullmatch(
            r"(주문)?취소"
            r"(할게요?|할께요?|할래요?|"
            r"해주세요|해줘요?|해|할게|할께|할래|요)?",
            compact,
        )
        or re.search(
            r"(전부|전체|모두|다).{0,8}"
            r"(취소|삭제|빼|없애)",
            compact,
        )
        or re.search(
            r"주문.{0,6}"
            r"(취소|없던걸로|없던것으로)",
            compact,
        )
    )


def _duplicate_selector_is_ambiguous(
    utterance: str,
    order_state: dict[str, Any] | None,
) -> bool:

    if (
        _has_ordinal(utterance)
        or re.search(
            r"(둘\s*다|모두|전부)",
            utterance,
        )
    ):
        return False

    items = _items(order_state)

    if len(items) <= 1:
        return False

    menu_map = {
        "불고기버거": "bulgogi_burger",
        "치킨버거": "chicken_burger",
        "치즈버거": "cheese_burger",
        "새우버거": "shrimp_burger",
    }

    for korean, canonical in menu_map.items():

        if korean not in utterance:
            continue

        count = sum(
            1
            for item in items
            if item.get("menu") == canonical
        )

        if count > 1:
            return True

    # "세트 하나 라지로 바꿔주세요"
    if "세트" in utterance:

        set_count = sum(
            1
            for item in items
            if item.get("type") == "set"
        )

        if (
            set_count > 1
            and re.search(
                r"(하나|한\s*개|라지|미디움|바꿔|변경)",
                utterance,
            )
        ):
            return True

    return False


def apply_router_safety_guard(
    output: RouterOutput,
    *,
    utterance: str,
    history: list[dict[str, Any]] | None = None,
    pending: dict[str, Any] | None = None,
    order_state: dict[str, Any] | None = None,
) -> RouterOutput:

    del pending  # reserved for later deterministic resolution

    history = history or []
    text = utterance.strip()

    acts = output.acts

    # --------------------------------------------------------
    # 0. User starts an order, then immediately cancels it.
    # Final correction wins: do not mutate order state.
    # --------------------------------------------------------

    if (
        re.search(
            r"(주세요|줘요|줘|주문)",
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
        return _deny()

    # --------------------------------------------------------
    # 1. Explicit "do not do it" beats every inferred action.
    # --------------------------------------------------------

    if _prospective_deny(text):
        return _deny()

    # --------------------------------------------------------
    # 2. Contradictory / self-correcting short utterances
    # must never mutate state.
    # --------------------------------------------------------

    if _contradictory(text):
        return _clarify()

    # --------------------------------------------------------
    # 3. Bare negative reply to a staff proposal = reject it.
    # It is NOT a staff-call-cancel event.
    # --------------------------------------------------------

    if _is_bare_negative(text):
        return _deny()

    # --------------------------------------------------------
    # 4. Conditional command must never execute immediately.
    # Until condition orchestration is deterministic,
    # uncertain conditional parsing falls back to clarify.
    # --------------------------------------------------------

    if _conditional(text):

        has_unsafe_execution = any(
            act.family.value == "order_action"
            and act.commitment.value == "explicit"
            for act in acts
        )

        if has_unsafe_execution:
            return _clarify()

    # --------------------------------------------------------
    # 5. Staff events require explicit current utterance,
    # or an affirmative response to a staff-call proposal.
    # --------------------------------------------------------

    staff_acts = [
        act
        for act in acts
        if act.family.value == "staff_request"
    ]

    if staff_acts:

        staff_text = _last_staff_text(history)

        allowed_from_previous = (
            _is_bare_affirm(text)
            and bool(
                re.search(
                    r"(직원|불러|호출)",
                    staff_text,
                )
            )
        )

        if not (
            _explicit_staff_language(text)
            or allowed_from_previous
        ):
            return _clarify()

    # --------------------------------------------------------
    # 6. reset is destructive. Require explicit reset wording.
    # "다시 주문할게요" is NOT enough.
    # --------------------------------------------------------

    for act in acts:

        if (
            act.family.value == "order_action"
            and act.subtype.value == "reset"
            and not _explicit_reset(text)
        ):
            return _clarify()

    # --------------------------------------------------------
    # 7. cancel_all is destructive.
    # --------------------------------------------------------

    for act in acts:

        if (
            act.family.value == "order_action"
            and act.subtype.value == "cancel_all"
            and not (
                _explicit_cancel_all(text)
                or _implicit_current_order_cancel_all(
                    text,
                    order_state,
                )
            )
        ):
            return _clarify()

    # --------------------------------------------------------
    # 8. Multiple matching current-order candidates:
    # no silent choice.
    # --------------------------------------------------------

    has_mutation = any(
        act.family.value == "order_action"
        and act.subtype.value
        in {
            "modify",
            "remove",
        }
        for act in acts
    )

    if (
        has_mutation
        and _duplicate_selector_is_ambiguous(
            text,
            order_state,
        )
    ):
        return _clarify()

    # --------------------------------------------------------
    # 9. Any mutation already marked ambiguous stays blocked.
    # --------------------------------------------------------

    for act in acts:

        if (
            act.family.value == "order_action"
            and act.resolution.value == "ambiguous"
        ):
            return _clarify()

    return output
