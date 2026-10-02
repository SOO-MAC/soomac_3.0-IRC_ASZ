#!/usr/bin/env python3

import json
from pathlib import Path

from router_schema import RouterOutput
from router_policy import evaluate_router_output
from router_gold_canonical import canonicalize_case


OUT = (
    Path(__file__).parent
    / "router_data/gold/router_gold_coverage_v1.jsonl"
)


def ref(source="explicit", resolved=True, value=None, line_ids=None):
    data = {
        "source": source,
        "resolved": resolved,
        "line_ids": line_ids or [],
    }

    if value is not None:
        data["value"] = value

    return data


def act(
    family,
    subtype,
    speech_act,
    commitment="none",
    *,
    target_domain=None,
    target=None,
    reference=None,
    resolution="clear",
):
    data = {
        "family": family,
        "subtype": subtype,
        "speech_act": speech_act,
        "commitment": commitment,
        "reference": (
            reference
            if reference is not None
            else ref(
                source="none",
                resolved=False,
            )
        ),
        "resolution": resolution,
    }

    if target_domain is not None:
        data["target_domain"] = target_domain

    if target is not None:
        data["target"] = target

    return data


def case(
    cid,
    utterance,
    router_act,
    policy,
    *,
    tag,
    reason,
    history=None,
    pending=None,
    items=None,
):
    return {
        "id": cid,
        "tag": tag,
        "utterance": utterance,
        "context": {
            "history": history or [],
            "pending": pending,
            "order_state": {
                "items": items or [],
            },
        },
        "expected": {
            "acts": [router_act],
        },
        "expected_policy": [policy],
        "policy_reason": reason,
    }


cases = []
counter = 1


def add(
    utterance,
    router_act,
    policy="read_only",
    *,
    tag,
    reason,
    history=None,
    pending=None,
    items=None,
):
    global counter

    cases.append(
        case(
            f"V{counter:03d}",
            utterance,
            router_act,
            policy,
            tag=tag,
            reason=reason,
            history=history,
            pending=pending,
            items=items,
        )
    )

    counter += 1


# ============================================================
# A. MENU QUERY
# 20
# ============================================================

MENU_CASES = [
    ("메뉴 뭐 있어요?", "menu", None),
    ("전체 메뉴 알려주세요", "menu", None),
    ("버거 뭐 있어요?", "burger", None),
    ("햄버거 종류 알려주세요", "burger", None),
    ("음료 뭐 있어요?", "drink", None),
    ("마실 거 뭐 있어요?", "drink", None),
    ("사이드 뭐 있어요?", "side", None),
    ("사이드 메뉴 알려주세요", "side", None),

    ("불고기버거 있어요?", "burger", "bulgogi_burger"),
    ("불고기버거 팔아요?", "burger", "bulgogi_burger"),
    ("치킨버거 있어요?", "burger", "chicken_burger"),
    ("치즈버거 팔아요?", "burger", "cheese_burger"),
    ("새우버거 있어요?", "burger", "shrimp_burger"),

    ("제로콜라 있어요?", "drink", "zero_coke"),
    ("사이다 팔아요?", "drink", "sprite"),
    ("환타 있어요?", "drink", "fanta"),
    ("아이스커피 있어요?", "drink", "iced_coffee"),

    ("감자튀김 있어요?", "side", "french_fries"),
    ("치즈스틱 팔아요?", "side", "cheese_stick"),
    ("제로 음료 있어요?", "drink", "zero_coke"),
]

for text, domain, target in MENU_CASES:

    add(
        text,
        act(
            "info_query",
            "menu",
            "question",
            target_domain=domain,
            target=target,
            reference=(
                ref(value=target)
                if target
                else ref(
                    source="none",
                    resolved=False,
                )
            ),
        ),
        tag="coverage_menu",
        reason="menu_catalog_or_existence_query",
    )


# ============================================================
# B. RECOMMENDATION
# 30
# ============================================================

GENERAL_REC = [
    "뭐 추천해줄 수 있어요?",
    "추천 좀 해주세요",
    "뭐 먹는 게 좋아요?",
    "하나만 추천해주세요",
    "버거 추천해주세요",
    "사이드 추천해주세요",
    "음료 추천해주세요",
    "세트 추천해주세요",
    "처음 왔는데 뭐가 좋아요?",
    "제일 무난한 메뉴가 뭐예요?",
]

for text in GENERAL_REC:

    add(
        text,
        act(
            "recommendation",
            "general_recommendation",
            "question" if "뭐" in text else "request",
        ),
        tag="coverage_recommendation",
        reason="general_recommendation_request",
    )


PREFERENCE_REC = [
    "안 매운 걸로 추천해주세요",
    "매운 거 좋아하는데 뭐가 좋아요?",
    "치즈 좋아하는데 추천해주세요",
    "가볍게 먹을 거 추천해주세요",
    "배부르게 먹고 싶은데 뭐가 좋아요?",
    "달달한 음료 추천해주세요",
    "느끼하지 않은 메뉴 추천해주세요",
    "콜라랑 잘 어울리는 버거 추천해주세요",
    "커피랑 먹을 만한 메뉴 추천해주세요",
    "혼자 먹기 적당한 거 추천해주세요",
]

for text in PREFERENCE_REC:

    add(
        text,
        act(
            "recommendation",
            "preference_recommendation",
            "request",
        ),
        tag="coverage_recommendation",
        reason="preference_based_recommendation",
    )


BUDGET_COMPARE = [
    ("만원 안으로 추천해주세요", "budget_recommendation"),
    ("8천 원 안으로 먹을 거 추천해주세요", "budget_recommendation"),
    ("둘이 2만원 안으로 추천해주세요", "budget_recommendation"),
    ("가성비 좋은 조합 추천해주세요", "budget_recommendation"),
    ("제 예산이 만오천 원인데 추천해주세요", "budget_recommendation"),

    ("불고기버거랑 치킨버거 뭐가 나아요?", "comparison"),
    ("치즈버거랑 새우버거 중 뭐가 좋아요?", "comparison"),
    ("감자튀김이랑 치즈스틱 뭐가 나아요?", "comparison"),
    ("콜라랑 제로콜라 중 뭐가 나아요?", "comparison"),
    ("단품이랑 세트 중 뭐가 나아요?", "comparison"),
]

for text, subtype in BUDGET_COMPARE:

    add(
        text,
        act(
            "recommendation",
            subtype,
            "question",
        ),
        tag="coverage_recommendation",
        reason="budget_or_comparison_recommendation",
    )


# ============================================================
# C. PAYMENT
# 20
# ============================================================

PAYMENT_CASES = [
    ("카드 돼요?", "card"),
    ("신용카드 돼요?", "credit_card"),
    ("체크카드 돼요?", "debit_card"),
    ("현금 돼요?", "cash"),
    ("계좌이체 가능해요?", "bank_transfer"),
    ("NFC 결제 돼요?", "nfc"),
    ("삼성페이 돼요?", "samsung_pay"),
    ("애플페이 돼요?", "apple_pay"),
    ("모바일 결제 가능해요?", "mobile_payment"),
    ("교통카드 결제 돼요?", "transit_card"),

    ("결제는 어떻게 해요?", "payment_method"),
    ("결제 어디서 해요?", "payment_location"),
    ("카드 어디에 대면 돼요?", "card_tap"),
    ("카드 꽂아야 하나요?", "card_insert"),
    ("태그하면 되는 거예요?", "card_tap"),
    ("결제는 언제 하면 돼요?", "payment_timing"),
    ("지금 결제해도 돼요?", "payment_timing"),
    ("결제 완료된 거 맞아요?", "payment_status"),
    ("결제가 안 된 것 같은데요", "payment_status"),
    ("영수증 받을 수 있어요?", "receipt"),
]

for text, target in PAYMENT_CASES:

    add(
        text,
        act(
            "info_query",
            "payment",
            "question",
            target_domain="payment",
            target=target,
            reference=ref(value=target),
        ),
        tag="coverage_payment",
        reason="payment_information_query",
    )


# ============================================================
# D. STORE / PICKUP / WAIT TIME
# 25
# ============================================================

STORE_CASES = [
    ("매장에 자리 있어요?", "seat"),
    ("먹고 갈 수 있어요?", "dine_in"),
    ("포장만 가능한가요?", "takeout"),
    ("화장실 있어요?", "restroom"),
    ("주차장 있어요?", "parking"),
    ("주차 가능해요?", "parking"),
    ("몇 시까지 해요?", "business_hours"),
    ("지금 영업 중이에요?", "open_status"),
    ("와이파이 있어요?", "wifi"),
    ("콘센트 있어요?", "power_outlet"),
]

for text, target in STORE_CASES:

    add(
        text,
        act(
            "info_query",
            "store",
            "question",
            target_domain="store",
            target=target,
            reference=ref(value=target),
        ),
        tag="coverage_store",
        reason="store_information_query",
    )


PICKUP_CASES = [
    ("어디서 받아요?", "pickup_location"),
    ("음식은 어디서 받으면 돼요?", "pickup_location"),
    ("다음 창구에서 받나요?", "pickup_window"),
    ("여기서 기다리면 돼요?", "waiting_location"),
    ("앞으로 이동하면 돼요?", "move_forward"),
    ("차에서 기다리면 되나요?", "vehicle_wait"),
    ("주문번호 어디서 확인해요?", "order_number"),
    ("수령할 때 주문번호 말하면 돼요?", "order_number"),
]

for text, target in PICKUP_CASES:

    add(
        text,
        act(
            "info_query",
            "pickup",
            "question",
            target_domain="order",
            target=target,
            reference=ref(value=target),
        ),
        tag="coverage_pickup",
        reason="pickup_process_query",
    )


WAIT_CASES = [
    "얼마나 걸려요?",
    "몇 분 기다려야 해요?",
    "언제 나와요?",
    "아직 멀었어요?",
    "지금 많이 밀렸어요?",
    "주문 언제 준비돼요?",
    "제 주문 준비됐어요?",
]

for text in WAIT_CASES:

    add(
        text,
        act(
            "info_query",
            "wait_time",
            "question",
            target_domain="order",
            target="current_order",
            reference=ref(
                source="current_order",
                resolved=True,
                value="current_order",
            ),
            resolution="context_resolved",
        ),
        tag="coverage_wait_time",
        reason="order_wait_or_ready_status_query",
    )


# ============================================================
# E. INGREDIENT / ALLERGEN / NUTRITION / OPTION
# 30
# ============================================================

INGREDIENT_CASES = [
    ("불고기버거에 뭐 들어가요?", "burger", "bulgogi_burger"),
    ("치킨버거 재료 뭐예요?", "burger", "chicken_burger"),
    ("치즈버거에 뭐 들어가요?", "burger", "cheese_burger"),
    ("새우버거 재료 알려주세요", "burger", "shrimp_burger"),
    ("불고기버거에 양파 들어가요?", "ingredient", "onion"),
    ("피클 들어가요?", "ingredient", "pickle"),
    ("토마토 들어가나요?", "ingredient", "tomato"),
    ("양상추 있어요?", "ingredient", "lettuce"),
    ("치즈 들어가나요?", "ingredient", "cheese"),
    ("베이컨 기본으로 들어가요?", "ingredient", "bacon"),
]

for text, domain, target in INGREDIENT_CASES:

    add(
        text,
        act(
            "info_query",
            "ingredient",
            "question",
            target_domain=domain,
            target=target,
            reference=ref(value=target),
        ),
        tag="coverage_ingredient",
        reason="ingredient_information_query",
    )


ALLERGEN_CASES = [
    ("알레르기 정보 있어요?", "allergen"),
    ("우유 들어가요?", "milk"),
    ("계란 들어가요?", "egg"),
    ("밀 들어가요?", "wheat"),
    ("갑각류 들어가요?", "shellfish"),
    ("새우 알레르기 있는데 먹어도 돼요?", "shellfish"),
    ("유제품 없는 메뉴 있어요?", "dairy"),
    ("알레르기 성분표 볼 수 있어요?", "allergen"),
]

for text, target in ALLERGEN_CASES:

    add(
        text,
        act(
            "info_query",
            "allergen",
            "question",
            target_domain="menu",
            target=target,
            reference=ref(value=target),
        ),
        tag="coverage_allergen",
        reason="allergen_information_query",
    )


NUTRITION_CASES = [
    ("칼로리 얼마예요?", "calorie"),
    ("불고기버거 칼로리 알려주세요", "calorie"),
    ("세트 칼로리 얼마예요?", "calorie"),
    ("영양정보 있어요?", "nutrition"),
    ("나트륨 얼마나 들어가요?", "sodium"),
    ("당류 얼마나 들어가요?", "sugar"),
]

for text, target in NUTRITION_CASES:

    add(
        text,
        act(
            "info_query",
            "nutrition",
            "question",
            target_domain="menu",
            target=target,
            reference=ref(value=target),
        ),
        tag="coverage_nutrition",
        reason="nutrition_information_query",
    )


OPTION_CASES = [
    ("세트에 뭐 들어가요?", "burger", "set"),
    ("단품이랑 세트 차이가 뭐예요?", "burger", "set"),
    ("음료 뭐 선택할 수 있어요?", "drink", "drink_options"),
    ("사이드 뭐 선택할 수 있어요?", "side", "side_options"),
    ("사이즈 뭐 있어요?", "drink", "size_options"),
    ("토핑 뭐 추가할 수 있어요?", "topping", "topping_options"),
]

for text, domain, target in OPTION_CASES:

    add(
        text,
        act(
            "info_query",
            "option",
            "question",
            target_domain=domain,
            target=target,
            reference=ref(value=target),
        ),
        tag="coverage_option",
        reason="option_information_query",
    )


# ============================================================
# F. CONVERSATION CONTROL / STAFF
# 20
# ============================================================

REPEAT_CASES = [
    "다시 말해주세요",
    "한 번 더 말해주세요",
    "뭐라고요?",
    "방금 뭐라고 했어요?",
    "질문 다시 해주세요",
    "아까 안내 다시 해주세요",
]

for text in REPEAT_CASES:

    add(
        text,
        act(
            "conversation_control",
            "repeat",
            "request",
            reference=ref(
                source="previous_staff",
                resolved=True,
                value="previous_staff_message",
            ),
            resolution="context_resolved",
        ),
        tag="coverage_conversation_control",
        reason="repeat_previous_staff_response",
        history=[
            {
                "role": "staff",
                "text": "음료를 선택해주세요.",
            }
        ],
    )


for text in [
    "잠깐만요",
    "잠시만 기다려주세요",
    "메뉴 좀 볼게요",
    "조금만 생각할게요",
]:
    add(
        text,
        act(
            "conversation_control",
            "pause",
            "request",
        ),
        tag="coverage_conversation_control",
        reason="pause_conversation_without_state_change",
    )


for text in [
    "이제 할게요",
    "계속할게요",
    "다시 주문할게요",
    "이제 이어서 할게요",
]:
    add(
        text,
        act(
            "conversation_control",
            "resume",
            "request",
        ),
        tag="coverage_conversation_control",
        reason="resume_paused_conversation",
    )


for text in [
    "직원 불러주세요",
    "직원분 좀 와주세요",
    "사람이랑 이야기하고 싶어요",
]:
    add(
        text,
        act(
            "staff_request",
            "staff_call",
            "request",
            "explicit",
            target_domain="staff",
            target="staff",
            reference=ref(value="staff"),
        ),
        "event",
        tag="coverage_staff",
        reason="explicit_staff_call",
    )


for text in [
    "직원 안 불러도 돼요",
    "직원 호출 취소해주세요",
    "아까 부른 직원 안 와도 돼요",
]:
    add(
        text,
        act(
            "staff_request",
            "staff_call_cancel",
            "request",
            "explicit",
            target_domain="staff",
            target="staff",
            reference=ref(value="staff"),
        ),
        "event",
        tag="coverage_staff",
        reason="explicit_staff_call_cancel",
    )


# ============================================================
# G. MOBILE ORDER
# 15
# ============================================================

MOBILE_CASES = [
    ("모바일 주문은 어떻게 받아요?", "mobile_process"),
    ("맥오더는 어디서 받아요?", "mobile_pickup"),
    ("앱으로 주문했는데 어디로 가면 돼요?", "mobile_pickup"),
    ("모바일 주문 번호는 어디서 확인해요?", "mobile_order_number"),
    ("맥오더 번호 말하면 돼요?", "mobile_order_number"),
    ("앱 주문도 여기서 결제해요?", "mobile_payment"),
    ("모바일 주문 취소는 어떻게 해요?", "mobile_cancel"),
    ("모바일 주문 수정 가능해요?", "mobile_modify"),
    ("맥오더가 안 보여요", "mobile_status"),
    ("앱 주문이 접수됐는지 확인할 수 있어요?", "mobile_status"),
    ("모바일 주문 수령은 여기서 하나요?", "mobile_pickup"),
    ("맥오더 주문번호 다시 확인해주세요", "mobile_order_number"),
    ("사전 주문도 받을 수 있어요?", "mobile_process"),
    ("앱으로 미리 주문할 수 있어요?", "mobile_process"),
    ("모바일 주문이랑 현장 주문 차이가 뭐예요?", "mobile_process"),
]

for text, target in MOBILE_CASES:

    add(
        text,
        act(
            "info_query",
            "mobile_order",
            "question",
            target_domain="mobile_order",
            target=target,
            reference=ref(value=target),
        ),
        tag="coverage_mobile",
        reason="mobile_order_information_query",
    )


# ============================================================
# H. GENERAL CHAT / OUT OF SCOPE
# 20
# ============================================================

GENERAL = [
    "너 로봇이야?",
    "AI가 주문 받는 거예요?",
    "로봇팔이 움직이는 거예요?",
    "이거 신기하네요",
    "말 잘 알아듣네요",
    "고마워요",
    "수고하세요",
    "오늘 사람 많네요",
    "이 시스템 재밌네요",
    "처음 써보는데 신기해요",
]

for text in GENERAL:

    add(
        text,
        act(
            "general_chat",
            "chat",
            "question" if "?" in text else "statement",
            target_domain="system",
        ),
        tag="coverage_general_chat",
        reason="non_mutating_drive_thru_conversation",
    )


OOS = [
    "오늘 날씨 어때요?",
    "축구 누가 이겼어요?",
    "야구 결과 알려주세요",
    "근처 지하철역 어디예요?",
    "학교 추천해주세요",
    "영화 추천해주세요",
    "지금 몇 시예요?",
    "오늘 며칠이에요?",
    "서울 관광지 추천해주세요",
    "수학 문제 풀어주세요",
]

for text in OOS:

    add(
        text,
        act(
            "out_of_scope",
            "unsupported",
            "question" if "?" in text else "request",
        ),
        tag="coverage_out_of_scope",
        reason="outside_drive_thru_scope",
    )


# ============================================================
# COUNT
# ============================================================

if len(cases) != 180:
    raise RuntimeError(
        f"expected 180 cases, got {len(cases)}"
    )


# ============================================================
# WRITE + SELF VALIDATION
# ============================================================

OUT.parent.mkdir(
    parents=True,
    exist_ok=True,
)

seen_ids = set()
seen_inputs = {}

written = 0
skipped = 0

with OUT.open(
    "w",
    encoding="utf-8",
) as f:

    for item in cases:

        if item["id"] in seen_ids:
            raise ValueError(
                f"duplicate id: {item['id']}"
            )

        seen_ids.add(item["id"])

        key = (
            item["utterance"],
            json.dumps(
                item["context"],
                ensure_ascii=False,
                sort_keys=True,
            ),
        )

        answer = json.dumps(
            {
                "expected": item["expected"],
                "expected_policy":
                    item["expected_policy"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )

        if key in seen_inputs:

            if seen_inputs[key] != answer:
                raise ValueError(
                    "CONFLICTING GOLD LABEL: "
                    + item["utterance"]
                )

            print(
                "[SKIP duplicate] "
                + item["id"]
                + " "
                + item["utterance"]
            )

            skipped += 1
            continue

        seen_inputs[key] = answer

        item = canonicalize_case(item)

        parsed = RouterOutput.model_validate(
            item["expected"]
        )

        decisions = evaluate_router_output(
            parsed
        )

        actual = [
            d.status.value
            for d in decisions
        ]

        if actual != item["expected_policy"]:

            raise ValueError(
                f"{item['id']} policy mismatch: "
                f"{actual} != "
                f"{item['expected_policy']}"
            )

        f.write(
            json.dumps(
                item,
                ensure_ascii=False,
            )
            + "\n"
        )

        written += 1


print()
print(
    f"[OK] generated={len(cases)} "
    f"unique_written={written} "
    f"skipped={skipped}"
)

print(f"[OK] {OUT}")


tags = {}

for item in cases:
    tags[item["tag"]] = (
        tags.get(item["tag"], 0)
        + 1
    )


print("\nCATEGORY COUNTS")

for key in sorted(tags):
    print(
        f"{key:32s} {tags[key]:3d}"
    )
