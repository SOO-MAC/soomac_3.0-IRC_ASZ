#!/usr/bin/env python3
"""
tts_text.py - TTS 에 넣기 전 텍스트 정리

숫자를 한글 읽기로 미리 풀어둔다. TTS 모델에게 "12,300원" 을 맡기면
"일만 이천삼백", "만 이천 삼백", 쉼표를 끊어 읽기 등 결과가 매번 다를 수 있고,
고정 문구 캐시도 깨진다. 읽는 방식을 여기서 한 번만 결정한다.

    12,300원      -> 만 이천삼백 원          (한자어 수)
    맥오더 123번  -> 맥오더 백이십삼 번       (한자어 수)
    2개 / x2      -> 두 개                   (고유어 수, 99 이하)

Supertonic 에 넘기기 전 반드시 필요한 이유 (SDK 소스 확인):
    - SDK 전처리는 NFKD·이모지·기호 정리뿐이고 한국어 숫자 읽기가 없다. 숫자는 모델이 알아서 읽는다.
    - 지원하지 않는 문자가 하나라도 있으면 synthesize() 가 ValueError 로 실패한다.
      그래서 마지막에 화이트리스트로 한 번 더 거른다(filter_unsupported).
    - duration predictor 가 입력 글자 수로 길이를 정하므로, 실제 읽는 글자로 풀어줘야 속도가 맞는다.

문장 단위로 쪼개는 이유:
    1) 문장별로 캐시한다. "주문이 완료되었습니다. 주문 금액은 ~원입니다. 앞으로 이동해주세요."
       에서 가운데 문장만 새로 합성하면 된다.
    2) 첫 문장을 재생하는 동안 다음 문장을 합성한다(체감 지연 감소).
"""
from __future__ import annotations

import re
import unicodedata

# ------------------------------------------------------------------ 한자어 수
_SINO_DIGITS = "영일이삼사오육칠팔구"


def _sino_under_10000(n: int) -> str:
    out = []
    for value, unit in ((1000, "천"), (100, "백"), (10, "십")):
        d, n = divmod(n, value)
        if d:
            # "일천", "일백", "일십" 이라고 읽지 않는다
            out.append(("" if d == 1 else _SINO_DIGITS[d]) + unit)
    if n:
        out.append(_SINO_DIGITS[n])
    return "".join(out)


def sino_korean(n: int) -> str:
    """한자어 수 읽기. 12300 -> '만 이천삼백'"""
    if n < 0:
        return "마이너스 " + sino_korean(-n)
    if n == 0:
        return "영"

    parts = []
    for value, unit in ((10**12, "조"), (10**8, "억"), (10**4, "만"), (1, "")):
        d, n = divmod(n, value)
        if not d:
            continue
        if unit == "만" and d == 1:
            parts.append("만")          # "일만" 이 아니라 "만"
        else:
            parts.append(_sino_under_10000(d) + unit)
    return " ".join(parts)


# ------------------------------------------------------------------ 고유어 수
_NATIVE_ONES = ["", "한", "두", "세", "네", "다섯", "여섯", "일곱", "여덟", "아홉"]
_NATIVE_TENS = ["", "열", "스물", "서른", "마흔", "쉰", "예순", "일흔", "여든", "아흔"]


def native_korean_counter(n: int) -> str | None:
    """단위명사 앞 고유어 수. 2 -> '두', 20 -> '스무', 21 -> '스물한'. 100 이상은 None"""
    if not 1 <= n <= 99:
        return None
    tens, ones = divmod(n, 10)
    if tens == 2 and ones == 0:
        return "스무"
    return _NATIVE_TENS[tens] + _NATIVE_ONES[ones]


# 고유어 수로 읽는 단위
NATIVE_COUNTERS = ("세트", "개", "잔", "명", "병", "봉지", "마리")
# 한자어 수로 읽는 단위
SINO_COUNTERS = ("원", "번")

_COUNTER_ALT = "|".join(
    sorted(NATIVE_COUNTERS + SINO_COUNTERS, key=len, reverse=True)
)

_NUM_WITH_COUNTER = re.compile(
    r"(\d{1,3}(?:,\d{3})+|\d+)\s*(" + _COUNTER_ALT + r")?"
)

# "x2", "×2" (수량 표기)
_TIMES_QTY = re.compile(r"[x×X]\s*(\d{1,2})\b")


def _replace_number(m: re.Match) -> str:
    number = int(m.group(1).replace(",", ""))
    counter = m.group(2)

    if counter in NATIVE_COUNTERS:
        native = native_korean_counter(number)
        if native is not None:
            return f"{native} {counter}"
        return f"{sino_korean(number)} {counter}"

    if counter in SINO_COUNTERS:
        return f"{sino_korean(number)} {counter}"

    return sino_korean(number)


# ------------------------------------------------------------------ 발음 교정 사전
# Supertonic 은 G2P 가 없어서 발음을 고칠 수단이 철자뿐이다.
# 모델이 잘못 읽는 단어가 확인되면 여기에(또는 lexicon JSON 에) 발음대로 적는다.
#   예) "맥오더": "매고더"
# 조사가 붙어도 걸리도록 부분 문자열로 치환한다. 긴 단어부터 적용한다.
PRONUNCIATION_OVERRIDES: dict[str, str] = {}


def load_lexicon(path) -> dict[str, str]:
    import json
    from pathlib import Path
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("lexicon 은 {단어: 발음} 형태의 JSON 객체여야 합니다.")
    return {str(k): str(v) for k, v in data.items()}


def apply_lexicon(text: str, lexicon: dict[str, str] | None = None) -> str:
    table = dict(PRONUNCIATION_OVERRIDES)
    if lexicon:
        table.update(lexicon)
    for word in sorted(table, key=len, reverse=True):
        text = text.replace(word, table[word])
    return text


# ------------------------------------------------------------------ 문장부호
# 끝 부호가 없을 때만 보정한다. 억양 조절 수단이 문장부호뿐이라 의문문의 "?" 가 중요하다.
_QUESTION_ENDING = re.compile(r"(까|까요|나요|가요|는지요|습니까|니까|ㄹ래요|을래요|죠|지요)$")
_HAS_END_PUNCT = re.compile(r"[.?!]$")


def ensure_end_punctuation(sentence: str) -> str:
    sentence = sentence.rstrip(" ,")
    if not sentence or _HAS_END_PUNCT.search(sentence):
        return sentence
    return sentence + ("?" if _QUESTION_ENDING.search(sentence) else ".")


# ------------------------------------------------------------------ 화이트리스트
# 한글 음절, 영문, 공백, 기본 문장부호만 남긴다. 숫자는 이 단계 전에 모두 한글로 바뀐다.
_ALLOWED = re.compile(r"[가-힣A-Za-z .,?!]")


def filter_unsupported(text: str) -> tuple[str, list[str]]:
    """허용 외 문자를 제거하고 제거한 문자 목록을 돌려준다(로그용)."""
    removed = sorted({ch for ch in text if not _ALLOWED.fullmatch(ch)})
    if removed:
        text = "".join(ch if _ALLOWED.fullmatch(ch) else " " for ch in text)
        text = re.sub(r" {2,}", " ", text).strip()
    return text, removed


def normalize_for_tts(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or ""))

    # 제어 문자·서식 문자 제거
    text = "".join(
        ch for ch in text
        if unicodedata.category(ch) not in ("Cc", "Cf") or ch in "\n"
    )

    text = _TIMES_QTY.sub(
        lambda m: f" {native_korean_counter(int(m.group(1))) or sino_korean(int(m.group(1)))} 개",
        text,
    )
    text = _NUM_WITH_COUNTER.sub(_replace_number, text)

    # TTS 가 소리 내어 읽거나 이상하게 끊는 기호
    text = text.replace("~", "").replace("…", ".")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


# ------------------------------------------------------------------ 문장 분할
_SENTENCE_END = re.compile(r"(?<=[.?!。？！])\s+|\n+")


def split_sentences(text: str) -> list[str]:
    parts = (p.strip() for p in _SENTENCE_END.split(text))
    return [p for p in parts if p and any(ch.isalnum() for ch in p)]


def prepare(text: str, lexicon: dict[str, str] | None = None,
            removed_out: list | None = None) -> list[str]:
    """
    정규화 -> 발음 교정 -> 문장 분할 -> 끝 부호 보정 -> 화이트리스트.
    결과 문장이 그대로 캐시 키이자 모델 입력이다.
    removed_out 을 넘기면 화이트리스트에서 지운 문자를 담아준다.
    """
    text = apply_lexicon(normalize_for_tts(text), lexicon)
    result = []
    for sentence in split_sentences(text):
        sentence, removed = filter_unsupported(ensure_end_punctuation(sentence))
        if removed and removed_out is not None:
            removed_out.extend(removed)
        if sentence and any(ch.isalnum() for ch in sentence):
            result.append(sentence)
    return result


if __name__ == "__main__":
    for sample in (
        "주문이 완료되었습니다. 주문 금액은 12,300원입니다. 앞으로 이동해주세요. ",
        "맥오더 123번 맞으신가요?",
        "불고기버거 세트 2개, 콜라 20잔, 치즈스틱 x3 맞으실까요?",
        "음료 사이즈는 스몰, 미디엄, 라지 중 어떤 것으로 드릴까요?",
        "합계 100,000원, 10000원, 1500원",
        "스프라이트 L 사이즈 ★ 드릴까요",
        "맞으신가요",
    ):
        removed = []
        print(prepare(sample, removed_out=removed), removed or "")