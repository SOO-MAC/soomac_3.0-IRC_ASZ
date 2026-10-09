# SOOMAC IRC — Drive-Thru LLM V14

음성 주문을 해석하고 주문 상태를 관리하여 ROS2 MAIN 노드에 확정 주문을 전달하는 실행 코드입니다.

## 폴더 구조

- `core/` : Router, Fastpath, 주문 상태 관리, 메뉴·가격 계산, 주문 JSON 발행
- `speech/` : STT 입력·필터·세션, UDP 브리지, TTS 텍스트 발행
- `ui/` : 고객·직원 UI와 UI 서버

## 주요 실행 파일

- `drive_thru_app.py` : 메인 주문 애플리케이션
- `start_app.sh` : 주문 애플리케이션 실행
- `start_vllm.sh` : vLLM 모델 서버 실행
- `soomac_io.launch.py` : STT 브리지, TTS Publisher, 주문 Publisher 실행

## 실행

vLLM 서버:

    bash start_vllm.sh

주문 앱:

    bash start_app.sh

ROS2 연동:

    source /opt/ros/humble/setup.bash
    ros2 launch "$(pwd)/soomac_io.launch.py"

각 명령은 llm/ 폴더에서 별도 터미널로 실행합니다.
실제 STT 모델, 음성 합성 노드 및 MAIN 노드는 별도 실행이 필요합니다.

## 주의사항

주문 기록과 발행 대기열은 `llm/runtime_data/handoffs/`에 유지됩니다.
이 폴더는 삭제하거나 초기화하지 마세요.

모델 경로와 Python 가상환경은 실행 PC에 맞게 설정해야 합니다.
기존 팀 저장소의 stt/, tts/, src/는 변경하지 않았습니다.
