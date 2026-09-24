#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python -m workbench local-network "$@"
fi
exec python3 -m workbench local-network "$@"
