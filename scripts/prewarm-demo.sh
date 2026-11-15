#!/bin/sh
# Pre-build the demo fields inside the deployed container so the live demo is instant.
# POSIX sh twin of scripts/prewarm-demo.mjs; in the image it lives at /app/prewarm-demo.sh.
#
#   fly ssh console -C "sh /app/prewarm-demo.sh"                 one target at a time (default)
#   fly ssh console -C "sh /app/prewarm-demo.sh --parallel"      all targets at once (needs RAM)
#   sh prewarm-demo.sh --fov 10 [targets-file]                   other FOV / other targets file
#
# Targets file: one "RA Dec" per line, '#' comments. Default: demo-targets.txt next to this script.
set -eu

# `fly ssh console` logs in as root; files created by root would not be writable by the web
# server (which runs as "app"), so re-run this script as that user.
if [ "$(id -u)" = "0" ] && id app >/dev/null 2>&1 && command -v setpriv >/dev/null 2>&1; then
  export HOME=/home/app
  exec setpriv --reuid=app --regid=app --init-groups sh "$0" "$@"
fi

HERE=$(cd "$(dirname "$0")" && pwd)
FILE="$HERE/demo-targets.txt"
FOV=""
PARALLEL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --parallel) PARALLEL=1 ;;
    --fov) shift; FOV="${1:?--fov needs arcmin}" ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) FILE="$1" ;;
  esac
  shift
done

export SPHEREX_CACHE_DIR="${SPHEREX_CACHE_DIR:-/data/cache}"
# Inside the image `app/` sits next to this script; in the repo it is ../backend/app.
if [ -d "$HERE/app" ]; then cd "$HERE"; else cd "$HERE/../backend"; fi

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
sed -e 's/\r$//' -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$FILE" | grep -v '^$' > "$TMP" || true
[ -s "$TMP" ] || { echo "No targets in $FILE" >&2; exit 2; }
echo "cache: $SPHEREX_CACHE_DIR"

if [ "$PARALLEL" = "1" ]; then
  set --
  while IFS= read -r t; do set -- "$@" "$t"; done < "$TMP"
  exec python -m app.prewarm ${FOV:+--fov "$FOV"} "$@"
fi

FAILED=0
while IFS= read -r t; do
  echo ">>> $t"
  python -m app.prewarm ${FOV:+--fov "$FOV"} "$t" </dev/null || { echo "FAILED: $t" >&2; FAILED=1; }
done < "$TMP"
exit "$FAILED"
