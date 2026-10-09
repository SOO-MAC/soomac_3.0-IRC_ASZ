"""Confirmed order summaries and a disk outbox shared with the ROS process.

This module deliberately depends only on the standard library: the ordering
application and the ROS publisher can use different Python environments.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


BURGER_NAMES = {
    "bulgogi_burger": "불고기버거", "chicken_burger": "치킨버거",
    "cheese_burger": "치즈버거", "shrimp_burger": "새우버거",
}
DRINK_NAMES = {
    "coke": "콜라", "zero_coke": "제로콜라", "sprite": "스프라이트",
    "fanta": "환타", "iced_coffee": "아이스커피",
}
SIZE_NAMES = {"small": "스몰", "medium": "미디엄", "large": "라지"}
SIDE_NAMES = {"french_fries": "감자튀김", "cheese_stick": "치즈스틱"}
TYPE_NAMES = {"single": "단품", "set": "세트"}


def _positive_int(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def build_main_order(handoff):
    """Make one String-message JSON object; preserve the charged total exactly."""
    order_no = _positive_int(handoff["order_id"], "order_id")
    if handoff["command"] == "mobile_pickup":
        number = _positive_int(handoff["mobile_order_id"], "mobile_order_id")
        if number > 999:
            raise ValueError("mobile_order_id must be <= 999")
        return {"order_no": order_no, "menu": f"맥오더 {number}번",
                "is_mcorder": True, "price": 0}
    if handoff["command"] != "counter_order":
        raise ValueError("Only completed counter/mobile handoffs can be published")

    groups = {}
    for item in handoff["menu"]:
        quantity = _positive_int(item["quantity"], "quantity")
        kind = item["item_type"]
        if kind == "burger":
            label = f"{BURGER_NAMES[item['menu']]} {TYPE_NAMES[item['type']]}"
            unit = "개"
        elif kind == "drink":
            label = f"{DRINK_NAMES[item['drink']]} {SIZE_NAMES[item['drink_size']]}"
            unit = "잔"
        elif kind == "side":
            label, unit = SIDE_NAMES[item["side"]], "개"
        else:
            raise ValueError(f"Unsupported item_type: {kind}")
        key = label, unit
        groups[key] = groups.get(key, 0) + quantity
    if not groups:
        raise ValueError("Cannot publish an empty order")
    price = handoff["total_price"]
    if type(price) is not int or price < 0:
        raise ValueError("total_price must be a nonnegative integer")
    return {
        "order_no": order_no,
        "menu": ", ".join(f"{label} {quantity}{unit}"
                          for (label, unit), quantity in groups.items()),
        "is_mcorder": False,
        "price": price,
    }


def validate_main_order(payload):
    _positive_int(payload["order_no"], "order_no")
    if not isinstance(payload["menu"], str) or not payload["menu"].strip():
        raise ValueError("menu must be a nonempty string")
    if type(payload["is_mcorder"]) is not bool:
        raise ValueError("is_mcorder must be boolean")
    if type(payload["price"]) is not int or payload["price"] < 0:
        raise ValueError("price must be a nonnegative integer")
    if set(payload) != {"order_no", "menu", "is_mcorder", "price"}:
        raise ValueError("Unexpected order fields")
    return payload


class OrderOutbox:
    """Only new finalizations enter pending; historical handoffs are not replayed."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.pending = self.directory / "pending"
        self.sent = self.directory / "sent"
        self.pending.mkdir(parents=True, exist_ok=True)
        self.sent.mkdir(parents=True, exist_ok=True)

    def existing_ids(self):
        ids = []
        for directory in (self.pending, self.sent):
            for path in directory.glob("order_*.json"):
                try:
                    ids.append(int(path.stem.removeprefix("order_")))
                except ValueError:
                    continue
        return ids

    def enqueue(self, payload):
        validate_main_order(payload)
        name = f"order_{payload['order_no']}.json"
        for directory in (self.pending, self.sent):
            existing = directory / name
            if existing.exists():
                if json.loads(existing.read_text(encoding="utf-8")) != payload:
                    raise ValueError(f"Conflicting order number: {payload['order_no']}")
                return
        fd, temporary = tempfile.mkstemp(prefix=".order-", suffix=".tmp", dir=self.pending)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.pending / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def next_pending(self):
        files = list(self.pending.glob("order_*.json"))
        if not files:
            return None
        # Invalid filenames stop the queue instead of silently losing an order.
        return min(files, key=lambda p: int(p.stem.removeprefix("order_")))

    def mark_sent(self, path):
        path = Path(path)
        if path.parent != self.pending:
            raise ValueError("Order is outside this outbox")
        path.replace(self.sent / path.name)
