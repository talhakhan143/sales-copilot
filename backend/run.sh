#!/usr/bin/env bash
# Start the Sales Copilot backend on http://0.0.0.0:8000 with reload.
# Creates the virtualenv and installs requirements on first run.
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

VENV_DIR=".venv"
PY="${VENV_DIR}/bin/python"
FRESH_VENV=0

if [ ! -x "${PY}" ]; then
  echo "[run.sh] no virtualenv at ${VENV_DIR}, creating one"
  if ! command -v uv >/dev/null 2>&1; then
    echo "[run.sh] uv is not installed. Install it with:"
    echo "         curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
  fi
  uv venv "${VENV_DIR}" --python 3.12
  FRESH_VENV=1
fi

if [ "${FRESH_VENV}" -eq 1 ]; then
  echo "[run.sh] installing requirements"
  uv pip install --python "${PY}" -r requirements.txt
fi

if [ -z "${GROQ_API_KEY:-}" ] && [ ! -f ".env" ]; then
  echo ""
  echo "  ****************************************************************"
  echo "  *  GROQ_API_KEY is not set and there is no backend/.env file.  *"
  echo "  *  The server will start, but transcription and suggestions    *"
  echo "  *  will be disabled until you add a key.                       *"
  echo "  *                                                              *"
  echo "  *  Get a free key at https://console.groq.com/keys then run:   *"
  echo "  *      cp .env.example .env                                    *"
  echo "  *  and paste the key into GROQ_API_KEY.                        *"
  echo "  ****************************************************************"
  echo ""
fi

echo "[run.sh] starting uvicorn on http://0.0.0.0:8000"
exec "${PY}" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
