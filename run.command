#!/bin/zsh
set -euo pipefail
APP_DIR="${0:A:h}"
HIGGS_DIR="${HIGGS_LOCAL_DIR:-${APP_DIR:h}/knowit/apps/higgs-tts-dashboard}"
cd "$APP_DIR"
if [[ -f "$APP_DIR/.venv/bin/activate" ]]; then
  source "$APP_DIR/.venv/bin/activate"
elif [[ -f "$HIGGS_DIR/.venv/bin/activate" ]]; then
  source "$HIGGS_DIR/.venv/bin/activate"
else
  echo "No Directors Room or Higgs virtualenv found." >&2
  exit 1
fi
mkdir -p logs

HIGGS_URL="${HIGGS_ENDPOINT:-http://127.0.0.1:8765}"
if [[ -z "${HIGGS_ENDPOINT:-}" && -f config.local.json ]]; then
  HIGGS_URL="$(python -c 'import json; print(json.load(open("config.local.json"))["higgs_endpoint"])')"
fi

if [[ "$HIGGS_URL" == "http://127.0.0.1:8765" ]] && ! curl -fsS --max-time 2 "$HIGGS_URL/api/status" >/dev/null 2>&1; then
  nohup python "$HIGGS_DIR/server.py" --host 127.0.0.1 --port 8765 >logs/higgs-backend.log 2>&1 &
  disown
  for attempt in {1..40}; do
    if curl -fsS --max-time 2 "$HIGGS_URL/api/status" >/dev/null 2>&1; then
      break
    fi
    sleep 0.25
  done
fi

python server.py --host 127.0.0.1 --port 8767 --higgs-endpoint "$HIGGS_URL"
