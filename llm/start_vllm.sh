#!/usr/bin/env bash

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
