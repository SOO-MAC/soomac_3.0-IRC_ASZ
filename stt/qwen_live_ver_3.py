"""
qwen_live.py - Qwen3-ASR 로컬 실시간 STT

이번 판 추가 (잡음 할루시네이션 대책)
    SpeechVerifier   분절된 오디오를 Silero VAD 로 재검증한다.
                     webrtcvad(2013년 GMM)는 의자 끄는 소리 같은 광대역 잡음을
                     음성으로 판정한다. 그게 Qwen 에 들어가면 LLM 디코더라
                     "모르겠다" 가 없어서 "그렇죠" 같은 걸 지어낸다.
                     Silero 가 음성이 아니라고 하면 Qwen 에 넘기지 않고, 계속 듣는다.

    신뢰도            생성 토큰의 평균 로그확률(--scored). 억지로 만든 텍스트는 낮게 나오는
                     경향이 있다. 척도가 모델마다 달라 기본은 기록만 한다.

    리샘플링          MicStream 이 16kHz 직접 열기를 먼저 시도하고, 안 되면 soxr 로 변환한다.
                     audioop.ratecv 는 안티앨리어싱이 없어서 고주파 잡음이 음성 대역으로
                     접혀 들어온다. 잡음 환경일수록 VAD 를 더 쉽게 속인다.

왜 webrtcvad 를 안 걷어내나
    Silero 는 16kHz 에서 정확히 512 샘플만 받는데 우리 프레임은 480 샘플이다.
    프레임 단위로 바꾸면 튜닝해둔 끝점 타이밍(600ms, 헛트리거 처리)을 다시 잡아야 한다.
    webrtcvad 는 빠른 1차 분절, Silero 는 분절 후 1회 검증으로 역할을 나눈다.

설치
    pip install silero-vad soxr

실행
    python3 qwen_live.py
    python3 qwen_live.py --record ./live --scored
    python3 qwen_live.py --no-verify            # 효과 비교용
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyaudio
import webrtcvad

# ------------------------------------------------------------------ 오디오 상수
SAMPLE_RATE = 16_000
CHANNELS = 1
FRAME_MS = 30
FRAMES_PER_BUFFER = SAMPLE_RATE * FRAME_MS // 1000     # 480
FRAME_BYTES = FRAMES_PER_BUFFER * CHANNELS * 2         # 960
AUDIO_FORMAT = pyaudio.paInt16


# ------------------------------------------------------------------ 결과
@dataclass
class LiveResult:
    text: str = ""
    pcm: bytes | None = None
    reason: str = "unknown"

    # 지연을 구간별로 쪼갠다. 어디서 먹는지 봐야 줄일 데가 보인다.
    speech_end_at: float = 0.0      # VAD 가 끝점을 선언한 시각 (monotonic)
    infer_ms: float = 0.0           # 모델 추론
    total_ms: float = 0.0           # 끝점 선언 -> 텍스트 확정 (손님 체감)
    audio_sec: float = 0.0
    speech_sec: float = 0.0         # 프리롤 제외한 실제 발화 길이
    false_starts: int = 0           # 너무 짧아서 버린 트리거
    noise_rejects: int = 0          # Silero 가 잡음으로 판정해 버린 횟수

    confidence: float | None = None      # 생성 토큰 평균 로그확률 (--scored)
    silero_prob: float | None = None     # 통과한 구간의 최고 음성 확률
    silero_speech_ms: float | None = None

    @property
    def rtf(self) -> float:
        return self.infer_ms / 1000 / self.audio_sec if self.audio_sec else 0.0


# ------------------------------------------------------------------ 마이크
class MicStream:
    """
    게이트가 닫혀도 계속 read 한다. 안 읽으면 드라이버 버퍼에 TTS 목소리가 쌓인다.
    큐는 유한하고 꽉 차면 오래된 것부터 버린다. 과거보다 현재가 중요하다.
    16kHz 로 못 여는 장치를 위해 리샘플링을 내장한다.
    """

    def __init__(self, device_index: int | None = None, queue_seconds: float = 6.0):
        self.q: queue.Queue[bytes] = queue.Queue(maxsize=int(queue_seconds * 1000 / FRAME_MS))
        self.dropped = 0
        self._enabled = threading.Event()
        self._stop = threading.Event()
        self._pa: pyaudio.PyAudio | None = None
        self._stream = None
        self._thread: threading.Thread | None = None
        self._device_index = device_index
        self._capture_rate = SAMPLE_RATE
        self._capture_frames = FRAMES_PER_BUFFER
        self._resampler = None

    def start(self) -> None:
        self._pa = pyaudio.PyAudio()

        try:
            if self._device_index is None:
                info = self._pa.get_default_input_device_info()
                self._device_index = int(info["index"])
            else:
                info = self._pa.get_device_info_by_index(self._device_index)
        except Exception as e:
            self._pa.terminate()
            self._pa = None
            raise RuntimeError(f"마이크 장치 정보를 못 읽었다: {e}")

        if int(info.get("maxInputChannels", 0)) < CHANNELS:
            self._pa.terminate()
            self._pa = None
            raise RuntimeError(
                f"장치 {self._device_index} ({info['name']}) 는 입력 장치가 아니다. "
                f"--list-devices 로 확인해라.")

        print(f"마이크: [{self._device_index}] {info['name']}", file=sys.stderr)

        # 1) 16kHz 직접. 되면 리샘플링이 없으니 제일 깨끗하다.
        try:
            self._stream = self._pa.open(
                format=AUDIO_FORMAT, channels=CHANNELS, rate=SAMPLE_RATE, input=True,
                input_device_index=self._device_index, frames_per_buffer=FRAMES_PER_BUFFER)
            print(f"캡처: {SAMPLE_RATE} Hz 직접", file=sys.stderr)

        # 2) 장치 기본 레이트 + soxr
        except OSError:
            self._capture_rate = int(round(float(info["defaultSampleRate"])))
            self._capture_frames = self._capture_rate * FRAME_MS // 1000
            try:
                self._stream = self._pa.open(
                    format=AUDIO_FORMAT, channels=CHANNELS, rate=self._capture_rate,
                    input=True, input_device_index=self._device_index,
                    frames_per_buffer=self._capture_frames)
            except OSError as e:
                self._pa.terminate()
                self._pa = None
                raise RuntimeError(f"마이크를 못 열었다: {e}")

            try:
                import soxr
                self._resampler = soxr.ResampleStream(
                    self._capture_rate, SAMPLE_RATE, CHANNELS, dtype="int16", quality="HQ")
                print(f"캡처: {self._capture_rate} -> {SAMPLE_RATE} Hz (soxr HQ)", file=sys.stderr)
            except ImportError:
                print(f"캡처: {self._capture_rate} -> {SAMPLE_RATE} Hz "
                      f"(★앨리어싱 있음. pip install soxr 권장)", file=sys.stderr)

        self._thread = threading.Thread(target=self._loop, name="mic", daemon=True)
        self._thread.start()

    def _resample(self, pcm: bytes, state):
        if self._capture_rate == SAMPLE_RATE:
            return pcm, state
        if self._resampler is not None:
            arr = np.frombuffer(pcm, dtype=np.int16)
            return np.asarray(self._resampler.resample_chunk(arr), dtype=np.int16).tobytes(), state
        import audioop      # 3.13 에서 삭제됐으므로 폴백 경로에서만 import
        return audioop.ratecv(pcm, 2, CHANNELS, self._capture_rate, SAMPLE_RATE, state)

    def _loop(self) -> None:
        acc = bytearray()
        first = True
        state = None
        while not self._stop.is_set():
            try:
                pcm = self._stream.read(self._capture_frames, exception_on_overflow=False)
            except Exception as e:
                if not self._stop.is_set():
                    print(f"마이크 read 실패: {e}", file=sys.stderr)
                    self._stop.set()
                return

            if not pcm:
                continue

            pcm, state = self._resample(pcm, state)

            if first:
                print(f"첫 chunk: {len(pcm)}바이트", file=sys.stderr)
                first = False

            acc.extend(pcm)
            while len(acc) >= FRAME_BYTES:
                frame = bytes(acc[:FRAME_BYTES])
                del acc[:FRAME_BYTES]
                if not self._enabled.is_set():
                    continue
                try:
                    self.q.put_nowait(frame)
                except queue.Full:
                    try:
                        self.q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self.q.put_nowait(frame)
                    except queue.Full:
                        pass
                    self.dropped += 1

    def drain(self) -> None:
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                return

    def open_gate(self) -> None:
        self.drain()
        self._enabled.set()

    def close_gate(self) -> None:
        self._enabled.clear()
        self.drain()

    def get(self, timeout: float) -> bytes | None:
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._stream:
            try:
                if self._stream.is_active():
                    self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
        if self._pa:
            try:
                self._pa.terminate()
            except Exception:
                pass


# ------------------------------------------------------------------ 잡음 검증
@dataclass
class VerifyResult:
    is_speech: bool
    speech_ms: float
    max_prob: float
    segments: int


class SpeechVerifier:
    """
    분절된 PCM 이 사람 목소리인지 Silero VAD 로 판정한다.

    전체 PCM 을 get_speech_timestamps 에 넘긴다. 윈도우 분할(512 샘플)은 Silero 가
    알아서 하므로 우리 480 샘플 프레임과 충돌하지 않는다.

    min_speech_ms 를 Silero 기본값(250)보다 낮게 둔다. "네" 같은 짧은 대답을
    잘라내면 안 되기 때문이다. 여기서 가릴 것은 길이가 아니라 음성이냐 잡음이냐다.
    """

    def __init__(self, threshold: float = 0.5, min_speech_ms: int = 120):
        from silero_vad import load_silero_vad, get_speech_timestamps
        import torch

        torch.set_num_threads(1)     # Qwen 추론과 CPU 를 다투지 않게
        self._torch = torch
        self._get_ts = get_speech_timestamps
        self._model = load_silero_vad()
        self.threshold = threshold
        self.min_speech_ms = min_speech_ms

    def verify(self, pcm: bytes) -> VerifyResult:
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        wav = self._torch.from_numpy(audio)

        ts = self._get_ts(wav, self._model, sampling_rate=SAMPLE_RATE,
                          threshold=self.threshold, min_speech_duration_ms=self.min_speech_ms)

        # 최고 확률은 임계값을 데이터로 정할 때 필요하다. 실패해도 판정 자체는 살린다.
        max_prob = 0.0
        try:
            self._model.reset_states()
            for i in range(0, len(audio) - 512 + 1, 512):
                max_prob = max(max_prob, self._model(wav[i:i + 512], SAMPLE_RATE).item())
            self._model.reset_states()
        except Exception:
            pass

        return VerifyResult(
            is_speech=bool(ts),
            speech_ms=sum(t["end"] - t["start"] for t in ts) / SAMPLE_RATE * 1000,
            max_prob=max_prob,
            segments=len(ts),
        )


# ------------------------------------------------------------------ 분절
@dataclass
class SegmentConfig:
    vad_mode: int = 2
    pre_roll_ms: int = 300
    # Clova 는 1300 이 필요했지만 그건 서버 flush 용이었다.
    # 로컬은 "말이 끝났나" 판단만 하면 되므로 훨씬 짧아도 된다.
    end_silence_ms: int = 600
    max_utterance_ms: int = 20_000
    start_timeout_ms: int = 6_000
    min_speech_ms: int = 200        # 이보다 짧으면 기침/잡음으로 본다


class Segmenter:
    """
    게이트가 열린 뒤 발화 하나를 모아서 PCM 으로 돌려준다.

    verifier 를 주면 끝점에서 Silero 검증을 거친다. 잡음이면 버리고 계속 듣는다.
    세션을 끝내지 않는 게 중요하다. 손님이 말을 꺼내기도 전에 닫히면 안 된다.
    """

    def __init__(self, mic: MicStream, cfg: SegmentConfig,
                 verifier: "SpeechVerifier | None" = None):
        self.mic = mic
        self.cfg = cfg
        self.verifier = verifier
        self.vad = webrtcvad.Vad(cfg.vad_mode)
        self.reason = "unknown"
        self.speech_end_at = 0.0
        self.speech_frames = 0
        self.false_starts = 0
        self.noise_rejects = 0
        self.last_verify: VerifyResult | None = None

    def _is_speech(self, pcm: bytes) -> bool:
        if len(pcm) != FRAME_BYTES:
            return True
        try:
            return self.vad.is_speech(pcm, SAMPLE_RATE)
        except Exception:
            return True

    def collect(self, cancel: threading.Event) -> bytes | None:
        pre = deque(maxlen=max(1, self.cfg.pre_roll_ms // FRAME_MS))
        end_frames = max(1, self.cfg.end_silence_ms // FRAME_MS)
        max_frames = max(1, self.cfg.max_utterance_ms // FRAME_MS)
        min_frames = max(1, self.cfg.min_speech_ms // FRAME_MS)

        chunks: list[bytes] = []
        started = False
        silence = 0
        voiced = 0
        self.false_starts = 0
        self.noise_rejects = 0
        deadline = time.monotonic() + self.cfg.start_timeout_ms / 1000

        def restart():
            """모은 걸 버리고 다시 대기 상태로. 세션은 살린다."""
            nonlocal chunks, started, silence, voiced, deadline
            chunks = []
            pre.clear()
            started = False
            silence = 0
            voiced = 0
            # 잡음에 반응하느라 소모된 시간만큼 대기 시간을 돌려준다
            deadline = time.monotonic() + self.cfg.start_timeout_ms / 1000

        while not cancel.is_set():
            pcm = self.mic.get(timeout=0.1)
            if pcm is None:
                if not started and time.monotonic() > deadline:
                    self.reason = "no_speech"
                    return None
                continue

            speech = self._is_speech(pcm)

            if not started:
                pre.append(pcm)
                if speech:
                    started = True
                    chunks.extend(pre)      # 프리롤부터 붙여야 첫 음절이 안 잘린다
                    pre.clear()
                elif time.monotonic() > deadline:
                    self.reason = "no_speech"
                    return None
                continue

            chunks.append(pcm)
            if speech:
                voiced += 1
                silence = 0
            else:
                silence += 1

            if silence < end_frames and len(chunks) < max_frames:
                continue

            self.speech_end_at = time.monotonic()
            self.speech_frames = voiced

            # 1차: 너무 짧으면 기침·클릭이다.
            # 여기서 return 하면 손님이 말을 꺼내기도 전에 세션이 죽는다.
            if voiced < min_frames:
                self.false_starts += 1
                restart()
                continue

            # 2차: Silero 가 사람 목소리라고 하는가
            if self.verifier is not None:
                try:
                    v = self.verifier.verify(b"".join(chunks))
                    self.last_verify = v
                    if not v.is_speech:
                        self.noise_rejects += 1
                        restart()
                        continue
                except Exception as e:
                    # 검증기가 죽어도 인식은 계속한다. 방어선 하나가 빠질 뿐이다.
                    print(f"Silero 검증 실패(통과시킴): {e}", file=sys.stderr)
                    self.last_verify = None

            self.reason = "local_silence" if silence >= end_frames else "max_length"
            return b"".join(chunks)

        self.reason = "cancelled"
        return None


# ------------------------------------------------------------------ 모델
class QwenAsr:
    def __init__(self, model_id: str, device: str = "cuda", dtype: str = "auto",
                 language: str | None = "ko", max_new_tokens: int = 256,
                 prompt: str | None = None):
        try:
            import torch
            from transformers import AutoProcessor, AutoModelForMultimodalLM
        except ImportError as e:
            sys.exit(f"의존성이 없다: {e}\n"
                     f"  pip install 'transformers>=5.13.0' torch soundfile librosa accelerate")

        self.torch = torch
        self.language = language
        self.max_new_tokens = max_new_tokens
        # 자유 형식 컨텍스트. Clova 의 weight 같은 세기 조절은 Qwen 에 없다.
        self.prompt = prompt or None
        torch_dtype = {"auto": "auto", "fp16": torch.float16,
                       "bf16": torch.bfloat16, "fp32": torch.float32}[dtype]

        print(f"모델 로딩: {model_id}", file=sys.stderr)
        t0 = time.perf_counter()
        self.processor = AutoProcessor.from_pretrained(model_id)
        self.model = AutoModelForMultimodalLM.from_pretrained(
            model_id, dtype=torch_dtype, device_map=device)
        self.model.eval()
        print(f"로딩 완료 ({time.perf_counter() - t0:.1f}초)", file=sys.stderr)

        if device == "cuda" and torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            print(f"VRAM {(total - free) / 2**30:.1f} / {total / 2**30:.1f} GiB", file=sys.stderr)

    def _inputs(self, pcm: bytes):
        """PCM 을 파일로 떨구지 않고 메모리에서 바로 넘긴다. 디스크 왕복도 아깝다."""
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        kwargs = {"audio": audio}
        if self.language:
            kwargs["language"] = self.language
        if self.prompt:
            kwargs["prompt"] = self.prompt
        return self.processor.apply_transcription_request(**kwargs).to(
            self.model.device, self.model.dtype)

    def transcribe(self, pcm: bytes) -> str:
        inputs = self._inputs(pcm)
        with self.torch.inference_mode():
            out = self.model.generate(**inputs, max_new_tokens=self.max_new_tokens)
        gen = out[:, inputs["input_ids"].shape[1]:]
        return (self.processor.decode(gen, return_format="transcription_only")[0] or "").strip()

    def transcribe_scored(self, pcm: bytes) -> tuple[str, float | None]:
        """
        인식 + 생성 토큰의 평균 로그확률.

        잡음에서 억지로 만든 텍스트는 로그확률이 낮게 나오는 경향이 있다.
        다만 척도가 모델마다 달라 기준값은 반드시 실측으로 정해야 한다.
        점수 계산이 실패해도 인식 결과는 살린다.
        transcribe() 보다 느리고 메모리를 더 쓴다 (스텝마다 로짓을 보관).
        """
        inputs = self._inputs(pcm)
        with self.torch.inference_mode():
            out = self.model.generate(**inputs, max_new_tokens=self.max_new_tokens,
                                      output_scores=True, return_dict_in_generate=True)

        gen = out.sequences[:, inputs["input_ids"].shape[1]:]
        text = (self.processor.decode(gen, return_format="transcription_only")[0] or "").strip()

        conf = None
        try:
            lp = self.model.compute_transition_scores(
                out.sequences, out.scores, normalize_logits=True)[0]
            lp = lp[self.torch.isfinite(lp)]
            if lp.numel():
                conf = float(lp.mean().item())
        except Exception:
            pass
        return text, conf

    def warmup(self, n: int = 2) -> None:
        """
        첫 추론은 CUDA 커널 컴파일 때문에 몇 배 느리다. (실측 2016ms vs 230ms)
        손님 첫 주문이 그 값을 맞으면 안 되니 미리 태운다.
        """
        if n <= 0:
            return
        silence = b"\x00" * (FRAME_BYTES * 33)     # 1초
        print(f"워밍업 {n}회...", file=sys.stderr)
        for i in range(n):
            t0 = time.perf_counter()
            try:
                self.transcribe(silence)
            except Exception as e:
                print(f"  워밍업 실패(무시): {e}", file=sys.stderr)
                return
            print(f"  {i + 1}회 {(time.perf_counter() - t0) * 1000:.0f}ms", file=sys.stderr)


# ------------------------------------------------------------------ 저장
def save_utterance(out_dir: Path, r: LiveResult) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    wav = out_dir / f"{stem}.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(r.pcm or b"")
    (out_dir / f"{stem}.json").write_text(json.dumps({
        "ref": "", "hyp": r.text, "reason": r.reason,
        "duration_sec": round(r.audio_sec, 3),
        "false_starts": r.false_starts,
        "noise_rejects": r.noise_rejects,
        "silero_prob": r.silero_prob,
        "silero_speech_ms": r.silero_speech_ms,
        "confidence": r.confidence,
        "speech_sec": round(r.speech_sec, 3),
        "infer_ms": round(r.infer_ms, 1),
        "total_ms": round(r.total_ms, 1),
        "rtf": round(r.rtf, 4),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return wav


# ------------------------------------------------------------------ 실행
def main() -> None:
    p = argparse.ArgumentParser()
    _s = SegmentConfig()

    g = p.add_argument_group("장치")
    g.add_argument("--device", type=int, default=None)
    g.add_argument("--list-devices", action="store_true")

    g = p.add_argument_group("분절")
    g.add_argument("--vad-mode", type=int, default=_s.vad_mode, choices=[0, 1, 2, 3])
    g.add_argument("--end-silence", type=int, default=_s.end_silence_ms,
                   help=f"끝점 무음 ms (기본 {_s.end_silence_ms}). "
                        f"Clova 의 1300 은 서버 사정이었다. 로컬은 더 짧아도 된다")
    g.add_argument("--pre-roll", type=int, default=_s.pre_roll_ms)
    g.add_argument("--min-speech", type=int, default=_s.min_speech_ms)
    g.add_argument("--max-utterance", type=int, default=_s.max_utterance_ms)

    g = p.add_argument_group("잡음 검증")
    g.add_argument("--no-verify", dest="verify", action="store_false",
                   help="Silero 검증 끄기. 효과를 비교할 때 쓴다")
    g.add_argument("--silero-threshold", type=float, default=0.5)
    g.add_argument("--silero-min-speech", type=int, default=120,
                   help="Silero 기본 250 보다 낮게. '네' 를 잘라내면 안 된다")

    g = p.add_argument_group("모델")
    g.add_argument("--model", default="Qwen/Qwen3-ASR-1.7B-hf")
    g.add_argument("--dtype", default="auto", choices=["auto", "fp16", "bf16", "fp32"])
    g.add_argument("--language", default="ko")
    g.add_argument("--max-new-tokens", type=int, default=256)
    g.add_argument("--warmup", type=int, default=2, help="0이면 끔")
    g.add_argument("--scored", action="store_true",
                   help="신뢰도(로그확률)까지 계산. 느려진다")
    g.add_argument("--prompt", help="주입할 컨텍스트 (인라인)")
    g.add_argument("--prompt-file", help="주입할 컨텍스트 (파일 내용 그대로)")

    p.add_argument("--record", metavar="DIR", help="발화를 wav+json 으로 저장")
    args = p.parse_args()

    if args.list_devices:
        pa = pyaudio.PyAudio()
        try:
            default_in = pa.get_default_input_device_info().get("index")
        except Exception:
            default_in = None
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if int(info.get("maxInputChannels", 0)) > 0:
                print(f"[{i}] {info['name']}{' [기본]' if i == default_in else ''}")
        pa.terminate()
        return

    cfg = SegmentConfig(vad_mode=args.vad_mode, pre_roll_ms=args.pre_roll,
                        end_silence_ms=args.end_silence, min_speech_ms=args.min_speech,
                        max_utterance_ms=args.max_utterance)

    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).expanduser().read_text(encoding="utf-8").strip()
    if prompt:
        print(f"컨텍스트 {len(prompt)}자 주입", file=sys.stderr)

    asr = QwenAsr(args.model, "cuda", args.dtype, args.language or None,
                  args.max_new_tokens, prompt)
    asr.warmup(args.warmup)

    verifier = None
    if args.verify:
        try:
            t0 = time.perf_counter()
            verifier = SpeechVerifier(args.silero_threshold, args.silero_min_speech)
            print(f"Silero 로딩 완료 ({time.perf_counter() - t0:.1f}초), "
                  f"threshold={args.silero_threshold}", file=sys.stderr)
        except Exception as e:
            print(f"★Silero 로딩 실패, 검증 없이 진행: {e}\n"
                  f"  pip install silero-vad", file=sys.stderr)

    mic = MicStream(device_index=args.device)
    try:
        mic.start()
    except RuntimeError as e:
        sys.exit(str(e))

    record_dir = Path(args.record).expanduser() if args.record else None
    print(f"\n분절: end_silence={cfg.end_silence_ms}ms vad={cfg.vad_mode} "
          f"pre_roll={cfg.pre_roll_ms}ms | 잡음검증 {'켬' if verifier else '끔'}",
          file=sys.stderr)
    print("Enter = 센서 트리거, Ctrl+C = 종료\n", file=sys.stderr)

    stats: list[LiveResult] = []
    total_noise = 0
    try:
        while True:
            try:
                input(">>> Enter 를 누르고 말하세요: ")
            except EOFError:
                break

            seg = Segmenter(mic, cfg, verifier)

            mic.open_gate()
            pcm = seg.collect(threading.Event())
            mic.close_gate()

            total_noise += seg.noise_rejects
            if pcm is None:
                print(f"  [빈 결과] 종료={seg.reason}"
                      + (f" 잡음거부={seg.noise_rejects}회" if seg.noise_rejects else "")
                      + "\n")
                continue

            r = LiveResult(pcm=pcm, reason=seg.reason, speech_end_at=seg.speech_end_at,
                           false_starts=seg.false_starts, noise_rejects=seg.noise_rejects)
            r.audio_sec = len(pcm) / 2 / SAMPLE_RATE
            r.speech_sec = seg.speech_frames * FRAME_MS / 1000
            if seg.last_verify:
                r.silero_prob = round(seg.last_verify.max_prob, 3)
                r.silero_speech_ms = round(seg.last_verify.speech_ms)

            t0 = time.perf_counter()
            try:
                if args.scored:
                    r.text, r.confidence = asr.transcribe_scored(pcm)
                else:
                    r.text = asr.transcribe(pcm)
            except Exception as e:
                print(f"  [추론 실패] {e}\n")
                continue
            r.infer_ms = (time.perf_counter() - t0) * 1000
            # 손님이 체감하는 지연: 말이 끝난 순간부터 텍스트가 나올 때까지.
            # end_silence 가 여기 포함된다. 이게 줄일 수 있는 전부다.
            r.total_ms = (time.monotonic() - r.speech_end_at) * 1000 + cfg.end_silence_ms

            print(f"  결과: {r.text or '(없음)'}")
            line = f"  종료={r.reason} 오디오={r.audio_sec:.2f}s 발화={r.speech_sec:.2f}s"
            if r.false_starts:
                line += f" 헛트리거={r.false_starts}"
            if r.noise_rejects:
                line += f" 잡음거부={r.noise_rejects}"
            if r.silero_prob is not None:
                line += f" silero={r.silero_prob:.2f}"
            if r.confidence is not None:
                line += f" conf={r.confidence:.2f}"
            print(line)
            print(f"  지연: 끝점대기 {cfg.end_silence_ms}ms + 추론 {r.infer_ms:.0f}ms "
                  f"+ 기타 {r.total_ms - cfg.end_silence_ms - r.infer_ms:.0f}ms "
                  f"= 체감 {r.total_ms:.0f}ms   (RTF {r.rtf:.3f})")

            if record_dir:
                print(f"  저장: {save_utterance(record_dir, r).name}")
            print()
            stats.append(r)

    except KeyboardInterrupt:
        pass
    finally:
        print("\n종료 중...", file=sys.stderr)
        mic.stop()
        if stats:
            inf = [s.infer_ms for s in stats]
            tot = [s.total_ms for s in stats]
            rtf = [s.rtf for s in stats if s.audio_sec]
            print(f"\n발화 {len(stats)}개 | 잡음거부 누적 {total_noise}회")
            print(f"  추론   중앙값 {sorted(inf)[len(inf) // 2]:.0f}ms  "
                  f"최대 {max(inf):.0f}ms")
            print(f"  체감   중앙값 {sorted(tot)[len(tot) // 2]:.0f}ms  "
                  f"최대 {max(tot):.0f}ms")
            if rtf:
                print(f"  RTF    중앙값 {sorted(rtf)[len(rtf) // 2]:.3f}")
            confs = sorted(s.confidence for s in stats if s.confidence is not None)
            if confs:
                print(f"  신뢰도 최저 {confs[0]:.2f}  중앙값 {confs[len(confs) // 2]:.2f}")
        if mic.dropped:
            print(f"  폐기된 프레임 {mic.dropped}")


if __name__ == "__main__":
    main()