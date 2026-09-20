# SOOMAC IRC Drive-Thru LLM V14

SOOMAC(수맥) IRC 매니퓰레이터 대회를 위한 드라이브스루 주문 해석 및 주문 상태 관리 시스템입니다.

사용자의 자연어 주문을 LLM으로 구조화된 주문 명령으로 변환하고, Pydantic Schema, Semantic Verification, Runtime Guard, Order State Manager를 통해 검증한 뒤 최종적으로 로봇 시스템이 사용할 수 있는 `FINAL HANDOFF` JSON을 생성합니다.

> 현재 버전: **V14**

---

## 1. System Overview

전체 주문 처리 흐름은 다음과 같습니다.

```text
Customer Utterance
        │
        ▼
      STT
        │
        ▼
Qwen3.5-9B + QLoRA (V14)
        │
        ▼
XGrammar Structured Output
        │
        ▼
Pydantic Schema Validation
        │
        ▼
Semantic Verification
        │
        ▼
Runtime Guard / Repair
        │
        ▼
Order State Manager
        │
        ▼
Deterministic Pricing
        │
        ▼
FINAL HANDOFF JSON
        │
        ▼
ROS2 Task Manager
        │
        ▼
Robot Manipulator
```

LLM은 자연어 해석과 구조화된 주문 업데이트 생성을 담당합니다.

가격 계산, 주문 상태 관리, semantic validation 및 최종 로봇 명령 전달은 Python 기반의 deterministic logic에서 처리합니다.

LLM이 로봇 모터를 직접 제어하지 않습니다.

---

## 2. Main Features

현재 V14 시스템에서 지원하는 주요 기능은 다음과 같습니다.

- 버거 / 음료 / 사이드 주문
- 단품 / 세트 선택
- 음료 종류 및 사이즈 선택
- 사이드 선택
- 토핑 추가
- 피클 / 양파 제외
- 기존 주문 항목 수정
- 여러 주문 항목 중 특정 항목 참조
- multi-turn 주문
- pending field 처리
- 주문 완료 의도 인식
- 모바일 주문번호 픽업
- deterministic 가격 계산
- 차량 단위 주문 세션 관리
- FINAL HANDOFF JSON 생성
- runtime semantic guard
- known model failure deterministic guard

---

## 3. Architecture

### LLM Parser

Base model:

```text
Qwen/Qwen3.5-9B
```

V14는 QLoRA 방식으로 주문 도메인에 맞게 fine-tuning되었습니다.

Runtime에서는 로컬 vLLM OpenAI-compatible API를 사용합니다.

```text
API Base:
http://127.0.0.1:8000/v1

Served Model Name:
drive-thru-v14
```

### Structured Output

LLM 출력은 자유 텍스트가 아니라 주문 업데이트용 JSON으로 제한합니다.

```text
LLM
 ↓
XGrammar
 ↓
Pydantic OrderUpdate
```

XGrammar는 JSON 구조를 제한하고, Pydantic은 타입 및 enum validation을 담당합니다.

그 이후 별도의 semantic verifier와 runtime guard가 현재 주문 상태와 출력의 의미적 일관성을 검사합니다.

---

## 4. Order Runtime

주요 runtime 구현:

```text
order_runtime_final.py
```

Runtime의 주요 역할:

```text
LLM Parsing
    ↓
Schema Validation
    ↓
Semantic Verification
    ↓
Pending Repair
    ↓
Runtime Guard
    ↓
Transactional State Update
```

LLM 출력이 JSON schema를 만족하더라도 현재 주문 상태와 의미적으로 충돌할 수 있기 때문에 별도의 deterministic validation layer를 사용합니다.

---

## 5. Order State

주문 schema:

```text
order_schema.py
order_update_schema.py
```

대표적인 주문 항목:

```text
burger
drink
side
```

주요 옵션:

```text
single / set
drink
drink_size
side
exclude
add_toppings
```

주문 수정은 전체 주문을 다시 생성하는 방식이 아니라 기존 state에 적용되는 structured update 방식으로 처리합니다.

---

## 6. Deterministic Pricing

가격은 LLM이 계산하지 않습니다.

```text
checkout_manager.py
drive_thru_app.py
```

Python 가격 테이블과 deterministic pricing logic을 사용합니다.

따라서 LLM의 산술 오류나 임의 가격 생성을 최종 결제 금액에 사용하지 않습니다.

최종 counter handoff에는 계산된 `total_price`가 포함됩니다.

---

## 7. Vehicle Session

드라이브스루 주문은 차량 단위 session으로 관리됩니다.

```text
IDLE
  │
  │ vehicle detected
  ▼
ORDERING
  │
  │ order finalized
  ▼
WAITING_FOR_EXIT
  │
  │ vehicle exits
  ▼
IDLE
```

개발 및 테스트 환경에서는 다음 명령을 사용할 수 있습니다.

```text
/carin
/carout
/reset
/resetall
/state
/order
/quit
```

---

## 8. FINAL HANDOFF

일반 주문 완료 시 예시:

```json
{
  "command": "counter_order",
  "order_id": 1,
  "menu": [
    {
      "item_type": "burger",
      "quantity": 1,
      "menu": "bulgogi_burger",
      "type": "single",
      "exclude": [
        "pickle"
      ],
      "add_toppings": []
    }
  ],
  "total_price": 4500
}
```

모바일 주문 픽업 예시:

```json
{
  "command": "mobile_pickup",
  "order_id": 2,
  "mobile_order_id": 24
}
```

Handoff는 기본적으로 다음 위치에 생성됩니다.

```text
runtime_data/handoffs/
```

향후 ROS2 Task Manager가 이 결과를 받아 실제 로봇 동작을 수행하도록 연결합니다.

---

## 9. V14 Training

V14 training script:

```text
train_qlora_v14.py
```

Base model:

```text
Qwen/Qwen3.5-9B
```

확인된 주요 학습 구성:

```text
QLoRA
4-bit quantization
NF4
Double Quantization
LoRA Adapter
Gradient Checkpointing
packing = False
completion_only_loss = True
```

Smoke training도 지원합니다.

```bash
python3 train_qlora_v14.py --smoke
```

Smoke mode는 전체 학습 전에 pipeline이 정상 동작하는지 빠르게 확인하기 위한 용도입니다.

---

## 10. Model Merge

QLoRA training 결과 adapter는 다음 script를 통해 base model과 병합합니다.

```bash
python3 merge_v14.py
```

기본 adapter:

```text
outputs/qwen35_drive_thru_qlora_v14/final_adapter
```

기본 merged model:

```text
outputs/qwen35_drive_thru_v14_merged
```

Merge 과정에서는 PEFT의 `merge_and_unload()`를 사용합니다.

Model weights는 repository에 포함하지 않습니다.

---

## 11. Dataset

V14 dataset:

```text
dataset_v14/
├── train.jsonl
├── val.jsonl
├── test.jsonl
├── real_blind_v14_test.jsonl
├── robustness_probe_v14.jsonl
└── build_report.json
```

이전 blind dataset들도 regression evaluation 재현을 위해 보존합니다.

V14 dataset 생성:

```bash
python3 make_dataset_v14.py
```

Robustness probe 생성:

```bash
python3 make_v14_robustness_probe.py
```

---

## 12. Evaluation

### Final model-only regression

V14 최종 regression 결과:

```text
276 / 278
99.28%
```

세부 결과:

| Suite | Result |
|---|---:|
| v14_blind | 11 / 12 |
| v13_blind | 12 / 12 |
| v12_blind | 12 / 12 |
| v11_blind | 12 / 12 |
| v9_blind | 10 / 10 |
| v8_blind | 12 / 12 |
| v7_blind | 7 / 8 |
| blind_v1 | 50 / 50 |
| blind_v2 | 50 / 50 |
| blind_v3 | 100 / 100 |
| **Total** | **276 / 278 (99.28%)** |

이 수치는 **model-only regression** 결과입니다.

모델 자체가 278/278을 달성했다고 보고하지 않습니다.

---

## 13. Known Model-Level Errors

최종 regression에서 알려진 model-level failure class는 두 가지입니다.

### Existing-state negative reference

예:

```text
양파 빼놓은 불고기버거에 베이컨 넣어주세요
```

현재 주문 state에 실제로 "양파를 제외한 불고기버거"가 존재하지 않는 경우 모델이 잘못된 항목을 수정할 가능성이 있습니다.

### Optional drink-size hallucination

예:

```text
아이스 아메리카노도 한 잔 넣어주세요
```

사용자가 사이즈를 말하지 않았음에도 모델이 임의의 size를 생성하는 경우가 있습니다.

이 두 error class는 model score를 숨기지 않고 그대로 유지하며, production runtime에서는 deterministic guard를 추가해 차단합니다.

---

## 14. Runtime Guard Verification

알려진 두 model-level error에 대해 강제로 잘못된 LLM 출력을 주입하는 regression test를 수행합니다.

```bash
python3 test_v14_known_failures_runtime.py
```

결과:

```text
PASS: negative reference grounding
PASS: hallucinated drink size

V14 known model failures runtime guard: 2/2 PASS
```

즉,

```text
Model-only:
276 / 278 (99.28%)

Known failure runtime guards:
2 / 2 PASS
```

로 구분하여 평가합니다.

---

## 15. Runtime Regression

최종 runtime regression:

```text
V14 pending field + modifier repair: 3/3 PASS
V14 pending modifier repair: 4/4 PASS
V14 runtime grounding smoke: 4/4 PASS
V14 runtime sanitizer + grounding: 3/3 PASS
V14 known model failures runtime guard: 2/2 PASS
```

App smoke test:

```text
Ran 7 tests
OK
```

주요 테스트 파일:

```text
test_pending_field_modifier_v14.py
test_pending_modifier_v14.py
test_runtime_grounding_v14.py
test_runtime_sanitizer_v14.py
test_v14_known_failures_runtime.py
test_checkout_manager.py
test_order_runtime_final.py
smoke_app_v14.py
```

---

## 16. STT Regression Set

```text
stt_test_278.txt
```

최종 LLM regression에 사용된 278개 사용자 발화를 STT 테스트용으로 정리한 파일입니다.

이를 통해 동일한 문장 집합을 기준으로

```text
Speech
  ↓
STT
  ↓
LLM
  ↓
Runtime
```

전체 pipeline의 오류 위치를 비교할 수 있습니다.

---

## 17. Environment

Runtime environment snapshot:

```text
requirements-runtime.txt
```

Training environment snapshot:

```text
requirements-training.txt
```

두 파일은 개발 당시 실제 Python environment의 `pip freeze` 결과입니다.

가상환경 자체는 repository에 포함하지 않습니다.

---

## 18. Running on Another Computer

이 repository에는 소스 코드, V14 dataset, 테스트 코드 및 환경 정보가 포함되어 있지만, **V14 merged model weight 자체는 포함되어 있지 않습니다.**

최종 V14 merged model은 약 17GB이므로 GitHub repository에서 제외되어 있습니다.

따라서 다른 컴퓨터에서 실제 V14 시스템을 실행하려면 다음 두 가지가 필요합니다.

```text
1. GitHub의 soomac_asz_llm 소스 코드
2. 별도로 전달받은 V14 merged model
```

### 1. GitHub 소스 코드 준비

GitHub에서 `soomac_asz_llm`을 받은 뒤 해당 폴더로 이동합니다.

### 2. Python 가상환경 생성

```bash
python3 -m venv drive_thru_venv
source drive_thru_venv/bin/activate
pip install -r requirements-runtime.txt
```

`requirements-runtime.txt`는 개발 당시 실제 runtime 환경의 `pip freeze` snapshot입니다.

새 컴퓨터의 NVIDIA driver, CUDA, PyTorch 및 vLLM 버전에 따라 GPU 관련 package의 호환성을 별도로 확인해야 할 수 있습니다.

### 3. V14 merged model 복사

GitHub에는 model weight가 포함되어 있지 않으므로 기존 컴퓨터에서 다음 model을 별도로 복사해야 합니다.

```text
qwen35_drive_thru_v14_merged
```

새 컴퓨터에서는 다음 위치에 배치합니다.

```text
soomac_asz_llm/
└── outputs/
    └── qwen35_drive_thru_v14_merged/
```

즉 최종적으로 다음과 같은 구조가 되어야 합니다.

```text
soomac_asz_llm/
├── drive_thru_app.py
├── order_runtime_final.py
├── checkout_manager.py
├── order_schema.py
├── order_update_schema.py
├── requirements-runtime.txt
│
└── outputs/
    └── qwen35_drive_thru_v14_merged/
        ├── config.json
        ├── generation_config.json
        ├── tokenizer.json
        ├── tokenizer_config.json
        ├── model.safetensors.index.json
        ├── model-00001-of-00005.safetensors
        ├── model-00002-of-00005.safetensors
        ├── model-00003-of-00005.safetensors
        ├── model-00004-of-00005.safetensors
        └── model-00005-of-00005.safetensors
```

### 4. vLLM server 실행

```bash
VLLM_USE_FLASHINFER_SAMPLER=0 \
vllm serve \
  ./outputs/qwen35_drive_thru_v14_merged \
  --served-model-name drive-thru-v14 \
  --port 8000 \
  --tensor-parallel-size 1 \
  --max-model-len 8192 \
  --max-num-seqs 32 \
  --quantization fp8_per_tensor \
  --gpu-memory-utilization 0.90 \
  --reasoning-parser qwen3 \
  --language-model-only \
  --structured-outputs-config \
  '{"backend":"xgrammar","disable_any_whitespace":true}'
```

server 확인:

```bash
curl http://127.0.0.1:8000/v1/models
```

응답에서 다음 model name이 확인되어야 합니다.

```text
drive-thru-v14
```

### 5. Application 실행

vLLM server는 그대로 실행해 둔 상태에서 새로운 terminal을 열고 실행합니다.

```bash
source drive_thru_venv/bin/activate
python3 drive_thru_app.py
```

### Model을 전달받지 못한 경우

repository에는 V14 dataset과 training/merge script도 포함되어 있으므로 model을 다시 학습하여 생성하는 것도 가능합니다.

```text
dataset_v14/
train_qlora_v14.py
merge_v14.py
requirements-training.txt
```

다만 재학습에는 별도의 GPU 환경과 시간이 필요하므로, 대회용 컴퓨터에서는 **이미 검증된 V14 merged model을 별도로 복사하여 사용하는 것을 권장합니다.**

---

## 19. Running V14

### Start vLLM

현재 competition PC에서 사용하는 V14 serving 예시:

```bash
cd ~/drive_thru_llm
source ~/drive_thru_venv/bin/activate

VLLM_USE_FLASHINFER_SAMPLER=0 \
vllm serve \
  ~/drive_thru_llm/outputs/qwen35_drive_thru_v14_merged \
  --served-model-name drive-thru-v14 \
  --port 8000 \
  --tensor-parallel-size 1 \
  --max-model-len 8192 \
  --max-num-seqs 32 \
  --quantization fp8_per_tensor \
  --gpu-memory-utilization 0.90 \
  --reasoning-parser qwen3 \
  --language-model-only \
  --structured-outputs-config \
  '{"backend":"xgrammar","disable_any_whitespace":true}'
```

Server 확인:

```bash
curl http://127.0.0.1:8000/v1/models
```

`drive-thru-v14`가 표시되어야 합니다.

### Start Application

새 terminal:

```bash
cd ~/drive_thru_llm
source ~/drive_thru_venv/bin/activate

python3 drive_thru_app.py
```

---

## 20. Repository Structure

```text
drive_thru_llm/
├── drive_thru_app.py
├── order_runtime_final.py
├── checkout_manager.py
├── order_schema.py
├── order_update_schema.py
│
├── make_dataset_v14.py
├── train_qlora_v14.py
├── merge_v14.py
├── eval_v14.py
├── eval_v14_probe.py
├── make_v14_robustness_probe.py
│
├── dataset/
├── dataset_v3/
├── ...
├── dataset_v14/
│
├── test_*.py
├── smoke_app_v14.py
│
├── eval_v14_blind_results.jsonl
├── stt_test_278.txt
│
├── requirements-runtime.txt
├── requirements-training.txt
├── .gitignore
└── README.md
```

다음 항목은 Git repository에서 제외됩니다.

```text
outputs/
runtime_data/
legacy/
__pycache__/
.vscode/
*.log
*.bak
```

---

## 21. Integration Direction

LLM subsystem의 최종 역할은 주문을 해석하고 검증된 `FINAL HANDOFF`를 생성하는 것입니다.

전체 대회 시스템에서는 다음 구조로 통합합니다.

```text
Vehicle Detection
      ↓
STT
      ↓
Drive-Thru LLM V14
      ↓
Order Runtime
      ↓
FINAL HANDOFF
      ↓
ROS2 Task Manager
      ↓
Manipulator / Payment / Delivery
      ↓
TTS
```

로봇의 IK, trajectory planning, actuator control 및 safety control은 LLM이 아닌 ROS2 기반 deterministic control layer에서 담당합니다.

---

## Status

**LLM V14 is frozen for integration.**

Current verified status:

```text
Model-only regression:
276 / 278 (99.28%)

Known model failure runtime guard:
2 / 2 PASS

Runtime regression:
PASS

Application smoke:
7 / 7 PASS

Counter-order FINAL HANDOFF:
Verified

Mobile-pickup FINAL HANDOFF:
Verified
```

다음 개발 단계는 기존 V14를 다시 학습하는 것이 아니라 STT/TTS 및 ROS2 Task Manager와의 integration입니다.
