#!/usr/bin/env python3

import json
from pathlib import Path

from router_schema import RouterOutput
from router_policy import evaluate_router_output


GOLD_PATH = Path(
    __file__
).parent / "router_data/gold/router_gold_smoke_v1.jsonl"


def main():

    passed = 0
    failed = 0

    with GOLD_PATH.open(
        encoding="utf-8"
    ) as f:

        for lineno, line in enumerate(
            f,
            start=1,
        ):

            line = line.strip()

            if not line:
                continue

            try:
                case = json.loads(line)

                expected_output = (
                    RouterOutput.model_validate(
                        case["expected"]
                    )
                )

                decisions = (
                    evaluate_router_output(
                        expected_output
                    )
                )

                actual_policy = [
                    decision.status.value
                    for decision in decisions
                ]

                expected_policy = (
                    case["expected_policy"]
                )

                if (
                    actual_policy
                    !=
                    expected_policy
                ):

                    raise AssertionError(
                        f"policy mismatch: "
                        f"expected={expected_policy}, "
                        f"actual={actual_policy}"
                    )

                print(
                    f"[PASS] "
                    f"{case['id']} "
                    f"{case['utterance']}"
                )

                passed += 1

            except Exception as e:

                print(
                    f"[FAIL] line={lineno}: "
                    f"{type(e).__name__}: {e}"
                )

                failed += 1


    print()
    print(
        f"RESULT: "
        f"{passed} PASS / "
        f"{failed} FAIL"
    )

    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
