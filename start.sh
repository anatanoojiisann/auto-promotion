#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ -n "${AUTO_PROMOTION_PYTHON:-}" ]; then
  AP_PYTHON="$AUTO_PROMOTION_PYTHON"
elif [ -x .venv/bin/python ]; then
  AP_PYTHON=.venv/bin/python
else
  AP_PYTHON=python3
fi
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$AP_PYTHON" -m auto_promotion.cli "$@"
