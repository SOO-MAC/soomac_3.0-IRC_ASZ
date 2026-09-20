#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any


# ============================================================
# PRICE TABLE
# 실제 대회 메뉴 가격에 맞춰 나중에 수정
# ============================================================

BURGER_BASE_PRICE = {
    "bulgogi_burger": 4500,
    "chicken_burger": 4800,
    "cheese_burger": 5000,
    "shrimp_burger": 5200,
}

SET_UPCHARGE = 2500

STANDALONE_DRINK_PRICE = {
    "coke": 2000,
    "zero_coke": 2000,
    "sprite": 2000,
    "fanta": 2000,
    "iced_coffee": 2500,
}

DRINK_SIZE_UPCHARGE = {
    "small": 0,
    "medium": 300,
    "large": 700,
}

SET_DRINK_UPCHARGE = {
    "coke": 0,
    "zero_coke": 0,
    "sprite": 0,
    "fanta": 0,
    "iced_coffee": 500,
}

SET_SIDE_UPCHARGE = {
    "french_fries": 0,
    "cheese_stick": 500,
}

STANDALONE_SIDE_PRICE = {
    "french_fries": 2000,
    "cheese_stick": 2500,
}

TOPPING_PRICE = {
    "cheese": 500,
    "bacon": 800,
    "tomato": 400,
}


# ============================================================
# ERROR
# ============================================================

class OrderHandoffError(RuntimeError):
    pass


# ============================================================
# MANAGER
# ============================================================

class OrderHandoffManager:

    def __init__(
        self,
        storage_dir: str | Path = "runtime_data/handoffs",
        first_order_id: int = 1,
    ):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.first_order_id = first_order_id

        self.lock = threading.Lock()

        self.last_handoff: dict[str, Any] | None = None

    # ========================================================
    # VALIDATION
    # ========================================================

    def validate_confirmed_state(
        self,
        state: dict[str, Any],
    ) -> None:

        if state.get("intent") != "confirm":
            raise OrderHandoffError(
                "일반 주문은 intent=confirm 상태에서만 확정 가능합니다."
            )

        items = state.get("items", [])

        if not items:
            raise OrderHandoffError(
                "주문 항목이 없습니다."
            )

        for item in items:

            line_id = item.get("line_id")
            quantity = item.get("quantity")

            if (
                not isinstance(quantity, int)
                or quantity <= 0
            ):
                raise OrderHandoffError(
                    f"line_id={line_id}: "
                    "quantity가 잘못되었습니다."
                )

            item_type = item.get(
                "item_type"
            )

            if item_type == "burger":
                self._validate_burger(item)

            elif item_type == "drink":
                self._validate_drink(item)

            elif item_type == "side":
                self._validate_side(item)

            else:
                raise OrderHandoffError(
                    f"line_id={line_id}: "
                    f"지원하지 않는 item_type={item_type!r}"
                )

    def _validate_burger(
        self,
        item: dict[str, Any],
    ) -> None:

        line_id = item.get("line_id")

        menu = item.get("menu")
        order_type = item.get("type")

        if menu not in BURGER_BASE_PRICE:
            raise OrderHandoffError(
                f"line_id={line_id}: "
                "burger menu가 없습니다/잘못되었습니다."
            )

        if order_type not in {
            "single",
            "set",
        }:
            raise OrderHandoffError(
                f"line_id={line_id}: "
                "burger type이 없습니다/잘못되었습니다."
            )

        if order_type == "set":

            if (
                item.get("drink")
                not in SET_DRINK_UPCHARGE
            ):
                raise OrderHandoffError(
                    f"line_id={line_id}: "
                    "세트 음료가 완성되지 않았습니다."
                )

            if (
                item.get("drink_size")
                not in DRINK_SIZE_UPCHARGE
            ):
                raise OrderHandoffError(
                    f"line_id={line_id}: "
                    "세트 음료 크기가 완성되지 않았습니다."
                )

            if (
                item.get("side")
                not in SET_SIDE_UPCHARGE
            ):
                raise OrderHandoffError(
                    f"line_id={line_id}: "
                    "세트 사이드가 완성되지 않았습니다."
                )

        for topping in (
            item.get("add_toppings", [])
            or []
        ):

            if topping not in TOPPING_PRICE:
                raise OrderHandoffError(
                    f"line_id={line_id}: "
                    f"알 수 없는 토핑={topping!r}"
                )

    def _validate_drink(
        self,
        item: dict[str, Any],
    ) -> None:

        line_id = item.get("line_id")

        if (
            item.get("drink")
            not in STANDALONE_DRINK_PRICE
        ):
            raise OrderHandoffError(
                f"line_id={line_id}: "
                "음료 종류가 없습니다/잘못되었습니다."
            )

        if (
            item.get("drink_size")
            not in DRINK_SIZE_UPCHARGE
        ):
            raise OrderHandoffError(
                f"line_id={line_id}: "
                "음료 크기가 없습니다/잘못되었습니다."
            )

    def _validate_side(
        self,
        item: dict[str, Any],
    ) -> None:

        line_id = item.get("line_id")

        if (
            item.get("side")
            not in STANDALONE_SIDE_PRICE
        ):
            raise OrderHandoffError(
                f"line_id={line_id}: "
                "사이드 종류가 없습니다/잘못되었습니다."
            )

    # ========================================================
    # PRICE
    # ========================================================

    def unit_price(
        self,
        item: dict[str, Any],
    ) -> int:

        item_type = item["item_type"]

        if item_type == "burger":

            price = BURGER_BASE_PRICE[
                item["menu"]
            ]

            for topping in (
                item.get("add_toppings", [])
                or []
            ):
                price += TOPPING_PRICE[
                    topping
                ]

            if item["type"] == "set":

                price += SET_UPCHARGE

                price += SET_DRINK_UPCHARGE[
                    item["drink"]
                ]

                price += DRINK_SIZE_UPCHARGE[
                    item["drink_size"]
                ]

                price += SET_SIDE_UPCHARGE[
                    item["side"]
                ]

            return price

        if item_type == "drink":

            return (
                STANDALONE_DRINK_PRICE[
                    item["drink"]
                ]
                + DRINK_SIZE_UPCHARGE[
                    item["drink_size"]
                ]
            )

        if item_type == "side":

            return STANDALONE_SIDE_PRICE[
                item["side"]
            ]

        raise OrderHandoffError(
            f"가격 계산 불가 "
            f"item_type={item_type!r}"
        )

    def price_order(
        self,
        state: dict[str, Any],
    ) -> int:

        self.validate_confirmed_state(
            state
        )

        total = 0

        for item in state["items"]:

            unit_price = self.unit_price(
                item
            )

            quantity = item[
                "quantity"
            ]

            total += (
                unit_price
                * quantity
            )

        return total

    # ========================================================
    # MENU PAYLOAD
    # ========================================================

    def build_menu_payload(
        self,
        state: dict[str, Any],
    ) -> list[dict[str, Any]]:

        result = []

        for item in state["items"]:

            menu_item = {
                "item_type":
                    item.get("item_type"),

                "quantity":
                    item.get("quantity", 1),

                "menu":
                    item.get("menu"),

                "type":
                    item.get("type"),

                "drink":
                    item.get("drink"),

                "drink_size":
                    item.get("drink_size"),

                "side":
                    item.get("side"),

                "exclude":
                    copy.deepcopy(
                        item.get(
                            "exclude",
                            [],
                        )
                    ),

                "add_toppings":
                    copy.deepcopy(
                        item.get(
                            "add_toppings",
                            [],
                        )
                    ),
            }

            # None 제거
            menu_item = {
                key: value
                for key, value
                in menu_item.items()
                if value is not None
            }

            result.append(
                menu_item
            )

        return result

    # ========================================================
    # ORDER NUMBER
    # ========================================================

    def _existing_order_ids(
        self,
    ) -> list[int]:

        ids = []

        for path in self.storage_dir.glob(
            "handoff_*.json"
        ):

            try:
                number = int(
                    path.stem.removeprefix(
                        "handoff_"
                    )
                )

                ids.append(number)

            except ValueError:
                pass

        return ids

    def next_order_id(
        self,
    ) -> int:

        existing = (
            self._existing_order_ids()
        )

        if not existing:
            return self.first_order_id

        return max(existing) + 1

    # ========================================================
    # STORAGE
    # ========================================================

    def _handoff_path(
        self,
        order_id: int,
    ) -> Path:

        return (
            self.storage_dir
            / f"handoff_{order_id}.json"
        )

    def save_handoff(
        self,
        handoff: dict[str, Any],
    ) -> Path:

        path = self._handoff_path(
            handoff["order_id"]
        )

        temp_path = path.with_suffix(
            ".json.tmp"
        )

        temp_path.write_text(
            json.dumps(
                handoff,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        temp_path.replace(path)

        return path

    # ========================================================
    # COUNTER ORDER
    # ========================================================

    def create_counter_handoff(
        self,
        confirmed_state: dict[str, Any],
    ) -> dict[str, Any]:

        total_price = (
            self.price_order(
                confirmed_state
            )
        )

        menu = (
            self.build_menu_payload(
                confirmed_state
            )
        )

        with self.lock:

            order_id = (
                self.next_order_id()
            )

            handoff = {
                "command":
                    "counter_order",

                "order_id":
                    order_id,

                "menu":
                    menu,

                "total_price":
                    total_price,
            }

            self.save_handoff(
                handoff
            )

            self.last_handoff = (
                copy.deepcopy(
                    handoff
                )
            )

        return copy.deepcopy(
            handoff
        )

    # ========================================================
    # MOBILE PICKUP
    # ========================================================

    def create_mobile_handoff(
        self,
        mobile_order_id: int,
    ) -> dict[str, Any]:

        if (
            not isinstance(
                mobile_order_id,
                int,
            )
            or not (
                1
                <= mobile_order_id
                <= 999
            )
        ):
            raise OrderHandoffError(
                "맥오더 번호가 잘못되었습니다."
            )

        with self.lock:

            order_id = (
                self.next_order_id()
            )

            handoff = {
                "command":
                    "mobile_pickup",

                "order_id":
                    order_id,

                "mobile_order_id":
                    mobile_order_id,
            }

            self.save_handoff(
                handoff
            )

            self.last_handoff = (
                copy.deepcopy(
                    handoff
                )
            )

        return copy.deepcopy(
            handoff
        )


# 이전 코드와 호환용
CheckoutManager = OrderHandoffManager
CheckoutError = OrderHandoffError
