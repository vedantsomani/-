#!/bin/sh
# Container entrypoint: make the (possibly root-owned, freshly mounted) volume writable by the
# unprivileged "app" user, then run the command as that user.
set -eu

DATA_DIR=/data
CACHE_DIR="${SPHEREX_CACHE_DIR:-$DATA_DIR/cache}"

if [ "$(id -u)" = "0" ]; then
  mkdir -p "$CACHE_DIR"
  # Recursive chown only when ownership is wrong (first boot on a new volume), not on every start.
  if [ "$(stat -c %u "$DATA_DIR")" != "10001" ] || [ "$(stat -c %u "$CACHE_DIR")" != "10001" ]; then
    chown -R app:app "$DATA_DIR"
  fi
  if command -v setpriv >/dev/null 2>&1; then
    exec setpriv --reuid=app --regid=app --init-groups "$@"
  elif command -v runuser >/dev/null 2>&1; then
    exec runuser -u app -- "$@"
  fi
  echo "WARNING: neither setpriv nor runuser found; running as root" >&2
fi

exec "$@"
