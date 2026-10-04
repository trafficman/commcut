#!/bin/sh
# commcut, for double-clicking on macOS.
#
# Finder runs a .command file by opening Terminal with it, and leaves the
# Terminal window on screen afterwards. That is the whole reason this file
# exists rather than run.sh alone: a user who double-clicks and gets an error
# needs to still be able to read it once the window would have closed.
#
# So this runs run.sh, keeps the status, says what happened in one line, waits
# for a keypress, and exits with the status run.sh gave. It deliberately does
# not exec -- an exec replaces this shell, and there would be nothing left to
# print or to wait with.
#
# There is nothing macOS-specific below that line. On a machine where this is
# run from Terminal instead of double-clicked, it behaves the same way.

set -u

cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

if sh ./run.sh "$@"; then
    status=0
    summary="commcut closed."
else
    status=$?
    summary="commcut could not start (exit $status)."
fi

printf '\n[commcut] %s\n' "$summary"
printf '[commcut] Press return to close this window.'
read -r _ || true

exit "$status"
