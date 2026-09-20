#!/bin/sh
# Run the whole backend test suite against the PINNED versions (requirements.txt + requirements-dev.txt)
# in a throw-away virtualenv. Changes nothing else: the tests use their own temporary data folder,
# never the live one; the venv is removed when the script ends, however it ends.
#
#   sh backend/verify-pinned.sh                       # venv at ${TMPDIR:-/tmp}/vibehealth-verify-venv
#   VIBEHEALTH_VERIFY_VENV=/some/path sh backend/verify-pinned.sh
#
# Needs python3 (3.12 like the container) with venv, and network access to PyPI for the installs.
set -eu

VENV="${VIBEHEALTH_VERIFY_VENV:-${TMPDIR:-/tmp}/vibehealth-verify-venv}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ -e "$VENV" ]; then
    echo "refusing to reuse $VENV: it exists. Remove it, or set VIBEHEALTH_VERIFY_VENV." >&2
    exit 2
fi
trap 'rm -rf "$VENV"' EXIT INT TERM

python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install -q --upgrade pip
"$VENV/bin/python" -m pip install -q -r "$HERE/requirements-dev.txt"
"$VENV/bin/python" -m pip list 2>/dev/null | grep -i -E '^(fastapi|starlette|pillow|pypdfium2|pytest|python-multipart|sqlmodel|uvicorn|pypdf) ' || true

cd "$HERE"
# A minimal environment: none of the server's own variables (SECRET_KEY, tokens, DATA_DIR) can leak in.
env -i PATH="$VENV/bin:/usr/bin:/bin" HOME="$HOME" LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 \
    "$VENV/bin/python" -m pytest -q -p no:cacheprovider
