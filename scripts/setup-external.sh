#!/bin/sh
# Install the independently authored crew in a project-local Python environment.

set -eu
cd "$(dirname "$0")/.."
if [ -n "${ALPHA_BASE_PYTHON:-}" ]; then
  alpha_python="$ALPHA_BASE_PYTHON"
elif command -v python3.12 >/dev/null 2>&1; then
  alpha_python="$(command -v python3.12)"
elif command -v python3.11 >/dev/null 2>&1; then
  alpha_python="$(command -v python3.11)"
else
  echo 'Provide Python 3.11 or 3.12 through ALPHA_BASE_PYTHON.' >&2
  exit 1
fi
"$alpha_python" -c 'import sys; assert (3,11) <= sys.version_info[:2] <= (3,12), "Use Python 3.11 or 3.12"'
"$alpha_python" -m venv work/crewai-venv
work/crewai-venv/bin/python -m pip install --disable-pip-version-check -r integrations/crewai-game-builder/requirements.lock
