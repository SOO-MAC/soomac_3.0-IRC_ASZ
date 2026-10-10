#!/usr/bin/env python3
from __future__ import annotations

import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from stt.qwen_live_ver_3 import (   # noqa: E402
    MicStream,
    SegmentConfig,
    Segmenter,
    QwenAsr,
    SpeechVerifier,
)

from stt_guard import STTResult     # noqa: E402


class QwenSTTAdapter:
    def __init__(
        self,
        *,
        model_id="Qwen/Qwen3-ASR-1.7B-hf",
        device="cuda",
        dtype="auto",
        language="ko",
        max_new_tokens=256,
        warmup=2,
        device_index=None,
        vad_mode=2,
        pre_roll_ms=300,
        end_silence_ms=600,
        min_speech_ms=200,
        max_utterance_ms=20_000,
        start_timeout_ms=6_000,
        verify_speech=True,
        silero_threshold=0.5,
        silero_min_speech_ms=120,
    ):
        self._closed = False
        self._lock = threading.Lock()

        self.asr = QwenAsr(
            model_id=model_id,
            device=device,
            dtype=dtype,
            language=language,
            max_new_tokens=max_new_tokens,
        )

        self.asr.warmup(warmup)

        # 16kHz 직접 열기를 먼저 시도하고, 안 되면 soxr 로 리샘플링한다.
        self.mic = MicStream(device_index=device_index)
        self.mic.start()

        self.segment_config = SegmentConfig(
            vad_mode=vad_mode,
            pre_roll_ms=pre_roll_ms,
            end_silence_ms=end_silence_ms,
            max_utterance_ms=max_utterance_ms,
            start_timeout_ms=start_timeout_ms,
            min_speech_ms=min_speech_ms,
        )

        self.verifier = None
        if verify_speech:
            try:
                self.verifier = SpeechVerifier(
                    silero_threshold,
                    silero_min_speech_ms,
                )
            except Exception as e:
                # 검증이 없어도 STT 자체는 돌아야 한다. 방어선 하나가 빠질 뿐이다.
                print(f"★Silero 없이 진행: {e}", file=sys.stderr)

    def transcribe_once(self, cancel: threading.Event | None = None) -> STTResult:
        """
        cancel 을 주면 수음 도중에 끊을 수 있다.

        노드는 TTS 가 말하기 시작한 순간 이 이벤트를 세운다. 이미 시작된 수음을
        끝까지 끌고 가면 로봇 목소리가 그대로 들어오기 때문이다.
        끊긴 경우 Segmenter 가 None 을 돌려주고 reason 은 "cancelled" 가 된다.
        """
        with self._lock:
            if self._closed:
                return STTResult(
                    ok=False,
                    error_code="STT_CLOSED",
                    error_detail="Qwen STT adapter is closed.",
                )

            segmenter = Segmenter(
                self.mic,
                self.segment_config,
                self.verifier,
            )

            cancel_event = cancel if cancel is not None else threading.Event()

            try:
                self.mic.open_gate()
                pcm = segmenter.collect(cancel_event)

            except Exception as e:
                return STTResult(
                    ok=False,
                    error_code=type(e).__name__,
                    error_detail=str(e),
                )

            finally:
                self.mic.close_gate()

            if pcm is None:
                # 잡음은 Segmenter 가 이미 걸렀다. 여기 오는 건 무발화·취소뿐이다.
                return STTResult(
                    transcript=None,
                    ok=True,
                    is_final=True,
                    error_detail=segmenter.reason,
                )

            # 수음은 끝났지만 그 사이 TTS 가 시작됐다면 로봇 목소리가 섞여 있다.
            # 추론 비용을 쓰기 전에 버린다.
            if cancel_event.is_set():
                return STTResult(
                    transcript=None,
                    ok=True,
                    is_final=True,
                    error_detail="cancelled",
                )

            try:
                text = self.asr.transcribe(pcm)

            except Exception as e:
                return STTResult(
                    ok=False,
                    error_code=type(e).__name__,
                    error_detail=str(e),
                )

            return STTResult(
                transcript=text,
                ok=True,
                is_final=True,
                confidence=None,
            )

    def close(self):
        with self._lock:
            if self._closed:
                return

            self._closed = True

            try:
                self.mic.close_gate()
            except Exception:
                pass

            try:
                self.mic.stop()
            except Exception:
                pass