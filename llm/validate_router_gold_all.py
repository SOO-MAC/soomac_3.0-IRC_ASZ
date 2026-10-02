#!/usr/bin/env python3

import json
from collections import Counter
from pathlib import Path

from router_schema import RouterOutput
from router_policy import evaluate_router_output


BASE = Path(__file__).parent

GOLD_FILES = sorted(
    (
        BASE / "router_data/gold"
    ).glob(
        "router_gold_*_v1.jsonl"
    )
)


def canonical(obj):
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
    )


def main():

    total = 0
    passed = 0
    failed = 0

    seen_ids = {}
    seen_inputs = {}

    tag_counts = Counter()
    family_counts = Counter()
    subtype_counts = Counter()
    policy_counts = Counter()
    resolution_counts = Counter()
    commitment_counts = Counter()

    cross_file_duplicates = []
    conflicts = []

    print("=" * 70)
    print("SOOMAC ROUTER GOLD VALIDATOR")
    print("=" * 70)

    for path in GOLD_FILES:

        if not path.exists():
            print(f"[WARN] missing: {path}")
            continue

        file_total = 0
        file_passed = 0

        print()
        print(f"[FILE] {path.name}")

        with path.open(
            encoding="utf-8"
        ) as f:

            for lineno, raw in enumerate(
                f,
                start=1,
            ):

                raw = raw.strip()

                if not raw:
                    continue

                total += 1
                file_total += 1

                try:
                    case = json.loads(raw)

                    case_id = case["id"]
                    utterance = case["utterance"]
                    context = case["context"]

                    # ------------------------------------------------
                    # ID duplicate
                    # ------------------------------------------------

                    if case_id in seen_ids:

                        raise ValueError(
                            "duplicate global id: "
                            f"{case_id} "
                            f"previous={seen_ids[case_id]} "
                            f"current={path.name}:{lineno}"
                        )

                    seen_ids[case_id] = (
                        f"{path.name}:{lineno}"
                    )

                    # ------------------------------------------------
                    # Schema validation
                    # ------------------------------------------------

                    expected = (
                        RouterOutput.model_validate(
                            case["expected"]
                        )
                    )

                    # ------------------------------------------------
                    # Policy validation
                    # ------------------------------------------------

                    decisions = (
                        evaluate_router_output(
                            expected
                        )
                    )

                    actual_policy = [
                        d.status.value
                        for d in decisions
                    ]

                    expected_policy = (
                        case["expected_policy"]
                    )

                    if (
                        actual_policy
                        != expected_policy
                    ):
                        raise AssertionError(
                            "policy mismatch: "
                            f"expected={expected_policy}, "
                            f"actual={actual_policy}"
                        )

                    # ------------------------------------------------
                    # Cross-file duplicate / conflict
                    # ------------------------------------------------

                    input_key = (
                        utterance,
                        canonical(context),
                    )

                    answer_signature = canonical(
                        {
                            "expected":
                                case["expected"],

                            "expected_policy":
                                expected_policy,
                        }
                    )

                    if input_key in seen_inputs:

                        previous = (
                            seen_inputs[input_key]
                        )

                        if (
                            previous["answer"]
                            ==
                            answer_signature
                        ):

                            cross_file_duplicates.append(
                                {
                                    "utterance":
                                        utterance,

                                    "previous":
                                        previous["location"],

                                    "current":
                                        f"{path.name}:{lineno}",
                                }
                            )

                        else:

                            conflicts.append(
                                {
                                    "utterance":
                                        utterance,

                                    "previous":
                                        previous["location"],

                                    "current":
                                        f"{path.name}:{lineno}",
                                }
                            )

                    else:

                        seen_inputs[input_key] = {
                            "answer":
                                answer_signature,

                            "location":
                                f"{path.name}:{lineno}",
                        }

                    # ------------------------------------------------
                    # Distribution
                    # ------------------------------------------------

                    tag = case.get(
                        "tag",
                        "untagged",
                    )

                    tag_counts[tag] += 1

                    for act in expected.acts:

                        family_counts[
                            act.family.value
                        ] += 1

                        subtype_counts[
                            act.subtype.value
                        ] += 1

                        resolution_counts[
                            act.resolution.value
                        ] += 1

                        commitment_counts[
                            act.commitment.value
                        ] += 1

                    for status in expected_policy:
                        policy_counts[status] += 1

                    passed += 1
                    file_passed += 1

                except Exception as e:

                    failed += 1

                    print(
                        f"[FAIL] "
                        f"{path.name}:{lineno} "
                        f"{type(e).__name__}: {e}"
                    )

        print(
            f"  -> {file_passed}/{file_total} schema+policy PASS"
        )

    # ========================================================
    # REPORT
    # ========================================================

    print()
    print("=" * 70)
    print("TOTAL")
    print("=" * 70)

    print(f"cases loaded       : {total}")
    print(f"schema/policy pass : {passed}")
    print(f"schema/policy fail : {failed}")

    print(
        "unique input/context: "
        f"{len(seen_inputs)}"
    )

    print(
        "cross-file duplicates: "
        f"{len(cross_file_duplicates)}"
    )

    print(
        "conflicting labels    : "
        f"{len(conflicts)}"
    )


    def show_counter(
        title,
        counter,
    ):

        print()
        print(title)

        for key, value in sorted(
            counter.items(),
            key=lambda x: (
                -x[1],
                x[0],
            ),
        ):

            print(
                f"  {key:32s} "
                f"{value:4d}"
            )


    show_counter(
        "TAG DISTRIBUTION",
        tag_counts,
    )

    show_counter(
        "FAMILY DISTRIBUTION",
        family_counts,
    )

    show_counter(
        "SUBTYPE DISTRIBUTION",
        subtype_counts,
    )

    show_counter(
        "POLICY DISTRIBUTION",
        policy_counts,
    )

    show_counter(
        "RESOLUTION DISTRIBUTION",
        resolution_counts,
    )

    show_counter(
        "COMMITMENT DISTRIBUTION",
        commitment_counts,
    )


    # ========================================================
    # DUPLICATES
    # ========================================================

    if cross_file_duplicates:

        print()
        print("CROSS-FILE DUPLICATES")

        for item in cross_file_duplicates:

            print(
                "  [DUP] "
                f"{item['utterance']!r} "
                f"{item['previous']} "
                f"<-> "
                f"{item['current']}"
            )


    # ========================================================
    # CONFLICTS
    # ========================================================

    if conflicts:

        print()
        print("!!! CONFLICTING GOLD LABELS !!!")

        for item in conflicts:

            print(
                "  [CONFLICT] "
                f"{item['utterance']!r} "
                f"{item['previous']} "
                f"<-> "
                f"{item['current']}"
            )


    print()
    print("=" * 70)

    if failed or conflicts:

        print(
            "RESULT: FAIL"
        )

        raise SystemExit(1)

    print(
        "RESULT: PASS"
    )


if __name__ == "__main__":
    main()
