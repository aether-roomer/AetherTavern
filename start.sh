#!/usr/bin/env bash
# Restart the AetherTavern server: stops the previous instance recorded in
# the pidfile (if any) and exec's a fresh uvicorn in its place.
set -euo pipefail

cd "$(dirname "$0")"

HOST="${AETHER_HOST:-127.0.0.1}"
PORT="${AETHER_PORT:-8000}"
DATA_DIR="${AETHER_DATA_DIR:-data}"
PIDFILE="${DATA_DIR}/server.pid"

mkdir -p "$DATA_DIR"

# Stop the previous instance if its pidfile points at a live process. We
# only target what we previously launched, so unrelated processes that
# happen to bind the port are left alone.
if [ -f "$PIDFILE" ]; then
  old_pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
    kill "$old_pid" 2>/dev/null || true
    # Wait up to ~5s for graceful shutdown, then SIGKILL.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$old_pid" 2>/dev/null || break
      sleep 0.5
    done
    if kill -0 "$old_pid" 2>/dev/null; then
      kill -9 "$old_pid" 2>/dev/null || true
    fi
  fi
  rm -f "$PIDFILE"
fi

# ``exec`` replaces this shell with uvicorn, so ``$$`` (recorded just below)
# stays valid as the running server's pid. We sync to ensure the pidfile is
# on disk before the process image is swapped out.
echo "$$" > "$PIDFILE"
sync

exec uv run python -m server --host "$HOST" --port "$PORT"
