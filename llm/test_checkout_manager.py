#!/usr/bin/env python3

import shutil
import tempfile
from pathlib import Path

from checkout_manager import (
    OrderHandoffManager,
)


def main():

    temp_dir = Path(
        tempfile.mkdtemp(
            prefix="handoff_test_"
        )
    )

    try:

        manager = (
            OrderHandoffManager(
                storage_dir=temp_dir,
                first_order_id=1,
            )
        )

        # ====================================================
        # COUNTER #1
        # ====================================================

        state1 = {
            "intent": "confirm",
            "order_id": None,
            "items": [
                {
                    "line_id": 1,
                    "item_type": "burger",
                    "quantity": 1,
                    "menu": "bulgogi_burger",
                    "type": "set",
                    "drink": "coke",
                    "drink_size": "large",
                    "side": "french_fries",
                    "exclude": [],
                    "add_toppings": [],
                }
            ],
        }

        order1 = (
            manager
            .create_counter_handoff(
                state1
            )
        )

        assert (
            order1["command"]
            == "counter_order"
        )

        assert (
            order1["order_id"]
            == 1
        )

        assert (
            order1["total_price"]
            == 7700
        )

        assert (
            "menu"
            in order1
        )

        # ====================================================
        # COUNTER #2
        # ====================================================

        state2 = {
            "intent": "confirm",
            "order_id": None,
            "items": [
                {
                    "line_id": 1,
                    "item_type": "burger",
                    "quantity": 1,
                    "menu": "chicken_burger",
                    "type": "single",
                    "drink": None,
                    "drink_size": None,
                    "side": None,
                    "exclude": [],
                    "add_toppings": [],
                }
            ],
        }

        order2 = (
            manager
            .create_counter_handoff(
                state2
            )
        )

        assert (
            order2["order_id"]
            == 2
        )

        # ====================================================
        # COUNTER #3
        # ====================================================

        order3 = (
            manager
            .create_counter_handoff(
                state1
            )
        )

        assert (
            order3["order_id"]
            == 3
        )

        # ====================================================
        # MOBILE #4
        # ====================================================

        order4 = (
            manager
            .create_mobile_handoff(
                65
            )
        )

        assert (
            order4
            == {
                "command":
                    "mobile_pickup",

                "order_id":
                    4,

                "mobile_order_id":
                    65,
            }
        )

        assert (
            "menu"
            not in order4
        )

        assert (
            "total_price"
            not in order4
        )

        # ====================================================
        # COUNTER #5
        # ====================================================

        order5 = (
            manager
            .create_counter_handoff(
                state1
            )
        )

        assert (
            order5["order_id"]
            == 5
        )

        print(
            "✅ 일반 주문 가격 계산 PASS"
        )

        print(
            "✅ 일반 주문 menu 포함 PASS"
        )

        print(
            "✅ 맥오더 menu/price 제외 PASS"
        )

        print(
            "✅ 맥오더 번호 분리 PASS"
        )

        print(
            "✅ 공통 주문번호 "
            "1 → 2 → 3 → 4 → 5 PASS"
        )

        print()
        print(
            "최종 테스트 PASS"
        )

    finally:

        shutil.rmtree(
            temp_dir,
            ignore_errors=True,
        )


if __name__ == "__main__":
    main()
