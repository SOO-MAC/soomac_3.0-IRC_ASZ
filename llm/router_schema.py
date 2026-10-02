from enum import Enum

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)


# ============================================================
# ROUTER FAMILY
# ============================================================

class RouterFamily(str, Enum):
    ORDER_ACTION = "order_action"
    INFO_QUERY = "info_query"
    RECOMMENDATION = "recommendation"
    CONVERSATION_CONTROL = "conversation_control"
    STAFF_REQUEST = "staff_request"
    GENERAL_CHAT = "general_chat"
    OUT_OF_SCOPE = "out_of_scope"
    CLARIFY = "clarify"
    NO_ACTION = "no_action"


# ============================================================
# ROUTER SUBTYPE
# ============================================================

class RouterSubtype(str, Enum):

    # --------------------------------------------------------
    # ORDER_ACTION
    # --------------------------------------------------------
    ADD = "add"
    MODIFY = "modify"
    REMOVE = "remove"
    CANCEL_ALL = "cancel_all"
    FINALIZE = "finalize"
    RESET = "reset"

    # --------------------------------------------------------
    # INFO_QUERY
    # --------------------------------------------------------
    MENU = "menu"
    PRICE = "price"
    POSSIBILITY = "possibility"
    OPTION = "option"
    STOCK = "stock"
    ORDER_STATE = "order_state"
    INGREDIENT = "ingredient"
    ALLERGEN = "allergen"
    NUTRITION = "nutrition"
    PAYMENT = "payment"
    STORE = "store"
    PICKUP = "pickup"
    WAIT_TIME = "wait_time"
    MOBILE_ORDER = "mobile_order"

    # --------------------------------------------------------
    # RECOMMENDATION
    # --------------------------------------------------------
    GENERAL_RECOMMENDATION = "general_recommendation"
    PREFERENCE_RECOMMENDATION = "preference_recommendation"
    BUDGET_RECOMMENDATION = "budget_recommendation"
    COMPARISON = "comparison"

    # --------------------------------------------------------
    # CONVERSATION_CONTROL
    # --------------------------------------------------------
    REPEAT = "repeat"
    PAUSE = "pause"
    RESUME = "resume"

    # --------------------------------------------------------
    # STAFF_REQUEST
    # --------------------------------------------------------
    STAFF_CALL = "staff_call"
    STAFF_CALL_CANCEL = "staff_call_cancel"

    # --------------------------------------------------------
    # OTHER
    # --------------------------------------------------------
    CHAT = "chat"
    UNSUPPORTED = "unsupported"
    CLARIFICATION = "clarification"
    DENY = "deny"
    ACKNOWLEDGE = "acknowledge"


# ============================================================
# SPEECH ACT
# ============================================================

class SpeechAct(str, Enum):
    REQUEST = "request"
    QUESTION = "question"
    AFFIRM = "affirm"
    DENY = "deny"
    CORRECTION = "correction"
    ACKNOWLEDGEMENT = "acknowledgement"
    STATEMENT = "statement"


# ============================================================
# COMMITMENT
# ============================================================

class Commitment(str, Enum):
    EXPLICIT = "explicit"
    CONDITIONAL = "conditional"
    TENTATIVE = "tentative"
    NONE = "none"


# ============================================================
# TARGET DOMAIN
# ============================================================

class TargetDomain(str, Enum):
    BURGER = "burger"
    DRINK = "drink"
    SIDE = "side"
    TOPPING = "topping"
    INGREDIENT = "ingredient"

    MENU = "menu"
    ORDER = "order"

    PAYMENT = "payment"
    STORE = "store"
    MOBILE_ORDER = "mobile_order"

    STAFF = "staff"
    SYSTEM = "system"


# ============================================================
# REFERENCE
# ============================================================

class ReferenceSource(str, Enum):
    EXPLICIT = "explicit"
    PENDING = "pending"
    CURRENT_ORDER = "current_order"
    PREVIOUS_STAFF = "previous_staff"
    PREVIOUS_CUSTOMER = "previous_customer"
    ORDINAL = "ordinal"
    DEICTIC = "deictic"
    CONVERSATION_HISTORY = "conversation_history"
    NONE = "none"


class RouterReference(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )

    source: ReferenceSource = ReferenceSource.NONE

    resolved: bool = False

    # ex)
    # "cheese_stick"
    # "previous_recommendation"
    # "first_burger"
    value: str | None = None

    # 현재 주문 line reference가 확정된 경우만 사용
    line_ids: list[int] = Field(
        default_factory=list,
    )


# ============================================================
# CONDITION
# ============================================================

class ConditionType(str, Enum):
    POSSIBLE = "possible"
    STOCK_AVAILABLE = "stock_available"
    PRICE_WITHIN_BUDGET = "price_within_budget"
    PREVIOUS_ACT_SUCCEEDED = "previous_act_succeeded"


class RouterCondition(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )

    type: ConditionType

    expected: bool = True


# ============================================================
# RESOLUTION
# ============================================================

class Resolution(str, Enum):
    CLEAR = "clear"
    CONTEXT_RESOLVED = "context_resolved"
    AMBIGUOUS = "ambiguous"


# ============================================================
# ROUTER ACT
# ============================================================

class RouterAct(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )

    family: RouterFamily
    subtype: RouterSubtype

    speech_act: SpeechAct
    commitment: Commitment

    target_domain: TargetDomain | None = None
    target: str | None = None

    quantity: int | None = Field(
        default=None,
        ge=1,
        le=99,
    )

    reference: RouterReference = Field(
        default_factory=RouterReference,
    )

    condition: RouterCondition | None = None

    # 이 act가 앞 act의 결과에 의존하면 해당 index.
    # 예:
    # act[0] = stock query
    # act[1] = 있으면 주문
    # -> act[1].depends_on_act = 0
    depends_on_act: int | None = Field(
        default=None,
        ge=0,
    )

    resolution: Resolution = Resolution.CLEAR

    @model_validator(mode="after")
    def validate_family_subtype(self):

        allowed = {

            RouterFamily.ORDER_ACTION: {
                RouterSubtype.ADD,
                RouterSubtype.MODIFY,
                RouterSubtype.REMOVE,
                RouterSubtype.CANCEL_ALL,
                RouterSubtype.FINALIZE,
                RouterSubtype.RESET,
            },

            RouterFamily.INFO_QUERY: {
                RouterSubtype.MENU,
                RouterSubtype.PRICE,
                RouterSubtype.POSSIBILITY,
                RouterSubtype.OPTION,
                RouterSubtype.STOCK,
                RouterSubtype.ORDER_STATE,
                RouterSubtype.INGREDIENT,
                RouterSubtype.ALLERGEN,
                RouterSubtype.NUTRITION,
                RouterSubtype.PAYMENT,
                RouterSubtype.STORE,
                RouterSubtype.PICKUP,
                RouterSubtype.WAIT_TIME,
                RouterSubtype.MOBILE_ORDER,
            },

            RouterFamily.RECOMMENDATION: {
                RouterSubtype.GENERAL_RECOMMENDATION,
                RouterSubtype.PREFERENCE_RECOMMENDATION,
                RouterSubtype.BUDGET_RECOMMENDATION,
                RouterSubtype.COMPARISON,
            },

            RouterFamily.CONVERSATION_CONTROL: {
                RouterSubtype.REPEAT,
                RouterSubtype.PAUSE,
                RouterSubtype.RESUME,
            },

            RouterFamily.STAFF_REQUEST: {
                RouterSubtype.STAFF_CALL,
                RouterSubtype.STAFF_CALL_CANCEL,
            },

            RouterFamily.GENERAL_CHAT: {
                RouterSubtype.CHAT,
            },

            RouterFamily.OUT_OF_SCOPE: {
                RouterSubtype.UNSUPPORTED,
            },

            RouterFamily.CLARIFY: {
                RouterSubtype.CLARIFICATION,
            },

            RouterFamily.NO_ACTION: {
                RouterSubtype.DENY,
                RouterSubtype.ACKNOWLEDGE,
            },
        }

        if self.subtype not in allowed[self.family]:
            raise ValueError(
                f"invalid router combination: "
                f"{self.family.value}/{self.subtype.value}"
            )

        return self


# ============================================================
# ROUTER OUTPUT
# ============================================================

class RouterOutput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )

    # 한 고객 발화에서 복수 의미가 발생할 수 있다.
    acts: list[RouterAct] = Field(
        min_length=1,
        max_length=4,
    )


# ============================================================
# RUNTIME POLICY
# LLM이 mutation 허용 여부를 결정하지 않는다.
# ============================================================

MUTATING_ORDER_SUBTYPES = {
    RouterSubtype.ADD,
    RouterSubtype.MODIFY,
    RouterSubtype.REMOVE,
    RouterSubtype.CANCEL_ALL,
    RouterSubtype.FINALIZE,
    RouterSubtype.RESET,
}


def is_order_mutation(act: RouterAct) -> bool:
    return (
        act.family == RouterFamily.ORDER_ACTION
        and
        act.subtype in MUTATING_ORDER_SUBTYPES
        and
        act.commitment
        in {
            Commitment.EXPLICIT,
            Commitment.CONDITIONAL,
        }
        and
        act.resolution
        != Resolution.AMBIGUOUS
    )
