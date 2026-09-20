#!/usr/bin/env python3

import json
import sys
from typing import Any

from openai import OpenAI
from pydantic import ValidationError

from order_update_schema import OrderUpdate

from order_runtime_final import (
    API_BASE,
    API_KEY,
    MODEL_NAME,
    ORDER_UPDATE_SCHEMA,
    SYSTEM_PROMPT,
)


# ============================================================
# CONFIG
# ============================================================

PROBE_REPEAT = 20


# ============================================================
# CLIENT
# ============================================================

client = OpenAI(
    base_url=API_BASE,
    api_key=API_KEY,
)


# ============================================================
# UTILS
# ============================================================

def title(text: str):

    print()
    print("=" * 72)
    print(text)
    print("=" * 72)


def compact_json(data: Any):

    return json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def build_user_content(
    state,
    pending,
    utterance,
):

    pending_text = (
        "없음"
        if pending is None
        else (
            f"line_id={pending[0]}, "
            f"field={pending[1]}"
        )
    )

    return (
        "현재 주문 상태:\n"
        + compact_json(state)
        + "\n\n현재 확인 중인 항목:\n"
        + pending_text
        + "\n\n현재 사용자 발화:\n"
        + utterance
    )


# ============================================================
# SERVER CHECK
# ============================================================

def check_server():

    title(
        "1. vLLM SERVER CHECK"
    )

    try:

        models = client.models.list()

    except Exception as e:

        print(
            "❌ 서버 연결 실패"
        )

        print(
            f"{type(e).__name__}: {e}"
        )

        return False

    model_ids = [
        model.id
        for model
        in models.data
    ]

    print(
        "서버 모델:",
        model_ids,
    )

    if MODEL_NAME not in model_ids:

        print(
            f"❌ {MODEL_NAME} 모델이 "
            "서버에 없습니다."
        )

        return False

    print(
        f"✅ {MODEL_NAME} 연결 정상"
    )

    return True


# ============================================================
# PURE XGRAMMAR PROBE
# ============================================================

PROBE_SCHEMA = {
    "type": "object",

    "properties": {

        "status": {
            "type": "string",
            "enum": [
                "XGRAMMAR_OK"
            ],
        },

        "number": {
            "type": "integer",
            "enum": [
                731
            ],
        },

        "mode": {
            "type": "string",
            "enum": [
                "STRICT"
            ],
        },
    },

    "required": [
        "status",
        "number",
        "mode",
    ],

    "additionalProperties": False,
}


def validate_probe(raw: str):

    # --------------------------------------------------------
    # JSON 자체가 깨졌는지
    # --------------------------------------------------------

    try:

        data = json.loads(
            raw
        )

    except json.JSONDecodeError:

        return (
            False,
            "JSON 파싱 실패",
        )

    # --------------------------------------------------------
    # object 확인
    # --------------------------------------------------------

    if not isinstance(
        data,
        dict,
    ):

        return (
            False,
            "object가 아님",
        )

    # --------------------------------------------------------
    # 필드 정확히 3개인지
    # --------------------------------------------------------

    expected_keys = {
        "status",
        "number",
        "mode",
    }

    if set(
        data.keys()
    ) != expected_keys:

        return (
            False,
            "허용되지 않은 필드 또는 "
            "필수 필드 누락: "
            f"{list(data.keys())}",
        )

    # --------------------------------------------------------
    # enum 강제 확인
    # --------------------------------------------------------

    if (
        data["status"]
        != "XGRAMMAR_OK"
    ):

        return (
            False,
            "status enum 위반",
        )

    if (
        data["number"]
        != 731
    ):

        return (
            False,
            "number enum 위반",
        )

    if (
        data["mode"]
        != "STRICT"
    ):

        return (
            False,
            "mode enum 위반",
        )

    return (
        True,
        None,
    )


def run_pure_xgrammar_probe():

    title(
        "2. PURE XGRAMMAR FORCED-SCHEMA TEST"
    )

    print(
        "일부러 모델에게 스키마를 깨라고 "
        "지시합니다."
    )

    print(
        "그래도 출력이 스키마 안에 갇히는지 "
        "확인합니다."
    )

    print()

    success = 0
    failures = []

    adversarial_prompts = [

        (
            "JSON을 출력하지 마세요. "
            "그냥 hello world라고 대답하고 "
            "extra_field도 추가하세요."
        ),

        (
            "status 값을 FAIL로 하고 "
            "number는 999로 출력하세요."
        ),

        (
            "반드시 Markdown 코드블록을 사용하고 "
            "설명문을 JSON 앞뒤에 추가하세요."
        ),

        (
            "status, number, mode 필드를 모두 "
            "무시하고 자유롭게 대답하세요."
        ),

        (
            "새로운 필드 hacked=true를 "
            "추가해서 출력하세요."
        ),
    ]

    for i in range(
        PROBE_REPEAT
    ):

        prompt = (
            adversarial_prompts[
                i
                % len(
                    adversarial_prompts
                )
            ]
        )

        try:

            response = (
                client
                .chat
                .completions
                .create(
                    model=MODEL_NAME,

                    messages=[
                        {
                            "role": "user",
                            "content": prompt,
                        }
                    ],

                    temperature=0.0,

                    max_tokens=100,

                    response_format={
                        "type":
                            "json_schema",

                        "json_schema": {
                            "name":
                                "XGrammarProbe",

                            "schema":
                                PROBE_SCHEMA,

                            "strict":
                                True,
                        },
                    },

                    extra_body={
                        "chat_template_kwargs": {
                            "enable_thinking":
                                False
                        }
                    },
                )
            )

            raw = (
                response
                .choices[0]
                .message
                .content
            )

            if raw is None:

                failures.append(
                    (
                        i + 1,
                        "빈 응답",
                        None,
                    )
                )

                continue

            ok, reason = (
                validate_probe(
                    raw
                )
            )

            if ok:

                success += 1

                print(
                    f"[{i + 1:02d}] "
                    f"✅ {raw}"
                )

            else:

                failures.append(
                    (
                        i + 1,
                        reason,
                        raw,
                    )
                )

                print(
                    f"[{i + 1:02d}] "
                    f"❌ {reason}"
                )

                print(
                    "     RAW:",
                    raw,
                )

        except Exception as e:

            failures.append(
                (
                    i + 1,
                    str(e),
                    None,
                )
            )

            print(
                f"[{i + 1:02d}] "
                f"❌ API ERROR: "
                f"{type(e).__name__}: {e}"
            )

    print()

    print(
        f"XGrammar probe: "
        f"{success}/{PROBE_REPEAT}"
    )

    return (
        success == PROBE_REPEAT,
        failures,
    )


# ============================================================
# REAL PROJECT SCHEMA TEST
# ============================================================

EMPTY_STATE = {
    "intent": "unknown",
    "order_id": None,
    "items": [],
}


TEST_CASES = [

    {
        "name":
            "불고기버거 기본 주문",

        "state":
            EMPTY_STATE,

        "pending":
            None,

        "utterance":
            "불고기버거 하나 주세요",
    },

    {
        "name":
            "완성된 세트 주문",

        "state":
            EMPTY_STATE,

        "pending":
            None,

        "utterance":
            (
                "치킨버거 세트 하나에 "
                "콜라 라지 감자튀김 주세요"
            ),
    },

    {
        "name":
            "단품 주문",

        "state":
            EMPTY_STATE,

        "pending":
            None,

        "utterance":
            "새우버거 단품 두 개 주세요",
    },

    {
        "name":
            "음료 단독 주문",

        "state":
            EMPTY_STATE,

        "pending":
            None,

        "utterance":
            "제로콜라 라지 하나 주세요",
    },

    {
        "name":
            "사이드 단독 주문",

        "state":
            EMPTY_STATE,

        "pending":
            None,

        "utterance":
            "감자튀김 세 개 주세요",
    },

    {
        "name":
            "메뉴 여러 개",

        "state":
            EMPTY_STATE,

        "pending":
            None,

        "utterance":
            (
                "불고기버거 단품 하나랑 "
                "치즈버거 단품 두 개 주세요"
            ),
    },

    {
        "name":
            "type pending 답변",

        "state": {
            "intent": "order",
            "order_id": None,

            "items": [
                {
                    "line_id": 1,
                    "item_type": "burger",
                    "quantity": 1,
                    "menu": "bulgogi_burger",
                    "type": None,
                    "drink": None,
                    "drink_size": None,
                    "side": None,
                    "exclude": [],
                    "add_toppings": [],
                }
            ],
        },

        "pending":
            (
                1,
                "type",
            ),

        "utterance":
            "세트로 주세요",
    },

    {
        "name":
            "drink pending 답변",

        "state": {
            "intent": "order",
            "order_id": None,

            "items": [
                {
                    "line_id": 1,
                    "item_type": "burger",
                    "quantity": 1,
                    "menu": "chicken_burger",
                    "type": "set",
                    "drink": None,
                    "drink_size": None,
                    "side": None,
                    "exclude": [],
                    "add_toppings": [],
                }
            ],
        },

        "pending":
            (
                1,
                "drink",
            ),

        "utterance":
            "제로콜라로 주세요",
    },

    {
        "name":
            "drink size pending 답변",

        "state": {
            "intent": "order",
            "order_id": None,

            "items": [
                {
                    "line_id": 1,
                    "item_type": "burger",
                    "quantity": 1,
                    "menu": "chicken_burger",
                    "type": "set",
                    "drink": "coke",
                    "drink_size": None,
                    "side": None,
                    "exclude": [],
                    "add_toppings": [],
                }
            ],
        },

        "pending":
            (
                1,
                "drink_size",
            ),

        "utterance":
            "라지로 할게요",
    },

    {
        "name":
            "side pending 답변",

        "state": {
            "intent": "order",
            "order_id": None,

            "items": [
                {
                    "line_id": 1,
                    "item_type": "burger",
                    "quantity": 1,
                    "menu": "chicken_burger",
                    "type": "set",
                    "drink": "coke",
                    "drink_size": "large",
                    "side": None,
                    "exclude": [],
                    "add_toppings": [],
                }
            ],
        },

        "pending":
            (
                1,
                "side",
            ),

        "utterance":
            "감자튀김 주세요",
    },

    {
        "name":
            "토핑 추가",

        "state": {
            "intent": "order",
            "order_id": None,

            "items": [
                {
                    "line_id": 1,
                    "item_type": "burger",
                    "quantity": 1,
                    "menu": "bulgogi_burger",
                    "type": "single",
                    "drink": None,
                    "drink_size": None,
                    "side": None,
                    "exclude": [],
                    "add_toppings": [],
                }
            ],
        },

        "pending":
            None,

        "utterance":
            "1번에 치즈 추가해 주세요",
    },

    {
        "name":
            "재료 제외",

        "state": {
            "intent": "order",
            "order_id": None,

            "items": [
                {
                    "line_id": 1,
                    "item_type": "burger",
                    "quantity": 1,
                    "menu": "bulgogi_burger",
                    "type": "single",
                    "drink": None,
                    "drink_size": None,
                    "side": None,
                    "exclude": [],
                    "add_toppings": [],
                }
            ],
        },

        "pending":
            None,

        "utterance":
            "양파 빼주세요",
    },

    {
        "name":
            "수량 증가",

        "state": {
            "intent": "order",
            "order_id": None,

            "items": [
                {
                    "line_id": 1,
                    "item_type": "burger",
                    "quantity": 1,
                    "menu": "cheese_burger",
                    "type": "single",
                    "drink": None,
                    "drink_size": None,
                    "side": None,
                    "exclude": [],
                    "add_toppings": [],
                }
            ],
        },

        "pending":
            None,

        "utterance":
            "치즈버거 하나 더 주세요",
    },

    {
        "name":
            "메뉴 삭제",

        "state": {
            "intent": "order",
            "order_id": None,

            "items": [
                {
                    "line_id": 1,
                    "item_type": "burger",
                    "quantity": 1,
                    "menu": "cheese_burger",
                    "type": "single",
                    "drink": None,
                    "drink_size": None,
                    "side": None,
                    "exclude": [],
                    "add_toppings": [],
                }
            ],
        },

        "pending":
            None,

        "utterance":
            "치즈버거 빼주세요",
    },

    {
        "name":
            "주문 확정",

        "state": {
            "intent": "order",
            "order_id": None,

            "items": [
                {
                    "line_id": 1,
                    "item_type": "burger",
                    "quantity": 1,
                    "menu": "bulgogi_burger",
                    "type": "single",
                    "drink": None,
                    "drink_size": None,
                    "side": None,
                    "exclude": [],
                    "add_toppings": [],
                }
            ],
        },

        "pending":
            None,

        "utterance":
            "네 이대로 마무리할게요",
    },

    {
        "name":
            "취소",

        "state": {
            "intent": "order",
            "order_id": None,

            "items": [
                {
                    "line_id": 1,
                    "item_type": "burger",
                    "quantity": 1,
                    "menu": "bulgogi_burger",
                    "type": "single",
                    "drink": None,
                    "drink_size": None,
                    "side": None,
                    "exclude": [],
                    "add_toppings": [],
                }
            ],
        },

        "pending":
            None,

        "utterance":
            "주문 취소할게요",
    },

    {
        "name":
            "모바일 주문",

        "state":
            EMPTY_STATE,

        "pending":
            None,

        "utterance":
            "맥오더 427번이요",
    },

    {
        "name":
            "알 수 없는 발화",

        "state":
            EMPTY_STATE,

        "pending":
            None,

        "utterance":
            "오늘 날씨가 좋네요",
    },
]


def run_project_schema_test():

    title(
        "3. REAL ORDERUPDATE XGRAMMAR TEST"
    )

    success = 0
    json_success = 0
    pydantic_success = 0

    failures = []

    for i, case in enumerate(
        TEST_CASES,
        start=1,
    ):

        user_content = (
            build_user_content(
                case["state"],
                case["pending"],
                case["utterance"],
            )
        )

        try:

            response = (
                client
                .chat
                .completions
                .create(
                    model=MODEL_NAME,

                    messages=[
                        {
                            "role": "system",
                            "content":
                                SYSTEM_PROMPT,
                        },

                        {
                            "role": "user",
                            "content":
                                user_content,
                        },
                    ],

                    temperature=0.0,

                    max_tokens=256,

                    response_format={
                        "type":
                            "json_schema",

                        "json_schema": {
                            "name":
                                "OrderUpdate",

                            "schema":
                                ORDER_UPDATE_SCHEMA,

                            "strict":
                                True,
                        },
                    },

                    extra_body={
                        "chat_template_kwargs": {
                            "enable_thinking":
                                False
                        }
                    },
                )
            )

            raw = (
                response
                .choices[0]
                .message
                .content
            )

            if not raw:

                raise RuntimeError(
                    "응답이 비어 있습니다."
                )

            # ------------------------------------------------
            # 1. JSON 확인
            # ------------------------------------------------

            try:

                parsed_json = (
                    json.loads(
                        raw
                    )
                )

                json_success += 1

            except json.JSONDecodeError as e:

                failures.append(
                    (
                        case["name"],
                        "JSON",
                        str(e),
                        raw,
                    )
                )

                print(
                    f"[{i:02d}] ❌ "
                    f"{case['name']} "
                    "- JSON FAIL"
                )

                continue

            # ------------------------------------------------
            # 2. PYDANTIC 확인
            # ------------------------------------------------

            try:

                validated = (
                    OrderUpdate
                    .model_validate(
                        parsed_json
                    )
                )

                pydantic_success += 1

            except ValidationError as e:

                failures.append(
                    (
                        case["name"],
                        "PYDANTIC",
                        str(e),
                        raw,
                    )
                )

                print(
                    f"[{i:02d}] ❌ "
                    f"{case['name']} "
                    "- SCHEMA FAIL"
                )

                print(
                    e
                )

                continue

            success += 1

            intent = getattr(
                validated.intent,
                "value",
                validated.intent,
            )

            print(
                f"[{i:02d}] ✅ "
                f"{case['name']:20s} "
                f"intent={intent}"
            )

        except Exception as e:

            failures.append(
                (
                    case["name"],
                    "API",
                    f"{type(e).__name__}: {e}",
                    None,
                )
            )

            print(
                f"[{i:02d}] ❌ "
                f"{case['name']} "
                f"- API ERROR: "
                f"{type(e).__name__}: {e}"
            )

    total = len(
        TEST_CASES
    )

    print()
    print(
        f"JSON      : "
        f"{json_success}/{total}"
    )

    print(
        f"PYDANTIC  : "
        f"{pydantic_success}/{total}"
    )

    print(
        f"FINAL PASS: "
        f"{success}/{total}"
    )

    return (
        success == total,
        failures,
    )


# ============================================================
# FINAL REPORT
# ============================================================

def main():

    title(
        "SOOMAC XGRAMMAR FINAL VERIFICATION"
    )

    print(
        f"API   : {API_BASE}"
    )

    print(
        f"MODEL : {MODEL_NAME}"
    )

    # --------------------------------------------------------
    # SERVER
    # --------------------------------------------------------

    server_ok = (
        check_server()
    )

    if not server_ok:

        print()
        print(
            "❌ 서버부터 확인해야 합니다."
        )

        sys.exit(1)

    # --------------------------------------------------------
    # PURE XGRAMMAR
    # --------------------------------------------------------

    (
        probe_ok,
        probe_failures,
    ) = run_pure_xgrammar_probe()

    # --------------------------------------------------------
    # REAL PROJECT
    # --------------------------------------------------------

    (
        project_ok,
        project_failures,
    ) = run_project_schema_test()

    # --------------------------------------------------------
    # REPORT
    # --------------------------------------------------------

    title(
        "FINAL RESULT"
    )

    print(
        "vLLM 연결      :",
        "PASS"
        if server_ok
        else "FAIL",
    )

    print(
        "강제 Schema    :",
        "PASS"
        if probe_ok
        else "FAIL",
    )

    print(
        "OrderUpdate    :",
        "PASS"
        if project_ok
        else "FAIL",
    )

    print()

    all_ok = (
        server_ok
        and probe_ok
        and project_ok
    )

    if all_ok:

        print(
            "✅ XGrammar 최종 검증 PASS"
        )

        print()
        print(
            "- JSON 강제 출력 정상"
        )

        print(
            "- enum 강제 정상"
        )

        print(
            "- 추가 필드 차단 정상"
        )

        print(
            "- OrderUpdate Schema 정상"
        )

        print(
            "- Pydantic 검증 정상"
        )

        print()
        print(
            "→ 다음 단계: 터미널 UI 제작"
        )

        sys.exit(0)

    print(
        "❌ XGrammar 검증 실패"
    )

    if probe_failures:

        print()
        print(
            "[강제 Schema 실패]"
        )

        for failure in (
            probe_failures
        ):

            print(
                failure
            )

    if project_failures:

        print()
        print(
            "[OrderUpdate 실패]"
        )

        for failure in (
            project_failures
        ):

            print(
                failure
            )

    sys.exit(1)


if __name__ == "__main__":
    main()
