#!/usr/bin/env python3
"""
qwen_stt_adapter.py - STT 계층과 ROS 사이를 잇는 얇은 층

ResamplingMicStream 을 걷어냈다. 이유:
  1) qwen_live 의 MicStream 이 리샘플링을 이미 내장한다. 중복이었다.
  2) MicStream 은 16kHz 직접 열기를 먼저 시도한다. 성공하면 리샘플링 자체가 없다.
     ResamplingMicStream 은 무조건 장치 기본 레이트(48kHz)로 열고 내렸다.
  3) audioop.ratecv 는 안티앨리어싱 필터가 없는 선형 보간이다.
     48k -> 16k 로 내릴 때 8~24kHz 성분이 0~8kHz 로 접혀 들어온다(앨리어싱).
     자음의 파열음·마찰음처럼 고주파가 몰린 소리가 가장 크게 망가지고,
     빠르게 말할수록 그 비중이 커진다. 실측에서 빠른 발화가 뭉개졌다.
     MicStream 은 soxr(HQ, 안티앨리어싱 포함)를 쓴다.

Silero 잡음 검증은 Segmenter 에 넘겨서 유지한다. 할루시네이션 대책은 이쪽이고
정확도 저하는 마이크 경로 문제라 서로 독립이다.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

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

    def transcribe_once(self) -> STTResult:
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

            cancel_event = threading.Event()

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