# 주문 확정 → 메인 노드 PUB

팀 저장소 SOO-MAC/soomac_3.0-IRC_ASZ의 main 커밋
`269ba1af9c7c356ad91138e9cc4966b2217c01a7` 규격을 기준으로 작성했습니다.
팀 저장소에는 직접 변경을 올리지 않았습니다.

## 전송 내용

토픽 `/order`, 메시지 `std_msgs/msg/String`. `data`에는 JSON 문자열 하나가 들어갑니다.
`menu`도 여러 메뉴를 쉼표로 연결한 문자열 하나입니다.

```json
{
  "order_no": 1,
  "menu": "치즈버거 세트 1개, 치킨버거 단품 2개, 환타 스몰 1잔",
  "is_mcorder": false,
  "price": 19100
}
```

- 버거는 같은 메뉴와 단품/세트별로 수량을 합칩니다.
- 토핑·재료 제외·세트 내 음료/사이드는 요약에서 생략합니다. 옵션이 다른 같은 세트도 합칩니다.
- 별도로 주문한 음료는 종류와 크기별로 합쳐 `N잔`, 사이드는 종류별로 `N개`로 표시합니다.
- 가격은 기존 checkout 계산값을 그대로 사용합니다. 생략한 토핑·사이즈·사이드 추가금도 포함합니다.
- 상세 handoff/주문 UI에는 기존 옵션 정보가 그대로 남습니다.

```json
{
  "order_no": 2,
  "menu": "맥오더 65번",
  "is_mcorder": true,
  "price": 0
}
```

맥오더에서 2는 접수 누적번호, 65는 고객이 말하고 확인한 맥오더 예약번호입니다.
현재 main_node와 arm_control 모두 `price` 키가 있어야 JSON을 받아들이므로
이미 결제된 맥오더에는 호환용 `price: 0`을 보냅니다.
일반 주문과 맥오더 모두 하나의 누적 순번을 사용합니다.

## 발행 시점과 구조

일반 주문은 확정 상태 검증과 가격 계산 후, 맥오더는 번호 재확인에 고객이 긍정한 후에만
발행 대기열에 들어갑니다. 메뉴를 추가하거나 가격을 물을 때, 맥오더 번호만 말했을 때는 보내지 않습니다.

`checkout_manager.py` → 디스크 발행 대기열 → `order_result_publisher.py` → `/order` → `drive_thru_main`

LLM 가상환경에 ROS 패키지를 추가할 필요가 없도록 기존 STT/TTS 브리지처럼 ROS 프로세스를 분리했습니다.
`soomac_io.launch.py`에 주문 publisher를 추가했습니다. LLM의 기존 `/tts/text` 발화 경로는 유지됩니다.
창구 TTS는 메인 노드가 `/tts`로 발화하는 기존 구조입니다. 팀의 `tts/deliver_tts_node.py`가 받습니다.

발행 파일은 `llm/runtime_data/handoffs/ros_outbox/pending/`에 저장합니다.
`drive_thru_main` 구독자가 연결되면 순번대로 발행하고 DDS 전송 확인 후 `sent/`로 이동합니다.
메인 노드 없이 `ros2 topic echo`만 켜 놓으면 대기열을 소비하지 않습니다.
정상 처리된 파일은 publisher 재실행 시 다시 보내지 않습니다. 과거 handoff도 자동 재전송하지 않습니다.

이 방식은 메인 노드의 주문 접수 응답(ACK)을 받는 프로토콜은 아닙니다.
발행 직후 프로세스가 비정상 종료되면 재실행 시 중복 전송 가능성이 있습니다.
메인 노드는 현재 대기열 안의 중복 번호를 거부하지만 완료된 주문번호를 영구 보관하지 않습니다.
메인 노드 자체 재시작 시 메모리 주문 대기열도 사라집니다. 자동 복구/정확히 한 번 처리가 필요하면
메인 노드 쪽 영구 저장과 주문별 ACK를 함께 추가해야 합니다.

## 실행

기존 파일은 백업하고 새 ZIP은 별도 폴더에 먼저 푸세요. 아래 명령은 새 프로젝트 루트 기준입니다.
기존 누적번호를 이어가려면 이전 `llm/runtime_data/handoffs/`를 새 프로젝트의 같은 위치에 복사하세요.
새 빈 폴더로 시작하면 누적번호는 1부터입니다. 기존 발행 대기/전송 이력도 함께 보관해야 합니다.
앱과 publisher는 반드시 같은 프로젝트 사본의 파일을 실행하세요.

1. ROS2 터미널에서 IO 런치를 실행합니다(설치된 ROS 배포판 환경을 먼저 source).

```bash
source /opt/ros/humble/setup.bash
ros2 launch "$(pwd)/llm/soomac_io.launch.py"
```

`ORDER PUB READY` 로그가 추가됩니다. ROS2 터미널의 `python3`가 `rclpy`를 import할 수 있어야 합니다.
기존 IO 런치가 켜져 있다면 종료한 뒤 새 파일로 다시 실행하세요. 같은 브리지를 중복 실행하지 마세요.
기존 vLLM 서버는 그대로 사용합니다.

2. 별도 터미널에서 새 프로젝트 루트로 이동한 뒤 앱을 실행합니다.

```bash
bash llm/start_app.sh
```

3. 팀 메인 노드를 실행하고, 별도 터미널에서 수신을 확인합니다.

```bash
ros2 topic info /order --verbose
ros2 topic echo /order std_msgs/msg/String
```

앱에서 기존처럼 `/carin` → 주문 → 마무리하면 `/order`에 JSON이 나와야 합니다.
맥오더는 `/carin` → `맥오더 65번` → `네` 순서로 확인합니다. 다음 차량은 `/carout` 후 `/carin`입니다.
이 변경은 결과 PUB 연결입니다. 메인 노드의 `llm/start_order` 서비스 호출로 자동 세션을 시작하는 기능은
이번에 추가하지 않았으므로 기존 수동/외부 차량 입력 경로를 계속 사용하세요.

서로 다른 PC의 메인 노드와 연결할 때는 ROS 네트워크 설정과 `ROS_DOMAIN_ID`를 맞춰야 합니다.
토픽/메인 노드 이름을 바꾼 경우 publisher를 단독 실행할 때 ROS 파라미터로 지정할 수 있습니다.

```bash
python3 llm/order_result_publisher.py --ros-args -p order_topic:=/order -p main_node_name:=drive_thru_main
```

앱 저장 위치는 앱 파일 기준으로 고정했습니다. 특별히 다른 대기열을 읽을 때는 publisher의
`outbox_dir` 파라미터에 해당 절대경로를 지정하세요.
`/resetall`은 개발용 주문 데이터를 초기화하지만 이미 확정된 발행 대기열/전송 이력은 지우지 않습니다.
메인에 접수된 주문과 번호가 충돌하지 않도록 다음 번호를 계속 이어갑니다.

## 메인 노드 맥오더 발화 수정 1줄

기존 메인 노드는 맥오더 안내에서 누적번호인 `order_no`를 읽습니다.
`order_no=2`, `menu="맥오더 65번"`을 받으면 기존 코드로는 “맥오더 2번”이라고 말합니다.
`integration/main_node_mcorder_tts.patch`를 적용하면 “맥오더 65번 준비해 드릴게요.”라고 말합니다.

팀 ROS 저장소 루트에서 패치 경로를 실제 압축 해제 위치로 바꿔 실행하세요.

```bash
git apply --check /수정본/프로젝트/integration/main_node_mcorder_tts.patch
git apply /수정본/프로젝트/integration/main_node_mcorder_tts.patch
colcon build --packages-select irc_main --symlink-install
source install/setup.bash
```

패치 적용 후 메인 노드를 다시 실행해야 합니다. 팀 저장소의 다른 변경과 충돌하면 강제로 적용하지 마세요.
추가로 현재 로봇팔의 봉투 인식(`item_cb`)도 `order_no`와 봉투 번호를 비교합니다.
맥오더 봉투에 예약번호가 붙는 운영이라면 이 매칭 규칙도 별도로 바꿔야 합니다.
이번 패치는 발화만 바꾸고, 로봇팔의 물품 선택 규칙은 바꾸지 않습니다.

## 검증

실사용용에는 `llm/test_main_order_publish.py`를 포함하고, GitHub용에는 사용자 요청대로 테스트를 제외했습니다.
검증은 오프라인 앱/발행 로직 테스트이며 실제 ROS DDS 통신·로봇 구동·마이크/GPU 테스트와 구분합니다.

```bash
cd llm
source ~/drive_thru_venv/bin/activate
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q test_modifier_selection.py test_main_order_publish.py
```

ROS publisher 테스트는 ROS 바인딩을 대체하여 수행합니다. 정상/맥오더 요약, 정확한 총액,
확정 전 미발행, 반복 확정, 예약번호 정정, 순번 복구, 미접속 대기, 전송 실패/ACK 대기,
정상 재시작 시 미재발행을 확인합니다. 실제 현장에서는 위 `/order` 수신도 확인하세요.
