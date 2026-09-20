#!/usr/bin/env python3
from __future__ import annotations

import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from stt.qwen_live_ver_2 import (
    MicStream,
    SegmentConfig,
    Segmenter,
    QwenAsr,
)

from stt_guard import STTResult


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

        self.mic = MicStream(
            device_index=device_index
        )
        self.mic.start()

        self.segment_config = SegmentConfig(
            vad_mode=vad_mode,
            pre_roll_ms=pre_roll_ms,
            end_silence_ms=end_silence_ms,
            max_utterance_ms=max_utterance_ms,
            start_timeout_ms=start_timeout_ms,
            min_speech_ms=min_speech_ms,
        )

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
