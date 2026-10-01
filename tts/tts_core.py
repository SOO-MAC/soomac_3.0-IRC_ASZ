#!/usr/bin/env python3
"""
tts_core.py - SOOMAC TTS 핵심부 (ROS 비의존)

    엔진      SupertonicEngine (CPU, ONNX Runtime) / DummyEngine (테스트)
    캐시      PhraseCache      문장 단위 메모리 LRU + 디스크 wav
    출력      PyAudioPlayer / NullPlayer
    파이프라인 TTSPipeline     합성 스레드 + 재생 스레드, 이벤트 콜백

ROS 노드(tts_ros_node.py)는 이 모듈을 감싸기만 한다. 로직 테스트와 음색 비교는
ROS 없이 이 파일 단독으로 한다.

    python3 tts_core.py --list-devices               # 스피커 장치 번호 확인
    python3 tts_core.py --voices
    python3 tts_core.py --say "음료는 무엇으로 드릴까요?" --voice F1
    python3 tts_core.py --compare-voices "주문 금액은 12,300원입니다." --out-dir voice_cmp
    python3 tts_core.py --bench                      # 고정 문구 합성 속도(RTF) 측정

이벤트 (on_event 콜백으로 dict 전달, t 는 time.monotonic())
    accepted  {id, topic, text, t}
    started   {id, t, duration_s, expected_end_t, synth_ms, cached}   첫 샘플 쓰기 직전
    progress  {id, t, index, total, duration_s}                        prefetch_all=False 일 때만
    done      {id, t, audio_s, total_ms}                              출력 지연까지 포함한 실제 종료
    stopped   {id, t}
    error     {id, t, detail}

prefetch_all=True(기본)면 발화 전체를 합성한 뒤 재생한다. started 에 전체 길이가 실리므로
메인 노드가 종료 시각을 미리 알 수 있다. Supertonic 은 빠르고 대부분 캐시 적중이라
이 방식의 추가 지연은 작다. 캐시에 없는 긴 문장이 많아지면 False 로 바꿔
첫 문장부터 재생하게 할 수 있다(대신 종료 시각은 done 에서야 확정).
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import itertools
import os
import queue
import sys
import threading
import time
import wave
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from tts_text import prepare


def log(msg: str) -> None:
    print(f"[TTS] {msg}", file=sys.stderr, flush=True)


# ============================================================
# 미리 합성해둘 고정 문구 (drive_thru_app / order_runtime 응답 기준)
# 문장 단위로 캐시되므로 여러 문장짜리 문구는 문장별로 들어간다.
# ============================================================

PRECACHE_PHRASES = (
    "안녕하세요. 주문을 말씀해주세요.",
    "주문을 다시 말씀해주세요.",
    "추가 주문이 있으시면 말씀해주세요. 주문을 마치시려면 마무리한다고 말씀해주세요.",
    "단품과 세트 중 어떤 것으로 드릴까요?",
    "음료는 무엇으로 드릴까요?",
    "음료 사이즈는 스몰, 미디엄, 라지 중 어떤 것으로 드릴까요?",
    "사이드는 감자튀김과 치즈스틱 중 어떤 것으로 드릴까요?",
    "필요한 옵션을 말씀해주세요.",
    "주문 내용을 정확히 말씀해주세요.",
    "단품 또는 세트 중에서 선택해주세요.",
    "음료를 선택해주세요.",
    "음료 사이즈를 선택해주세요.",
    "사이드 메뉴를 선택해주세요.",
    "주문 내용을 다시 말씀해주세요.",
    "잘 듣지 못했습니다. 다시 말씀해주세요.",
    "잘 듣지 못했습니다. 주문을 다시 말씀해주세요.",
    "주문 내용을 조금 짧게 나누어서 말씀해주세요.",
    "맥오더 주문번호가 맞는지 말씀해주세요.",
    "어떤 햄버거를 주문하시겠어요?",
    "어떤 음료를 주문하시겠어요?",
    "어떤 사이드 메뉴를 주문하시겠어요?",
    "주문 내용을 잘 듣지 못했습니다. 메뉴와 주문 내용을 다시 말씀해주세요.",
    "주문 처리 중 문제가 발생했습니다. 다시 말씀해주세요.",
    "주문 내용을 정확히 이해하지 못했습니다. 다시 말씀해주세요.",
    "주문 해석이 정확하지 않았습니다. 주문 내용을 다시 말씀해주세요.",
    "어떤 버거를 주문하시는지 다시 말씀해주세요.",
    "메뉴를 정확히 확인하지 못했습니다. 주문하실 버거 메뉴를 다시 말씀해주세요.",
    "해당 메뉴를 정확히 확인하지 못했습니다. 주문 가능한 버거 메뉴를 다시 말씀해주세요.",
    "주문이 완료되었습니다. 앞으로 이동해주세요.",
    "주문 내용을 다시 확인해주세요.",
    "주문을 취소했습니다. 앞으로 이동해주세요.",
    "죄송합니다. 주문 내용을 다시 말씀해주세요.",
    "맥오더 주문번호를 말씀해주세요.",
)


# ============================================================
# 오디오 유틸
# ============================================================

def resample(audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr or audio.size == 0:
        return audio
    try:
        import soxr
        return soxr.resample(audio, src_sr, dst_sr, quality="HQ").astype(np.float32)
    except ImportError:
        n = int(round(audio.size * dst_sr / src_sr))
        x_old = np.linspace(0.0, 1.0, audio.size, endpoint=False)
        x_new = np.linspace(0.0, 1.0, n, endpoint=False)
        return np.interp(x_new, x_old, audio).astype(np.float32)


def trim_silence(audio: np.ndarray, sr: int, threshold: float = 0.01,
                 keep_ms: int = 40) -> np.ndarray:
    """앞뒤 무음 제거. 문장 사이 간격은 파이프라인이 일정하게 넣는다."""
    idx = np.flatnonzero(np.abs(audio) > threshold)
    if idx.size == 0:
        return audio
    keep = int(sr * keep_ms / 1000)
    return audio[max(0, idx[0] - keep): min(audio.size, idx[-1] + keep)]


# ============================================================
# ENGINE
# ============================================================

class SupertonicEngine:
    """
    supertonic 파이썬 SDK (1.3.x, supertonic-3 모델) 래퍼.

    - CPU 전용(ONNX Runtime). GPU 예산에 들어가지 않는다.
    - 스레드 수를 제한한다. 합성이 코어를 다 쓰면 STT 마이크 스레드가 밀릴 수 있다.
    - 초기 노이즈가 np.random 전역 난수라서, 문장마다 텍스트 기반 시드를 건다.
      같은 문장은 몇 번을 합성해도 같은 소리가 난다(재현성, 캐시 재생성 시 일관성).
    - 지원하지 않는 문자는 SDK 가 예외를 내므로 합성 전에 한 번 더 거른다.
    """

    def __init__(self, voice: str = "F1", total_steps: int = 8, speed: float = 1.05,
                 lang: str = "ko", seed: int = 1234, threads: int | None = 2,
                 model_dir: str | None = None, auto_download: bool = True,
                 silence_between_chunks: float = 0.15):
        from supertonic import TTS

        t0 = time.perf_counter()
        kwargs = {"auto_download": auto_download}
        if model_dir:
            kwargs["model_dir"] = model_dir
        if threads:
            kwargs["intra_op_num_threads"] = int(threads)
            kwargs["inter_op_num_threads"] = 1
        self.tts = TTS(**kwargs)

        self.voice = voice
        self.style = self.tts.get_voice_style(voice_name=voice)
        self.sample_rate = int(self.tts.sample_rate)
        self.total_steps = int(total_steps)
        self.speed = float(speed)
        self.lang = lang
        self.seed = int(seed)
        self.silence_between_chunks = float(silence_between_chunks)
        self.model_dir = str(self.tts.model_dir)
        log(f"Supertonic 로딩 {time.perf_counter() - t0:.1f}s  voice={voice} "
            f"steps={total_steps} speed={speed} sr={self.sample_rate} "
            f"threads={threads} model_dir={self.model_dir}")

    @property
    def voices(self) -> list[str]:
        return list(getattr(self.tts, "voice_style_names", []))

    @property
    def key(self) -> str:
        return (f"supertonic|{Path(self.model_dir).name}|{self.voice}|{self.total_steps}|"
                f"{self.speed}|{self.lang}|{self.seed}")

    def _sanitize(self, text: str) -> str:
        validate = getattr(getattr(self.tts.model, "text_processor", None), "validate_text", None)
        if validate is None:
            return text
        ok, unsupported = validate(text)
        if ok:
            return text
        log(f"모델 미지원 문자 제거 {unsupported}: {text}")
        bad = set(unsupported)
        return " ".join("".join(" " if ch in bad else ch for ch in text).split())

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        text = self._sanitize(text)
        if not text:
            raise ValueError("합성할 문자가 남지 않았습니다.")
        np.random.seed((self.seed + zlib.crc32(text.encode("utf-8"))) & 0xFFFFFFFF)
        wav, _ = self.tts.synthesize(
            text,
            voice_style=self.style,
            total_steps=self.total_steps,
            speed=self.speed,
            lang=self.lang,
            silence_duration=self.silence_between_chunks,
        )
        return np.asarray(wav, dtype=np.float32).reshape(-1), self.sample_rate


class DummyEngine:
    """모델 없이 파이프라인·타이밍을 확인하는 용도. 글자당 70ms 톤."""

    def __init__(self, sample_rate: int = 44_100, synth_delay: float = 0.03):
        self.sample_rate = sample_rate
        self.synth_delay = synth_delay
        self.voices = ["dummy"]

    @property
    def key(self) -> str:
        return f"dummy|{self.sample_rate}"

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        time.sleep(self.synth_delay)
        n = int(self.sample_rate * 0.07 * max(1, len(text)))
        t = np.arange(n, dtype=np.float32) / self.sample_rate
        return (0.05 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), self.sample_rate


# ============================================================
# CACHE
# ============================================================

class PhraseCache:
    """문장 -> 오디오. 키에 엔진 설정이 들어가므로 음색·스텝을 바꾸면 자동으로 새로 만든다."""

    def __init__(self, engine_key: str, cache_dir: str | Path,
                 max_mem_items: int = 256, max_disk_items: int = 3000):
        self.engine_key = engine_key
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_mem = max_mem_items
        self.max_disk = max_disk_items
        self._mem: collections.OrderedDict[str, tuple[np.ndarray, int]] = collections.OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def _key(self, text: str) -> str:
        return hashlib.sha1(f"{self.engine_key}\n{text}".encode("utf-8")).hexdigest()[:24]

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.wav"

    def contains(self, text: str) -> bool:
        key = self._key(text)
        return key in self._mem or self._path(key).exists()

    def get(self, text: str) -> tuple[np.ndarray, int] | None:
        key = self._key(text)
        with self._lock:
            if key in self._mem:
                self._mem.move_to_end(key)
                self.hits += 1
                return self._mem[key]

        path = self._path(key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            with wave.open(str(path), "rb") as wf:
                sr = wf.getframerate()
                pcm = wf.readframes(wf.getnframes())
            audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            os.utime(path, None)
        except Exception as e:
            log(f"캐시 파일 손상, 다시 합성: {path.name} ({e})")
            path.unlink(missing_ok=True)
            self.misses += 1
            return None

        self._remember(key, audio, sr)
        self.hits += 1
        return audio, sr

    def put(self, text: str, audio: np.ndarray, sr: int) -> None:
        key = self._key(text)
        self._remember(key, audio, sr)
        path = self._path(key)
        tmp = path.with_suffix(".wav.tmp")
        pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
        try:
            with wave.open(str(tmp), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(pcm)
            tmp.replace(path)
        except Exception as e:
            log(f"캐시 저장 실패(무시): {e}")
            tmp.unlink(missing_ok=True)
            return
        self._prune_disk()

    def _remember(self, key, audio, sr):
        with self._lock:
            self._mem[key] = (audio, sr)
            self._mem.move_to_end(key)
            while len(self._mem) > self.max_mem:
                self._mem.popitem(last=False)

    def _prune_disk(self):
        files = list(self.dir.glob("*.wav"))
        if len(files) <= self.max_disk:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for p in files[: len(files) - self.max_disk]:
            p.unlink(missing_ok=True)


# ============================================================
# PLAYER
# ============================================================

class PyAudioPlayer:
    """출력 스트림을 계속 열어둔다. 20ms 단위로 써서 중단에 즉시 반응한다."""

    def __init__(self, sample_rate: int, device_index: int | None = None, chunk_ms: int = 20):
        import pyaudio
        self._pa = pyaudio.PyAudio()
        if device_index is None:
            info = self._pa.get_default_output_device_info()
            device_index = int(info["index"])
        else:
            info = self._pa.get_device_info_by_index(device_index)
        log(f"스피커: [{device_index}] {info['name']}")

        self.rate = sample_rate
        try:
            self._stream = self._pa.open(format=pyaudio.paInt16, channels=1, rate=self.rate,
                                         output=True, output_device_index=device_index)
        except OSError:
            self.rate = int(round(float(info["defaultSampleRate"])))
            self._stream = self._pa.open(format=pyaudio.paInt16, channels=1, rate=self.rate,
                                         output=True, output_device_index=device_index)
        log(f"출력 {self.rate} Hz (모델 {sample_rate} Hz)")
        self.chunk = max(1, self.rate * chunk_ms // 1000)

    def latency(self) -> float:
        try:
            return min(max(float(self._stream.get_output_latency()), 0.0), 0.5)
        except Exception:
            return 0.1

    def play(self, audio: np.ndarray, should_stop) -> bool:
        pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
        for start in range(0, pcm.size, self.chunk):
            if should_stop():
                tail = pcm[start: start + self.rate // 100].astype(np.float32)
                if tail.size:       # 10ms 페이드아웃으로 클릭음 방지
                    tail *= np.linspace(1.0, 0.0, tail.size, dtype=np.float32)
                    self._stream.write(tail.astype(np.int16).tobytes())
                return False
            self._stream.write(pcm[start: start + self.chunk].tobytes())
        return True

    def drain(self) -> None:
        time.sleep(self.latency())

    def close(self) -> None:
        try:
            self._stream.stop_stream()
            self._stream.close()
        finally:
            self._pa.terminate()


class NullPlayer:
    """스피커 없이 재생 시간만큼 기다린다."""

    def __init__(self, sample_rate: int, speed: float = 1.0):
        self.rate = sample_rate
        self.speed = speed

    def latency(self) -> float:
        return 0.0

    def play(self, audio: np.ndarray, should_stop) -> bool:
        remaining = audio.size / self.rate / self.speed
        while remaining > 0:
            if should_stop():
                return False
            step = min(0.02, remaining)
            time.sleep(step)
            remaining -= step
        return True

    def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


# ============================================================
# PIPELINE
# ============================================================

@dataclass
class Job:
    id: object
    text: str
    topic: str | None
    generation: int
    received_at: float = field(default_factory=time.monotonic)
    started: bool = False
    finished: bool = False
    audio_s: float = 0.0


class TTSPipeline:
    def __init__(self, engine, player, cache: PhraseCache, *, on_event=None,
                 lexicon: dict[str, str] | None = None, prefetch_all: bool = True,
                 lead_silence_ms: int = 80, sentence_gap_ms: int = 180, volume: float = 1.0):
        self.engine = engine
        self.player = player
        self.cache = cache
        self.on_event = on_event or (lambda ev: None)
        self.lexicon = lexicon
        self.prefetch_all = prefetch_all
        self.volume = float(volume)
        self.lead_silence = np.zeros(player.rate * lead_silence_ms // 1000, dtype=np.float32)
        self.sentence_gap = np.zeros(player.rate * sentence_gap_ms // 1000, dtype=np.float32)

        self._jobs: queue.Queue[Job] = queue.Queue()
        self._play_q: queue.Queue = queue.Queue(maxsize=3)
        self._gen = 0
        self._gen_lock = threading.Lock()
        self._active = 0
        self._active_lock = threading.Lock()
        self._auto_ids = itertools.count(1)
        self._closed = threading.Event()
        self._threads = [
            threading.Thread(target=self._synth_loop, name="tts-synth", daemon=True),
            threading.Thread(target=self._play_loop, name="tts-play", daemon=True),
        ]
        for t in self._threads:
            t.start()

    # --------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self._active > 0

    @property
    def pending(self) -> int:
        """수신했지만 아직 끝나지 않은(합성·재생 중 포함) 발화 수"""
        return self._active

    def _emit(self, event: str, job: Job | None = None, **fields) -> None:
        ev = {"event": event, "t": time.monotonic(), **fields}
        if job is not None:
            ev["id"] = job.id
            if job.topic:
                ev["topic"] = job.topic
        try:
            self.on_event(ev)
        except Exception as e:
            log(f"on_event 오류: {type(e).__name__}: {e}")

    def _stale(self, job: Job) -> bool:
        return job.generation != self._gen

    def _finish(self, job: Job, kind: str, **fields) -> None:
        # stop() 과 재생 스레드가 같은 발화를 동시에 끝낼 수 있다. 확인·표시를 한 번에 한다.
        with self._active_lock:
            if job.finished:
                return
            job.finished = True
            self._active -= 1
        self._emit(kind, job, **fields)

    # --------------------------------------------------------
    def submit(self, text: str, *, job_id=None, topic: str | None = None,
               interrupt: bool = False):
        text = str(text or "").strip()
        if not text:
            return None
        if interrupt:
            self.stop()
        if job_id is None:
            job_id = next(self._auto_ids)
        job = Job(id=job_id, text=text, topic=topic, generation=self._gen)
        with self._active_lock:
            self._active += 1
        self._emit("accepted", job, text=text)
        self._jobs.put(job)
        return job_id

    def stop(self) -> int:
        """재생 중·대기 중 발화를 모두 중단한다. 폐기한 대기 건수를 돌려준다."""
        with self._gen_lock:
            self._gen += 1
        dropped = 0
        for q in (self._jobs, self._play_q):
            while True:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
                job = item if isinstance(item, Job) else item[1]
                self._finish(job, "stopped")
                dropped += 1
        return dropped

    # --------------------------------------------------------
    def synthesize_sentence(self, sentence: str) -> tuple[np.ndarray, bool, float]:
        """(출력 레이트 오디오, 캐시 적중 여부, 합성 ms)"""
        cached = self.cache.get(sentence)
        if cached is not None:
            audio, sr = cached
            return resample(audio, sr, self.player.rate), True, 0.0

        t0 = time.perf_counter()
        audio, sr = self.engine.synthesize(sentence)
        audio = trim_silence(audio, sr)
        self.cache.put(sentence, audio, sr)
        ms = (time.perf_counter() - t0) * 1000
        log(f"합성 {ms:.0f}ms ({audio.size / sr:.2f}s, RTF {ms / 1000 / max(audio.size / sr, 1e-6):.3f}): {sentence}")
        return resample(audio, sr, self.player.rate), False, ms

    def precache(self, phrases) -> None:
        sentences = []
        for p in phrases:
            for s in prepare(p, self.lexicon):
                if s not in sentences and not self.cache.contains(s):
                    sentences.append(s)
        if not sentences:
            log("고정 문구 캐시 완료 상태")
            return
        log(f"고정 문구 {len(sentences)}문장 합성 시작")
        t0 = time.perf_counter()
        for s in sentences:
            try:
                self.synthesize_sentence(s)
            except Exception as e:
                log(f"사전 합성 실패(무시): {s} ({e})")
        log(f"고정 문구 합성 완료 {time.perf_counter() - t0:.1f}s")

    # --------------------------------------------------------
    def _synth_loop(self) -> None:
        while not self._closed.is_set():
            try:
                job = self._jobs.get(timeout=0.2)
            except queue.Empty:
                continue

            if self._stale(job):
                self._finish(job, "stopped")
                continue

            removed: list[str] = []
            sentences = prepare(job.text, self.lexicon, removed_out=removed)
            if removed:
                log(f"#{job.id} 화이트리스트 제거 {sorted(set(removed))}")
            if not sentences:
                self._finish(job, "done", audio_s=0.0, total_ms=0)
                continue

            try:
                if self.prefetch_all:
                    parts, all_cached, synth_ms = [], True, 0.0
                    for i, s in enumerate(sentences):
                        audio, cached, ms = self.synthesize_sentence(s)
                        if self._stale(job):
                            break
                        parts.append(self.lead_silence if i == 0 else self.sentence_gap)
                        parts.append(audio)
                        all_cached &= cached
                        synth_ms += ms
                    else:
                        self._play_q.put(("audio", job, 0, 1, np.concatenate(parts),
                                          {"synth_ms": round(synth_ms), "cached": all_cached}))
                else:
                    for i, s in enumerate(sentences):
                        audio, cached, ms = self.synthesize_sentence(s)
                        if self._stale(job):
                            break
                        pad = self.lead_silence if i == 0 else self.sentence_gap
                        self._play_q.put(("audio", job, i, len(sentences),
                                          np.concatenate([pad, audio]),
                                          {"synth_ms": round(ms), "cached": cached}))
                self._play_q.put(("end", job, None))
            except Exception as e:
                log(f"합성 실패 #{job.id}: {type(e).__name__}: {e}")
                self._play_q.put(("end", job, f"{type(e).__name__}: {e}"))

    def _play_loop(self) -> None:
        while not self._closed.is_set():
            try:
                item = self._play_q.get(timeout=0.2)
            except queue.Empty:
                continue

            kind, job = item[0], item[1]
            if self._stale(job):
                self._finish(job, "stopped")
                continue

            if kind == "end":
                if item[2]:
                    self._finish(job, "error", detail=item[2])
                    continue
                self.player.drain()
                if self._stale(job):
                    self._finish(job, "stopped")
                else:
                    self._finish(job, "done", audio_s=round(job.audio_s, 3),
                                 total_ms=int((time.monotonic() - job.received_at) * 1000))
                continue

            _, _, index, total, audio, info = item
            if self.volume != 1.0:
                audio = np.clip(audio * self.volume, -1.0, 1.0)
            duration = audio.size / self.player.rate
            job.audio_s += duration
            now = time.monotonic()

            if not job.started:
                job.started = True
                fields = {"duration_s": round(duration, 3), **info}
                if total == 1:
                    fields["expected_end_t"] = now + duration + self.player.latency()
                self._emit("started", job, **fields)
            else:
                self._emit("progress", job, index=index, total=total,
                           duration_s=round(duration, 3))

            if not self.player.play(audio, lambda: self._stale(job)):
                self._finish(job, "stopped")

    # --------------------------------------------------------
    def close(self) -> None:
        self.stop()
        self._closed.set()
        for t in self._threads:
            t.join(timeout=1.0)
        self.player.close()
        log(f"종료 (캐시 hit {self.cache.hits} / miss {self.cache.misses})")


# ============================================================
# 공용 생성 함수 (ROS 노드와 CLI 가 같이 쓴다)
# ============================================================

def build_engine(kind: str = "supertonic", **kw):
    if kind == "dummy":
        return DummyEngine()
    return SupertonicEngine(**kw)


def build_player(kind: str, sample_rate: int, device_index: int | None = None):
    if kind == "null":
        return NullPlayer(sample_rate)
    return PyAudioPlayer(sample_rate, device_index=device_index)


# ============================================================
# CLI
# ============================================================

def _save_wav(path: Path, audio: np.ndarray, sr: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())


def main() -> None:
    p = argparse.ArgumentParser(description="SOOMAC TTS core (ROS 없이 테스트)")
    p.add_argument("--engine", choices=["supertonic", "dummy"], default="supertonic")
    p.add_argument("--voice", default="F1")
    p.add_argument("--steps", type=int, default=8)
    p.add_argument("--speed", type=float, default=1.05)
    p.add_argument("--threads", type=int, default=2)
    p.add_argument("--model-dir", default=None)
    p.add_argument("--offline", action="store_true", help="모델 자동 다운로드 금지")
    p.add_argument("--player", choices=["pyaudio", "null"], default="pyaudio")
    p.add_argument("--output-device", type=int, default=None)

    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list-devices", action="store_true", help="출력(스피커) 장치 목록")
    g.add_argument("--voices", action="store_true", help="사용 가능한 음색 목록")
    g.add_argument("--say", help="합성해서 재생 (--out 이 있으면 파일로 저장)")
    g.add_argument("--compare-voices", metavar="TEXT", help="모든 음색으로 저장해 비교")
    g.add_argument("--bench", action="store_true", help="고정 문구 합성 속도 측정(캐시 미사용)")
    p.add_argument("--out", default=None)
    p.add_argument("--out-dir", default="voice_compare")
    args = p.parse_args()

    if args.list_devices:
        import pyaudio
        pa = pyaudio.PyAudio()
        try:
            default_out = pa.get_default_output_device_info().get("index")
        except Exception:
            default_out = None
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if int(info.get("maxOutputChannels", 0)) > 0:
                mark = "  [기본]" if i == default_out else ""
                print(f"[{i}] {info['name']} ({int(info['defaultSampleRate'])} Hz){mark}")
        pa.terminate()
        return

    kw = dict(voice=args.voice, total_steps=args.steps, speed=args.speed, threads=args.threads,
              model_dir=args.model_dir, auto_download=not args.offline)
    engine = build_engine(args.engine, **({} if args.engine == "dummy" else kw))

    if args.voices:
        print("\n".join(engine.voices))
        return

    if args.compare_voices:
        for v in engine.voices:
            e = SupertonicEngine(**{**kw, "voice": v})
            audio = np.concatenate([e.synthesize(s)[0] for s in prepare(args.compare_voices)])
            _save_wav(Path(args.out_dir) / f"{v}.wav", audio, e.sample_rate)
            print(f"{v}: {Path(args.out_dir) / f'{v}.wav'}")
        return

    if args.bench:
        sentences = []
        for ph in PRECACHE_PHRASES:
            sentences += [s for s in prepare(ph) if s not in sentences]
        engine.synthesize("안녕하세요.")         # 워밍업
        total_synth = total_audio = 0.0
        for s in sentences:
            t0 = time.perf_counter()
            audio, sr = engine.synthesize(s)
            dt = time.perf_counter() - t0
            total_synth += dt
            total_audio += audio.size / sr
            print(f"{dt * 1000:7.0f}ms  {audio.size / sr:5.2f}s  {s}")
        print(f"\n{len(sentences)}문장  합성 {total_synth:.2f}s / 오디오 {total_audio:.2f}s  "
              f"RTF {total_synth / total_audio:.3f}  (1보다 작을수록 빠름)")
        return

    sentences = prepare(args.say)
    print("모델 입력:", sentences)
    parts = []
    for i, s in enumerate(sentences):
        t0 = time.perf_counter()
        audio, sr = engine.synthesize(s)
        print(f"  {(time.perf_counter() - t0) * 1000:.0f}ms  {audio.size / sr:.2f}s  {s}")
        parts += [np.zeros(int(sr * (0.08 if i == 0 else 0.18)), np.float32), trim_silence(audio, sr)]
    audio = np.concatenate(parts)
    if args.out:
        _save_wav(Path(args.out), audio, engine.sample_rate)
        print("저장:", args.out)
        return
    player = build_player(args.player, engine.sample_rate, args.output_device)
    player.play(resample(audio, engine.sample_rate, player.rate), lambda: False)
    player.drain()
    player.close()


if __name__ == "__main__":
    main()