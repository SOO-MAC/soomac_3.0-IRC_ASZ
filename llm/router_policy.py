from enum import Enum

from pydantic import BaseModel, ConfigDict

from router_schema import (
    Commitment,
    ReferenceSource,
    Resolution,
    RouterFamily,
    RouterOutput,
    RouterSubtype,
)


# ============================================================
# POLICY RESULT
# ============================================================

class PolicyStatus(str, Enum):
    EXECUTE_ORDER = "execute_order"
    READ_ONLY = "read_only"
    WAIT_CONDITION = "wait_condition"
    EVENT = "event"
    NO_ACTION = "no_action"
    CLARIFY = "clarify"


class PolicyDecision(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )

    act_index: int
    status: PolicyStatus

    # 주문 state를 실제로 변경해도 되는가?
    mutation_allowed: bool = False

    reason: str


# ============================================================
# HELPERS
# ============================================================

TARGET_REQUIRED_SUBTYPES = {
    RouterSubtype.ADD,
    RouterSubtype.MODIFY,
    RouterSubtype.REMOVE,
}


CONTEXT_REFERENCE_SOURCES = {
    ReferenceSource.PENDING,
    ReferenceSource.CURRENT_ORDER,
    ReferenceSource.PREVIOUS_STAFF,
    ReferenceSource.PREVIOUS_CUSTOMER,
    ReferenceSource.ORDINAL,
    ReferenceSource.DEICTIC,
    ReferenceSource.CONVERSATION_HISTORY,
}


def has_resolved_target(act) -> bool:
    """
    주문 실행에 필요한 대상이 충분히 확정되어 있는지 확인한다.

    다음 중 하나면 대상이 있다고 본다.
    1. target 필드가 명시됨
    2. reference.value가 resolve됨
    3. 현재 주문 line_ids가 resolve됨
    """

    if act.target:
        return True

    ref = act.reference

    if (
        ref.resolved
        and
        ref.value
    ):
        return True

    if (
        ref.resolved
        and
        ref.line_ids
    ):
        return True

    return False


def reference_is_safe(act) -> bool:
    """
    문맥 참조가 필요한데 resolve되지 않았다면 실행 금지.
    """

    ref = act.reference

    if ref.source == ReferenceSource.NONE:
        return True

    if ref.source == ReferenceSource.EXPLICIT:
        return (
            ref.resolved
            or
            act.target is not None
        )

    if ref.source in CONTEXT_REFERENCE_SOURCES:
        return ref.resolved

    return False


# ============================================================
# SINGLE ACT POLICY
# ============================================================

def evaluate_act(
    act,
    *,
    index: int,
    total_acts: int,
):
    """
    RouterAct 하나에 대한 실행 정책을 결정한다.

    주의:
    Router LLM의 결과를 그대로 신뢰하지 않는다.
    실제 mutation 허용 여부는 여기서 결정한다.
    """

    # --------------------------------------------------------
    # 1. ambiguous는 무조건 실행 금지
    # --------------------------------------------------------

    if act.resolution == Resolution.AMBIGUOUS:

        return PolicyDecision(
            act_index=index,
            status=PolicyStatus.CLARIFY,
            mutation_allowed=False,
            reason="의미 또는 참조 대상이 모호함",
        )


    # --------------------------------------------------------
    # 2. dependency index 검증
    # --------------------------------------------------------

    if act.depends_on_act is not None:

        dependency = act.depends_on_act

        if dependency >= total_acts:

            return PolicyDecision(
                act_index=index,
                status=PolicyStatus.CLARIFY,
                mutation_allowed=False,
                reason="존재하지 않는 선행 act를 참조함",
            )

        # 뒤쪽 act나 자기 자신에게 의존하면 안 됨.
        if dependency >= index:

            return PolicyDecision(
                act_index=index,
                status=PolicyStatus.CLARIFY,
                mutation_allowed=False,
                reason="선행 act dependency 순서가 잘못됨",
            )


    # --------------------------------------------------------
    # 3. 명시적 CLARIFY
    # --------------------------------------------------------

    if act.family == RouterFamily.CLARIFY:

        return PolicyDecision(
            act_index=index,
            status=PolicyStatus.CLARIFY,
            mutation_allowed=False,
            reason="Router가 clarification 필요로 분류함",
        )


    # --------------------------------------------------------
    # 4. NO_ACTION
    # --------------------------------------------------------

    if act.family == RouterFamily.NO_ACTION:

        return PolicyDecision(
            act_index=index,
            status=PolicyStatus.NO_ACTION,
            mutation_allowed=False,
            reason="고객이 실행을 거부하거나 단순 acknowledgement함",
        )


    # --------------------------------------------------------
    # 5. 직원 호출 등 event
    # --------------------------------------------------------

    if act.family == RouterFamily.STAFF_REQUEST:

        return PolicyDecision(
            act_index=index,
            status=PolicyStatus.EVENT,
            mutation_allowed=False,
            reason="주문 State와 분리된 직원 호출 event",
        )


    # --------------------------------------------------------
    # 6. ORDER_ACTION이 아닌 모든 route는 READ ONLY
    # --------------------------------------------------------

    if act.family != RouterFamily.ORDER_ACTION:

        return PolicyDecision(
            act_index=index,
            status=PolicyStatus.READ_ONLY,
            mutation_allowed=False,
            reason="정보 조회/추천/대화 route는 주문 State를 변경하지 않음",
        )


    # ========================================================
    # 여기부터 ORDER_ACTION 전용 정책
    # ========================================================


    # --------------------------------------------------------
    # 7. tentative / none commitment는 실행 금지
    # --------------------------------------------------------

    if act.commitment == Commitment.TENTATIVE:

        return PolicyDecision(
            act_index=index,
            status=PolicyStatus.CLARIFY,
            mutation_allowed=False,
            reason="고객의 주문 의사가 tentative 상태임",
        )


    if act.commitment == Commitment.NONE:

        return PolicyDecision(
            act_index=index,
            status=PolicyStatus.CLARIFY,
            mutation_allowed=False,
            reason="명확한 주문 commitment가 없음",
        )


    # --------------------------------------------------------
    # 8. reference 안전성 검사
    # --------------------------------------------------------

    if not reference_is_safe(act):

        return PolicyDecision(
            act_index=index,
            status=PolicyStatus.CLARIFY,
            mutation_allowed=False,
            reason="주문 대상 reference가 resolve되지 않음",
        )


    # --------------------------------------------------------
    # 9. ADD / MODIFY / REMOVE는 target 필요
    # --------------------------------------------------------

    if (
        act.subtype in TARGET_REQUIRED_SUBTYPES
        and
        not has_resolved_target(act)
    ):

        return PolicyDecision(
            act_index=index,
            status=PolicyStatus.CLARIFY,
            mutation_allowed=False,
            reason="주문 변경 대상이 충분히 특정되지 않음",
        )


    # --------------------------------------------------------
    # 10. conditional action
    # --------------------------------------------------------

    if act.commitment == Commitment.CONDITIONAL:

        if act.condition is None:

            return PolicyDecision(
                act_index=index,
                status=PolicyStatus.CLARIFY,
                mutation_allowed=False,
                reason="conditional 주문인데 condition 정보가 없음",
            )

        if act.depends_on_act is None:

            return PolicyDecision(
                act_index=index,
                status=PolicyStatus.CLARIFY,
                mutation_allowed=False,
                reason="conditional 주문인데 선행 act dependency가 없음",
            )

        return PolicyDecision(
            act_index=index,
            status=PolicyStatus.WAIT_CONDITION,
            mutation_allowed=False,
            reason="선행 조건 결과 확인 후 주문 실행 가능",
        )


    # --------------------------------------------------------
    # 11. explicit action
    # --------------------------------------------------------

    if act.commitment == Commitment.EXPLICIT:

        return PolicyDecision(
            act_index=index,
            status=PolicyStatus.EXECUTE_ORDER,
            mutation_allowed=True,
            reason="명확하고 resolve된 주문 실행 요청",
        )


    # 방어적 fallback
    return PolicyDecision(
        act_index=index,
        status=PolicyStatus.CLARIFY,
        mutation_allowed=False,
        reason="정책에서 처리되지 않은 주문 상태",
    )


# ============================================================
# WHOLE ROUTER OUTPUT
# ============================================================

def evaluate_router_output(
    output: RouterOutput,
):
    """
    RouterOutput 전체를 평가한다.
    """

    decisions = []

    total = len(
        output.acts
    )

    for index, act in enumerate(
        output.acts
    ):

        decisions.append(
            evaluate_act(
                act,
                index=index,
                total_acts=total,
            )
        )

    return decisions
