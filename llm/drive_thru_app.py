#!/usr/bin/env python3

import json
import re
import unicodedata
import traceback

from enum import Enum
from pathlib import Path

from checkout_manager import (
    OrderHandoffError,
    OrderHandoffManager,
    BURGER_BASE_PRICE,
    SET_UPCHARGE,
    STANDALONE_DRINK_PRICE,
    DRINK_SIZE_UPCHARGE,
    SET_DRINK_UPCHARGE,
    SET_SIDE_UPCHARGE,
    STANDALONE_SIDE_PRICE,
    TOPPING_PRICE,
)

from order_runtime_final import (
    DriveThruRuntime,
)

from runtime_worker import (
    RuntimeWorker,
    StaleRuntimeRequest,
)


# ============================================================
# CONFIG
# ============================================================

WIDTH = 68


# ============================================================
# APP STATE
# ============================================================

class AppState(str, Enum):
    IDLE = "IDLE"
    ORDERING = "ORDERING"
    WAITING_FOR_EXIT = "WAITING_FOR_EXIT"


# ============================================================
# LABELS
# ============================================================

MENU_LABELS = {
    "bulgogi_burger": "불고기버거",
    "chicken_burger": "치킨버거",
    "cheese_burger": "치즈버거",
    "shrimp_burger": "새우버거",
}

TYPE_LABELS = {
    "single": "단품",
    "set": "세트",
}

DRINK_LABELS = {
    "coke": "콜라",
    "zero_coke": "제로콜라",
    "sprite": "스프라이트",
    "fanta": "환타",
    "iced_coffee": "아이스커피",
}

SIZE_LABELS = {
    "small": "스몰",
    "medium": "미디엄",
    "large": "라지",
}

SIDE_LABELS = {
    "french_fries": "감자튀김",
    "cheese_stick": "치즈스틱",
}

EXCLUDE_LABELS = {
    "onion": "양파",
    "pickle": "피클",
    "tomato": "토마토",
    "cheese": "치즈",
    "lettuce": "양상추",
}

TOPPING_LABELS = {
    "cheese": "치즈",
    "bacon": "베이컨",
    "tomato": "토마토",
}




# ============================================================
# UI
# ============================================================

def line(char="="):
    print(char * WIDTH)


def header():
    print()
    line("=")
    print("SOOMAC DRIVE-THRU V14".center(WIDTH))
    line("=")

    print("주문 시스템 : READY")
    print("차량 감지   : 수동 시뮬레이션")
    print()
    print("차량 명령   : /carin /carout")
    print("개발 명령   : /debug /reset /resetall /state /order /quit")

    line("-")


def system_message(text):
    print()
    print(f"[SYSTEM] {text}")


def soomac_say(text):
    """
    추후 TTS 연결 위치.
    """

    print()
    print(f"SOOMAC > {text}")


def show_idle():
    print()
    print("● 차량 진입 대기 중")


def show_waiting_for_exit():
    print()
    print("● 현재 차량 이동 대기 중")


# ============================================================
# INPUT
# ============================================================

def get_customer_input():
    """
    추후 STT 연결 위치.
    """

    return input("\n고객   > ").strip()


def get_control_input():
    """
    차량 센서가 아직 없으므로
    /carin /carout을 직접 입력한다.
    """

    return input("\n제어   > ").strip()


# ============================================================
# TEXT NORMALIZE
# ============================================================

def normalize_input(text):
    text = unicodedata.normalize(
        "NFKC",
        text,
    )

    result = []

    for ch in text:
        if unicodedata.category(ch) == "Cf":
            continue

        result.append(ch)

    return "".join(result).strip()


def normalize_command(text):
    text = unicodedata.normalize(
        "NFKC",
        text,
    )

    text = text.lower().strip()

    text = re.sub(
        r"\s+",
        "",
        text,
    )

    return text


# ============================================================
# VEHICLE SESSION
# ============================================================

class VehicleSessionController:

    def __init__(self, runtime_worker):
        self.runtime_worker = runtime_worker

        self.state = AppState.IDLE

        self.vehicle_present = False

    # --------------------------------------------------------
    # VEHICLE SIGNAL
    # --------------------------------------------------------

    def update_vehicle_signal(self, present):
        present = bool(present)

        previous = self.vehicle_present

        # 같은 값이 계속 들어오면 아무 일도 하지 않는다.
        if previous == present:
            return False

        self.vehicle_present = present

        # ----------------------------------------------------
        # OFF -> ON
        # 새 차량 진입
        # ----------------------------------------------------

        if (
            previous is False
            and present is True
        ):
            self.vehicle_enter()

            return True

        # ----------------------------------------------------
        # ON -> OFF
        # 차량 이탈
        # ----------------------------------------------------

        if (
            previous is True
            and present is False
        ):
            self.vehicle_exit()

            return True

        return False

    # --------------------------------------------------------
    # VEHICLE ENTER
    # --------------------------------------------------------

    def vehicle_enter(self):

        # 새로운 차량은 IDLE일 때만 받음
        if self.state != AppState.IDLE:
            return

        # 혹시 모를 이전 고객 주문 제거
        self.runtime_worker.invalidate_and_reset(wait=False)

        self.state = AppState.ORDERING

        system_message(
            "차량 감지"
        )

        soomac_say(
            "안녕하세요. 주문을 말씀해주세요."
        )

    # --------------------------------------------------------
    # VEHICLE EXIT
    # --------------------------------------------------------

    def vehicle_exit(self):

        # 주문 중 차량이 나가버린 경우
        if self.state == AppState.ORDERING:

            self.runtime_worker.invalidate_and_reset(wait=False)

            system_message(
                "차량 이탈 - 진행 중 주문을 자동 폐기했습니다."
            )

        # 주문 완료 후 정상적으로 차량 이동
        elif self.state == AppState.WAITING_FOR_EXIT:

            system_message(
                "현재 차량 이동 완료"
            )

        else:

            system_message(
                "차량 이탈 감지"
            )

        self.state = AppState.IDLE

        show_idle()

    # --------------------------------------------------------
    # ORDER COMPLETE
    # --------------------------------------------------------

    def finish_customer_order(self):
        """
        이 함수가 호출되는 순간 현재 차량의 주문 업무는 끝.

        주문 state는 즉시 reset.

        하지만 실제 차량 센서는 아직 ON 상태이므로
        WAITING_FOR_EXIT 상태로 둔다.

        이후 차량 센서가 ON -> OFF가 되면 IDLE.
        """

        self.runtime_worker.invalidate_and_reset(wait=False)

        self.state = AppState.WAITING_FOR_EXIT

        show_waiting_for_exit()

    # --------------------------------------------------------
    # DEBUG RESET
    # --------------------------------------------------------

    def force_reset(self):

        self.runtime_worker.invalidate_and_reset(wait=False)

        if self.vehicle_present:

            self.state = AppState.ORDERING

        else:

            self.state = AppState.IDLE


# ============================================================
# PRICE
# ============================================================

def unit_price(item):

    try:

        item_type = item.get(
            "item_type"
        )

        # ----------------------------------------------------
        # BURGER
        # ----------------------------------------------------

        if item_type == "burger":

            menu = item.get(
                "menu"
            )

            order_type = item.get(
                "type"
            )

            if menu not in BURGER_BASE_PRICE:
                return None

            if order_type not in {
                "single",
                "set",
            }:
                return None

            price = BURGER_BASE_PRICE[
                menu
            ]

            # 토핑
            for topping in (
                item.get(
                    "add_toppings",
                    [],
                )
                or []
            ):

                price += TOPPING_PRICE.get(
                    topping,
                    0,
                )

            # 세트
            if order_type == "set":

                drink = item.get(
                    "drink"
                )

                size = item.get(
                    "drink_size"
                )

                side = item.get(
                    "side"
                )

                if drink not in SET_DRINK_UPCHARGE:
                    return None

                if size not in DRINK_SIZE_UPCHARGE:
                    return None

                if side not in SET_SIDE_UPCHARGE:
                    return None

                price += SET_UPCHARGE

                price += SET_DRINK_UPCHARGE[
                    drink
                ]

                price += DRINK_SIZE_UPCHARGE[
                    size
                ]

                price += SET_SIDE_UPCHARGE[
                    side
                ]

            return price

        # ----------------------------------------------------
        # DRINK
        # ----------------------------------------------------

        if item_type == "drink":

            drink = item.get(
                "drink"
            )

            size = item.get(
                "drink_size"
            )

            if drink not in STANDALONE_DRINK_PRICE:
                return None

            if size not in DRINK_SIZE_UPCHARGE:
                return None

            return (
                STANDALONE_DRINK_PRICE[
                    drink
                ]
                +
                DRINK_SIZE_UPCHARGE[
                    size
                ]
            )

        # ----------------------------------------------------
        # SIDE
        # ----------------------------------------------------

        if item_type == "side":

            side = item.get(
                "side"
            )

            if side not in STANDALONE_SIDE_PRICE:
                return None

            return STANDALONE_SIDE_PRICE[
                side
            ]

    except Exception:
        return None

    return None


# ============================================================
# PRICE BREAKDOWN
# ============================================================

def price_breakdown(item):

    result = []

    item_type = item.get(
        "item_type"
    )

    # --------------------------------------------------------
    # BURGER
    # --------------------------------------------------------

    if item_type == "burger":

        menu = item.get(
            "menu"
        )

        order_type = item.get(
            "type"
        )

        if menu not in BURGER_BASE_PRICE:
            return result

        result.append(
            (
                "기본 버거",
                BURGER_BASE_PRICE[
                    menu
                ],
                False,
            )
        )

        # 세트
        if order_type == "set":

            result.append(
                (
                    "세트 변경",
                    SET_UPCHARGE,
                    True,
                )
            )

            drink = item.get(
                "drink"
            )

            if (
                drink in SET_DRINK_UPCHARGE
                and
                SET_DRINK_UPCHARGE[
                    drink
                ] > 0
            ):

                result.append(
                    (
                        DRINK_LABELS.get(
                            drink,
                            drink,
                        )
                        + " 변경",

                        SET_DRINK_UPCHARGE[
                            drink
                        ],

                        True,
                    )
                )

            size = item.get(
                "drink_size"
            )

            if (
                size in DRINK_SIZE_UPCHARGE
                and
                DRINK_SIZE_UPCHARGE[
                    size
                ] > 0
            ):

                result.append(
                    (
                        SIZE_LABELS.get(
                            size,
                            size,
                        )
                        + " 변경",

                        DRINK_SIZE_UPCHARGE[
                            size
                        ],

                        True,
                    )
                )

            side = item.get(
                "side"
            )

            if (
                side in SET_SIDE_UPCHARGE
                and
                SET_SIDE_UPCHARGE[
                    side
                ] > 0
            ):

                result.append(
                    (
                        SIDE_LABELS.get(
                            side,
                            side,
                        )
                        + " 변경",

                        SET_SIDE_UPCHARGE[
                            side
                        ],

                        True,
                    )
                )

        # 토핑
        for topping in (
            item.get(
                "add_toppings",
                [],
            )
            or []
        ):

            price = TOPPING_PRICE.get(
                topping,
                0,
            )

            if price <= 0:
                continue

            result.append(
                (
                    TOPPING_LABELS.get(
                        topping,
                        topping,
                    )
                    + " 추가",

                    price,

                    True,
                )
            )

        return result

    # --------------------------------------------------------
    # DRINK
    # --------------------------------------------------------

    if item_type == "drink":

        drink = item.get(
            "drink"
        )

        size = item.get(
            "drink_size"
        )

        if drink not in STANDALONE_DRINK_PRICE:
            return result

        result.append(
            (
                "음료 기본",
                STANDALONE_DRINK_PRICE[
                    drink
                ],
                False,
            )
        )

        if (
            size in DRINK_SIZE_UPCHARGE
            and
            DRINK_SIZE_UPCHARGE[
                size
            ] > 0
        ):

            result.append(
                (
                    SIZE_LABELS.get(
                        size,
                        size,
                    )
                    + " 변경",

                    DRINK_SIZE_UPCHARGE[
                        size
                    ],

                    True,
                )
            )

        return result

    # --------------------------------------------------------
    # SIDE
    # --------------------------------------------------------

    if item_type == "side":

        side = item.get(
            "side"
        )

        if side in STANDALONE_SIDE_PRICE:

            result.append(
                (
                    "사이드",
                    STANDALONE_SIDE_PRICE[
                        side
                    ],
                    False,
                )
            )

    return result


# ============================================================
# ITEM DISPLAY
# ============================================================

def item_title(item):

    item_type = item.get(
        "item_type"
    )

    quantity = item.get(
        "quantity",
        1,
    )

    # --------------------------------------------------------
    # BURGER
    # --------------------------------------------------------

    if item_type == "burger":

        menu = MENU_LABELS.get(
            item.get(
                "menu"
            ),
            "버거",
        )

        order_type = TYPE_LABELS.get(
            item.get(
                "type"
            ),
            "옵션 선택 필요",
        )

        return (
            f"{menu} "
            f"{order_type} "
            f"× {quantity}"
        )

    # --------------------------------------------------------
    # DRINK
    # --------------------------------------------------------

    if item_type == "drink":

        drink = DRINK_LABELS.get(
            item.get(
                "drink"
            ),
            "음료",
        )

        size = SIZE_LABELS.get(
            item.get(
                "drink_size"
            ),
            "사이즈 선택 필요",
        )

        return (
            f"{drink} "
            f"{size} "
            f"× {quantity}"
        )

    # --------------------------------------------------------
    # SIDE
    # --------------------------------------------------------

    if item_type == "side":

        side = SIDE_LABELS.get(
            item.get(
                "side"
            ),
            "사이드",
        )

        return (
            f"{side} "
            f"× {quantity}"
        )

    return (
        f"알 수 없는 메뉴 "
        f"× {quantity}"
    )


def item_option_lines(item):

    result = []

    item_type = item.get(
        "item_type"
    )

    # --------------------------------------------------------
    # SET
    # --------------------------------------------------------

    if (
        item_type == "burger"
        and
        item.get("type") == "set"
    ):

        drink = DRINK_LABELS.get(
            item.get(
                "drink"
            ),
            "음료 선택 필요",
        )

        size = SIZE_LABELS.get(
            item.get(
                "drink_size"
            ),
            "사이즈 선택 필요",
        )

        side = SIDE_LABELS.get(
            item.get(
                "side"
            ),
            "사이드 선택 필요",
        )

        result.append(
            "   세트 : "
            f"{drink} / "
            f"{size} / "
            f"{side}"
        )

    # --------------------------------------------------------
    # EXCLUDE
    # --------------------------------------------------------

    excludes = (
        item.get(
            "exclude",
            [],
        )
        or []
    )

    if excludes:

        labels = [
            EXCLUDE_LABELS.get(
                value,
                value,
            )
            for value in excludes
        ]

        result.append(
            "   제외 : "
            + ", ".join(
                labels
            )
        )

    # --------------------------------------------------------
    # TOPPING
    # --------------------------------------------------------

    toppings = (
        item.get(
            "add_toppings",
            [],
        )
        or []
    )

    if toppings:

        labels = []

        for topping in toppings:

            label = TOPPING_LABELS.get(
                topping,
                topping,
            )

            price = TOPPING_PRICE.get(
                topping,
                0,
            )

            if price > 0:

                labels.append(
                    f"{label} "
                    f"(+{price:,}원)"
                )

            else:

                labels.append(
                    label
                )

        result.append(
            "   추가 : "
            + ", ".join(
                labels
            )
        )

    return result


# ============================================================
# CURRENT ORDER
# ============================================================

def show_current_order(state):

    items = state.get(
        "items",
        [],
    )

    if not items:
        return

    print()
    print("[ 현재 주문 ]")

    line("-")

    total = 0

    complete = True

    for index, item in enumerate(
        items,
        start=1,
    ):

        price = unit_price(
            item
        )

        quantity = int(
            item.get(
                "quantity",
                1,
            )
        )

        print(
            f"{index}. "
            f"{item_title(item)}"
        )

        for option in item_option_lines(
            item
        ):
            print(option)

        # ----------------------------------------------------
        # INCOMPLETE
        # ----------------------------------------------------

        if price is None:

            if (
                item.get(
                    "item_type"
                )
                == "burger"
            ):

                menu = item.get(
                    "menu"
                )

                if menu in BURGER_BASE_PRICE:

                    print(
                        f"   기본가 : "
                        f"{BURGER_BASE_PRICE[menu]:,}원"
                    )

            print(
                "   최종가 : "
                "옵션 선택 후 확정"
            )

            complete = False

            print()

            continue

        # ----------------------------------------------------
        # BREAKDOWN
        # ----------------------------------------------------

        breakdown = price_breakdown(
            item
        )

        if breakdown:
            print()

        for (
            label,
            value,
            is_plus,
        ) in breakdown:

            if is_plus:
                value_text = (
                    f"+{value:,}원"
                )

            else:
                value_text = (
                    f"{value:,}원"
                )

            print(
                f"   {label:<9} "
                f": {value_text}"
            )

        subtotal = (
            price
            * quantity
        )

        total += subtotal

        print(
            f"   {'단가':<9} "
            f": {price:,}원"
        )

        if quantity > 1:

            print(
                f"   {'소계':<9} "
                f": {price:,}원 "
                f"× {quantity} "
                f"= {subtotal:,}원"
            )

        else:

            print(
                f"   {'소계':<9} "
                f": {subtotal:,}원"
            )

        print()

    line("-")

    if complete:

        print(
            f"총 합계 : "
            f"{total:,}원"
        )

    else:

        if total > 0:

            print(
                f"현재 계산 금액 : "
                f"{total:,}원"
            )

        print(
            "총 합계 : "
            "옵션 선택 완료 후 확정"
        )

    line("-")


# ============================================================
# PENDING
# ============================================================

def pending_message(pending):

    if pending is None:

        return (
            "추가 주문이 있으시면 말씀해주세요. "
            "주문을 마치시려면 "
            "마무리한다고 말씀해주세요."
        )

    _, field = pending

    if field == "type":

        return (
            "단품과 세트 중 "
            "어떤 것으로 드릴까요?"
        )

    if field == "drink":

        return (
            "음료는 무엇으로 드릴까요?"
        )

    if field == "drink_size":

        return (
            "음료 사이즈는 "
            "스몰, 미디엄, 라지 중 "
            "어떤 것으로 드릴까요?"
        )

    if field == "side":

        return (
            "사이드는 감자튀김과 "
            "치즈스틱 중 "
            "어떤 것으로 드릴까요?"
        )

    return (
        "필요한 옵션을 말씀해주세요."
    )


# ============================================================
# COMPLETE UI
# ============================================================

def show_counter_complete(handoff):

    print()

    line("=")

    print(
        "주문 접수 완료".center(
            WIDTH
        )
    )

    line("=")

    print()

    print(
        f"내부 주문번호 : "
        f"{handoff['order_id']}"
    )

    print(
        f"주문 금액     : "
        f"{handoff['total_price']:,}원"
    )

    print()

    print(
        "FINAL HANDOFF 생성 완료"
    )

    line("=")


def show_mobile_complete(handoff):

    print()

    line("=")

    print(
        "맥오더 접수 완료".center(
            WIDTH
        )
    )

    line("=")

    print()

    print(
        f"내부 주문번호 : "
        f"{handoff['order_id']}"
    )

    print(
        f"맥오더 번호   : "
        f"{handoff['mobile_order_id']}"
    )

    print()

    print(
        "FINAL HANDOFF 생성 완료"
    )

    line("=")


# ============================================================
# DEBUG
# ============================================================

def show_debug_result(result):

    print()

    line("=")

    print("DEBUG MODE")

    line("=")

    print()
    print("[ LLM UPDATE ]")

    print(
        json.dumps(
            result.get(
                "llm_update"
            ),
            ensure_ascii=False,
            indent=2,
        )
    )

    print()
    print("[ VERIFIED UPDATE ]")

    print(
        json.dumps(
            result.get(
                "verified_update"
            ),
            ensure_ascii=False,
            indent=2,
        )
    )

    print()
    print("[ STATE ]")

    print(
        json.dumps(
            result.get(
                "state"
            ),
            ensure_ascii=False,
            indent=2,
        )
    )

    print(
        "pending:",
        result.get(
            "pending"
        ),
    )

    warnings = result.get(
        "warnings",
        [],
    )

    if warnings:

        print()
        print("[ WARNINGS ]")

        for warning in warnings:
            print(
                "-",
                warning,
            )

    line("=")


def show_debug_handoff(handoff):

    print()

    line("=")

    print(
        "DEBUG : FINAL HANDOFF"
    )

    line("=")

    print(
        json.dumps(
            handoff,
            ensure_ascii=False,
            indent=2,
        )
    )

    line("=")


# ============================================================
# RESET ALL
# ============================================================

def reset_all(
    runtime_worker,
    handoff_manager,
):

    runtime_worker.invalidate_and_reset(
        wait=True
    )

    storage_dir = Path(
        handoff_manager.storage_dir
    )

    deleted = 0

    for pattern in (
        "handoff_*.json",
        "order_*.json",
    ):

        for path in storage_dir.glob(
            pattern
        ):

            try:

                path.unlink()

                deleted += 1

            except FileNotFoundError:

                pass

    return deleted


# ============================================================
# MAIN
# ============================================================


# ============================================================
# CUSTOMER INPUT GUARD
# ============================================================

def pending_retry_prompt(pending):
    """
    현재 pending 상태에 맞는 재질문 문장을 반환한다.

    pending 예:
        (line_id, "type")
        (line_id, "drink")
        (line_id, "drink_size")
        (line_id, "side")
    """

    if (
        not isinstance(pending, (tuple, list))
        or len(pending) < 2
    ):
        return (
            "주문 내용을 정확히 말씀해주세요."
        )

    field = pending[1]

    prompts = {
        "type": (
            "단품 또는 세트 중에서 "
            "선택해주세요."
        ),
        "drink": (
            "음료를 선택해주세요."
        ),
        "drink_size": (
            "음료 사이즈를 선택해주세요."
        ),
        "side": (
            "사이드 메뉴를 선택해주세요."
        ),
    }

    return prompts.get(
        field,
        "주문 내용을 다시 말씀해주세요.",
    )


def guard_customer_input(
    text,
    pending=None,
    mobile_confirmation_pending=False,
):
    """
    V14 Runtime으로 보내기 전의 최소 입력 안전망.

    원칙:
    - Python에서 자연어 주문을 다시 해석하지 않는다.
    - 명백한 무입력/잡음/불완전 카테고리만 차단한다.
    - 정상적인 짧은 pending 답변은 허용한다.
    """

    # ========================================================
    # 1. 안전한 문자열 정규화
    # ========================================================

    if text is None:
        raw = ""
    else:
        raw = str(text)

    raw = normalize_input(raw).strip()

    normalized = (
        raw.lower()
        .replace(" ", "")
    )

    # 공백/문장부호를 제거한 의미 비교용 문자열.
    # 실제 Runtime으로 보내는 원문은 raw 그대로 유지한다.
    compact = "".join(
        ch
        for ch in normalized
        if ch.isalnum()
    )

    # ========================================================
    # 2. 빈 입력
    # ========================================================

    if not raw:
        return {
            "allow": False,
            "reason": "empty_input",
            "reply": (
                "잘 듣지 못했습니다. "
                "다시 말씀해주세요."
            ),
        }

    # ========================================================
    # 3. 지나치게 긴 비정상 입력
    # ========================================================

    # 정상 주문 발화가 이 정도 길이에 도달할 이유가 거의 없다.
    # STT 폭주/깨진 transcript가 LLM context로 들어가는 것을 막는다.
    if len(raw) > 300:
        return {
            "allow": False,
            "reason": "input_too_long",
            "reply": (
                "주문 내용을 조금 짧게 나누어서 "
                "말씀해주세요."
            ),
        }

    # ========================================================
    # 4. 문자/숫자가 전혀 없는 입력
    # ========================================================

    if not compact:
        return {
            "allow": False,
            "reason": "non_semantic_input",
            "reply": (
                "잘 듣지 못했습니다. "
                "다시 말씀해주세요."
            ),
        }

    # ========================================================
    # 5. 명백한 filler / 잡음
    # ========================================================

    filler_inputs = {
        "음",
        "음음",
        "으음",
        "흠",
        "어",
        "어어",
        "아",
        "아아",
        "저기",
    }

    repeated_noise = (
        len(compact) >= 3
        and len(set(compact)) == 1
        and compact[0] in {
            "아",
            "어",
            "음",
            "흠",
            "ㅋ",
            "ㅎ",
        }
    )

    if (
        compact in filler_inputs
        or repeated_noise
    ):

        if pending is not None:
            reply = pending_retry_prompt(
                pending
            )

        elif mobile_confirmation_pending:
            reply = (
                "맥오더 주문번호가 맞는지 "
                "말씀해주세요."
            )

        else:
            reply = (
                "잘 듣지 못했습니다. "
                "주문을 다시 말씀해주세요."
            )

        return {
            "allow": False,
            "reason": "filler_input",
            "reply": reply,
        }

    # ========================================================
    # 6. 카테고리 이름만 말한 불완전 주문
    # ========================================================

    incomplete_categories = {
        "햄버거": (
            "어떤 햄버거를 "
            "주문하시겠어요?"
        ),
        "버거": (
            "어떤 햄버거를 "
            "주문하시겠어요?"
        ),
        "음료": (
            "어떤 음료를 "
            "주문하시겠어요?"
        ),
        "음료수": (
            "어떤 음료를 "
            "주문하시겠어요?"
        ),
        "사이드": (
            "어떤 사이드 메뉴를 "
            "주문하시겠어요?"
        ),
        "사이드메뉴": (
            "어떤 사이드 메뉴를 "
            "주문하시겠어요?"
        ),
    }

    # STT에서 흔한 정중어 정도만 제거한다.
    # 자연어 전체를 규칙으로 해석하지 않는다.
    category_candidate = compact

    for suffix in (
        "주세요",
        "이요",
        "요",
    ):
        if (
            category_candidate.endswith(suffix)
            and len(category_candidate) > len(suffix)
        ):
            category_candidate = (
                category_candidate[
                    :-len(suffix)
                ]
            )
            break

    if (
        category_candidate
        in incomplete_categories
    ):

        # 이미 Runtime pending 질문이 있다면
        # 새로운 category 질문보다 기존 pending을 우선한다.
        if pending is not None:
            reply = pending_retry_prompt(
                pending
            )
        else:
            reply = (
                incomplete_categories[
                    category_candidate
                ]
            )

        return {
            "allow": False,
            "reason": "incomplete_category",
            "reply": reply,
        }

    # ========================================================
    # 정상 입력
    # ========================================================

    return {
        "allow": True,
        "reason": None,
        "reply": None,
    }


def main():

    runtime_worker = RuntimeWorker(
        DriveThruRuntime
    )

    handoff_manager = (
        OrderHandoffManager(
            storage_dir=(
                "runtime_data/handoffs"
            ),
            first_order_id=1,
        )
    )

    session = (
        VehicleSessionController(
            runtime_worker
        )
    )

    debug_mode = False

    last_handoff = None

    # 모바일 주문번호는 고객 확인 후에만 FINAL HANDOFF 한다.
    pending_mobile_order_id = None


    header()

    show_idle()

    # ========================================================
    # LOOP
    # ========================================================

    while True:

        try:

            if (
                session.state
                == AppState.ORDERING
            ):

                raw_text = (
                    get_customer_input()
                )

            else:

                raw_text = (
                    get_control_input()
                )

        except (
            EOFError,
            KeyboardInterrupt,
        ):

            print()

            system_message(
                "프로그램 종료"
            )

            break

        text = normalize_input(
            raw_text
        )

        if not text:
            continue

        command = normalize_command(
            text
        )

        # ====================================================
        # VEHICLE COMMAND
        #
        # 가장 먼저 처리한다.
        # 주문 상태와 관계없이 차량 센서 이벤트가 우선이다.
        # ====================================================

        if command in {
            "/carin",
            "carin",
        }:

            changed = (
                session
                .update_vehicle_signal(
                    True
                )
            )


            if not changed:

                system_message(
                    "이미 차량이 감지되고 있습니다."
                )

            continue

        if command in {
            "/carout",
            "carout",
        }:

            pending_mobile_order_id = None

            changed = (
                session
                .update_vehicle_signal(
                    False
                )
            )


            if not changed:

                system_message(
                    "현재 감지된 차량이 없습니다."
                )

            continue

        # ====================================================
        # DEVELOPMENT COMMANDS
        # ====================================================

        if command == "/quit":

            system_message(
                "프로그램 종료"
            )

            break

        if command == "/debug":

            debug_mode = (
                not debug_mode
            )

            system_message(
                "DEBUG MODE : "
                + (
                    "ON"
                    if debug_mode
                    else "OFF"
                )
            )

            continue

        if command == "/reset":

            runtime_worker.invalidate_and_reset(
                wait=True
            )

            pending_mobile_order_id = None


            system_message(
                "현재 주문 state 초기화"
            )

            if (
                session.state
                == AppState.ORDERING
            ):

                soomac_say(
                    "주문을 다시 말씀해주세요."
                )

            continue

        if command == "/resetall":

            deleted = reset_all(
                runtime_worker,
                handoff_manager,
            )

            session.vehicle_present = False

            session.state = AppState.IDLE


            last_handoff = None
            pending_mobile_order_id = None

            system_message(
                "전체 개발 데이터 초기화 완료"
            )

            print(
                f"삭제된 데이터 : "
                f"{deleted}개"
            )

            print(
                "다음 내부 주문번호 : 1"
            )

            show_idle()

            continue

        if command == "/state":

            print()

            print(
                "APP STATE:",
                session.state.value,
            )

            print(
                "VEHICLE:",
                session.vehicle_present,
            )

            print(
                "ORDER STATE:"
            )

            print(
                json.dumps(
                    runtime_worker.snapshot()["state"],
                    ensure_ascii=False,
                    indent=2,
                )
            )

            print(
                "pending:",
                runtime_worker.snapshot()["pending"],
            )

            continue

        if command == "/order":

            if last_handoff is None:

                system_message(
                    "아직 완료된 주문이 없습니다."
                )

            else:

                print()

                print(
                    json.dumps(
                        last_handoff,
                        ensure_ascii=False,
                        indent=2,
                    )
                )

            continue

        # ====================================================
        # UNKNOWN COMMAND
        # ====================================================

        if command.startswith("/"):

            system_message(
                "알 수 없는 명령입니다."
            )

            continue

        # ====================================================
        # NOT ORDERING
        # ====================================================

        if (
            session.state
            != AppState.ORDERING
        ):

            if (
                session.state
                == AppState.IDLE
            ):

                system_message(
                    "차량 감지 전입니다."
                )

            elif (
                session.state
                == AppState.WAITING_FOR_EXIT
            ):

                system_message(
                    "현재 차량이 이동할 때까지 "
                    "다음 주문을 시작하지 않습니다."
                )

            continue

        # ====================================================
        # CUSTOMER TEXT NORMALIZATION
        # ====================================================
        # 이후 STT가 None이나 예상하지 못한 값을 반환하더라도
        # .strip() 호출 때문에 앱이 죽지 않게 한다.

        if text is None:
            text = ""
        elif not isinstance(text, str):
            text = str(text)

        text = text.strip()

        # ====================================================
        # MOBILE PICKUP CONFIRMATION
        # ====================================================
        # mobile_pickup은 번호를 인식하자마자 handoff하지 않는다.
        # 고객이 긍정 응답한 경우에만 FINAL HANDOFF를 생성한다.
        #
        # 부정/정정 발화는 V14 Runtime으로 다시 보내서
        # 새로운 mobile_order_id를 해석하도록 한다.

        if pending_mobile_order_id is not None:

            normalized_confirmation = (
                text.strip()
                .lower()
                .replace(" ", "")
            )

            positive_mobile_answers = {
                "네",
                "네맞아요",
                "네맞습니다",
                "맞아요",
                "맞습니다",
                "응",
                "어",
                "예",
                "예맞아요",
                "맞아",
                "그거맞아요",
                "그거맞습니다",
            }

            if (
                normalized_confirmation
                in positive_mobile_answers
            ):

                try:

                    handoff = (
                        handoff_manager
                        .create_mobile_handoff(
                            int(
                                pending_mobile_order_id
                            )
                        )
                    )

                except OrderHandoffError as e:

                    system_message(
                        f"맥오더 처리 오류: {e}"
                    )

                    continue

                last_handoff = handoff

                show_mobile_complete(
                    handoff
                )

                soomac_say(
                    f"맥오더 "
                    f"{pending_mobile_order_id}번을 "
                    "확인했습니다. "
                    "앞으로 이동해주세요."
                )

                if debug_mode:

                    show_debug_handoff(
                        handoff
                    )

                pending_mobile_order_id = None

                session.finish_customer_order()

                continue

            # 긍정 응답이 아니면 번호 정정 가능성이 있으므로
            # 기존 번호를 확정하지 않고 아래 V14 Runtime으로 보낸다.

        # ====================================================
        # CUSTOMER INPUT GUARD
        # ====================================================

        input_guard = guard_customer_input(
            text,
            pending=runtime_worker.snapshot()["pending"],
            mobile_confirmation_pending=(
                pending_mobile_order_id
                is not None
            ),
        )

        if not input_guard["allow"]:

            if debug_mode:
                system_message(
                    "[INPUT GUARD] "
                    f"{input_guard['reason']}"
                )

            soomac_say(
                input_guard["reply"]
            )

            continue

        # ====================================================
        # ALL CUSTOMER UTTERANCES -> V14 RUNTIME
        # ====================================================
        # 모바일 주문도 별도 Python fast path로 우회하지 않는다.
        # LLM -> XGrammar/Pydantic -> deterministic verifier -> state manager
        # 경로를 동일하게 사용한다.

        # ====================================================
        # NORMAL / MOBILE ORDER PARSING
        # ====================================================

        try:

            result = runtime_worker.process(
                text
            )

        except StaleRuntimeRequest:

            if debug_mode:
                system_message(
                    "[RUNTIME] "
                    "stale customer request discarded"
                )

            continue

        except Exception as e:

            # 예상하지 못한 내부 오류는 traceback을 숨기지 않는다.
            # Runtime 내부에서는 state가 이미 rollback된 상태다.
            traceback.print_exc()

            system_message(
                "[INTERNAL ERROR] "
                f"{type(e).__name__}: "
                f"{e}"
            )

            soomac_say(
                "주문 처리 중 문제가 발생했습니다. "
                "다시 말씀해주세요."
            )

            continue

        if debug_mode:

            show_debug_result(
                result
            )

        # ====================================================
        # HANDLED ORDER ERROR
        # ====================================================
        # 개발자용 detail과 고객에게 들려줄 reply를 분리한다.
        error = result.get("error")

        if error:

            system_message(
                f"[{error.get('kind')} / "
                f"{error.get('code')}] "
                f"{error.get('detail')}"
            )

            soomac_say(
                result.get("reply")
                or "주문 내용을 다시 말씀해주세요."
            )

            continue

        state = result[
            "state"
        ]

        intent = state.get(
            "intent"
        )

        pending = result.get(
            "pending"
        )

        # ====================================================
        # ORDER
        # ====================================================

        if intent == "order":

            show_current_order(
                state
            )

            soomac_say(
                pending_message(
                    pending
                )
            )

            continue

        # ====================================================
        # CONFIRM
        # ====================================================

        if intent == "confirm":

            try:

                handoff = (
                    handoff_manager
                    .create_counter_handoff(
                        state
                    )
                )

            except OrderHandoffError as e:

                system_message(
                    f"주문 확정 오류: {e}"
                )

                soomac_say(
                    "주문 내용을 다시 확인해주세요."
                )

                continue

            last_handoff = handoff

            show_counter_complete(
                handoff
            )

            soomac_say(
                f"주문이 완료되었습니다. "
                f"주문 금액은 "
                f"{handoff['total_price']:,}원입니다. "
                "앞으로 이동해주세요. "
            )

            if debug_mode:

                show_debug_handoff(
                    handoff
                )


            # 주문 시스템 역할 종료
            # 주문 state 즉시 reset
            session.finish_customer_order()

            continue

        # ====================================================
        # CANCEL
        # ====================================================

        if intent == "cancel":

            soomac_say(
                "주문을 취소했습니다. "
                "앞으로 이동해주세요."
            )


            session.finish_customer_order()

            continue

        # ====================================================
        # UNKNOWN
        # ====================================================

        if intent == "unknown":

            soomac_say(
                "죄송합니다. "
                "주문 내용을 다시 말씀해주세요."
            )

            continue

        # ====================================================
        # MOBILE PICKUP (V14 RUNTIME RESULT)
        # ====================================================

        if intent == "mobile_pickup":

            mobile_order_id = state.get(
                "order_id"
            )

            if mobile_order_id is None:

                soomac_say(
                    "맥오더 주문번호를 말씀해주세요."
                )

                continue

            # 즉시 FINAL HANDOFF 하지 않고 고객 확인을 기다린다.
            pending_mobile_order_id = int(
                mobile_order_id
            )

            soomac_say(
                f"맥오더 "
                f"{pending_mobile_order_id}번 "
                "맞으신가요?"
            )

            continue


    runtime_worker.stop()


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":
    main()
