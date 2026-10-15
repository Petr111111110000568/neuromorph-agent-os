#!/usr/bin/env bash
# openKylin 3.0 optional bootstrap. Default mode is inspection only.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:---check}"
PY="${PYTHON_BIN:-python3}"
case "$MODE" in
  --check)
    "$PY" "$HERE/blackwall.py" doctor "${@:2}"
    ;;
  --install-base)
    if ! grep -qi '^ID=.*openkylin' /etc/os-release; then
      echo 'Refusing OS package installation on non-openKylin host' >&2; exit 2
    fi
    command -v apt-get >/dev/null || { echo 'apt-get unavailable' >&2; exit 2; }
    echo 'Installing only OS tools: git, tmux, python3-venv. No agent autoexecution.'
    sudo apt-get update
    sudo apt-get install -y git tmux python3-venv
    ;;
  --install-clawteam)
    # Download only published wheel with a verified SHA-256 checksum. The
    # transitive dependencies use normal pip resolution; review them separately.
    "$PY" -m venv "${XDG_DATA_HOME:-$HOME/.local/share}/blackwall/clawteam-venv"
    VENV="${XDG_DATA_HOME:-$HOME/.local/share}/blackwall/clawteam-venv"
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    "$VENV/bin/python" -m pip download --no-deps --only-binary=:all: --dest "$TMP" 'clawteam==0.2.0'
    echo '635f7125490067ce5838a511bf2fce02716d2d552806ceb89854d1c30ddcee3d  '"$TMP/clawteam-0.2.0-py3-none-any.whl" | sha256sum -c -
    "$VENV/bin/python" -m pip install "$TMP/clawteam-0.2.0-py3-none-any.whl"
    echo "ClawTeam installed in $VENV; no team, permissions or agents activated."
    ;;
  *)
    echo 'Usage: bootstrap-openkylin.sh [--check [doctor args] | --install-base | --install-clawteam]' >&2
    exit 2
    ;;
esac
