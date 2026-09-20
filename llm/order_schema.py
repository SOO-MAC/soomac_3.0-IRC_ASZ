from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


# =========================
# DOMAIN
# =========================

class Intent(str, Enum):
    ORDER = "order"
    MOBILE_PICKUP = "mobile_pickup"
    CONFIRM = "confirm"
    CANCEL = "cancel"
    UNKNOWN = "unknown"


class ItemType(str, Enum):
    BURGER = "burger"
    DRINK = "drink"
    SIDE = "side"


class Burger(str, Enum):
    BULGOGI = "bulgogi_burger"
    CHICKEN = "chicken_burger"
    CHEESE = "cheese_burger"
    SHRIMP = "shrimp_burger"


class OrderType(str, Enum):
    SINGLE = "single"
    SET = "set"


class Drink(str, Enum):
    COKE = "coke"
    ZERO_COKE = "zero_coke"
    SPRITE = "sprite"
    FANTA = "fanta"
    ICED_COFFEE = "iced_coffee"


class DrinkSize(str, Enum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class Side(str, Enum):
    FRENCH_FRIES = "french_fries"
    CHEESE_STICK = "cheese_stick"


class Exclude(str, Enum):
    ONION = "onion"
    PICKLE = "pickle"
    TOMATO = "tomato"
    CHEESE = "cheese"
    LETTUCE = "lettuce"


class Topping(str, Enum):
    CHEESE = "cheese"
    BACON = "bacon"
    TOMATO = "tomato"


# =========================
# FINAL ORDER STATE
# =========================

class OrderItem(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )

    line_id: int = Field(
        ge=1,
    )

    item_type: ItemType

    quantity: int = Field(
        default=1,
        ge=1,
    )

    # BURGER
    menu: Burger | None = None
    type: OrderType | None = None

    # DRINK
    drink: Drink | None = None
    drink_size: DrinkSize | None = None

    # SIDE
    side: Side | None = None

    # BURGER OPTIONS
    exclude: list[Exclude] = Field(
        default_factory=list,
    )

    add_toppings: list[Topping] = Field(
        default_factory=list,
    )


class OrderCommand(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )

    intent: Intent

    order_id: int | None = Field(
        default=None,
        ge=1,
    )

    items: list[OrderItem] = Field(
        default_factory=list,
    )