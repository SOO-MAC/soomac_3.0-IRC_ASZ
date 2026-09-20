"""
compare.py - replay 결과 비교

`--replay` 를 돌리면 <eval>/results/ 에 json 이 쌓인다. 그걸 표로 늘어놓는다.

    python3 compare.py ./eval/results              전체 비교표
    python3 compare.py ./eval/results --diff a b   두 실행의 항목별 차이
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_all(d: Path) -> list[tuple[Path, dict]]:
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append((p, json.loads(p.read_text(encoding="utf-8"))))
        except Exception:
            print(f"(읽기 실패: {p.name})", file=sys.stderr)
    return out


def table(runs: list[tuple[Path, dict]]) -> None:
    if not runs:
        sys.exit("결과 파일이 없다.")

    print(f"{'실행':26} {'weight':>7} {'분할':>5} {'n':>4} "
          f"{'CER':>8} {'완전일치':>10} {'키워드':>12}")
    print("-" * 80)

    best_cer = min(r["summary"]["cer"] for _, r in runs)
    best_kw = max((r["summary"]["kw_hit"] / r["summary"]["kw_total"])
                  if r["summary"]["kw_total"] else 0 for _, r in runs)

    for p, r in runs:
        s, st = r["summary"], r["settings"]
        n = s["n"] or 1
        kw = (s["kw_hit"] / s["kw_total"]) if s["kw_total"] else None
        mark_c = " *" if s["cer"] == best_cer else "  "
        mark_k = " *" if kw is not None and abs(kw - best_kw) < 1e-9 else "  "
        print(f"{p.stem:26} {st.get('boost_weight', 0):7.1f} "
              f"{st.get('split') or '전체':>5} {s['n']:4} "
              f"{s['cer'] * 100:7.2f}%{mark_c}"
              f"{s['exact']:5}/{n:<4}"
              f"{(f'{kw * 100:6.1f}%' if kw is not None else '     -'):>8}{mark_k}")

    print("\n* = 최고. CER 과 키워드 재현율이 다른 실행을 가리키면 키워드 쪽을 믿어라.")
    print("  주문 성공률에는 키워드가 훨씬 가깝다.")

    # 자주 놓친 것 모으기
    print("\n--- 실행별 자주 놓친 키워드 ---")
    for p, r in runs:
        top = r["summary"].get("top_missed") or []
        if top:
            print(f"  {p.stem}: " + ", ".join(f"{w}({n})" for w, n in top[:5]))


def diff(runs: list[tuple[Path, dict]], a: str, b: str) -> None:
    def pick(name: str):
        hits = [(p, r) for p, r in runs if name in p.stem]
        if not hits:
            sys.exit(f"'{name}' 에 맞는 결과가 없다.")
        if len(hits) > 1:
            sys.exit(f"'{name}' 이 여러 개다: " + ", ".join(p.stem for p, _ in hits))
        return hits[0]

    (pa, ra), (pb, rb) = pick(a), pick(b)
    ma = {row["file"]: row for row in ra["rows"] if "cer" in row}
    mb = {row["file"]: row for row in rb["rows"] if "cer" in row}
    common = sorted(set(ma) & set(mb))

    if not common:
        sys.exit("겹치는 항목이 없다.")

    better = worse = same = 0
    print(f"A = {pa.stem}   (weight {ra['settings'].get('boost_weight')})")
    print(f"B = {pb.stem}   (weight {rb['settings'].get('boost_weight')})\n")

    lines = []
    for f in common:
        ca, cb = ma[f]["cer"], mb[f]["cer"]
        if abs(ca - cb) < 1e-9:
            same += 1
            continue
        if cb < ca:
            better += 1
        else:
            worse += 1
        lines.append((cb - ca, f, ma[f], mb[f]))

    # 나빠진 것부터. 회귀가 제일 중요하다.
    for delta, f, ra_row, rb_row in sorted(lines, reverse=True):
        sign = "나빠짐" if delta > 0 else "좋아짐"
        print(f"[{sign}] {f}  CER {ra_row['cer'] * 100:.1f}% → {rb_row['cer'] * 100:.1f}%")
        print(f"  정답: {ra_row['ref']}")
        print(f"  A   : {ra_row['hyp']}")
        print(f"  B   : {rb_row['hyp']}")
        print()

    print(f"B 가 좋아진 것 {better} / 나빠진 것 {worse} / 동일 {same}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("dir", help="results 폴더")
    p.add_argument("--diff", nargs=2, metavar=("A", "B"),
                   help="두 실행의 항목별 차이 (파일명 일부)")
    args = p.parse_args()

    d = Path(args.dir).expanduser()
    if not d.is_dir():
        sys.exit(f"{d} 가 없다.")

    runs = load_all(d)
    if args.diff:
        diff(runs, *args.diff)
    else:
        table(runs)


if __name__ == "__main__":
    main()