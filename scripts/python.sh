#!/bin/sh
# npm is a convenience wrapper; the experiment engine needs only Python 3.11+.

set -eu
alpha_core_python="${ALPHA_CORE_PYTHON:-python3}"
"$alpha_core_python" -c 'import sys; assert sys.version_info >= (3, 11), "Alpha requires Python 3.11 or newer"'
exec "$alpha_core_python" "$@"
