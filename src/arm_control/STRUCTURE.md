# IRC ROS2 구조 정리

기준 커밋: `78d4bec` (`전달 지연 및 픽업 보정 안정화`).

## 패키지와 파일 역할

| 위치 | 역할 |
| --- | --- |
| `src/drive_thru_detection/drive_thru_detection/paper_bag_detection_node.py` | Camera1: 물체 검출, 번호 OCR, `Detection2DArray`와 `CameraInfo` 발행. 원본 코드 그대로 이동. |
| `src/drive_thru_detection/drive_thru_detection/pose_node.py` | Camera2: 운전자 포즈 추정, 카메라 좌표계의 `PointStamped` 발행. 원본 코드 그대로 이동. |
| `src/drive_thru_detection/models/` | 기존 두 YOLO 모델. 원본 바이트 그대로 이동. |
| `src/drive_thru_detection/launch/detection.launch.py` | Camera1과 Camera2 노드를 함께 실행. |
| `src/arm_control/arm_control/arm_control_node.py` | 주문 수신, 좌표 변환, 검출 잠금, IK, 경로 검증, 결제·픽업·전달·복귀와 모터 서비스 요청. ROS 노드는 `ArmControlNode` 하나. |
| `src/arm_control/arm_control/robot_config.py` | 공통 설정 읽기와 FK/IK 체인, 각도↔틱 변환, 관절·틱 제한 검증 함수. Motor에서도 이 모듈만 가져옴. |
| `src/arm_control/config/robot_config.yaml` | 로봇 기구학, 관절 제한, 모터 ID·포트·속도·허용오차. 기존 값 유지. |
| `src/arm_control/config/motion_parameters.yaml` | 물체별 모션 상수, 기준 자세·치수, 카메라 보정행렬과 오프셋. 계산식에서 파생하는 값은 코드에서 계산. |
| `src/arm_control/config/arm_control.yaml` | ROS 노드 파라미터: 잠금·필터 정책, 봉투 Y +25 mm 보정, 최종 전달 OPEN 전 5초 대기. |
| `src/arm_control/config/cup_z105_FULL_VERIFIED.json` | 기존 검증된 컵 경로 데이터. 원본 바이트 유지. |
| `src/arm_control/launch/full_control.launch.py` | Main·Arm·Motor 실행. `with_detection:=true`이면 비전 두 노드도 실행. |
| `src/motor_control/motor_control/motor_control_node.py` | Dynamixel 구동, 경로 추종·정착 검사, 그리퍼 서비스. 공통 설정 import 경로만 변경. |
| `src/irc_main/irc_main/main_node.py` | 주문 FIFO와 드라이브스루 상태기계. 변경 없음. |
| `src/soomac_interfaces/msg/DetectedItem.msg` | 번호·물체 종류·팔 기준 XY(mm)·신뢰도를 담는 메시지 정의. |
| `src/soomac_interfaces/srv/` | `MoveToTicks`, `MoveTickPath`, `ExecutePickPlace`, `ArmCommand`의 요청·응답 정의. 기존 타입과 서비스 계약 유지. |
| `src/arm_control/test/test_motion_regression.py` | 원본 모션 체크포인트에 대한 계산 전용 회귀검증. 노드 생성과 모터 요청 없음. |
| `src/arm_control/test/test_pose_gate.py` | 운전자 입력 OFF/ON과 좌표 변환 검증. 노드 생성과 모터 요청 없음. |

각 패키지의 `package.xml`은 ROS 의존성, `setup.py`는 Python 모듈·실행파일·설정·launch·모델 설치, `setup.cfg`는 실행파일 설치 위치, `resource/`는 ament 패키지 등록 표식이다. `soomac_interfaces`는 `CMakeLists.txt`에서 메시지와 서비스 타입을 생성한다.

## 통합한 코드

`bridge_logic`, `motion_runtime`, `handoff_runner_logic.py`, `motion_logic_immutable.py`를 제거하고 실제 사용 계산·검증·실행 코드를 `arm_control_node.py`에 통합했다. 플래너는 같은 파일의 내부 클래스로 구분하여 물체마다 다른 상수와 함수 이름이 충돌하지 않게 했다. 클래스별로 ROS 노드를 생성하지 않는다. CLI 실험 실행부는 제거했다.

Runner는 부모 노드를 명시적으로 받아 해당 노드의 모터 클라이언트를 사용한다. 이전의 `sys.path` 삽입, `sys.modules` 교체, 전역 부모 노드 등록을 제거했다. 서비스 응답 처리는 기존 Reentrant callback group과 MultiThreadedExecutor 구조를 유지한다.

`control_config`라는 별도 ROS 패키지는 없다. 공유 설정이 `arm_control` 안에 있으므로 `motor_control`은 `arm_control`에 의존한다. Motor는 `robot_config`만 import하며 `ArmControlNode`를 생성하거나 모션 플래너를 실행하지 않는다.

## DetectedItem과 통신

`DetectedItem.msg` 자체는 발행 노드가 아니라 메시지 형식이다. 기존 Camera1 검출 노드는 `/paper_bag/detections`에 `Detection2DArray`를 발행했다. Arm 내부 브리지가 이를 팔 기준 좌표로 변환해 `/drive_thru/detected_item`에 `DetectedItem`을 발행하고, Arm이 다시 구독했다.

통합본에서는 Arm의 `detections_cb`가 변환 후 `item_cb`를 직접 호출한다. Camera2도 `driver_camera_pose_cb`가 변환 후 Runner와 운전자 잠금 콜백을 직접 호출한다. `/drive_thru/detected_item`, `/driver_pose/target_base` 발행은 관측용으로 유지하며 Arm 자신은 다시 구독하지 않는다. 외부에서 이 변환 결과 토픽에 직접 발행하여 Arm에 입력하던 방식은 사용하지 않는다.

Main↔Arm의 기존 토픽·서비스와 Arm↔Motor의 서비스 타입은 유지한다. `soomac_interfaces`는 통신을 수행하는 노드가 아니며, 양쪽이 같은 타입을 사용하기 위한 공통 인터페이스 패키지다. 삭제하면 메시지·서비스 import와 타입 생성이 깨진다.

기존 Main은 `/pose_estimation/enable` (`std_srvs/srv/SetBool`)을 호출하지만 원본에는 해당 서비스 서버가 없었다. 통합 Arm이 이 서비스를 제공하여 운전자 검출 뒤 입력을 차단하고, Main의 7초 재활성화 타이머가 ON을 보내면 다시 받는다. 카메라 추론 자체는 계속 실행되며, OFF는 Arm의 운전자 입력·좌표 발행·잠금을 차단한다. 비전 패키지를 바꿔도 운전자 노드 이름 `driver_pose`를 유지하므로 기존 `/driver_pose/target` 토픽과 연결된다.

## 로컬 적용과 확인

이번 누적 패치는 기준 커밋 `78d4bec`의 구조에서 적용한다. 작업 중인 수정이 있다면 먼저 별도 보관하고, 패치는 다운로드한 위치에 맞게 경로를 조정한다. 적용은 커밋이나 푸시를 하지 않는다.

```bash
cd ~/Documents/IRC_ASZ_GITHUB
git switch -c refactor/irc-package-structure
git apply --check ~/Downloads/IRC_structure_local.patch && \
  git apply ~/Downloads/IRC_structure_local.patch
git diff --stat
git status --short
```

아래 빌드와 테스트 후 Arm만 실행하면 모터 서비스 요청 없이 서비스 등록을 확인할 수 있다. 다른 터미널에도 같은 ROS 및 새 install 환경을 source한다.

```bash
ros2 run arm_control arm_control
```

다른 터미널에서:

```bash
ros2 node info /arm_control
ros2 service call /arm_control/execute soomac_interfaces/srv/ArmCommand "{command: 'PING', object_type: '', x_mm: 0.0, y_mm: 0.0, z_mm: 0.0}"
ros2 service call /pose_estimation/enable std_srvs/srv/SetBool "{data: false}"
ros2 service call /pose_estimation/enable std_srvs/srv/SetBool "{data: true}"
```

이 단계에서는 Main·Motor를 실행하지 않는다. 실제 카메라 토픽과 모터 응답 검증은 장비가 있는 환경에서 전체 launch로 별도 확인한다. 로컬 확인을 마친 뒤 변경 내용을 검토하고 커밋·푸시한다.

## 실행

새 체크아웃에서 빌드하거나, 이전 설치 결과와 분리한 빌드·설치 폴더를 사용한다. Ubuntu 22.04 / ROS2 Humble 기준:

```bash
source /opt/ros/humble/setup.bash
python3 -m pip install 'ikpy==4.1.0' PyYAML
colcon --log-base log_refactored build --symlink-install \
  --build-base build_refactored --install-base install_refactored
source install_refactored/setup.bash
```

비전 두 노드만 실행:

```bash
ros2 launch drive_thru_detection detection.launch.py
```

Main·Arm·Motor와 비전 두 노드를 모두 실행:

```bash
ros2 launch arm_control full_control.launch.py with_detection:=true
```

Main·Arm·Motor만 실행하는 기존 동작:

```bash
ros2 launch arm_control full_control.launch.py
```

비전을 별도 실행했다면 `with_detection:=true`를 중복 실행하지 않는다. 모델 경로와 Camera1/Camera2 serial, 기존 검출·좌표 변환 정책은 유지한다. 카메라 라이브러리는 기존 설치 환경을 사용하며 통합 requirements는 `src/drive_thru_detection/requirements.txt`에 있다.

`comm_test.launch.py`는 삭제했다. `full_control.launch.py`는 실제 운용용이므로 유지한다.

계산 및 입력 게이트 테스트(노드 생성·모터 요청 없음):

```bash
python3 -m unittest discover -s src/arm_control/test -v
```

## 검증 범위

원본과 통합본을 같은 Python/NumPy/SciPy/ikpy 환경에서 비교했다. ROS 메시지·서비스를 모의 객체로 대체하여 실제 모터 요청 없이 다음 결과를 비교했다:

- 봉투·손잡이 봉투·컵의 PICK→HANDOFF→RETURN_HOME 서비스 요청 순서와 모든 틱 경로.
- 결제 자세 경로와 서비스 요청.
- 컵 좌표 (80,500), (120,450), (120,400)의 픽업 경로와 최종 pull 거리.
- 잘못된 작업영역·운전자 방향의 거절 결과.
- Camera1 OCR 번호·XY 변환·필터 잠금, Camera2 변환·운전자 잠금.
- 기존 컵 JSON, 하드웨어 제한, 5초 전달 OPEN 대기, 부모 Runner 소유권.

17개 비교 항목의 수치 4,480개가 정확히 일치했고, 네 시퀀스의 경로·그리퍼 요청 35개가 일치했다. 계산 회귀 테스트 6개와 입력 게이트 테스트 2개도 통과했다. 모의 비동기 서비스·토픽으로 실제 Main과 Arm 콜백을 연결하여 일반 주문 결제·전달, 다음 McOrder 전달, FIFO 진행, 운전자 입력 OFF→재활성화→ON 흐름을 확인했다. 두 검출 소스·모델·컵 JSON·Main·인터페이스는 바이트 단위로 동일하고, Motor 소스 변경은 공통 설정 import 한 줄뿐이다.

원본에도 있던 미사용 손잡이 봉투 reference plan의 READY_4 IK 실패는 그대로이며, 실제 V2 손잡이 봉투 시퀀스는 비교 입력에서 완료된다. 이번 변경에서 모션 알고리즘을 교정하지 않았다.

이 환경에는 ROS2가 없어 실제 `colcon build`, DDS 통신, 카메라·모터 구동은 실행하지 않았다. Python 문법과 setuptools 설치 데이터, 모의 ROS 환경의 노드 초기화·콜백·계산·요청 결과를 검증했다.
