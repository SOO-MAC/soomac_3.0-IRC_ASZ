#!/usr/bin/env python3

import json
import queue
import socket
import re
import select
import sys
import unicodedata
import traceback
import time

from enum import Enum

# 기능별 하위 폴더의 모듈을 기존 이름으로 가져오기
from pathlib import Path
_LLM_ROOT = Path(__file__).resolve().parent
for _folder in ("core", "speech", "ui"):
    sys.path.insert(0, str(_LLM_ROOT / _folder))

from modifier_selection import (
    detect_request as detect_modifier_request,
    resolve_targets as resolve_modifier_targets,
    explicit_new_order as explicit_new_modifier_order,
    normalize_target_followup as normalize_modifier_target_followup,
)
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
    MENU_ALIASES,
    TYPE_ALIASES,
    DRINK_ALIASES,
    SIZE_ALIASES,
    SIDE_ALIASES,
    EXCLUDE_INGREDIENT_ALIASES,
    FINALIZATION_KEYWORDS,
)

from runtime_worker import (
    RuntimeWorker,
    StaleRuntimeRequest,
)

from speech_input_worker import (
    SpeechInputWorker,
)

from ros_stt_udp_input import (
    RosSTTUDPInput,
)

from stt_session_controller import (
    STTSessionController,
)

from router_client import (
    route as route_customer_utterance,
    RouterClientError,
)

from router_policy import (
    evaluate_router_output,
)

from router_schema import RouterOutput

from menu_knowledge import (
    answer_menu_query,
    BURGERS as MENU_KNOWLEDGE_BURGERS,
    DRINKS as MENU_KNOWLEDGE_DRINKS,
    SIDES as MENU_KNOWLEDGE_SIDES,
)

from price_calorie_info_engine import (
    answer_price_calorie_query,
)

from router_runtime_bridge import build_router_runtime_text
from router_fastpath import build_router_fastpath
from router_pre_fastpath import (
    route_speed_pre_fastpath,
    route_pre_fastpath,
    route_explicit_full_set_pre_fastpath,
)

from ui_runtime_bridge import (
    start_customer_ui_server,
    stop_customer_ui_server,
    ui_reset_for_vehicle,
    ui_add_customer_message,
    ui_add_staff_message,
    ui_set_voice_mode,
    ui_set_order_items,
    ui_set_order_meta,
    ui_request_staff_call,
)


def pre_router_allergy_safety_reply(utterance):
    """새우·갑각류는 해당 버거를 제외해 추천하고 별도 안전 확인을 안내한다."""
    compact = re.sub(r"\s+", "", str(utterance or "").lower())
    if not any(token in compact for token in ("알레르기", "알러지", "알러르기")):
        return None

    allergens = (
        ("새우", "새우"),
        ("갑각류", "갑각류"),
        ("게살", "게"),
        ("대게", "게"),
        ("꽃게", "게"),
        ("게", "게"),
        ("조개", "조개류"),
        ("굴", "굴"),
        ("홍합", "홍합"),
        ("전복", "전복"),
        ("랍스터", "갑각류"),
        ("바닷가재", "갑각류"),
        ("우유", "우유"),
        ("땅콩", "땅콩"),
        ("견과", "견과류"),
        ("밀", "밀"),
        ("계란", "계란"),
        ("달걀", "계란"),
        ("대두", "대두"),
        ("콩", "콩"),
        ("참깨", "참깨"),
        ("생선", "생선"),
    )
    mentioned = {
        label for source, label in allergens
        if (
            source in compact if len(source) > 1 else
            any(source + term in compact for term in ("알레르기", "알러지", "알러르기"))
        )
    }
    if mentioned and mentioned <= {"새우", "갑각류", "게"}:
        candidates = [
            MENU_KNOWLEDGE_BURGERS[key]["name"]
            for key in ("bulgogi_burger", "cheese_burger", "chicken_burger")
            if "shrimp_patty" not in MENU_KNOWLEDGE_BURGERS[key]["ingredients"]
        ]
        if candidates:
            return (
                "새우버거를 제외하고 " + ", ".join(candidates) + "를 추천드려요. "
                "주문 전 원재료와 조리 중 교차접촉 여부는 직원에게 확인해주세요."
            )

    matched = next((label for _, label in allergens if label in mentioned), None)
    if matched:
        allergen = matched
        return (
            f"{allergen} 알레르기가 있으시군요. 현재 메뉴별 원재료와 조리 중 "
            "교차접촉 정보를 확인할 수 없어 안전한 메뉴를 추천해 드릴 수 없습니다. "
            "주문 전에 직원에게 원재료와 조리 과정을 확인해 주세요."
        )

    return (
        "현재 메뉴별 알레르기와 조리 중 교차접촉 정보를 확인할 수 없어 안전한 메뉴를 "
        "추천하거나 알레르기 유발 성분이 없다고 안내할 수 없습니다. "
        "주문 전에 직원에게 원재료와 조리 과정을 확인해 주세요."
    )


def pre_router_partial_menu_reply(utterance):
    """Incomplete product names must not be guessed into order additions."""
    value = re.sub(r"[\s!?.,~]+", "", str(utterance or "").lower())
    match = re.fullmatch(
        r"(?:저기|저|혹시)?(?P<word>튀김|치즈|감자|스틱)"
        r"(?P<tail>(?:하나|한개|두개|세개|네개|\d+개?|주세요|줘요|줘|"
        r"주문할게요|주문해주세요|추가해주세요|추가해줘|추가|"
        r"요|이요|으로|로|을|를|은|는|좀|만)*)",
        value,
    )
    if match is None:
        return None
    word = match['word']
    # An explicit cheese addition is a supported topping request, not a
    # shorthand cheese-burger or cheese-stick order.
    if word == '치즈' and '추가' in match['tail']:
        return None
    if word == '치즈':
        return (
            "치즈버거, 치즈스틱, 치즈 토핑 중 어떤 것을 원하시나요? "
            "메뉴 이름이나 토핑 추가 요청을 정확히 말씀해주세요."
        )
    return (
        f"'{word}'만으로는 메뉴를 확정할 수 없어요. "
        "감자튀김이나 치즈스틱처럼 메뉴 이름을 끝까지 말씀해주세요."
    )


def pre_router_store_guidance_reply(utterance):
    """가게 종류와 주문 시작 방법을 묻는 질문에 답한다."""
    compact = re.sub(r"\s+", "", str(utterance or "").lower())
    if any(token in compact for token in (
        "무슨가게", "어떤가게", "무슨매장", "어떤매장",
        "뭐하는가게", "뭐하는곳", "뭐파는곳", "뭐파는가게",
    )):
        return "여기는 햄버거 가게예요."

    if (
        any(token in compact for token in (
            "뭐주문", "뭘주문", "무엇을주문", "어떤걸주문", "어떤거주문",
            "뭐시키", "뭘시키", "뭐시켜", "뭘시켜",
        ))
        and any(token in compact for token in (
            "하면", "할까", "할수", "해야", "가능", "시키면", "시킬까", "시켜야",
        ))
    ):
        return (
            "햄버거, 사이드, 음료를 주문하실 수 있어요. "
            "원하시는 메뉴를 말씀해주세요."
        )
    return None


def _is_burger_ingredient_question(utterance):
    compact = re.sub(r"\s+", "", str(utterance or "").lower())
    asks_about_ingredients = any(
        token in compact
        for token in (
            "속재료",
            "재료",
            "뭐들어",
            "뭐들어가",
            "뭐가들어",
            "뭐가들어가",
            "뭐들었",
            "무엇들어",
            "무엇들어가",
            "무엇이들어",
            "무엇이들어가",
            "구성",
        )
    )
    mentions_burger = any(
        token in compact
        for token in (
            "햄버거",
            "버거",
            "불고기",
            "치킨",
            "치즈",
            "새우",
        )
    )
    return asks_about_ingredients and mentions_burger


def pre_router_unknown_menu_reply(utterance):
    """
    현재 시연 메뉴에 없는 외부/미지원 메뉴를
    Router 호출 전에 closed-world 방식으로 차단한다.

    현재 지원:
      burger:
        불고기버거 / 치킨버거 / 치즈버거 / 새우버거

      side:
        감자튀김 / 치즈스틱

    외부 브랜드 고유 메뉴는 다른 지원 메뉴로
    임의 변환하지 않는다.
    """

    # ORDINAL BURGER MUTATION BYPASS V1
    # "첫 번째 버거 취소해주세요" 같은 reference를
    # 미지원 메뉴명으로 오인하지 않는다.
    _unknown_guard_compact = re.sub(
        r"\s+",
        "",
        str(utterance or "").lower(),
    )

    if (
        any(
            word in _unknown_guard_compact
            for word in (
                "취소",
                "삭제",
                "제거",
                "빼",
                "없애",
                "바꿔",
                "변경",
            )
        )
        and re.search(
            r"(?:첫|두|세|네|다섯|여섯|일곱|여덟|아홉|\d+)"
            r"(?:번째|번)?버거",
            _unknown_guard_compact,
        )
    ):
        return None


    raw = str(
        utterance or ""
    ).strip()

    compact = re.sub(
        r"\s+",
        "",
        raw.lower(),
    )

    if not compact:
        return None

    # --------------------------------------------------------
    # 메뉴 요청/조회 의도가 있어야 한다.
    # 일반 대화를 메뉴 오류로 잡지 않는다.
    # --------------------------------------------------------

    menu_intent = any(
        marker in compact
        for marker in (
            "줘",
            "주세요",
            "주문",
            "추가",
            "먹을래",
            "먹을게",
            "먹고싶",
            "있어",
            "있나요",
            "있어요",
            "파나요",
            "판매",
            "가능해",
            "가능한가",
        )
    )

    if not menu_intent:
        return None

    # ========================================================
    # 1. 외부 / 미지원 SIDE
    #
    # 브랜드 고유 치즈스틱 이름은
    # plain "치즈스틱"보다 먼저 검사해야 한다.
    # ========================================================

    unsupported_sides = (
        (
            "해쉬브라운",
            (
                "해쉬브라운",
                "해시브라운",
            ),
        ),

        (
            "너겟",
            (
                "맥너겟",
                "치킨맥너겟",
                "너겟킹",
                "치킨너겟",
                "양념너겟",
                "너겟",
            ),
        ),

        (
            "어니언링",
            (
                "리얼어니언링",
                "어니언링",
                "양파링",
            ),
        ),

        (
            "치킨텐더",
            (
                "맥스파이시치킨텐더",
                "크리스퍼텐더",
                "치킨텐더",
            ),
        ),

        (
            "바삭킹",
            (
                "바삭킹",
            ),
        ),

        (
            "코코넛슈림프",
            (
                "코코넛슈림프",
            ),
        ),

        (
            "쉐이킹프라이",
            (
                "쉐이킹프라이",
            ),
        ),

        (
            "콘샐러드",
            (
                "콘샐러드",
            ),
        ),

        (
            "코울슬로",
            (
                "코울슬로",
                "콜슬로",
            ),
        ),

        (
            "스낵랩",
            (
                "상하이치킨스낵랩",
                "게살크림크로켓스낵랩",
                "크리스퍼랩",
                "비프킹랩",
                "스낵랩",
            ),
        ),

        (
            "치킨윙",
            (
                "맥윙",
                "화이어윙",
                "치킨윙",
            ),
        ),

        (
            "우리쌀 칩",
            (
                "홍천우리쌀칩",
                "우리쌀칩",
            ),
        ),

        (
            "양념감자",
            (
                "양념감자",
            ),
        ),

        (
            "통오징어링",
            (
                "통오징어링",
                "오징어링",
            ),
        ),

        (
            "지파이",
            (
                "지파이",
            ),
        ),

        (
            "치킨휠레",
            (
                "치킨휠레",
                "치킨필레",
            ),
        ),

        # 브랜드 고유 cheese-stick 이름.
        # plain "치즈스틱"은 아래 supported side로 통과한다.
        (
            "브랜드 치즈스틱",
            (
                "21치즈스틱",
                "골든모짜렐라치즈스틱",
                "롱치즈스틱",
                "내츄럴치즈스틱",
            ),
        ),
    )

    for display_name, aliases in unsupported_sides:

        if any(
            alias in compact
            for alias in aliases
        ):
            return (
                f"{display_name} 메뉴는 현재 제공하지 않습니다. "
                "주문 가능한 사이드는 "
                "감자튀김, 치즈스틱입니다."
            )

    # ========================================================
    # 1-A. 미지원 디저트 / 음료
    #
    # LLM이 아이스크림 -> 아이스커피처럼
    # 지원 메뉴로 임의 치환하는 것을 Router 전에 차단한다.
    # ========================================================

    unsupported_desserts = (
        (
            "아이스크림",
            (
                "아이스크림",
                "소프트아이스크림",
                "소프트콘",
                "아이스콘",
            ),
        ),

        (
            "선데이",
            (
                "선데이",
                "초코선데이",
                "딸기선데이",
                "카라멜선데이",
            ),
        ),

        (
            "맥플러리",
            (
                "맥플러리",
                "오레오맥플러리",
            ),
        ),

        (
            "쉐이크",
            (
                "밀크쉐이크",
                "밀크셰이크",
                "초코쉐이크",
                "초코셰이크",
                "딸기쉐이크",
                "딸기셰이크",
                "바닐라쉐이크",
                "바닐라셰이크",
                "쉐이크",
                "셰이크",
            ),
        ),

        (
            "주스",
            (
                "오렌지주스",
                "사과주스",
                "애플주스",
                "주스",
            ),
        ),

        (
            "에이드",
            (
                "레몬에이드",
                "자몽에이드",
                "청포도에이드",
                "에이드",
            ),
        ),
    )

    for display_name, aliases in unsupported_desserts:

        if any(
            alias in compact
            for alias in aliases
        ):
            return (
                f"{display_name} 메뉴는 현재 제공하지 않습니다. "
                "주문 가능한 음료는 "
                "콜라, 제로콜라, 스프라이트, 환타, "
                "아이스커피입니다."
            )

    # ========================================================
    # 2. 현재 지원 SIDE
    # ========================================================

    supported_sides = (
        "감자튀김",
        "감튀",
        "프렌치프라이",
        "치즈스틱",
    )

    if any(
        name in compact
        for name in supported_sides
    ):
        return None

    # ========================================================
    # 3. 현재 지원 BURGER
    # ========================================================

    supported_burgers = (
        "불고기버거",
        "치킨버거",
        "치즈버거",
        "새우버거",
    )

    if any(
        name in compact
        for name in supported_burgers
    ):
        return None

    # ========================================================
    # 4. 대표 외부 BURGER
    # ========================================================

    unsupported_burgers = (
        "빅맥",
        "와퍼",
        "상하이버거",
        "싸이버거",
        "징거버거",
        "1955버거",
        "슈비버거",
        "슈슈버거",
        "데리버거",
        "한우불고기버거",
        "통새우와퍼",
        "콰트로치즈와퍼",
        "몬스터와퍼",
    )

    for name in unsupported_burgers:

        if name in compact:
            return (
                f"{name} 메뉴는 현재 제공하지 않습니다. "
                "주문 가능한 버거는 "
                "불고기버거, 치킨버거, "
                "치즈버거, 새우버거입니다."
            )

    # ========================================================
    # 5. 등록되지 않은 "...버거"
    # ========================================================

    match = re.search(
        r"([가-힣a-z0-9]{1,24}?버거)",
        compact,
    )

    if match:

        candidate = match.group(1)

        return (
            f"{candidate} 메뉴는 현재 제공하지 않습니다. "
            "주문 가능한 버거는 "
            "불고기버거, 치킨버거, "
            "치즈버거, 새우버거입니다."
        )

    return None



def pre_router_fast_general_recommendation_reply(
    utterance,
    history,
):
    """
    조건 없는 일반 추천만 LLM 없이 즉시 처리한다.

    순환:
      불고기 -> 새우 -> 치킨 -> 치즈 -> 반복

    조건 추천은 기존 Router/menu_knowledge로 fallback:
      칼로리 낮은 거 추천
      치즈 없는 거 추천
      만원 안으로 추천
    """

    compact = re.sub(
        r"\s+",
        "",
        str(utterance or "").lower(),
    )

    generic_requests = {
        "추천해줘",
        "추천해주세요",
        "추천해줘요",
        "추천좀",
        "추천부탁해",
        "추천부탁해요",

        "메뉴추천해줘",
        "메뉴추천해주세요",

        "버거추천해줘",
        "버거추천해주세요",

        "뭐가좋아",
        "뭐가좋아요",
        "뭐먹지",
        "뭐먹을까",

        "다른거추천해줘",
        "다른거추천해주세요",
        "다른메뉴추천해줘",
        "다른메뉴추천해주세요",
        "다른걸로추천해줘",
        "다른것도추천해줘",

        "또다른거추천해줘",
        "또다른메뉴추천해줘",

        "다른거",
        "또다른거",
    }

    if compact not in generic_requests:
        return None

    order = (
        "bulgogi_burger",
        "shrimp_burger",
        "chicken_burger",
        "cheese_burger",
    )

    labels = {
        "bulgogi_burger":
            "불고기버거",
        "shrimp_burger":
            "새우버거",
        "chicken_burger":
            "치킨버거",
        "cheese_burger":
            "치즈버거",
    }

    replies = {
        "bulgogi_burger": (
            "불고기버거 추천드릴게요. "
            "단품 4,500원이고 가장 무난하게 "
            "먹기 좋은 메뉴예요."
        ),

        "shrimp_burger": (
            "새우버거 추천드릴게요. "
            "단품 5,200원이고 새우 패티를 "
            "좋아하시면 괜찮은 선택이에요."
        ),

        "chicken_burger": (
            "치킨버거 추천드릴게요. "
            "단품 4,800원이고 치킨 패티를 "
            "좋아하시면 추천드려요."
        ),

        "cheese_burger": (
            "치즈버거 추천드릴게요. "
            "단품 5,000원이고 치즈 맛을 "
            "원하시면 잘 맞아요."
        ),
    }

    seen = []

    for item in history or []:

        if not isinstance(item, dict):
            continue

        if item.get("role") != "staff":
            continue

        staff_text = str(
            item.get("text")
            or item.get("content")
            or ""
        )

        if "추천" not in staff_text:
            continue

        for key in order:

            if (
                labels[key] in staff_text
                and key not in seen
            ):
                seen.append(key)

    # "다른 거"만 단독으로 말했는데
    # 이전 추천 문맥이 없으면 기존 Router로.
    if (
        compact in {
            "다른거",
            "또다른거",
        }
        and not seen
    ):
        return None

    # 아직 안 보여준 메뉴 우선.
    remaining = [
        key
        for key in order
        if key not in seen
    ]

    if remaining:
        choice = remaining[0]

    elif seen:

        last = seen[-1]

        choice = order[
            (
                order.index(last)
                + 1
            )
            % len(order)
        ]

    else:
        choice = order[0]

    return replies[choice]


def pre_router_unsupported_option_reply(utterance):
    """
    Router 오판과 무관하게 명백한 미지원 옵션 요청을
    Router 호출 전에 차단한다.

    현재 시연에서 추가 가능한 burger topping:
    - cheese
    - bacon
    - tomato

    패티 추가는 지원하지 않는다.
    """

    compact = re.sub(
        r"\s+",
        "",
        str(utterance or "").lower(),
    )

    if not compact:
        return None

    if "패티" not in compact:
        return None

    option_context = any(
        word in compact
        for word in (
            "추가",
            "토핑",
            "넣어",
            "넣을",
            "넣는",
            "더넣",
            "올려",
            "얹어",
            "가능",
        )
    )

    if not option_context:
        return None

    return (
        "패티 추가는 현재 지원하지 않습니다. "
        "추가 가능한 토핑은 치즈, 베이컨, 패티입니다."
    )


def _explicit_non_burger_product_in_utterance(
    utterance,
):
    """
    topping 추가 발화에서 음료/사이드 상품이
    명시적으로 대상이 된 경우 True.

    이 경우 generic 'burger 없음' guard가 아니라
    기존 unsupported_topping_mutation_reply가 처리한다.
    """

    compact = re.sub(
        r"\s+",
        "",
        str(utterance or "").lower(),
    )

    words = (
        # drink
        "콜라",
        "제로콜라",
        "코카콜라",
        "사이다",
        "스프라이트",
        "환타",
        "판타",
        "아이스커피",
        "아메리카노",

        # side
        "감자튀김",
        "감튀",
        "프렌치프라이",
        "후렌치후라이",
        "치즈스틱",
    )

    return any(
        word in compact
        for word in words
    )


def pre_router_empty_burger_topping_reply(
    utterance,
    state,
):
    """
    현재 주문에 burger가 하나도 없는데
    지원 topping만 단독으로 추가하려는 발화는
    Router 호출 전에 차단한다.

    신규 burger + topping 주문은 막지 않는다.
    """

    compact = re.sub(
        r"\s+",
        "",
        str(utterance or "").lower(),
    )

    if not compact:
        return None

    # 치즈스틱 상품 추가를 cheese topping으로 오인하지 않는다.
    if "치즈스틱" in compact:
        return None

    # 현재 발화에 burger가 직접 명시되어 있으면
    # 기존 Router / missing-burger guard에 맡긴다.
    if (
        _explicit_burger_from_utterance(
            utterance
        )
        is not None
    ):
        return None

    # 콜라/치즈스틱 등 non-burger 상품이
    # 명시된 경우에는 generic burger 없음 guard가
    # 가로채지 않는다.
    if _explicit_non_burger_product_in_utterance(
        utterance
    ):
        return None

    topping_present = any(
        word in compact
        for word in (
            "치즈",
            "베이컨",
            "패티",
        )
    )

    if not topping_present:
        return None

    add_action = any(
        word in compact
        for word in (
            "추가",
            "넣어",
            "넣을",
            "더넣",
            "올려",
            "얹어",
        )
    )

    if not add_action:
        return None

    items = (
        state.get("items", [])
        if isinstance(state, dict)
        else []
    ) or []

    has_burger = any(
        item.get("item_type") == "burger"
        for item in items
    )

    if has_burger:
        return None

    return (
        "현재 주문에 토핑을 추가할 버거가 없습니다. "
        "먼저 버거를 주문해주세요."
    )


# ============================================================
# CONFIG
# ============================================================

WIDTH = 68

TTS_UDP_HOST = "127.0.0.1"
TTS_UDP_PORT = 5007

_tts_udp_socket = socket.socket(
    socket.AF_INET,
    socket.SOCK_DGRAM,
)


# ============================================================
# ROUTER CONVERSATION HISTORY
# ============================================================

_router_history = []
_ROUTER_HISTORY_MAX = 24


def reset_router_history():
    _router_history.clear()
    reset_modifier_context()


def append_router_history(role, text):
    value = str(text or "").strip()

    if not value:
        return

    _router_history.append(
        {
            "role": role,
            "text": value,
        }
    )

    if len(_router_history) > _ROUTER_HISTORY_MAX:
        del _router_history[
            :len(_router_history) - _ROUTER_HISTORY_MAX
        ]


def router_history_before_current_customer():
    history = [
        dict(item)
        for item in _router_history
    ]

    if (
        history
        and history[-1].get("role") == "customer"
    ):
        history.pop()

    return history



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
    "lettuce": "양상추",
}

TOPPING_LABELS = {
    "cheese": "치즈",
    "bacon": "베이컨",
    "patty": "패티",
}




# ============================================================
# UI
# ============================================================

def line(char="="):
    print(char * WIDTH)


def show_menu_board():

    print()
    print("[ 메뉴판 ]")
    line("-")

    # --------------------------------------------------------
    # BURGER
    # --------------------------------------------------------

    print("BURGER")

    for key in (
        "bulgogi_burger",
        "chicken_burger",
        "cheese_burger",
        "shrimp_burger",
    ):
        name = MENU_LABELS[key]
        price = BURGER_BASE_PRICE[key]
        kcal = (
            MENU_KNOWLEDGE_BURGERS[
                key
            ]["calories"]
        )

        print(
            f"  {name:<8} "
            f"{price:>5,}원  "
            f"{kcal:>3} kcal"
        )

    print()
    print(
        f"  세트 변경       "
        f"+{SET_UPCHARGE:,}원"
    )

    print(
        "  ※ 미디엄 +300원 / "
        "라지 +700원 / "
        "치즈스틱 +500원 / "
        "아이스커피 +500원"
    )

    # --------------------------------------------------------
    # DRINK
    # --------------------------------------------------------

    print()
    print("DRINK")
    print(
        "  메뉴            "
        "스몰         미디엄       라지"
    )

    for key in (
        "coke",
        "zero_coke",
        "sprite",
        "fanta",
        "iced_coffee",
    ):

        name = DRINK_LABELS[key]

        small_price = (
            STANDALONE_DRINK_PRICE[key]
            + DRINK_SIZE_UPCHARGE[
                "small"
            ]
        )

        medium_price = (
            STANDALONE_DRINK_PRICE[key]
            + DRINK_SIZE_UPCHARGE[
                "medium"
            ]
        )

        large_price = (
            STANDALONE_DRINK_PRICE[key]
            + DRINK_SIZE_UPCHARGE[
                "large"
            ]
        )

        calories = (
            MENU_KNOWLEDGE_DRINKS[
                key
            ]["calories"]
        )

        print(
            f"  {name:<8} "
            f"{small_price:>4,}원/"
            f"{calories['small']:>3}  "
            f"{medium_price:>4,}원/"
            f"{calories['medium']:>3}  "
            f"{large_price:>4,}원/"
            f"{calories['large']:>3} kcal"
        )

    # --------------------------------------------------------
    # SIDE
    # --------------------------------------------------------

    print()
    print("SIDE")

    for key in (
        "french_fries",
        "cheese_stick",
    ):

        name = SIDE_LABELS[key]

        price = (
            STANDALONE_SIDE_PRICE[
                key
            ]
        )

        kcal = (
            MENU_KNOWLEDGE_SIDES[
                key
            ]["calories"]
        )

        print(
            f"  {name:<8} "
            f"{price:>5,}원  "
            f"{kcal:>3} kcal"
        )

    # --------------------------------------------------------
    # TOPPING
    # --------------------------------------------------------

    print()
    print("TOPPING")

    print(
        f"  치즈 +{TOPPING_PRICE['cheese']:,}원 / "
        f"베이컨 +{TOPPING_PRICE['bacon']:,}원 / "
        f"패티 +{TOPPING_PRICE['patty']:,}원"
    )

    line("-")


def header():
    print()
    line("=")
    print("SOOMAC DRIVE-THRU V14".center(WIDTH))
    line("=")

    show_menu_board()

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
    고객에게 전달해야 하는 STAFF 응답.

    1. Customer UI에 표시
    2. TTS ROS2 bridge로 전송
    """
    # REPEAT PREFIX SANITIZER
    text = re.sub(
        r"^(?:다시 말씀드리겠습니다\.\s*){2,}",
        "다시 말씀드리겠습니다. ",
        str(text or ""),
    )


    # KOREAN TTS UNIT NORMALIZER
    text = re.sub(
        r"(?i)kcal",
        "칼로리",
        str(text or ""),
    )

    text = str(text or "").strip()

    if not text:
        return

    append_router_history(
        "staff",
        text,
    )

    # --------------------------------------------------------
    # CUSTOMER UI
    # --------------------------------------------------------

    ui_add_staff_message(
        text
    )

    # --------------------------------------------------------
    # TTS
    #
    # app.py -> UDP 5007 -> tts_text_publisher.py
    #        -> ROS2 /tts/text
    # --------------------------------------------------------

    try:

        _tts_udp_socket.sendto(
            text.encode("utf-8"),
            (
                TTS_UDP_HOST,
                TTS_UDP_PORT,
            ),
        )

    except OSError as e:

        print(
            f"[TTS UDP ERROR] {e}"
        )

    # TTS 완료 callback은 아직 없으므로
    # 현재는 STAFF 응답 생성 후 다시 LISTENING 상태로 복귀
    ui_set_voice_mode(
        "listening"
    )

    print()
    print(
        f"STAFF > {text}"
    )


def show_idle():
    print()
    print("● 차량 진입 대기 중")


def show_waiting_for_exit():
    print()
    print("● 현재 차량 이동 대기 중")


# ============================================================
# INPUT
# ============================================================

def get_customer_input(
    speech_worker,
):
    """
    실제 고객 입력은 SpeechInputWorker에서 받는다.

    개발 중에는 /carout, /reset 같은 명령을
    터미널에서도 입력할 수 있게 stdin을 함께 확인한다.
    """

    while True:

        # 개발자 키보드 명령
        try:
            readable, _, _ = select.select(
                [sys.stdin],
                [],
                [],
                0,
            )
        except (ValueError, OSError):
            readable = []

        if readable:
            line = sys.stdin.readline()

            if line == "":
                raise EOFError

            text = line.strip()

            if text:
                return text

        # STT event
        try:
            event = speech_worker.get(
                timeout=0.1
            )

        except queue.Empty:
            continue

        try:
            if event.kind == "utterance":

                customer_text = (
                    event.text
                    or ""
                )

                print(
                    f"\n[STT] {customer_text}"
                )

                return customer_text

            if event.kind == "stt_reject":

                if event.reply:
                    soomac_say(
                        event.reply
                    )

                continue

        finally:
            speech_worker.task_done()


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



def extract_mobile_pickup_number(text):
    """
    맥오더/모바일 픽업 주문번호만 추출한다.
    일반 주문 의미 판단에는 사용하지 않는다.
    """

    value = str(text or "").strip()

    has_mobile_word = bool(
        re.search(
            r"(맥\s*오더|모바일\s*주문|앱\s*주문|픽업)",
            value,
            re.IGNORECASE,
        )
    )

    if not has_mobile_word:
        return None

    match = re.search(
        r"(\d{1,6})\s*번?",
        value,
    )

    if match is None:
        return None

    return int(match.group(1))


def normalize_command(text):
    text = unicodedata.normalize(
        "NFKC",
        str(text or ""),
    )

    # 터미널/복붙/STT 입력에서 들어올 수 있는
    # slash 유사 문자를 ASCII "/"로 통일한다.
    for slash_like in (
        "／",  # FULLWIDTH SOLIDUS
        "∕",   # DIVISION SLASH
        "⁄",   # FRACTION SLASH
        "⧸",   # BIG SOLIDUS
        "╱",   # BOX DRAWINGS DIAGONAL
    ):
        text = text.replace(
            slash_like,
            "/",
        )

    # zero-width / 제어문자 제거
    text = "".join(
        ch
        for ch in text
        if not unicodedata.category(ch).startswith("C")
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

    def __init__(
        self,
        runtime_worker,
        stt_session=None,
    ):
        self.runtime_worker = runtime_worker
        self.stt_session = stt_session

        self.state = AppState.IDLE
        self.vehicle_present = False

    def _start_stt(self):
        if self.stt_session is not None:
            self.stt_session.start()

    def _stop_stt(self):
        if self.stt_session is not None:
            self.stt_session.stop()

    def reset_current_order(self):
        """
        같은 차량에서 주문만 초기화한다.

        이전 STT queue/buffer를 먼저 폐기하고,
        Runtime reset 완료 후 다시 수음을 시작한다.
        """

        reset_modifier_context()
        self._stop_stt()

        self.runtime_worker.invalidate_and_reset(
            wait=True
        )

        if self.state == AppState.ORDERING:
            self._start_stt()

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

        # 새로운 차량은 이전 고객의 Router 문맥을 절대 상속하지 않는다.
        reset_router_history()

        # 새로운 차량은 IDLE일 때만 받음
        if self.state != AppState.IDLE:
            return

        # 이전 고객 STT 결과와 오디오를 먼저 폐기한다.
        self._stop_stt()

        # 새 고객 generation으로 완전히 전환한다.
        self.runtime_worker.invalidate_and_reset(
            wait=True
        )

        # 고객 화면도 새 주문 세션으로 초기화한다.
        ui_reset_for_vehicle()

        self.state = AppState.ORDERING

        system_message(
            "차량 감지"
        )

        soomac_say(
            "안녕하세요. 주문을 말씀해주세요."
        )

        # 새 고객 세션에서만 STT 허용
        self._start_stt()

    # --------------------------------------------------------
    # VEHICLE EXIT
    # --------------------------------------------------------

    def vehicle_exit(self):

        ui_set_voice_mode(
            "standby"
        )

        # 차량 이탈 순간부터 추가 STT를 받지 않는다.
        self._stop_stt()

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

        reset_router_history()

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

        # 주문 완료 이후의 음성은 다음 주문에 섞이면 안 된다.
        self._stop_stt()

        self.runtime_worker.invalidate_and_reset(wait=True)

        reset_modifier_context()
        self.state = AppState.WAITING_FOR_EXIT

        ui_set_voice_mode(
            "complete"
        )

        show_waiting_for_exit()

    # --------------------------------------------------------
    # DEBUG RESET
    # --------------------------------------------------------

    def force_reset(self):

        reset_modifier_context()
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
            f"{item['line_id']}. "
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
# PENDING CLOSED-WORLD ROUTER LANE
# ============================================================
#
# 주문 옵션을 이미 질문한 상태에서는:
#
#   pending=drink      + "콜라요"
#   pending=drink_size + "라지로 주세요"
#   pending=side       + "감자튀김이요"
#
# 같은 답을 일반 신규 주문으로 해석하면 안 된다.
#
# 이 경우에만 Router V14 자유 분류를 생략하고
# 기존 Runtime pending resolver에 원문을 전달한다.
#
# 반면:
#   "콜라는 얼마예요?"      -> read-only Router
#   "치즈스틱 하나 추가해줘" -> 실제 신규 주문
# 은 기존 Router 경로를 그대로 사용한다.
# ============================================================

_ROUTE_CUSTOMER_UTTERANCE_V14 = (
    route_customer_utterance
)

_BUILD_ROUTER_RUNTIME_TEXT_BASE = (
    build_router_runtime_text
)


def _pending_alias_map(field):

    maps = {
        "type": TYPE_ALIASES,
        "drink": DRINK_ALIASES,
        "drink_size": SIZE_ALIASES,
        "side": SIDE_ALIASES,
    }

    return maps.get(
        field
    )


def _pending_compact(text):

    return re.sub(
        r"[\s!?.,~]+",
        "",
        str(
            text
            or ""
        ).lower(),
    )


def is_closed_world_pending_answer(
    text,
    pending,
):
    """
    현재 pending 질문에 대한 명시적 옵션 답변인지 판별.

    True인 경우에만 Router V14의 자유 semantic 분류보다
    현재 state-machine의 pending 의미를 우선한다.
    """

    if (
        not isinstance(
            pending,
            (tuple, list),
        )
        or len(pending) < 2
    ):
        return False

    field = pending[1]

    alias_map = (
        _pending_alias_map(
            field
        )
    )

    if not alias_map:
        return False

    compact = _pending_compact(
        text
    )

    if not compact:
        return False

    # --------------------------------------------------------
    # 정보 질문/가능 여부 질문은 절대 pending mutation으로
    # 먹지 않는다.
    #
    # T040:
    #   pending=drink
    #   "콜라는 얼마예요?"
    #   -> Router read_only 유지
    # --------------------------------------------------------

    readonly_markers = (
        "얼마",
        "가격",
        "칼로리",
        "영양",
        "알레르기",
        "추천",
        "가능",
        "있어요",
        "있나요",
        "파나요",
        "뭐예요",
        "뭔가요",
        "무엇",
        "어떤",
    )

    if any(
        marker in compact
        for marker in readonly_markers
    ):
        return False

    # --------------------------------------------------------
    # 명백한 추가 주문은 pending 답변으로 삼키지 않는다.
    #
    # 예:
    #   "치즈스틱 하나 추가해줘"
    #   "콜라 하나 더"
    # --------------------------------------------------------

    new_item_markers = (
        "하나추가",
        "한개추가",
        "두개추가",
        "세개추가",
        "네개추가",
        "하나더",
        "한개더",
        "두개더",
        "세개더",
        "네개더",
    )

    if any(
        marker in compact
        for marker in new_item_markers
    ):
        return False

    # --------------------------------------------------------
    # 현재 pending field에 실제 허용 alias가 하나라도
    # 발화에 있어야 한다.
    # --------------------------------------------------------

    for aliases in alias_map.values():

        if isinstance(
            aliases,
            str,
        ):
            aliases = (
                aliases,
            )

        for alias in aliases:

            alias_compact = (
                _pending_compact(
                    alias
                )
            )

            if (
                alias_compact
                and alias_compact
                in compact
            ):
                return True

    return False


def _pending_closed_world_output(
    pending,
):
    """
    Policy에는 execute_order로 보이되,
    Fast Path가 임의로 신규 상품을 만들 수 없도록
    line_ids를 일부러 확정하지 않는다.

    Runtime이 실제 pending state를 기준으로 처리한다.
    """

    return RouterOutput.model_validate({
        "acts": [
            {
                "family":
                    "order_action",

                "subtype":
                    "modify",

                "speech_act":
                    "request",

                "commitment":
                    "explicit",

                "target":
                    "__pending_closed_world__",

                "reference": {
                    "source":
                        "pending",

                    "resolved":
                        True,

                    "line_ids":
                        [],
                },

                "resolution":
                    "context_resolved",
            }
        ]
    })


def route_customer_utterance(
    utterance,
    *,
    history=None,
    pending=None,
    order_state=None,
    timeout=60.0,
):

    if is_closed_world_pending_answer(
        utterance,
        pending,
    ):

        return (
            _pending_closed_world_output(
                pending
            )
        )

    return (
        _ROUTE_CUSTOMER_UTTERANCE_V14(
            utterance,
            history=history,
            pending=pending,
            order_state=order_state,
            timeout=timeout,
        )
    )


def build_router_runtime_text(
    output,
    utterance,
):
    """
    synthetic pending route는 canonical text를 만들지 않고
    고객 원문을 그대로 Runtime에 전달한다.
    """

    for act in (
        getattr(
            output,
            "acts",
            [],
        )
        or []
    ):

        if (
            getattr(
                act,
                "target",
                None,
            )
            == "__pending_closed_world__"
        ):
            return utterance

    return (
        _BUILD_ROUTER_RUNTIME_TEXT_BASE(
            output,
            utterance,
        )
    )




# ============================================================
# STATE / REFERENCE SEMANTIC REPAIR V1
# ============================================================
#
# 이 레이어는 특정 문장을 정답으로 외우지 않는다.
#
# 1. "N개만 남겨"       -> 동일 품목 묶음 목표 수량
# 2. "하나 더"          -> 현재 품목 옵션까지 그대로 반복
# 3. 명시적 신규 버거인데 Router가 burger act를 잃음
#                        -> burger 의미만 복구 후 Runtime
# 4. 동일 품목 묶음에서 "그중 N개만 세트/단품"
#                        -> subset reference 복구
# ============================================================


_ROUTE_CUSTOMER_UTTERANCE_STATE_REPAIR_BASE = (
    route_customer_utterance
)

_BUILD_ROUTER_FASTPATH_STATE_REPAIR_BASE = (
    build_router_fastpath
)


_KOREAN_COUNT = {
    "한": 1,
    "하나": 1,
    "한개": 1,
    "두": 2,
    "둘": 2,
    "두개": 2,
    "세": 3,
    "셋": 3,
    "세개": 3,
    "네": 4,
    "넷": 4,
    "네개": 4,
    "다섯": 5,
    "다섯개": 5,
}


def _state_repair_compact(text):

    return re.sub(
        r"\s+",
        "",
        str(
            text
            or ""
        ).lower(),
    )


def _state_repair_count(token):

    token = (
        str(token)
        .replace(
            " ",
            "",
        )
    )

    if token.endswith("개"):
        token = token[:-1]

    if token.isdigit():
        return int(token)

    return _KOREAN_COUNT.get(
        token
    )


def _state_repair_item_signature(item):
    """
    line_id / quantity를 제외한 실제 상품 구성이 같은지 확인.
    """

    return (
        item.get("item_type"),
        item.get("menu"),
        item.get("type"),
        item.get("drink"),
        item.get("drink_size"),
        item.get("side"),
        tuple(
            sorted(
                item.get(
                    "exclude",
                    [],
                )
                or []
            )
        ),
        tuple(
            sorted(
                item.get(
                    "add_toppings",
                    [],
                )
                or []
            )
        ),
    )


def _state_repair_homogeneous_items(
    order_state,
):
    """
    현재 주문 전체가 동일 구성 품목 묶음일 때만 반환한다.

    애매한 여러 메뉴가 있을 때 임의 reference를 만들지 않는다.
    """

    items = list(
        (
            order_state
            or {}
        ).get(
            "items",
            [],
        )
        or []
    )

    if not items:
        return []

    signatures = {
        _state_repair_item_signature(
            item
        )
        for item in items
    }

    if len(signatures) != 1:
        return []

    return sorted(
        items,
        key=lambda x:
            int(
                x.get(
                    "line_id",
                    0,
                )
            ),
    )


def _state_repair_target(
    item,
):
    kind = item.get(
        "item_type"
    )

    if kind == "burger":
        return (
            "burger",
            item.get(
                "menu"
            ),
        )

    if kind == "drink":
        return (
            "drink",
            item.get(
                "drink"
            ),
        )

    if kind == "side":
        return (
            "side",
            item.get(
                "side"
            ),
        )

    return (
        None,
        None,
    )


def _repair_keep_quantity_router(
    utterance,
    order_state,
):
    """
    예:
        동일 감자튀김 3개 존재
        "두 개만 남겨주세요"

    -> 제거할 개수와 line reference가 state상 확정 가능할 때만
       remove act를 deterministic하게 복구.

    여러 종류의 주문이 섞여 있으면 개입하지 않는다.
    """

    compact = (
        _state_repair_compact(
            utterance
        )
    )

    match = re.search(
        r"(?P<count>"
        r"\d+|"
        r"한|하나|한개|"
        r"두|둘|두개|"
        r"세|셋|세개|"
        r"네|넷|네개|"
        r"다섯|다섯개"
        r")"
        r"(?:개)?"
        r"만"
        r"(?:남겨|남기|남길)",
        compact,
    )

    if not match:
        return None

    wanted = (
        _state_repair_count(
            match.group(
                "count"
            )
        )
    )

    if wanted is None:
        return None

    items = (
        _state_repair_homogeneous_items(
            order_state
        )
    )

    if not items:
        return None

    current = len(
        items
    )

    if (
        wanted < 0
        or wanted >= current
    ):
        return None

    remove_count = (
        current
        - wanted
    )

    domain, target = (
        _state_repair_target(
            items[0]
        )
    )

    if (
        domain is None
        or target is None
    ):
        return None

    remove_items = (
        items[
            -remove_count:
        ]
    )

    line_ids = [
        item["line_id"]
        for item
        in remove_items
    ]

    return RouterOutput.model_validate({
        "acts": [
            {
                "family":
                    "order_action",

                "subtype":
                    "remove",

                "speech_act":
                    "request",

                "commitment":
                    "explicit",

                "target_domain":
                    domain,

                "target":
                    target,

                "quantity":
                    remove_count,

                "reference": {
                    "source":
                        "current_order",

                    "resolved":
                        True,

                    "value":
                        "__keep_quantity__",

                    "line_ids":
                        line_ids,
                },

                "resolution":
                    "context_resolved",
            }
        ]
    })


def _repair_subset_type_router(
    utterance,
    order_state,
):
    """
    동일한 burger 묶음에서만:

        "그중 하나만 세트로 바꿔"
        "그중 두 개만 단품으로 변경"

    reference를 deterministic하게 정한다.

    동일한 상품들이므로 어느 line을 선택해도
    고객 의미상 동등하다.
    """

    compact = (
        _state_repair_compact(
            utterance
        )
    )

    if "그중" not in compact:
        return None

    match = re.search(
        r"그중"
        r"(?P<count>"
        r"\d+|"
        r"한|하나|한개|"
        r"두|둘|두개|"
        r"세|셋|세개|"
        r"네|넷|네개"
        r")"
        r"(?:개)?"
        r"만",
        compact,
    )

    if not match:
        return None

    count = (
        _state_repair_count(
            match.group(
                "count"
            )
        )
    )

    if not count:
        return None

    if "세트" in compact:
        wanted_type = "set"

    elif "단품" in compact:
        wanted_type = "single"

    else:
        return None

    if not any(
        keyword in compact
        for keyword in (
            "바꿔",
            "변경",
            "바꾸",
            "해줘",
            "해주세요",
        )
    ):
        return None

    items = (
        _state_repair_homogeneous_items(
            order_state
        )
    )

    if not items:
        return None

    if any(
        item.get(
            "item_type"
        )
        != "burger"
        for item
        in items
    ):
        return None

    if count > len(items):
        return None

    if count > 4:
        return None

    chosen = items[
        :count
    ]

    acts = []

    for item in chosen:

        if (
            item.get(
                "type"
            )
            == wanted_type
        ):
            continue

        acts.append({
            "family":
                "order_action",

            "subtype":
                "modify",

            "speech_act":
                "correction",

            "commitment":
                "explicit",

            "target_domain":
                "burger",

            "target":
                wanted_type,

            "reference": {
                "source":
                    "current_order",

                "resolved":
                    True,

                "value":
                    wanted_type,

                "line_ids": [
                    item[
                        "line_id"
                    ]
                ],
            },

            "resolution":
                "context_resolved",
        })

    if not acts:
        return None

    return RouterOutput.model_validate({
        "acts":
            acts
    })


def _explicit_full_burger_menu(
    utterance,
):
    """
    짧은 alias가 아니라 실제 '...버거' 메뉴명을
    발화에 직접 말했을 때만 사용한다.
    """

    compact = (
        _state_repair_compact(
            utterance
        )
    )

    found = set()

    for canonical, aliases in (
        MENU_ALIASES.items()
    ):

        for alias in aliases:

            alias_text = (
                _state_repair_compact(
                    alias
                )
            )

            if (
                "버거"
                not in alias_text
            ):
                continue

            if (
                alias_text
                and alias_text
                in compact
            ):
                found.add(
                    canonical
                )
                break

    if len(found) != 1:
        return None

    return next(
        iter(
            found
        )
    )


def _repair_missed_explicit_burger(
    utterance,
    order_state,
    output,
):
    """
    명백한 신규 burger 주문인데 Router가 burger act를
    통째로 잃어버린 경우에만 burger 의미를 복구한다.

    세부 drink/side/options는 여기서 생성하지 않는다.
    이후 Runtime에 원문 전체를 넘긴다.
    """

    detector = globals().get(
        "explicit_new_burger_order_request"
    )

    if detector is None:
        return output

    try:

        is_new_order = detector(
            utterance,
            order_state,
        )

    except Exception:
        return output

    if not is_new_order:
        return output

    menu = (
        _explicit_full_burger_menu(
            utterance
        )
    )

    if menu is None:
        return output

    acts = list(
        getattr(
            output,
            "acts",
            [],
        )
        or []
    )

    # Router가 이미 burger ADD를 잡았다면 건드리지 않는다.
    for act in acts:

        family = _router_value(
            getattr(
                act,
                "family",
                None,
            )
        )

        subtype = _router_value(
            getattr(
                act,
                "subtype",
                None,
            )
        )

        domain = _router_value(
            getattr(
                act,
                "target_domain",
                None,
            )
        )

        if (
            family == "order_action"
            and subtype == "add"
            and domain == "burger"
        ):
            return output

    # 수량은 명시값이 있으면 사용하되,
    # 못 찾으면 신규 주문 기본 1개.
    compact = (
        _state_repair_compact(
            utterance
        )
    )

    quantity = 1

    quantity_match = re.search(
        r"(?P<count>"
        r"\d+개?|"
        r"하나|한개|"
        r"두개|둘|"
        r"세개|셋|"
        r"네개|넷"
        r")",
        compact,
    )

    if quantity_match:

        parsed = (
            _state_repair_count(
                quantity_match.group(
                    "count"
                )
            )
        )

        if parsed:
            quantity = parsed

    # reference.value marker는 실제 주문값이 아니다.
    # 아래 fastpath wrapper가 이 marker를 보고
    # 두 번째 Runtime V14로 보내기 위한 내부 표식.
    return RouterOutput.model_validate({
        "acts": [
            {
                "family":
                    "order_action",

                "subtype":
                    "add",

                "speech_act":
                    "request",

                "commitment":
                    "explicit",

                "target_domain":
                    "burger",

                "target":
                    menu,

                "quantity":
                    quantity,

                "reference": {
                    "source":
                        "explicit",

                    "resolved":
                        False,

                    "value":
                        "__router_repaired_new_burger__",

                    "line_ids":
                        [],
                },

                "resolution":
                    "clear",
            }
        ]
    })



def _state_repair_alias_entries(
    mapping,
):
    entries = []

    for canonical, aliases in (
        mapping.items()
    ):

        if isinstance(
            aliases,
            str,
        ):
            aliases = (
                aliases,
            )

        for alias in aliases:

            value = (
                _state_repair_compact(
                    alias
                )
            )

            if value:

                entries.append(
                    (
                        value,
                        canonical,
                    )
                )

    return sorted(
        entries,
        key=lambda x:
            len(x[0]),
        reverse=True,
    )


def _state_repair_relation_selector(
    compact,
):
    """
    "... 들어간 세트"
    "... 있는 세트"

    같은 attribute reference를 찾는다.
    """

    candidates = []

    for field, mapping in (
        (
            "drink",
            DRINK_ALIASES,
        ),
        (
            "side",
            SIDE_ALIASES,
        ),
    ):

        for alias, canonical in (
            _state_repair_alias_entries(
                mapping
            )
        ):

            for relation in (
                "들어간",
                "들어있는",
                "있는",
                "포함된",
            ):

                needle = (
                    alias
                    + relation
                )

                pos = compact.find(
                    needle
                )

                if pos != -1:

                    candidates.append(
                        (
                            len(alias),
                            pos,
                            field,
                            canonical,
                        )
                    )

    if not candidates:
        return None

    # "제로콜라"와 "콜라"처럼 alias가 겹치면
    # 긴 alias 우선.
    candidates.sort(
        key=lambda x:
            (
                x[0],
                -x[1],
            ),
        reverse=True,
    )

    _, _, field, value = (
        candidates[0]
    )

    return (
        field,
        value,
    )


def _state_repair_change_target(
    compact,
):
    """
    "...을 X로 바꿔"
    "...를 X으로 변경"

    에서 새 값을 찾는다.
    """

    mappings = (
        (
            "drink",
            DRINK_ALIASES,
        ),
        (
            "side",
            SIDE_ALIASES,
        ),
        (
            "drink",
            SIZE_ALIASES,
        ),
        (
            "burger",
            TYPE_ALIASES,
        ),
    )

    matches = []

    for domain, mapping in mappings:

        for alias, canonical in (
            _state_repair_alias_entries(
                mapping
            )
        ):

            for m in re.finditer(
                re.escape(
                    alias
                ),
                compact,
            ):

                suffix = compact[
                    m.end():
                ]

                if re.match(
                    r"(?:으로|로)"
                    r"(?:바꿔|바꾸|변경|교체)",
                    suffix,
                ):

                    matches.append(
                        (
                            m.start(),
                            len(alias),
                            domain,
                            canonical,
                        )
                    )

    if not matches:
        return None

    # 문장 뒤쪽의 변경 대상 우선.
    matches.sort(
        key=lambda x:
            (
                x[0],
                x[1],
            ),
        reverse=True,
    )

    _, _, domain, value = (
        matches[0]
    )

    return (
        domain,
        value,
    )


def _repair_attribute_reference_modify(
    utterance,
    order_state,
):
    """
    예:
        콜라 들어간 세트의
        감자튀김을 치즈스틱으로 바꿔주세요.

    selector와 변경 대상이 모두 명시되고
    현재 state에서 정확히 한 line만 선택될 때만 복구한다.
    """

    compact = (
        _state_repair_compact(
            utterance
        )
    )

    if not any(
        word in compact
        for word in (
            "바꿔",
            "바꾸",
            "변경",
            "교체",
        )
    ):
        return None

    selector = (
        _state_repair_relation_selector(
            compact
        )
    )

    replacement = (
        _state_repair_change_target(
            compact
        )
    )

    if (
        selector is None
        or replacement is None
    ):
        return None

    selector_field, selector_value = (
        selector
    )

    target_domain, target_value = (
        replacement
    )

    items = list(
        (
            order_state
            or {}
        ).get(
            "items",
            [],
        )
        or []
    )

    matches = []

    for item in items:

        if (
            item.get(
                "item_type"
            )
            != "burger"
        ):
            continue

        if (
            item.get(
                "type"
            )
            != "set"
        ):
            continue

        if (
            item.get(
                selector_field
            )
            != selector_value
        ):
            continue

        matches.append(
            item
        )

    # state에서 정확히 하나일 때만 resolve.
    if len(matches) != 1:
        return None

    line_id = (
        matches[0][
            "line_id"
        ]
    )

    return RouterOutput.model_validate({
        "acts": [
            {
                "family":
                    "order_action",

                "subtype":
                    "modify",

                "speech_act":
                    "request",

                "commitment":
                    "explicit",

                "target_domain":
                    target_domain,

                "target":
                    target_value,

                "reference": {
                    "source":
                        "current_order",

                    "resolved":
                        True,

                    "value":
                        target_value,

                    "line_ids": [
                        line_id
                    ],
                },

                "resolution":
                    "context_resolved",
            }
        ]
    })




# ============================================================
# FINAL CORRECTION SEMANTIC FIX
# ============================================================

def _ordered_alias_occurrences(
    utterance,
    alias_map,
):
    """
    발화에 등장한 enum 값을 위치 순서대로 반환한다.
    긴 alias를 우선해 겹치는 표현의 오검출을 줄인다.
    """

    raw = _state_repair_compact(
        utterance
    )

    candidates = []

    for canonical, aliases in (
        alias_map.items()
    ):

        if isinstance(
            aliases,
            str,
        ):
            aliases = (
                aliases,
            )

        for alias in aliases:

            compact_alias = (
                _state_repair_compact(
                    alias
                )
            )

            if not compact_alias:
                continue

            start = 0

            while True:

                index = raw.find(
                    compact_alias,
                    start,
                )

                if index < 0:
                    break

                candidates.append(
                    (
                        index,
                        len(
                            compact_alias
                        ),
                        canonical,
                    )
                )

                start = (
                    index
                    + len(
                        compact_alias
                    )
                )

    # 같은 위치라면 긴 alias 우선
    candidates.sort(
        key=lambda x:
            (
                x[0],
                -x[1],
            )
    )

    result = []

    used_positions = set()

    for index, length, canonical in candidates:

        if index in used_positions:
            continue

        used_positions.add(
            index
        )

        result.append(
            (
                index,
                canonical,
            )
        )

    return result


def _rightmost_corrected_alias_value(
    utterance,
    alias_map,
):
    """
    같은 field를 한 발화 안에서 정정한 경우
    correction 뒤의 최종 값을 반환.

    예:
        라지 아니 미디엄 -> medium
        스몰 말고 라지   -> large
    """

    raw = _state_repair_compact(
        utterance
    )

    occurrences = (
        _ordered_alias_occurrences(
            utterance,
            alias_map,
        )
    )

    distinct = {
        value
        for _, value
        in occurrences
    }

    if len(distinct) < 2:
        return None

    markers = (
        "아니",
        "아니다",
        "아니고",
        "말고",
        "대신",
    )

    marker_positions = []

    for marker in markers:

        start = 0

        while True:

            index = raw.find(
                marker,
                start,
            )

            if index < 0:
                break

            marker_positions.append(
                (
                    index,
                    marker,
                )
            )

            start = (
                index
                + len(marker)
            )

    marker_positions.sort(
        key=lambda x:
            x[0],
        reverse=True,
    )

    for index, marker in marker_positions:

        after = [
            (
                pos,
                value,
            )
            for pos, value
            in occurrences
            if pos
            > (
                index
                + len(marker)
                - 1
            )
        ]

        if after:

            return max(
                after,
                key=lambda x:
                    x[0],
            )[1]

    return None


def _repair_explicit_burger_menu_replacement(
    utterance,
    order_state,
):
    """
    현재 주문의 burger menu 자체를
    다른 burger menu로 교체하는 표현.

    예:
        불고기버거 말고
        치킨버거 단품으로 바꿔주세요

    source/destination이 모두 명시되고
    현재 state에서 source burger가 정확히 하나일 때만
    deterministic하게 reference를 복구한다.
    """

    raw = _state_repair_compact(
        utterance
    )

    if not any(
        word in raw
        for word in (
            "바꿔",
            "바꾸",
            "변경",
            "교체",
        )
    ):
        return None

    correction_markers = (
        "말고",
        "대신",
    )

    marker_positions = [
        raw.find(marker)
        for marker
        in correction_markers
        if raw.find(marker)
        >= 0
    ]

    if not marker_positions:
        return None

    marker_pos = min(
        marker_positions
    )

    menu_occurrences = []

    for canonical, aliases in (
        MENU_ALIASES.items()
    ):

        for alias in aliases:

            compact_alias = (
                _state_repair_compact(
                    alias
                )
            )

            # 메뉴 교체는 명시적인 "...버거" 이름만 사용.
            if (
                not compact_alias
                or "버거"
                not in compact_alias
            ):
                continue

            start = 0

            while True:

                index = raw.find(
                    compact_alias,
                    start,
                )

                if index < 0:
                    break

                menu_occurrences.append(
                    (
                        index,
                        canonical,
                    )
                )

                start = (
                    index
                    + len(
                        compact_alias
                    )
                )

    if not menu_occurrences:
        return None

    menu_occurrences.sort(
        key=lambda x:
            x[0]
    )

    before = [
        item
        for item
        in menu_occurrences
        if item[0] < marker_pos
    ]

    after = [
        item
        for item
        in menu_occurrences
        if item[0] > marker_pos
    ]

    if (
        not before
        or not after
    ):
        return None

    source_menu = before[-1][1]
    destination_menu = after[-1][1]

    if (
        source_menu
        == destination_menu
    ):
        return None

    state_items = [
        item
        for item in (
            (
                order_state
                or {}
            ).get(
                "items",
                [],
            )
            or []
        )
        if (
            item.get(
                "item_type"
            )
            == "burger"
            and item.get(
                "menu"
            )
            == source_menu
        )
    ]

    if len(state_items) != 1:
        return None

    line_id = state_items[0][
        "line_id"
    ]

    return RouterOutput.model_validate({
        "acts": [
            {
                "family":
                    "order_action",

                "subtype":
                    "modify",

                "speech_act":
                    "correction",

                "commitment":
                    "explicit",

                "target_domain":
                    "burger",

                "target":
                    destination_menu,

                "reference": {
                    "source":
                        "current_order",

                    "resolved":
                        True,

                    "value":
                        destination_menu,

                    "line_ids": [
                        line_id
                    ],
                },

                "resolution":
                    "context_resolved",
            }
        ]
    })



def route_customer_utterance(
    utterance,
    *,
    history=None,
    pending=None,
    order_state=None,
    timeout=60.0,
):
    """
    기존 Router/Pending lane을 먼저 실행한 뒤,
    state로 확정 가능한 의미만 제한적으로 보정한다.
    """

    output = (
        _ROUTE_CUSTOMER_UTTERANCE_STATE_REPAIR_BASE(
            utterance,
            history=history,
            pending=pending,
            order_state=order_state,
            timeout=timeout,
        )
    )

    # pending 질문을 처리 중인 경우 기존 pending lane 우선.
    if pending is not None:
        return output

    # --------------------------------------------------------
    # 기존 burger menu -> 다른 burger menu 교체
    # --------------------------------------------------------

    repaired = (
        _repair_explicit_burger_menu_replacement(
            utterance,
            order_state,
        )
    )

    if repaired is not None:
        return repaired

    # --------------------------------------------------------
    # N개만 남겨
    # --------------------------------------------------------

    repaired = (
        _repair_keep_quantity_router(
            utterance,
            order_state,
        )
    )

    if repaired is not None:
        return repaired

    # --------------------------------------------------------
    # 그중 N개만 세트/단품 변경
    # --------------------------------------------------------

    repaired = (
        _repair_subset_type_router(
            utterance,
            order_state,
        )
    )

    if repaired is not None:
        return repaired

    # --------------------------------------------------------
    # 주문 속성으로 특정 line을 가리키는 modify
    # --------------------------------------------------------

    repaired = (
        _repair_attribute_reference_modify(
            utterance,
            order_state,
        )
    )

    if repaired is not None:
        return repaired

    # --------------------------------------------------------
    # Router가 명시적 신규 burger 자체를 누락
    # --------------------------------------------------------

    output = (
        _repair_missed_explicit_burger(
            utterance,
            order_state,
            output,
        )
    )

    return output


def _extract_fastpath_context(
    args,
    kwargs,
):
    """
    build_router_fastpath의 실제 parameter 이름에
    의존하지 않도록 전달값의 형태로 찾는다.
    """

    output = None
    utterance = None
    state = None

    values = list(
        args
    ) + list(
        kwargs.values()
    )

    for value in values:

        if (
            output is None
            and hasattr(
                value,
                "acts",
            )
        ):
            output = value
            continue

        if (
            utterance is None
            and isinstance(
                value,
                str,
            )
        ):
            utterance = value
            continue

        if (
            state is None
            and isinstance(
                value,
                dict,
            )
            and isinstance(
                value.get(
                    "items"
                ),
                list,
            )
        ):
            state = value

    return (
        output,
        utterance,
        state,
    )


def build_router_fastpath(
    *args,
    **kwargs,
):
    """
    기존 fastpath는 그대로 사용.

    단:
    1. Router semantic repair로 복구한 신규 burger는
       반드시 Runtime 원문 경로로 보냄.
    2. '하나 더' contextual repeat는 원본 품목의
       옵션까지 복제함.
    """

    (
        output,
        utterance,
        state,
    ) = _extract_fastpath_context(
        args,
        kwargs,
    )

    # --------------------------------------------------------
    # 새 burger 의미를 Router 후처리로 복구한 경우
    # fastpath를 쓰지 않고 Runtime V14에 원문 전달.
    # --------------------------------------------------------

    if output is not None:

        for act in (
            getattr(
                output,
                "acts",
                [],
            )
            or []
        ):

            reference = getattr(
                act,
                "reference",
                None,
            )

            reference_value = (
                getattr(
                    reference,
                    "value",
                    None,
                )
                if reference is not None
                else None
            )

            if (
                reference_value
                == "__keep_quantity__"
            ):

                remove_ids = list(
                    getattr(
                        reference,
                        "line_ids",
                        [],
                    )
                    or []
                )

                if remove_ids:

                    actions = []

                    for line_id in remove_ids:

                        actions.append({
                            "operation":
                                "remove",

                            "target": {
                                "line_id":
                                    int(
                                        line_id
                                    ),

                                "item_type":
                                    None,

                                "menu":
                                    None,

                                "drink":
                                    None,

                                "side":
                                    None,
                            },

                            "item":
                                None,

                            "quantity_delta":
                                None,

                            "apply_to_all":
                                False,

                            "exclude_add":
                                [],

                            "exclude_remove":
                                [],

                            "toppings_add":
                                [],

                            "toppings_remove":
                                [],
                        })

                    return (
                        {
                            "intent":
                                "order",

                            "order_id":
                                None,

                            "actions":
                                actions,
                        },
                        "state_keep_quantity",
                    )

            if (
                reference_value
                == "__router_repaired_new_burger__"
            ):
                return (
                    None,
                    "router_repaired_new_burger",
                )

    result = (
        _BUILD_ROUTER_FASTPATH_STATE_REPAIR_BASE(
            *args,
            **kwargs,
        )
    )

    if (
        not isinstance(
            result,
            tuple,
        )
        or len(result) != 2
    ):
        return result

    update, reason = result

    # --------------------------------------------------------
    # drink size self-correction
    #
    # "라지 아니 미디엄"처럼 같은 field를 정정한 경우
    # simple fastpath가 첫 alias를 고른 결과를 최종 alias로 보정.
    # --------------------------------------------------------

    corrected_size = (
        _rightmost_corrected_alias_value(
            utterance,
            SIZE_ALIASES,
        )
    )

    if (
        corrected_size is not None
        and update is not None
    ):

        import copy as _copy

        if isinstance(
            update,
            dict,
        ):

            corrected_update = (
                _copy.deepcopy(
                    update
                )
            )

            actions = (
                corrected_update.get(
                    "actions",
                    [],
                )
                or []
            )

            if len(actions) == 1:

                action = actions[0]

                item = (
                    action.get(
                        "item"
                    )
                    or {}
                )

                if (
                    action.get(
                        "operation"
                    )
                    == "add"

                    and
                    item.get(
                        "item_type"
                    )
                    == "drink"
                ):

                    item[
                        "drink_size"
                    ] = corrected_size

                    update = (
                        corrected_update
                    )

                    result = (
                        update,
                        reason,
                    )

    if (
        update is None
        or output is None
        or state is None
        or not utterance
    ):
        return result


    # ========================================================
    # FASTPATH RESOLVED MODIFY SLOT CANONICALIZATION
    # ========================================================
    #
    # Router가 이미:
    #
    #   target_domain = side
    #   target        = cheese_stick
    #   line_ids      = [1]
    #
    # 처럼 명확하게 resolve했다면 fastpath는 그 의미를
    # 다른 slot(type 등)으로 재해석하면 안 된다.
    #
    # semantic 결정은 Router가 하고,
    # fastpath는 canonical state slot으로 옮기기만 한다.
    # ========================================================

    resolved_modify_acts = list(
        getattr(
            output,
            "acts",
            [],
        )
        or []
    )

    if len(
        resolved_modify_acts
    ) == 1:

        resolved_act = (
            resolved_modify_acts[0]
        )

        resolved_family = (
            _router_value(
                getattr(
                    resolved_act,
                    "family",
                    None,
                )
            )
        )

        resolved_subtype = (
            _router_value(
                getattr(
                    resolved_act,
                    "subtype",
                    None,
                )
            )
        )

        resolved_domain = (
            _router_value(
                getattr(
                    resolved_act,
                    "target_domain",
                    None,
                )
            )
        )

        resolved_target = (
            getattr(
                resolved_act,
                "target",
                None,
            )
        )

        resolved_reference = (
            getattr(
                resolved_act,
                "reference",
                None,
            )
        )

        resolved_line_ids = list(
            getattr(
                resolved_reference,
                "line_ids",
                [],
            )
            or []
        )

        resolved_reference_ok = bool(
            resolved_reference is not None
            and getattr(
                resolved_reference,
                "resolved",
                False,
            )
            and len(
                resolved_line_ids
            ) == 1
        )

        slot_patch = None

        if (
            resolved_family
            == "order_action"

            and
            resolved_subtype
            == "modify"

            and
            resolved_reference_ok
        ):

            # -----------------------------------------------
            # SIDE
            # -----------------------------------------------

            if (
                resolved_domain
                == "side"

                and
                resolved_target
                in SIDE_LABELS
            ):

                slot_patch = {
                    "side":
                        resolved_target,
                }

            # -----------------------------------------------
            # DRINK / DRINK SIZE
            # -----------------------------------------------

            elif (
                resolved_domain
                == "drink"
            ):

                if (
                    resolved_target
                    in DRINK_LABELS
                ):

                    slot_patch = {
                        "drink":
                            resolved_target,
                    }

                elif (
                    resolved_target
                    in SIZE_LABELS
                ):

                    slot_patch = {
                        "drink_size":
                            resolved_target,
                    }

            # -----------------------------------------------
            # BURGER TYPE
            # -----------------------------------------------

            elif (
                resolved_domain
                == "burger"
            ):

                if (
                    resolved_target
                    in MENU_LABELS
                ):

                    slot_patch = {
                        "menu":
                            resolved_target,
                    }

                    # 같은 문장에 단품/세트가 하나만 명시됐다면
                    # 그 값도 함께 반영한다.
                    explicit_types = {
                        value
                        for _, value
                        in _ordered_alias_occurrences(
                            utterance,
                            TYPE_ALIASES,
                        )
                    }

                    if (
                        len(
                            explicit_types
                        )
                        == 1
                    ):

                        slot_patch[
                            "type"
                        ] = next(
                            iter(
                                explicit_types
                            )
                        )

                elif (
                    resolved_target
                    in TYPE_LABELS
                ):

                    slot_patch = {
                        "type":
                            resolved_target,
                    }

        if slot_patch is not None:

            item_patch = {
                "item_type":
                    None,

                "quantity":
                    None,

                "menu":
                    None,

                "type":
                    None,

                "drink":
                    None,

                "drink_size":
                    None,

                "side":
                    None,
            }

            item_patch.update(
                slot_patch
            )

            canonical_update = {
                "intent":
                    "order",

                "order_id":
                    None,

                "actions": [
                    {
                        "operation":
                            "modify",

                        "target": {
                            "line_id":
                                int(
                                    resolved_line_ids[0]
                                ),

                            "item_type":
                                None,

                            "menu":
                                None,

                            "drink":
                                None,

                            "side":
                                None,
                        },

                        "item":
                            item_patch,

                        "quantity_delta":
                            None,

                        "apply_to_all":
                            False,

                        "exclude_add":
                            [],

                        "exclude_remove":
                            [],

                        "toppings_add":
                            [],

                        "toppings_remove":
                            [],
                    }
                ],
            }

            return (
                canonical_update,
                reason,
            )

    acts = list(
        getattr(
            output,
            "acts",
            [],
        )
        or []
    )

    if len(acts) != 1:
        return result

    act = acts[0]

    family = _router_value(
        getattr(
            act,
            "family",
            None,
        )
    )

    subtype = _router_value(
        getattr(
            act,
            "subtype",
            None,
        )
    )

    reference = getattr(
        act,
        "reference",
        None,
    )

    source = _router_value(
        getattr(
            reference,
            "source",
            None,
        )
    )

    line_ids = list(
        getattr(
            reference,
            "line_ids",
            [],
        )
        or []
    )

    compact = (
        _state_repair_compact(
            utterance
        )
    )

    repeat_signal = any(
        phrase in compact
        for phrase in (
            "하나더",
            "한개더",
            "하나더추가",
            "한개더추가",
            "같은거하나더",
            "그거하나더",
        )
    )

    # 사용자가 새 type을 직접 말했으면 복제하지 않는다.
    explicit_type = (
        "단품" in compact
        or "세트" in compact
    )

    if not (
        family == "order_action"
        and subtype == "add"
        and source == "current_order"
        and len(line_ids) == 1
        and repeat_signal
        and not explicit_type
    ):
        return result

    source_item = next(
        (
            item
            for item in (
                state.get(
                    "items",
                    [],
                )
                or []
            )
            if item.get(
                "line_id"
            )
            == line_ids[0]
        ),
        None,
    )

    if source_item is None:
        return result

    import copy as _copy

    if isinstance(
        update,
        dict,
    ):
        data = _copy.deepcopy(
            update
        )

    elif hasattr(
        update,
        "model_dump",
    ):
        data = update.model_dump(
            mode="json",
            exclude_none=True,
        )

    else:
        return result

    actions = data.get(
        "actions",
        [],
    )

    if len(actions) != 1:
        return result

    action = actions[0]

    if action.get(
        "operation"
    ) != "add":
        return result

    item = action.get(
        "item"
    )

    if not isinstance(
        item,
        dict,
    ):
        return result

    # 상품 종류가 다르면 절대 복제하지 않는다.
    if (
        item.get(
            "item_type"
        )
        != source_item.get(
            "item_type"
        )
    ):
        return result

    kind = source_item.get(
        "item_type"
    )

    if kind == "burger":

        item["menu"] = (
            source_item.get(
                "menu"
            )
        )

        item["type"] = (
            source_item.get(
                "type"
            )
        )

        item["drink"] = (
            source_item.get(
                "drink"
            )
        )

        item["drink_size"] = (
            source_item.get(
                "drink_size"
            )
        )

        item["side"] = (
            source_item.get(
                "side"
            )
        )

        action["exclude_add"] = list(
            source_item.get(
                "exclude",
                [],
            )
            or []
        )

        action["toppings_add"] = list(
            source_item.get(
                "add_toppings",
                [],
            )
            or []
        )

    elif kind == "drink":

        item["drink"] = (
            source_item.get(
                "drink"
            )
        )

        item["drink_size"] = (
            source_item.get(
                "drink_size"
            )
        )

    elif kind == "side":

        item["side"] = (
            source_item.get(
                "side"
            )
        )

    else:
        return result

    if isinstance(
        update,
        dict,
    ):
        repaired_update = data

    else:

        try:

            repaired_update = (
                type(update)
                .model_validate(
                    data
                )
            )

        except Exception:
            return result

    return (
        repaired_update,
        reason,
    )



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


# ============================================================
# ORDER DOMAIN GUARD
# ============================================================

def _build_order_domain_terms():
    """
    Runtime이 실제 지원하는 메뉴/옵션 alias로
    주문 도메인 단어 집합을 만든다.
    """

    terms = set()

    alias_maps = (
        MENU_ALIASES,
        TYPE_ALIASES,
        DRINK_ALIASES,
        SIZE_ALIASES,
        SIDE_ALIASES,
        EXCLUDE_INGREDIENT_ALIASES,
    )

    for alias_map in alias_maps:
        for aliases in alias_map.values():
            for alias in aliases:
                value = re.sub(
                    r"\s+",
                    "",
                    str(alias).lower(),
                )

                if value:
                    terms.add(value)

    terms.update({
        "햄버거",
        "버거",
        "음료",
        "음료수",
        "사이드",
        "사이드메뉴",
        "베이컨",
        "토핑",
        "맥오더",
        "모바일주문",
        "픽업",
        "주문번호",
    })

    return terms


ORDER_DOMAIN_TERMS = _build_order_domain_terms()


ASR_HALLUCINATION_PHRASES = (
    "시청해주셔서감사합니다",
    "시청해주셔서고맙습니다",
    "구독과좋아요",
    "구독좋아요",
    "자막제공",
)



# === ASZ STAFF CALL GUARD ===

def is_staff_call_utterance(text):

    compact = re.sub(
        r"[\s!?.,~]+",
        "",
        str(text or "").lower(),
    )

    # 호출 취소 표현은 호출로 처리하지 않는다.
    negative = (
        "부르지마",
        "부르지말",
        "호출하지마",
        "호출하지말",
        "안불러",
        "안불러도",
    )

    if any(
        phrase in compact
        for phrase in negative
    ):
        return False


    has_target = any(
        word in compact
        for word in (
            "직원",
            "직원분",
            "사람",
        )
    )

    has_call = any(
        word in compact
        for word in (
            "불러",
            "불러줘",
            "불러주세요",
            "호출",
            "와주세요",
            "와줘",
        )
    )

    return (
        has_target
        and has_call
    )


def has_order_domain_signal(
    text,
    *,
    pending=None,
    mobile_confirmation_pending=False,
):
    """
    자연어 주문을 Python에서 해석하는 게 아니라
    V14에 보낼 만한 '주문 관련 발화인지'만 검사한다.
    """

    raw = normalize_input(
        str(text or "")
    ).lower()

    compact = re.sub(
        r"\s+",
        "",
        raw,
    )

    if not compact:
        return False

    # 맥오더 번호 확인 중에는 네/아니요 같은 답도 정상.
    if mobile_confirmation_pending:
        return True

    # 메뉴 / 옵션 / 재료 직접 언급
    if any(
        term in compact
        for term in ORDER_DOMAIN_TERMS
    ):
        return True

    # 주문 확정 / 종료
    if any(
        keyword in compact
        for keyword in FINALIZATION_KEYWORDS
    ):
        return True

    # 주문 취소
    if any(
        word in compact
        for word in (
            "주문취소",
            "취소할게",
            "취소해주세요",
            "취소해줘",
        )
    ):
        return True

    # 맥오더 번호만 말하는 경우
    if re.fullmatch(
        r"\d{1,3}번(?:이요|요)?",
        compact,
    ):
        return True

    # 기존 주문을 문맥으로 사용하는 짧은 정상 발화
    contextual_phrases = (
        "하나더",
        "한개더",
        "두개더",
        "세개더",
        "그거",
        "그걸",
        "이거",
        "이걸",
        "저거",
        "저걸",
        "아까거",
        "아까꺼",
        "그대로",
        "빼주세요",
        "제외해주세요",
        "바꿔주세요",
        "변경해주세요",
    )

    if any(
        phrase in compact
        for phrase in contextual_phrases
    ):
        return True

    return False


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
    # 7. ASR hallucination / 주문 도메인 밖 입력 차단
    # ========================================================

    compact_for_guard = re.sub(
        r"\s+",
        "",
        raw.lower(),
    )

    if any(
        phrase in compact_for_guard
        for phrase in ASR_HALLUCINATION_PHRASES
    ):
        return {
            "allow": False,
            "reason": "stt_hallucination_phrase",
            "reply": (
                "잘 듣지 못했습니다. "
                "주문을 다시 말씀해주세요."
            ),
        }

    # 주문 도메인 신호가 없는 자연스러운 발화도
    # Router까지 전달한다.
    #
    # Router가 general_chat / out_of_scope를 구분하고,
    # policy가 read_only로 주문 state 변경을 막는다.
    #
    # 명백한 STT hallucination은 위에서 이미 차단한다.

    # ========================================================
    # 정상 입력
    # ========================================================

    return {
        "allow": True,
        "reason": None,
        "reply": None,
    }



# ============================================================
# ROUTER -> APP POLICY BRIDGE
# ============================================================

def _router_value(value):
    if value is None:
        return None

    return getattr(
        value,
        "value",
        value,
    )


def _router_known_target(
    act,
    utterance,
):
    target = getattr(
        act,
        "target",
        None,
    )

    known = (
        set(MENU_LABELS)
        | set(DRINK_LABELS)
        | set(SIDE_LABELS)
        | set(TOPPING_LABELS)
        | set(TYPE_LABELS)
        | set(SIZE_LABELS)
    )

    if target in known:
        return target

    raw = str(
        utterance
        or ""
    ).lower()

    alias_groups = (
        MENU_ALIASES,
        DRINK_ALIASES,
        SIDE_ALIASES,
        TYPE_ALIASES,
        SIZE_ALIASES,
    )

    # 긴 alias부터 확인해서 "제로콜라"를 "콜라"보다 먼저 잡는다.
    candidates = []

    for mapping in alias_groups:
        for canonical, aliases in mapping.items():
            for alias in aliases:
                value = str(alias or "").strip().lower()
                if value:
                    candidates.append(
                        (
                            len(value),
                            value,
                            canonical,
                        )
                    )

    for _, alias, canonical in sorted(
        candidates,
        reverse=True,
    ):
        if alias in raw:
            return canonical

    return None


def _explicit_burger_from_utterance(utterance):
    """
    현재 사용자 발화에 실제로 명시된 burger만 추출한다.

    과거 history / Router context보다 현재 발화가 최우선이다.
    긴 alias를 우선한다.
    """

    compact = re.sub(
        r"\s+",
        "",
        str(utterance or "").lower(),
    )

    candidates = []

    for canonical, aliases in MENU_ALIASES.items():

        names = set()

        if isinstance(aliases, str):
            names.add(aliases)
        else:
            try:
                names.update(
                    str(value)
                    for value in aliases
                    if value
                )
            except TypeError:
                pass

        label = MENU_LABELS.get(canonical)

        if label:
            names.add(label)

        for name in names:

            alias = re.sub(
                r"\s+",
                "",
                str(name).lower(),
            )

            if alias and alias in compact:
                candidates.append(
                    (
                        len(alias),
                        canonical,
                    )
                )

    if not candidates:
        return None

    candidates.sort(
        reverse=True
    )

    return candidates[0][1]


def _router_order_summary(state):
    items = (
        state.get("items", [])
        if isinstance(state, dict)
        else []
    )

    if not items:
        return "현재 담긴 주문이 없습니다."

    # ========================================================
    # 같은 옵션의 동일 품목만 묶는다.
    # state 자체는 변경하지 않고 표시용으로만 집계한다.
    # ========================================================

    grouped = {}

    for item in items:

        key = (
            item.get("item_type"),
            item.get("menu"),
            item.get("type"),
            item.get("drink"),
            item.get("drink_size"),
            item.get("side"),
            tuple(sorted(
                item.get("exclude", [])
                or []
            )),
            tuple(sorted(
                item.get("add_toppings", [])
                or []
            )),
        )

        quantity = int(
            item.get("quantity", 1)
            or 1
        )

        if key not in grouped:
            grouped[key] = {
                "item": item,
                "quantity": 0,
            }

        grouped[key]["quantity"] += (
            quantity
        )

    # ========================================================
    # 말하기용 품목명
    # ========================================================

    titles = []

    for entry in grouped.values():

        item = entry["item"]
        quantity = entry["quantity"]

        item_type = item.get(
            "item_type"
        )

        if item_type == "burger":

            menu = MENU_LABELS.get(
                item.get("menu"),
                "버거",
            )

            order_type = TYPE_LABELS.get(
                item.get("type"),
                "옵션 선택 필요",
            )

            title = (
                f"{menu} "
                f"{order_type} "
                f"{quantity}개"
            )

        elif item_type == "drink":

            drink = DRINK_LABELS.get(
                item.get("drink"),
                "음료",
            )

            size = SIZE_LABELS.get(
                item.get("drink_size"),
                "사이즈 선택 필요",
            )

            title = (
                f"{drink} "
                f"{size} "
                f"{quantity}잔"
            )

        elif item_type == "side":

            side = SIDE_LABELS.get(
                item.get("side"),
                "사이드",
            )

            title = (
                f"{side} "
                f"{quantity}개"
            )

        else:

            title = (
                f"메뉴 {quantity}개"
            )

        titles.append(
            title
        )

    # ========================================================
    # 총 가격은 기존 실제 state 기준으로 계산
    # ========================================================

    total = 0
    complete = True

    for item in items:

        price = unit_price(
            item
        )

        if price is None:
            complete = False
            continue

        quantity = int(
            item.get("quantity", 1)
            or 1
        )

        total += (
            price
            * quantity
        )

    text = (
        "현재 주문은 "
        + ", ".join(titles)
        + "입니다."
    )

    if complete:
        text += (
            f" 현재 합계는 "
            f"{total:,}원입니다."
        )

    return text


def _router_menu_reply(domain=None):
    domain = _router_value(domain)

    burger_text = ", ".join(
        MENU_LABELS.values()
    )

    drink_text = ", ".join(
        DRINK_LABELS.values()
    )

    side_text = ", ".join(
        SIDE_LABELS.values()
    )

    if domain == "burger":
        return "버거 메뉴는 " + burger_text + "입니다."

    if domain == "drink":
        return (
            "음료 메뉴는 "
            + drink_text
            + "입니다."
        )

    if domain == "side":
        return "사이드 메뉴는 " + side_text + "입니다."

    return (
        "버거는 "
        + burger_text
        + ". 음료는 "
        + drink_text
        + ". 사이드는 "
        + side_text
        + "입니다."
    )


def _router_price_reply(
    act,
    utterance,
    state,
):
    target = _router_known_target(
        act,
        utterance,
    )

    if target in BURGER_BASE_PRICE:
        return (
            f"{MENU_LABELS[target]} 단품 기본가는 "
            f"{BURGER_BASE_PRICE[target]:,}원입니다. "
            f"세트는 기본적으로 +{SET_UPCHARGE:,}원이며 "
            "음료, 사이즈, 사이드 선택에 따라 추가금이 반영됩니다."
        )

    if target in STANDALONE_DRINK_PRICE:
        base = STANDALONE_DRINK_PRICE[
            target
        ]

        return (
            f"{DRINK_LABELS[target]}는 "
            f"스몰 {base + DRINK_SIZE_UPCHARGE['small']:,}원, "
            f"미디엄 {base + DRINK_SIZE_UPCHARGE['medium']:,}원, "
            f"라지 {base + DRINK_SIZE_UPCHARGE['large']:,}원입니다."
        )

    if target in STANDALONE_SIDE_PRICE:
        return (
            f"{SIDE_LABELS[target]}은 "
            f"{STANDALONE_SIDE_PRICE[target]:,}원입니다."
        )

    if target in TOPPING_PRICE:
        return (
            f"{TOPPING_LABELS[target]} 추가는 "
            f"+{TOPPING_PRICE[target]:,}원입니다."
        )

    if target == "set":
        return (
            f"버거를 세트로 변경하면 "
            f"{SET_UPCHARGE:,}원의 추가금이 붙습니다. "
            "선택한 음료, 사이즈, 사이드에 따라 "
            "추가금이 더 붙을 수 있습니다."
        )

    return _router_order_summary(
        state
    )


def _router_option_reply(
    domain,
    utterance,
):
    domain = _router_value(domain)
    compact = re.sub(
        r"\s+",
        "",
        str(utterance or "").lower(),
    )

    if (
        domain == "drink"
        or "음료" in compact
    ):
        return (
            "음료는 "
            + ", ".join(DRINK_LABELS.values())
            + " 중에서 선택할 수 있습니다."
        )

    if (
        domain == "side"
        or "사이드" in compact
    ):
        return (
            "사이드는 "
            + ", ".join(SIDE_LABELS.values())
            + " 중에서 선택할 수 있습니다."
        )

    if "사이즈" in compact or "크기" in compact:
        return (
            "음료 사이즈는 "
            + ", ".join(SIZE_LABELS.values())
            + "입니다."
        )

    return (
        "버거는 단품 또는 세트로 선택할 수 있고, "
        "세트는 음료, 음료 사이즈, 사이드를 선택합니다."
    )





def explicit_new_burger_order_request(
    utterance,
    state,
):
    """
    Router가 add/modify를 잘못 분류해도,
    발화 자체가 명확한 '신규 버거 주문'인지 판단한다.

    인정:
      새우버거 양상추 제외해서 단품 하나 주세요
      불고기버거 베이컨 넣어서 단품 하나 주세요
      치킨버거 토마토 빼고 하나 주세요

    비인정:
      불고기버거에 베이컨 추가해줘
      불고기버거를 세트로 바꿔줘

    보수적으로:
      - full burger menu가 정확히 하나
      - 현재 같은 burger가 없음
      - 명시적인 수량 또는 단품/세트 주문 신호 존재
      - 변경/수정 표현이 아님
    """

    compact = re.sub(
        r"\s+",
        "",
        str(
            utterance
            or ""
        ).lower(),
    )

    if not compact:
        return False

    # 기존 품목 변경 표현은 신규 주문으로 보지 않는다.
    change_terms = (
        "바꿔",
        "바꾸",
        "변경",
        "수정",
        "교체",
    )

    if any(
        term in compact
        for term in change_terms
    ):
        return False

    explicit_menus = set()

    for canonical, aliases in (
        MENU_ALIASES.items()
    ):

        for alias in aliases:

            alias_compact = re.sub(
                r"\s+",
                "",
                str(alias).lower(),
            )

            # short alias는 오탐 방지를 위해 제외.
            if (
                not alias_compact
                or "버거"
                not in alias_compact
            ):
                continue

            if alias_compact in compact:

                explicit_menus.add(
                    canonical
                )

                break

    if len(explicit_menus) != 1:
        return False

    menu = next(
        iter(
            explicit_menus
        )
    )

    # 현재 같은 버거가 이미 있으면
    # 우선 기존 품목 수정 가능성을 존중한다.
    same_menu_exists = any(
        item.get("item_type")
        == "burger"
        and item.get("menu")
        == menu
        for item in (
            state.get(
                "items",
                [],
            )
            or []
        )
    )

    if same_menu_exists:
        return False

    # 강한 신규 주문 신호
    type_signal = any(
        word in compact
        for word in (
            "단품",
            "세트",
            "셋트",
        )
    )

    quantity_signal = bool(
        re.search(
            r"(?:"
            r"하나|한개|"
            r"두개|두개|"
            r"세개|"
            r"네개|"
            r"\d+개"
            r")",
            compact,
        )
    )

    # 명시 수량/타입 없이
    # "버거에 베이컨 추가해줘" 같은 modify를
    # 신규 주문으로 오인하지 않는다.
    if not (
        type_signal
        or quantity_signal
    ):
        return False

    order_request_signal = any(
        word in compact
        for word in (
            "주세요",
            "줘",
            "주라",
            "주십시오",
            "주문",
            "추가",
        )
    )

    return order_request_signal


def missing_burger_modify_reply(
    output,
    utterance,
    state,
):
    """
    현재 주문에 존재하지 않는 명시적 burger를
    수정하려는 요청을 Runtime 전에 차단한다.

    단, 발화 자체가 명확한 신규 버거 주문이면
    Router가 modify로 잘못 분류했더라도 차단하지 않는다.
    """

    acts = list(
        getattr(
            output,
            "acts",
            [],
        )
        or []
    )

    if len(acts) != 1:
        return None

    act = acts[0]

    family = _router_value(
        getattr(
            act,
            "family",
            None,
        )
    )

    subtype = _router_value(
        getattr(
            act,
            "subtype",
            None,
        )
    )

    if not (
        family == "order_action"
        and subtype == "modify"
    ):
        return None

    # --------------------------------------------------------
    # Router subtype가 틀려도 발화 자체가 신규 주문이면 통과
    # --------------------------------------------------------

    if explicit_new_burger_order_request(
        utterance,
        state,
    ):
        return None

    compact = re.sub(
        r"\s+",
        "",
        str(
            utterance
            or ""
        ).lower(),
    )

    explicit_menus = set()

    for canonical, aliases in (
        MENU_ALIASES.items()
    ):

        for alias in aliases:

            compact_alias = re.sub(
                r"\s+",
                "",
                str(alias).lower(),
            )

            if (
                "버거"
                not in compact_alias
                or not compact_alias
            ):
                continue

            if compact_alias in compact:

                explicit_menus.add(
                    canonical
                )

                break

    if len(explicit_menus) != 1:
        return None

    menu = next(
        iter(
            explicit_menus
        )
    )

    exists = any(
        item.get("item_type")
        == "burger"
        and item.get("menu")
        == menu
        for item in (
            state.get(
                "items",
                [],
            )
            or []
        )
    )

    if exists:
        return None

    label = (
        MENU_LABELS.get(
            menu
        )
        or "해당 버거"
    )

    return (
        f"현재 주문에 {label}가 없어요. "
        f"먼저 {label}를 주문해주세요."
    )



def burger_topping_state_guard_reply(
    output,
    utterance,
    state,
):
    """
    Runtime 진입 전 topping mutation safety guard.

    - 패티 추가는 현재 지원하지 않음.
    - 치즈/베이컨/패티를 기존 burger에 추가하려는데
      현재 주문에 burger가 하나도 없으면 차단.
    - 신규 burger 주문 + topping은 여기서 막지 않는다.
    """

    acts = list(
        getattr(output, "acts", [])
        or []
    )

    if not acts:
        return None

    mutation_acts = [
        act
        for act in acts
        if _router_value(
            getattr(
                act,
                "family",
                None,
            )
        ) == "order_action"
    ]

    if not mutation_acts:
        return None

    compact = re.sub(
        r"\s+",
        "",
        str(utterance or "").lower(),
    )

    if not compact:
        return None

    positive_action = any(
        word in compact
        for word in (
            "추가",
            "넣어",
            "넣을",
            "더넣",
            "올려",
            "얹어",
        )
    )

    modify_like = any(
        (
            _router_value(
                getattr(
                    act,
                    "subtype",
                    None,
                )
            ) == "modify"
        )
        or (
            _router_value(
                getattr(
                    act,
                    "subtype",
                    None,
                )
            ) == "add"
            and _router_value(
                getattr(
                    act,
                    "target_domain",
                    None,
                )
            ) == "topping"
        )
        for act in mutation_acts
    )

    if not modify_like:
        return None

    # 현재 시연에서 지원하지 않는 topping.
    if (
        "패티" in compact
        and positive_action
    ):
        return (
            "패티 추가는 현재 지원하지 않습니다. "
            "추가 가능한 토핑은 치즈, 베이컨, 패티입니다."
        )

    supported_topping = next(
        (
            label
            for label in (
                "치즈",
                "베이컨",
                "패티",
            )
            if label in compact
        ),
        None,
    )

    if (
        supported_topping is None
        or not positive_action
    ):
        return None

    # 음료/사이드가 topping 대상이라고 명시된 경우에는
    # 아래 burger 전용 guard가 가로채지 않는다.
    # 기존 unsupported_topping_mutation_reply에 맡긴다.
    if _explicit_non_burger_product_in_utterance(
        utterance
    ):
        return None

    explicit_burger = (
        _explicit_burger_from_utterance(
            utterance
        )
    )

    burgers = [
        item
        for item in (
            state.get(
                "items",
                [],
            )
            if isinstance(
                state,
                dict,
            )
            else []
        )
        if item.get(
            "item_type"
        ) == "burger"
    ]

    # --------------------------------------------------------
    # 현재 주문에 burger가 하나도 없는데
    # 특정 burger를 명시했다면 generic 메시지보다
    # 해당 burger가 없다는 사실을 정확히 알려준다.
    # --------------------------------------------------------

    if (
        not burgers
        and explicit_burger is not None
    ):
        label = MENU_LABELS.get(
            explicit_burger,
            explicit_burger,
        )

        return (
            f"현재 주문에 {label}가 없어요."
        )

    # --------------------------------------------------------
    # 여러 burger 중 선택 표현
    #
    # 첫 번째 / 두 번째 / 그중 하나만 같은 표현은
    # downstream resolver가 처리할 수 있으므로
    # 여기서 ambiguity로 차단하지 않는다.
    # --------------------------------------------------------

    selection_hint = any(
        word in compact
        for word in (
            "첫번째",
            "두번째",
            "세번째",
            "1번째",
            "2번째",
            "3번째",
            "그중하나만",
            "그중한개만",
            "그중1개만",
        )
    )

    # 현재 주문에 burger가 여러 개라면
    # 정말 대상 단서가 없는 topping mutation만 차단한다.
    if len(burgers) > 1:

        if (
            explicit_burger is None
            and not selection_hint
        ):
            return (
                "어느 버거에 토핑을 추가할지 말씀해주세요."
            )

    if burgers:
        return None

    return (
        "현재 주문에 토핑을 추가할 버거가 없습니다. "
        "먼저 버거를 주문해주세요."
    )


def unsupported_topping_mutation_reply(
    output,
    utterance,
):
    """
    음료/사이드에 topping을 적용하려는 명시적 발화는
    Router 해석과 관계없이 Runtime 진입 전에 차단한다.

    예:
        콜라에 베이컨 추가해줘
        치즈스틱에 치즈 추가해줘
        감튀에 토마토 넣어줘

    주의:
        치즈스틱 추가해줘
        콜라 추가해줘
    같은 일반 상품 추가는 막지 않는다.
    """

    acts = list(
        getattr(output, "acts", [])
        or []
    )

    if not acts:
        return None

    # mutation 요청일 때만 실행 안전망 적용
    if not any(
        _router_value(
            getattr(act, "family", None)
        ) == "order_action"
        for act in acts
    ):
        return None

    compact = re.sub(
        r"\s+",
        "",
        str(utterance or "").lower(),
    )

    if not compact:
        return None

    # topping 관련 단어
    topping_terms = tuple(
        str(label).lower()
        for label in TOPPING_LABELS.values()
    ) + (
        "토핑",
    )

    topping_action_terms = (
        "추가",
        "넣어",
        "넣을",
        "더넣",
        "올려",
        "빼",
        "제거",
    )

    if not any(
        word in compact
        for word in topping_action_terms
    ):
        return None

    # --------------------------------------------------------
    # 음료 / 사이드의 실제 alias를 사용자 문장에서 찾는다.
    #
    # Router의 target_domain을 사용하지 않는 것이 핵심.
    # --------------------------------------------------------

    candidates = []

    domain_specs = (
        (
            "drink",
            DRINK_ALIASES,
            DRINK_LABELS,
        ),
        (
            "side",
            SIDE_ALIASES,
            SIDE_LABELS,
        ),
    )

    for (
        domain,
        alias_map,
        label_map,
    ) in domain_specs:

        for canonical, aliases in alias_map.items():

            names = set(
                str(alias)
                for alias in aliases
            )

            label = label_map.get(
                canonical
            )

            if label:
                names.add(label)

            for name in names:

                alias = re.sub(
                    r"\s+",
                    "",
                    name.lower(),
                )

                if not alias:
                    continue

                # --------------------------------------------
                # "콜라에 베이컨 추가"
                # "치즈스틱에 치즈 추가"
                # "감튀에 토마토 넣어"
                # --------------------------------------------

                particle_target = any(
                    prefix in compact
                    for prefix in (
                        alias + "에",
                        alias + "에는",
                        alias + "에다",
                        alias + "에다가",
                    )
                )

                # --------------------------------------------
                # 조사 생략형:
                # "치즈스틱 치즈 추가"
                # -> compact 후 "치즈스틱치즈추가"
                # --------------------------------------------

                direct_target = any(
                    (
                        alias
                        + topping
                    ) in compact
                    for topping in topping_terms
                )

                if not (
                    particle_target
                    or direct_target
                ):
                    continue

                # 상품명 뒤쪽에 실제 topping 단어가 있는지 확인.
                pos = compact.find(alias)

                tail = (
                    compact[
                        pos + len(alias):
                    ]
                    if pos >= 0
                    else ""
                )

                topping_found = any(
                    topping in tail
                    for topping in topping_terms
                )

                if not topping_found:
                    continue

                candidates.append(
                    (
                        len(alias),
                        domain,
                        canonical,
                        label or name,
                    )
                )

    if not candidates:
        return None

    # "제로콜라"와 "콜라"처럼 겹치는 경우
    # 긴 alias를 우선한다.
    candidates.sort(
        key=lambda x: x[0],
        reverse=True,
    )

    _, domain, canonical, label = (
        candidates[0]
    )

    return (
        f"{label}에는 추가 토핑을 "
        "적용할 수 없어요."
    )



def router_read_only_reply(
    output,
    utterance,
    state,
):
    if not output.acts:
        return (
            "주문 또는 메뉴 관련 내용을 "
            "다시 말씀해주세요."
        )

    act = output.acts[0]

    family = _router_value(
        act.family
    )

    subtype = _router_value(
        act.subtype
    )

    domain = _router_value(
        getattr(
            act,
            "target_domain",
            None,
        )
    )

    target = _router_known_target(
        act,
        utterance,
    )

    # ========================================================
    # OPTION CAPABILITY PRIORITY
    # ========================================================
    # "치즈스틱에 치즈 추가 가능해?" 같은 질문은
    # 단순히 메뉴 존재 여부를 묻는 것이 아니다.
    #
    # answer_menu_query()보다 먼저 옵션 적용 가능 여부를 판단한다.
    #
    # 현재 시연 규칙:
    #   burger -> cheese / bacon / patty 토핑 가능
    #   drink  -> 추가 토핑 불가
    #   side   -> 추가 토핑 불가

    if (
        family == "info_query"
        and subtype == "possibility"
    ):
        compact_capability = re.sub(
            r"\s+",
            "",
            str(utterance or "").lower(),
        )

        target_label = (
            MENU_LABELS.get(target)
            or DRINK_LABELS.get(target)
            or SIDE_LABELS.get(target)
        )

        # 상품명 안에 들어 있는 "치즈"를
        # topping cheese로 오인하지 않게 상품명을 먼저 제거.
        #
        # 예:
        #   "치즈스틱 추가 가능해?"
        #       -> topping 질문 아님
        #
        #   "치즈스틱에 치즈 추가 가능해?"
        #       -> topping 질문 맞음
        capability_text = compact_capability

        if target_label:
            compact_target_label = re.sub(
                r"\s+",
                "",
                target_label.lower(),
            )

            capability_text = (
                capability_text.replace(
                    compact_target_label,
                    "",
                )
            )

        topping_labels = (
            "치즈",
            "베이컨",
            "패티",
        )

        mentioned_topping = next(
            (
                value
                for value in topping_labels
                if value in capability_text
            ),
            None,
        )

        topping_action = any(
            marker in capability_text
            for marker in (
                "추가",
                "토핑",
                "넣어",
                "넣을",
                "더넣",
                "올려",
            )
        )

        topping_question = (
            "토핑" in capability_text
            or (
                mentioned_topping is not None
                and topping_action
            )
        )

        if topping_question:

            if target in BURGER_BASE_PRICE:

                if mentioned_topping is not None:
                    return (
                        f"네, {target_label}에는 "
                        f"{mentioned_topping} 토핑을 "
                        "추가할 수 있어요."
                    )

                return (
                    f"네, {target_label}에는 "
                    "치즈, 베이컨, 패티 토핑을 "
                    "추가할 수 있어요."
                )

            if (
                target in STANDALONE_DRINK_PRICE
                or target in STANDALONE_SIDE_PRICE
            ):
                return (
                    f"{target_label}에는 "
                    "추가 토핑을 적용할 수 없어요."
                )

    # --------------------------------------------------------
    # EXPLICIT CURRENT-UTTERANCE BURGER PRIORITY
    #
    # 현재 문장에 burger가 명시되어 있으면 과거 대화에서
    # 끌려온 Router target보다 현재 발화를 우선한다.
    #
    # 예:
    #   새우버거 얘기 후
    #   "치킨버거 매워?"
    #       -> chicken_burger가 반드시 우선
    # --------------------------------------------------------

    explicit_burger = (
        _explicit_burger_from_utterance(
            utterance
        )
    )

    if explicit_burger is not None:
        target = explicit_burger

    # Router가 이미 해석한 의미를 기반으로
    # 등록된 메뉴 지식을 먼저 조회한다.
    # SHORT GENERIC MENU REPLY
    if (
        family == "info_query"
        and subtype == "menu"
        and target is None
        and domain not in {
            "burger",
            "drink",
            "side",
        }
    ):
        return (
            "햄버거, 사이드, 음료 메뉴를 제공하고 있습니다. "
            "원하시는 종류를 말씀해주시면 자세히 안내해드릴게요."
        )

    knowledge_reply = answer_menu_query(
        family=family,
        subtype=subtype,
        target=target,
        utterance=utterance,
        burger_prices=BURGER_BASE_PRICE,
        criteria=getattr(
            act,
            "criteria",
            None,
        ),
    )

    if knowledge_reply is not None:
        return knowledge_reply

    if family == "info_query":

        if subtype == "menu":
            return _router_menu_reply(
                domain
            )

        if subtype == "price":
            return _router_price_reply(
                act,
                utterance,
                state,
            )

        if subtype == "option":
            return _router_option_reply(
                domain,
                utterance,
            )

        if subtype == "order_state":
            return _router_order_summary(
                state
            )

        if subtype == "possibility":

            compact = re.sub(
                r"\\s+",
                "",
                str(utterance or "").lower(),
            )

            # ------------------------------------------------
            # TOPPING CAPABILITY
            # ------------------------------------------------
            # 메뉴가 존재하는 것과 해당 메뉴에 특정 옵션을
            # 적용할 수 있는 것은 별개의 문제다.
            #
            # 현재 시연 규칙:
            # - burger : cheese / bacon / patty 토핑 가능
            # - drink  : 토핑 불가
            # - side   : 토핑 불가

            topping_action = any(
                marker in compact
                for marker in (
                    "토핑",
                    "추가",
                    "넣어",
                    "넣을",
                    "넣는",
                    "더넣",
                    "올려",
                )
            )

            mentioned_topping = any(
                label in compact
                for label in (
                    "치즈",
                    "베이컨",
                    "패티",
                )
            )

            topping_question = (
                "토핑" in compact
                or (
                    topping_action
                    and mentioned_topping
                )
            )

            if topping_question:

                # 버거에만 추가 토핑 허용
                if target in BURGER_BASE_PRICE:

                    return (
                        "네, 버거에는 치즈, 베이컨, "
                        "패티 토핑을 추가할 수 있어요."
                    )

                # 음료 / 사이드는 토핑 적용 불가
                if (
                    target in STANDALONE_DRINK_PRICE
                    or target in STANDALONE_SIDE_PRICE
                ):

                    label = (
                        DRINK_LABELS.get(target)
                        or SIDE_LABELS.get(target)
                        or "해당 메뉴"
                    )

                    return (
                        f"{label}에는 추가 토핑을 "
                        "적용할 수 없어요."
                    )

            # ------------------------------------------------
            # MENU AVAILABILITY
            # ------------------------------------------------

            if (
                target in BURGER_BASE_PRICE
                or target in STANDALONE_DRINK_PRICE
                or target in STANDALONE_SIDE_PRICE
            ):
                label = (
                    MENU_LABELS.get(target)
                    or DRINK_LABELS.get(target)
                    or SIDE_LABELS.get(target)
                )

                return (
                    f"{label}은 현재 시연 메뉴에 포함되어 있습니다. "
                    "주문하시려면 수량과 함께 말씀해주세요."
                )

            return (
                "해당 메뉴의 주문 가능 여부를 "
                "현재 정보만으로 확정할 수 없습니다."
            )

        if subtype == "stock":
            return (
                "현재 주문 시스템에는 실시간 재고 정보가 "
                "연결되어 있지 않아 재고 수량은 확인할 수 없습니다."
            )

        if subtype == "ingredient":
            return (
                "현재 시스템에는 메뉴별 상세 원재료 정보가 "
                "등록되어 있지 않습니다."
            )

        if subtype == "allergen":
            return (
                "현재 시스템에는 확정된 알레르기 정보가 "
                "등록되어 있지 않습니다. 직원에게 확인해주세요."
            )

        if subtype == "nutrition":
            return (
                "현재 시스템에는 영양성분 정보가 "
                "등록되어 있지 않습니다."
            )

        if subtype == "payment":
            return (
                "결제는 주문 접수 후 다음 단계에서 진행됩니다."
            )

        if subtype == "wait_time":
            return (
                "현재 시스템에는 실시간 예상 대기시간 정보가 "
                "연결되어 있지 않습니다."
            )

        if subtype in {
            "pickup",
            "mobile_order",
        }:
            return (
                "맥오더 또는 앱 주문을 픽업하시는 경우 "
                "주문번호를 말씀해주시면 번호를 확인한 뒤 접수합니다."
            )

        if subtype == "store":
            return (
                "현재 시연 시스템에서는 매장 상세 정보 조회는 "
                "지원하지 않습니다."
            )

    if family == "recommendation":

        if subtype == "budget_recommendation":

            compact = re.sub(
                r"\\s+",
                "",
                str(utterance or ""),
            )

            budget = None

            match = re.search(
                r"(\\d[\\d,]{2,})원",
                compact,
            )

            if match:
                try:
                    budget = int(
                        match.group(1).replace(
                            ",",
                            "",
                        )
                    )
                except ValueError:
                    budget = None

            korean_budget = {
                "삼천원": 3000,
                "사천원": 4000,
                "오천원": 5000,
                "육천원": 6000,
                "칠천원": 7000,
                "팔천원": 8000,
                "구천원": 9000,
                "만원": 10000,
            }

            if budget is None:
                for word, value in korean_budget.items():
                    if word in compact:
                        budget = value
                        break

            if budget is not None:

                candidates = [
                    (
                        MENU_LABELS[key],
                        BURGER_BASE_PRICE[key],
                    )
                    for key in MENU_LABELS
                    if BURGER_BASE_PRICE[key] <= budget
                ]

                if candidates:

                    return (
                        f"예산 {budget:,}원이면 단품 기준 "
                        + ", ".join(
                            f"{label} {price:,}원"
                            for label, price in candidates
                        )
                        + " 중에서 선택할 수 있습니다. "
                        "원하시는 메뉴를 말씀해주세요."
                    )

                return (
                    f"예산 {budget:,}원 이하의 버거 단품은 "
                    "현재 시연 메뉴에 없습니다."
                )

            return (
                "예산 금액을 말씀해주시면 "
                "그 범위에 맞는 메뉴를 추천해드리겠습니다."
            )

        # 명시적으로 특정 burger를 물어본 경우에는
        # 일반 추천을 반복하지 않고 해당 burger를 설명한다.
        explicit_recommendation_target = (
            _explicit_burger_from_utterance(
                utterance
            )
        )

        if (
            explicit_recommendation_target
            in MENU_KNOWLEDGE_BURGERS
        ):
            info = MENU_KNOWLEDGE_BURGERS[
                explicit_recommendation_target
            ]

            label = (
                info.get("name")
                or MENU_LABELS.get(
                    explicit_recommendation_target
                )
                or "해당 버거"
            )

            description = (
                info.get("description")
                or "현재 시연 메뉴입니다"
            )

            calories = info.get(
                "calories"
            )

            price = BURGER_BASE_PRICE.get(
                explicit_recommendation_target
            )

            details = []

            if price is not None:
                details.append(
                    f"단품 {price:,}원"
                )

            if calories is not None:
                details.append(
                    f"{calories} kcal"
                )

            suffix = (
                "이고 "
                + ", ".join(details)
                + "입니다."
                if details
                else "입니다."
            )

            return (
                f"{label}는 {description}"
                + suffix
            )

        # 일반 추천
        return (
            "버거 단품은 "
            + ", ".join(
                f"{MENU_LABELS[key]} "
                f"{BURGER_BASE_PRICE[key]:,}원"
                for key in MENU_LABELS
            )
            + "입니다. 예산이나 선호하는 메뉴 종류를 "
            "말씀해주시면 범위를 좁혀드리겠습니다."
        )

    if family == "conversation_control":

        if subtype == "repeat":
            for item in reversed(
                _router_history[:-1]
            ):
                if item.get("role") == "staff":
                    return (
                        "다시 말씀드리겠습니다. "
                        + item.get(
                            "text",
                            "",
                        )
                    )

            return "다시 말씀해드릴 내용이 없습니다."

        if subtype == "pause":
            return "네, 잠시 기다리겠습니다."

        if subtype == "resume":
            return "네, 계속 주문을 도와드리겠습니다."

    if family == "general_chat":
        return (
            "네. 메뉴나 주문에 관해 말씀해주시면 도와드리겠습니다."
        )

    if family == "out_of_scope":
        return (
            "해당 내용은 현재 주문 시스템에서 처리하기 어렵습니다. "
            "메뉴나 주문 관련 내용을 말씀해주세요."
        )

    return (
        "주문 또는 메뉴 관련 내용을 "
        "조금 더 구체적으로 말씀해주세요."
    )


def router_clarify_reply(
    pending,
):
    if pending is not None:
        return pending_retry_prompt(
            pending
        )

    return (
        "어떤 메뉴나 주문 항목을 말씀하시는지 "
        "조금 더 구체적으로 말씀해주세요."
    )


def show_debug_router(
    output,
    decisions,
):
    print()
    line("=")
    print("DEBUG : ROUTER")
    line("=")

    print(
        json.dumps(
            output.model_dump(
                mode="json",
                exclude_none=True,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )

    print(
        "POLICY:",
        [
            decision.status.value
            for decision in decisions
        ],
    )

    print(
        "MUTATION_ALLOWED:",
        [
            bool(
                decision.mutation_allowed
            )
            for decision in decisions
        ],
    )

    line("=")


def handle_router_event(
    output,
):
    for act in output.acts:
        subtype = _router_value(
            act.subtype
        )

        if subtype == "staff_call":
            ui_request_staff_call()

            system_message(
                "직원 호출 요청"
            )

            soomac_say(
                "직원을 호출하겠습니다. "
                "잠시만 기다려주세요."
            )

            return

        if subtype == "staff_call_cancel":
            system_message(
                "직원 호출 취소 요청"
            )

            soomac_say(
                "직원 호출 취소 요청을 확인했습니다."
            )

            return

    soomac_say(
        "요청을 확인했습니다."
    )




# ============================================================
# CANCEL TARGET FLOW
# ============================================================

def _cancel_compact(text):

    return re.sub(
        r"[\s!?.,~]+",
        "",
        str(text or "").lower(),
    )


def is_vague_cancel_request(text):
    """
    대상이 없는 취소 요청.

    여기서는 절대 주문을 바로 삭제하지 않는다.
    """

    compact = _cancel_compact(text)

    return bool(
        re.fullmatch(
            r"(아)?(그냥)?(주문)?"
            r"취소"
            r"(할게요?|할께요?|할래요?|"
            r"해주세요|해줘요?|해줘|"
            r"할게|할께|할래|요)?",
            compact,
        )
    )


def _cancel_alias_matches(
    text,
    alias_map,
    allowed_keys,
):

    compact = _cancel_compact(
        text
    )

    matches = []

    for key, aliases in (
        alias_map.items()
    ):

        if key not in allowed_keys:
            continue

        for alias in aliases:

            normalized = _cancel_compact(
                alias
            )

            if (
                normalized
                and normalized in compact
            ):
                matches.append(
                    (
                        len(normalized),
                        key,
                    )
                )

    if not matches:
        return set()

    # "제로콜라"가 있을 때
    # 내부의 "콜라"까지 같이 잡히지 않도록
    # 가장 긴 alias를 우선한다.
    max_len = max(
        length
        for length, _ in matches
    )

    return {
        key
        for length, key in matches
        if length == max_len
    }


def _cancel_inventory(state):
    """
    현재 주문에 실제로 존재하는 취소 가능 대상 목록.
    """

    result = []

    items = (
        state.get(
            "items",
            [],
        )
        if isinstance(
            state,
            dict,
        )
        else []
    )

    for index, item in enumerate(
        items,
        start=1,
    ):

        if not isinstance(
            item,
            dict,
        ):
            continue

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

            if menu in MENU_LABELS:

                result.append(
                    {
                        "kind": "menu",
                        "key": menu,
                        "label": MENU_LABELS[
                            menu
                        ],
                        "line": index,
                        "source": "line",
                    }
                )

            # 세트 내부 음료
            drink = item.get(
                "drink"
            )

            if drink in DRINK_LABELS:

                result.append(
                    {
                        "kind": "drink",
                        "key": drink,
                        "label": DRINK_LABELS[
                            drink
                        ],
                        "line": index,
                        "source": "set",
                    }
                )

            # 세트 내부 사이드
            side = item.get(
                "side"
            )

            if side in SIDE_LABELS:

                result.append(
                    {
                        "kind": "side",
                        "key": side,
                        "label": SIDE_LABELS[
                            side
                        ],
                        "line": index,
                        "source": "set",
                    }
                )

            # 추가 토핑
            for topping in (
                item.get(
                    "add_toppings",
                    [],
                )
                or []
            ):

                if topping in TOPPING_LABELS:

                    result.append(
                        {
                            "kind": "topping",
                            "key": topping,
                            "label": TOPPING_LABELS[
                                topping
                            ],
                            "line": index,
                            "source": "burger",
                        }
                    )

            # 제외한 기본 재료
            for ingredient in (
                item.get(
                    "exclude",
                    [],
                )
                or []
            ):

                if ingredient in EXCLUDE_LABELS:

                    result.append(
                        {
                            "kind": "exclude",
                            "key": ingredient,
                            "label": EXCLUDE_LABELS[
                                ingredient
                            ],
                            "line": index,
                            "source": "burger",
                        }
                    )

        # ----------------------------------------------------
        # STANDALONE DRINK
        # ----------------------------------------------------

        elif item_type == "drink":

            drink = item.get(
                "drink"
            )

            if drink in DRINK_LABELS:

                result.append(
                    {
                        "kind": "drink",
                        "key": drink,
                        "label": DRINK_LABELS[
                            drink
                        ],
                        "line": index,
                        "source": "standalone",
                    }
                )

        # ----------------------------------------------------
        # STANDALONE SIDE
        # ----------------------------------------------------

        elif item_type == "side":

            side = item.get(
                "side"
            )

            if side in SIDE_LABELS:

                result.append(
                    {
                        "kind": "side",
                        "key": side,
                        "label": SIDE_LABELS[
                            side
                        ],
                        "line": index,
                        "source": "standalone",
                    }
                )

    return result


def _cancel_category_reply(
    candidates,
    category_name,
):

    if not candidates:
        return (
            f"현재 주문에 취소할 "
            f"{category_name}이 없어요. "
            "취소할 대상을 다시 말씀해주세요."
        )

    labels = [
        item["label"]
        for item in candidates
    ]

    # 중복 line
    if len(
        set(labels)
    ) != len(labels):

        return (
            f"같은 {category_name}이 "
            "여러 개 있어요. "
            "몇 번째 항목인지 말씀해주세요."
        )

    return (
        f"어떤 {category_name}을 "
        "취소하시겠어요? "
        + ", ".join(labels)
        + " 중에서 말씀해주세요."
    )


def _cancel_candidate_command(
    candidate,
):
    """
    실제 State 수정은 여기서 하지 않는다.

    취소 대상을 명확한 자연어 명령으로 바꿔서
    기존 Router -> Runtime mutation 경로에 보낸다.
    """

    kind = candidate[
        "kind"
    ]

    label = candidate[
        "label"
    ]

    source = candidate[
        "source"
    ]

    # --------------------------------------------------------
    # MENU
    # --------------------------------------------------------

    if kind == "menu":

        return {
            "kind": "command",
            "text": (
                f"현재 주문에서 "
                f"{label} 주문을 취소해주세요"
            ),
        }

    # --------------------------------------------------------
    # DRINK
    # --------------------------------------------------------

    if kind == "drink":

        # 세트 구성요소를 그냥 None으로 만들어서는 안 된다.
        if source == "set":

            return {
                "kind": "reply",
                "text": (
                    f"{label}은 현재 세트에 포함된 "
                    "음료예요. "
                    "다른 음료로 바꾸시거나 "
                    "세트를 단품으로 변경해주세요."
                ),
            }

        return {
            "kind": "command",
            "text": (
                f"현재 주문에서 "
                f"{label} 주문을 취소해주세요"
            ),
        }

    # --------------------------------------------------------
    # SIDE
    # --------------------------------------------------------

    if kind == "side":

        if source == "set":

            return {
                "kind": "reply",
                "text": (
                    f"{label}은 현재 세트에 포함된 "
                    "사이드예요. "
                    "다른 사이드로 바꾸시거나 "
                    "세트를 단품으로 변경해주세요."
                ),
            }

        return {
            "kind": "command",
            "text": (
                f"현재 주문에서 "
                f"{label} 주문을 취소해주세요"
            ),
        }

    # --------------------------------------------------------
    # ADDED TOPPING
    # --------------------------------------------------------

    if kind == "topping":

        return {
            "kind": "command",
            "text": (
                f"현재 주문에서 추가한 "
                f"{label} 토핑을 빼주세요"
            ),
        }

    # --------------------------------------------------------
    # EXCLUDED INGREDIENT
    # --------------------------------------------------------

    if kind == "exclude":

        return {
            "kind": "command",
            "text": (
                f"현재 주문에서 "
                f"{label} 제외를 취소하고 "
                f"{label}을 다시 넣어주세요"
            ),
        }

    return {
        "kind": "reply",
        "text": (
            "취소할 대상을 다시 말씀해주세요."
        ),
    }


def resolve_pending_cancel_target(
    text,
    state,
):
    """
    '취소할게'
        ↓
    '어떤 걸 취소하시겠어요?'
        ↓
    이 함수가 다음 고객 발화를 해석한다.
    """

    compact = _cancel_compact(
        text
    )

    inventory = _cancel_inventory(
        state
    )

    # --------------------------------------------------------
    # 취소 작업 자체를 포기
    # --------------------------------------------------------

    if compact in {
        "아니",
        "아니요",
        "됐어",
        "됐어요",
        "괜찮아",
        "괜찮아요",
        "취소안할게",
        "취소안할게요",
        "그냥둘게",
        "그냥둘게요",
    }:

        return {
            "kind": "abort",
            "text": "알겠습니다.",
        }

    # --------------------------------------------------------
    # 전체 주문
    # --------------------------------------------------------

    if compact in {
        "전체",
        "전체주문",
        "전부",
        "전부다",
        "모두",
        "다",
        "주문전체",
        "주문전부",
    }:

        return {
            "kind": "command",
            "text": (
                "현재 주문 전체를 "
                "취소해주세요"
            ),
        }

    # --------------------------------------------------------
    # 실제 현재 주문에서 가능한 canonical 목록
    # --------------------------------------------------------

    menu_keys = {
        x["key"]
        for x in inventory
        if x["kind"] == "menu"
    }

    drink_keys = {
        x["key"]
        for x in inventory
        if x["kind"] == "drink"
    }

    side_keys = {
        x["key"]
        for x in inventory
        if x["kind"] == "side"
    }

    topping_keys = {
        x["key"]
        for x in inventory
        if x["kind"] == "topping"
    }

    exclude_keys = {
        x["key"]
        for x in inventory
        if x["kind"] == "exclude"
    }

    # --------------------------------------------------------
    # 구체적인 메뉴/음료/사이드 이름
    # --------------------------------------------------------

    menu_matches = (
        _cancel_alias_matches(
            text,
            MENU_ALIASES,
            menu_keys,
        )
    )

    drink_matches = (
        _cancel_alias_matches(
            text,
            DRINK_ALIASES,
            drink_keys,
        )
    )

    side_matches = (
        _cancel_alias_matches(
            text,
            SIDE_ALIASES,
            side_keys,
        )
    )

    # --------------------------------------------------------
    # topping / exclude
    # --------------------------------------------------------

    topping_matches = set()

    for key in topping_keys:

        label = TOPPING_LABELS.get(
            key,
            key,
        )

        if _cancel_compact(label) in compact:
            topping_matches.add(
                key
            )

    exclude_matches = set()

    for key in exclude_keys:

        label = EXCLUDE_LABELS.get(
            key,
            key,
        )

        if _cancel_compact(label) in compact:
            exclude_matches.add(
                key
            )

    explicit_candidates = []

    for item in inventory:

        key = item["key"]
        kind = item["kind"]

        matched = (
            (
                kind == "menu"
                and key in menu_matches
            )
            or (
                kind == "drink"
                and key in drink_matches
            )
            or (
                kind == "side"
                and key in side_matches
            )
            or (
                kind == "topping"
                and key in topping_matches
            )
            or (
                kind == "exclude"
                and key in exclude_matches
            )
        )

        if matched:
            explicit_candidates.append(
                item
            )

    # --------------------------------------------------------
    # 같은 단어가 topping/exclude 둘 다일 수 있다.
    # 예: 치즈
    # --------------------------------------------------------

    kinds = {
        item["kind"]
        for item in explicit_candidates
    }

    if (
        "topping" in kinds
        and
        "exclude" in kinds
    ):

        return {
            "kind": "reply",
            "text": (
                "추가한 토핑을 취소하실 건가요, "
                "빼달라고 한 재료를 다시 넣으실 건가요?"
            ),
        }

    if len(
        explicit_candidates
    ) == 1:

        return _cancel_candidate_command(
            explicit_candidates[0]
        )

    if len(
        explicit_candidates
    ) > 1:

        labels = [
            item["label"]
            for item
            in explicit_candidates
        ]

        if len(
            set(labels)
        ) == 1:

            return {
                "kind": "reply",
                "text": (
                    f"{labels[0]}이 "
                    "여러 주문에 있어요. "
                    "몇 번째 항목인지 말씀해주세요."
                ),
            }

        return {
            "kind": "reply",
            "text": (
                "취소할 대상이 여러 개로 보여요. "
                "조금 더 구체적으로 말씀해주세요."
            ),
        }

    # --------------------------------------------------------
    # CATEGORY
    # --------------------------------------------------------

    if compact in {
        "메뉴",
        "버거",
        "햄버거",
        "버거메뉴",
    }:

        candidates = [
            x
            for x in inventory
            if x["kind"] == "menu"
        ]

        if len(candidates) == 1:
            return _cancel_candidate_command(
                candidates[0]
            )

        return {
            "kind": "reply",
            "text": _cancel_category_reply(
                candidates,
                "메뉴",
            ),
        }

    if compact in {
        "음료",
        "음료수",
        "드링크",
    }:

        candidates = [
            x
            for x in inventory
            if x["kind"] == "drink"
        ]

        if len(candidates) == 1:
            return _cancel_candidate_command(
                candidates[0]
            )

        return {
            "kind": "reply",
            "text": _cancel_category_reply(
                candidates,
                "음료",
            ),
        }

    if compact in {
        "사이드",
        "사이드메뉴",
    }:

        candidates = [
            x
            for x in inventory
            if x["kind"] == "side"
        ]

        if len(candidates) == 1:
            return _cancel_candidate_command(
                candidates[0]
            )

        return {
            "kind": "reply",
            "text": _cancel_category_reply(
                candidates,
                "사이드",
            ),
        }

    if compact in {
        "토핑",
        "추가토핑",
        "추가한토핑",
    }:

        candidates = [
            x
            for x in inventory
            if x["kind"] == "topping"
        ]

        if len(candidates) == 1:
            return _cancel_candidate_command(
                candidates[0]
            )

        return {
            "kind": "reply",
            "text": _cancel_category_reply(
                candidates,
                "추가 토핑",
            ),
        }

    if compact in {
        "제외",
        "제외재료",
        "제외한재료",
        "제외한거",
        "뺀거",
        "뺀재료",
        "빼달라고한거",
    }:

        candidates = [
            x
            for x in inventory
            if x["kind"] == "exclude"
        ]

        if len(candidates) == 1:
            return _cancel_candidate_command(
                candidates[0]
            )

        return {
            "kind": "reply",
            "text": _cancel_category_reply(
                candidates,
                "제외한 재료",
            ),
        }

    return {
        "kind": "reply",
        "text": (
            "현재 주문에서 그 대상을 찾지 못했어요. "
            "취소할 대상을 다시 말씀해주세요."
        ),
    }




def menu_category_followup_reply(text):
    """
    직전 STAFF가 전체 메뉴 카테고리 선택을 요청한 경우에만
    '햄버거요 / 사이드요 / 음료요' 같은 짧은 답을
    주문이 아니라 메뉴 상세 요청으로 처리한다.
    """

    history = router_history_before_current_customer()

    if not history:
        return None

    previous = history[-1]

    if previous.get("role") != "staff":
        return None

    previous_text = str(
        previous.get("text") or ""
    )

    if not (
        "햄버거, 사이드, 음료 메뉴를 제공하고 있습니다"
        in previous_text
        and
        "원하시는 종류"
        in previous_text
    ):
        return None

    compact = re.sub(
        r"\s+",
        "",
        str(text or ""),
    )

    # MENU CATEGORY FOLLOWUP FASTPATH

    if compact in {
        "햄버거",
        "햄버거요",
        "버거",
        "버거요",
    }:
        return (
            "버거 메뉴는 "
            + ", ".join(
                MENU_LABELS.values()
            )
            + "입니다."
        )

    if compact in {
        "음료",
        "음료요",
        "드링크",
        "드링크요",
    }:
        return (
            "음료 메뉴는 "
            + ", ".join(
                DRINK_LABELS.values()
            )
            + "입니다."
        )

    if compact in {
        "사이드",
        "사이드요",
    }:
        return (
            "사이드 메뉴는 "
            + ", ".join(
                SIDE_LABELS.values()
            )
            + "입니다."
        )

    return None


def pre_router_current_order_query_reply(
    text,
    state,
):
    """
    현재 주문 내용을 묻는 자연스러운 표현을
    Router에 보내지 않고 read-only로 바로 응답한다.
    """

    compact = re.sub(
        r"[\s!?.,~]+",
        "",
        str(text or "").lower(),
    )

    patterns = (
        r"(?:나)?(?:메뉴)?지금(?:뭐|머)"
        r"(?:시켰|시켯|시킴|주문했|주문햇|주문함|담았|담앗|담음)"
        r"(?:지)?(?:나)?$",

        r"(?:나)?(?:뭐|머)"
        r"(?:시켰|시켯|시킴|주문했|주문햇|주문함|담았|담앗|담음)"
        r"(?:지)?(?:나)?$",

        r"(?:현재|지금|내)?주문(?:내역)?"
        r"(?:뭐|머)(?:야|지|임|있어|있나요)?$",

        r"(?:내가|나)?시킨거"
        r"(?:뭐|머)(?:야|지)?$",

        r"(?:현재|지금|내)?주문내역"
        r"(?:알려줘|알려주세요|보여줘|보여주세요|확인해줘|확인해주세요)$",
    )

    if not any(
        re.fullmatch(pattern, compact)
        for pattern in patterns
    ):
        return None

    # CURRENT ORDER QUERY PRE-ROUTER
    return _router_order_summary(
        state
    )


def generic_burger_menu_query_reply(text):
    """
    '햄버거 뭐 있어?', '버거 종류 알려줘'처럼
    generic burger category 자체를 묻는 경우 처리한다.

    '햄버거 주세요' 같은 주문 요청은 처리하지 않는다.
    """

    compact = re.sub(
        r"[\s!?.,~]+",
        "",
        str(text or "").lower(),
    )

    if not (
        compact.startswith("햄버거")
        or compact.startswith("버거")
    ):
        return None

    query_signals = (
        "뭐있",
        "머있",
        "종류알려",
        "메뉴알려",
        "종류보여",
        "메뉴보여",
        "어떤거있",
        "어떤게있",
        "어떤메뉴",
        "종류가뭐",
        "종류가머",
    )

    if not any(
        signal in compact
        for signal in query_signals
    ):
        return None

    # GENERIC BURGER MENU QUERY FASTPATH
    return (
        "버거 메뉴는 "
        + ", ".join(
            MENU_LABELS.values()
        )
        + "입니다."
    )


# THREE UX FIXES 20261007

def pre_router_burger_set_price_reply(text):
    """
    '불고기버거 세트 얼마예요?' 같은 질문.
    기존 single_info가 단품 가격만 반환하기 전에 처리한다.
    """

    compact = re.sub(
        r"[\s!?.,~]+",
        "",
        str(text or "").lower(),
    )

    if "세트" not in compact:
        return None

    if not any(
        signal in compact
        for signal in (
            "얼마",
            "가격",
            "금액",
            "추가금",
            "추가비",
        )
    ):
        return None

    found = []

    for key, label in MENU_LABELS.items():
        if label in compact:
            found.append(key)

    if len(found) != 1:
        return None

    menu = found[0]

    price = (
        BURGER_BASE_PRICE[menu]
        + SET_UPCHARGE
    )

    return (
        f"{MENU_LABELS[menu]} 기본 세트 가격은 "
        f"{price:,}원입니다. "
        "음료, 사이즈, 사이드 선택에 따라 "
        "추가금이 붙을 수 있습니다."
    )


def pre_router_generic_category_order_reply(text):
    """
    '햄버거 주세요', '음료 주세요', '사이드 주실래요?'처럼
    구체적인 상품 없이 카테고리만 주문하는 경우
    임의 메뉴 mutation을 막고 선택을 요청한다.
    """

    compact = re.sub(
        r"[\s!?.,~]+",
        "",
        str(text or "").lower(),
    )

    request_signals = (
        "주세요",
        "주실래요",
        "주실래",
        "주시겠어요",
        "줄래요",
        "줄래",
        "주라",
        "줘라",
        "줘요",
        "줘",
        "주문할게",
        "주문할래",
    )

    if not any(
        signal in compact
        for signal in request_signals
    ):
        return None

    # --------------------------------------------------------
    # 실제 구체 메뉴가 있으면 기존 주문 로직으로 보낸다.
    # --------------------------------------------------------

    known_menu_words = (
        tuple(MENU_LABELS.values())
        + tuple(DRINK_LABELS.values())
        + tuple(SIDE_LABELS.values())
    )

    if any(
        word in compact
        for word in known_menu_words
    ):
        return None

    # --------------------------------------------------------
    # generic category only
    # --------------------------------------------------------

    if compact.startswith("햄버거") or compact.startswith("버거"):
        return (
            "어떤 버거를 주문하시겠어요? "
            + ", ".join(MENU_LABELS.values())
            + "가 있습니다."
        )

    if compact.startswith("음료"):
        return (
            "어떤 음료를 주문하시겠어요? "
            + ", ".join(DRINK_LABELS.values())
            + "가 있습니다."
        )

    if compact.startswith("사이드"):
        return (
            "어떤 사이드 메뉴를 주문하시겠어요? "
            + ", ".join(SIDE_LABELS.values())
            + "가 있습니다."
        )

    return None


def all_burger_price_query_reply(text):
    """
    버거 전체의 가격을 묻는 경우.
    특정 버거 하나의 가격 질문은 기존 single_info에 맡긴다.
    """

    compact = re.sub(
        r"[\s!?.,~]+",
        "",
        str(text or "").lower(),
    )

    # 특정 버거 이름이 있으면 전체 가격 질문이 아님
    if any(
        label in compact
        for label in MENU_LABELS.values()
    ):
        return None

    if not (
        "버거" in compact
        or "햄버거" in compact
    ):
        return None

    if not any(
        signal in compact
        for signal in (
            "얼마",
            "가격",
            "금액",
        )
    ):
        return None

    # 전체/복수 의미
    if not (
        "각각" in compact
        or "버거들" in compact
        or "햄버거들" in compact
        or "버거가격" in compact
        or "햄버거가격" in compact
    ):
        return None

    # ALL BURGER PRICE QUERY FASTPATH
    return (
        "불고기버거는 4,500원, "
        "치킨버거는 4,800원, "
        "치즈버거는 5,000원, "
        "새우버거는 5,200원입니다."
    )


def all_category_price_query_reply(text):
    """
    카테고리 전체 가격 질문 처리.

    예:
      버거들 가격 알려줘
      햄버거 각각 얼마야
      사이드 각각 얼마야
      사이드 가격 알려줘
      음료들은 얼마야
      음료 가격 알려줘

    특정 메뉴 하나의 가격 질문은 기존 single_info에 맡긴다.
    """

    compact = re.sub(
        r"[\s!?.,~]+",
        "",
        str(text or "").lower(),
    )

    if not any(
        signal in compact
        for signal in (
            "얼마",
            "가격",
            "금액",
            "추가금",
            "추가비",
        )
    ):
        return None

    # 세트 음료를 어떤 것으로 바꿀 때 붙는 추가금만 간단히 안내한다.
    # 변경 가격 질문을 음료별 스몰/미디엄/라지 단품 가격으로 오해하지 않는다.
    if (
        "음료" in compact
        and not ("사이즈" in compact and "사이드" in compact)
        and any(
            signal in compact
            for signal in (
                "변경",
                "바꾸",
                "바꿔",
                "추가금",
                "추가비",
            )
        )
    ):
        free_drinks = [
            DRINK_LABELS[key]
            for key, fee in SET_DRINK_UPCHARGE.items()
            if fee == 0
        ]
        paid_drinks = [
            f"{DRINK_LABELS[key]} +{fee:,}원"
            for key, fee in SET_DRINK_UPCHARGE.items()
            if fee > 0
        ]
        return (
            "세트 음료 변경은 "
            + ", ".join(free_drinks)
            + " 추가금 없음, "
            + ", ".join(paid_drinks)
            + "입니다."
        )

    # 세트 업그레이드 추가금은 일반 음료/사이드 단품 가격과 다르다.
    # 예: "음료 사이즈랑 사이드 변경은 각각 얼마예요?"
    if (
        "사이즈" in compact
        and "사이드" in compact
        and any(
            signal in compact
            for signal in (
                "변경",
                "추가금",
                "추가비",
                "더내",
                "얼마",
                "가격",
            )
        )
    ):
        return (
            "세트 음료 사이즈 변경은 스몰 추가금 없음, "
            f"미디엄 +{DRINK_SIZE_UPCHARGE['medium']:,}원, "
            f"라지 +{DRINK_SIZE_UPCHARGE['large']:,}원입니다. "
            "사이드는 감자튀김 그대로면 추가금이 없고, "
            f"치즈스틱으로 변경하면 +{SET_SIDE_UPCHARGE['cheese_stick']:,}원입니다. "
            f"세트 음료는 아이스커피 변경 시 +{SET_DRINK_UPCHARGE['iced_coffee']:,}원, "
            "그 외 음료는 추가금이 없습니다."
        )

    # --------------------------------------------------------
    # 특정 메뉴 하나를 명시했으면 기존 single_info로 보낸다.
    # --------------------------------------------------------

    specific_labels = (
        tuple(MENU_LABELS.values())
        + tuple(DRINK_LABELS.values())
        + tuple(SIDE_LABELS.values())
    )

    if any(
        label in compact
        for label in specific_labels
    ):
        return None


    # ========================================================
    # BURGER
    # ========================================================

    if (
        "햄버거" in compact
        or "버거" in compact
    ):
        parts = [
            f"{MENU_LABELS[key]}는 "
            f"{BURGER_BASE_PRICE[key]:,}원"
            for key in MENU_LABELS
        ]

        # ALL CATEGORY PRICE QUERY FASTPATH
        return (
            ", ".join(parts)
            + "입니다."
        )


    # ========================================================
    # SIDE
    # ========================================================

    if "사이드" in compact:
        parts = [
            f"{SIDE_LABELS[key]}은 "
            f"{STANDALONE_SIDE_PRICE[key]:,}원"
            for key in SIDE_LABELS
        ]

        return (
            ", ".join(parts)
            + "입니다."
        )


    # ========================================================
    # DRINK
    # ========================================================

    if "음료" in compact or "드링크" in compact:

        parts = []

        for key in DRINK_LABELS:

            small = (
                STANDALONE_DRINK_PRICE[key]
            )

            medium = (
                small
                + DRINK_SIZE_UPCHARGE["medium"]
            )

            large = (
                small
                + DRINK_SIZE_UPCHARGE["large"]
            )

            parts.append(
                f"{DRINK_LABELS[key]}는 "
                f"스몰 {small:,}원, "
                f"미디엄 {medium:,}원, "
                f"라지 {large:,}원"
            )

        return (
            ". ".join(parts)
            + "입니다."
        )

    return None


def contextual_category_price_query_reply(text):
    """
    직전 STAFF가 특정 카테고리 메뉴를 안내한 직후

      "각각 얼마야?"
      "그럼 가격은?"
      "각각 가격 알려줘"

    같은 생략형 질문이 오면 직전 카테고리를 이어받는다.

    오래된 history는 보지 않고 바로 직전 STAFF만 사용한다.
    """

    compact = re.sub(
        r"[\s!?.,~]+",
        "",
        str(text or "").lower(),
    )

    if not any(
        signal in compact
        for signal in (
            "얼마",
            "가격",
            "금액",
        )
    ):
        return None

    # 현재 발화에 이미 카테고리가 있으면
    # 기존 all_category_price_query_reply()가 처리한다.
    if any(
        word in compact
        for word in (
            "버거",
            "햄버거",
            "사이드",
            "음료",
            "드링크",
        )
    ):
        return None

    # 특정 메뉴 하나를 직접 말했으면
    # 기존 single_info에 맡긴다.
    specific_labels = (
        tuple(MENU_LABELS.values())
        + tuple(DRINK_LABELS.values())
        + tuple(SIDE_LABELS.values())
    )

    if any(
        label in compact
        for label in specific_labels
    ):
        return None

    history = (
        router_history_before_current_customer()
    )

    if not history:
        return None

    previous = history[-1]

    if previous.get("role") != "staff":
        return None

    previous_text = str(
        previous.get("text") or ""
    ).strip()

    category = None

    # CONTEXTUAL CATEGORY PRICE FOLLOWUP

    if (
        previous_text.startswith("버거 메뉴는")
        or previous_text.startswith("햄버거 메뉴는")
    ):
        category = "버거"

    elif (
        previous_text.startswith("사이드 메뉴는")
        or previous_text.startswith("사이드는")
    ):
        category = "사이드"

    elif (
        previous_text.startswith("음료 메뉴는")
        or previous_text.startswith("음료는")
    ):
        category = "음료"

    if category is None:
        return None

    return all_category_price_query_reply(
        f"{category} {text}"
    )


# TOPPING TARGET FOLLOWUP REWRITE

def rewrite_topping_target_followup(
    text,
    state,
):
    result = (
        _resolve_pending_modifier_target(
            text,
            state,
        )
    )

    if result is None:
        return None

    if result.get("mode") != "topping":
        return None

    if result["status"] != "resolved":
        return None

    label = result[
        "modifier_label"
    ]

    clauses = []

    for item in result["items"]:

        line_id = item.get(
            "line_id"
        )

        if line_id is None:
            return None

        # 메뉴명을 다시 넣지 않는다.
        # "치즈버거에 치즈 추가"처럼
        # menu/topping 이름이 겹치는 ambiguity 방지.
        clauses.append(
            f"주문 {line_id}번 항목에 "
            f"토핑으로 {label}를 "
            f"추가해주세요"
        )

    return " 그리고 ".join(
        clauses
    )


# EXCLUDE TARGET FOLLOWUP 20261007

def burger_exclude_state_guard_reply(
    router_output,
    utterance,
    state,
):
    burgers = (
        _burger_target_items(
            state
        )
    )

    if len(burgers) <= 1:
        return None

    compact = (
        _burger_target_compact(
            utterance
        )
    )

    if not any(
        signal in compact
        for signal in (
            "빼",
            "제외",
            "없이",
        )
    ):
        return None

    # 지원되는 제외 재료인지 고객 발화 자체로 확인
    ingredient_key = None

    for key, label in EXCLUDE_LABELS.items():

        if label in compact:
            ingredient_key = key
            break

    if ingredient_key is None:
        return None

    # --------------------------------------------------------
    # Router의 line_ids=[1,2,3,4] 같은 후보 해석은
    # 명시적 고객 선택으로 간주하지 않는다.
    # 고객 발화 자체에서 target을 찾는다.
    # --------------------------------------------------------

    result = (
        resolve_burger_target_reference(
            utterance,
            state,
        )
    )

    if result["status"] == "resolved":
        return None

    if result["status"] == "ambiguous":

        items = result["items"]

        descriptions = [
            _burger_target_description(
                item
            )
            for item in items
        ]

        if (
            descriptions
            and
            len(set(descriptions)) == 1
        ):

            return (
                f"같은 {descriptions[0]} 주문이 "
                f"{len(items)}개 있습니다. "
                "전부인지, 하나만인지, 특정 하나인지 말씀해주세요."
            )

    return (
        "어느 버거에서 빼드릴지 말씀해주세요. "
        "메뉴 이름이나 단품·세트, 순서, 또는 전부라고 말씀하셔도 됩니다."
    )


def rewrite_exclude_target_followup(
    text,
    state,
):
    result = (
        _resolve_pending_modifier_target(
            text,
            state,
        )
    )

    if result is None:
        return None

    if result.get("mode") != "exclude":
        return None

    if result["status"] != "resolved":
        return None

    label = result[
        "modifier_label"
    ]

    clauses = []

    for item in result["items"]:

        line_id = item.get(
            "line_id"
        )

        if line_id is None:
            return None

        # 메뉴명 대신 line_id + ingredient domain을 명시.
        clauses.append(
            f"주문 {line_id}번 항목에서 "
            f"재료 {label}를 "
            f"제외해주세요"
        )

    return " 그리고 ".join(
        clauses
    )

# GENERIC BURGER TARGET RESOLVER 20261007

def _burger_target_items(state):
    items = (
        state.get("items", [])
        if isinstance(state, dict)
        else []
    )

    burgers = [
        item
        for item in items
        if item.get("item_type") == "burger"
    ]

    burgers.sort(
        key=lambda item: int(
            item.get("line_id", 0)
            or 0
        )
    )

    return burgers


def _burger_target_compact(text):
    return re.sub(
        r"[\s!?.,~]+",
        "",
        str(text or "").lower(),
    )


def _burger_target_all_signal(text):
    compact = _burger_target_compact(text)

    return any(
        token in compact
        for token in (
            "전부",
            "모두",
            "둘다",
            "두개다",
            "세개다",
            "네개다",
            "다요",
            "다빼",
            "다추가",
        )
    )


def _burger_target_description(item):
    menu = MENU_LABELS.get(
        item.get("menu"),
        "버거",
    )

    order_type = TYPE_LABELS.get(
        item.get("type"),
        "",
    )

    result = (
        f"{menu} {order_type}"
    ).strip()

    if item.get("type") == "set":

        drink = DRINK_LABELS.get(
            item.get("drink"),
            None,
        )

        size = SIZE_LABELS.get(
            item.get("drink_size"),
            None,
        )

        side = SIDE_LABELS.get(
            item.get("side"),
            None,
        )

        options = [
            x
            for x in (
                drink,
                size,
                side,
            )
            if x
        ]

        if options:
            result += (
                " (" +
                ", ".join(options) +
                ")"
            )

    return result


def _detect_mapping_keys(
    compact,
    labels,
    aliases=None,
):
    """
    긴 표현부터 잡는다.
    예: 제로콜라가 콜라로 중복 인식되지 않게 함.
    """

    candidates = []

    for key, label in labels.items():
        token = _burger_target_compact(label)

        if token:
            candidates.append(
                (
                    len(token),
                    token,
                    key,
                )
            )

    if aliases:

        for key, values in aliases.items():

            for value in (
                values
                if isinstance(
                    values,
                    (list, tuple, set),
                )
                else [values]
            ):
                token = _burger_target_compact(
                    value
                )

                if token:
                    candidates.append(
                        (
                            len(token),
                            token,
                            key,
                        )
                    )

    found = []
    work = compact

    for _, token, key in sorted(
        candidates,
        reverse=True,
    ):

        if token not in work:
            continue

        if key not in found:
            found.append(key)

        work = work.replace(
            token,
            "",
            1,
        )

    return found


def _resolve_burger_target_reference_base(
    text,
    state,
):
    """
    손님이 버거를 가리키는 다양한 방식 지원.

    예:
      두번째
      두번째랑 네번째
      3번
      마지막
      전부
      치즈버거
      치즈버거 단품
      불고기버거 세트
      불고기버거 세트 둘 다
      콜라 들어간 거
      치즈스틱 들어간 세트
    """

    burgers = _burger_target_items(
        state
    )

    if not burgers:
        return {
            "status": "none",
            "items": [],
        }

    compact = _burger_target_compact(
        text
    )

    selected = []

    # ========================================================
    # 1. 숫자 n번
    #
    # line_id가 실제로 존재하면 line_id 우선.
    # 없으면 n번째 burger로 해석.
    # ========================================================

    numeric_refs = [
        int(match.group(1))
        for match in re.finditer(
            r"(\d+)번",
            compact,
        )
    ]

    for number in numeric_refs:

        by_line = next(
            (
                item
                for item in burgers
                if int(
                    item.get("line_id", -1)
                    or -1
                ) == number
            ),
            None,
        )

        if by_line is not None:

            if by_line not in selected:
                selected.append(by_line)

            continue

        if 1 <= number <= len(burgers):

            item = burgers[
                number - 1
            ]

            if item not in selected:
                selected.append(item)

    # ========================================================
    # 2. 한글 순번
    # ========================================================

    ordinal_patterns = (
        (1, ("첫번째", "첫째")),
        (2, ("두번째", "둘째")),
        (3, ("세번째", "셋째")),
        (4, ("네번째", "넷째")),
        (5, ("다섯번째", "다섯째")),
        (6, ("여섯번째", "여섯째")),
        (7, ("일곱번째", "일곱째")),
        (8, ("여덟번째", "여덟째")),
        (9, ("아홉번째", "아홉째")),
        (10, ("열번째", "열째")),
    )

    for position, patterns in ordinal_patterns:

        if not any(
            pattern in compact
            for pattern in patterns
        ):
            continue

        if (
            1
            <= position
            <= len(burgers)
        ):

            item = burgers[
                position - 1
            ]

            if item not in selected:
                selected.append(item)

    if "마지막" in compact:

        item = burgers[-1]

        if item not in selected:
            selected.append(item)

    # 순번이 명시됐으면 가장 강한 reference로 사용
    if selected:
        return {
            "status": "resolved",
            "items": selected,
        }

    # ========================================================
    # 3. 전부만 말한 경우
    # ========================================================

    all_signal = (
        _burger_target_all_signal(
            text
        )
    )

    # ========================================================
    # 4. 메뉴 / 단품세트 / 음료 / 사이즈 / 사이드
    # ========================================================

    menu_keys = _detect_mapping_keys(
        compact,
        MENU_LABELS,
        MENU_ALIASES,
    )

    type_keys = _detect_mapping_keys(
        compact,
        TYPE_LABELS,
        TYPE_ALIASES,
    )

    drink_keys = _detect_mapping_keys(
        compact,
        DRINK_LABELS,
        DRINK_ALIASES,
    )

    size_keys = _detect_mapping_keys(
        compact,
        SIZE_LABELS,
        SIZE_ALIASES,
    )

    side_keys = _detect_mapping_keys(
        compact,
        SIDE_LABELS,
        SIDE_ALIASES,
    )

    has_semantic_reference = any(
        (
            menu_keys,
            type_keys,
            drink_keys,
            size_keys,
            side_keys,
        )
    )

    if (
        all_signal
        and not has_semantic_reference
    ):
        return {
            "status": "resolved",
            "items": burgers,
        }

    if not has_semantic_reference:
        return {
            "status": "none",
            "items": [],
        }

    matches = []

    for item in burgers:

        if (
            menu_keys
            and item.get("menu")
            not in menu_keys
        ):
            continue

        if (
            type_keys
            and item.get("type")
            not in type_keys
        ):
            continue

        if (
            drink_keys
            and item.get("drink")
            not in drink_keys
        ):
            continue

        if (
            size_keys
            and item.get("drink_size")
            not in size_keys
        ):
            continue

        if (
            side_keys
            and item.get("side")
            not in side_keys
        ):
            continue

        matches.append(item)

    if not matches:
        return {
            "status": "not_found",
            "items": [],
        }

    if len(matches) == 1:
        return {
            "status": "resolved",
            "items": matches,
        }

    # "불고기버거 세트 둘 다"
    if all_signal:
        return {
            "status": "resolved",
            "items": matches,
        }

    return {
        "status": "ambiguous",
        "items": matches,
    }

# RICH MODIFIER TARGET RESOLVER 20261007

def _modifier_item_signature(item):
    return (
        item.get("menu"),
        item.get("type"),
        item.get("drink"),
        item.get("drink_size"),
        item.get("side"),
        tuple(sorted(item.get("exclude") or [])),
        tuple(sorted(item.get("add_toppings") or [])),
    )


def _modifier_same_variant(items):
    if not items:
        return False

    return (
        len({
            _modifier_item_signature(item)
            for item in items
        }) == 1
    )


def _modifier_count_value(text):
    compact = _burger_target_compact(
        text
    )

    digit = re.search(
        r"(\d+)개",
        compact,
    )

    if digit:
        return int(
            digit.group(1)
        )

    rules = (
        (10, ("열개",)),
        (9, ("아홉개",)),
        (8, ("여덟개",)),
        (7, ("일곱개",)),
        (6, ("여섯개",)),
        (5, ("다섯개",)),
        (4, ("네개",)),
        (3, ("세개",)),
        (2, ("두개",)),
        (1, (
            "한개",
            "하나",
        )),
    )

    for number, words in rules:
        if any(
            word in compact
            for word in words
        ):
            return number

    return None


def _modifier_menu_mentions(text):
    compact = _burger_target_compact(
        text
    )

    found = []

    for key, label in MENU_LABELS.items():

        tokens = {
            _burger_target_compact(label)
        }

        aliases = MENU_ALIASES.get(
            key,
            (),
        )

        if isinstance(aliases, str):
            aliases = (aliases,)

        for alias in aliases:
            token = _burger_target_compact(
                alias
            )

            if token:
                tokens.add(token)

        for token in sorted(
            tokens,
            key=len,
            reverse=True,
        ):

            if not token:
                continue

            for match in re.finditer(
                re.escape(token),
                compact,
            ):
                found.append(
                    (
                        match.start(),
                        match.end(),
                        key,
                        token,
                    )
                )

    # 긴 token 우선 + 중복 span 제거
    found.sort(
        key=lambda x: (
            x[0],
            -(x[1] - x[0]),
        )
    )

    selected = []
    occupied = []

    for entry in found:

        start, end, _, _ = entry

        if any(
            not (
                end <= a
                or start >= b
            )
            for a, b in occupied
        ):
            continue

        selected.append(entry)
        occupied.append(
            (start, end)
        )

    selected.sort(
        key=lambda x: x[0]
    )

    return compact, selected


def _modifier_feature_filter(
    text,
    items,
):
    compact, menu_mentions = (
        _modifier_menu_mentions(text)
    )

    feature_text = compact

    # 치즈버거의 "치즈"를
    # 치즈 토핑으로 오인하지 않게 메뉴명을 먼저 제거
    for _, _, _, token in sorted(
        menu_mentions,
        key=lambda x: len(x[3]),
        reverse=True,
    ):
        feature_text = (
            feature_text.replace(
                token,
                "",
            )
        )

    filtered = list(items)
    used = False

    # --------------------------------------------------------
    # 추가해 둔 토핑 특징
    #
    # 베이컨 추가한 거
    # 치즈 토핑 들어간 거
    # 패티 추가된 거
    # --------------------------------------------------------

    topping_marker = any(
        marker in feature_text
        for marker in (
            "추가한",
            "추가된",
            "넣은",
            "올린",
            "얹은",
            "토핑",
        )
    )

    if topping_marker:

        for key, label in TOPPING_LABELS.items():

            token = _burger_target_compact(
                label
            )

            if token not in feature_text:
                continue

            filtered = [
                item
                for item in filtered
                if key in (
                    item.get(
                        "add_toppings"
                    )
                    or []
                )
            ]

            used = True

    # --------------------------------------------------------
    # 제외해 둔 재료 특징
    #
    # 양파 뺀 거
    # 피클 제외한 거
    # 토마토 없는 거
    # --------------------------------------------------------

    exclude_marker = any(
        marker in feature_text
        for marker in (
            "뺀",
            "빼놓",
            "제외한",
            "제외된",
            "없는",
            "빠진",
        )
    )

    if exclude_marker:

        for key, label in EXCLUDE_LABELS.items():

            token = _burger_target_compact(
                label
            )

            if token not in feature_text:
                continue

            filtered = [
                item
                for item in filtered
                if key in (
                    item.get(
                        "exclude"
                    )
                    or []
                )
            ]

            used = True

    return used, filtered


def _resolve_burger_target_reference_rich(
    text,
    state,
):
    items = [
        item
        for item in (
            state.get(
                "items",
                [],
            )
            or []
        )
        if item.get("item_type")
        == "burger"
    ]

    items.sort(
        key=lambda x: int(
            x.get("line_id", 0)
            or 0
        )
    )

    if not items:
        return None

    compact, mentions = (
        _modifier_menu_mentions(
            text
        )
    )

    menu_keys = []

    for _, _, key, _ in mentions:
        if key not in menu_keys:
            menu_keys.append(key)

    # --------------------------------------------------------
    # 메뉴명이 있으면 우선 해당 메뉴들로 후보 제한
    # --------------------------------------------------------

    candidates = list(items)

    if menu_keys:
        candidates = [
            item
            for item in candidates
            if item.get("menu")
            in menu_keys
        ]

    # --------------------------------------------------------
    # 특징으로 추가 제한
    # --------------------------------------------------------

    feature_used, candidates = (
        _modifier_feature_filter(
            text,
            candidates,
        )
    )

    if feature_used:

        if not candidates:
            return {
                "status": "not_found",
                "items": [],
            }

        if len(candidates) == 1:
            return {
                "status": "resolved",
                "items": candidates,
            }

        # 메뉴 + 특징으로도 여러 개면
        # 임의 선택하지 않는다.
        return {
            "status": "ambiguous",
            "items": candidates,
        }

    # --------------------------------------------------------
    # "치즈버거 하나 치킨버거 하나"
    # "치즈버거 두개 치킨버거 하나"
    # --------------------------------------------------------

    if mentions:

        explicit_counts = {}
        one_each = (
            len(menu_keys) >= 2
            and "하나씩" in compact
        )

        for index, entry in enumerate(
            mentions
        ):
            start, end, key, _ = entry

            next_start = (
                mentions[index + 1][0]
                if index + 1 < len(mentions)
                else len(compact)
            )

            segment = compact[
                end:
                next_start
            ]

            count = (
                1
                if one_each
                else
                _modifier_count_value(
                    segment
                )
            )

            if count is not None:
                explicit_counts[key] = count

        if explicit_counts:

            result = []

            for key in menu_keys:

                group = [
                    item
                    for item in candidates
                    if item.get("menu")
                    == key
                ]

                if not group:
                    return {
                        "status": "not_found",
                        "items": [],
                    }

                count = explicit_counts.get(
                    key
                )

                # 일부 메뉴만 개수를 말한 경우는
                # 아직 애매함
                if count is None:
                    return {
                        "status": "ambiguous",
                        "items": candidates,
                    }

                if count > len(group):
                    return {
                        "status": "not_found",
                        "items": [],
                    }

                # 같은 메뉴 안에 단품/세트 등 서로 다른 variant가 있는데
                # 일부만 고르겠다고 하면 임의 선택 금지
                if (
                    count < len(group)
                    and
                    not _modifier_same_variant(
                        group
                    )
                ):
                    return {
                        "status": "ambiguous",
                        "items": group,
                    }

                result.extend(
                    group[:count]
                )

            if result:
                return {
                    "status": "resolved",
                    "items": result,
                }

        # "치즈버거랑 치킨버거 전부"
        if any(
            token in compact
            for token in (
                "전부",
                "모두",
                "다요",
            )
        ):
            if candidates:
                return {
                    "status": "resolved",
                    "items": candidates,
                }

    return None


def resolve_burger_target_reference(
    text,
    state,
):
    rich = (
        _resolve_burger_target_reference_rich(
            text,
            state,
        )
    )

    if rich is not None:
        return rich

    return (
        _resolve_burger_target_reference_base(
            text,
            state,
        )
    )


# MODIFIER TARGET FOLLOWUP V2 20261007

def _pending_burger_modifier_context(state):
    """
    burger modifier target 선택 문맥.

    mode:
      topping         = 토핑 추가
      exclude         = 기본 재료 제외
      topping_remove  = 추가했던 토핑 제거
      exclude_restore = 제외했던 재료 다시 넣기
    """

    history = (
        router_history_before_current_customer()
    )

    if not history:
        return None

    question_index = None
    mode = None

    # --------------------------------------------------------
    # 가장 최근 target 질문 탐색
    # --------------------------------------------------------

    for index in range(
        len(history) - 1,
        -1,
        -1,
    ):
        entry = history[index]

        if entry.get("role") != "staff":
            continue

        compact = _burger_target_compact(
            entry.get("text")
        )

        if (
            "어느버거에토핑을추가할지"
            in compact
        ):
            question_index = index
            mode = "topping"
            break

        if (
            "어느버거에서빼드릴지"
            in compact
        ):
            question_index = index
            mode = "exclude"
            break

        if (
            "어느버거에서추가한"
            in compact
            and
            "토핑을빼드릴지"
            in compact
        ):
            question_index = index
            mode = "topping_remove"
            break

        if (
            "어느버거에"
            in compact
            and
            "다시넣어드릴지"
            in compact
        ):
            question_index = index
            mode = "exclude_restore"
            break

    if question_index is None:
        return None

    # --------------------------------------------------------
    # 질문 이후에는 target clarification 응답만 허용
    # --------------------------------------------------------

    for entry in history[
        question_index + 1:
    ]:

        if entry.get("role") != "staff":
            continue

        staff = _burger_target_compact(
            entry.get("text")
        )

        allowed = (
            (
                "개있습니다"
                in staff
                and
                "말씀해주세요"
                in staff
            )
            or
            "해당되는버거가여러개"
            in staff
            or
            "현재선택가능한버거는"
            in staff
        )

        if not allowed:
            return None

    # --------------------------------------------------------
    # target 질문 직전의 원래 customer 요청
    # --------------------------------------------------------

    original_customer = None

    for index in range(
        question_index - 1,
        -1,
        -1,
    ):

        entry = history[index]

        if entry.get("role") == "customer":

            original_customer = str(
                entry.get("text")
                or ""
            )

            break

    if not original_customer:
        return None

    original_compact = (
        _burger_target_compact(
            original_customer
        )
    )

    modifier_key = None
    modifier_label = None

    # --------------------------------------------------------
    # topping
    # --------------------------------------------------------

    if mode in {
        "topping",
        "topping_remove",
    }:

        for key, label in TOPPING_LABELS.items():

            if (
                _burger_target_compact(label)
                in original_compact
            ):
                modifier_key = key
                modifier_label = label
                break

    # --------------------------------------------------------
    # excluded ingredient
    # --------------------------------------------------------

    else:

        for key, label in EXCLUDE_LABELS.items():

            if (
                _burger_target_compact(label)
                in original_compact
            ):
                modifier_key = key
                modifier_label = label
                break

    if modifier_key is None:
        return None

    burgers = (
        _burger_target_items(
            state
        )
    )

    # --------------------------------------------------------
    # 역방향은 실제 적용돼 있는 burger만 후보
    # --------------------------------------------------------

    if mode == "topping_remove":

        burgers = [
            item
            for item in burgers
            if modifier_key
            in (
                item.get(
                    "add_toppings",
                    [],
                )
                or []
            )
        ]

    elif mode == "exclude_restore":

        burgers = [
            item
            for item in burgers
            if modifier_key
            in (
                item.get(
                    "exclude",
                    [],
                )
                or []
            )
        ]

    if not burgers:
        return None

    candidates = burgers

    # --------------------------------------------------------
    # 질문 이후 고객이 이미
    # "치즈버거요", "단품이요" 등으로 좁혔다면
    # 후보 subset을 계속 유지
    # --------------------------------------------------------

    for entry in history[
        question_index + 1:
    ]:

        if entry.get("role") != "customer":
            continue

        result = (
            resolve_burger_target_reference(
                entry.get("text"),
                {
                    "items": candidates,
                },
            )
        )

        if result["status"] in {
            "resolved",
            "ambiguous",
        }:
            candidates = result["items"]

    return {
        "mode": mode,
        "modifier_key": modifier_key,
        "modifier_label": modifier_label,
        "candidates": candidates,
    }



# MODIFIER RICH TARGET V3 20261008

def _modifier_target_signature(item):
    """
    '같은 버거' 판정 시 메뉴뿐 아니라
    현재 토핑/제외 상태까지 포함한다.
    """

    return (
        item.get("menu"),
        item.get("type"),
        item.get("drink"),
        item.get("drink_size"),
        item.get("side"),
        tuple(
            sorted(
                item.get(
                    "exclude",
                    [],
                )
                or []
            )
        ),
        tuple(
            sorted(
                item.get(
                    "add_toppings",
                    [],
                )
                or []
            )
        ),
    )


def _modifier_target_detail(item):

    base = (
        _burger_target_description(
            item
        )
    )

    extras = []

    toppings = (
        item.get(
            "add_toppings",
            [],
        )
        or []
    )

    if toppings:

        labels = [
            TOPPING_LABELS.get(
                value,
                value,
            )
            for value in toppings
        ]

        extras.append(
            "추가: "
            + ", ".join(labels)
        )

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

        extras.append(
            "제외: "
            + ", ".join(labels)
        )

    if not extras:
        return base

    return (
        base
        + " / "
        + " / ".join(extras)
    )


def _modifier_count_from_text(text):

    compact = (
        _burger_target_compact(
            text
        )
    )

    match = re.search(
        r"(\d+)개",
        compact,
    )

    if match:
        return int(
            match.group(1)
        )

    mapping = (
        (10, ("열개",)),
        (9, ("아홉개",)),
        (8, ("여덟개",)),
        (7, ("일곱개",)),
        (6, ("여섯개",)),
        (5, ("다섯개",)),
        (4, ("네개",)),
        (3, ("세개",)),
        (2, ("두개",)),
        (1, (
            "한개",
            "하나",
        )),
    )

    for count, signals in mapping:

        if any(
            signal in compact
            for signal in signals
        ):
            return count

    return None


def _resolve_modifier_rich_reference(
    text,
    candidates,
):
    """
    modifier target 선택 전용.

    지원 예:
      치즈버거 하나 치킨버거 하나
      치즈버거 단품 하나
      베이컨 추가한 거
      패티 추가한 거
      양파 뺀 거
      피클 제외한 거
      베이컨 추가한 치즈버거
    """

    compact = (
        _burger_target_compact(
            text
        )
    )

    pool = sorted(
        list(candidates),
        key=lambda item: int(
            item.get(
                "line_id",
                0,
            )
            or 0
        ),
    )

    if not pool:
        return None

    feature_used = False

    # ========================================================
    # 기존 추가 토핑 특징
    # ========================================================

    topping_feature_signal = any(
        signal in compact
        for signal in (
            "추가한",
            "추가된",
            "토핑있는",
            "토핑들어간",
            "토핑추가한",
            "올린",
        )
    )

    if topping_feature_signal:

        for key, label in (
            TOPPING_LABELS.items()
        ):

            token = (
                _burger_target_compact(
                    label
                )
            )

            if token not in compact:
                continue

            pool = [
                item
                for item in pool
                if key
                in (
                    item.get(
                        "add_toppings",
                        [],
                    )
                    or []
                )
            ]

            feature_used = True

    # ========================================================
    # 기존 제외 재료 특징
    # ========================================================

    exclude_feature_signal = any(
        signal in compact
        for signal in (
            "뺀",
            "뺐",
            "제외한",
            "제외된",
            "없는거",
            "없이한",
        )
    )

    if exclude_feature_signal:

        for key, label in (
            EXCLUDE_LABELS.items()
        ):

            token = (
                _burger_target_compact(
                    label
                )
            )

            if token not in compact:
                continue

            pool = [
                item
                for item in pool
                if key
                in (
                    item.get(
                        "exclude",
                        [],
                    )
                    or []
                )
            ]

            feature_used = True

    if feature_used and not pool:

        return {
            "status": "not_found",
            "items": [],
        }

    # ========================================================
    # 여러 메뉴 이름을 한 발화에서 동시에 선택
    #
    # 치즈버거 하나 치킨버거 하나
    # ========================================================

    menu_occurrences = []

    for menu_key, label in (
        MENU_LABELS.items()
    ):

        token = (
            _burger_target_compact(
                label
            )
        )

        start = 0

        while True:

            pos = compact.find(
                token,
                start,
            )

            if pos < 0:
                break

            menu_occurrences.append(
                (
                    pos,
                    menu_key,
                    token,
                )
            )

            start = (
                pos
                + len(token)
            )

    menu_occurrences.sort(
        key=lambda x: x[0]
    )

    # 동일 메뉴가 실수로 두 번 잡힌 경우 제거
    cleaned = []

    seen_positions = set()

    for entry in menu_occurrences:

        identity = (
            entry[0],
            entry[1],
        )

        if identity in seen_positions:
            continue

        seen_positions.add(
            identity
        )

        cleaned.append(entry)

    menu_occurrences = cleaned

    if menu_occurrences:

        selected = []
        ambiguous = []

        for index, (
            pos,
            menu_key,
            token,
        ) in enumerate(
            menu_occurrences
        ):

            next_pos = (
                menu_occurrences[
                    index + 1
                ][0]
                if (
                    index + 1
                    < len(
                        menu_occurrences
                    )
                )
                else len(compact)
            )

            segment = compact[
                pos:next_pos
            ]

            group = [
                item
                for item in pool
                if item.get("menu")
                == menu_key
            ]

            if "단품" in segment:

                group = [
                    item
                    for item in group
                    if item.get("type")
                    == "single"
                ]

            elif "세트" in segment:

                group = [
                    item
                    for item in group
                    if item.get("type")
                    == "set"
                ]

            if not group:

                return {
                    "status": "not_found",
                    "items": [],
                }

            count = (
                _modifier_count_from_text(
                    segment
                )
            )

            # -------------------------------
            # 개수까지 말함
            # -------------------------------

            if count is not None:

                if count > len(group):

                    return {
                        "status": "invalid_count",
                        "items": group,
                        "requested_count": count,
                    }

                if count == len(group):

                    selected.extend(
                        group
                    )

                    continue

                signatures = {
                    _modifier_target_signature(
                        item
                    )
                    for item in group
                }

                # 옵션/토핑/제외 상태까지 동일하면
                # 앞 line부터 N개 선택 가능
                if len(signatures) == 1:

                    selected.extend(
                        group[:count]
                    )

                    continue

                ambiguous.extend(
                    group
                )

                continue

            # -------------------------------
            # 메뉴명만 말함
            # -------------------------------

            if len(group) == 1:

                selected.extend(
                    group
                )

            else:

                ambiguous.extend(
                    group
                )

        if ambiguous:

            combined = []

            for item in (
                selected
                + ambiguous
            ):

                if item not in combined:
                    combined.append(item)

            return {
                "status": "ambiguous",
                "items": combined,
            }

        result = []

        for item in selected:

            if item not in result:
                result.append(item)

        if result:

            return {
                "status": "resolved",
                "items": result,
            }

    # ========================================================
    # 메뉴명 없이 특징만 말한 경우
    #
    # "베이컨 추가한 거요"
    # ========================================================

    if feature_used:

        count = (
            _modifier_count_from_text(
                compact
            )
        )

        if count is not None:

            if count > len(pool):

                return {
                    "status": "invalid_count",
                    "items": pool,
                    "requested_count": count,
                }

            if count == len(pool):

                return {
                    "status": "resolved",
                    "items": pool,
                }

            signatures = {
                _modifier_target_signature(
                    item
                )
                for item in pool
            }

            if len(signatures) == 1:

                return {
                    "status": "resolved",
                    "items": pool[:count],
                }

        if len(pool) == 1:

            return {
                "status": "resolved",
                "items": pool,
            }

        return {
            "status": "ambiguous",
            "items": pool,
        }

    return None


# MODIFIER SEMANTIC TARGET SELECTOR 20261008

def _resolve_modifier_semantic_targets(
    text,
    candidates,
):
    """
    modifier 대상 선택에서 메뉴 이름/특징을 해석한다.

    예:
      치즈버거 하나 치킨버거 하나
      치즈버거 1개 치킨버거 1개
      베이컨 추가한 거
      패티 추가한 거
      양파 뺀 거
      피클 제외한 거
    """

    compact = _burger_target_compact(
        text
    )

    candidates = sorted(
        list(candidates or []),
        key=lambda item: int(
            item.get("line_id", 0)
            or 0
        ),
    )

    if not candidates:
        return {
            "status": "not_found",
            "items": [],
        }

    # ========================================================
    # 1. 현재 적용된 추가 토핑으로 선택
    # ========================================================

    topping_context = any(
        token in compact
        for token in (
            "추가한",
            "추가된",
            "넣은",
            "넣었던",
            "토핑있는",
            "토핑들어간",
        )
    )

    if topping_context:

        for key, label in TOPPING_LABELS.items():

            token = _burger_target_compact(
                label
            )

            if token not in compact:
                continue

            matches = [
                item
                for item in candidates
                if key in (
                    item.get(
                        "add_toppings",
                        [],
                    )
                    or []
                )
            ]

            if not matches:
                return {
                    "status": "not_found",
                    "items": [],
                }

            return {
                "status": (
                    "resolved"
                    if len(matches) == 1
                    else "ambiguous"
                ),
                "items": matches,
            }

    # ========================================================
    # 2. 현재 제외된 재료로 선택
    # ========================================================

    exclude_context = any(
        token in compact
        for token in (
            "뺀",
            "빼놓은",
            "제외한",
            "제외된",
            "없는거",
            "없이한",
        )
    )

    if exclude_context:

        for key, label in EXCLUDE_LABELS.items():

            token = _burger_target_compact(
                label
            )

            if token not in compact:
                continue

            matches = [
                item
                for item in candidates
                if key in (
                    item.get(
                        "exclude",
                        [],
                    )
                    or []
                )
            ]

            if not matches:
                return {
                    "status": "not_found",
                    "items": [],
                }

            return {
                "status": (
                    "resolved"
                    if len(matches) == 1
                    else "ambiguous"
                ),
                "items": matches,
            }

    # ========================================================
    # 3. 여러 메뉴 이름 동시 선택
    # ========================================================

    menu_mentions = []

    for key, label in MENU_LABELS.items():

        token = _burger_target_compact(
            label
        )

        pos = compact.find(
            token
        )

        if pos >= 0:
            menu_mentions.append(
                (
                    pos,
                    key,
                    token,
                )
            )

    menu_mentions.sort(
        key=lambda x: x[0]
    )

    if not menu_mentions:
        return None

    # --------------------------------------------------------
    # 각 메뉴 이름 뒤에 붙은 개수 확인
    #
    # 치즈버거 하나 치킨버거 하나
    # 치즈버거 1개 치킨버거 1개
    # --------------------------------------------------------

    def parse_count(segment):

        m = re.search(
            r"(\d+)개",
            segment,
        )

        if m:
            return int(
                m.group(1)
            )

        count_words = (
            (1, (
                "하나",
                "한개",
            )),
            (2, (
                "두개",
                "둘",
            )),
            (3, (
                "세개",
                "셋",
            )),
            (4, (
                "네개",
                "넷",
            )),
            (5, (
                "다섯개",
            )),
        )

        for count, words in count_words:
            if any(
                word in segment
                for word in words
            ):
                return count

        return None

    selected = []
    any_count = False

    for index, (
        position,
        menu_key,
        menu_token,
    ) in enumerate(menu_mentions):

        segment_start = (
            position
            + len(menu_token)
        )

        if (
            index + 1
            < len(menu_mentions)
        ):
            segment_end = (
                menu_mentions[
                    index + 1
                ][0]
            )
        else:
            segment_end = len(
                compact
            )

        segment = compact[
            segment_start:
            segment_end
        ]

        requested_count = (
            parse_count(
                segment
            )
        )

        matches = [
            item
            for item in candidates
            if item.get("menu")
            == menu_key
        ]

        if not matches:
            continue

        if requested_count is None:

            selected.extend(
                matches
            )

            continue

        any_count = True

        if (
            requested_count
            > len(matches)
        ):
            return {
                "status": "not_found",
                "items": matches,
            }

        # 같은 메뉴에서 N개라고 했으면
        # line_id가 빠른 것부터 N개 선택.
        selected.extend(
            matches[
                :requested_count
            ]
        )

    # 중복 제거
    unique = []

    seen = set()

    for item in selected:

        line_id = item.get(
            "line_id"
        )

        if line_id in seen:
            continue

        seen.add(
            line_id
        )

        unique.append(
            item
        )

    if not unique:
        return {
            "status": "not_found",
            "items": [],
        }

    # 메뉴별 개수가 명시됐다면
    # "치즈버거 하나 치킨버거 하나"
    # → 바로 resolved
    if any_count:
        return {
            "status": "resolved",
            "items": unique,
        }

    # 메뉴명만 여러 개 말했고 후보가 여러 개면
    # 추가 clarification.
    if len(unique) > 1:
        return {
            "status": "ambiguous",
            "items": unique,
        }

    return {
        "status": "resolved",
        "items": unique,
    }

# MODIFIER SMART TARGET V3 20261008

def _modifier_target_count(compact):
    m = re.search(
        r"(\d+)개",
        compact,
    )

    if m:
        return int(m.group(1))

    patterns = (
        (1, (
            "하나만",
            "한개만",
            "하나요",
            "한개요",
            "하나",
            "한개",
        )),
        (2, (
            "두개만",
            "두개요",
            "두개",
        )),
        (3, (
            "세개만",
            "세개요",
            "세개",
        )),
        (4, (
            "네개만",
            "네개요",
            "네개",
        )),
        (5, (
            "다섯개만",
            "다섯개요",
            "다섯개",
        )),
        (6, (
            "여섯개만",
            "여섯개요",
            "여섯개",
        )),
        (7, (
            "일곱개만",
            "일곱개요",
            "일곱개",
        )),
        (8, (
            "여덟개만",
            "여덟개요",
            "여덟개",
        )),
        (9, (
            "아홉개만",
            "아홉개요",
            "아홉개",
        )),
        (10, (
            "열개만",
            "열개요",
            "열개",
        )),
    )

    for count, words in patterns:
        if any(
            word in compact
            for word in words
        ):
            return count

    return None


def _modifier_feature_target(
    text,
    candidates,
):
    """
    주문 상태의 특징으로 버거를 가리키는 표현.

    예:
      베이컨 추가한 거
      패티 추가된 거
      치즈 토핑 들어간 거

      양파 뺀 거
      피클 제외한 거
      토마토 없는 거
    """

    compact = _burger_target_compact(
        text
    )

    # --------------------------------------------------------
    # 추가 토핑 특징
    # --------------------------------------------------------

    for key, label in TOPPING_LABELS.items():

        token = _burger_target_compact(
            label
        )

        feature = any(
            signal in compact
            for signal in (
                token + "추가한",
                token + "추가된",
                token + "추가해둔",
                token + "토핑",
                token + "올린",
                token + "얹은",
                token + "들어간",
            )
        )

        if not feature:
            continue

        matches = [
            item
            for item in candidates
            if key in (
                item.get(
                    "add_toppings",
                    [],
                )
                or []
            )
        ]

        return matches

    # --------------------------------------------------------
    # 제외 재료 특징
    # --------------------------------------------------------

    for key, label in EXCLUDE_LABELS.items():

        token = _burger_target_compact(
            label
        )

        feature = any(
            signal in compact
            for signal in (
                token + "뺀",
                token + "빼놓은",
                token + "제외한",
                token + "제외된",
                token + "없는",
                token + "빠진",
            )
        )

        if not feature:
            continue

        matches = [
            item
            for item in candidates
            if key in (
                item.get(
                    "exclude",
                    [],
                )
                or []
            )
        ]

        return matches

    return None


def _resolve_pending_modifier_target(
    text,
    state,
):
    context = (
        _pending_burger_modifier_context(
            state
        )
    )

    if context is None:
        return None

    candidates = list(
        context["candidates"]
    )

    if not candidates:
        return {
            **context,
            "status": "not_found",
            "items": [],
        }

    candidates.sort(
        key=lambda item: int(
            item.get("line_id", 0)
            or 0
        )
    )

    compact = _burger_target_compact(
        text
    )

    # ========================================================
    # 1. 전부 / 모두
    # ========================================================

    if any(
        signal in compact
        for signal in (
            "전부",
            "모두",
            "전부다",
            "모두다",
            "다요",
        )
    ):
        return {
            **context,
            "status": "resolved",
            "items": candidates,
        }

    # ========================================================
    # 2. 둘 다
    # ========================================================

    if "둘다" in compact:

        if len(candidates) == 2:
            return {
                **context,
                "status": "resolved",
                "items": candidates,
            }

        return {
            **context,
            "status": "ambiguous",
            "items": candidates,
        }

    # ========================================================
    # 3. 주문 특징으로 선택
    #
    # 베이컨 추가한 거
    # 패티 추가한 거
    # 양파 뺀 거
    # ========================================================

    feature_matches = (
        _modifier_feature_target(
            text,
            candidates,
        )
    )

    if feature_matches is not None:

        if not feature_matches:
            return {
                **context,
                "status": "not_found",
                "items": [],
            }

        if len(feature_matches) == 1:
            return {
                **context,
                "status": "resolved",
                "items": feature_matches,
            }

        return {
            **context,
            "status": "ambiguous",
            "items": feature_matches,
        }

    # ========================================================
    # 4. 메뉴 이름별 선택
    #
    # 치즈버거 하나 치킨버거 하나
    # 치즈버거 1개 치킨버거 1개
    # 치즈버거랑 치킨버거
    # ========================================================

    mentions = []

    for menu_key, label in MENU_LABELS.items():

        token = _burger_target_compact(
            label
        )

        pos = compact.find(token)

        if pos >= 0:
            mentions.append(
                (
                    pos,
                    menu_key,
                    token,
                )
            )

    mentions.sort(
        key=lambda x: x[0]
    )

    # --------------------------------------------------------
    # 메뉴가 두 종류 이상 명시됨
    # --------------------------------------------------------

    if len(mentions) >= 2:

        selected = []

        for index, (
            pos,
            menu_key,
            token,
        ) in enumerate(mentions):

            next_pos = (
                mentions[index + 1][0]
                if index + 1 < len(mentions)
                else len(compact)
            )

            segment = compact[
                pos:next_pos
            ]

            group = [
                item
                for item in candidates
                if item.get("menu")
                == menu_key
            ]

            if not group:
                return {
                    **context,
                    "status": "not_found",
                    "items": [],
                }

            group.sort(
                key=lambda item: int(
                    item.get("line_id", 0)
                    or 0
                )
            )

            count = (
                _modifier_target_count(
                    segment
                )
            )

            # 메뉴명만 여러 개 말했다면
            # 해당 메뉴들의 모든 항목을 선택.
            if count is None:

                chosen = group

            else:

                if count > len(group):
                    return {
                        **context,
                        "status": "invalid_count",
                        "items": group,
                        "requested_count": count,
                    }

                # 같은 메뉴라도 단품/세트 등 서로 다른
                # 항목에서 일부만 고르면 임의 선택 금지.
                descriptions = [
                    _burger_target_description(
                        item
                    )
                    for item in group
                ]

                if (
                    count < len(group)
                    and
                    len(
                        set(descriptions)
                    ) != 1
                ):
                    return {
                        **context,
                        "status": "ambiguous",
                        "items": group,
                    }

                chosen = group[
                    :count
                ]

            selected.extend(
                chosen
            )

        # line_id 기준 중복 제거
        unique = []
        seen = set()

        for item in selected:

            line_id = item.get(
                "line_id"
            )

            if line_id in seen:
                continue

            seen.add(line_id)
            unique.append(item)

        return {
            **context,
            "status": "resolved",
            "items": unique,
        }

    # --------------------------------------------------------
    # 메뉴 하나 + 개수
    #
    # 치즈버거 하나
    # 치킨버거 2개
    # --------------------------------------------------------

    if len(mentions) == 1:

        _, menu_key, _ = (
            mentions[0]
        )

        group = [
            item
            for item in candidates
            if item.get("menu")
            == menu_key
        ]

        count = (
            _modifier_target_count(
                compact
            )
        )

        if count is not None:

            if not group:
                return {
                    **context,
                    "status": "not_found",
                    "items": [],
                }

            if count > len(group):
                return {
                    **context,
                    "status": "invalid_count",
                    "items": group,
                    "requested_count": count,
                }

            group.sort(
                key=lambda item: int(
                    item.get("line_id", 0)
                    or 0
                )
            )

            descriptions = [
                _burger_target_description(
                    item
                )
                for item in group
            ]

            # 예:
            # 치즈버거 단품 + 치즈버거 세트가 있는데
            # "치즈버거 하나"라고만 하면 임의선택 금지.
            if (
                count < len(group)
                and
                len(
                    set(descriptions)
                ) != 1
            ):
                return {
                    **context,
                    "status": "ambiguous",
                    "items": group,
                }

            return {
                **context,
                "status": "resolved",
                "items": group[:count],
            }

    # ========================================================
    # 5. 일반 수량 선택
    #
    # 이미 앞 대화에서 후보가
    # 동일한 메뉴들로 좁혀진 상황:
    #
    # 하나만요 / 두개요 / 2개요
    # ========================================================

    requested_count = (
        _modifier_target_count(
            compact
        )
    )

    if requested_count is not None:

        if requested_count < 1:
            return {
                **context,
                "status": "invalid_count",
                "items": candidates,
            }

        if requested_count > len(candidates):
            return {
                **context,
                "status": "invalid_count",
                "items": candidates,
                "requested_count":
                    requested_count,
            }

        if (
            requested_count
            == len(candidates)
        ):
            return {
                **context,
                "status": "resolved",
                "items": candidates,
            }

        descriptions = [
            _burger_target_description(
                item
            )
            for item in candidates
        ]

        # 동일한 주문끼리라면 앞쪽 N개를
        # deterministic하게 선택 가능.
        if len(set(descriptions)) == 1:

            return {
                **context,
                "status": "resolved",
                "items": candidates[
                    :requested_count
                ],
            }

        return {
            **context,
            "status": "ambiguous",
            "items": candidates,
            "requested_count":
                requested_count,
        }

    # ========================================================
    # 6. 기존 resolver
    #
    # 번호 / 순번 / 단품세트 / 음료 / 사이드
    # ========================================================

    result = (
        resolve_burger_target_reference(
            text,
            {
                "items": candidates,
            },
        )
    )

    return {
        **context,
        "status": result["status"],
        "items": result["items"],
    }


def burger_target_followup_clarify_reply(
    text,
    state,
):
    result = (
        _resolve_pending_modifier_target(
            text,
            state,
        )
    )

    if result is None:
        return None

    if result["status"] == "resolved":
        return None

    items = result.get(
        "items",
        [],
    )

    if result["status"] == "invalid_count":

        return (
            f"현재 선택 가능한 버거는 "
            f"{len(items)}개입니다. "
            "적용할 개수를 다시 말씀해주세요."
        )

    if result["status"] == "not_found":

        return (
            "말씀하신 조건에 맞는 버거를 찾지 못했습니다. "
            "메뉴 이름, 단품·세트, 기존 토핑이나 제외 재료, "
            "순서 또는 번호로 다시 말씀해주세요."
        )

    if not items:
        return None

    descriptions = [
        _modifier_target_detail(
            item
        )
        for item in items
    ]

    if len(set(descriptions)) == 1:

        return (
            f"같은 {descriptions[0]} 주문이 "
            f"{len(items)}개 있습니다. "
            "전부인지, 몇 개인지, 또는 특정 하나인지 "
            "말씀해주세요."
        )

    options = [
        (
            f"{item.get('line_id')}번 "
            f"{_modifier_target_detail(item)}"
        )
        for item in items
    ]

    return (
        "해당되는 버거가 여러 개 있습니다. "
        + ", ".join(options)
        + " 중 어느 것인지 말씀해주세요."
    )

# REVERSE MODIFIER TARGET RESOLVER 20261007

def _reverse_modifier_explicit_text(
    mode,
    label,
    items,
):
    clauses = []

    for item in items:

        line_id = item.get(
            "line_id"
        )

        if line_id is None:
            return None

        if mode == "topping_remove":

            clauses.append(
                f"주문 {line_id}번 항목에서 "
                f"추가한 {label} 토핑을 "
                f"제거해주세요"
            )

        elif mode == "exclude_restore":

            clauses.append(
                f"주문 {line_id}번 항목에 "
                f"제외했던 재료 {label}를 "
                f"다시 넣어주세요"
            )

    if not clauses:
        return None

    return " 그리고 ".join(
        clauses
    )


def _reverse_modifier_question(
    mode,
    label,
):
    if mode == "topping_remove":

        return (
            f"어느 버거에서 추가한 "
            f"{label} 토핑을 빼드릴지 "
            f"말씀해주세요. "
            "메뉴 이름이나 단품·세트, "
            "순서, 개수, 또는 전부라고 "
            "말씀하셔도 됩니다."
        )

    return (
        f"어느 버거에 {label}를 "
        f"다시 넣어드릴지 말씀해주세요. "
        "메뉴 이름이나 단품·세트, "
        "순서, 개수, 또는 전부라고 "
        "말씀하셔도 됩니다."
    )


def detect_reverse_modifier_request(
    text,
    state,
):
    """
    사용자의 현재 발화가

      추가했던 토핑 제거
      제외했던 재료 복구

    인지 판별하고 실제 적용 가능한 burger만 후보로 만든다.
    """

    compact = (
        _burger_target_compact(
            text
        )
    )

    burgers = (
        _burger_target_items(
            state
        )
    )

    if not burgers:
        return None

    # ========================================================
    # 1. added topping remove
    # ========================================================

    topping_remove_signal = (
        (
            "토핑" in compact
            and any(
                signal in compact
                for signal in (
                    "빼",
                    "제거",
                    "없애",
                    "취소",
                    "삭제",
                )
            )
        )
        or
        (
            any(
                signal in compact
                for signal in (
                    "추가한",
                    "추가했던",
                    "추가분",
                    "추가취소",
                )
            )
            and any(
                signal in compact
                for signal in (
                    "빼",
                    "제거",
                    "없애",
                    "취소",
                    "삭제",
                )
            )
        )
    )

    if topping_remove_signal:

        for key, label in TOPPING_LABELS.items():

            label_compact = (
                _burger_target_compact(
                    label
                )
            )

            if label_compact not in compact:
                continue

            candidates = [
                item
                for item in burgers
                if key
                in (
                    item.get(
                        "add_toppings",
                        [],
                    )
                    or []
                )
            ]

            return {
                "mode": "topping_remove",
                "modifier_key": key,
                "modifier_label": label,
                "candidates": candidates,
            }

    # ========================================================
    # 2. excluded ingredient restore
    # ========================================================

    restore_signal = (
        "다시넣" in compact
        or "제외취소" in compact
        or (
            "제외" in compact
            and "취소" in compact
        )
        or (
            any(
                signal in compact
                for signal in (
                    "뺀거",
                    "뺐던",
                    "빼달랬",
                    "빼달라고",
                )
            )
            and "취소" in compact
        )
    )

    if restore_signal:

        for key, label in EXCLUDE_LABELS.items():

            label_compact = (
                _burger_target_compact(
                    label
                )
            )

            if label_compact not in compact:
                continue

            candidates = [
                item
                for item in burgers
                if key
                in (
                    item.get(
                        "exclude",
                        [],
                    )
                    or []
                )
            ]

            return {
                "mode": "exclude_restore",
                "modifier_key": key,
                "modifier_label": label,
                "candidates": candidates,
            }

    return None


def pre_router_reverse_modifier_request(
    text,
    state,
):
    context = (
        detect_reverse_modifier_request(
            text,
            state,
        )
    )

    if context is None:
        return None

    mode = context["mode"]
    label = context["modifier_label"]
    candidates = context["candidates"]

    # --------------------------------------------------------
    # 실제로 취소/복구할 게 없음
    # --------------------------------------------------------

    if not candidates:

        if mode == "topping_remove":

            return {
                "kind": "reply",
                "text": (
                    f"현재 주문에는 추가된 "
                    f"{label} 토핑이 없습니다."
                ),
            }

        return {
            "kind": "reply",
            "text": (
                f"현재 주문에는 {label}가 "
                f"제외된 버거가 없습니다."
            ),
        }

    # --------------------------------------------------------
    # 후보 하나뿐이면 바로 명시형으로 rewrite
    # --------------------------------------------------------

    if len(candidates) == 1:

        return {
            "kind": "rewrite",
            "text": (
                _reverse_modifier_explicit_text(
                    mode,
                    label,
                    candidates,
                )
            ),
        }

    # --------------------------------------------------------
    # 현재 발화 자체에 target이 있는지 확인
    # --------------------------------------------------------

    result = (
        resolve_burger_target_reference(
            text,
            {
                "items": candidates,
            },
        )
    )

    if result["status"] == "resolved":

        return {
            "kind": "rewrite",
            "text": (
                _reverse_modifier_explicit_text(
                    mode,
                    label,
                    result["items"],
                )
            ),
        }

    # --------------------------------------------------------
    # 여러 후보 -> target 질문
    # --------------------------------------------------------

    return {
        "kind": "reply",
        "text": (
            _reverse_modifier_question(
                mode,
                label,
            )
        ),
    }


def rewrite_reverse_modifier_target_followup(
    text,
    state,
):
    """
    reverse modifier target 질문 뒤의

      두번째요
      치즈버거 단품이요
      두개요
      둘 다요
      전부요

    등을 기존 공통 target resolver로 처리.
    """

    result = (
        _resolve_pending_modifier_target(
            text,
            state,
        )
    )

    if result is None:
        return None

    mode = result.get(
        "mode"
    )

    if mode not in {
        "topping_remove",
        "exclude_restore",
    }:
        return None

    if result["status"] != "resolved":
        return None

    return (
        _reverse_modifier_explicit_text(
            mode,
            result["modifier_label"],
            result["items"],
        )
    )

# ============================================================
# UNIFIED BURGER MODIFIER FLOW 20261007
# ============================================================
#
# 4가지 동작을 하나의 flow로 처리한다.
#
#   topping_add
#   topping_remove
#   exclude_add
#   exclude_remove
#
# 대상 선택이 끝나면 Router/LLM에 다시 의미 해석을 맡기지 않고
# process_prebuilt()로 deterministic update를 적용한다.
# ============================================================

_unified_modifier_pending = None


def _um_compact(text):
    return re.sub(
        r"[\s!?.,~]+",
        "",
        str(text or "").lower(),
    )


def _um_find_label(text, mapping):
    compact = _um_compact(text)

    candidates = sorted(
        mapping.items(),
        key=lambda pair: len(
            str(pair[1])
        ),
        reverse=True,
    )

    for key, label in candidates:
        if _um_compact(label) in compact:
            return key, label

    return None, None


def _um_detect_request(text, state):
    request = detect_modifier_request(
        text, TOPPING_LABELS, EXCLUDE_LABELS, MENU_LABELS, SIDE_LABELS,
    )
    if request and request.get("operation"):
        selector = _um_compact(request["selector_text"])
        if not any(token in selector for token in (*MENU_LABELS.values(), "버거", "세트", "들어")):
            for label in (*DRINK_LABELS.values(), *SIDE_LABELS.values()):
                if label + "에" in selector:
                    return {"kind": "non_burger", "label": label}
    return request



def _um_eligible_items(
    flow,
    state,
):
    burgers = _burger_target_items(
        state
    )

    op = flow["operation"]
    key = flow["key"]

    if op == "topping_add":
        return [
            item
            for item in burgers
            if key not in (
                item.get(
                    "add_toppings",
                    [],
                )
                or []
            )
        ]

    if op == "topping_remove":
        return [
            item
            for item in burgers
            if key in (
                item.get(
                    "add_toppings",
                    [],
                )
                or []
            )
        ]

    if op == "exclude_add":
        return [
            item
            for item in burgers
            if key not in (
                item.get(
                    "exclude",
                    [],
                )
                or []
            )
        ]

    if op == "exclude_remove":
        return [
            item
            for item in burgers
            if key in (
                item.get(
                    "exclude",
                    [],
                )
                or []
            )
        ]

    return []


def _um_no_candidate_reply(flow):
    label, op = flow["label"], flow["operation"]
    if op == "topping_add":
        return f"현재 {label} 토핑을 새로 추가할 버거가 없습니다."
    if op == "topping_remove":
        return f"현재 {label} 토핑이 추가된 버거가 없습니다."
    if op == "exclude_add":
        return f"현재 재료 {label} 제외를 새로 적용할 버거가 없습니다."
    return f"현재 {label} 제외가 적용된 버거가 없습니다."



def _um_question(flow):
    label, op = flow["label"], flow["operation"]
    action = {
        "topping_add": f"{label} 토핑 추가",
        "topping_remove": f"{label} 토핑 추가 취소",
        "exclude_add": f"재료 {label} 제외",
        "exclude_remove": f"재료 {label} 제외 취소",
    }[op]
    return (f"{action} 대상 버거를 말씀해주세요. "
            "메뉴 이름이나 단품·세트, 번호, 순서, 개수, 특징, 또는 전부라고 말씀하셔도 됩니다.")



def _um_count(text):
    compact = _um_compact(
        text
    )

    m = re.search(
        r"(\d+)개",
        compact,
    )

    if m:
        return int(
            m.group(1)
        )

    patterns = (
        (1, (
            "하나만",
            "한개만",
            "하나요",
            "한개요",
            "하나",
            "한개",
        )),
        (2, (
            "두개만",
            "두개요",
            "두개",
        )),
        (3, (
            "세개만",
            "세개요",
            "세개",
        )),
        (4, (
            "네개만",
            "네개요",
            "네개",
        )),
        (5, (
            "다섯개만",
            "다섯개요",
            "다섯개",
        )),
        (6, (
            "여섯개만",
            "여섯개요",
            "여섯개",
        )),
    )

    for number, values in patterns:
        if any(
            value in compact
            for value in values
        ):
            return number

    return None


def _um_all(text):
    compact = _um_compact(
        text
    )

    if any(
        token in compact
        for token in (
            "전부",
            "모두",
        )
    ):
        return True

    return compact in {
        "다",
        "다요",
        "둘다",
        "둘다요",
        "두개다",
        "두개다요",
    }


def _um_items_from_ids(
    state,
    line_ids,
):
    wanted = set(
        int(x)
        for x in line_ids
    )

    result = [
        item
        for item in _burger_target_items(
            state
        )
        if int(
            item.get(
                "line_id",
                -1,
            )
            or -1
        )
        in wanted
    ]

    result.sort(
        key=lambda item: int(
            item.get(
                "line_id",
                0,
            )
            or 0
        )
    )

    return result


def _um_resolve_selection(text, candidates, reference_items=None):
    return resolve_modifier_targets(text, candidates, {
        "menu": MENU_LABELS, "type": TYPE_LABELS,
        "drink": DRINK_LABELS, "drink_size": SIZE_LABELS,
        "side": SIDE_LABELS, "add_toppings": TOPPING_LABELS,
        "exclude": EXCLUDE_LABELS,
    }, reference_items=reference_items)



def _um_ambiguous_reply(
    flow,
    items,
):
    if not items:
        return _um_question(
            flow
        )

    descriptions = [
        _burger_target_description(
            item
        )
        for item in items
    ]

    if (
        descriptions
        and
        len(set(descriptions)) == 1
    ):
        return (
            f"같은 {descriptions[0]} 주문이 "
            f"{len(items)}개 있습니다. "
            "전부인지, 몇 개인지, 또는 "
            "특정 하나인지 말씀해주세요."
        )

    options = [
        (
            f"{item.get('line_id')}번 "
            f"{_burger_target_description(item)}"
        )
        for item in items
    ]

    return (
        "해당되는 버거가 여러 개 있습니다. "
        + ", ".join(options)
        + " 중 어느 것인지 말씀해주세요."
    )


def _um_build_update(
    flow,
    items,
):
    op = flow["operation"]
    key = flow["key"]

    actions = []

    for item in items:

        action = {
            "operation": "modify",
            "target": {
                "line_id": int(
                    item["line_id"]
                ),
                "item_type": None,
                "menu": None,
                "drink": None,
                "side": None,
            },
            "item": None,
            "quantity_delta": None,
            "apply_to_all": False,
            "exclude_add": [],
            "exclude_remove": [],
            "toppings_add": [],
            "toppings_remove": [],
        }

        if op == "topping_add":
            action[
                "toppings_add"
            ] = [key]

        elif op == "topping_remove":
            action[
                "toppings_remove"
            ] = [key]

        elif op == "exclude_add":
            action[
                "exclude_add"
            ] = [key]

        elif op == "exclude_remove":
            action[
                "exclude_remove"
            ] = [key]

        actions.append(
            action
        )

    return {
        "intent": "order",
        "order_id": None,
        "actions": actions,
    }


def _um_source_text(
    flow,
    items,
):
    label = flow["label"]
    op = flow["operation"]

    parts = []

    for item in items:

        line_id = item[
            "line_id"
        ]

        if op == "topping_add":
            text = (
                f"주문 {line_id}번 버거에 "
                f"{label} 토핑 추가"
            )

        elif op == "topping_remove":
            text = (
                f"주문 {line_id}번 버거의 "
                f"{label} 토핑 추가 취소"
            )

        elif op == "exclude_add":
            text = (
                f"주문 {line_id}번 버거에서 "
                f"{label} 제외"
            )

        else:
            text = (
                f"주문 {line_id}번 버거의 "
                f"{label} 제외 취소"
            )

        parts.append(text)

    return " 그리고 ".join(
        parts
    )


def reset_modifier_context():
    global _unified_modifier_pending
    _unified_modifier_pending = None


def unified_modifier_flow(text, state):
    global _unified_modifier_pending

    compact = _um_compact(text)
    if compact in {"/reset", "/resetall", "carout", "/carout"} or state.get("intent") in {"confirm", "cancel", "mobile_pickup"}:
        reset_modifier_context()
        return None

    fresh = _um_detect_request(text, state)
    if fresh and fresh.get("kind"):
        reset_modifier_context()
        reply = ("추가 가능한 토핑은 치즈(+500원), 베이컨(+800원), 패티(+900원)입니다. "
                 "토마토는 기본 재료 제외·복원으로만 변경할 수 있습니다.")
        if fresh["kind"] == "multiple_requests":
            reply = "변경할 토핑이나 재료를 한 가지씩 말씀해주세요. 아직 주문은 변경하지 않았습니다."
        elif fresh["kind"] == "non_burger":
            reply = f"{fresh['label']}에는 버거 토핑·재료 변경을 적용할 수 없습니다."
        return {"kind": "reply", "text": reply}

    selector_text = text
    if fresh is None and _unified_modifier_pending is not None:
        selector_text = normalize_modifier_target_followup(
            text, _unified_modifier_pending['operation'], TOPPING_LABELS, EXCLUDE_LABELS,
        )
        # Explicit new orders/finalization leave modifier selection; bare menu
        # names and counts remain target answers, including '두 개 빼주세요'.
        if (explicit_new_modifier_order(selector_text, MENU_LABELS, DRINK_LABELS, SIDE_LABELS)
                or any(token in compact for token in FINALIZATION_KEYWORDS)
                or compact in {
            "주문완료", "주문완료요", "이상입니다", "이상이에요", "결제할게요",
            "주문취소", "전체주문취소", "처음부터", "취소", "취소할게요",
        }):
            reset_modifier_context()
            return None

    if fresh is not None:
        flow = {key: fresh[key] for key in ("operation", "key", "label")}
        candidates = _um_eligible_items(flow, state)
        selector = fresh["selector_text"]
        reset_modifier_context()
    elif _unified_modifier_pending is not None:
        flow = dict(_unified_modifier_pending)
        allowed = set(flow["candidate_ids"])
        candidates = [item for item in _um_eligible_items(flow, state)
                      if item["line_id"] in allowed]
        selector = selector_text
    else:
        return None

    if not candidates:
        reset_modifier_context()
        return {"kind": "reply", "text": _um_no_candidate_reply(flow)}

    resolved = _um_resolve_selection(selector, candidates, state.get("items", []))
    status = resolved["status"]
    # Only a genuinely absent target may auto-select the sole eligible item.
    if fresh is not None and re.fullmatch(r"(?:요|에|에서|에게|만|버거|의|좀)*", _um_compact(selector)):
        resolved = {"status": "resolved" if len(candidates) == 1 else "ambiguous", "items": candidates}
        status = resolved["status"]

    if status == "resolved":
        chosen = resolved["items"]

        # pending 후보가 잘못 넓혀졌거나 상태가 바뀌어도
        # 실제 operation 조건을 만족하는 항목에만 update를 만든다.
        eligible_ids = {
            int(item["line_id"])
            for item in _um_eligible_items(flow, state)
        }
        if any(
            int(item.get("line_id", -1)) not in eligible_ids
            for item in chosen
        ):
            reset_modifier_context()
            return {
                "kind": "reply",
                "text": (
                    "말씀하신 버거에는 현재 "
                    f"{flow['label']} 변경을 적용할 수 없습니다. "
                    "대상을 다시 말씀해주세요."
                ),
            }

        reset_modifier_context()
        return {"kind": "apply", "flow": flow, "items": chosen,
                "update": _um_build_update(flow, chosen),
                "source_text": _um_source_text(flow, chosen)}

    # Unknown/invalid target answers must never fall through to quantity edits.
    subset = resolved["items"] if status == "ambiguous" else candidates
    _unified_modifier_pending = {**flow, "candidate_ids": [item["line_id"] for item in subset]}
    if status == "ambiguous":
        reply = _um_ambiguous_reply(flow, subset)
    elif status == "invalid_count":
        reply = "말씀하신 개수를 선택할 수 없습니다. 현재 후보의 개수나 대상 조건을 다시 말씀해주세요."
    elif status == "not_found":
        reply = "말씀하신 조건에 맞는 변경 가능한 버거를 찾지 못했습니다. 다시 선택해주세요."
    else:
        reply = _um_question(flow)
    return {"kind": "reply", "text": reply}


def main():

    start_customer_ui_server(
        host="127.0.0.1",
        port=8080,
    )

    runtime_worker = RuntimeWorker(
        DriveThruRuntime
    )

    stt_input = RosSTTUDPInput(
        host="127.0.0.1",
        port=5006,
        timeout=0.25,
    )

    speech_worker = SpeechInputWorker(
        stt_input.receive_once,
        generation_provider=lambda: (
            runtime_worker.generation
        ),
        max_queue_size=8,
        min_confidence=None,
    )

    stt_session = STTSessionController(
        speech_worker,
        input_source=stt_input,
    )

    handoff_manager = (
        OrderHandoffManager(
            storage_dir=(
                Path(__file__).resolve().parent / "runtime_data/handoffs"
            ),
            first_order_id=1,
        )
    )

    session = (
        VehicleSessionController(
            runtime_worker,
            stt_session=stt_session,
        )
    )

    debug_mode = True

    last_handoff = None

    # 모바일 주문번호는 고객 확인 후에만 FINAL HANDOFF 한다.
    pending_mobile_order_id = None

    # 대상 없는 "취소할게" 이후
    # 다음 고객 발화가 취소 대상을 지정하는 상태.
    pending_cancel_target = False

    # 버거 재료 질문에 메뉴명을 되물은 뒤 한 번의 답변을 받는다.
    pending_burger_ingredient_query = False


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
                    get_customer_input(
                        speech_worker
                    )
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
            pending_cancel_target = False

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

            session.reset_current_order()
            reset_router_history()

            pending_mobile_order_id = None
            pending_cancel_target = False
            pending_burger_ingredient_query = False


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

            stt_session.stop()

            deleted = reset_all(
                runtime_worker,
                handoff_manager,
            )

            session.vehicle_present = False

            session.state = AppState.IDLE


            last_handoff = None
            pending_mobile_order_id = None
            pending_cancel_target = False
            pending_burger_ingredient_query = False
            reset_router_history()

            system_message(
                "전체 개발 데이터 초기화 완료"
            )

            print(
                f"삭제된 데이터 : "
                f"{deleted}개"
            )

            print(
                f"다음 내부 주문번호 : {handoff_manager.next_order_id()}"
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
        # CUSTOMER -> UI
        # STT 입력이든 터미널 입력이든 여기서 한 번만 처리한다.
        # ====================================================

        ui_add_customer_message(
            text
        )

        append_router_history(
            "customer",
            text,
        )

        partial_menu_reply = pre_router_partial_menu_reply(text)
        if partial_menu_reply is not None:
            if debug_mode:
                system_message("[PARTIAL MENU GUARD] full product name required")
            soomac_say(partial_menu_reply)
            continue

        allergy_reply = pre_router_allergy_safety_reply(text)
        if allergy_reply is not None:
            pending_burger_ingredient_query = False
            if debug_mode:
                system_message("[ALLERGY SAFETY FASTPATH] verified data unavailable")
            soomac_say(allergy_reply)
            continue

        store_reply = pre_router_store_guidance_reply(text)
        if store_reply is not None:
            pending_burger_ingredient_query = False
            if debug_mode:
                system_message("[STORE GUIDANCE FASTPATH]")
            soomac_say(store_reply)
            continue

        # 되물은 버거명을 받아, 등록된 해당 버거 재료만 답한다.
        if pending_burger_ingredient_query:
            pending_burger_ingredient_query = False
            followup_burger = _explicit_burger_from_utterance(text)
            if followup_burger is not None:
                soomac_say(
                    answer_menu_query(
                        family="info_query",
                        subtype="ingredient",
                        target=followup_burger,
                        utterance="기본 재료",
                        burger_prices=BURGER_BASE_PRICE,
                    )
                )
                continue

        # 버거 종류가 빠진 재료 질문은 먼저 종류를 확인한다.
        if _is_burger_ingredient_question(text):
            requested_burger = _explicit_burger_from_utterance(text)
            if requested_burger is None:
                pending_burger_ingredient_query = True
                soomac_say(
                    "어떤 버거의 재료가 궁금하세요? "
                    "불고기버거, 치킨버거, 치즈버거, 새우버거 중 말씀해주세요."
                )
                continue

            soomac_say(
                answer_menu_query(
                    family="info_query",
                    subtype="ingredient",
                    target=requested_burger,
                    utterance="기본 재료",
                    burger_prices=BURGER_BASE_PRICE,
                )
            )
            continue

        ui_set_voice_mode(
            "processing"
        )

        # ====================================================
        # REVERSE MODIFIER TARGET FOLLOWUP
        # ====================================================

        # ====================================================
        # UNIFIED MODIFIER FLOW
        # ====================================================

        modifier_result = (
            unified_modifier_flow(
                text,
                runtime_worker.snapshot().get(
                    "state",
                    {},
                ),
            )
        )

        if modifier_result is not None:

            if (
                modifier_result["kind"]
                == "reply"
            ):

                if debug_mode:
                    system_message(
                        "[MODIFIER FLOW] "
                        "target clarification"
                    )

                soomac_say(
                    modifier_result[
                        "text"
                    ]
                )

                continue

            if (
                modifier_result["kind"]
                == "apply"
            ):

                flow = modifier_result[
                    "flow"
                ]

                line_ids = [
                    item["line_id"]
                    for item in (
                        modifier_result[
                            "items"
                        ]
                    )
                ]

                if debug_mode:
                    system_message(
                        "[MODIFIER FAST PATH] "
                        f"{flow['operation']} "
                        f"{flow['key']} -> "
                        f"lines {line_ids}"
                    )

                try:

                    result = (
                        runtime_worker
                        .process_prebuilt(
                            modifier_result[
                                "update"
                            ],
                            source_text=(
                                modifier_result[
                                    "source_text"
                                ]
                            ),
                        )
                    )

                except StaleRuntimeRequest:

                    if debug_mode:
                        system_message(
                            "[RUNTIME] "
                            "stale modifier request discarded"
                        )

                    continue

                except Exception as e:

                    traceback.print_exc()

                    system_message(
                        "[MODIFIER INTERNAL ERROR] "
                        f"{type(e).__name__}: {e}"
                    )

                    soomac_say(
                        "주문 수정 중 문제가 발생했습니다. "
                        "다시 말씀해주세요."
                    )

                    continue

                if debug_mode:
                    show_debug_result(
                        result
                    )

                ui_set_order_items(
                    result.get(
                        "state",
                        {},
                    ).get(
                        "items",
                        [],
                    )
                )

                error = result.get(
                    "error"
                )

                if error:

                    system_message(
                        f"[{error.get('kind')} / "
                        f"{error.get('code')}] "
                        f"{error.get('detail')}"
                    )

                    soomac_say(
                        result.get(
                            "reply"
                        )
                        or
                        "주문 수정 내용을 다시 말씀해주세요."
                    )

                    continue

                state = result[
                    "state"
                ]

                show_current_order(
                    state
                )

                soomac_say(
                    pending_message(
                        result.get(
                            "pending"
                        )
                    )
                )

                continue

        # Modifier requests and clarification are owned by unified_modifier_flow.

        all_category_price_reply = (
            contextual_category_price_query_reply(
                text
            )
        )

        if all_category_price_reply is None:
            all_category_price_reply = (
                all_category_price_query_reply(
                    text
                )
            )

        if all_category_price_reply is not None:

            if debug_mode:
                system_message(
                    "[INFO FASTPATH] "
                    "all category prices"
                )

            soomac_say(
                all_category_price_reply
            )

            continue

        # ----------------------------------------------------
        # BURGER SET PRICE
        # ----------------------------------------------------

        burger_set_price_reply = (
            pre_router_burger_set_price_reply(
                text
            )
        )

        if burger_set_price_reply is not None:

            if debug_mode:
                system_message(
                    "[PRE-ROUTER FAST] "
                    "burger set price"
                )

            soomac_say(
                burger_set_price_reply
            )

            continue

        # ----------------------------------------------------
        # GENERIC CATEGORY ORDER
        # ----------------------------------------------------

        generic_category_reply = (
            pre_router_generic_category_order_reply(
                text
            )
        )

        if generic_category_reply is not None:

            if debug_mode:
                system_message(
                    "[PRE-ROUTER GUARD] "
                    "generic category order blocked"
                )

            soomac_say(
                generic_category_reply
            )

            continue

        burger_menu_reply = (
            generic_burger_menu_query_reply(
                text
            )
        )

        if burger_menu_reply is not None:

            if debug_mode:
                system_message(
                    "[PRE-ROUTER FAST] "
                    "generic burger menu query"
                )

            soomac_say(
                burger_menu_reply
            )

            continue

        category_reply = (
            menu_category_followup_reply(
                text
            )
        )

        if category_reply is not None:

            if debug_mode:
                system_message(
                    "[PRE-ROUTER FAST] "
                    "menu category followup"
                )

            soomac_say(
                category_reply
            )

            continue

        # 이 고객 발화가 CANCEL TARGET에서 변환된 명령인지 표시.
        # 일반 주문에서는 항상 False로 시작한다.
        cancel_target_command_active = False

        # ====================================================
        # GREETING
        # ====================================================

        greeting_text = re.sub(
            r"[\\s!?.,~]+",
            "",
            text,
        )

        if greeting_text in {
            "안녕하세요",
            "안녕",
            "안녕하십니까",
            "반갑습니다",
            "반가워요"
        }:

            soomac_say(
                "안녕하세요 주문 내용을 말씀해주세요."
            )

            continue

        # ====================================================
        # FINALIZATION
        # ====================================================
        # 일반 주문 확정 발화도 Router를 통과시킨다.
        # "내일 끝낼게요" 같은 애매한 발화를 먼저 Router가 차단하고,
        # execute_order로 허용된 확정 발화만 아래 V14 Runtime에서
        # deterministic finalization guard를 거친다.


        # ====================================================
        # MOBILE PICKUP FAST PATH
        # ====================================================
        # 모바일 주문번호는 자연어 주문 의미가 아니라
        # 외부 주문 식별자이므로 Router 전에 처리한다.
        #
        # 예:
        # "맥오더 65번이요"
        # "모바일 주문 65번"
        #
        # 번호를 인식한 뒤에는 즉시 handoff하지 않고
        # 고객 확인을 기다린다.

        mobile_number = extract_mobile_pickup_number(
            text
        )

        if mobile_number is not None:

            pending_mobile_order_id = int(
                mobile_number
            )

            soomac_say(
                f"맥오더 "
                f"{pending_mobile_order_id}번 "
                "맞으신가요?"
            )

            continue

        if (
            re.search(
                r"(맥\s*오더|모바일\s*주문|앱\s*주문|픽업)",
                text,
                re.IGNORECASE,
            )
            and mobile_number is None
        ):
            soomac_say(
                "맥오더 주문번호를 말씀해주세요."
            )

            continue


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
                "엉",
                "웅",
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

                ui_set_order_meta(
                    order_id=handoff.get("order_id"),
                    mobile_order_id=handoff.get("mobile_order_id"),
                    total_price=handoff.get("total_price"),
                    order_mode="mobile",
                )

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
                pending_cancel_target = False

                session.finish_customer_order()

                continue

            # 긍정 응답이 아니면 번호 정정 가능성이 있으므로
            # 기존 번호를 확정하지 않고 Router -> V14 경로로 보낸다.

        # ====================================================
        # CANCEL TARGET STATE MACHINE
        # ====================================================
        # "취소할게"처럼 대상이 없는 취소는 Router V14로 바로
        # 보내지 않고, 먼저 취소 대상을 물어본다.
        #
        # 다음 고객 발화에서 대상을 해석한 뒤 명확한 command로
        # 변환해서 기존 V14 Router / Runtime 경로를 그대로 사용한다.

        cancel_target_command_active = False

        cancel_snapshot = runtime_worker.snapshot()

        cancel_state = (
            cancel_snapshot.get(
                "state",
                {},
            )
        )

        # ----------------------------------------------------
        # 이미 "어떤 걸 취소하시겠어요?"라고 물은 상태
        # ----------------------------------------------------
        if pending_cancel_target:

            cancel_resolution = (
                resolve_pending_cancel_target(
                    text,
                    cancel_state,
                )
            )

            cancel_kind = (
                cancel_resolution.get("kind")
            )

            cancel_reply = (
                cancel_resolution.get("text")
            )

            # 취소 작업 자체를 포기
            if cancel_kind == "abort":

                pending_cancel_target = False

                soomac_say(
                    cancel_reply
                    or "알겠습니다."
                )

                continue

            # 대상이 아직 불명확
            if cancel_kind == "reply":

                soomac_say(
                    cancel_reply
                    or "취소할 대상을 다시 말씀해주세요."
                )

                continue

            # 명확한 취소 명령으로 변환 성공
            if cancel_kind == "command":

                text = cancel_reply

                cancel_target_command_active = True

            else:

                soomac_say(
                    "취소할 대상을 다시 말씀해주세요."
                )

                continue

        # ----------------------------------------------------
        # 새로 들어온 대상 없는 취소
        # ----------------------------------------------------
        elif is_vague_cancel_request(text):

            current_items = (
                cancel_state.get(
                    "items",
                    [],
                )
                or []
            )

            if not current_items:

                soomac_say(
                    "현재 취소할 주문이 없어요."
                )

                continue

            pending_cancel_target = True

            soomac_say(
                "어떤 걸 취소하시겠어요?"
            )

            continue


        # ====================================================
        # TOPPING / EXCLUDE INFO FASTPATH
        # ====================================================
        # 메뉴 변경 요청이 아닌 "무엇을 선택할 수 있는지" 묻는 질문은
        # Router의 문맥 오분류를 피하기 위해 deterministic하게 응답한다.
        #
        # 예:
        #   "제외 어떤 거 할 수 있어요?"
        #   "제외 토핑 뭐 있어요?"
        #   "토핑 가능한가요?"
        #   "추가 토핑 뭐 있어요?"
        #
        # 실제 mutation:
        #   "피클 빼주세요"
        #   "베이컨 추가해주세요"
        # 는 이 경로에서 잡지 않는다.

        _info_text = re.sub(
            r"\s+",
            "",
            str(text or "").lower(),
        )

        _info_question = any(
            token in _info_text
            for token in (
                "뭐",
                "무엇",
                "어떤",
                "어느",
                "종류",
                "가능",
                "할수",
                "있어",
                "있나요",
                "되나요",
                "돼",
                "됩니까",
            )
        )

        _info_mutation = any(
            token in _info_text
            for token in (
                "빼줘",
                "빼주세요",
                "제외해줘",
                "제외해주세요",
                "없이해주세요",
                "추가해줘",
                "추가해주세요",
                "넣어줘",
                "넣어주세요",
            )
        )

        if (
            _info_question
            and not _info_mutation
            and (
                "제외" in _info_text
                or "뺄수" in _info_text
            )
        ):
            if debug_mode:
                system_message(
                    "[TOPPING/EXCLUDE INFO FASTPATH] exclude"
                )

            soomac_say(
                "제외 가능한 재료는 "
                "양파, 피클, 토마토, 양상추입니다."
            )

            continue

        if (
            _info_question
            and not _info_mutation
            and "토핑" in _info_text
        ):
            if debug_mode:
                system_message(
                    "[TOPPING/EXCLUDE INFO FASTPATH] topping"
                )

            soomac_say(
                "추가 가능한 토핑은 "
                "치즈(+500원), 베이컨(+800원), "
                "패티(+900원)입니다."
            )

            continue



        # ====================================================
        # UNSUPPORTED CHEESE EXCLUDE GUARD
        # ====================================================
        # 기본 버거 재료에서 치즈 제거는 지원하지 않는다.
        #
        # 단,
        #   "치즈버거 빼주세요"      -> 버거 자체 삭제
        #   "치즈 토핑 빼주세요"     -> 추가 토핑 제거
        #   "추가한 치즈 빼주세요"   -> 추가 토핑 제거
        # 는 기존 주문 처리 경로로 보낸다.

        _cheese_guard_text = re.sub(
            r"\s+",
            "",
            str(text or "").lower(),
        )

        _cheese_remove_request = any(
            token in _cheese_guard_text
            for token in (
                "빼줘",
                "빼주세요",
                "빼고",
                "빼달라",
                "제외해",
                "제외해주세요",
                "제외해줘",
                "없이해",
                "없이해주세요",
                "없애줘",
                "없애주세요",
            )
        )

        if (
            "치즈" in _cheese_guard_text
            and _cheese_remove_request
            and "치즈버거" not in _cheese_guard_text
            and "치즈스틱" not in _cheese_guard_text
            and "토핑" not in _cheese_guard_text
            and "추가한치즈" not in _cheese_guard_text
            and "추가치즈" not in _cheese_guard_text
        ):
            if debug_mode:
                system_message(
                    "[CAPABILITY GUARD] "
                    "unsupported cheese exclude blocked"
                )

            soomac_say(
                "치즈는 제외 가능한 재료가 아닙니다. "
                "제외 가능한 재료는 양파, 피클, 토마토, 양상추입니다."
            )

            continue



        # ====================================================
        # General non-modifier requests continue through the unchanged Router gate.

        router_snapshot = (
            runtime_worker.snapshot()
        )

        router_state = (
            router_snapshot.get(
                "state",
                {},
            )
        )

        router_pending = (
            router_snapshot.get(
                "pending"
            )
        )

        current_order_reply = (
            pre_router_current_order_query_reply(
                text,
                router_state,
            )
        )

        if current_order_reply is not None:

            if debug_mode:
                system_message(
                    "[PRE-ROUTER FAST] "
                    "current order query"
                )

            soomac_say(
                current_order_reply
            )

            continue

        # ====================================================
        # ====================================================
        # UNKNOWN MENU PRE-ROUTER GUARD
        # ====================================================

        unknown_menu_reply = (
            pre_router_unknown_menu_reply(
                text
            )
        )

        if unknown_menu_reply is not None:

            if debug_mode:
                system_message(
                    "[PRE-ROUTER GUARD] "
                    "unknown menu blocked"
                )

            soomac_say(
                unknown_menu_reply
            )

            continue

        # EMPTY BURGER TOPPING PRE-ROUTER GUARD
        # ====================================================

        empty_topping_reply = (
            pre_router_empty_burger_topping_reply(
                text,
                router_state,
            )
        )

        if empty_topping_reply is not None:

            if debug_mode:
                system_message(
                    "[PRE-ROUTER GUARD] "
                    "empty burger topping blocked"
                )

            soomac_say(
                empty_topping_reply
            )

            continue

        # ====================================================
        # UNSUPPORTED OPTION PRE-ROUTER GUARD
        #
        # 명백한 미지원 옵션은 Router가 잘못 분류하더라도
        # 주문 / read-only 경로에 진입시키지 않는다.
        # ====================================================

        unsupported_option_reply = (
            pre_router_unsupported_option_reply(
                text
            )
        )

        if unsupported_option_reply is not None:

            if debug_mode:
                system_message(
                    "[PRE-ROUTER GUARD] "
                    "unsupported option blocked"
                )

            soomac_say(
                unsupported_option_reply
            )

            continue

        # ====================================================
        # PRICE / CALORIE INFO FAST PATH
        #
        # 명확한 가격·칼로리·비교 질문은 Router V14보다 먼저
        # deterministic engine으로 처리한다.
        #
        # 처리 불가능하거나 애매하면 None -> 기존 Router V14.
        # 주문 State는 절대 변경하지 않는다.
        # ====================================================

        try:
            info_fast_result = (
                answer_price_calorie_query(
                    text,
                    state=router_state,
                    unit_price_fn=unit_price,
                )
            )
        except Exception as e:

            info_fast_result = None

            if debug_mode:
                system_message(
                    "[INFO FASTPATH ERROR] "
                    f"{type(e).__name__}: {e}"
                )

        if info_fast_result is not None:

            (
                info_fast_reply,
                info_fast_reason,
            ) = info_fast_result

            if debug_mode:
                system_message(
                    "[INFO FASTPATH] "
                    f"{info_fast_reason} / "
                    "Router V14 = SKIPPED / "
                    "Runtime V14 = SKIPPED"
                )

            soomac_say(
                info_fast_reply
            )

            continue

        try:
            router_history_context = (
                router_history_before_current_customer()
            )

            # ====================================================
            # GENERAL RECOMMENDATION PRE-ROUTER FAST PATH
            # ====================================================

            recommendation_fast_reply = (
                pre_router_fast_general_recommendation_reply(
                    text,
                    router_history_context,
                )
            )

            if recommendation_fast_reply is not None:

                if debug_mode:
                    system_message(
                        "[PRE-ROUTER FAST] "
                        "general recommendation"
                    )

                soomac_say(
                    recommendation_fast_reply
                )

                continue

            router_started = time.perf_counter()

            # ====================================================
            # EXPLICIT FULL SET PRE-ROUTER FAST PATH
            #
            # 완전히 명시된 신규 버거 세트 주문만 deterministic하게
            # 처리한다. 조금이라도 애매하면 기존 V14 Router로 fallback.
            # ====================================================

            pre_router_output = (
                route_explicit_full_set_pre_fastpath(
                    text,
                    pending=router_pending,
                    order_state=router_state,
                )
            )

            if pre_router_output is not None:

                router_source = (
                    "FULL_SET_PREFASTPATH"
                )

                router_output = (
                    pre_router_output
                )

            else:

                speed_pre_router_output = (
                    route_speed_pre_fastpath(
                        text,
                        history=router_history_context,
                        pending=router_pending,
                        order_state=router_state,
                    )
                )

                if (
                    speed_pre_router_output
                    is not None
                ):

                    router_source = (
                        "SPEED_PREFASTPATH"
                    )

                    router_output = (
                        speed_pre_router_output
                    )

                else:

                    router_source = "V14"

                    router_output = (
                        route_customer_utterance(
                            text,
                            history=router_history_context,
                            pending=router_pending,
                            order_state=router_state,
                            timeout=60.0,
                        )
                    )

            router_elapsed = (
                time.perf_counter()
                - router_started
            )

            router_decisions = (
                evaluate_router_output(
                    router_output
                )
            )

        except RouterClientError as e:
            system_message(
                "[ROUTER ERROR] "
                f"{e}"
            )

            soomac_say(
                "주문 내용을 판단하는 중 문제가 발생했습니다. "
                "다시 말씀해주세요."
            )

            continue

        except Exception as e:
            traceback.print_exc()

            system_message(
                "[ROUTER INTERNAL ERROR] "
                f"{type(e).__name__}: "
                f"{e}"
            )

            soomac_say(
                "주문 내용을 다시 말씀해주세요."
            )

            continue

        if debug_mode:
            system_message(
                "[ROUTER SOURCE] "
                f"{router_source} / "
                f"{router_elapsed:.3f}s"
            )

            show_debug_router(
                router_output,
                router_decisions,
            )

        router_statuses = [
            decision.status.value
            for decision in router_decisions
        ]

        # ====================================================
        # ====================================================
        # ====================================================
        # BURGER EXCLUDE TARGET GUARD
        # ====================================================

        exclude_target_reply = (
            burger_exclude_state_guard_reply(
                router_output,
                text,
                router_state,
            )
        )

        if exclude_target_reply is not None:

            if debug_mode:
                system_message(
                    "[STATE GUARD] "
                    "burger exclude target ambiguous"
                )

            soomac_say(
                exclude_target_reply
            )

            continue

        # MISSING BURGER MODIFY GUARD
        # ====================================================

        # ====================================================
        # BURGER TOPPING STATE GUARD
        # ====================================================

        topping_state_reply = (
            burger_topping_state_guard_reply(
                router_output,
                text,
                router_state,
            )
        )

        if topping_state_reply is not None:

            if debug_mode:
                system_message(
                    "[STATE GUARD] "
                    "burger topping mutation blocked"
                )

            soomac_say(
                topping_state_reply
            )

            continue

        missing_burger_reply = (
            missing_burger_modify_reply(
                router_output,
                text,
                router_state,
            )
        )

        if missing_burger_reply is not None:

            if debug_mode:
                system_message(
                    "[STATE GUARD] "
                    "missing burger modify blocked"
                )

            soomac_say(
                missing_burger_reply
            )

            continue


        # TOPPING CAPABILITY GUARD
        # ====================================================
        # Router 오판과 관계없이 음료/사이드에는
        # 추가 topping mutation을 허용하지 않는다.

        topping_capability_reply = (
            unsupported_topping_mutation_reply(
                router_output,
                text,
            )
        )

        if topping_capability_reply is not None:

            if debug_mode:
                system_message(
                    "[CAPABILITY GUARD] "
                    "unsupported topping target blocked"
                )

            soomac_say(
                topping_capability_reply
            )

            continue

        # Router가 하나라도 clarify를 요구하면 mutation 금지.
        if "clarify" in router_statuses:
            soomac_say(
                router_clarify_reply(
                    router_pending
                )
            )
            continue

        # 조건부 주문은 조건 확인 orchestration 전까지 mutation 금지.
        if "wait_condition" in router_statuses:
            soomac_say(
                "조건 확인이 필요한 주문입니다. "
                "가능 여부를 먼저 확인한 뒤 다시 주문해주세요."
            )
            continue

        # 직원 호출 등 주문 State 밖 event.
        if "event" in router_statuses:
            handle_router_event(
                router_output
            )
            continue

        # 정보 조회 / 추천 / 대화는 읽기 전용.
        if "read_only" in router_statuses:
            soomac_say(
                router_read_only_reply(
                    router_output,
                    text,
                    router_state,
                )
            )
            continue

        # 거부 / 단순 acknowledgement.
        if "no_action" in router_statuses:
            soomac_say(
                "알겠습니다."
            )
            continue

        # 실제 주문 mutation은 모든 act가 execute_order일 때만 허용.
        if not (
            router_statuses
            and all(
                status == "execute_order"
                for status in router_statuses
            )
        ):
            system_message(
                "[ROUTER] unsupported policy combination: "
                f"{router_statuses}"
            )

            soomac_say(
                "주문 내용을 조금 더 명확하게 말씀해주세요."
            )
            continue

        # 여기까지 온 경우에만 아래 V14 Runtime이 주문 State를 변경할 수 있다.

        # ====================================================
        # STAFF CALL
        # ====================================================
        # 직원 호출은 LLM을 거치지 않는다.
        # 주문 State도 절대 변경하지 않는다.

        if is_staff_call_utterance(
            text
        ):

            ui_request_staff_call()

            system_message(
                "직원 호출 요청"
            )

            soomac_say(
                "직원을 호출하겠습니다. "
                "잠시만 기다려주세요."
            )

            continue


        # ====================================================
        # ROUTER FAST PATH
        # ====================================================

        fast_update, fast_reason = build_router_fastpath(
            router_output,
            text,
            router_state,
            router_pending,
        )

        try:

            if fast_update is not None:

                runtime_text = build_router_runtime_text(
                    router_output,
                    text,
                )

                if debug_mode:
                    system_message(
                        "[FAST PATH] "
                        f"{fast_reason} / "
                        "second V14 call = SKIPPED"
                    )

                result = runtime_worker.process_prebuilt(
                    fast_update,
                    source_text=runtime_text,
                )

            else:

                if debug_mode:
                    system_message(
                        "[FAST PATH FALLBACK] "
                        f"{fast_reason}"
                    )

                # ================================================
                # CUSTOMER INPUT GUARD
                # 복잡/불확실 주문 fallback에서만 사용
                # ================================================

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

                # ================================================
                # EXISTING V14 FALLBACK
                # ================================================

                # ================================================
                # COMPLEX BURGER ADD
                # ================================================
                # 새 버거 주문에 재료 제외/토핑 추가가 같이 있으면
                # Router canonical text가 옵션 정보를 잃을 수 있다.
                #
                # 예:
                #   새우버거에 피클 빼서 단품 하나 추가
                #   새우버거에 치즈 추가해서 하나
                #
                # 이 경우에는 고객 원문을 Runtime V14에 그대로 전달한다.
                # 그 외 fallback은 기존 canonicalization을 유지한다.

                preserve_original_runtime_text = (
                    explicit_new_burger_order_request(
                        text,
                        router_state,
                    )
                )

                if (
                    not preserve_original_runtime_text
                    and len(router_output.acts) == 1
                ):

                    runtime_act = router_output.acts[0]

                    runtime_family = _router_value(
                        getattr(
                            runtime_act,
                            "family",
                            None,
                        )
                    )

                    runtime_subtype = _router_value(
                        getattr(
                            runtime_act,
                            "subtype",
                            None,
                        )
                    )

                    runtime_domain = _router_value(
                        getattr(
                            runtime_act,
                            "target_domain",
                            None,
                        )
                    )

                    if (
                        runtime_family == "order_action"
                        and runtime_subtype == "add"
                        and runtime_domain == "burger"
                    ):
                        from router_fastpath import (
                            _burger_add_has_explicit_modifiers,
                        )

                        preserve_original_runtime_text = (
                            _burger_add_has_explicit_modifiers(
                                text
                            )
                        )

                if preserve_original_runtime_text:

                    runtime_text = text

                    if debug_mode:
                        system_message(
                            "[ROUTER->RUNTIME] "
                            "explicit new burger order -> "
                            "original utterance preserved"
                        )

                else:

                    runtime_text = build_router_runtime_text(
                        router_output,
                        text,
                    )

                    if (
                        debug_mode
                        and runtime_text != text
                    ):
                        system_message(
                            "[ROUTER->RUNTIME] "
                            f"{text!r} -> {runtime_text!r}"
                        )

                result = runtime_worker.process(
                    runtime_text
                )

        except StaleRuntimeRequest:

            if debug_mode:
                system_message(
                    "[RUNTIME] "
                    "stale customer request discarded"
                )

            continue

        except Exception as e:

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

        # Runtime이 확정한 현재 주문 상태를
        # 고객 화면에 그대로 전달한다.
        ui_set_order_items(
            result.get(
                "state",
                {},
            ).get(
                "items",
                [],
            )
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

        # 취소 대상 선택을 통해 만들어진 mutation이
        # Runtime까지 정상 처리된 경우에만 pending 종료.
        if cancel_target_command_active:
            pending_cancel_target = False

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

            ui_set_order_meta(
                order_id=handoff.get("order_id"),
                mobile_order_id=None,
                total_price=handoff.get("total_price"),
                order_mode="counter",
            )

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


    stt_session.stop()
    speech_worker.stop()
    stt_input.close()
    runtime_worker.stop()

    stop_customer_ui_server()


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":
    main()
