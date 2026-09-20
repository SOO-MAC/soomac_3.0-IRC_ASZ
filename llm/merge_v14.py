#!/usr/bin/env python3
import argparse
import gc
import shutil
from pathlib import Path

import torch
from peft import PeftModel
from transformers import AutoTokenizer, Qwen3_5Config, Qwen3_5ForCausalLM

MODEL_ID = "Qwen/Qwen3.5-9B"
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ADAPTER_DIR = BASE_DIR / "outputs" / "qwen35_drive_thru_qlora_v14" / "final_adapter"
DEFAULT_OUTPUT_DIR = BASE_DIR / "outputs" / "qwen35_drive_thru_v14_merged"

def ensure_adapter(adapter_dir: Path):
    if not (adapter_dir / "adapter_config.json").exists():
        raise FileNotFoundError(f"adapter_config.json 없음: {adapter_dir}")
    if not any((adapter_dir / x).exists() for x in ("adapter_model.safetensors", "adapter_model.bin")):
        raise FileNotFoundError(f"adapter weight 없음: {adapter_dir}")

def prepare_output(output_dir: Path, force: bool):
    if output_dir.exists() and any(output_dir.iterdir()):
        if not force:
            raise RuntimeError(f"출력 폴더가 이미 존재합니다: {output_dir}\n덮어쓰려면 --force")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter", type=Path, default=DEFAULT_ADAPTER_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    adapter_dir = args.adapter.expanduser().resolve()
    output_dir = args.output.expanduser().resolve()

    print("=" * 72)
    print("SOOMAC DRIVE-THRU V14 MERGE")
    print("=" * 72)
    print("Base model :", MODEL_ID)
    print("Adapter    :", adapter_dir)
    print("Output     :", output_dir)

    ensure_adapter(adapter_dir)
    prepare_output(output_dir, args.force)

    print("\n[1/5] Qwen3.5 text config 로딩...")
    full_config = Qwen3_5Config.from_pretrained(MODEL_ID)
    text_config = full_config.text_config
    text_config.use_cache = True

    print("[2/5] Base model BF16 CPU 로딩...")
    base_model = Qwen3_5ForCausalLM.from_pretrained(
        MODEL_ID,
        config=text_config,
        dtype=torch.bfloat16,
        low_cpu_mem_usage=True,
        device_map={"": "cpu"},
    )

    print("[3/5] V14 LoRA adapter 로딩...")
    peft_model = PeftModel.from_pretrained(
        base_model,
        str(adapter_dir),
        is_trainable=False,
    )

    print("[4/5] merge_and_unload...")
    merged_model = peft_model.merge_and_unload(safe_merge=True)
    merged_model.eval()
    merged_model.config.use_cache = True

    del peft_model
    del base_model
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    print("[5/5] merged model 저장...")
    merged_model.save_pretrained(
        str(output_dir),
        safe_serialization=True,
        max_shard_size="4GB",
    )

    tokenizer_source = adapter_dir if (
        (adapter_dir / "tokenizer_config.json").exists()
        or (adapter_dir / "tokenizer.json").exists()
    ) else MODEL_ID

    tokenizer = AutoTokenizer.from_pretrained(str(tokenizer_source))
    tokenizer.save_pretrained(str(output_dir))

    if not (output_dir / "config.json").exists():
        raise RuntimeError("config.json 저장 실패")

    weights = list(output_dir.glob("*.safetensors"))
    if not weights:
        raise RuntimeError("safetensors 저장 실패")

    print("\n" + "=" * 72)
    print("✅ V14 MERGE 완료")
    print("Output:", output_dir)
    print("safetensors:", len(weights))
    print("=" * 72)

if __name__ == "__main__":
    main()
