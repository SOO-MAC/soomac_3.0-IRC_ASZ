#!/usr/bin/env python3

import json
import re
from copy import deepcopy
from typing import Any

from router_schema import RouterOutput


# ------------------------------------------------------------
# Canonical lexical targets
# ------------------------------------------------------------

ALIASES = [
    # Long aliases first is not required because we resolve by
    # position + alias length, but keeping this readable helps.
    ("아이스 아메리카노", "iced_coffee", "drink"),
    ("아이스아메리카노", "iced_coffee", "drink"),
    ("아이스 커피", "iced_coffee", "drink"),
    ("아이스커피", "iced_coffee", "drink"),

    ("불고기버거", "bulgogi_burger", "burger"),
    ("치킨버거", "chicken_burger", "burger"),
    ("치즈버거", "cheese_burger", "burger"),
    ("새우버거", "shrimp_burger", "burger"),

    ("제로 콜라", "zero_coke", "drink"),
    ("제로콜라", "zero_coke", "drink"),

    ("감자튀김", "french_fries", "side"),
    ("치즈스틱", "cheese_stick", "side"),

    ("사이다", "sprite", "drink"),
    ("환타", "fanta", "drink"),
    ("콜라", "coke", "drink"),

    ("감튀", "french_fries", "side"),
    ("아아", "iced_coffee", "drink"),

    ("라지", "large", "drink"),
    ("미디움", "medium", "drink"),

    ("단품", "single", "burger"),
    ("세트", "set", "burger"),

    ("카드", "card", "payment"),
    ("직원", "staff", "staff"),
]


TARGET_DOMAIN = {
    target: domain
    for _, target, domain in ALIASES
}


BURGER_TARGETS = {
    "bulgogi_burger",
    "chicken_burger",
    "cheese_burger",
    "shrimp_burger",
}


def _find_explicit_target(
    text: str,
) -> tuple[str | None, str | None]:

    candidates = []

    for alias, target, domain in ALIASES:

        for match in re.finditer(
            re.escape(alias),
            text,
        ):
            candidates.append(
                (
                    match.end(),
                    len(alias),
                    target,
                    domain,
                )
            )

    if not candidates:
        return None, None

    # Last semantic mention wins.
    # Same ending position -> longer alias wins.
    _, _, target, domain = max(
        candidates,
        key=lambda x: (
            x[0],
            x[1],
        ),
    )

    return target, domain


def _last_staff_text(
    history: list[dict[str, Any]],
) -> str | None:

    for item in reversed(history or []):

        if item.get("role") == "staff":

            text = item.get("text")

            if isinstance(text, str):
                return text

    return None


def _extract_staff_proposal(
    text: str | None,
) -> tuple[
    str | None,
    str | None,
    str | None,
]:

    if not text:
        return None, None, None

    # Recommendation:
    # "치즈버거 세트를 추천..."
    # target       = cheese_burger
    # ref.value    = cheese_burger_set
    if "추천" in text:

        found = []

        for alias, target, domain in ALIASES:

            if (
                target not in BURGER_TARGETS
                and domain not in {
                    "drink",
                    "side",
                }
            ):
                continue

            match = re.search(
                re.escape(alias),
                text,
            )

            if match:
                found.append(
                    (
                        match.start(),
                        len(alias),
                        target,
                        domain,
                    )
                )

        if found:

            _, _, target, domain = min(
                found,
                key=lambda x: (
                    x[0],
                    -x[1],
                ),
            )

            value = target

            if (
                domain == "burger"
                and "세트" in text
            ):
                value = target + "_set"

            return target, domain, value

    target, domain = _find_explicit_target(
        text
    )

    return target, domain, target


def _order_items(
    order_state: dict[str, Any] | None,
) -> list[dict[str, Any]]:

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


def _valid_line_ids(
    line_ids: list[int],
    order_state: dict[str, Any] | None,
) -> bool:

    if not line_ids:
        return False

    valid_ids = {
        x.get("line_id")
        for x in _order_items(
            order_state
        )
        if x.get("line_id") is not None
    }

    return all(
        line_id in valid_ids
        for line_id in line_ids
    )


def _unique_line_id(
    order_state: dict[str, Any] | None,
) -> int | None:

    items = _order_items(
        order_state
    )

    if len(items) != 1:
        return None

    value = items[0].get(
        "line_id"
    )

    return (
        value
        if isinstance(value, int)
        else None
    )


def _none_reference() -> dict[str, Any]:

    return {
        "source": "none",
        "resolved": False,
        "line_ids": [],
    }


def safe_clarify_output() -> RouterOutput:
    """
    Invalid model JSON must never become an order mutation.
    """

    return RouterOutput.model_validate(
        {
            "acts": [
                {
                    "family": "clarify",
                    "subtype": "clarification",
                    "speech_act": "statement",
                    "commitment": "none",
                    "reference":
                        _none_reference(),
                    "resolution":
                        "ambiguous",
                }
            ]
        }
    )


def _dedupe_acts(
    acts: list[dict[str, Any]],
) -> list[dict[str, Any]]:

    result = []
    seen = set()

    for act in acts:

        key = json.dumps(
            act,
            ensure_ascii=False,
            sort_keys=True,
        )

        if key in seen:
            continue

        seen.add(key)
        result.append(act)

    return result


def normalize_router_output(
    output: RouterOutput,
    *,
    utterance: str,
    history: list[dict[str, Any]] | None = None,
    pending: dict[str, Any] | None = None,
    order_state: dict[str, Any] | None = None,
) -> RouterOutput:

    history = history or []
    order_state = order_state or {
        "items": [],
    }

    data = output.model_dump(
        mode="json",
        exclude_none=True,
    )

    acts = deepcopy(
        data["acts"]
    )

    explicit_target, explicit_domain = (
        _find_explicit_target(
            utterance
        )
    )

    staff_text = _last_staff_text(
        history
    )

    (
        staff_target,
        staff_domain,
        staff_reference_value,
    ) = _extract_staff_proposal(
        staff_text
    )

    for act in acts:

        family = act["family"]
        subtype = act["subtype"]

        reference = act.setdefault(
            "reference",
            _none_reference(),
        )

        reference.setdefault(
            "line_ids",
            [],
        )

        # ====================================================
        # 1. STAFF REQUEST
        # ====================================================

        if family == "staff_request":

            act["target_domain"] = (
                "staff"
            )

            act["target"] = "staff"

            act["reference"] = {
                "source": "explicit",
                "resolved": True,
                "value": "staff",
                "line_ids": [],
            }

            act["resolution"] = "clear"

            continue

        # ====================================================
        # 2. CLARIFY
        # ====================================================

        if family == "clarify":

            act["resolution"] = (
                "ambiguous"
            )

            if any(
                token in utterance
                for token in [
                    "그거",
                    "저거",
                    "그 메뉴",
                    "그 버거",
                    "그걸",
                ]
            ):
                act["reference"] = {
                    "source": "deictic",
                    "resolved": False,
                    "line_ids": [],
                }

            if "주세요" in utterance:
                act["speech_act"] = (
                    "request"
                )

            continue

        # ====================================================
        # 3. PREVIOUS STAFF
        # ====================================================

        use_previous_staff = (
            reference.get("source")
            == "previous_staff"
            or (
                utterance.strip()
                in {
                    "네",
                    "아니요",
                    "아뇨",
                }
                and staff_target is not None
            )
        )

        if use_previous_staff:

            if staff_target is not None:

                if family == "order_action":

                    if not act.get(
                        "target"
                    ):
                        act["target"] = (
                            staff_target
                        )

                    if not act.get(
                        "target_domain"
                    ):
                        act[
                            "target_domain"
                        ] = staff_domain

                    # Recommendation set:
                    # semantic order target is burger,
                    # reference keeps full proposal.
                    if (
                        isinstance(
                            act.get("target"),
                            str,
                        )
                        and act[
                            "target"
                        ].endswith(
                            "_set"
                        )
                        and staff_target
                        in BURGER_TARGETS
                    ):
                        act["target"] = (
                            staff_target
                        )

                line_ids = []

                if (
                    staff_text
                    and "변경" in staff_text
                ):
                    line_id = (
                        _unique_line_id(
                            order_state
                        )
                    )

                    if line_id is not None:
                        line_ids = [
                            line_id
                        ]

                act["reference"] = {
                    "source":
                        "previous_staff",

                    "resolved":
                        True,

                    "value":
                        staff_reference_value,

                    "line_ids":
                        line_ids,
                }

                act["resolution"] = (
                    "context_resolved"
                )

                reference = act[
                    "reference"
                ]

        # ====================================================
        # 4. PENDING ANSWER
        # ====================================================

        if (
            family == "order_action"
            and subtype == "modify"
            and isinstance(
                pending,
                dict,
            )
        ):

            target = (
                act.get("target")
                or explicit_target
            )

            line_id = pending.get(
                "line_id"
            )

            if (
                target is not None
                and isinstance(
                    line_id,
                    int,
                )
            ):

                act["target"] = target

                if not act.get(
                    "target_domain"
                ):
                    act[
                        "target_domain"
                    ] = (
                        TARGET_DOMAIN.get(
                            target
                        )
                        or explicit_domain
                    )

                act["reference"] = {
                    "source": "pending",
                    "resolved": True,
                    "value": target,
                    "line_ids": [
                        line_id
                    ],
                }

                act["resolution"] = (
                    "context_resolved"
                )

                reference = act[
                    "reference"
                ]

        # ====================================================
        # 5. CURRENT ORDER REFERENCE
        # ====================================================

        if (
            reference.get("source")
            == "current_order"
        ):

            line_ids = reference.get(
                "line_ids",
                [],
            )

            if _valid_line_ids(
                line_ids,
                order_state,
            ):

                reference["resolved"] = (
                    True
                )

                reference["value"] = (
                    act.get("target")
                    or "current_order"
                )

                act["resolution"] = (
                    "context_resolved"
                )

        # Whole-order references.
        if (
            _order_items(order_state)
            and (
                (
                    family == "info_query"
                    and subtype
                    == "order_state"
                )
                or (
                    family
                    == "order_action"
                    and subtype
                    == "finalize"
                )
            )
        ):

            act["target_domain"] = (
                "order"
            )

            act["reference"] = {
                "source":
                    "current_order",

                "resolved":
                    True,

                "value":
                    "current_order",

                "line_ids":
                    [],
            }

            act["resolution"] = (
                "context_resolved"
            )

            reference = act[
                "reference"
            ]

        # ====================================================
        # 6. EXPLICIT TARGET
        # ====================================================

        if explicit_target is not None:

            # Do not attach order target fields to no_action.
            if family not in {
                "no_action",
                "clarify",
            }:

                if not act.get(
                    "target"
                ):
                    act["target"] = (
                        explicit_target
                    )

                target = act.get(
                    "target"
                )

                if (
                    target
                    == explicit_target
                ):

                    if not act.get(
                        "target_domain"
                    ):
                        act[
                            "target_domain"
                        ] = (
                            explicit_domain
                        )

                    if (
                        act["reference"].get(
                            "source"
                        )
                        == "none"
                    ):

                        act["reference"] = {
                            "source":
                                "explicit",

                            "resolved":
                                True,

                            "value":
                                target,

                            "line_ids":
                                [],
                        }

        # ====================================================
        # 7. DOMAIN FROM KNOWN TARGET
        # ====================================================

        target = act.get(
            "target"
        )

        if (
            target
            and not act.get(
                "target_domain"
            )
        ):

            domain = TARGET_DOMAIN.get(
                target
            )

            if domain:
                act[
                    "target_domain"
                ] = domain

        # ====================================================
        # 8. TENTATIVE
        # ====================================================

        if (
            act.get("commitment")
            == "tentative"
        ):

            act["resolution"] = (
                "ambiguous"
            )

    acts = _dedupe_acts(
        acts
    )

    return RouterOutput.model_validate(
        {
            "acts": acts,
        }
    )
