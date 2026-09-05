#!/usr/bin/env bash
# Starts the FastAPI backend and the Next.js frontend together.
# Ctrl+C stops both.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"

pids=()
cleanup() {
  echo ""
  echo "shutting down..."
  for pid in "${pids[@]:-}"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM

if [[ ! -f backend/.env ]]; then
  echo "!! backend/.env is missing."
  echo "   Copy the first block of .env.local.example into backend/.env and put your"
  echo "   free Groq key in GROQ_API_KEY (https://console.groq.com/keys)."
  echo ""
fi

if [[ ! -d backend/.venv ]]; then
  echo ">> creating backend venv"
  uv venv backend/.venv --python 3.12
  uv pip install --python backend/.venv/bin/python -r backend/requirements.txt
fi

if [[ ! -d frontend/node_modules ]]; then
  echo ">> installing frontend deps"
  (cd frontend && npm install)
fi

if [[ ! -f frontend/.env.local ]]; then
  echo ">> writing frontend/.env.local from the example"
  cp frontend/.env.local.example frontend/.env.local 2>/dev/null || true
fi

echo ">> backend  http://127.0.0.1:${BACKEND_PORT}"
(cd backend && .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "$BACKEND_PORT" --reload) &
pids+=($!)

echo ">> frontend http://localhost:${FRONTEND_PORT}"
(cd frontend && npm run dev -- --port "$FRONTEND_PORT") &
pids+=($!)

echo ""
echo "   open http://localhost:${FRONTEND_PORT}"
echo ""
wait
