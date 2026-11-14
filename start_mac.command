#!/usr/bin/env bash
# Двойной клик в Finder запускает NewsRadar и открывает его в браузере.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  uv venv --python 3.12 .venv
  uv pip install --python .venv/bin/python -r requirements.txt
fi
export DEMO_MODE=1 DEMO_OTP=1 EDITOR_PASSWORD=change-this-before-deployment
(sleep 2 && open http://127.0.0.1:8000/) &
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
