#!/bin/sh
# Run one aoa service with the repo's .env loaded. launchd cannot source a .env file, so it calls this.
#   deploy/run.sh temporal|worker|server|slack-bridge
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ -f .env ]; then set -a; . ./.env; set +a; fi
mkdir -p "${AOA_DATA_DIR:-$ROOT/data}/logs"
PY="$ROOT/.venv/bin/python"
case "${1:-}" in
  temporal) exec temporal server start-dev --db-filename "${AOA_DATA_DIR:-$ROOT/data}/temporal.db" --ip 127.0.0.1 ;;
  worker) exec "$PY" -m aoa.worker ;;
  server) exec "$ROOT/.venv/bin/uvicorn" aoa.server:app --host 127.0.0.1 --port 8000 ;;
  slack-bridge) exec "$PY" -m aoa.slack_bridge ;;
  *) echo "usage: $0 temporal|worker|server|slack-bridge" >&2; exit 2 ;;
esac
