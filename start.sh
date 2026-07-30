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

# Resolve the pidfile to a process that still looks like the launcher we
# started, echoing its pid. A pidfile left behind by an unclean exit -- a
# killed terminal, a power cut -- can name an unrelated process by the time we
# read it, and we would otherwise signal whatever inherited the number. The
# ``exec`` below means the recorded pid belongs to uv itself, so its name is
# what we check against. macOS reports the full path where Linux reports the
# bare name, hence the basename.
tracked_pid() {
  local pidfile="$1" pid comm
  [ -f "$pidfile" ] || return 1
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  case "$pid" in
    '' | *[!0-9]*) return 1 ;;
  esac
  kill -0 "$pid" 2>/dev/null || return 1
  comm="$(ps -p "$pid" -o comm= 2>/dev/null || true)"
  [ "$(basename -- "${comm:-/}")" = "uv" ] || return 1
  printf '%s\n' "$pid"
}

# Stop the previous instance, so unrelated processes that happen to bind the
# port are left alone.
if old_pid="$(tracked_pid "$PIDFILE")"; then
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

# ``exec`` replaces this shell with uvicorn, so ``$$`` (recorded just below)
# stays valid as the running server's pid. We sync to ensure the pidfile is
# on disk before the process image is swapped out.
echo "$$" > "$PIDFILE"
sync

# Extra arguments go on to ``python -m server`` (--no-evade-used-port,
# --proxy-rules, ...). argparse honours the last occurrence of an option, so
# passing --port or --host here overrides the environment defaults above.
exec uv run python -m server --host "$HOST" --port "$PORT" "$@"
