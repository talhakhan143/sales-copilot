#!/usr/bin/env bash
# Opens a public address so Twilio can reach this machine, and writes it into
# backend/.env for you. Free, and it needs no account.
#
# The address changes every time you run this, which is why the script edits
# .env itself. Leave this running while you make calls. Ctrl+C stops it.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$(mktemp -t cftunnel)"

if ! command -v cloudflared >/dev/null; then
  echo "!! cloudflared is not installed. Run:  brew install cloudflared"
  exit 1
fi

cleanup() {
  echo ""
  echo "tunnel stopped. The address in backend/.env will not work until you run this again."
  [[ -n "${TUNNEL_PID:-}" ]] && kill "$TUNNEL_PID" 2>/dev/null
  exit 0
}
trap cleanup INT TERM

echo ">> opening a public address..."
cloudflared tunnel --url http://localhost:8000 > "$LOG" 2>&1 &
TUNNEL_PID=$!

URL=""
for _ in $(seq 1 40); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1)"
  [[ -n "$URL" ]] && break
  sleep 1
done

if [[ -z "$URL" ]]; then
  echo "!! could not get an address. Last few lines:"
  tail -12 "$LOG"
  kill "$TUNNEL_PID" 2>/dev/null
  exit 1
fi

python3 - "$URL" <<'PY'
import pathlib, re, sys
url = sys.argv[1]
p = pathlib.Path(__file__).resolve().parent if False else pathlib.Path("backend/.env")
if not p.exists():
    print("!! backend/.env not found. Copy .env.local.example to backend/.env first.")
    raise SystemExit(1)
s = p.read_text()
if re.search(r"^PUBLIC_BASE_URL=", s, re.M):
    s = re.sub(r"^PUBLIC_BASE_URL=.*$", f"PUBLIC_BASE_URL={url}", s, flags=re.M)
else:
    s = s.rstrip("\n") + f"\nPUBLIC_BASE_URL={url}\n"
p.write_text(s)
print(f">> PUBLIC_BASE_URL is now {url}")
PY

echo ">> restart the backend so it reads the new address, then keep this window open"
echo ""
wait "$TUNNEL_PID"
