#!/usr/bin/env python3
from __future__ import annotations

import audioop
import queue
import sys
import threading
from pathlib import Path

import pyaudio

REPO_ROOT = Path(__file__).resolve().parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from stt.qwen_live_ver_3 import (
    MicStream,
    SegmentConfig,
    Segmenter,
    QwenAsr,
    SpeechVerifier,
    SAMPLE_RATE,
    CHANNELS,
    FRAME_MS,
    FRAME_BYTES,
    AUDIO_FORMAT,
)

from stt_guard import STTResult


class ResamplingMicStream(MicStream):
    """
    마이크가 16 kHz를 직접 지원하지 않아도
    장치의 native sample rate로 캡처한 뒤
    16 kHz PCM으로 변환해서 기존 Segmenter에 전달한다.
    """

    def start(self) -> None:
        self._pa = pyaudio.PyAudio()

        try:
            if self._device_index is None:
                info = self._pa.get_default_input_device_info()
                self._device_index = int(info["index"])
            else:
                info = self._pa.get_device_info_by_index(
                    self._device_index
                )
        except Exception as e:
            self._pa.terminate()
            self._pa = None
            raise RuntimeError(
                f"마이크 장치 정보를 읽지 못했다: {e}"
            )

        if int(info.get("maxInputChannels", 0)) < CHANNELS:
            self._pa.terminate()
            self._pa = None
            raise RuntimeError(
                f"장치 {self._device_index} "
                f"({info['name']}) 는 입력 장치가 아니다."
            )

        self._capture_rate = int(
            round(float(info["defaultSampleRate"]))
        )

        self._capture_frames = (
            self._capture_rate * FRAME_MS // 1000
        )

        print(
            f"마이크: [{self._device_index}] {info['name']}",
            file=sys.stderr,
        )

        print(
            f"캡처: {self._capture_rate} Hz "
            f"-> STT/VAD: {SAMPLE_RATE} Hz",
            file=sys.stderr,
        )

        try:
            self._stream = self._pa.open(
                format=AUDIO_FORMAT,
                channels=CHANNELS,
                rate=self._capture_rate,
                input=True,
                input_device_index=self._device_index,
                frames_per_buffer=self._capture_frames,
            )

        except OSError as e:
            self._pa.terminate()
            self._pa = None
            raise RuntimeError(
                f"마이크를 못 열었다: {e}"
            )

        self._thread = threading.Thread(
            target=self._loop,
            name="mic-resample",
            daemon=True,
        )

        self._thread.start()

    def _loop(self) -> None:
        acc = bytearray()
        first = True
        rate_state = None

        while not self._stop.is_set():

            try:
                pcm = self._stream.read(
                    self._capture_frames,
                    exception_on_overflow=False,
                )

            except Exception as e:
                if not self._stop.is_set():
                    print(
                        f"마이크 read 실패: {e}",
                        file=sys.stderr,
                    )
                    self._stop.set()
                return

            if not pcm:
                continue

            if self._capture_rate != SAMPLE_RATE:
                pcm, rate_state = audioop.ratecv(
                    pcm,
                    2,                  # int16 = 2 bytes
                    CHANNELS,
                    self._capture_rate,
                    SAMPLE_RATE,
                    rate_state,
                )

            if first:
                print(
                    f"리샘플 후 첫 chunk: "
                    f"{len(pcm)} bytes",
                    file=sys.stderr,
                )
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

        self.mic = ResamplingMicStream(
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

        self.verifier = None
        try:
            self.verifier = SpeechVerifier(0.5, 120)
        except Exception as e:
            print(f"★Silero 없이 진행: {e}", file=sys.stderr)


    def transcribe_once(self) -> STTResult:
        with self._lock:
            if self._closed:
                return STTResult(
                    ok=False,
                    error_code="STT_CLOSED",
                    error_detail="Qwen STT adapter is closed.",
                )

            segmenter = Segmenter(self.mic, self.segment_config, self.verifier)

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
