from copy import deepcopy


def _none_ref():
    return {
        "source": "none",
        "resolved": False,
        "line_ids": [],
    }


CANONICAL_EMPTY_CONTEXT = {
    "메뉴 뭐 있어요?": {
        "acts": [
            {
                "family": "info_query",
                "subtype": "menu",
                "speech_act": "question",
                "commitment": "none",
                "target_domain": "menu",
                "reference": _none_ref(),
                "resolution": "clear",
            }
        ],
        "policy": ["read_only"],
    },

    "음료 뭐 있어요?": {
        "acts": [
            {
                "family": "info_query",
                "subtype": "menu",
                "speech_act": "question",
                "commitment": "none",
                "target_domain": "drink",
                "reference": _none_ref(),
                "resolution": "clear",
            }
        ],
        "policy": ["read_only"],
    },

    "만원 안으로 추천해주세요": {
        "acts": [
            {
                "family": "recommendation",
                "subtype": "budget_recommendation",
                "speech_act": "request",
                "commitment": "none",
                "reference": _none_ref(),
                "resolution": "clear",
            }
        ],
        "policy": ["read_only"],
    },

    "얼마나 걸려요?": {
        "acts": [
            {
                "family": "info_query",
                "subtype": "wait_time",
                "speech_act": "question",
                "commitment": "none",
                "target_domain": "order",
                "reference": _none_ref(),
                "resolution": "clear",
            }
        ],
        "policy": ["read_only"],
    },

    "알레르기 정보 있어요?": {
        "acts": [
            {
                "family": "info_query",
                "subtype": "allergen",
                "speech_act": "question",
                "commitment": "none",
                "target_domain": "menu",
                "reference": _none_ref(),
                "resolution": "clear",
            }
        ],
        "policy": ["read_only"],
    },

    "칼로리 얼마예요?": {
        "acts": [
            {
                "family": "info_query",
                "subtype": "nutrition",
                "speech_act": "question",
                "commitment": "none",
                "target_domain": "menu",
                "reference": _none_ref(),
                "resolution": "clear",
            }
        ],
        "policy": ["read_only"],
    },

    "직원 불러주세요": {
        "acts": [
            {
                "family": "staff_request",
                "subtype": "staff_call",
                "speech_act": "request",
                "commitment": "explicit",
                "target_domain": "staff",
                "target": "staff",
                "reference": {
                    "source": "explicit",
                    "resolved": True,
                    "value": "staff",
                    "line_ids": [],
                },
                "resolution": "clear",
            }
        ],
        "policy": ["event"],
    },
}


def is_empty_context(case):
    context = case.get("context") or {}

    history = context.get("history") or []
    pending = context.get("pending")

    order_state = (
        context.get("order_state")
        or {}
    )

    items = order_state.get("items") or []

    return (
        not history
        and pending is None
        and not items
    )


def canonicalize_case(case):
    """
    같은 utterance + 같은 empty context는
    모든 Gold generator에서 완전히 동일한
    expected / expected_policy를 사용한다.
    """

    case = deepcopy(case)

    utterance = case.get("utterance")

    if (
        utterance not in CANONICAL_EMPTY_CONTEXT
        or not is_empty_context(case)
    ):
        return case

    canonical = CANONICAL_EMPTY_CONTEXT[
        utterance
    ]

    case["expected"] = {
        "acts": deepcopy(
            canonical["acts"]
        )
    }

    case["expected_policy"] = deepcopy(
        canonical["policy"]
    )

    case["policy_reason"] = (
        "canonical_router_policy_v1"
    )

    return case
