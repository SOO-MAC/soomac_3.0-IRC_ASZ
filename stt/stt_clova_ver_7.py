"""
clova_stt.py - CLOVA Speech 실시간 스트리밍(NEST) STT 클라이언트

ROS 의존성 없음. 단독 실행으로 API 동작을 먼저 확인하고,
ROS 노드는 나중에 이 파일을 import 해서 쓴다.

구조
    MicStream          마이크를 항상 read 한다. 게이트가 닫혀 있으면 큐에 안 넣을 뿐.
                       (안 읽으면 드라이버 버퍼에 로봇 TTS 목소리가 쌓인다)
    UtteranceSource    큐에서 프레임을 꺼내며 끝점을 판정한다. 마지막 프레임에 표시를 붙인다.
    ClovaStreamingStt  gRPC 스트림 하나 = 발화 하나. 채널은 재사용한다.

발화 시작은 이 파일이 결정하지 않는다. 드라이브 스루 센서가 결정한다.
단독 실행 모드에서는 Enter 키가 센서를 흉내낸다.

실행:
    export CLOVA_SPEECH_SECRET_KEY=...
    python3 clova_stt.py
    python3 clova_stt.py --list-devices
    python3 clova_stt.py --device 3 --debug
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import wave
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import grpc
import pyaudio
import webrtcvad

import nest_pb2
import nest_pb2_grpc

log = logging.getLogger("clova_stt")

# ------------------------------------------------------------------ 오디오 상수
# NEST는 16kHz / 1ch / 16bit raw PCM(헤더 없음)만 받는다. 문서에 명시돼 있다.
SAMPLE_RATE = 16_000
CHANNELS = 1
FRAME_MS = 30                                    # webrtcvad는 10/20/30ms만 허용
FRAMES_PER_BUFFER = SAMPLE_RATE * FRAME_MS // 1000   # 480 샘플
FRAME_BYTES = FRAMES_PER_BUFFER * CHANNELS * 2       # 960 바이트 (16bit)
SILENT_FRAME = b"\x00" * FRAME_BYTES                 # 서버 flush 유도용 무음 프레임
AUDIO_FORMAT = pyaudio.paInt16

CLOVA_HOST = "clovaspeech-gw.ncloud.com:50051"
SECRET_ENV_NAME = "CLOVA_SPEECH_SECRET_KEY"
DEFAULT_ENV_FILE = "env.sh"


# ------------------------------------------------------------------ 시크릿 로딩
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def load_env_file(path: Path) -> dict[str, str]:
    """
    `export KEY='value'` 형태 파일을 읽는다. 셸을 실행하지 않는다.
    (파일에 임의 명령을 넣어두고 source 시키는 사고를 막는다)
    """
    values: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return values

    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        values[key] = val

    return values


def resolve_secret(env_file: str | None = None) -> str:
    """
    우선순위: 환경변수 -> env 파일 -> 실패.
    환경변수가 이겨야 systemd / ros2 launch 에서 주입할 때 파일을 안 고쳐도 된다.
    """
    secret = os.environ.get(SECRET_ENV_NAME)
    if secret:
        log.debug("시크릿: 환경변수에서 읽음")
        return secret

    # 실행 위치가 아니라 이 스크립트 옆을 본다.
    # ros2 launch 는 작업 디렉토리를 마음대로 바꾼다.
    candidates = []
    if env_file:
        candidates.append(Path(env_file).expanduser())
    if os.environ.get("CLOVA_ENV_FILE"):
        candidates.append(Path(os.environ["CLOVA_ENV_FILE"]).expanduser())
    candidates.append(Path(__file__).resolve().parent / DEFAULT_ENV_FILE)

    for path in candidates:
        if not path.is_file():
            continue
        try:
            mode = path.stat().st_mode
            if mode & 0o077:
                log.warning("%s 가 남에게도 읽힌다. chmod 600 해라.", path)
        except OSError:
            pass

        secret = load_env_file(path).get(SECRET_ENV_NAME)
        if secret:
            log.info("시크릿: %s 에서 읽음", path)
            return secret
        log.warning("%s 에 %s 가 없다", path, SECRET_ENV_NAME)

    raise RuntimeError(
        f"{SECRET_ENV_NAME} 를 못 찾았다. 셋 중 하나를 해라.\n"
        f"  1) export {SECRET_ENV_NAME}=...\n"
        f"  2) {Path(__file__).resolve().parent / DEFAULT_ENV_FILE} 에 "
        f"export {SECRET_ENV_NAME}='...' 한 줄\n"
        f"  3) CLOVA_ENV_FILE=/경로/env.sh 로 파일 위치 지정"
    )

# 문서상 gRPC Connection Lifetime 제한이 5분이다.
# 유휴 채널을 그 근처까지 끌고 가지 않고 미리 새로 만든다.
CHANNEL_MAX_IDLE_SEC = 200.0


# ------------------------------------------------------------------ 결과 타입
@dataclass
class SttResult:
    text: str = ""
    confidence: float | None = None
    epd_type: str | None = None          # gap | endPoint | durationThreshold | period | ...
    pieces: int = 0
    reason: str = "unknown"              # server_ep | local_silence | no_speech | cancelled | error
    first_partial_ms: float | None = None
    total_ms: float | None = None
    error: str | None = None
    # 음절 단위 (word, confidence). 어느 글자가 문제인지 보려면 이걸 봐야 한다.
    aligns: list[tuple[str, float]] = field(default_factory=list)
    # 서버로 보낸 원본 오디오. 평가셋을 만들 때 저장한다.
    pcm: bytes | None = None

    def weakest(self, n: int = 3) -> list[tuple[str, float]]:
        """신뢰도가 가장 낮은 음절 n개. 부스팅할 단어를 고를 때 쓴다."""
        return sorted((a for a in self.aligns if a[0].strip()), key=lambda a: a[1])[:n]

    def __bool__(self) -> bool:
        return bool(self.text)


# ------------------------------------------------------------------ 텍스트 조립
class TextAssembler:
    """
    NEST 응답은 조각으로 온다. 문서는 text 와 position 으로 full text 를 구성하라고 한다.
    (position 0 에 "ABC", position 3 에 "DEFG" -> "ABCDEFG")

    position 을 무시하고 그냥 이어붙이면 대부분 맞다가 가끔 어긋난다.
    position 이 예상 위치와 다르면 로그를 남겨서, 실제로 문제가 되는지 실측하게 한다.
    """

    def __init__(self):
        self.buf: list[str] = []
        self.gaps = 0

    def add(self, text: str, position: int | None) -> None:
        if not text:
            return

        if position is None or position < 0:
            position = len(self.buf)

        if position != len(self.buf):
            self.gaps += 1
            log.debug("position 불일치: 기대 %d, 수신 %d (조각=%r)", len(self.buf), position, text)

        if position > len(self.buf):
            self.buf.extend(" " * (position - len(self.buf)))

        self.buf[position:position + len(text)] = list(text)

    def text(self) -> str:
        return "".join(self.buf).strip()


# ------------------------------------------------------------------ 마이크
class MicStream:
    """
    마이크를 항상 read 한다. 게이트가 닫혀 있으면 큐에 안 넣고 버린다.
    큐는 반드시 유한해야 한다. 네트워크가 밀리면 무한 큐는 과거 오디오를 무한히 쌓는다.
    """

    def __init__(self, device_index: int | None = None, queue_seconds: float = 6.0):
        maxsize = int(queue_seconds * 1000 / FRAME_MS)
        self.q: queue.Queue[bytes] = queue.Queue(maxsize=maxsize)
        self.dropped = 0

        self._enabled = threading.Event()
        self._stop = threading.Event()
        self._pa: pyaudio.PyAudio | None = None
        self._stream = None
        self._thread: threading.Thread | None = None
        self._device_index = device_index

    def start(self) -> None:
        self._pa = pyaudio.PyAudio()

        # 장치를 지정했으면 열기 전에 검증한다.
        # ALSA 에러 메시지는 원인을 알려주지 않는다.
        if self._device_index is not None:
            try:
                info = self._pa.get_device_info_by_index(self._device_index)
            except Exception:
                self._pa.terminate()
                raise RuntimeError(
                    f"장치 {self._device_index} 가 없다. --list-devices 로 확인해라."
                ) from None

            ch = int(info.get("maxInputChannels", 0))
            if ch < CHANNELS:
                usable = []
                for i in range(self._pa.get_device_count()):
                    d = self._pa.get_device_info_by_index(i)
                    if int(d.get("maxInputChannels", 0)) > 0:
                        usable.append(f"  [{i}] {d['name']}")
                self._pa.terminate()
                raise RuntimeError(
                    f"장치 {self._device_index} ({info['name']}) 는 입력 채널이 {ch}개다. "
                    f"녹음용이 아니다.\n"
                    f"--device 를 빼고 기본 장치를 쓰거나, 아래에서 골라라:\n"
                    + "\n".join(usable or ["  (입력 장치 없음)"])
                )

        # hw:0,0 을 직접 열면 16kHz 를 못 여는 장치가 있다.
        # 기본 플러그인 경로(default/pulse)가 채널·샘플레이트를 변환해준다.
        try:
            self._stream = self._pa.open(
                format=AUDIO_FORMAT, channels=CHANNELS, rate=SAMPLE_RATE, input=True,
                input_device_index=self._device_index, frames_per_buffer=FRAMES_PER_BUFFER,
            )
        except OSError as e:
            self._pa.terminate()
            self._pa = None
            raise RuntimeError(
                f"마이크를 못 열었다: {e}\n"
                f"장치={self._device_index if self._device_index is not None else '기본'}, "
                f"{SAMPLE_RATE}Hz/{CHANNELS}ch/16bit 로 시도했다.\n"
                f"--device 를 빼고 기본 장치로 다시 해봐라."
            ) from None

        self._thread = threading.Thread(target=self._loop, name="mic", daemon=True)
        self._thread.start()
        log.info("마이크 열림 (%dHz/%dch/16bit, 장치=%s)",
                 SAMPLE_RATE, CHANNELS,
                 self._device_index if self._device_index is not None else "기본")

    def _loop(self) -> None:
        # read 가 항상 요청한 만큼 준다는 보장이 없다.
        # 바이트로 누적해서 정확히 FRAME_BYTES 단위로 잘라 낸다.
        # webrtcvad 는 길이가 1바이트만 어긋나도 프레임 전체를 거부한다.
        acc = bytearray()
        first = True
        empty_reads = 0

        while not self._stop.is_set():
            try:
                pcm = self._stream.read(FRAMES_PER_BUFFER, exception_on_overflow=False)
            except Exception:
                if not self._stop.is_set():
                    log.exception("마이크 read 실패")
                    self._stop.set()
                return

            if first:
                log.info("첫 read: %d바이트 (기대 %d)", len(pcm), FRAME_BYTES)
                if len(pcm) != FRAME_BYTES:
                    log.error(
                        "read 크기가 다르다. 장치가 %d채널로 열렸을 수 있다. "
                        "--list-devices 로 다른 장치를 시도해라.",
                        (len(pcm) // 2) // FRAMES_PER_BUFFER if FRAMES_PER_BUFFER else 0,
                    )
                first = False

            if not pcm:
                empty_reads += 1
                if empty_reads in (1, 50, 500):
                    log.error("read 가 빈 데이터를 준다 (%d회). "
                              "입력 소스가 잘못 잡혔는지 pavucontrol 로 확인해라.", empty_reads)
                continue
            empty_reads = 0

            acc.extend(pcm)

            while len(acc) >= FRAME_BYTES:
                frame = bytes(acc[:FRAME_BYTES])
                del acc[:FRAME_BYTES]

                # 게이트가 닫혀 있어도 read 는 계속한다. 큐에 안 넣을 뿐.
                # 안 읽으면 드라이버 버퍼에 TTS 로봇 목소리가 쌓인다.
                if not self._enabled.is_set():
                    continue

                try:
                    self.q.put_nowait(frame)
                except queue.Full:
                    # 가장 오래된 프레임을 버린다. 과거보다 현재가 중요하다.
                    try:
                        self.q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self.q.put_nowait(frame)
                    except queue.Full:
                        pass
                    self.dropped += 1
                    if self.dropped % 50 == 1:
                        log.warning("큐 포화, 오래된 프레임 폐기 (누적 %d)", self.dropped)

    def drain(self) -> None:
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                return

    def open_gate(self) -> None:
        self.drain()          # 게이트 열기 직전 잔향을 버린다
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
                log.exception("마이크 스트림 정리 실패")
            self._stream = None
        if self._pa:
            try:
                self._pa.terminate()
            except Exception:
                log.exception("PyAudio 정리 실패")
            self._pa = None


# ------------------------------------------------------------------ 발화 분절
@dataclass
class UtteranceConfig:
    vad_mode: int = 2                # 0=관대 3=엄격
    pre_roll_ms: int = 300           # VAD가 반응한 시점엔 첫 음절이 이미 지났다
    end_silence_ms: int = 800        # 로컬 끝점 판정 기준
    max_utterance_ms: int = 20_000   # 이보다 길면 강제 종료
    start_timeout_ms: int = 6_000    # 게이트 열고 이 시간 안에 말이 없으면 포기

    # 끝점 판정 후 무음 프레임을 이만큼 더 밀어넣고 스트림을 닫는다.
    #
    # 기본 0 = 사용 안 함. 실측에서 효과가 없었다.
    #   녹음 27개에 600ms 패딩을 붙여 끝무음을 900ms -> 1550ms 로 늘렸지만
    #   잘림은 46% -> 42% 로 거의 그대로였고, rec2 에서 보이던 상관관계도 사라졌다.
    #   (완결 중앙값 1605ms vs 잘림 중앙값 1575ms, 차이 30ms)
    #
    # 결론: 서버가 필요로 하는 건 오디오 양이 아니라 실제 경과 시간이다.
    #   서버는 거의 실시간 속도로 처리하고, 스트림이 닫히면 미처리분을 버린다.
    #   순간에 밀어넣은 무음은 처리할 시간을 주지 못한다.
    #   시간이 필요하면 end_silence_ms 를 늘려야 한다.
    tail_padding_ms: int = 0


class UtteranceSource:
    """
    게이트가 열린 뒤 프레임을 꺼내며 (pcm, is_last) 를 내보낸다.
    말이 시작되기 전 무음은 안 보낸다. 과금되는 초를 아끼려는 것.
    """

    def __init__(self, mic: MicStream, cfg: UtteranceConfig, cancel: threading.Event):
        self.mic = mic
        self.cfg = cfg
        self.cancel = cancel
        self.vad = webrtcvad.Vad(cfg.vad_mode)
        self.reason = "unknown"
        self._vad_errors = 0

    def _is_speech(self, pcm: bytes) -> bool:
        """VAD 실패 시 발화로 취급한다. 잘못 끊는 것보다 낫다."""
        if len(pcm) != FRAME_BYTES:
            self._vad_errors += 1
            if self._vad_errors in (1, 100):
                log.error("프레임 크기가 %d바이트다 (기대 %d). VAD 를 건너뛴다.",
                          len(pcm), FRAME_BYTES)
            return True

        try:
            return self.vad.is_speech(pcm, SAMPLE_RATE)
        except Exception as e:
            self._vad_errors += 1
            if self._vad_errors in (1, 100):
                # 트레이스백은 한 번이면 충분하다. 프레임마다 찍으면 로그가 못 쓰게 된다.
                log.error("VAD 처리 실패: %s (프레임 %d바이트)", e, len(pcm))
            return True

    def frames(self) -> Iterator[tuple[bytes, bool]]:
        pre = deque(maxlen=max(1, self.cfg.pre_roll_ms // FRAME_MS))
        end_frames = max(1, self.cfg.end_silence_ms // FRAME_MS)
        max_frames = max(1, self.cfg.max_utterance_ms // FRAME_MS)

        started = False
        silence = 0
        sent = 0
        deadline = time.monotonic() + self.cfg.start_timeout_ms / 1000

        while not self.cancel.is_set():
            pcm = self.mic.get(timeout=0.1)
            if pcm is None:
                if not started and time.monotonic() > deadline:
                    self.reason = "no_speech"
                    return
                continue

            speech = self._is_speech(pcm)

            if not started:
                pre.append(pcm)
                if speech:
                    started = True
                    for f in pre:
                        yield f, False
                        sent += 1
                    pre.clear()
                elif time.monotonic() > deadline:
                    self.reason = "no_speech"
                    return
                continue

            silence = 0 if speech else silence + 1
            sent += 1
            last = silence >= end_frames or sent >= max_frames

            if not last:
                yield pcm, False
                continue

            # reason 은 반드시 yield 앞에서 기록한다.
            # 소비자가 is_last 를 받고 return 하면 이 제너레이터는 GeneratorExit 로 닫히고,
            # yield 뒤의 코드는 영영 실행되지 않는다. (종료=unknown 으로 찍히던 원인)
            self.reason = "local_silence" if silence >= end_frames else "max_length"

            n_pad = max(0, self.cfg.tail_padding_ms // FRAME_MS)

            if n_pad == 0:
                # 패딩을 안 쓰면 마지막 실제 프레임이 끝을 표시한다.
                # 합성 무음 프레임에 epFlag 를 실으면 서버가 다르게 반응할 수 있어서
                # 실제 오디오로 끝내는 쪽이 안전하다.
                yield pcm, True
                return

            yield pcm, False

            # 서버가 마지막 구간을 flush 하도록 무음을 밀어넣는다.
            for i in range(n_pad):
                if self.cancel.is_set():
                    return
                yield SILENT_FRAME, (i == n_pad - 1)
            return

        self.reason = "cancelled"


# ------------------------------------------------------------------ gRPC 클라이언트
@dataclass
class ClovaConfig:
    language: str = "ko"

    # 메뉴 이름을 여기 넣으면 인식률이 크게 오른다.
    # (콤마로 구분된 단어 문자열, weight) 튜플의 리스트.
    # 한 그룹 안의 모든 키워드는 같은 weight 를 가진다. 가중치를 나누려면 그룹을 나눈다.
    keyword_boostings: list[tuple[str, float]] = field(default_factory=list)

    # 서버측 EPD.
    # durationThreshold 는 "길이 상한"이 아니라 서버가 결과를 만드는 주 트리거다.
    #   0 = 서버 기본값(실측 약 400ms 주기). 이게 정상 동작이다.
    #   크게 올리면 상한이 풀리는 게 아니라 트리거가 그만큼 미뤄져서 결과가 아예 안 나온다.
    #   (20000 으로 뒀더니 서버가 410ms 만 처리하고 빈 결과를 반환했다. 실측 확인.)
    gap_threshold_ms: int = 0           # 이 시간 이상 무음이면 결과 생성. 0 = 미사용
    duration_threshold_ms: int = 0      # 0 = 서버 기본값. 함부로 올리지 말 것
    syllable_threshold: int = 0         # 음절 수 기준. 0 = 미사용
    use_word_epd: bool = True           # 단어 경계에서 결과를 끝냄. 단어 중간 절단 방지
    use_period_epd: bool = False        # 문장부호에서 결과를 끝냄
    skip_empty_text: bool = False       # true 면 음절 없는 결과를 서버가 생략

    def to_json(self) -> str:
        cfg: dict = {"transcription": {"language": self.language}}

        if self.keyword_boostings:
            cfg["keywordBoosting"] = {
                "boostings": [{"words": w, "weight": v} for w, v in self.keyword_boostings]
            }

        semantic: dict = {"skipEmptyText": self.skip_empty_text}
        if self.gap_threshold_ms > 0:
            semantic["gapThreshold"] = self.gap_threshold_ms
        if self.duration_threshold_ms > 0:
            semantic["durationThreshold"] = self.duration_threshold_ms
        if self.syllable_threshold > 0:
            semantic["syllableThreshold"] = self.syllable_threshold
        if self.use_word_epd:
            semantic["useWordEpd"] = True
        if self.use_period_epd:
            # 문서 권장: usePeriodEpd 를 켜면 useWordEpd 도 켜야 정확도가 오른다
            semantic["usePeriodEpd"] = True
            semantic["useWordEpd"] = True
        cfg["semanticEpd"] = semantic

        return json.dumps(cfg, ensure_ascii=False)


# 한글 음절 판정용. 부스팅 규칙 검증에 쓴다.
_HANGUL = re.compile(r"[가-힣]")


def load_boost_words(path: str) -> list[str]:
    """
    메뉴 파일을 읽는다. 한 줄에 하나든 콤마 구분이든 다 받는다.
    '#' 로 시작하는 줄은 주석.
    """
    raw = Path(path).expanduser().read_text(encoding="utf-8")
    words: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        words.extend(w.strip() for w in line.split(",") if w.strip())
    return words


def validate_boost_words(words: list[str]) -> list[str]:
    """
    문서에 명시된 제약을 걸러낸다. 조용히 무시당하느니 여기서 알려주는 게 낫다.
      - 1음절 단어는 부스팅 미지원 (오인식 위험)
      - 한글/영어만 지원
      - 최대 1000개
      - 여러 단어로 된 긴 구문은 정확히 그 구문일 때만 효과가 있다
    """
    ok: list[str] = []
    seen: set[str] = set()

    for w in words:
        # 띄어쓰기는 서버가 무시하므로 중복 판정도 공백 제거 기준으로 한다
        key = w.replace(" ", "")
        if not key or key in seen:
            continue
        seen.add(key)

        if len(key) <= 1:
            log.warning("1음절이라 부스팅 미지원, 제외: %r", w)
            continue
        if not re.fullmatch(r"[가-힣A-Za-z0-9 ]+", w):
            log.warning("한글/영어만 지원, 제외: %r", w)
            continue
        if len(_HANGUL.findall(key)) == 1 and len(key) == 1:
            log.warning("1음절이라 부스팅 미지원, 제외: %r", w)
            continue
        if " " in w.strip():
            log.warning("여러 단어 구문은 정확히 일치할 때만 먹는다. 쪼개는 게 낫다: %r", w)

        ok.append(w)

    if len(ok) > 1000:
        log.warning("키워드가 %d개다. 최대 1000개까지만 보낸다.", len(ok))
        ok = ok[:1000]

    return ok


class ClovaStreamingStt:
    """
    스트림 하나 = 발화 하나. 채널은 재사용하되 유휴가 길면 새로 만든다.
    문서가 Connection Lifetime 5분 제한을 명시하고 retry 로직을 권장한다.
    """

    RETRYABLE = {
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.INTERNAL,
        grpc.StatusCode.UNKNOWN,
    }

    def __init__(self, secret_key: str, config: ClovaConfig | None = None):
        if not secret_key:
            raise ValueError(f"{SECRET_ENV_NAME} 가 비어 있다")

        self.config = config or ClovaConfig()
        self.config_json = self.config.to_json()
        self.metadata = (("authorization", f"Bearer {secret_key}"),)  # 키 이름은 반드시 소문자

        self._channel: grpc.Channel | None = None
        self._stub = None
        self._last_used = 0.0
        self._channel_lock = threading.Lock()

        self._call = None
        self._call_lock = threading.Lock()
        self._cancel = threading.Event()

        self._seq = 0

    # -------------------------------------------------------------- 채널 관리
    def _ensure_channel(self):
        with self._channel_lock:
            idle = time.monotonic() - self._last_used
            if self._channel is not None and idle > CHANNEL_MAX_IDLE_SEC:
                log.info("채널 유휴 %.0f초, 새로 연결한다", idle)
                self._close_channel_locked()

            if self._channel is None:
                self._channel = grpc.secure_channel(
                    CLOVA_HOST,
                    grpc.ssl_channel_credentials(),
                    options=[
                        ("grpc.keepalive_time_ms", 30_000),
                        ("grpc.keepalive_timeout_ms", 10_000),
                        ("grpc.max_receive_message_length", 4 * 1024 * 1024),
                    ],
                )
                self._stub = nest_pb2_grpc.NestServiceStub(self._channel)
                log.debug("gRPC 채널 생성")

            self._last_used = time.monotonic()
            return self._stub

    def _close_channel_locked(self) -> None:
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception:
                log.exception("채널 정리 실패")
        self._channel = None
        self._stub = None

    def reset_channel(self) -> None:
        with self._channel_lock:
            self._close_channel_locked()

    # -------------------------------------------------------------- 요청 생성
    def _requests(self, frames, seq: int, recorder: list[bytes] | None):
        yield nest_pb2.NestRequest(
            type=nest_pb2.RequestType.CONFIG,
            config=nest_pb2.NestConfig(config=self.config_json),
        )

        for pcm, is_last in frames:
            if self._cancel.is_set():
                return
            if recorder is not None:
                recorder.append(pcm)

            # seqId 는 0이 아닌 값을 권장한다. epFlag=true 응답을 이 값으로 대조한다.
            extra = json.dumps({"seqId": seq, "epFlag": bool(is_last)})
            yield nest_pb2.NestRequest(
                type=nest_pb2.RequestType.DATA,
                data=nest_pb2.NestData(chunk=pcm, extra_contents=extra),
            )
            if is_last:
                return

    @staticmethod
    def _replay(recorded: list[bytes]):
        for i, pcm in enumerate(recorded):
            yield pcm, i == len(recorded) - 1

    # -------------------------------------------------------------- 인식
    def transcribe(self, frames, on_partial: Callable[[str], None] | None = None) -> SttResult:
        """frames 는 (pcm, is_last) 를 내보내는 iterable."""
        self._cancel.clear()
        self._seq += 1
        seq = self._seq

        recorded: list[bytes] = []
        result = self._attempt(frames, seq, recorded, on_partial)

        # 첫 시도가 재시도 가능한 에러로 죽었고 오디오를 녹음해뒀으면 한 번 더 친다.
        # 채널 lifetime 만료로 발화 하나를 통째로 잃는 걸 막는 유일한 방법이다.
        if result.error and result.reason == "retryable" and recorded and not self._cancel.is_set():
            log.warning("재시도한다 (%s)", result.error)
            self.reset_channel()
            result = self._attempt(self._replay(recorded), seq, None, on_partial)

        if recorded:
            result.pcm = b"".join(recorded)
        return result

    def _attempt(self, frames, seq: int, recorder: list[bytes] | None,
                 on_partial: Callable[[str], None] | None) -> SttResult:
        assembler = TextAssembler()
        result = SttResult()
        confidences: list[float] = []
        started = time.perf_counter()
        call = None

        try:
            stub = self._ensure_channel()
            call = stub.recognize(self._requests(frames, seq, recorder), metadata=self.metadata)

            with self._call_lock:
                self._call = call

            for resp in call:
                if self._cancel.is_set():
                    result.reason = "cancelled"
                    return result

                try:
                    contents = json.loads(resp.contents)
                except Exception:
                    log.warning("응답 JSON 파싱 실패: %r", resp.contents[:200])
                    continue

                log.debug("RAW %s", resp.contents)

                types = contents.get("responseType") or []

                # Config 응답. keywordBoosting 이 조용히 실패하는 걸 여기서 잡는다.
                if "config" in types:
                    cfg = contents.get("config") or {}
                    status = cfg.get("status")
                    if status and status != "Success":
                        log.error("Config 거부됨: %s", status)
                    for key in ("keywordBoosting", "forbidden", "semanticEpd"):
                        sub = cfg.get(key) or {}
                        if sub.get("status") and sub["status"] != "Success":
                            log.error("Config %s 실패: %s", key, sub["status"])
                    continue

                if "recognize" in types:
                    rec = contents.get("recognize") or {}
                    if rec.get("status") and rec["status"] != "Success":
                        log.error("Recognize 실패: %s", rec["status"])
                    continue

                tr = contents.get("transcription") or {}
                if not tr:
                    continue

                text = tr.get("text") or ""
                if text:
                    assembler.add(text, tr.get("position"))
                    result.pieces += 1
                    if result.first_partial_ms is None:
                        result.first_partial_ms = (time.perf_counter() - started) * 1000
                    conf = tr.get("confidence")
                    if isinstance(conf, (int, float)):
                        confidences.append(float(conf))
                    for a in tr.get("alignInfos") or []:
                        w, c = a.get("word"), a.get("confidence")
                        if w is not None and isinstance(c, (int, float)):
                            result.aligns.append((w, float(c)))
                    if on_partial:
                        on_partial(assembler.text())

                if tr.get("epdType"):
                    result.epd_type = tr["epdType"]

                if tr.get("epFlag"):
                    resp_seq = tr.get("seqId")
                    if resp_seq not in (None, 0, seq):
                        log.warning("seqId 불일치: 보낸 %d, 받은 %s", seq, resp_seq)
                    result.reason = "server_ep"
                    break

            if not result.reason or result.reason == "unknown":
                # 서버 epFlag 없이 스트림이 끝났다. 우리가 마지막 프레임을 보냈으니 결과는 살린다.
                result.reason = "stream_end"

            result.text = assembler.text()
            if confidences:
                result.confidence = sum(confidences) / len(confidences)
            return result

        except grpc.RpcError as e:
            code = e.code()
            if self._cancel.is_set() or code == grpc.StatusCode.CANCELLED:
                result.reason = "cancelled"
                return result

            result.error = f"{code}: {e.details()}"
            result.reason = "retryable" if code in self.RETRYABLE else "error"

            if code == grpc.StatusCode.RESOURCE_EXHAUSTED:
                # 도메인당 동시 스텁 한도(문서 기준 15개)를 넘겼을 때 나온다.
                log.error("동시 세션 한도 초과. 이전 스트림이 안 닫히고 있는지 확인할 것.")

            log.error("gRPC 에러: %s", result.error)
            return result

        except Exception as e:
            result.error = str(e)
            result.reason = "error"
            log.exception("인식 중 예외")
            return result

        finally:
            result.total_ms = (time.perf_counter() - started) * 1000
            if call is not None:
                try:
                    call.cancel()   # 안 닫으면 서버 슬롯이 계속 물려 있다
                except Exception:
                    pass
            with self._call_lock:
                if self._call is call:
                    self._call = None
            with self._channel_lock:
                self._last_used = time.monotonic()

    def cancel(self) -> None:
        """외부에서 발화를 중단시킨다. ROS 의 /stt_stop 이 여기로 붙는다."""
        self._cancel.set()
        with self._call_lock:
            call = self._call
        if call is not None:
            try:
                call.cancel()
            except Exception:
                pass

    def close(self) -> None:
        self.cancel()
        self.reset_channel()


# ------------------------------------------------------------------ 녹음 / 재생
def save_utterance(out_dir: Path, pcm: bytes, result: SttResult) -> Path:
    """
    발화 하나를 wav + json 으로 남긴다.
    같은 오디오를 설정만 바꿔 다시 돌려야 튜닝을 비교할 수 있다.
    json 의 ref 는 비워둔다. 나중에 손으로 정답을 채워 넣으면 그대로 평가셋이 된다.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"

    wav_path = out_dir / f"{stem}.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)

    (out_dir / f"{stem}.json").write_text(
        json.dumps(
            {
                "ref": "",                       # 손으로 채울 정답
                "hyp": result.text,
                "confidence": result.confidence,
                "epd_type": result.epd_type,
                "reason": result.reason,
                "duration_sec": round(len(pcm) / 2 / SAMPLE_RATE, 3),
                "weakest": result.weakest(5),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return wav_path


def frames_from_wav(path: Path, speed: float = 1.0):
    """
    저장된 wav 를 (pcm, is_last) 로 흘려보낸다.

    반드시 실시간 속도로 보내야 한다. 파일이라고 한꺼번에 던지면 서버가 처리할 시간이
    없어서 결과가 거의 비어서 돌아온다. (실측: 페이싱 없이 돌렸더니 CER 93%, 완전일치 0/52)
    서버는 받은 오디오 양이 아니라 실제 경과 시간에 맞춰 인식한다.

    speed 를 올리면 그만큼 빨리 보낸다. 2.0 이상은 결과가 깨질 수 있으니 직접 확인하고 써라.
    """
    with wave.open(str(path), "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (SAMPLE_RATE, CHANNELS, 2):
            raise ValueError(f"{path.name}: 16kHz/1ch/16bit 가 아니다")
        pcm = w.readframes(w.getnframes())

    total = len(pcm) // FRAME_BYTES
    frame_sec = (FRAME_MS / 1000) / max(speed, 0.01)
    next_at = time.perf_counter()

    for i in range(total):
        chunk = pcm[i * FRAME_BYTES:(i + 1) * FRAME_BYTES]
        is_last = (i == total - 1)
        yield chunk, is_last
        if is_last:
            break
        next_at += frame_sec
        delay = next_at - time.perf_counter()
        if delay > 0:
            time.sleep(delay)


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1,          # 삭제
                           cur[j - 1] + 1,       # 삽입
                           prev[j - 1] + (ca != cb)))  # 치환
        prev = cur
    return prev[-1]


# 서버는 ITN 으로 "2개" 라 쓰고 사람은 "두 개" 라 적는다. 소리는 맞게 알아들은 것이므로
# 오류로 세면 CER 이 부풀려지고, 부스팅 A/B 에서 진짜 개선이 이 노이즈에 묻힌다.
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
    """'두개' -> '2개'. 수량 단위 앞의 수사만 바꾼다."""
    return _NUM_RE.sub(lambda m: _NUM_WORDS[m.group(1)], s)

# 구두점. 모델마다 붙이는 정책이 달라서(Clova 는 거의 안 붙이고 Qwen 은 꼬박 붙인다)
# 이걸 안 지우면 모델 비교가 성립하지 않는다. 주문 파싱에도 영향이 없다.
_PUNCT_RE = re.compile(r"[.,!?;:\u00b7\u2026\u2025\-~'\"\u201c\u201d\u2018\u2019()\[\]]")


def strip_punct(s: str) -> str:
    return _PUNCT_RE.sub("", s)


def cer(ref: str, hyp: str, ignore_space: bool = True, normalize: bool = True) -> float:
    """
    글자 오류율. 한국어는 WER 이 아니라 이걸 봐야 한다.
    주문 파싱에 띄어쓰기는 영향이 없으므로 기본적으로 공백을 뺀 뒤 잰다.
    """
    if ignore_space:
        ref, hyp = ref.replace(" ", ""), hyp.replace(" ", "")
    ref, hyp = strip_punct(ref), strip_punct(hyp)
    if normalize:
        ref, hyp = normalize_numbers(ref), normalize_numbers(hyp)
    if not ref:
        return 0.0 if not hyp else 1.0
    return _levenshtein(ref, hyp) / len(ref)


def keyword_recall(ref: str, hyp: str, words: list[str]) -> tuple[int, int, list[str]]:
    """
    정답에 들어 있는 키워드 중 인식 결과에도 있는 것의 비율.
    전체 CER 보다 이게 주문 성공률에 훨씬 가깝다.
    반환: (맞힌 수, 정답에 있던 수, 놓친 단어들)
    """
    r, h = ref.replace(" ", ""), hyp.replace(" ", "")
    present = [w for w in words if w.replace(" ", "") in r]
    missed = [w for w in present if w.replace(" ", "") not in h]
    return len(present) - len(missed), len(present), missed


def print_result(result: SttResult, source_reason: str = "", verbose: bool = False) -> None:
    if result.error:
        print(f"  [실패] {result.error}")
        return
    if not result.text:
        print(f"  [빈 결과] 종료={source_reason}/{result.reason} epd={result.epd_type}")
        return

    conf = f"{result.confidence:.3f}" if result.confidence is not None else "없음"
    first = f"{result.first_partial_ms:.0f}" if result.first_partial_ms is not None else "-"
    print(f"  결과: {result.text}")
    print(f"  종료={source_reason}/{result.reason} epd={result.epd_type} "
          f"조각={result.pieces} conf={conf} 첫응답={first}ms 총={result.total_ms:.0f}ms")

    weak = result.weakest(3)
    if weak and (verbose or (weak[0][1] < 0.8)):
        # 신뢰도 낮은 음절이 곧 부스팅 후보다
        print("  약한 음절: " + ", ".join(f"{w!r}={c:.2f}" for w, c in weak)) 


def build_config(args) -> ClovaConfig:
    """CLI 인자를 Clova Config JSON 으로 옮긴다."""
    words: list[str] = []
    if args.boost_file:
        words.extend(load_boost_words(args.boost_file))
    if args.boost:
        words.extend(w.strip() for w in args.boost.split(",") if w.strip())

    boostings: list[tuple[str, float]] = []
    if words:
        ok = validate_boost_words(words)
        if ok:
            # 한 그룹 안의 키워드는 전부 같은 weight 를 갖는다.
            # 가중치를 나누고 싶으면 그룹을 나눠서 여기에 튜플을 더 넣으면 된다.
            boostings.append((",".join(ok), args.boost_weight))
            log.info("키워드 부스팅 %d개, weight=%.1f", len(ok), args.boost_weight)

    return ClovaConfig(
        language=args.lang,
        keyword_boostings=boostings,
        gap_threshold_ms=args.gap_threshold,
        duration_threshold_ms=args.duration_threshold,
        syllable_threshold=args.syllable_threshold,
        use_word_epd=args.word_epd,
        use_period_epd=args.period_epd,
        skip_empty_text=args.skip_empty_text,
    )


# ------------------------------------------------------------------ 실행 모드
def run_interactive(args, stt: ClovaStreamingStt) -> None:
    mic = MicStream(device_index=args.device)
    try:
        mic.start()
    except RuntimeError as e:
        sys.exit(str(e))

    ucfg = UtteranceConfig(
        vad_mode=args.vad_mode,
        end_silence_ms=args.end_silence,
        max_utterance_ms=args.max_utterance,
        tail_padding_ms=args.tail_padding,
    )
    record_dir = Path(args.record).expanduser() if args.record else None

    # 설정이 실제로 반영됐는지 매 실행마다 눈으로 확인할 수 있게 찍는다.
    # (데이터클래스 기본값만 고치고 CLI 가 덮어써서 테스트가 무의미해진 적이 있다)
    log.info("분절 설정: end_silence=%dms vad_mode=%d tail_padding=%dms max=%dms",
             ucfg.end_silence_ms, ucfg.vad_mode, ucfg.tail_padding_ms, ucfg.max_utterance_ms)

    print("\nEnter = 센서 트리거(발화 시작), Ctrl+C = 종료\n", file=sys.stderr)

    try:
        while True:
            try:
                input(">>> Enter 를 누르고 말하세요: ")
            except EOFError:
                break

            source = UtteranceSource(mic, ucfg, threading.Event())

            mic.open_gate()
            result = stt.transcribe(
                source.frames(),
                on_partial=(lambda t: print(f"\r  ... {t}", end="", flush=True))
                if args.partial else None,
            )
            mic.close_gate()

            print()
            print_result(result, source.reason, verbose=args.debug)

            if record_dir and result.pcm:
                path = save_utterance(record_dir, result.pcm, result)
                print(f"  저장: {path.name}")

    except KeyboardInterrupt:
        pass
    finally:
        print("\n종료 중...", file=sys.stderr)
        mic.stop()
        if mic.dropped:
            print(f"폐기된 프레임: {mic.dropped}", file=sys.stderr)


def run_replay(args, stt: ClovaStreamingStt) -> None:
    """
    녹음해둔 wav 를 현재 설정으로 다시 돌린다.
    같은 오디오여야 설정 A/B 가 의미를 갖는다. 마이크 앞에서 두 번 말하는 건 비교가 아니다.
    """
    d = Path(args.replay).expanduser()
    wavs = sorted(d.glob("*.wav"))
    if not wavs:
        sys.exit(f"{d} 에 wav 가 없다. 먼저 --record 로 모아라.")

    # dev/test 필터. test 를 보면서 튜닝하면 과적합이다.
    if args.split:
        keep = []
        for w in wavs:
            meta = w.with_suffix(".json")
            if not meta.is_file():
                continue
            try:
                if (json.loads(meta.read_text(encoding="utf-8")).get("split")) == args.split:
                    keep.append(w)
            except Exception:
                pass
        if not keep:
            sys.exit(f"split={args.split} 인 항목이 없다. label_eval.py --split 먼저 돌려라.")
        wavs = keep

    # 부스팅 목록을 키워드 재현율 측정에도 그대로 쓴다
    boost_words: list[str] = []
    for words, _ in stt.config.keyword_boostings:
        boost_words.extend(w.strip() for w in words.split(",") if w.strip())

    total_sec = 0.0
    for p in wavs:
        try:
            with wave.open(str(p), "rb") as w:
                total_sec += w.getnframes() / w.getframerate()
        except Exception:
            pass

    print(f"{len(wavs)}개 재생"
          + (f" [{args.split}]" if args.split else "")
          + (f" | 키워드 {len(boost_words)}개 기준" if boost_words else "")
          + f" | 실시간 {args.replay_speed:.1f}배속, 약 {total_sec / args.replay_speed / 60:.1f}분 소요"
          + "\n")

    total = exact = scored = 0
    cers: list[float] = []
    kw_hit = kw_total = 0
    all_missed: list[str] = []
    rows: list[dict] = []
    errors = 0

    for path in wavs:
        try:
            result = stt.transcribe(frames_from_wav(path, args.replay_speed))
        except ValueError as e:
            print(f"[건너뜀] {e}")
            continue

        total += 1
        if result.error:
            errors += 1

        print(f"[{path.name}]")
        print_result(result, "replay", verbose=args.debug)

        # 옆에 있는 json 의 ref 가 채워져 있으면 채점한다
        meta_path = path.with_suffix(".json")
        ref = ""
        if meta_path.is_file():
            try:
                ref = (json.loads(meta_path.read_text(encoding="utf-8")).get("ref") or "").strip()
            except Exception:
                ref = ""

        row = {"file": path.name, "ref": ref, "hyp": result.text,
               "confidence": result.confidence, "total_ms": result.total_ms}

        if ref:
            scored += 1
            score = cer(ref, result.text)
            cers.append(score)
            exact += (score == 0.0)
            row["cer"] = round(score, 4)

            line = f"  CER {score * 100:5.1f}%"
            if boost_words:
                hit, present, missed = keyword_recall(ref, result.text, boost_words)
                kw_hit += hit
                kw_total += present
                all_missed.extend(missed)
                row["kw_hit"], row["kw_total"], row["kw_missed"] = hit, present, missed
                if present:
                    line += f" | 키워드 {hit}/{present}"
                    if missed:
                        line += " 놓침: " + ", ".join(missed)
            print(line)
            if score > 0:
                print(f"  정답: {ref}")
        rows.append(row)
        print()

    print("=" * 52)
    print(f"처리 {total}개" + (f" (실패 {errors}개)" if errors else ""))

    if not scored:
        print("json 의 ref 를 채우면 CER 과 키워드 재현율을 계산한다.")
        return

    avg = sum(cers) / len(cers)
    print(f"평균 CER   {avg * 100:.2f}%   (낮을수록 좋음)")
    print(f"완전일치   {exact}/{scored} ({exact / scored * 100:.0f}%)")
    if kw_total:
        print(f"키워드     {kw_hit}/{kw_total} ({kw_hit / kw_total * 100:.1f}%)")
        if all_missed:
            top = Counter(all_missed).most_common(5)
            print("자주 놓친: " + ", ".join(f"{w}({n})" for w, n in top))

    # 결과를 남긴다. A/B 는 여러 번 돌리는데 터미널만 보면 반드시 놓친다.
    out_dir = d / "results"
    out_dir.mkdir(exist_ok=True)
    tag = f"w{args.boost_weight:g}" + (f"_{args.split}" if args.split else "")
    out = out_dir / f"{time.strftime('%m%d-%H%M%S')}_{tag}.json"
    out.write_text(json.dumps({
        "settings": {
            "boost_weight": args.boost_weight,
            "boost_words": boost_words,
            "split": args.split,
            "end_silence_ms": args.end_silence,
            "gap_threshold_ms": args.gap_threshold,
            "replay_speed": args.replay_speed,
        },
        "summary": {
            "n": scored,
            "cer": round(avg, 4),
            "exact": exact,
            "kw_hit": kw_hit,
            "kw_total": kw_total,
            "top_missed": Counter(all_missed).most_common(10),
        },
        "rows": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장: {out}")


def main() -> None:
    p = argparse.ArgumentParser()

    g = p.add_argument_group("장치")
    g.add_argument("--device", type=int, default=None)
    g.add_argument("--list-devices", action="store_true")

    _u = UtteranceConfig()   # 기본값 출처는 여기 하나뿐이다. CLI 가 조용히 덮어쓰면 안 된다.

    g = p.add_argument_group("로컬 분절 (우리가 끊는 기준)")
    g.add_argument("--vad-mode", type=int, default=_u.vad_mode, choices=[0, 1, 2, 3],
                   help="0=관대 3=엄격")
    g.add_argument("--end-silence", type=int, default=_u.end_silence_ms,
                   help=f"로컬 끝점 무음 ms (기본 {_u.end_silence_ms})")
    g.add_argument("--tail-padding", type=int, default=_u.tail_padding_ms,
                   help="끝점 판정 후 밀어넣을 무음 ms. 실측에서 효과 없었다(기본 0). "
                        "서버가 필요한 건 오디오 양이 아니라 실제 경과 시간이다")
    g.add_argument("--max-utterance", type=int, default=_u.max_utterance_ms)

    g = p.add_argument_group("서버 EPD (Clova 가 끊는 기준)")
    g.add_argument("--gap-threshold", type=int, default=0,
                   help="이 시간 이상 무음이면 서버가 결과 생성 ms. "
                        "기본 0 = 미사용(로컬이 끊는다). 로컬 값 확정 후 백스톱으로 켜라")
    g.add_argument("--duration-threshold", type=int, default=0,
                   help="서버가 결과를 만드는 주기 ms. 0 = 서버 기본값(약 400ms). "
                        "크게 올리면 결과가 아예 안 나온다. 건드리지 마라")
    g.add_argument("--skip-empty-text", action="store_true",
                   help="음절 없는 결과를 서버가 안 보냄. 마지막 무음 구간 결과까지 사라질 수 있다")
    g.add_argument("--syllable-threshold", type=int, default=0,
                   help="결과 음절 수 상한. 0이면 미사용")
    g.add_argument("--no-word-epd", dest="word_epd", action="store_false",
                   help="단어 경계 정렬 끄기 (기본은 켜짐. 단어 중간 절단 방지용)")
    g.add_argument("--period-epd", action="store_true", help="문장부호에서 끊기")

    g = p.add_argument_group("키워드 부스팅")
    g.add_argument("--boost", help="단어 목록, 콤마 구분")
    g.add_argument("--boost-file", help="단어 파일 (한 줄에 하나 또는 콤마 구분, # 주석)")
    g.add_argument("--boost-weight", type=float, default=2.0, help="0~5.0")

    g = p.add_argument_group("녹음 / 재생")
    g.add_argument("--record", metavar="DIR", help="발화를 wav+json 으로 저장")
    g.add_argument("--replay", metavar="DIR", help="저장된 wav 를 현재 설정으로 다시 인식")
    g.add_argument("--split", choices=["dev", "test"],
                   help="이 분할만 재생. 튜닝은 dev 로만 해라")
    g.add_argument("--replay-speed", type=float, default=1.0,
                   help="재생 배속. 기본 1.0=실시간. 서버가 실시간으로 처리하므로 "
                        "올리면 결과가 깨진다. 올릴 거면 반드시 결과를 확인해라")

    p.add_argument("--lang", default="ko", choices=["ko", "en", "ja"])
    p.add_argument("--partial", action="store_true", help="부분 결과 실시간 출력")
    p.add_argument("--env-file", help="시크릿이 든 env 파일 경로 (기본: 스크립트 옆 env.sh)")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if args.list_devices:
        pa = pyaudio.PyAudio()
        try:
            default_in = pa.get_default_input_device_info().get("index")
        except Exception:
            default_in = None

        print("입력 가능한 장치만 표시한다. --device 없이 실행하면 [기본]을 쓴다.\n")
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            ch = int(info.get("maxInputChannels", 0))
            if ch <= 0:
                continue
            mark = " [기본]" if i == default_in else ""
            print(f"[{i}] {info['name']}{mark}\n"
                  f"      입력채널 {ch}, 기본 {int(info['defaultSampleRate'])}Hz")
        pa.terminate()
        return

    if not 0 <= args.boost_weight <= 5.0:
        sys.exit("--boost-weight 는 0~5.0 이다.")

    try:
        secret = resolve_secret(args.env_file)
    except RuntimeError as e:
        sys.exit(str(e))

    cfg = build_config(args)
    log.debug("Config JSON: %s", cfg.to_json())
    stt = ClovaStreamingStt(secret, cfg)

    try:
        if args.replay:
            run_replay(args, stt)
        else:
            run_interactive(args, stt)
    finally:
        stt.close()


if __name__ == "__main__":
    main()