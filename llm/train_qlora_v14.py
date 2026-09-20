#!/usr/bin/env python3
"""V11 학습 설정을 유지한 V14 학습기.

python3 train_qlora_v14.py --inspect-only  # tokenizer로 길이만 검사
python3 train_qlora_v14.py --smoke         # 새 base + LoRA, 20 optimizer steps
python3 train_qlora_v14.py                 # 새 base + LoRA, 2 epochs

V14 blind는 평가용이며, 이 학습기는 blind 파일을 읽거나 학습에 사용하지 않는다.
학습에는 train/val만 사용하며, test/blind는 학습에 넣지 않는다.
토큰 길이는 기존 V9와 같은 chat template 호출로 측정한다.
"""

import argparse
import gc
import inspect
import json
import math
import os
import statistics
from pathlib import Path

os.environ.setdefault(
    "PYTORCH_CUDA_ALLOC_CONF",
    "expandable_segments:True",
)

import torch

from datasets import Dataset
from peft import (
    LoraConfig,
    get_peft_model,
    prepare_model_for_kbit_training,
)
from transformers import (
    AutoTokenizer,
    BitsAndBytesConfig,
    Qwen3_5Config,
    Qwen3_5ForCausalLM,
)
from trl import (
    SFTConfig,
    SFTTrainer,
)


# ============================================================
# CONFIG
# ============================================================

MODEL_ID = "Qwen/Qwen3.5-9B"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "dataset_v14"

TRAIN_PATH = DATA_DIR / "train.jsonl"
VAL_PATH = DATA_DIR / "val.jsonl"
TEST_PATH = DATA_DIR / "test.jsonl"

OUTPUT_ROOT = BASE_DIR / "outputs"

LORA_R = 16
LORA_ALPHA = 32
LORA_DROPOUT = 0.05

LORA_TARGETS = [
    "q_proj",
    "k_proj",
    "v_proj",
    "o_proj",
    "in_proj_qkv",
    "out_proj",
]

TRAIN_EPOCHS = 2.0
TRAIN_BATCH_SIZE = 1
EVAL_BATCH_SIZE = 1
GRAD_ACCUM = 8
LEARNING_RATE = 2e-4

# 토큰 길이 검사 후 아래 bucket 중 가장 작은 값을 자동 선택한다.
MAX_LENGTH_BUCKETS = [
    256,
    320,
    384,
    448,
    512,
    640,
]

SMOKE_STEPS = 20


# ============================================================
# HELPERS
# ============================================================

def gb(value: int) -> float:
    return value / (1024 ** 3)


def print_gpu_memory(title: str) -> None:
    if not torch.cuda.is_available():
        return

    free_mem, total_mem = torch.cuda.mem_get_info()

    print(f"\n===== {title} =====")
    print(f"GPU            : {torch.cuda.get_device_name(0)}")
    print(f"Total VRAM     : {gb(total_mem):.2f} GB")
    print(f"Free VRAM      : {gb(free_mem):.2f} GB")
    print(
        f"Allocated      : "
        f"{gb(torch.cuda.memory_allocated()):.2f} GB"
    )
    print(
        f"Reserved       : "
        f"{gb(torch.cuda.memory_reserved()):.2f} GB"
    )
    print(
        f"Peak allocated : "
        f"{gb(torch.cuda.max_memory_allocated()):.2f} GB"
    )


def check_local_api() -> None:
    """
    현재 설치된 TRL의 SFTConfig가 이 스크립트에서 쓰는
    인자를 실제로 지원하는지 모델을 로드하기 전에 확인한다.
    """

    required = [
        "warmup_steps",
        "lr_scheduler_type",
        "optim",
        "gradient_checkpointing",
        "gradient_checkpointing_kwargs",
        "activation_offloading",
        "torch_empty_cache_steps",
        "max_length",
        "packing",
        "completion_only_loss",
        "shuffle_dataset",
        "eval_strategy",
        "save_strategy",
    ]

    params = inspect.signature(
        SFTConfig
    ).parameters

    missing = [
        name
        for name in required
        if name not in params
    ]

    if missing:
        raise RuntimeError(
            "현재 설치된 TRL의 SFTConfig에서 "
            "지원하지 않는 인자가 있습니다:\n"
            + "\n".join(
                f"  - {name}"
                for name in missing
            )
        )

    print("SFTConfig API 확인: ✅")


def read_jsonl(path: Path):
    rows = []

    with path.open(
        "r",
        encoding="utf-8",
    ) as f:
        for line_number, line in enumerate(
            f,
            start=1,
        ):
            line = line.strip()

            if not line:
                continue

            try:
                data = json.loads(
                    line
                )
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"{path} {line_number}번째 줄 JSON 오류"
                ) from error

            rows.append(data)

    return rows


def validate_dataset_paths() -> None:
    for path in [
        TRAIN_PATH,
        VAL_PATH,
        TEST_PATH,
    ]:
        if not path.exists():
            raise FileNotFoundError(
                f"데이터 파일을 찾을 수 없습니다: {path}"
            )


def inspect_token_lengths(
    tokenizer,
    paths,
):
    """
    실제 chat template 적용 후 train/val 전체 길이를 재고,
    16GB GPU에서 불필요한 max_length 증가를 막는다.
    """

    lengths = []
    longest = []

    print("\n토큰 길이 검사...")

    for path in paths:
        rows = read_jsonl(
            path
        )

        for row in rows:
            messages = row[
                "messages"
            ]

            encoded = (
                tokenizer
                .apply_chat_template(
                    messages,
                    tokenize=True,
                    add_generation_prompt=False,
                    return_dict=True,
                )
            )

            input_ids = encoded[
                "input_ids"
            ]

            # tokenizer 구현에 따라 [ids] 또는 ids 형태 모두 대응
            if (
                input_ids
                and isinstance(
                    input_ids[0],
                    list,
                )
            ):
                length = len(
                    input_ids[0]
                )
            else:
                length = len(
                    input_ids
                )

            lengths.append(
                length
            )

            user_text = (
                messages[1]["content"]
                .split(
                    "현재 사용자 발화:\n"
                )[-1]
            )

            longest.append(
                (
                    length,
                    row.get(
                        "category",
                        "unknown",
                    ),
                    user_text,
                )
            )

    if not lengths:
        raise RuntimeError(
            "토큰 길이를 측정할 데이터가 없습니다."
        )

    ordered = sorted(
        lengths
    )

    def percentile(p):
        index = int(
            (len(ordered) - 1)
            * p
        )
        return ordered[index]

    maximum = max(
        lengths
    )

    chosen = None

    for bucket in (
        MAX_LENGTH_BUCKETS
    ):
        if maximum <= bucket:
            chosen = bucket
            break

    print()
    print("=" * 64)
    print("V14 TOKEN LENGTH")
    print("=" * 64)
    print(
        f"샘플 수 : {len(lengths)}"
    )
    print(
        f"MIN     : {min(lengths)}"
    )
    print(
        f"평균    : "
        f"{statistics.mean(lengths):.1f}"
    )
    print(
        f"P50     : "
        f"{percentile(0.50)}"
    )
    print(
        f"P90     : "
        f"{percentile(0.90)}"
    )
    print(
        f"P95     : "
        f"{percentile(0.95)}"
    )
    print(
        f"P99     : "
        f"{percentile(0.99)}"
    )
    print(
        f"MAX     : {maximum}"
    )

    for limit in (
        MAX_LENGTH_BUCKETS
    ):
        count = sum(
            length > limit
            for length in lengths
        )
        print(
            f">{limit:3d} tokens : "
            f"{count:4d}"
        )

    print()
    print("가장 긴 샘플 TOP 5")

    for length, category, text in sorted(
        longest,
        reverse=True,
    )[:5]:
        print(
            f"  {length:3d} | "
            f"{category:28s} | "
            f"{text}"
        )

    if chosen is None:
        raise RuntimeError(
            "\nV14 데이터 MAX 토큰 길이가 "
            f"{MAX_LENGTH_BUCKETS[-1]}을 초과했습니다.\n"
            f"현재 MAX={maximum}.\n"
            "16GB GPU에서 자동으로 길이를 더 올리지 않고 "
            "여기서 중단합니다."
        )

    print(
        f"\n자동 선택 max_length: "
        f"{chosen}"
    )

    return chosen


def load_dataset_file(
    path: Path,
) -> Dataset:
    """
    messages:
      system + user + assistant

    를

      prompt     = system + user
      completion = assistant

    형태로 바꿔 completion-only loss를 사용한다.
    """

    rows = []

    for line_number, data in enumerate(
        read_jsonl(path),
        start=1,
    ):
        messages = data.get(
            "messages"
        )

        if (
            not messages
            or len(messages) < 2
            or messages[-1].get(
                "role"
            ) != "assistant"
        ):
            raise ValueError(
                f"{path} "
                f"{line_number}번째 샘플의 "
                "messages 형식이 잘못되었습니다."
            )

        rows.append(
            {
                "prompt":
                    messages[:-1],

                "completion":
                    [
                        messages[-1]
                    ],
            }
        )

    return Dataset.from_list(
        rows
    )


def count_trainable(
    model,
) -> None:
    trainable = 0
    total = 0

    for parameter in (
        model.parameters()
    ):
        total += (
            parameter.numel()
        )

        if parameter.requires_grad:
            trainable += (
                parameter.numel()
            )

    print()
    print(
        "Trainable params:",
        f"{trainable:,}",
    )
    print(
        "Total params:",
        f"{total:,}",
    )

    if total:
        print(
            "Trainable ratio:",
            f"{100 * trainable / total:.6f}%",
        )


# ============================================================
# MODEL
# ============================================================

def load_model_and_tokenizer(
    tokenizer,
):
    if not torch.cuda.is_available():
        raise RuntimeError(
            "CUDA GPU를 사용할 수 없습니다."
        )

    compute_dtype = (
        torch.bfloat16
        if torch.cuda.is_bf16_supported()
        else torch.float16
    )

    print(
        "\nGPU:",
        torch.cuda.get_device_name(0),
    )
    print(
        "Compute dtype:",
        compute_dtype,
    )

    # --------------------------------
    # TEXT CONFIG
    # --------------------------------

    print(
        "\n[1] Qwen3.5 text config 로딩..."
    )

    full_config = (
        Qwen3_5Config
        .from_pretrained(
            MODEL_ID
        )
    )

    text_config = (
        full_config.text_config
    )

    text_config.use_cache = False

    # --------------------------------
    # 4-BIT NF4
    # --------------------------------

    quant_config = (
        BitsAndBytesConfig(
            load_in_4bit=True,

            bnb_4bit_quant_type=(
                "nf4"
            ),

            bnb_4bit_compute_dtype=(
                compute_dtype
            ),

            bnb_4bit_use_double_quant=(
                True
            ),
        )
    )

    print(
        "[2] Qwen3.5-9B Text-only "
        "4-bit NF4 로딩..."
    )

    model = (
        Qwen3_5ForCausalLM
        .from_pretrained(
            MODEL_ID,

            config=text_config,

            quantization_config=(
                quant_config
            ),

            device_map={
                "": 0
            },

            dtype=(
                compute_dtype
            ),
        )
    )

    model.config.use_cache = False

    print_gpu_memory(
        "4-bit Base Model 로드 후"
    )

    # --------------------------------
    # K-BIT TRAINING
    # --------------------------------

    print(
        "\n[3] k-bit training 준비..."
    )

    model = (
        prepare_model_for_kbit_training(
            model,
            use_gradient_checkpointing=True,
        )
    )

    model.config.use_cache = False

    # --------------------------------
    # NEW LoRA
    # --------------------------------
    #
    # 중요:
    # 기존 V9/V10 adapter를 불러오지 않는다.
    # V14 전체 데이터로 base model에서 새로운 adapter를 학습한다.
    # --------------------------------

    print(
        "[4] 새로운 V14 LoRA Adapter 부착..."
    )

    lora_config = (
        LoraConfig(
            r=LORA_R,
            lora_alpha=LORA_ALPHA,
            lora_dropout=(
                LORA_DROPOUT
            ),
            bias="none",
            task_type="CAUSAL_LM",
            target_modules=(
                LORA_TARGETS
            ),
        )
    )

    gc.collect()
    torch.cuda.empty_cache()

    model = (
        get_peft_model(
            model,
            lora_config,
            autocast_adapter_dtype=False,
        )
    )

    model.config.use_cache = False

    count_trainable(
        model
    )

    print_gpu_memory(
        "V14 LoRA 부착 후"
    )

    return (
        model,
        compute_dtype,
    )


# ============================================================
# TRAIN
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser()

    mode_group = parser.add_mutually_exclusive_group()

    mode_group.add_argument(
        "--smoke",
        action="store_true",
        help="20 optimizer step smoke test",
    )

    mode_group.add_argument(
        "--inspect-only",
        action="store_true",
        help="tokenizer로 train/val 길이만 검사하고 종료 (모델 가중치/CUDA 불필요)",
    )

    args = parser.parse_args()
    smoke = args.smoke

    print(
        "=" * 72
    )

    if args.inspect_only:
        print("Qwen3.5-9B V14 TOKEN INSPECTION ONLY")
    elif smoke:
        print(
            "Qwen3.5-9B QLoRA V14 "
            "SMOKE TEST"
        )
    else:
        print(
            "Qwen3.5-9B QLoRA V14 "
            "FULL TRAINING"
        )

    print(
        "=" * 72
    )

    validate_dataset_paths()

    if not args.inspect_only:
        check_local_api()
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA GPU를 사용할 수 없습니다.")
        gc.collect()
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()

    # ========================================================
    # TOKENIZER + LENGTH CHECK
    # ========================================================

    print(
        "\nTokenizer 로딩..."
    )

    tokenizer = (
        AutoTokenizer
        .from_pretrained(
            MODEL_ID
        )
    )

    if tokenizer.pad_token is None:
        tokenizer.pad_token = (
            tokenizer.eos_token
        )

    tokenizer.padding_side = (
        "right"
    )

    max_length = (
        inspect_token_lengths(
            tokenizer,
            [
                TRAIN_PATH,
                VAL_PATH,
            ],
        )
    )

    if args.inspect_only:
        print("\n토큰 검사 완료. 모델 가중치를 로드하거나 학습하지 않았습니다.")
        print(f"학습에 자동 적용될 max_length: {max_length}")
        print("학습 전 vLLM을 종료하고 nvidia-smi로 GPU 메모리를 확인하세요.")
        print("다음: python3 train_qlora_v14.py --smoke")
        return

    # ========================================================
    # DATASET
    # ========================================================

    print(
        "\nDataset V14 로딩..."
    )

    train_dataset = (
        load_dataset_file(
            TRAIN_PATH
        )
    )

    val_dataset = (
        load_dataset_file(
            VAL_PATH
        )
    )

    print(
        "train:",
        len(train_dataset),
    )
    print(
        "val  :",
        len(val_dataset),
    )
    print(
        "max_length:",
        max_length,
    )

    # ========================================================
    # MODEL
    # ========================================================

    model, compute_dtype = (
        load_model_and_tokenizer(
            tokenizer
        )
    )

    # ========================================================
    # OUTPUT
    # ========================================================

    if smoke:
        output_dir = (
            OUTPUT_ROOT
            / "qwen35_drive_thru_v14_smoke"
        )
    else:
        output_dir = (
            OUTPUT_ROOT
            / "qwen35_drive_thru_qlora_v14"
        )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # ========================================================
    # WARMUP
    # ========================================================

    if smoke:
        warmup_steps = 1
    else:
        optimizer_steps_per_epoch = (
            math.ceil(
                len(train_dataset)
                / (
                    TRAIN_BATCH_SIZE
                    * GRAD_ACCUM
                )
            )
        )

        total_optimizer_steps = (
            math.ceil(
                optimizer_steps_per_epoch
                * TRAIN_EPOCHS
            )
        )

        warmup_steps = max(
            1,
            round(
                total_optimizer_steps
                * 0.05
            ),
        )

        print()
        print(
            "예상 optimizer steps:",
            total_optimizer_steps,
        )
        print(
            "warmup_steps (5%):",
            warmup_steps,
        )

    # ========================================================
    # SFT CONFIG
    # ========================================================

    common_kwargs = dict(
        output_dir=str(
            output_dir
        ),

        per_device_train_batch_size=(
            TRAIN_BATCH_SIZE
        ),

        gradient_accumulation_steps=(
            GRAD_ACCUM
        ),

        learning_rate=(
            LEARNING_RATE
        ),

        warmup_steps=(
            warmup_steps
        ),

        lr_scheduler_type=(
            "cosine"
        ),

        optim=(
            "paged_adamw_8bit"
        ),

        weight_decay=0.0,

        max_grad_norm=1.0,

        bf16=(
            compute_dtype
            == torch.bfloat16
        ),

        fp16=(
            compute_dtype
            == torch.float16
        ),

        gradient_checkpointing=True,

        gradient_checkpointing_kwargs={
            "use_reentrant": False,
        },

        activation_offloading=True,

        max_length=(
            max_length
        ),

        packing=False,

        completion_only_loss=True,

        shuffle_dataset=True,

        logging_first_step=True,

        report_to="none",

        seed=42,
        data_seed=42,
    )

    if smoke:
        training_args = (
            SFTConfig(
                **common_kwargs,

                max_steps=(
                    SMOKE_STEPS
                ),

                logging_steps=1,

                torch_empty_cache_steps=5,

                eval_strategy="no",

                save_strategy="no",
            )
        )
    else:
        training_args = (
            SFTConfig(
                **common_kwargs,

                num_train_epochs=(
                    TRAIN_EPOCHS
                ),

                per_device_eval_batch_size=(
                    EVAL_BATCH_SIZE
                ),

                logging_steps=5,

                torch_empty_cache_steps=10,

                eval_strategy="epoch",

                save_strategy="epoch",

                save_total_limit=2,
            )
        )

    # ========================================================
    # TRAINER
    # ========================================================

    print(
        "\nSFTTrainer 생성..."
    )

    trainer = (
        SFTTrainer(
            model=model,

            args=(
                training_args
            ),

            train_dataset=(
                train_dataset
            ),

            eval_dataset=(
                None
                if smoke
                else val_dataset
            ),

            processing_class=(
                tokenizer
            ),
        )
    )

    print_gpu_memory(
        "trainer.train() 직전"
    )

    print(
        "\n학습 시작..."
    )

    result = (
        trainer.train()
    )

    print()
    print(
        "=" * 72
    )
    print(
        "학습 완료"
    )
    print(
        "=" * 72
    )
    print(
        result
    )

    print_gpu_memory(
        "학습 완료 후"
    )

    # ========================================================
    # SAVE
    # ========================================================

    final_dir = (
        output_dir
        / "final_adapter"
    )

    trainer.save_model(
        str(
            final_dir
        )
    )

    tokenizer.save_pretrained(
        str(
            final_dir
        )
    )

    print()
    print(
        "Adapter 저장 완료:"
    )
    print(
        final_dir
    )

    if smoke:
        print()
        print(
            "✅ V14 smoke test 완료"
        )
        print(
            "다음 본 학습:"
        )
        print(
            "python3 train_qlora_v14.py"
        )
    else:
        print()
        print(
            "✅ V14 전체 QLoRA 학습 완료"
        )


if __name__ == "__main__":
    main()
