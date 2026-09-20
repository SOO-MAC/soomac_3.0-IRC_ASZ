from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from order_schema import (
    Burger,
    Drink,
    DrinkSize,
    Exclude,
    Intent,
    ItemType,
    OrderType,
    Side,
    Topping,
)


# =========================
# OPERATION
# =========================

class Operation(str, Enum):
    ADD = "add"
    MODIFY = "modify"
    REMOVE = "remove"
    ADJUST_QUANTITY = "adjust_quantity"
    RESET = "reset"


# =========================
# TARGET
# =========================

class ItemSelector(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )

    line_id: int | None = Field(
        default=None,
        ge=1,
    )

    item_type: ItemType | None = None

    menu: Burger | None = None
    drink: Drink | None = None
    side: Side | None = None


# =========================
# ITEM PATCH
# =========================

class ItemPatch(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )

    item_type: ItemType | None = None

    quantity: int | None = Field(
        default=None,
        ge=1,
    )

    menu: Burger | None = None
    type: OrderType | None = None

    drink: Drink | None = None
    drink_size: DrinkSize | None = None

    side: Side | None = None


# =========================
# ACTION
# =========================

class OrderAction(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )

    operation: Operation

    target: ItemSelector | None = None
    item: ItemPatch | None = None

    # 기존 상품 수량 +/-에만 사용
    quantity_delta: int | None = None

    apply_to_all: bool = False

    exclude_add: list[Exclude] = Field(
        default_factory=list,
    )

    exclude_remove: list[Exclude] = Field(
        default_factory=list,
    )

    toppings_add: list[Topping] = Field(
        default_factory=list,
    )

    toppings_remove: list[Topping] = Field(
        default_factory=list,
    )


# =========================
# LLM OUTPUT
# =========================

class OrderUpdate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )

    intent: Intent

    order_id: int | None = Field(
        default=None,
        ge=1,
    )

    actions: list[OrderAction] = Field(
        default_factory=list,
    )