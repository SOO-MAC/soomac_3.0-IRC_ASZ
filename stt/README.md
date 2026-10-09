# 음성인식 파일 준비

앱의 Qwen STT 경로는 `qwen_live_ver_3.py`입니다. CLOVA를 별도로 사용할 때만
아래 protobuf 파일과 CLOVA 인증 정보가 필요합니다.

## CLOVA protobuf

`stt_clova_ver_7.py`는 같은 폴더의 `nest_pb2.py`, `nest_pb2_grpc.py`를 import합니다.
실제 사용용 ZIP에는 두 생성 파일을 포함했습니다. GitHub용에는 명세와 생성 스크립트를
포함했으며, CLOVA 최초 실행 전에 다음 명령으로 생성하세요. 프로젝트 루트 기준입니다.

```bash
source ~/drive_thru_venv/bin/activate
python3 -m pip install grpcio-tools==1.76.0
python3 stt/generate_proto.py
```

생성 스크립트는 실행하는 터미널 위치와 무관하게 자기 폴더에 파일을 만듭니다.
동봉한 생성 파일의 실행 의존성은 `grpcio>=1.76.0`, `protobuf>=6.31.1,<7`입니다.
생성 도구는 최초 생성/재생성에만 필요합니다. 생성 파일은 `.gitignore`에서 제외됩니다.

CLOVA 실행에는 `grpcio`, `protobuf`, `pyaudio`, `webrtcvad`, 마이크 및 유효한 인증 정보가
필요합니다. 기존 `env.sh`를 보관해 사용하거나 `env.sh.example`을 참고하세요.
개인 `env.sh`는 ZIP에 포함하지 않았습니다. 공개 GitHub에 올리지 마세요.

```bash
python3 stt/stt_clova_ver_7.py --env-file /실제/인증파일/env.sh
```

## Qwen 및 ROS

Qwen 음성인식에는 별도로 `numpy`, `pyaudio`, `webrtcvad`, `soxr`, `silero_vad`,
`torch`, Qwen3-ASR을 지원하는 `transformers`와 모델이 필요합니다.
ROS 연동에는 ROS2의 `launch`, `rclpy`, `std_msgs`를 제공하는 환경을 사용하세요.
`llm/requirements-runtime.txt`는 개발 환경 참고 스냅샷이며 통째 재설치 지시가 아닙니다.
기존 정상 작동 환경을 유지하세요. 앱 STT 어댑터가 `audioop`를 사용하므로
Python 3.12 환경을 권장합니다(Python 3.13에서는 표준 라이브러리에서 제거됨).

클라우드 검증에는 실제 마이크·GPU 모델·CLOVA 인증·ROS2 장치 연결을 사용하지 않았습니다.
