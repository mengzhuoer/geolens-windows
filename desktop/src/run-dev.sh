#!/usr/bin/env bash
# Dev runners for the bundled native stack.
set -e
source "$(dirname "$0")/env-dev.sh"
export PYTHONPATH="$APP/backend;$ROOT/src"
cd "$APP/backend"
case "${1:-api}" in
  api|worker|titiler)
    exec "$ROOT/runtime/py/python.exe" "$ROOT/src/server_entry.py" "$1"
    ;;
  *)
    echo "usage: $0 api|worker|titiler" >&2; exit 2
    ;;
esac
