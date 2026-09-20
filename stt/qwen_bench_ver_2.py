"""
qwen_bench.py - Qwen3-ASR 을 기존 평가셋으로 재본다

Clova 의 --replay 와 같은 형식으로 결과를 남기므로 compare.py 로 나란히 볼 수 있다.
같은 오디오, 같은 정답, 같은 지표여야 비교가 성립한다.

주의: 채점 함수는 clova_stt.py 에도 같은 게 있다. 고칠 일이 생기면 양쪽 다 고쳐라.
      (qwen 은 별도 conda 환경에서 돌리므로 clova_stt 를 import 할 수 없다)

    python3 qwen_bench.py ./eval --split test
    python3 qwen_bench.py ./eval --split test --model TeamUNIVA/qwen3_asr_1.7b_ko_beta
    python3 qwen_bench.py ./eval --limit 5 --verbose      # 먼저 5개만
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import wave
from collections import Counter
from pathlib import Path


# ------------------------------------------------------------------ 채점
# clova_stt.py 와 동일해야 한다. 다르면 비교가 무의미해진다.
_NUM_WORDS = {
    "한": "1", "하나": "1", "일": "1", "두": "2", "둘": "2", "이": "2",
    "세": "3", "셋": "3", "삼": "3", "네": "4", "넷": "4", "사": "4",
    "다섯": "5", "오": "5", "여섯": "6", "육": "6", "일곱": "7", "칠": "7",
    "여덟": "8", "팔": "8", "아홉": "9", "구": "9", "열": "10", "십": "10",
}
_COUNTERS = "개잔장명번컵판인"
_NUM_RE = re.compile(
    "(" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + ")"
    + f"(?=[{_COUNTERS}])"
)


def normalize_numbers(s: str) -> str:
    return _NUM_RE.sub(lambda m: _NUM_WORDS[m.group(1)], s)


# 구두점. 모델마다 붙이는 정책이 달라서(Clova 는 거의 안 붙이고 Qwen 은 꼬박 붙인다)
# 이걸 안 지우면 모델 비교가 성립하지 않는다. 주문 파싱에도 영향이 없다.
_PUNCT_RE = re.compile(r"[.,!?;:·…‥\-~'\"“”‘’()\[\]]")


def strip_punct(s: str) -> str:
    return _PUNCT_RE.sub("", s)

# 한자어 수사. Qwen 은 "735번" 을 "칠백삼십오 번" 으로 쓰고 Clova 는 숫자로 쓴다.
# 소리는 맞게 알아들은 것이므로 오류로 세면 안 된다.
#
# "이", "사", "구" 는 일상 단어에도 흔해서("이랑", "사이즈", "구매") 무조건 바꾸면 문장이 깨진다.
# 그래서 뒤에 수량/순번 단위가 붙은 경우에만 변환한다.
_SINO_DIGIT = {"영": 0, "공": 0, "일": 1, "이": 2, "삼": 3, "사": 4,
               "오": 5, "육": 6, "륙": 6, "칠": 7, "팔": 8, "구": 9}
_SINO_UNIT = {"십": 10, "백": 100, "천": 1000}
_SINO_BIG = {"만": 10 ** 4, "억": 10 ** 8}
_SINO_CHARS = "".join(list(_SINO_DIGIT) + list(_SINO_UNIT) + list(_SINO_BIG))
# 이 단위가 뒤에 붙어야 수사로 인정한다
_SINO_SUFFIX = "번호원개잔명년월일시분초층份"
_SINO_RE = re.compile(f"([{_SINO_CHARS}]{{1,12}})(?=[{_SINO_SUFFIX}])")


def _sino_to_int(s: str) -> int | None:
    """'칠백삼십오' -> 735. 해석 불가면 None."""
    total = part = 0
    cur = None
    for ch in s:
        if ch in _SINO_DIGIT:
            cur = _SINO_DIGIT[ch]
        elif ch in _SINO_UNIT:
            part += (cur if cur is not None else 1) * _SINO_UNIT[ch]
            cur = None
        elif ch in _SINO_BIG:
            part = (part + (cur or 0)) or 1
            total += part * _SINO_BIG[ch]
            part = 0
            cur = None
        else:
            return None
    return total + part + (cur or 0)


def normalize_sino(s: str) -> str:
    def sub(m):
        v = _sino_to_int(m.group(1))
        return str(v) if v is not None else m.group(1)
    return _SINO_RE.sub(sub, s)


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


def cer(ref: str, hyp: str, normalize: bool = True) -> float:
    r, h = ref.replace(" ", ""), hyp.replace(" ", "")
    r, h = strip_punct(r), strip_punct(h)
    if normalize:
        r, h = normalize_sino(r), normalize_sino(h)
        r, h = normalize_numbers(r), normalize_numbers(h)
    if not r:
        return 0.0 if not h else 1.0
    return _lev(r, h) / len(r)


def keyword_recall(ref: str, hyp: str, words: list[str]) -> tuple[int, int, list[str]]:
    r, h = ref.replace(" ", ""), hyp.replace(" ", "")
    present = [w for w in words if w.replace(" ", "") in r]
    missed = [w for w in present if w.replace(" ", "") not in h]
    return len(present) - len(missed), len(present), missed


def keyword_inserted(ref: str, hyp: str, words: list[str]) -> list[str]:
    """
    정답에 없는데 인식 결과에 나타난 키워드. 프롬프트 주입의 실패 방식이다.

    Clova 의 과부스팅은 음절 중복("콜라라")으로 나타나 CER 이 잡아냈지만,
    Qwen 은 LLM 디코더라 없는 메뉴 이름을 통째로 지어낼 수 있다.
    재현율만 보면 이걸 놓치고, 오히려 "재현율 100%" 로 착각하게 된다.
    """
    r, h = ref.replace(" ", ""), hyp.replace(" ", "")
    return [w for w in words if w.replace(" ", "") in h and w.replace(" ", "") not in r]


def load_boost_words(path: str) -> list[str]:
    words: list[str] = []
    for line in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        words.extend(w.strip() for w in line.split(",") if w.strip())
    return words


# ------------------------------------------------------------------ 모델
class QwenAsr:
    """
    transformers 백엔드. 발화 하나를 통째로 던지고 텍스트를 받는다.
    스트리밍(vLLM 전용)은 쓰지 않는다. 센서가 시작을, VAD 가 끝을 이미 결정하므로
    발화 단위 오프라인 추론으로 충분하다.
    """

    def __init__(self, model_id: str, device: str = "cuda", dtype: str = "auto",
                 language: str | None = "ko", prompt: str | None = None):
        try:
            import torch
            from transformers import AutoProcessor, AutoModelForMultimodalLM
        except ImportError as e:
            sys.exit(f"의존성이 없다: {e}\n"
                     f"  pip install 'transformers>=5.13.0' torch soundfile accelerate")

        self.language = language
        self.prompt = prompt or None
        torch_dtype = {"auto": "auto", "fp16": torch.float16,
                       "bf16": torch.bfloat16, "fp32": torch.float32}[dtype]

        print(f"모델 로딩: {model_id} ({device}, {dtype})", file=sys.stderr)
        t0 = time.perf_counter()
        self.processor = AutoProcessor.from_pretrained(model_id)
        self.model = AutoModelForMultimodalLM.from_pretrained(
            model_id, dtype=torch_dtype, device_map=device,
        )
        self.model.eval()
        print(f"로딩 완료 ({time.perf_counter() - t0:.1f}초)", file=sys.stderr)

        if device == "cuda":
            try:
                free, total = torch.cuda.mem_get_info()
                print(f"VRAM 사용 {(total - free) / 2**30:.1f} / {total / 2**30:.1f} GiB",
                      file=sys.stderr)
            except Exception:
                pass

    def transcribe(self, wav_path: Path, max_new_tokens: int = 256) -> str:
        kwargs = {"audio": str(wav_path)}
        if self.language:
            kwargs["language"] = self.language
        if self.prompt:
            # 자유 형식 컨텍스트. Clova 의 weight 같은 세기 조절은 없다.
            kwargs["prompt"] = self.prompt

        inputs = self.processor.apply_transcription_request(**kwargs)
        inputs = inputs.to(self.model.device, self.model.dtype)

        import torch
        with torch.inference_mode():
            out = self.model.generate(**inputs, max_new_tokens=max_new_tokens)

        gen = out[:, inputs["input_ids"].shape[1]:]
        text = self.processor.decode(gen, return_format="transcription_only")[0]
        return (text or "").strip()


# ------------------------------------------------------------------ 실행
def wav_seconds(p: Path) -> float:
    try:
        with wave.open(str(p), "rb") as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        return 0.0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("dir", help="평가셋 폴더 (wav + json)")
    p.add_argument("--model", default="Qwen/Qwen3-ASR-1.7B-hf")
    p.add_argument("--split", choices=["dev", "test"], help="이 분할만")
    p.add_argument("--device", default="cuda")
    p.add_argument("--dtype", default="auto", choices=["auto", "fp16", "bf16", "fp32"])
    p.add_argument("--language", default="ko", help="빈 문자열이면 자동 감지")
    p.add_argument("--boost-file", help="키워드 재현율/정밀도 측정 기준 (menu.txt)")
    p.add_argument("--prompt", help="모델에 주입할 컨텍스트 (인라인)")
    p.add_argument("--prompt-file", help="모델에 주입할 컨텍스트 (파일 내용 그대로)")
    p.add_argument("--limit", type=int, help="앞에서 N개만. 먼저 소수로 확인할 때")
    p.add_argument("--max-new-tokens", type=int, default=256)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    d = Path(args.dir).expanduser()
    if not d.is_dir():
        sys.exit(f"{d} 가 없다.")

    items = []
    for wav in sorted(d.glob("*.wav")):
        meta = wav.with_suffix(".json")
        if not meta.is_file():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("exclude") or not (data.get("ref") or "").strip():
            continue
        if args.split and data.get("split") != args.split:
            continue
        items.append((wav, data))

    if not items:
        sys.exit("대상이 없다. label_eval.py --stats 로 확인해라.")
    if args.limit:
        items = items[:args.limit]

    boost_words = load_boost_words(args.boost_file) if args.boost_file else []

    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).expanduser().read_text(encoding="utf-8").strip()
    if prompt:
        print(f"컨텍스트 주입 ({len(prompt)}자): {prompt[:120]}"
              + ("..." if len(prompt) > 120 else ""), file=sys.stderr)

    asr = QwenAsr(args.model, args.device, args.dtype, args.language or None, prompt)

    rows, cers = [], []
    exact = kw_hit = kw_total = 0
    all_missed: list[str] = []
    all_inserted: list[str] = []
    kw_found = 0          # 인식 결과에 나타난 키워드 총수 (정밀도 분모)
    audio_sec = infer_sec = 0.0

    print(f"\n{len(items)}개 인식"
          + (f" [{args.split}]" if args.split else "")
          + (f" | 키워드 {len(boost_words)}개 기준" if boost_words else "") + "\n")

    for i, (wav, data) in enumerate(items, 1):
        ref = data["ref"].strip()
        dur = wav_seconds(wav)

        t0 = time.perf_counter()
        try:
            hyp = asr.transcribe(wav, args.max_new_tokens)
        except Exception as e:
            print(f"[{i}/{len(items)}] {wav.name} 실패: {e}")
            continue
        el = time.perf_counter() - t0

        audio_sec += dur
        infer_sec += el

        score = cer(ref, hyp)
        cers.append(score)
        exact += (score == 0.0)

        row = {"file": wav.name, "ref": ref, "hyp": hyp,
               "cer": round(score, 4), "infer_ms": round(el * 1000, 1),
               "audio_sec": round(dur, 2)}

        line = f"[{i}/{len(items)}] CER {score * 100:5.1f}%  {el * 1000:5.0f}ms"
        if boost_words:
            hit, present, missed = keyword_recall(ref, hyp, boost_words)
            kw_hit += hit
            kw_total += present
            all_missed.extend(missed)
            inserted = keyword_inserted(ref, hyp, boost_words)
            all_inserted.extend(inserted)
            kw_found += hit + len(inserted)
            row.update(kw_hit=hit, kw_total=present, kw_missed=missed,
                       kw_inserted=inserted)
            if present or inserted:
                line += f"  키워드 {hit}/{present}"
                if missed:
                    line += " 놓침: " + ", ".join(missed)
                if inserted:
                    line += " ★지어냄: " + ", ".join(inserted)
        print(line)
        if args.verbose or score > 0:
            print(f"    정답: {ref}")
            print(f"    인식: {hyp}")
        rows.append(row)

    if not cers:
        sys.exit("결과가 없다.")

    avg = sum(cers) / len(cers)
    rtf = infer_sec / audio_sec if audio_sec else 0

    print("\n" + "=" * 52)
    print(f"모델       {args.model}")
    print(f"처리       {len(cers)}개")
    print(f"평균 CER   {avg * 100:.2f}%   (낮을수록 좋음)")
    print(f"완전일치   {exact}/{len(cers)} ({exact / len(cers) * 100:.0f}%)")
    if kw_total:
        print(f"재현율     {kw_hit}/{kw_total} ({kw_hit / kw_total * 100:.1f}%)  놓치지 않았나")
        if kw_found:
            print(f"정밀도     {kw_hit}/{kw_found} ({kw_hit / kw_found * 100:.1f}%)  지어내지 않았나")
        if all_missed:
            print("자주 놓친: " + ", ".join(
                f"{w}({n})" for w, n in Counter(all_missed).most_common(5)))
        if all_inserted:
            print("★지어낸: " + ", ".join(
                f"{w}({n})" for w, n in Counter(all_inserted).most_common(5)))
    print(f"평균 추론  {infer_sec / len(cers) * 1000:.0f}ms / 발화")
    print(f"RTF        {rtf:.3f}   (1초 오디오를 {rtf:.3f}초에 처리. 낮을수록 빠름)")

    out_dir = d / "results"
    out_dir.mkdir(exist_ok=True)
    tag = args.model.split("/")[-1].replace(".", "")[:20]
    if args.prompt_file:
        tag += "_" + Path(args.prompt_file).stem[:12]
    elif prompt:
        tag += "_prompt"
    out = out_dir / f"{time.strftime('%m%d-%H%M%S')}_{tag}{'_' + args.split if args.split else ''}.json"
    out.write_text(json.dumps({
        "settings": {
            "model": args.model, "split": args.split, "language": args.language,
            "dtype": args.dtype, "boost_weight": 0,
            "prompt_file": args.prompt_file, "prompt": prompt,
        },
        "summary": {
            "n": len(cers), "cer": round(avg, 4), "exact": exact,
            "kw_hit": kw_hit, "kw_total": kw_total, "kw_found": kw_found,
            "inserted": Counter(all_inserted).most_common(10),
            "top_missed": Counter(all_missed).most_common(10),
            "rtf": round(rtf, 4),
            "avg_infer_ms": round(infer_sec / len(cers) * 1000, 1),
        },
        "rows": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장: {out}")


if __name__ == "__main__":
    main()