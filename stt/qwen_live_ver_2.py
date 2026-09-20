"""
qwen_live.py - Qwen3-ASR 로컬 실시간 STT

clova_stt.py 에서 마이크·VAD·분절 구조를 그대로 가져오고 서버 관련 로직만 걷어냈다.

버린 것과 이유:
    end_silence 1300ms  Clova 서버가 마지막 구간을 flush 할 시간을 주려고 넣은 값이다.
                        로컬은 그럴 이유가 없다. 순수하게 "말이 끝났나" 판단만 하면 되므로
                        기본값을 600ms 로 내렸다. 실측으로 조정해라.
    epFlag / seqId      서버 프로토콜
    채널 재접속 / 재시도  네트워크
    tail_padding        서버가 처리할 시간을 벌려던 것

남긴 것:
    게이트가 닫혀도 마이크는 계속 read (안 읽으면 드라이버 버퍼에 TTS 가 쌓인다)
    유한 큐 + 오래된 것부터 폐기
    read 결과를 바이트로 누적해 정확히 960B 로 자름 (webrtcvad 는 1바이트도 안 봐준다)
    프리롤 300ms (VAD 가 반응한 시점엔 첫 음절이 지났다)

실행:
    conda activate qwen3-asr
    python3 qwen_live.py
    python3 qwen_live.py --end-silence 400 --record ./live
    python3 qwen_live.py --warmup 3 --list-devices
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
    false_starts: int = 0           # 잡음에 VAD 가 헛트리거된 횟수

    @property
    def rtf(self) -> float:
        return self.infer_ms / 1000 / self.audio_sec if self.audio_sec else 0.0


# ------------------------------------------------------------------ 마이크
class MicStream:
    """clova_stt.py 와 동일. 서버와 무관하게 맞는 설계라 그대로 가져왔다."""

    def __init__(self, device_index: int | None = None, queue_seconds: float = 6.0):
        self.q: queue.Queue[bytes] = queue.Queue(maxsize=int(queue_seconds * 1000 / FRAME_MS))
        self.dropped = 0
        self._enabled = threading.Event()
        self._stop = threading.Event()
        self._pa: pyaudio.PyAudio | None = None
        self._stream = None
        self._thread: threading.Thread | None = None
        self._device_index = device_index

    def start(self) -> None:
        self._pa = pyaudio.PyAudio()

        if self._device_index is not None:
            try:
                info = self._pa.get_device_info_by_index(self._device_index)
            except Exception:
                self._pa.terminate()
                raise RuntimeError(f"장치 {self._device_index} 가 없다. --list-devices 로 확인해라.")
            if int(info.get("maxInputChannels", 0)) < CHANNELS:
                self._pa.terminate()
                raise RuntimeError(
                    f"장치 {self._device_index} ({info['name']}) 는 입력 채널이 없다. "
                    f"--device 를 빼고 기본 장치를 써라.")

        try:
            self._stream = self._pa.open(
                format=AUDIO_FORMAT, channels=CHANNELS, rate=SAMPLE_RATE, input=True,
                input_device_index=self._device_index, frames_per_buffer=FRAMES_PER_BUFFER)
        except OSError as e:
            self._pa.terminate()
            self._pa = None
            raise RuntimeError(f"마이크를 못 열었다: {e}\n--device 를 빼고 다시 해봐라.")

        self._thread = threading.Thread(target=self._loop, name="mic", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        acc = bytearray()
        first = True
        while not self._stop.is_set():
            try:
                pcm = self._stream.read(FRAMES_PER_BUFFER, exception_on_overflow=False)
            except Exception:
                if not self._stop.is_set():
                    print("마이크 read 실패", file=sys.stderr)
                    self._stop.set()
                return

            if first:
                print(f"첫 read: {len(pcm)}바이트 (기대 {FRAME_BYTES})", file=sys.stderr)
                first = False
            if not pcm:
                continue

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
    """게이트가 열린 뒤 발화 하나를 모아서 PCM 으로 돌려준다."""

    def __init__(self, mic: MicStream, cfg: SegmentConfig):
        self.mic = mic
        self.cfg = cfg
        self.vad = webrtcvad.Vad(cfg.vad_mode)
        self.reason = "unknown"
        self.speech_end_at = 0.0
        self.speech_frames = 0
        self.false_starts = 0

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

            if silence >= end_frames or len(chunks) >= max_frames:
                self.speech_end_at = time.monotonic()
                self.speech_frames = voiced

                if voiced < min_frames:
                    # 기침·클릭 같은 짧은 잡음에 VAD 가 반응한 것.
                    # 여기서 return 하면 손님이 말을 꺼내기도 전에 세션이 죽는다.
                    # 모은 걸 버리고 다시 대기 상태로 돌아간다.
                    self.false_starts += 1
                    chunks.clear()
                    pre.clear()
                    started = False
                    silence = 0
                    voiced = 0
                    # 잡음에 반응하느라 소모된 시간만큼 대기 시간을 돌려준다
                    deadline = time.monotonic() + self.cfg.start_timeout_ms / 1000
                    continue

                self.reason = "local_silence" if silence >= end_frames else "max_length"
                return b"".join(chunks)

        self.reason = "cancelled"
        return None


# ------------------------------------------------------------------ 모델
class QwenAsr:
    def __init__(self, model_id: str, device: str = "cuda", dtype: str = "auto",
                 language: str | None = "ko", max_new_tokens: int = 256):
        try:
            import torch
            from transformers import AutoProcessor, AutoModelForMultimodalLM
        except ImportError as e:
            sys.exit(f"의존성이 없다: {e}\n"
                     f"  pip install 'transformers>=5.13.0' torch soundfile librosa accelerate")

        self.torch = torch
        self.language = language
        self.max_new_tokens = max_new_tokens
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

    def transcribe(self, pcm: bytes) -> str:
        """
        PCM 을 파일로 떨구지 않고 메모리에서 바로 넘긴다.
        디스크 왕복은 수십 ms 지만 실시간에선 그것도 아깝다.
        """
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

        inputs = self.processor.apply_transcription_request(
            audio=audio, **({"language": self.language} if self.language else {}))
        inputs = inputs.to(self.model.device, self.model.dtype)

        with self.torch.inference_mode():
            out = self.model.generate(**inputs, max_new_tokens=self.max_new_tokens)

        gen = out[:, inputs["input_ids"].shape[1]:]
        return (self.processor.decode(gen, return_format="transcription_only")[0] or "").strip()

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

    g = p.add_argument_group("모델")
    g.add_argument("--model", default="Qwen/Qwen3-ASR-1.7B-hf")
    g.add_argument("--dtype", default="auto", choices=["auto", "fp16", "bf16", "fp32"])
    g.add_argument("--language", default="ko")
    g.add_argument("--max-new-tokens", type=int, default=256)
    g.add_argument("--warmup", type=int, default=2, help="0이면 끔")

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

    asr = QwenAsr(args.model, "cuda", args.dtype, args.language or None, args.max_new_tokens)
    asr.warmup(args.warmup)

    mic = MicStream(device_index=args.device)
    try:
        mic.start()
    except RuntimeError as e:
        sys.exit(str(e))

    record_dir = Path(args.record).expanduser() if args.record else None
    print(f"\n분절: end_silence={cfg.end_silence_ms}ms vad={cfg.vad_mode} "
          f"pre_roll={cfg.pre_roll_ms}ms", file=sys.stderr)
    print("Enter = 센서 트리거, Ctrl+C = 종료\n", file=sys.stderr)

    stats: list[LiveResult] = []
    try:
        while True:
            try:
                input(">>> Enter 를 누르고 말하세요: ")
            except EOFError:
                break

            seg = Segmenter(mic, cfg)
            cancel = threading.Event()

            mic.open_gate()
            pcm = seg.collect(cancel)
            mic.close_gate()

            if pcm is None:
                print(f"  [빈 결과] 종료={seg.reason}\n")
                continue

            r = LiveResult(pcm=pcm, reason=seg.reason, speech_end_at=seg.speech_end_at)
            r.false_starts = seg.false_starts
            r.audio_sec = len(pcm) / 2 / SAMPLE_RATE
            r.speech_sec = seg.speech_frames * FRAME_MS / 1000

            t0 = time.perf_counter()
            try:
                r.text = asr.transcribe(pcm)
            except Exception as e:
                print(f"  [추론 실패] {e}\n")
                continue
            r.infer_ms = (time.perf_counter() - t0) * 1000
            # 손님이 체감하는 지연: 말이 끝난 순간부터 텍스트가 나올 때까지.
            # end_silence 가 여기 포함된다. 이게 줄일 수 있는 전부다.
            r.total_ms = (time.monotonic() - r.speech_end_at) * 1000 + cfg.end_silence_ms

            print(f"  결과: {r.text or '(없음)'}")
            print(f"  종료={r.reason} 오디오={r.audio_sec:.2f}s 발화={r.speech_sec:.2f}s"
                  + (f" 헛트리거={r.false_starts}회" if r.false_starts else ""))
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
            print(f"\n발화 {len(stats)}개")
            print(f"  추론   중앙값 {sorted(inf)[len(inf) // 2]:.0f}ms  "
                  f"최대 {max(inf):.0f}ms")
            print(f"  체감   중앙값 {sorted(tot)[len(tot) // 2]:.0f}ms  "
                  f"최대 {max(tot):.0f}ms")
            if rtf:
                print(f"  RTF    중앙값 {sorted(rtf)[len(rtf) // 2]:.3f}")
        if mic.dropped:
            print(f"  폐기된 프레임 {mic.dropped}")


if __name__ == "__main__":
    main()