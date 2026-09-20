"""
label_eval.py - 평가셋 라벨링 도구

녹음해둔 wav 를 하나씩 들려주고 정답(ref)을 받아 json 에 채운다.
저장은 매번 즉시 하므로 중간에 끊어도 안전하다.

    python3 label_eval.py ./rec                 라벨링 시작 (안 된 것만)
    python3 label_eval.py ./rec --all           이미 채운 것도 다시
    python3 label_eval.py ./rec --stats         진행률과 베이스라인 점수
    python3 label_eval.py ./rec --split         dev/test 로 나누기

라벨링 중 입력:
    (그냥 Enter)  인식 결과를 정답으로 채택
    텍스트 입력    그걸 정답으로 저장
    r             다시 듣기
    s             건너뛰기 (나중에)
    x             평가셋에서 제외 (녹음 실패, 말 실수 등)
    q             종료
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path


# ------------------------------------------------------------------ 오디오 재생
def find_player() -> list[str] | None:
    for cmd in (["aplay", "-q"], ["paplay"], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]):
        if shutil.which(cmd[0]):
            return cmd
    return None


PLAYER = find_player()


def play(path: Path) -> None:
    if PLAYER is None:
        print("  (재생 프로그램이 없다. sudo apt install alsa-utils)")
        return
    try:
        # ALSA 경고가 화면을 덮어서 정답 입력을 방해한다. 삼킨다.
        subprocess.run(PLAYER + [str(path)], check=False, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"  (재생 실패: {e})")


# ------------------------------------------------------------------ 채점
def _lev(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a or not b:
        return len(a) or len(b)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# 수관형사 / 수사. 서버는 ITN 으로 "2개" 처럼 쓰고 사람은 "두 개" 로 적는다.
# 소리는 맞게 알아들은 것이므로 오류로 세면 CER 이 부풀려지고,
# 부스팅 A/B 를 할 때 진짜 개선이 이 노이즈에 묻힌다.
_NUM_WORDS = {
    "한": "1", "하나": "1", "일": "1",
    "두": "2", "둘": "2", "이": "2",
    "세": "3", "셋": "3", "삼": "3",
    "네": "4", "넷": "4", "사": "4",
    "다섯": "5", "오": "5",
    "여섯": "6", "육": "6",
    "일곱": "7", "칠": "7",
    "여덟": "8", "팔": "8",
    "아홉": "9", "구": "9",
    "열": "10", "십": "10",
}
# 수량을 세는 말. 이 앞에 붙은 수사만 바꾼다.
# ("이"->2 같은 규칙을 무조건 적용하면 "이랑", "사이즈" 까지 망가진다)
_COUNTERS = "개잔장명번份컵판인"

_NUM_RE = re.compile(
    "(" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + ")"
    + f"(?=[{_COUNTERS}])"
)


def normalize_numbers(s: str) -> str:
    """'두개' -> '2개'. 수량 단위 앞에 붙은 수사만 숫자로 통일한다."""
    return _NUM_RE.sub(lambda m: _NUM_WORDS[m.group(1)], s)

# 구두점. 모델마다 붙이는 정책이 달라서(Clova 는 거의 안 붙이고 Qwen 은 꼬박 붙인다)
# 이걸 안 지우면 모델 비교가 성립하지 않는다. 주문 파싱에도 영향이 없다.
_PUNCT_RE = re.compile(r"[.,!?;:\u00b7\u2026\u2025\-~'\"\u201c\u201d\u2018\u2019()\[\]]")


def strip_punct(s: str) -> str:
    return _PUNCT_RE.sub("", s)


def cer(ref: str, hyp: str, normalize: bool = True) -> float:
    """공백 제외 글자 오류율. 주문 파싱에 띄어쓰기는 영향이 없다."""
    r, h = ref.replace(" ", ""), hyp.replace(" ", "")
    r, h = strip_punct(r), strip_punct(h)
    if normalize:
        r, h = normalize_numbers(r), normalize_numbers(h)
    if not r:
        return 0.0 if not h else 1.0
    return _lev(r, h) / len(r)


# ------------------------------------------------------------------ 파일 입출력
def load(meta: Path) -> dict:
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save(meta: Path, data: dict) -> None:
    meta.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def pairs(d: Path) -> list[tuple[Path, Path]]:
    out = []
    for wav in sorted(d.glob("*.wav")):
        meta = wav.with_suffix(".json")
        if meta.is_file():
            out.append((wav, meta))
    return out


def duration(wav: Path) -> float:
    try:
        with wave.open(str(wav), "rb") as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        return 0.0


# ------------------------------------------------------------------ 라벨링
def run_label(d: Path, relabel: bool, only: str | None = None) -> None:
    items = pairs(d)
    if not items:
        sys.exit(f"{d} 에 wav+json 쌍이 없다.")

    todo = []
    for wav, meta in items:
        data = load(meta)
        if only:
            # 파일명 일부로 특정 항목만 고칠 때
            if only not in wav.name:
                continue
        else:
            if data.get("exclude"):
                continue
            if not relabel and (data.get("ref") or "").strip():
                continue
        todo.append((wav, meta))

    if not todo:
        print("라벨링할 게 없다. --stats 로 확인해봐라.")
        return

    print(f"\n{len(todo)}개 라벨링. Enter=인식결과 채택, 텍스트=직접입력,")
    print("r=다시듣기, b=이전으로, s=건너뛰기, x=제외, q=종료\n")

    saved = 0
    i = 0
    while 0 <= i < len(todo):
        wav, meta = todo[i]
        data = load(meta)          # 매번 다시 읽는다. b 로 돌아왔을 때 최신값을 보려면 필요하다.
        hyp = (data.get("hyp") or "").strip()
        cur = (data.get("ref") or "").strip()

        print(f"[{i + 1}/{len(todo)}] {wav.name}  ({duration(wav):.1f}초)")
        print(f"  인식: {hyp or '(없음)'}")
        if cur:
            print(f"  현재 정답: {cur}")
        if data.get("exclude"):
            print("  (현재 제외됨)")

        play(wav)

        advance = True
        while True:
            try:
                ans = input("  정답> ").strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n중단. {saved}개 저장됨.")
                return

            if ans == "r":
                play(wav)
                continue
            if ans == "b":
                if i == 0:
                    print("  처음이다.")
                    continue
                i -= 1
                advance = False
                print("  이전으로\n")
                break
            if ans == "s":
                print("  건너뜀\n")
                break
            if ans == "q":
                print(f"\n종료. {saved}개 저장됨.")
                return
            if ans == "x":
                data["exclude"] = True
                data["ref"] = ""
                save(meta, data)
                saved += 1
                print("  평가셋에서 제외\n")
                break

            ref = ans if ans else hyp
            if not ref:
                print("  비어 있다. 직접 입력하거나 x 로 제외해라.")
                continue

            data["ref"] = ref
            data.pop("exclude", None)
            save(meta, data)
            saved += 1
            score = cer(ref, hyp)
            print(f"  저장 ({'일치' if score == 0 else f'CER {score * 100:.0f}%'})\n")
            break

        if advance:
            i += 1

    print(f"완료. {saved}개 저장됨.")


# ------------------------------------------------------------------ 통계
def run_stats(d: Path) -> None:
    items = pairs(d)
    if not items:
        sys.exit(f"{d} 에 wav+json 쌍이 없다.")

    labeled, unlabeled, excluded = [], 0, 0
    for wav, meta in items:
        data = load(meta)
        if data.get("exclude"):
            excluded += 1
        elif (data.get("ref") or "").strip():
            labeled.append((wav, data))
        else:
            unlabeled += 1

    print(f"전체 {len(items)}개 | 라벨 완료 {len(labeled)} | 미완료 {unlabeled} | 제외 {excluded}")
    if not labeled:
        print("라벨링부터 해라.")
        return

    scores = [cer(d["ref"], d.get("hyp") or "") for _, d in labeled]
    raw = [cer(d["ref"], d.get("hyp") or "", normalize=False) for _, d in labeled]
    exact = sum(1 for s in scores if s == 0)
    total_sec = sum(duration(w) for w, _ in labeled)

    print("\n--- 현재 설정 베이스라인 ---")
    print(f"평균 CER   {sum(scores) / len(scores) * 100:.2f}%   (숫자표기 정규화 후)")
    print(f"           {sum(raw) / len(raw) * 100:.2f}%   (정규화 전, 참고용)")
    print(f"완전일치   {exact}/{len(labeled)} ({exact / len(labeled) * 100:.0f}%)")
    print(f"총 길이    {total_sec:.0f}초")

    splits = {}
    for _, data in labeled:
        splits[data.get("split", "미분할")] = splits.get(data.get("split", "미분할"), 0) + 1
    print(f"분할       {splits}")

    worst = sorted(zip(scores, labeled), key=lambda x: -x[0])[:5]
    if worst and worst[0][0] > 0:
        print("\n--- 오차 큰 것 (고치려면 --only 뒤 이름 일부) ---")
        for s, (wav, data) in worst:
            if s == 0:
                break
            print(f"  {wav.stem}   CER {s * 100:.1f}%")
            print(f"    정답: {data['ref']}")
            print(f"    인식: {data.get('hyp')}")


# ------------------------------------------------------------------ 분할
def run_split(d: Path, test_ratio: float, seed: int) -> None:
    """
    dev / test 로 나눈다.
      dev  - 파라미터를 여기에 맞춘다
      test - 최종 확인에만 쓴다. 여기 맞춰 튜닝하면 과적합이다
    """
    items = [(w, m, load(m)) for w, m in pairs(d)]
    usable = [(w, m, x) for w, m, x in items
              if not x.get("exclude") and (x.get("ref") or "").strip()]
    if not usable:
        sys.exit("라벨링된 게 없다.")

    rnd = random.Random(seed)
    idx = list(range(len(usable)))
    rnd.shuffle(idx)
    n_test = max(1, int(len(usable) * test_ratio))

    for rank, i in enumerate(idx):
        w, m, data = usable[i]
        data["split"] = "test" if rank < n_test else "dev"
        save(m, data)

    print(f"dev {len(usable) - n_test}개 / test {n_test}개 (seed={seed})")
    print("dev 로 튜닝하고 test 는 최종 확인에만 써라.")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("dir", help="녹음 폴더")
    p.add_argument("--all", action="store_true", help="이미 채운 것도 다시 라벨링")
    p.add_argument("--only", metavar="이름일부",
                   help="파일명에 이 문자열이 들어간 것만 (오타 고칠 때)")
    p.add_argument("--stats", action="store_true", help="진행률과 베이스라인")
    p.add_argument("--split", action="store_true", help="dev/test 분할")
    p.add_argument("--test-ratio", type=float, default=0.25)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    d = Path(args.dir).expanduser()
    if not d.is_dir():
        sys.exit(f"{d} 가 없다.")

    if args.stats:
        run_stats(d)
    elif args.split:
        run_split(d, args.test_ratio, args.seed)
    else:
        run_label(d, args.all, args.only)


if __name__ == "__main__":
    main()