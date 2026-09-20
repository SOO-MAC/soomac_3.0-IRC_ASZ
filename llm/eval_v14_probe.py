#!/usr/bin/env python3
import argparse
import copy
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from openai import OpenAI
from order_update_schema import OrderUpdate

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_API_BASE = "http://127.0.0.1:8000/v1"
DEFAULT_MODEL = "drive-thru-v14"
DEFAULT_RESULT = BASE_DIR / "eval_v14_blind_results.jsonl"

def close_schema(node: Any):
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            node["additionalProperties"] = False
        for value in node.values():
            close_schema(value)
    elif isinstance(node, list):
        for value in node:
            close_schema(value)
    return node

ORDER_UPDATE_SCHEMA = close_schema(copy.deepcopy(OrderUpdate.model_json_schema()))

def read_jsonl(path: Path):
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{line_no} JSON 오류") from e
    return rows

def canonical(model: OrderUpdate):
    return model.model_dump(
        mode="json",
        exclude_none=True,
        exclude_defaults=True,
    )


def normalize_independent_modify_order(payload):
    """
    서로 다른 line_id를 대상으로 하는 modify-only actions는
    실행 순서와 무관하므로 line_id 기준으로 정렬해서 비교한다.

    예:
      expected: line 2 수정 -> line 1 수정
      actual  : line 1 수정 -> line 2 수정

    최종 주문 상태가 같으면 semantic PASS로 본다.

    주의:
    - modify 외 operation이 섞이면 정렬하지 않는다.
    - 같은 line_id를 두 번 이상 수정하면 순서가 의미를 가질 수 있으므로 정렬하지 않는다.
    """
    normalized = copy.deepcopy(payload)

    actions = normalized.get("actions")

    if not isinstance(actions, list) or len(actions) < 2:
        return normalized

    if not all(
        isinstance(action, dict)
        and action.get("operation") == "modify"
        for action in actions
    ):
        return normalized

    line_ids = []

    for action in actions:
        target = action.get("target")

        if not isinstance(target, dict):
            return normalized

        line_id = target.get("line_id")

        if not isinstance(line_id, int):
            return normalized

        line_ids.append(line_id)

    if len(set(line_ids)) != len(line_ids):
        return normalized

    normalized["actions"] = sorted(
        actions,
        key=lambda action: (
            action["target"]["line_id"],
            json.dumps(
                action,
                ensure_ascii=False,
                sort_keys=True,
            ),
        ),
    )

    return normalized


def semantic_equal(expected, actual):
    if actual == expected:
        return True, False

    expected_normalized = normalize_independent_modify_order(expected)
    actual_normalized = normalize_independent_modify_order(actual)

    if actual_normalized == expected_normalized:
        return True, True

    return False, False

def get_message(row, role):
    for msg in row.get("messages", []):
        if msg.get("role") == role:
            return msg.get("content", "")
    raise ValueError(f"{role} message 없음")

def extract_utterance(content: str):
    marker = "현재 사용자 발화:\n"
    return content.split(marker, 1)[1].strip() if marker in content else content.strip()

def first_existing(candidates):
    for rel in candidates:
        path = BASE_DIR / rel
        if path.exists():
            return path
    return None

def discover_suites():
    suites = {}
    candidates = {
        "v14_probe": ["dataset_v14/robustness_probe_v14.jsonl"],
        "v14_test": ["dataset_v14/test.jsonl"],
        "v14_blind": ["dataset_v14/real_blind_v14_test.jsonl"],
        "v13_test": ["dataset_v13/test.jsonl"],
        "v13_blind": ["dataset_v13/real_blind_v13_test.jsonl"],
        "v12_test": ["dataset_v12/test.jsonl"],
        "v12_blind": ["dataset_v12/real_blind_v12_test.jsonl"],
        "v11_test": ["dataset_v11/test.jsonl"],
        "v11_blind": ["dataset_v11/real_blind_v11_test.jsonl"],
        "v9_test": ["dataset_v9/test.jsonl"],
        "v9_blind": [
            "dataset_v9/real_blind_v9_test.jsonl",
            "dataset_v9/v9_blind.jsonl",
            "dataset_v9/blind.jsonl",
        ],
        "v8_test": ["dataset_v8/test.jsonl"],
        "v8_blind": ["dataset_v8/real_blind_v8_test.jsonl"],
        "v7_test": ["dataset_v7/test.jsonl"],
        "v7_blind": ["dataset_v7/real_blind_v7_test.jsonl"],
        "blind_v1": ["dataset/real_blind_test.jsonl", "real_blind_test.jsonl"],
        "blind_v2": ["dataset/real_blind_v2_test.jsonl", "real_blind_v2_test.jsonl"],
        "blind_v3": ["dataset/real_blind_v3_test.jsonl", "real_blind_v3_test.jsonl"],
    }
    for name, paths in candidates.items():
        path = first_existing(paths)
        if path is not None:
            suites[name] = path
    return suites

def call_model(client, model_name, system_text, user_text):
    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ],
        temperature=0.0,
        max_tokens=256,
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
    text = response.choices[0].message.content
    if not text:
        raise RuntimeError("LLM 응답 비어 있음")
    return text

def evaluate_suite(client, model_name, suite_name, path, limit, result_fp, show_failures):
    rows = read_jsonl(path)
    if limit > 0:
        rows = rows[:limit]

    print("\n" + "=" * 88)
    print(f"[{suite_name}] {path}")
    print(f"샘플 수: {len(rows)}")
    print("=" * 88)

    counts = Counter()
    category_counts = defaultdict(Counter)
    failure_printed = 0

    for idx, row in enumerate(rows, 1):
        category = row.get("category", "unknown")
        counts["total"] += 1
        category_counts[category]["total"] += 1

        system_text = get_message(row, "system")
        user_text = get_message(row, "user")
        expected_text = get_message(row, "assistant")
        utterance = extract_utterance(user_text)

        record = {
            "suite": suite_name,
            "index": idx,
            "category": category,
            "utterance": utterance,
            "expected_raw": expected_text,
        }

        try:
            expected = canonical(OrderUpdate.model_validate_json(expected_text))
            record["expected"] = expected
        except Exception as e:
            counts["dataset_error"] += 1
            category_counts[category]["dataset_error"] += 1
            record["status"] = "DATASET_ERROR"
            record["error"] = f"{type(e).__name__}: {e}"
            result_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
            continue

        try:
            actual_text = call_model(client, model_name, system_text, user_text)
            counts["api_ok"] += 1
            category_counts[category]["api_ok"] += 1
            record["actual_raw"] = actual_text
        except Exception as e:
            counts["api_fail"] += 1
            category_counts[category]["api_fail"] += 1
            record["status"] = "API_FAIL"
            record["error"] = f"{type(e).__name__}: {e}"
            result_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
            if failure_printed < show_failures:
                failure_printed += 1
                print(f"\n❌ API FAIL #{idx} [{category}]")
                print("발화:", utterance)
                print(record["error"])
            continue

        try:
            actual = canonical(OrderUpdate.model_validate_json(actual_text))
            counts["pydantic_ok"] += 1
            category_counts[category]["pydantic_ok"] += 1
            record["actual"] = actual
        except Exception as e:
            counts["pydantic_fail"] += 1
            category_counts[category]["pydantic_fail"] += 1
            record["status"] = "PYDANTIC_FAIL"
            record["error"] = f"{type(e).__name__}: {e}"
            result_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
            if failure_printed < show_failures:
                failure_printed += 1
                print(f"\n❌ PYDANTIC FAIL #{idx} [{category}]")
                print("발화:", utterance)
                print("RAW:", actual_text)
            continue

        is_equal, order_normalized = semantic_equal(
            expected,
            actual,
        )

        if is_equal:
            counts["semantic_pass"] += 1
            category_counts[category]["semantic_pass"] += 1
            record["status"] = "PASS"

            if order_normalized:
                counts["order_normalized_pass"] += 1
                category_counts[category]["order_normalized_pass"] += 1
                record["comparison_note"] = (
                    "independent modify action order ignored"
                )
        else:
            counts["semantic_fail"] += 1
            category_counts[category]["semantic_fail"] += 1
            record["status"] = "SEMANTIC_FAIL"
            if failure_printed < show_failures:
                failure_printed += 1
                print(f"\n❌ SEMANTIC FAIL #{idx} [{category}]")
                print("발화:", utterance)
                print("EXPECTED:")
                print(json.dumps(expected, ensure_ascii=False, indent=2))
                print("ACTUAL:")
                print(json.dumps(actual, ensure_ascii=False, indent=2))

        result_fp.write(json.dumps(record, ensure_ascii=False) + "\n")

        if idx % 25 == 0 or idx == len(rows):
            print(f"\r진행: {idx}/{len(rows)}", end="", flush=True)

    print()
    total = counts["total"]
    passed = counts["semantic_pass"]
    rate = 100.0 * passed / total if total else 0.0

    print(f"\n{suite_name} RESULT")
    print("-" * 88)
    print(f"TOTAL          : {total}")
    print(f"API OK         : {counts['api_ok']}/{total}")
    print(f"PYDANTIC OK    : {counts['pydantic_ok']}/{total}")
    print(f"SEMANTIC PASS  : {passed}/{total} ({rate:.2f}%)")
    print(f"ORDER-NORM PASS: {counts['order_normalized_pass']}")
    print(f"SEMANTIC FAIL  : {counts['semantic_fail']}")
    print(f"API FAIL       : {counts['api_fail']}")
    print(f"PYDANTIC FAIL  : {counts['pydantic_fail']}")
    print(f"DATASET ERROR  : {counts['dataset_error']}")

    print("\n카테고리별:")
    for category in sorted(category_counts):
        c = category_counts[category]
        c_total = c["total"]
        c_pass = c["semantic_pass"]
        c_rate = 100.0 * c_pass / c_total if c_total else 0.0
        mark = "✅" if c_pass == c_total else "❌"
        print(f"  {mark} {category:42s} {c_pass:4d}/{c_total:4d} {c_rate:6.2f}%")

    return {
        "suite": suite_name,
        "path": str(path),
        "total": total,
        "passed": passed,
        "rate": rate,
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--suites",
        default="v14_probe",
        help="기본값: v14_probe. 필요 시 기존 blind suite도 지정 가능",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--show-failures", type=int, default=20)
    parser.add_argument("--result", type=Path, default=DEFAULT_RESULT)
    args = parser.parse_args()

    suites = discover_suites()
    requested = [x.strip() for x in args.suites.split(",") if x.strip()]
    missing = [x for x in requested if x not in suites]

    if missing:
        print("suite 파일 없음:", ", ".join(missing), file=sys.stderr)
        print("발견된 suite:", file=sys.stderr)
        for name, path in suites.items():
            print(f"  {name}: {path}", file=sys.stderr)
        sys.exit(2)

    print("=" * 88)
    print("SOOMAC DRIVE-THRU V14 EVALUATION")
    print("=" * 88)
    print("API   :", args.api_base)
    print("MODEL :", args.model)
    for name in requested:
        print(f"{name:12s}: {suites[name]}")

    client = OpenAI(base_url=args.api_base, api_key="EMPTY")

    try:
        available = [m.id for m in client.models.list().data]
    except Exception as e:
        raise RuntimeError("vLLM 서버 연결 실패. 먼저 V14 서버를 실행하세요.") from e

    if args.model not in available:
        raise RuntimeError(
            f"서버에 '{args.model}' 모델 없음. 현재 모델: {available}"
        )

    result_path = args.result.expanduser().resolve()
    summaries = []

    with result_path.open("w", encoding="utf-8") as result_fp:
        for name in requested:
            summaries.append(
                evaluate_suite(
                    client=client,
                    model_name=args.model,
                    suite_name=name,
                    path=suites[name],
                    limit=args.limit,
                    result_fp=result_fp,
                    show_failures=args.show_failures,
                )
            )

    print("\n" + "=" * 88)
    print("FINAL SUMMARY")
    print("=" * 88)

    all_pass = True
    for s in summaries:
        ok = s["total"] > 0 and s["passed"] == s["total"]
        all_pass = all_pass and ok
        mark = "✅ PASS" if ok else "❌ FAIL"
        print(f"{mark:8s} {s['suite']:12s} {s['passed']}/{s['total']} ({s['rate']:.2f}%)")

    print("\n상세 결과:", result_path)

    if all_pass:
        print("\n✅ 선택한 모든 suite 완전 PASS")
        sys.exit(0)

    print("\n❌ 하나 이상의 suite에서 실패가 있습니다.")
    sys.exit(1)

if __name__ == "__main__":
    main()
