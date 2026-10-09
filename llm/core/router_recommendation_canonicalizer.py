#!/usr/bin/env python3

from copy import deepcopy

from router_schema import RouterOutput


def canonicalize_recommendation_output(
    output: RouterOutput,
) -> RouterOutput:
    """
    Router가 하나의 추천 요청을 여러 recommendation act로
    중복 출력한 경우 하나의 act로 합친다.

    자연어를 다시 해석하지 않는다.
    Router가 이미 추출한 structured semantics만 병합한다.
    """

    data = output.model_dump(
        mode="json",
        exclude_none=True,
    )

    acts = data.get("acts") or []

    if len(acts) <= 1:
        return output

    # recommendation만으로 구성된 경우에만 병합.
    # 주문 + 질문 같은 multi-act는 절대 건드리지 않는다.
    if not all(
        act.get("family") == "recommendation"
        for act in acts
    ):
        return output

    # --------------------------------------------------------
    # criteria merge
    # --------------------------------------------------------

    merged_criteria = {}

    scalar_fields = [
        "budget_max",
        "spicy_preference",
        "calorie_preference",
        "calorie_max",
    ]

    list_fields = [
        "include_ingredients",
        "exclude_ingredients",
    ]

    # 서로 충돌하는 scalar 값이 있으면 억지로 병합하지 않는다.
    for field in scalar_fields:

        values = []

        for act in acts:

            criteria = (
                act.get("criteria")
                or {}
            )

            value = criteria.get(field)

            if (
                value is not None
                and value not in values
            ):
                values.append(value)

        if len(values) > 1:
            return output

        if values:
            merged_criteria[field] = values[0]

    for field in list_fields:

        values = []

        for act in acts:

            criteria = (
                act.get("criteria")
                or {}
            )

            for value in (
                criteria.get(field)
                or []
            ):
                if value not in values:
                    values.append(value)

        if values:
            merged_criteria[field] = values

    # --------------------------------------------------------
    # subtype 결정
    # --------------------------------------------------------

    subtypes = {
        act.get("subtype")
        for act in acts
    }

    if (
        "budget_max" in merged_criteria
        or "budget_recommendation" in subtypes
    ):
        subtype = "budget_recommendation"

    elif (
        merged_criteria
        or "preference_recommendation" in subtypes
    ):
        subtype = "preference_recommendation"

    elif subtypes == {"comparison"}:
        subtype = "comparison"

    else:
        subtype = "general_recommendation"

    # --------------------------------------------------------
    # 공통 act 생성
    # --------------------------------------------------------

    result = deepcopy(
        acts[0]
    )

    result["family"] = "recommendation"
    result["subtype"] = subtype
    result["commitment"] = "none"

    # recommendation 하나 안에서 dependency/condition 불필요
    result.pop(
        "condition",
        None,
    )

    result.pop(
        "depends_on_act",
        None,
    )

    if merged_criteria:
        result["criteria"] = merged_criteria
    else:
        result.pop(
            "criteria",
            None,
        )

    # target이 여러 개로 충돌하면 특정 target을 만들지 않는다.
    targets = {
        act.get("target")
        for act in acts
        if act.get("target") is not None
    }

    if len(targets) > 1:
        result.pop(
            "target",
            None,
        )

    domains = {
        act.get("target_domain")
        for act in acts
        if act.get("target_domain") is not None
    }

    if len(domains) > 1:
        result.pop(
            "target_domain",
            None,
        )

    return RouterOutput.model_validate(
        {
            "acts": [
                result
            ]
        }
    )
