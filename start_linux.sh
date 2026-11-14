#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then python3 -m venv .venv; fi
source .venv/bin/activate
pip install -r requirements.txt
export DEMO_MODE=1 DEMO_OTP=1 EDITOR_PASSWORD=change-this-before-deployment
exec uvicorn app.main:app --host 127.0.0.1 --port 8000
