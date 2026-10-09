#!/usr/bin/env bash

cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
source ~/drive_thru_venv/bin/activate

export SOOMAC_LLM_URL="http://127.0.0.1:8000/v1"
export SOOMAC_ROUTER_URL="http://127.0.0.1:8000/v1"
export SOOMAC_ROUTER_MODEL="drive-thru-v14"
export SOOMAC_ROUTER_THINKING="off"
export SOOMAC_ROUTER_TELEMETRY=0

python3 drive_thru_app.py
