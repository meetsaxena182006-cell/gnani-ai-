#!/bin/sh
# Container entrypoint. SAARTHI_MODE=review (default, no keys) or full.
set -e
export PORT="${PORT:-8080}"
if [ "$SAARTHI_MODE" = "full" ]; then
  export HTTP_PORT="$PORT"
  # first boot: create the synthetic demo fleets so the office and companion links work
  [ -f "${DB_PATH:-data/saarthi.db}" ] || python -m saarthi.seed --demo
  # pages work at once; each key (LLM, Gnani, Cloudflare TURN) switches its part on
  exec python -m saarthi.main --allow-missing-keys
else
  export REVIEW_PUBLIC=0
  exec python scripts/review_demo.py
fi
