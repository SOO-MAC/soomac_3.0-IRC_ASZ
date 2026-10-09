# SOOMAC IRC — Drive-Thru LLM V14 (실행용)

이 폴더는 자연어 주문을 구조화하고 주문 상태를 관리한 후 로봇 제어 쪽에 `FINAL HANDOFF`를 전달하는 실행 코드입니다.

## 처리 흐름

STT → vLLM (Qwen V14) → XGrammar / Pydantic 검증 → Router / Order Runtime → 결정론적 가격 계산 → FINAL HANDOFF → ROS2 Task Manager

## 주요 파일

- `drive_thru_app.py`: 메인 주문 앱
- `router_client.py`, `router_schema.py`, `router_policy.py`, `router_*fastpath.py`: 명령 해석과 Router
- `order_runtime_final.py`, `order_schema.py`, `order_update_schema.py`: 주문 상태·검증
- `menu_knowledge.py`, `checkout_manager.py`, `price_calorie_info_engine.py`: 메뉴 및 결제 정보
- `ui/`, `ui_runtime_bridge.py`: 직원·고객 UI
- `qwen_stt_ros_node.py`, `ros_stt_udp_bridge.py`, `tts_text_publisher.py`: 음성 및 ROS 연동
- `requirements.txt`: 주요 의존성 및 CUDA/vLLM 관련 주의 사항
- `requirements-runtime.txt`: 개발 PC 전체 환경의 참고 스냅샷(다른 컴퓨터에서 그대로 설치하는 것과는 다름)

## 실행

V14 모델을 별도로 준비하고 `start_vllm.sh` 및 `start_app.sh`를 사용하세요.

`start_vllm.sh`에 지정된 개발 PC 모델 경로: `~/drive_thru_llm/outputs/qwen35_drive_thru_v14_merged`

`start_app.sh`의 가상환경 경로: `~/drive_thru_venv`. 앱 작업 폴더는 스크립트 위치를 기준으로 자동 설정됩니다.

`soomac_io.launch.py`도 런치 파일 위치를 기준으로 STT/TTS 스크립트를 찾습니다. 다른 PC에서는 외부 가상환경과 모델 경로만 확인하세요. ROS2 환경은 별도로 필요합니다.

## GitHub에 포함하지 않은 항목

학습용 `dataset_v14/`, Router 평가 `router_data/`, `regression_cases/`, `test_*.py`, `eval_*.py`, `*.jsonl`, 테스트 결과 JSON, 모델 가중치와 가상환경은 제외했습니다. 학습이나 회귀 평가를 다시 수행하려면 별도로 보관한 원본 프로젝트에서 해당 파일을 가져와야 합니다.
