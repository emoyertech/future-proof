#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared is required. Install with: brew install cloudflared"
  exit 1
fi

PORT="${1:-8080}"

echo "Starting Notes Hub in cloud mode on port ${PORT}..."
python3 notes0.py --cloud --port "${PORT}"
