#!/usr/bin/env python3
import copy
import json
import re
import unicodedata
from typing import Any

from openai import OpenAI


class OrderRuntimeError(Exception):
    """SOOMAC 주문 런타임에서 의도적으로 처리하는 오류의 기반 클래스."""

    kind = "order_runtime"
    default_code = "ORDER_RUNTIME_ERROR"
    default_reply = "주문 처리 중 문제가 발생했습니다. 다시 말씀해주세요."

    def __init__(self, message, *, code=None, reply=None):
        super().__init__(message)
        self.code = code or self.default_code
        self.reply = reply or self.default_reply


class RecoverableOrderError(OrderRuntimeError):
    """고객에게 다시 질문하면 정상적으로 복구 가능한 주문 오류."""

    kind = "recoverable"
    default_code = "RECOVERABLE_ORDER_ERROR"
    default_reply = (
        "주문 내용을 정확히 이해하지 못했습니다. "
        "다시 말씀해주세요."
    )


class ModelOutputError(OrderRuntimeError):
    """LLM이 구조적/의미적으로 사용할 수 없는 update를 만든 경우."""

    kind = "model_output"
    default_code = "MODEL_OUTPUT_ERROR"
    default_reply = (
        "주문 해석이 정확하지 않았습니다. "
        "주문 내용을 다시 말씀해주세요."
    )


class InternalInvariantError(OrderRuntimeError):
    """정상적인 런타임 흐름이라면 발생하면 안 되는 내부 상태 오류."""

    kind = "internal_invariant"
    default_code = "INTERNAL_INVARIANT_ERROR"
    default_reply = (
        "주문 처리 중 문제가 발생했습니다. "
        "다시 말씀해주세요."
    )

from order_update_schema import OrderUpdate


API_BASE = "http://127.0.0.1:8000/v1"
API_KEY = "EMPTY"
MODEL_NAME = "drive-thru-v14"

SYSTEM_PROMPT = (
    "너는 드라이브스루 주문 파서다. 현재 주문 상태, 현재 확인 중인 항목, "
    "사용자 발화를 읽고 OrderUpdate JSON만 출력한다. "
    "사용자가 직접 말하지 않은 값은 추측하지 않는다."
)

SELECTION_GUIDANCE = (
    "각 line_id는 상품 한 개다. 일부만 변경할 때 선택한 각 line_id에 modify를 출력한다. "
    "같은 종류 중 두 번째는 해당 종류의 상품을 line_id 순서로 센 두 번째다. "
    "그중 두 개처럼 임의 선택이면 조건에 맞는 앞 두 개를 선택한다. "
    "recent_selection은 직전에 추가/수정한 상품 번호다. 그 두 개 같은 지시어는 이 목록을 참조한다. "
    "그 두 개 중 하나와 다른 하나는 서로 다른 번호에 각각 수정한다. "
    "수정에서는 변경 필드만 출력하며 기존 수량과 나머지 옵션은 유지한다."
)


def repair_add_items(data):
    """
    add action의 상품 종류를 보완하고, 상품 종류상 명백히 불가능한
    교차 필드 hallucination만 제거한다.

    여기서는 자연어 의미를 다시 해석하지 않는다.
    - drink에 burger/side 전용 필드가 붙으면 제거
    - side에 burger/drink 전용 필드가 붙으면 제거
    - 실제 옵션 의미 검증(drink_size 등)은 verify_semantics가 담당
    """
    data = copy.deepcopy(data)
    warnings = []

    if data.get("intent") != "order":
        return data, warnings

    for index, action in enumerate(data.get("actions", []), 1):
        if action.get("operation") != "add":
            continue

        item = action.get("item")
        if not isinstance(item, dict) or not item:
            raise ModelOutputError(f"action[{index}]: LLM이 추가할 상품 정보를 누락했습니다.")

        kind = item.get("item_type")
        menu, drink, side = (item.get(k) for k in ("menu", "drink", "side"))

        if kind is None:
            if menu in MENU_ALIASES:
                kind = "burger"
            elif menu is None and drink in DRINK_ALIASES and side is None:
                kind = "drink"
            elif menu is None and drink is None and side in SIDE_ALIASES:
                kind = "side"
            else:
                raise ModelOutputError(
                    f"action[{index}]: 상품 종류가 누락됐고 식별 정보가 불명확합니다."
                )

            item["item_type"] = kind
            warnings.append(
                f"action[{index}]: 상품 식별 필드로 item_type={kind} 누락 보완"
            )

        if kind not in {"burger", "drink", "side"}:
            raise ModelOutputError(f"action[{index}]: 지원하지 않는 상품 종류입니다.")

        # --------------------------------------------------------
        # 구조적으로 불가능한 cross-type field만 제거한다.
        # LLM이 독립 음료에 side를 붙였다고 주문 전체를 죽이지 않는다.
        # --------------------------------------------------------
        incompatible = {
            "drink": ("menu", "type", "side"),
            "side": ("menu", "type", "drink", "drink_size"),
            "burger": (),
        }[kind]

        for field in incompatible:
            value = item.get(field)
            if value is not None:
                item.pop(field, None)
                warnings.append(
                    f"action[{index}]: {kind}에 적용할 수 없는 "
                    f"{field}={value} 제거"
                )

    return data, warnings


def reconcile_subset_count(utterance, state, recent_selection, data):
    """명시된 'N개만'과 수정 대상 수를 대조한다. 복합 요청은 임의 축소하지 않는다."""
    data = copy.deepcopy(data)
    warnings = []
    matches = list(re.finditer(r"(?P<count>[0-9]+|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*개\s*만", utterance))
    if not matches or data.get("intent") != "order":
        return data, warnings
    actions = data.get("actions", [])
    if not any(a.get("operation") == "modify" for a in actions):
        return data, warnings
    if len(matches) != 1 or any(a.get("operation") != "modify" for a in actions):
        raise RecoverableOrderError("일부 변경의 대상 수를 확인할 수 없습니다. 변경할 상품을 나누어 말씀해주세요.")
    token = matches[0].group("count")
    numbers = {"한":1,"두":2,"세":3,"네":4,"다섯":5,"여섯":6,"일곱":7,"여덟":8,"아홉":9,"열":10}
    count = int(token) if token.isdigit() else numbers[token]
    if count < 1:
        raise RecoverableOrderError("변경할 상품 개수는 1개 이상이어야 합니다.")
    items = {x["line_id"]: x for x in state.get("items", [])}
    ids = []
    for action in actions:
        target = action.get("target") or {}
        line_id = target.get("line_id")
        if action.get("apply_to_all") or line_id not in items:
            raise ModelOutputError("일부 변경에는 존재하는 개별 상품 번호가 필요합니다.")
        if any(v is not None and items[line_id].get(k) != v for k,v in target.items()):
            raise ModelOutputError("변경 대상의 상품 정보가 일치하지 않습니다.")
        ids.append(line_id)
    if len(set(ids)) != len(ids):
        raise ModelOutputError("일부 변경에 같은 상품 번호가 중복되었습니다.")
    if len(ids) == count:
        return data, warnings
    # '그중'으로 지칭한 동일 상품 묶음에서 동일한 수정이 과다 생성된 경우만 축소.
    refers_to_group = bool(re.search(r"그\s*중", utterance))
    recent = set(recent_selection)
    def effect(a):
        return {k:v for k,v in a.items() if k not in {"target", "apply_to_all"}}
    same_effect = all(effect(a) == effect(actions[0]) for a in actions)
    same_items = len({signature(items[i]) for i in ids}) == 1
    if (len(ids) > count and refers_to_group and set(ids) == recent
            and same_effect and same_items):
        selected = sorted(ids)[:count]
        data["actions"] = [a for i in selected for a in actions if a["target"]["line_id"] == i]
        warnings.append(f"일부 변경 수량 보정: 요청 {count}개 / LLM {len(ids)}개 -> 동일 상품 {selected} 선택")
        return data, warnings
    raise ModelOutputError(f"요청은 {count}개 변경인데 모델이 {len(ids)}개를 선택했습니다. 주문은 변경하지 않았습니다.")


def val(x):
    return getattr(x, "value", x)


def dump_model(x):
    if x is None:
        return {}
    if hasattr(x, "model_dump"):
        return x.model_dump(mode="json", exclude_none=True)
    return copy.deepcopy(x)


def close_schema(node: Any):
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            node["additionalProperties"] = False
        for v in node.values():
            close_schema(v)
    elif isinstance(node, list):
        for v in node:
            close_schema(v)
    return node


ORDER_UPDATE_SCHEMA = close_schema(
    copy.deepcopy(OrderUpdate.model_json_schema())
)


# ============================================================
# deterministic mobile order-id parser
# ============================================================

DIGITS = {
    "영": 0, "공": 0,
    "일": 1, "이": 2, "삼": 3, "사": 4, "오": 5,
    "육": 6, "칠": 7, "팔": 8, "구": 9,
}
UNITS = {"십": 10, "백": 100, "천": 1000}


def korean_number(text: str):
    text = re.sub(r"\s+", "", text)
    if not text:
        return None

    total = 0
    digit = None

    for ch in text:
        if ch in DIGITS:
            digit = DIGITS[ch]
        elif ch in UNITS:
            total += (1 if digit is None else digit) * UNITS[ch]
            digit = None
        else:
            return None

    if digit is not None:
        total += digit

    return total


def extract_order_id(utterance: str):
    candidates = []

    for m in re.finditer(r"(?<!\d)(\d{1,3})\s*번", utterance):
        n = int(m.group(1))
        if 1 <= n <= 999:
            candidates.append((m.start(), n))

    pat = (
        r"([영공일이삼사오육칠팔구십백천]"
        r"(?:\s*[영공일이삼사오육칠팔구십백천])*)\s*번"
    )
    for m in re.finditer(pat, utterance):
        n = korean_number(m.group(1))
        if n is not None and 1 <= n <= 999:
            candidates.append((m.start(), n))

    # "주문번호는 427"처럼 '번'이 빠진 STT도 처리
    prefixes = r"(?:주문\s*번호|주문번호|픽업\s*번호|맥오더\s*번호)"
    for m in re.finditer(prefixes + r"\s*(?:는|은|이|가|:)?\s*(\d{1,3})(?!\d)", utterance):
        n = int(m.group(1))
        if 1 <= n <= 999:
            candidates.append((m.start(1), n))

    for m in re.finditer(
        prefixes
        + r"\s*(?:는|은|이|가|:)?\s*"
        + r"([영공일이삼사오육칠팔구십백천]+)",
        utterance,
    ):
        n = korean_number(m.group(1))
        if n is not None and 1 <= n <= 999:
            candidates.append((m.start(1), n))

    if not candidates:
        return None

    # "184번 아니고 427번" -> 마지막 번호 427
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]


def mobile_context(utterance, state, update):
    words = (
        "모바일", "맥오더", "픽업", "앱 주문", "앱으로",
        "미리 주문", "주문번호", "주문 번호",
    )
    return (
        val(update.intent) == "mobile_pickup"
        or state.get("intent") == "mobile_pickup"
        or any(w in utterance for w in words)
    )


def verify_order_id(utterance, state, update):
    warnings = []

    if not mobile_context(utterance, state, update):
        return update, warnings

    deterministic = extract_order_id(utterance)
    if deterministic is None:
        return update, warnings

    if update.order_id is not None and update.order_id != deterministic:
        warnings.append(
            f"order_id 보정: LLM={update.order_id} -> deterministic={deterministic}"
        )

    update = update.model_copy(update={"order_id": deterministic})
    return update, warnings


# ============================================================
# deterministic semantic verifier
# ============================================================

# XGrammar는 JSON 구조를 강제하지만 의미적으로 틀린 enum 값까지 막지는 못한다.
# 따라서 사용자가 실제로 말한 옵션과 LLM 출력이 충돌하거나,
# 사용자가 말하지 않은 옵션을 LLM이 임의로 채운 경우 Python에서 보정한다.

MENU_ALIASES = {
    "bulgogi_burger": ("불고기버거", "불고기 버거", "불고기"),
    "chicken_burger": ("치킨버거", "치킨 버거", "치킨"),
    "cheese_burger": ("치즈버거", "치즈 버거"),
    "shrimp_burger": ("새우버거", "새우 버거", "새우"),
}

TYPE_ALIASES = {
    "single": ("단품",),
    "set": ("세트", "셋트"),
}

DRINK_ALIASES = {
    "zero_coke": (
        "제로콜라", "제로 콜라", "콜라제로", "콜라 제로",
        "제로코크", "제로 코크", "제로",
    ),
    "coke": ("코카콜라", "코카 콜라", "콜라", "코크"),
    "sprite": ("스프라이트", "사이다"),
    "fanta": ("환타", "판타", "환타오렌지", "판타오렌지"),
    "iced_coffee": (
        "아이스커피", "아이스 커피", "아메리카노", "아이스아메리카노",
        "아이스 아메리카노", "커피",
    ),
}

SIZE_ALIASES = {
    "small": (
        "스몰", "작은사이즈", "작은 사이즈", "작은걸로", "작은 걸로",
        "작게", "작은거", "작은 거",
    ),
    "medium": (
        "미디엄", "미디움", "미듐", "중간사이즈", "중간 사이즈",
        "중간걸로", "중간 걸로", "보통사이즈", "보통 사이즈", "보통으로",
    ),
    "large": (
        "라지", "큰사이즈", "큰 사이즈", "큰걸로", "큰 걸로",
        "크게", "큰거", "큰 거",
    ),
}

SIDE_ALIASES = {
    "french_fries": (
        "감자튀김", "감자 튀김", "감튀", "프렌치프라이", "프렌치 프라이",
        "후렌치후라이", "후렌치 후라이",
    ),
    "cheese_stick": ("치즈스틱", "치즈 스틱"),
}


# 기본 버거에서 제외 가능한 재료.
# "피클 빼놓은 버거" 같은 reference 서술을 명령으로 오인하지 않도록
# 아래 deterministic 보정은 강한 요청 표현("빼주세요", "제외해주세요" 등)만 잡는다.
EXCLUDE_INGREDIENT_ALIASES = {
    "onion": ("양파",),
    "pickle": ("피클",),
    "tomato": ("토마토",),
    "cheese": ("치즈",),
    "lettuce": ("양상추",),
}


def explicit_exclude_request_values(text: str):
    """
    사용자가 '재료를 빼달라'고 명시적으로 요청한 값만 반환한다.

    reference 표현:
        "피클 빼놓은 버거에 베이컨 추가"
    는 잡지 않는다.

    명령 표현:
        "피클은 빼주세요"
        "양파 제외해주세요"
        "토마토 없이 해주세요"
    는 잡는다.
    """
    raw = compact_text(text)
    found = set()

    for value, aliases in EXCLUDE_INGREDIENT_ALIASES.items():
        for alias in aliases:
            a = re.escape(compact_text(alias))

            # 조사 허용: 피클은/피클을/피클도/피클만...
            stem = rf"{a}(?:은|는|을|를|도|만)?"

            # 명시적 부정은 제외.
            negative_patterns = (
                rf"{stem}빼지(?:마|말)",
                rf"{stem}제외하지(?:마|말)",
                rf"{stem}없애지(?:마|말)",
            )
            if any(re.search(p, raw) for p in negative_patterns):
                continue

            # reference '빼놓은/제외한'은 포함하지 않는다.
            request_patterns = (
                rf"{stem}빼(?:주세요|줘|주라|주실래|주실까요|주십시오)",
                rf"{stem}제외(?:해주세요|해줘|해주라|해주실래|해주십시오)",
                rf"{stem}없이(?:해주세요|해줘|주세요|줘)",
            )

            if any(re.search(p, raw) for p in request_patterns):
                found.add(value)
                break

    return found


def repair_pending_explicit_field(utterance: str, state, pending, update):
    """
    현재 pending 필드의 답을 사용자가 명시했는데 LLM이 item patch를
    비우거나 해당 필드를 누락한 경우, 그 pending 필드만 deterministic하게 복원한다.

    예:
        pending=(1, "type")
        사용자: "단품으로 하는데 피클은 빼주세요"
        LLM: modify(line_id=1), item=None, exclude_add=["pickle"]
        -> item={"type": "single"}를 복원

    안전 조건:
    - intent=order
    - pending 필드가 type/drink/drink_size/side 중 하나
    - 사용자 발화에서 그 필드의 후보가 정확히 1개
    - modify action이 정확히 1개
    - 그 action이 현재 pending line을 대상으로 함
    """
    warnings = []

    if pending is None or val(update.intent) != "order":
        return update, warnings

    _, field = pending
    if field not in {"type", "drink", "drink_size", "side"}:
        return update, warnings

    explicit = explicit_values_by_field(utterance)
    candidates = explicit.get(field, set())
    if len(candidates) != 1:
        return update, warnings

    expected = next(iter(candidates))
    data = update.model_dump(mode="json", exclude_none=True)
    actions = data.get("actions", [])
    modifies = [a for a in actions if a.get("operation") == "modify"]

    if len(modifies) != 1:
        return update, warnings

    action = modifies[0]
    if not _action_targets_pending(action, state, pending):
        return update, warnings

    patch = action.get("item")
    if not isinstance(patch, dict):
        patch = {}

    current = patch.get(field)
    if current == expected:
        return update, warnings

    patch[field] = expected
    action["item"] = patch

    if current is None:
        warnings.append(
            f"pending 보정: {field} 누락 -> {expected} 추가 "
            f"(사용자 발화에 명시됨)"
        )
    else:
        warnings.append(
            f"pending 보정: {field} {current} -> {expected}"
        )

    return OrderUpdate.model_validate(data), warnings


def repair_pending_burger_modifiers(utterance: str, state, pending, update):
    """
    pending 옵션에 답하면서 같은 문장에 재료 제외까지 말한 경우,
    LLM이 pending 필드만 출력하고 exclude_add를 누락해도 복원한다.

    예:
        pending=(2, "type")
        "단품으로 하는데 피클은 빼주세요"
        -> type=single + exclude_add=["pickle"]

    안전 범위:
    - 현재 pending 대상이 burger
    - order intent
    - modify action이 정확히 1개
    - 그 action이 pending line을 대상으로 함
    - 강한 제외 요청 표현만 사용
    """
    warnings = []

    if pending is None or val(update.intent) != "order":
        return update, warnings

    pending_line_id, _ = pending
    pending_item = next(
        (item for item in state.get("items", [])
         if item.get("line_id") == pending_line_id),
        None,
    )
    if not pending_item or pending_item.get("item_type") != "burger":
        return update, warnings

    requested = explicit_exclude_request_values(utterance)
    if not requested:
        return update, warnings

    data = update.model_dump(mode="json", exclude_none=True)
    actions = data.get("actions", [])
    modifies = [a for a in actions if a.get("operation") == "modify"]

    if len(modifies) != 1:
        return update, warnings

    action = modifies[0]
    if not _action_targets_pending(action, state, pending):
        return update, warnings

    existing = set(action.get("exclude_add") or [])
    missing = requested - existing
    if not missing:
        return update, warnings

    action["exclude_add"] = sorted(existing | requested)
    warnings.append(
        "pending 보정: 같은 발화의 명시적 재료 제외 복원 -> "
        + ", ".join(sorted(missing))
    )

    return OrderUpdate.model_validate(data), warnings

FINALIZATION_KEYWORDS = (
    "마무리",
    "주문확정",
    "확정할게",
    "확정해",
    "확정해주세요",
    "이대로할게",
    "이대로해주세요",
    "이대로주문",
    "그대로할게",
    "그대로해주세요",
    "결제할게",
    "결제해주세요",
    "결제진행",
    "주문끝",
    "주문종료",
    "끝낼게",
)

PENDING_FIELD_NAME = {
    "type": "단품/세트",
    "drink": "음료 종류",
    "drink_size": "음료 크기",
    "side": "사이드 메뉴",
}


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    return "".join(
        ch for ch in text
        if unicodedata.category(ch) != "Cf"
    )


def compact_text(text: str) -> str:
    return re.sub(r"\s+", "", normalize_text(text).lower())


def _compact_aliases(aliases):
    return tuple(compact_text(x) for x in aliases)


def _extract_values(text: str, alias_map):
    """
    한 발화에 명시된 enum 후보들을 집합으로 반환한다.

    예:
        "미디움으로" -> {"medium"}
        "콜라랑 사이다" -> {"coke", "sprite"}
    """
    raw = compact_text(text)
    found = set()

    for value, aliases in alias_map.items():
        for alias in _compact_aliases(aliases):
            if alias and alias in raw:
                found.add(value)
                break

    return found


def explicit_menu_values(text: str):
    return _extract_values(text, MENU_ALIASES)


def explicit_type_values(text: str):
    return _extract_values(text, TYPE_ALIASES)


def explicit_size_values(text: str):
    return _extract_values(text, SIZE_ALIASES)


def explicit_side_values(text: str):
    return _extract_values(text, SIDE_ALIASES)


def explicit_drink_values(text: str):
    """
    '제로콜라' 안의 '콜라' 때문에 coke까지 동시에 잡히는 것을 방지한다.
    다만 실제로 '콜라 말고 제로콜라'처럼 둘 다 말한 경우에는 둘 다 잡힌다.
    """
    raw = compact_text(text)
    found = set()

    zero_aliases = _compact_aliases(DRINK_ALIASES["zero_coke"])
    zero_spans = []

    for alias in zero_aliases:
        if not alias:
            continue
        start = 0
        while True:
            idx = raw.find(alias, start)
            if idx < 0:
                break
            zero_spans.append((idx, idx + len(alias)))
            start = idx + 1

    if zero_spans:
        found.add("zero_coke")

    def inside_zero_span(start, end):
        return any(start >= a and end <= b for a, b in zero_spans)

    for value, aliases in DRINK_ALIASES.items():
        if value == "zero_coke":
            continue

        for alias in _compact_aliases(aliases):
            if not alias:
                continue

            start = 0
            matched = False

            while True:
                idx = raw.find(alias, start)
                if idx < 0:
                    break

                end = idx + len(alias)

                if value == "coke" and inside_zero_span(idx, end):
                    start = idx + 1
                    continue

                matched = True
                break

            if matched:
                found.add(value)
                break

    return found


# ------------------------------------------------------------
# ordered explicit values / deterministic quantity
# ------------------------------------------------------------

QUANTITY_WORDS = {
    "한": 1, "하나": 1,
    "두": 2, "둘": 2,
    "세": 3, "셋": 3,
    "네": 4, "넷": 4,
    "다섯": 5,
    "여섯": 6,
    "일곱": 7,
    "여덟": 8,
    "아홉": 9,
    "열": 10,
}

CORRECTION_WORDS = (
    "말고", "아니고", "대신", "변경", "바꿔", "바꾸",
)


def extract_explicit_quantity(utterance: str):
    """
    신규 add 품목의 명시 수량을 deterministic하게 읽는다.

    예:
        "불고기버거 세트 2개요" -> 2
        "콜라 두 잔 주세요" -> 2
        "치즈스틱 세 개" -> 3
        "2개 말고 3개요" -> 3 (마지막 명시 수량)

    여러 품목이 한 문장에 있는 경우에는 verifier 쪽에서
    add action이 하나일 때만 이 값을 사용한다.
    """
    text = normalize_text(utterance).lower()
    candidates = []

    # 숫자 + 개/잔/세트
    for m in re.finditer(r"(?<!\d)(\d{1,2})\s*(?:개|잔|세트)(?:요|만|주세요|주세용)?", text):
        n = int(m.group(1))
        if 1 <= n <= 99:
            candidates.append((m.start(), n))

    # 한/두/세... + 개/잔/세트
    qty_words = sorted(QUANTITY_WORDS.keys(), key=len, reverse=True)
    qty_pat = "|".join(re.escape(x) for x in qty_words)
    for m in re.finditer(rf"({qty_pat})\s*(?:개|잔|세트)(?:요|만|주세요|주세용)?", text):
        candidates.append((m.start(), QUANTITY_WORDS[m.group(1)]))

    # 자연스러운 "하나요 / 둘이요"도 지원.
    # 단, 메뉴/품목 add action이 하나일 때만 실제 보정에 쓰므로 과보정을 줄인다.
    tail_words = {
        "하나": 1,
        "둘": 2,
        "셋": 3,
        "넷": 4,
    }
    tail_pat = "|".join(re.escape(x) for x in tail_words)
    for m in re.finditer(rf"({tail_pat})\s*(?:요|이요|주세요)(?![가-힣])", text):
        candidates.append((m.start(), tail_words[m.group(1)]))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]


def _ordered_alias_values(text: str, alias_map):
    """
    발화에 등장한 enum 값을 실제 언급 순서대로 반환한다.

    긴 alias를 우선 선택하여
    '제로콜라' 안의 '콜라', '아이스커피' 안의 '커피'처럼
    포함 관계인 짧은 alias가 별도 값으로 중복 검출되는 것을 막는다.
    """
    raw = compact_text(text)
    matches = []

    for value, aliases in alias_map.items():
        unique_aliases = sorted(
            {compact_text(alias) for alias in aliases if compact_text(alias)},
            key=len,
            reverse=True,
        )

        for alias in unique_aliases:
            start = 0
            while True:
                idx = raw.find(alias, start)
                if idx < 0:
                    break
                matches.append((idx, idx + len(alias), value, len(alias)))
                start = idx + 1

    # 같은 시작점에서는 긴 alias 우선
    matches.sort(key=lambda x: (x[0], -x[3]))

    selected = []
    occupied = []

    for start, end, value, length in matches:
        if any(not (end <= a or start >= b) for a, b in occupied):
            continue
        selected.append((start, value))
        occupied.append((start, end))

    selected.sort(key=lambda x: x[0])
    return [value for _, value in selected]


def ordered_values_for_field(utterance: str, field: str):
    alias_map = {
        "menu": MENU_ALIASES,
        "type": TYPE_ALIASES,
        "drink": DRINK_ALIASES,
        "drink_size": SIZE_ALIASES,
        "side": SIDE_ALIASES,
    }.get(field)

    if alias_map is None:
        return []

    return _ordered_alias_values(utterance, alias_map)


def is_correction_utterance(utterance: str) -> bool:
    raw = compact_text(utterance)
    return any(word in raw for word in CORRECTION_WORDS)


def explicit_values_by_field(utterance: str):
    return {
        "menu": explicit_menu_values(utterance),
        "type": explicit_type_values(utterance),
        "drink": explicit_drink_values(utterance),
        "drink_size": explicit_size_values(utterance),
        "side": explicit_side_values(utterance),
    }


def _set_or_remove_field(
    patch,
    field,
    explicit_values,
    warnings,
    prefix,
    *,
    fill_missing=False,
):
    """
    patch[field]를 사용자 발화의 명시 값과 비교한다.

    - LLM이 값을 냈는데 사용자가 말하지 않음 -> 제거
    - 사용자가 한 값만 명시했는데 LLM 값이 다름 -> 강제 보정
    - 사용자가 한 값만 명시했는데 LLM이 필드를 누락함
      + fill_missing=True -> Python이 그 값을 추가
    - 여러 값을 명시 -> LLM 값이 후보 중 하나면 유지
      (예: "스몰 말고 미디움"처럼 후보가 여러 개면 LLM 문맥 판단을 존중)
    """
    candidates = explicit_values.get(field, set())

    # --------------------------------------------------------
    # 사용자가 명시한 단일 값이 있는데 LLM이 필드를 빠뜨린 경우
    # --------------------------------------------------------
    if field not in patch or patch.get(field) is None:
        if fill_missing and len(candidates) == 1:
            expected = next(iter(candidates))
            patch[field] = expected
            warnings.append(
                f"semantic 보정: {prefix}.{field} 누락 -> {expected} 추가 "
                f"(사용자 발화에 명시됨)"
            )
        return

    current = patch.get(field)

    # --------------------------------------------------------
    # LLM이 값을 만들었는데 사용자가 해당 옵션을 말하지 않은 경우
    # --------------------------------------------------------
    if not candidates:
        patch.pop(field, None)
        warnings.append(
            f"semantic 보정: {prefix}.{field}={current} 제거 "
            f"(사용자 발화에 해당 옵션 없음)"
        )
        return

    # --------------------------------------------------------
    # 사용자가 한 값만 명시한 경우 그 값이 최종 정답
    # --------------------------------------------------------
    if len(candidates) == 1:
        expected = next(iter(candidates))
        if current != expected:
            patch[field] = expected
            warnings.append(
                f"semantic 보정: {prefix}.{field} {current} -> {expected}"
            )
        return

    # --------------------------------------------------------
    # 여러 후보가 있는데 LLM 값이 후보에도 없는 경우 제거
    # --------------------------------------------------------
    if current not in candidates:
        patch.pop(field, None)
        warnings.append(
            f"semantic 보정: {prefix}.{field}={current} 제거 "
            f"(명시 후보={sorted(candidates)})"
        )


def _action_targets_pending(action, state, pending):
    """
    modify action이 현재 pending 항목을 대상으로 하는지 확인한다.

    여러 action이 한 발화에 있을 때 explicit 값을 모든 action에 뿌리면
    서로 다른 메뉴의 옵션이 섞일 수 있으므로, pending target에는 안전하게
    누락된 명시 옵션을 보충할 수 있게 한다.
    """
    if pending is None:
        return False

    pending_line_id, _ = pending

    pending_item = next(
        (
            item for item in state.get("items", [])
            if item.get("line_id") == pending_line_id
        ),
        None,
    )

    if pending_item is None:
        return False

    target = action.get("target") or {}

    # target이 비어 있고 현재 주문 항목이 하나뿐이면 그 항목으로 본다.
    if not target:
        return len(state.get("items", [])) == 1

    if target.get("line_id") is not None:
        return target.get("line_id") == pending_line_id

    for key in ("item_type", "menu", "drink", "side"):
        if target.get(key) is not None and target.get(key) != pending_item.get(key):
            return False

    return True
# ============================================================
# deterministic existing-state reference guard
# ============================================================

def referenced_excluded_ingredient(text: str):
    """
    기존 주문 상태를 설명하는 재료 제외 참조만 찾는다.

    예:
      "양파 빼놓은 불고기버거에 베이컨 넣어주세요"
      "양파 뺀 치즈버거에 토마토 추가해주세요"

    반면 새 명령은 잡지 않는다:
      "양파 빼주세요"
      "첫 번째 불고기버거는 양파 빼주세요"
    """

    ingredient_patterns = {
        "pickle": [
            r"피클\s*(?:을\s*)?(?:뺀|빼놓은|빼둔|제외한)",
        ],
        "onion": [
            r"양파\s*(?:를\s*)?(?:뺀|빼놓은|빼둔|제외한)",
        ],
    }

    found = set()

    for ingredient, patterns in ingredient_patterns.items():
        for pattern in patterns:
            if re.search(pattern, text):
                found.add(ingredient)
                break

    return found


def guard_nonexistent_exclusion_reference(utterance, state, update):
    """
    사용자가 'X를 빼놓은 상품'처럼 기존 상태를 참조했는데
    실제 state에 그런 상품이 없으면 LLM의 modify를 차단한다.

    이 guard는 새 제외 명령에는 적용하지 않는다.
    """

    warnings = []

    if val(update.intent) != "order":
        return update, warnings

    referenced = referenced_excluded_ingredient(utterance)

    if not referenced:
        return update, warnings

    data = update.model_dump(mode="json", exclude_none=True)
    actions = data.get("actions", [])

    modify_actions = [
        action for action in actions
        if action.get("operation") == "modify"
    ]

    if not modify_actions:
        return update, warnings

    state_items = state.get("items", [])

    for ingredient in referenced:
        matching_items = []

        for item in state_items:
            excluded = {
                val(x)
                for x in (item.get("exclude") or [])
            }

            if ingredient in excluded:
                matching_items.append(item)

        if matching_items:
            continue

        # 사용자가 존재한다고 전제한 subset 자체가 state에 없다.
        # 이 발화의 modify를 적용하면 존재하지 않는 참조를
        # 새 상태로 만들어 버리므로 modify 전체를 제거한다.
        data["actions"] = [
            action for action in actions
            if action.get("operation") != "modify"
        ]

        warnings.append(
            f"존재하지 않는 기존 제외 참조 차단: {ingredient}"
        )

        break

    try:
        verified = OrderUpdate.model_validate(data)
    except Exception as e:
        warnings.append(
            "reference guard 재검증 실패 -> 원본 유지: "
            f"{type(e).__name__}: {e}"
        )
        return update, warnings

    return verified, warnings

def verify_semantics(utterance: str, state, pending, update):
    """
    LLM이 사용자가 말하지 않은 메뉴/옵션을 임의 생성하는 것을 막는다.

    핵심 예 1:
        사용자: "아이스커피 하나 추가요"
        LLM:     drink_size="large"
        VERIFIED: drink_size 제거 -> State Manager가 size pending 생성

    핵심 예 2:
        pending=drink, 사용자: "제로콜라 라지요"
        LLM:     drink="zero_coke"만 출력
        VERIFIED: drink_size="large"도 사용자 발화에서 복원하여 함께 반영

    수정(modify)은 변경하려는 item patch의 옵션을 엄격히 검증한다.
    추가(add)는 사용자가 새 품목을 직접 이름으로 말한 경우에만
    그 품목의 옵션을 엄격히 검증한다. 이렇게 해야 "하나 더 주세요" 같은
    문맥 기반 수량/반복 주문을 불필요하게 깨지 않는다.
    """
    warnings = []

    if val(update.intent) != "order":
        return update, warnings

    data = update.model_dump(mode="json", exclude_none=True)
    explicit = explicit_values_by_field(utterance)

    actions = data.get("actions", [])

    for index, action in enumerate(actions, start=1):
        op = action.get("operation")
        patch = action.get("item")

        if not isinstance(patch, dict):
            continue

        prefix = f"action[{index}]"
        explicit = explicit_values_by_field(utterance)

        # ----------------------------------------------------
        # MODIFY
        # 사용자가 말하지 않은 값을 변경 patch에 넣지 못하게 한다.
        # ----------------------------------------------------
        if op == "modify":
            # 한 발화에 modify가 하나뿐이면 명시 옵션 누락을 안전하게 보충한다.
            # 여러 modify action이 있으면 옵션이 서로 섞일 수 있으므로
            # 현재 pending 항목을 대상으로 하는 action에만 누락 보충을 허용한다.
            modify_count = sum(
                1 for x in actions
                if x.get("operation") == "modify"
            )

            fill_missing = (
                len(actions) == 1
            )

            # 메뉴 언급에는 변경 전 대상도 포함된다. 문맥 해석은 LLM에 맡긴다.
            # 대상/상품 종류 검증은 상태 적용 단계에서 수행한다.
            for field in ("type", "drink", "drink_size", "side"):
                _set_or_remove_field(
                    patch,
                    field,
                    explicit,
                    warnings,
                    prefix,
                    fill_missing=fill_missing,
                )
            continue

        # ----------------------------------------------------
        # ADD
        # 사용자가 새 품목을 직접 이름으로 말했을 때만 엄격 검증.
        # ----------------------------------------------------
        if op != "add":
            continue

        # 분리 주문의 새 항목은 원본에서 변경하지 않은 옵션을 상속할 수 있다.
        # 단일 명시 line_id의 수량 감소 + 같은 종류 add인 경우에만 인정한다.
        reductions = [a for a in actions if a.get("operation") == "adjust_quantity"
                      and a.get("quantity_delta", 0) < 0]
        if len(reductions) == 1:
            reduction = reductions[0]
            selector = reduction.get("target") or {}
            source = next((x for x in state.get("items", [])
                           if x.get("line_id") == selector.get("line_id")), None)
            if (source and source.get("item_type") == patch.get("item_type")
                    and patch.get("quantity") == -reduction["quantity_delta"]):
                for field in ("type", "drink", "drink_size", "side"):
                    value = patch.get(field)
                    if value is not None and value == source.get(field):
                        explicit[field].add(value)

        item_type = patch.get("item_type")
        fill_add_missing = len(actions) == 1

        if item_type == "drink":
            # '아이스커피/콜라/사이다...'를 직접 말한 새 독립 음료
            if explicit["drink"]:
                _set_or_remove_field(
                    patch, "drink", explicit, warnings, prefix,
                    fill_missing=fill_add_missing,
                )
                _set_or_remove_field(
                    patch, "drink_size", explicit, warnings, prefix,
                    fill_missing=fill_add_missing,
                )

        elif item_type == "burger":
            # 특정 버거를 직접 말한 신규 버거
            if explicit["menu"]:
                _set_or_remove_field(
                    patch, "menu", explicit, warnings, prefix,
                    fill_missing=fill_add_missing,
                )
                _set_or_remove_field(
                    patch, "type", explicit, warnings, prefix,
                    fill_missing=fill_add_missing,
                )

                # 세트 관련 값도 사용자가 말한 것만 허용
                _set_or_remove_field(
                    patch, "drink", explicit, warnings, prefix,
                    fill_missing=fill_add_missing,
                )
                _set_or_remove_field(
                    patch, "drink_size", explicit, warnings, prefix,
                    fill_missing=fill_add_missing,
                )
                _set_or_remove_field(
                    patch, "side", explicit, warnings, prefix,
                    fill_missing=fill_add_missing,
                )

        elif item_type == "side":
            if explicit["side"]:
                _set_or_remove_field(
                    patch, "side", explicit, warnings, prefix,
                    fill_missing=fill_add_missing,
                )

    try:
        verified = OrderUpdate.model_validate(data)
    except Exception as e:
        # 검증기 자체 때문에 정상 주문 전체가 죽는 것보다 원본 LLM 출력을 유지한다.
        warnings.append(
            f"semantic verifier 재검증 실패 -> 원본 유지: {type(e).__name__}: {e}"
        )
        return update, warnings

    return verified, warnings


# ============================================================
# deterministic quantity verifier
# ============================================================

def verify_add_quantity(utterance: str, update):
    """
    LLM이 명시 수량을 1개로 축소/누락하는 경우 보정한다.

    보수적으로 add action이 정확히 하나일 때만 적용한다.
    여러 품목이 한 문장에 있으면 각 품목별 수량 매핑을 LLM에 맡긴다.
    """
    warnings = []

    if val(update.intent) != "order":
        return update, warnings

    quantity = extract_explicit_quantity(utterance)
    if quantity is None:
        return update, warnings

    data = update.model_dump(mode="json", exclude_none=True)
    actions = data.get("actions", [])
    add_actions = [a for a in actions if a.get("operation") == "add"]

    if len(add_actions) != 1 or len(actions) != 1:
        return update, warnings

    action = add_actions[0]
    patch = action.get("item")
    if not isinstance(patch, dict):
        return update, warnings

    item_type = patch.get("item_type")
    if item_type not in {"burger", "drink", "side"}:
        return update, warnings

    old = patch.get("quantity", 1)
    if old != quantity:
        patch["quantity"] = quantity
        warnings.append(
            f"quantity 보정: LLM={old} -> deterministic={quantity}"
        )

    try:
        return OrderUpdate.model_validate(data), warnings
    except Exception as e:
        warnings.append(
            f"quantity verifier 재검증 실패 -> 원본 유지: {type(e).__name__}: {e}"
        )
        return update, warnings


# ============================================================
# deterministic multi-item pending option resolver
# ============================================================

def _clone_add_action_from_item(item, *, quantity=1, field=None, value=None):
    patch = {
        "item_type": item.get("item_type"),
        "quantity": quantity,
        "menu": item.get("menu"),
        "type": item.get("type"),
        "drink": item.get("drink"),
        "drink_size": item.get("drink_size"),
        "side": item.get("side"),
    }

    if field is not None:
        patch[field] = value

    patch = {k: v for k, v in patch.items() if v is not None}

    action = {
        "operation": "add",
        "item": patch,
    }

    excludes = list(item.get("exclude", []) or [])
    toppings = list(item.get("add_toppings", []) or [])

    if excludes:
        action["exclude_add"] = excludes
    if toppings:
        action["toppings_add"] = toppings

    return action


def build_pending_group_update(utterance: str, state, pending, manager):
    """
    같은 메뉴 여러 개에 서로 다른 옵션을 말하는 경우를 deterministic하게 처리한다.

    예 1:
        state: 불고기 세트 x2, pending=drink
        user : "콜라랑 사이다요"
        -> line1 콜라 x1 + line2 사이다 x1 로 분리

    예 2:
        위에서 분리된 두 세트가 모두 drink_size 미선택
        user : "미디움이요"
        -> 같은 option group의 두 세트 모두 medium

    예 3:
        두 세트가 모두 side 미선택
        user : "감자튀김이고 치즈스틱이요"
        -> 첫 세트 감자튀김, 둘째 세트 치즈스틱

    '콜라 말고 사이다' 같은 정정 표현은 분배로 오인하지 않도록 제외한다.
    """
    warnings = []

    if pending is None or is_correction_utterance(utterance):
        return None, warnings, None

    line_id, field = pending
    if field not in {"type", "drink", "drink_size", "side"}:
        return None, warnings, None

    ordered = ordered_values_for_field(utterance, field)
    if not ordered:
        return None, warnings, None

    items = state.get("items", [])
    item = next((x for x in items if x.get("line_id") == line_id), None)
    if item is None:
        return None, warnings, None

    # --------------------------------------------------------
    # A) 이미 이전 턴에서 quantity를 여러 line으로 분리한 option group
    # --------------------------------------------------------
    group_ids = manager.option_group_for(line_id)
    if len(group_ids) > 1:
        group_items = [
            x for x in sorted(items, key=lambda y: y.get("line_id", 0))
            if x.get("line_id") in group_ids and x.get(field) is None
        ]

        if not group_items:
            return None, warnings, None

        actions = []

        # 한 값만 말하면 현재 option group의 미선택 항목 전체에 같은 값 적용
        if len(ordered) == 1:
            value = ordered[0]
            for x in group_items:
                actions.append({
                    "operation": "modify",
                    "target": {"line_id": x["line_id"]},
                    "item": {field: value},
                })

            try:
                update = OrderUpdate.model_validate({
                    "intent": "order",
                    "actions": actions,
                })
            except Exception:
                return None, warnings, None

            warnings.append(
                f"multi-option 보정: group={group_ids} {field}={value} 전체 적용"
            )
            return update, warnings, group_ids

        # 항목 수만큼 서로 다른 값을 말하면 line_id 순서대로 1:1 배정
        if len(ordered) == len(group_items):
            for x, value in zip(group_items, ordered):
                actions.append({
                    "operation": "modify",
                    "target": {"line_id": x["line_id"]},
                    "item": {field: value},
                })

            try:
                update = OrderUpdate.model_validate({
                    "intent": "order",
                    "actions": actions,
                })
            except Exception:
                return None, warnings, None

            warnings.append(
                f"multi-option 보정: group={group_ids} {field}={ordered} 순서 배정"
            )
            return update, warnings, group_ids

        return None, warnings, None

    # --------------------------------------------------------
    # B) 아직 한 line에 quantity>1로 묶여 있음
    # --------------------------------------------------------
    quantity = int(item.get("quantity", 1))
    if quantity <= 1:
        return None, warnings, None

    # 한 값이면 기존 한 line에 그대로 적용하면 quantity 전체가 같은 옵션이 됨.
    # 이 경우 기존 semantic verifier / LLM 처리를 그대로 사용한다.
    if len(ordered) == 1:
        return None, warnings, None

    # quantity 개수와 명시 옵션 수가 정확히 맞을 때만 안전하게 분리
    if len(ordered) != quantity:
        return None, warnings, None

    actions = [
        {
            "operation": "modify",
            "target": {"line_id": line_id},
            "item": {
                "quantity": 1,
                field: ordered[0],
            },
        }
    ]

    for value in ordered[1:]:
        actions.append(
            _clone_add_action_from_item(
                item,
                quantity=1,
                field=field,
                value=value,
            )
        )

    try:
        update = OrderUpdate.model_validate({
            "intent": "order",
            "actions": actions,
        })
    except Exception as e:
        warnings.append(
            f"multi-option verifier 재검증 실패 -> LLM 처리 유지: {type(e).__name__}: {e}"
        )
        return None, warnings, None

    max_line_id = max((x.get("line_id", 0) for x in items), default=0)
    planned_group_ids = [line_id] + [
        max_line_id + i
        for i in range(1, quantity)
    ]

    warnings.append(
        f"multi-option 보정: line_id={line_id} quantity={quantity}를 "
        f"{field}={ordered} 기준으로 개별 line 분리"
    )

    return update, warnings, planned_group_ids


# ============================================================
# deterministic finalization guard
# ============================================================

def is_finalization_utterance(utterance: str) -> bool:
    text = compact_text(utterance)
    return any(keyword in text for keyword in FINALIZATION_KEYWORDS)


def explicitly_answers_pending(utterance: str, pending) -> bool:
    if pending is None:
        return False

    _, field = pending
    explicit = explicit_values_by_field(utterance)

    if field == "type":
        return bool(explicit["type"])

    if field == "drink":
        return bool(explicit["drink"])

    if field == "drink_size":
        return bool(explicit["drink_size"])

    if field == "side":
        return bool(explicit["side"])

    return False


def make_order_update():
    return OrderUpdate.model_validate({
        "intent": "order",
        "actions": [],
    })


def make_unknown_update():
    return OrderUpdate.model_validate({
        "intent": "unknown",
        "actions": [],
    })


def make_confirm_update():
    return OrderUpdate.model_validate({
        "intent": "confirm",
        "actions": [],
    })


# ============================================================
# vLLM + XGrammar parser
# ============================================================

class V6Parser:
    def __init__(self):
        self.client = OpenAI(
            base_url=API_BASE,
            api_key=API_KEY,
        )

    def parse(self, state, pending, utterance):
        pending_text = (
            "없음"
            if pending is None
            else f"line_id={pending[0]}, field={pending[1]}"
        )

        user_content = (
            "현재 주문 상태:\n"
            + json.dumps(state, ensure_ascii=False, separators=(",", ":"))
            + "\n\n현재 확인 중인 항목:\n"
            + pending_text
            + "\n\n현재 사용자 발화:\n"
            + utterance
        )

        response = self.client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT + (
                    " " + SELECTION_GUIDANCE if state.get("items") else ""
                )},
                {"role": "user", "content": user_content},
            ],
            temperature=0.0,
            max_tokens=1024,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "OrderUpdate",
                    "schema": ORDER_UPDATE_SCHEMA,
                    "strict": True,
                },
            },
            extra_body={
                "chat_template_kwargs": {
                    "enable_thinking": False
                }
            },
        )

        if response.choices[0].finish_reason == "length":
            raise ModelOutputError("주문 해석 응답이 길이 제한으로 중단됐습니다. 주문을 나누어 말씀해주세요.")
        text = response.choices[0].message.content
        if not text:
            raise ModelOutputError("LLM 응답이 비어 있습니다.")

        return OrderUpdate.model_validate_json(text)


# ============================================================
# state manager
# ============================================================

def empty_state():
    return {"intent": "unknown", "order_id": None, "items": []}


def normalize_item(item):
    out = {
        "line_id": item.get("line_id"),
        "item_type": item.get("item_type"),
        "quantity": int(item.get("quantity", 1)),
        "menu": item.get("menu"),
        "type": item.get("type"),
        "drink": item.get("drink"),
        "drink_size": item.get("drink_size"),
        "side": item.get("side"),
        "exclude": sorted(set(item.get("exclude", []) or [])),
        "add_toppings": sorted(set(item.get("add_toppings", []) or [])),
    }

    if out["item_type"] == "burger" and out["type"] == "single":
        out["drink"] = None
        out["drink_size"] = None
        out["side"] = None

    return out


def signature(item):
    x = normalize_item(item)
    return (
        x["item_type"],
        x["menu"],
        x["type"],
        x["drink"],
        x["drink_size"],
        x["side"],
        tuple(x["exclude"]),
        tuple(x["add_toppings"]),
    )


class OrderStateManager:
    def __init__(self):
        self.state = empty_state()
        self.pending = None
        # quantity>1 품목이 서로 다른 옵션으로 분리된 경우
        # 같은 원 주문에서 나온 line_id들을 런타임 내부에서만 묶어둔다.
        self.option_groups = {}
        self.last_selected_ids = []
        self._line_id_counter = 0

    def reset(self):
        self.state = empty_state()
        self.pending = None
        self.option_groups = {}
        self.last_selected_ids = []
        self._line_id_counter = 0

    def register_option_group(self, line_ids):
        group = tuple(sorted(set(int(x) for x in line_ids)))
        if len(group) <= 1:
            return
        for line_id in group:
            self.option_groups[line_id] = group

    def option_group_for(self, line_id):
        group = self.option_groups.get(int(line_id))
        if not group:
            return [int(line_id)]

        existing = {x.get("line_id") for x in self.state.get("items", [])}
        filtered = [x for x in group if x in existing]
        if len(filtered) <= 1:
            return [int(line_id)]
        return filtered

    def cleanup_option_groups(self):
        existing = {x.get("line_id") for x in self.state.get("items", [])}
        rebuilt = {}
        seen = set()

        for group in self.option_groups.values():
            key = tuple(x for x in group if x in existing)
            if len(key) <= 1 or key in seen:
                continue
            seen.add(key)
            for line_id in key:
                rebuilt[line_id] = key

        self.option_groups = rebuilt

    def next_line_id(self):
        current = max((x["line_id"] for x in self.state["items"]), default=0)
        self._line_id_counter = max(self._line_id_counter, current) + 1
        return self._line_id_counter

    def expand_individual_items(self):
        """상품을 한 개씩 유지하고 기존 ID는 변경하지 않는다."""
        expanded = []
        added_ids = []
        for item in self.state["items"]:
            quantity = item["quantity"]
            if type(quantity) is not int or quantity < 1:
                raise InternalInvariantError("상품 수량이 잘못되었습니다.")
            item["quantity"] = 1
            expanded.append(item)
            for _ in range(quantity - 1):
                clone = copy.deepcopy(item)
                clone["line_id"] = self.next_line_id()
                expanded.append(clone)
                added_ids.append(clone["line_id"])
        self.state["items"] = expanded
        return added_ids

    def targets(self, target, apply_to_all=False):
        items = self.state["items"]
        t = dump_model(target)

        if not t:
            if len(items) == 1:
                return [items[0]]
            raise RecoverableOrderError("대상 항목이 여러 개라 line_id가 필요합니다.")

        found = []
        for item in items:
            ok = True
            for key in ("line_id", "item_type", "menu", "drink", "side"):
                if t.get(key) is not None and item.get(key) != t[key]:
                    ok = False
                    break
            if ok:
                found.append(item)

        if not found:
            raise RecoverableOrderError(f"target을 찾지 못했습니다: {t}")

        if apply_to_all:
            return found

        if len(found) != 1:
            raise RecoverableOrderError("target이 여러 항목과 일치합니다.")

        return [found[0]]

    def merge_identical(self):
        # 호환용 이름: 개별 상품 ID를 보존하므로 같은 옵션이라도 병합하지 않는다.
        self.expand_individual_items()

    def compute_pending(self):
        if self.state["intent"] != "order":
            return None

        for item in sorted(self.state["items"], key=lambda x: x["line_id"]):
            line_id = item["line_id"]

            if item["item_type"] == "burger":
                if item["type"] is None:
                    return (line_id, "type")

                if item["type"] == "set":
                    for field in ("drink", "drink_size", "side"):
                        if item.get(field) is None:
                            return (line_id, field)

            if item["item_type"] == "drink" and item["drink_size"] is None:
                return (line_id, "drink_size")

        return None

    @staticmethod
    def validate_items(items):
        allowed = {
            "burger": {"menu", "type", "drink", "drink_size", "side", "exclude", "add_toppings"},
            "drink": {"drink", "drink_size"},
            "side": {"side"},
        }
        fields = {"menu", "type", "drink", "drink_size", "side", "exclude", "add_toppings"}
        ids = set()
        for item in items:
            kind = item.get("item_type")
            if kind not in allowed:
                raise InternalInvariantError("지원하지 않는 상품 종류입니다.")
            line_id = item.get("line_id")
            if type(line_id) is not int or line_id < 1 or line_id in ids:
                raise InternalInvariantError("주문 줄 번호가 잘못되었습니다.")
            ids.add(line_id)
            quantity = item.get("quantity")
            if type(quantity) is not int or quantity < 1:
                raise InternalInvariantError("상품 수량이 잘못되었습니다.")
            for field in fields - allowed[kind]:
                if item.get(field) not in (None, []):
                    raise InternalInvariantError(f"{kind} 항목에는 {field} 옵션을 적용할 수 없습니다.")
            required = {"burger": "menu", "drink": "drink", "side": "side"}[kind]
            if not item.get(required):
                raise InternalInvariantError(f"{kind} 항목에 {required} 값이 없습니다.")

    def apply(self, update):
        # 한 발화의 여러 action은 전부 성공해야 반영한다.
        # 뒤 action이 실패해도 앞 action의 수량 감소 등이 남지 않게 한다.
        snapshot = copy.deepcopy(self.__dict__)
        try:
            self._apply_unchecked(update)
            self.validate_items(self.state["items"])
        except Exception:
            self.__dict__.clear()
            self.__dict__.update(snapshot)
            raise

    def _apply_unchecked(self, update):
        selected_ids = []
        intent = val(update.intent)

        if intent == "mobile_pickup":
            self.state = {
                "intent": "mobile_pickup",
                "order_id": update.order_id,
                "items": [],
            }
            self.pending = None
            return

        if intent == "confirm":
            self.state["intent"] = "confirm"
            self.pending = None
            return

        if intent == "cancel":
            self.state["intent"] = "cancel"
            self.pending = None
            return

        if intent == "unknown":
            return

        if intent != "order":
            raise InternalInvariantError(f"지원하지 않는 intent: {intent}")

        self.state["intent"] = "order"
        self.state["order_id"] = None

        for action in update.actions:
            op = val(action.operation)

            if op == "reset":
                self.last_selected_ids = []
                self.option_groups = {}
                selected_ids = []
                self.state = {
                    "intent": "order",
                    "order_id": None,
                    "items": [],
                }
                continue

            if op == "add":
                patch = dump_model(action.item)
                if not patch:
                    raise ModelOutputError("add item 없음")

                new_item = normalize_item({
                    "line_id": self.next_line_id(),
                    "item_type": patch.get("item_type"),
                    "quantity": patch.get("quantity", 1),
                    "menu": patch.get("menu"),
                    "type": patch.get("type"),
                    "drink": patch.get("drink"),
                    "drink_size": patch.get("drink_size"),
                    "side": patch.get("side"),
                    "exclude": list(getattr(action, "exclude_add", []) or []),
                    "add_toppings": list(getattr(action, "toppings_add", []) or []),
                })

                self.validate_items([new_item])
                self.state["items"].append(new_item)
                selected_ids.append(new_item["line_id"])
                selected_ids.extend(self.expand_individual_items())
                continue

            target_items = self.targets(
                action.target,
                bool(getattr(action, "apply_to_all", False)),
            )

            selected_ids.extend(x["line_id"] for x in target_items)

            if op == "modify":
                patch = dump_model(action.item)
                if patch.get("quantity") not in (None, 1):
                    raise ModelOutputError("개별 상품 수정으로 수량을 바꿀 수 없습니다. 추가/수량 조정이 필요합니다.")
                if not patch and not any(getattr(action, f, []) for f in
                                         ("exclude_add", "exclude_remove", "toppings_add", "toppings_remove")):
                    raise ModelOutputError("변경할 옵션이 확인되지 않았습니다. 주문을 다시 말씀해주세요.")

                for item in target_items:
                    if patch.get("item_type", item["item_type"]) != item["item_type"]:
                        raise ModelOutputError("상품 종류 변경은 기존 항목 제거와 새 항목 추가로 처리해야 합니다.")
                    for k, v in patch.items():
                        if v is not None:
                            item[k] = v

                    exc = set(item.get("exclude", []))
                    exc.update(getattr(action, "exclude_add", []) or [])
                    exc.difference_update(getattr(action, "exclude_remove", []) or [])
                    item["exclude"] = sorted(exc)

                    tops = set(item.get("add_toppings", []))
                    tops.update(getattr(action, "toppings_add", []) or [])
                    tops.difference_update(getattr(action, "toppings_remove", []) or [])
                    item["add_toppings"] = sorted(tops)

                    self.validate_items([item])
                    normalized = normalize_item(item)
                    item.clear()
                    item.update(normalized)

                continue

            if op == "adjust_quantity":
                delta = action.quantity_delta
                if delta is None:
                    raise ModelOutputError("quantity_delta 없음")

                remove_ids = set()
                for item in target_items:
                    item["quantity"] += int(delta)
                    if item["quantity"] <= 0:
                        remove_ids.add(item["line_id"])

                self.state["items"] = [
                    x for x in self.state["items"]
                    if x["line_id"] not in remove_ids
                ]
                continue

            if op == "remove":
                remove_ids = {x["line_id"] for x in target_items}
                self.state["items"] = [
                    x for x in self.state["items"]
                    if x["line_id"] not in remove_ids
                ]
                continue

            raise InternalInvariantError(f"지원하지 않는 operation: {op}")

        # 동일 품목 자동 병합
        selected_ids.extend(self.expand_individual_items())
        existing = {x["line_id"] for x in self.state["items"]}
        self.last_selected_ids = list(dict.fromkeys(
            x for x in (selected_ids or self.last_selected_ids) if x in existing
        ))
        self.cleanup_option_groups()

        self.pending = self.compute_pending()


# ============================================================
# full runtime
# ============================================================

class DriveThruRuntime:
    def __init__(self):
        self.parser = V6Parser()
        self.manager = OrderStateManager()

    def _restore_snapshot(self, snapshot):
        """현재 요청에서 발생한 변경을 모두 원상복구한다."""
        self.manager.__dict__.clear()
        self.manager.__dict__.update(snapshot)

    def _print_last_llm_update(self, title):
        """개발자 디버깅용 LLM 원본 출력."""
        if self._last_llm_update is None:
            return

        print(f"\\n[{title}]")
        print(
            json.dumps(
                self._last_llm_update,
                ensure_ascii=False,
                indent=2,
            )
        )

    def _handled_error_result(self, error):
        """
        고객 재질문으로 복구 가능한 오류만
        정상적인 Runtime result 형태로 반환한다.
        """
        unknown_update = make_unknown_update()

        return {
            "llm_update": copy.deepcopy(self._last_llm_update),
            "verified_update": unknown_update.model_dump(mode="json"),
            "state": copy.deepcopy(self.manager.state),
            "pending": self.manager.pending,
            "warnings": [
                f"{error.kind}: {error}"
            ],
            "error": {
                "kind": error.kind,
                "code": error.code,
                "detail": str(error),
            },
            "reply": error.reply,

            # 기존 코드/테스트 호환용.
            # 추후 완전히 error 구조로 전환하면 제거 가능.
            "recoverable_error": str(error),
        }

    def process(self, utterance):
        snapshot = copy.deepcopy(self.manager.__dict__)
        self._last_llm_update = None

        try:
            return self._process(utterance)

        # ====================================================
        # 1. 고객이 다시 말하면 해결 가능한 오류
        # ====================================================
        except RecoverableOrderError as e:
            self._restore_snapshot(snapshot)
            self._print_last_llm_update(
                "RECOVERABLE ORDER ERROR - LLM OUTPUT"
            )

            print(
                f"\\n[ORDER ERROR] "
                f"kind={e.kind} "
                f"code={e.code} "
                f"detail={e}"
            )

            return self._handled_error_result(e)

        # ====================================================
        # 2. LLM 출력 자체가 잘못된 경우
        # ====================================================
        except ModelOutputError as e:
            self._restore_snapshot(snapshot)
            self._print_last_llm_update(
                "MODEL OUTPUT ERROR - LLM OUTPUT"
            )

            print(
                f"\\n[MODEL ERROR] "
                f"kind={e.kind} "
                f"code={e.code} "
                f"detail={e}"
            )

            return self._handled_error_result(e)

        # ====================================================
        # 3. 정상 코드에서는 발생하면 안 되는 내부 오류
        # ====================================================
        # 이것은 고객 입력 오류로 숨기면 안 된다.
        except InternalInvariantError:
            self._restore_snapshot(snapshot)
            self._print_last_llm_update(
                "INTERNAL INVARIANT FAILURE - LLM OUTPUT"
            )
            raise

        # ====================================================
        # 4. 예상하지 못한 Python 오류
        # ====================================================
        except Exception:
            self._restore_snapshot(snapshot)
            self._print_last_llm_update(
                "UNEXPECTED FAILURE - LLM OUTPUT"
            )
            raise

    def _process(self, utterance):
        before = copy.deepcopy(self.manager.state)
        pending_before = self.manager.pending

        parser_state = copy.deepcopy(before)
        if before.get("items"):
            parser_state["recent_selection"] = list(self.manager.last_selected_ids)
        update = self.parser.parse(
            parser_state,
            pending_before,
            utterance,
        )

        # DEBUG에서 순수 LLM 출력을 볼 수 있도록 보정 전에 저장한다.
        llm_raw = update.model_dump(mode="json")
        self._last_llm_update = copy.deepcopy(llm_raw)
        repaired, warnings = repair_add_items(llm_raw)
        update = OrderUpdate.model_validate(repaired)

        # ----------------------------------------------------
        # 1. 모바일 주문번호 deterministic verifier
        # ----------------------------------------------------
        update, mobile_warnings = verify_order_id(
            utterance,
            before,
            update,
        )
        warnings.extend(mobile_warnings)

        # ----------------------------------------------------
        # 2. 메뉴/옵션 semantic verifier
        # ----------------------------------------------------
        update, semantic_warnings = verify_semantics(
            utterance,
            before,
            pending_before,
            update,
        )
        warnings.extend(semantic_warnings)

                # ----------------------------------------------------
        # 2-0. 기존 상태 reference grounding
        # ----------------------------------------------------
        update, reference_warnings = guard_nonexistent_exclusion_reference(
            utterance,
            before,
            update,
        )
        warnings.extend(reference_warnings)

        # ----------------------------------------------------
        # 2-1. pending 필드 자체를 LLM이 누락한 경우 deterministic 복원
        #      예: pending=type + "단품으로..."인데 item patch가 비어 있음
        # ----------------------------------------------------
        update, pending_field_warnings = repair_pending_explicit_field(
            utterance,
            before,
            pending_before,
            update,
        )
        warnings.extend(pending_field_warnings)

        # ----------------------------------------------------
        # 2-2. pending 답변 + 같은 발화의 버거 재료 제외 보정
        #      예: "단품으로 하는데 피클은 빼주세요"
        # ----------------------------------------------------
        update, pending_modifier_warnings = repair_pending_burger_modifiers(
            utterance,
            before,
            pending_before,
            update,
        )
        warnings.extend(pending_modifier_warnings)

        # ----------------------------------------------------
        # 3. 신규 품목 명시 수량 deterministic verifier
        # ----------------------------------------------------
        update, quantity_warnings = verify_add_quantity(
            utterance,
            update,
        )
        warnings.extend(quantity_warnings)

        # ----------------------------------------------------
        # 4. quantity>1 세트의 서로 다른 옵션 분배
        #    예: x2 + "콜라랑 사이다"
        # ----------------------------------------------------
        # 개별 상품에서는 옵션 대상을 LLM이 line_id별로 선택한다.
        # 과거 그룹 보정은 일부 선택을 전체 선택으로 바꿀 수 있어 호출하지 않는다.
        planned_group_ids = None
        # 5. 미완성 주문을 confirm으로 확정하지 못하게 막는다.
        # ----------------------------------------------------
        finalization_requested = is_finalization_utterance(utterance)

        if (
            finalization_requested
            and pending_before is not None
            and not explicitly_answers_pending(utterance, pending_before)
        ):
            update = make_order_update()
            warnings.append(
                "확정 보류: "
                f"{PENDING_FIELD_NAME.get(pending_before[1], pending_before[1])} "
                "선택이 아직 필요함"
            )

        elif val(update.intent) == "confirm" and pending_before is not None:
            update = make_order_update()
            warnings.append(
                "confirm 보정: 미완성 옵션이 있어 주문 확정을 보류함"
            )

        elif val(update.intent) == "confirm" and not before.get("items"):
            update = make_unknown_update()
            warnings.append(
                "confirm 보정: 주문 항목이 없어 unknown으로 변경함"
            )

        # ----------------------------------------------------
        # 6. state 반영
        # ----------------------------------------------------
        checked, selection_warnings = reconcile_subset_count(
            utterance, before, self.manager.last_selected_ids,
            update.model_dump(mode="json", exclude_none=True),
        )
        warnings.extend(selection_warnings)
        update = OrderUpdate.model_validate(checked)
        self.manager.apply(update)

        if planned_group_ids:
            self.manager.register_option_group(planned_group_ids)

        # ----------------------------------------------------
        # 7. "단품으로 하고 마무리할게요"처럼
        #    pending 답변 + 확정을 한 문장에 같이 말한 경우
        # ----------------------------------------------------
        if (
            finalization_requested
            and pending_before is not None
            and explicitly_answers_pending(utterance, pending_before)
            and self.manager.pending is None
            and self.manager.state.get("items")
            and self.manager.state.get("intent") == "order"
        ):
            confirm_update = make_confirm_update()
            self.manager.apply(confirm_update)
            update = confirm_update
            warnings.append(
                "pending 옵션 처리 후 같은 발화의 주문 확정까지 적용함"
            )

        return {
            "llm_update": llm_raw,
            "verified_update": update.model_dump(mode="json"),
            "state": copy.deepcopy(self.manager.state),
            "pending": self.manager.pending,
            "warnings": warnings,
        }


def main():
    runtime = DriveThruRuntime()

    print("=" * 72)
    print("SOOMAC DRIVE-THRU V14 RUNTIME")
    print("=" * 72)
    print("/reset 초기화 | /state 상태 | /quit 종료")

    while True:
        try:
            text = input("\n사용자: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not text:
            continue

        if text == "/quit":
            break

        if text == "/reset":
            runtime.manager.reset()
            print("초기화 완료")
            continue

        if text == "/state":
            print(json.dumps(
                runtime.manager.state,
                ensure_ascii=False,
                indent=2,
            ))
            print("pending:", runtime.manager.pending)
            continue

        try:
            result = runtime.process(text)

            print("\nLLM:")
            print(json.dumps(
                result["llm_update"],
                ensure_ascii=False,
                indent=2,
            ))

            if result["warnings"]:
                print("\n검증 경고:")
                for w in result["warnings"]:
                    print("-", w)

            print("\nVERIFIED:")
            print(json.dumps(
                result["verified_update"],
                ensure_ascii=False,
                indent=2,
            ))

            print("\nSTATE:")
            print(json.dumps(
                result["state"],
                ensure_ascii=False,
                indent=2,
            ))

            print("pending:", result["pending"])

        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()

