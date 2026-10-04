#!/bin/sh
# Start commcut.
#
#     ./run.sh
#
# One thing, on purpose. This finds the virtual environment install_deps.sh
# built, runs main.py with it, and hands the process over -- exec, not a
# subshell -- so the app is the process and Ctrl-C reaches commcut rather than
# a wrapper holding it open.
#
# It will not create the environment for you. A first run that quietly
# downloaded PySide6 would put a four-minute wait and a pip error message
# inside window-startup, with no terminal to read either in; install_deps.sh is
# where that work belongs and it can print what it is doing.
#
# Arguments are passed through to main.py, which forwards them to Qt. It reads
# none of them itself: there is no --window flag and no per-window entry point.

set -eu

cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

VENV_PYTHON=".venv/bin/python"

if [ ! -x "$VENV_PYTHON" ]; then
    cat >&2 <<'MISSING_VENV'
[commcut] There is no virtual environment in this folder, so there is nothing
[commcut] to run commcut with.

[commcut] Install the dependencies first:

[commcut]     ./install_deps.sh

[commcut] It builds one in this folder and tells you what else is needed.
MISSING_VENV
    exit 1
fi

exec "$VENV_PYTHON" main.py "$@"
