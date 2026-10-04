#!/bin/sh
# Install commcut's Python dependencies and check the ones it cannot install.
#
#     ./install_deps.sh              # Python environment, then report on the rest
#     ./install_deps.sh --brew       # ...and run brew install first, on macOS
#
# Two halves, on purpose.
#
# The Python half this script owns: it finds a Python new enough to run the
# pinned PySide6, builds a virtual environment in this folder, and installs
# requirements.txt into it. The virtual environment is not a convenience. PEP
# 668 makes `pip install` into a Homebrew or Debian/Ubuntu system interpreter an
# error rather than a warning, so without one this script would have nothing to
# do but explain that.
#
# The rest it only reports: ffmpeg, ffprobe, libmpv and libx264 are installed by
# a package manager, and this script will not run one for you. It will not run
# sudo either. Instead it asks the app's own resolvers -- the same functions
# the editor asks at run time -- and prints what they said, which names every
# folder searched and every command that would help.
#
# That means this script cannot disagree with the app about what is missing.
# A second, shell-side implementation of the search order would be a second
# answer to the same question, and it would be wrong the day someone added a
# platform.
#
# What it writes: .venv/ beside this script. Nothing else in this folder is
# modified, and nothing outside it is touched at all.

set -eu

cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

VENV_DIR=".venv"
REQUIREMENTS="requirements.txt"
MIN_PYTHON="3.10"

say() {
    printf '[commcut] %s\n' "$1"
}

fail() {
    printf '\n[commcut] %s\n' "$1" >&2
    exit 1
}

usage() {
    cat <<'USAGE'
usage: ./install_deps.sh [--brew]

  --brew   Run `brew install python ffmpeg mpv` first, if Homebrew is
           installed. macOS only, and only on this flag -- nothing is ever
           installed from a package manager without asking.
USAGE
}

want_brew=0
while [ $# -gt 0 ]; do
    case "$1" in
        --brew) want_brew=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail "Unknown argument: $1" ;;
    esac
    shift
done

# --------------------------------------------------------------------------
# Platform
# --------------------------------------------------------------------------

OS="$(uname -s)"
case "$OS" in
    Darwin) PLATFORM="macos" ;;
    Linux)  PLATFORM="linux" ;;
    *) fail "commcut ships source releases for macOS and Linux. This machine
       reports '$OS', which is neither. The Windows build is a download from
       the releases page." ;;
esac
say "platform $PLATFORM ($OS)"

# --------------------------------------------------------------------------
# System packages, only on request
# --------------------------------------------------------------------------

if [ "$want_brew" -eq 1 ]; then
    if [ "$PLATFORM" != "macos" ]; then
        fail "--brew is a macOS thing; there is no Homebrew on $PLATFORM."
    fi
    if ! command -v brew >/dev/null 2>&1; then
        fail "--brew was given but Homebrew is not installed. See
       https://brew.sh, or run this script without the flag."
    fi
    say "installing python, ffmpeg and mpv with Homebrew (this takes a while)"
    brew install python ffmpeg mpv
fi

# --------------------------------------------------------------------------
# A Python new enough
# --------------------------------------------------------------------------

# requirements.txt says 3.11, and that pin is real: a PyInstaller output is not
# portable across PySide6 minor versions, so the Windows build is pinned to the
# interpreter it was built with. The *code* needs less -- there is no 3.11-only
# syntax anywhere in the tree -- so what is enforced here is the floor PySide6
# itself has, and the check below reports either way.
python_ok() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
        >/dev/null 2>&1
}

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    if [ "$PLATFORM" = "macos" ]; then
        fail "No Python 3.10 or newer found on PATH.

macOS ships 3.9, and commcut needs 3.10 or newer for PySide6. Install one with
Homebrew:

    brew install python@3.11

then run this script again."
    fi
    fail "No Python 3.10 or newer found on PATH. On Debian or Ubuntu:

    sudo apt install python3 python3-venv

(the python3-venv part is not optional -- without it python3 -m venv builds a
virtual environment with no pip in it, and this script cannot tell you that
anyway.)"
fi

say "python: $PYTHON ($("$PYTHON" -c 'import platform; print(platform.python_version())'))"

# --------------------------------------------------------------------------
# The virtual environment
# --------------------------------------------------------------------------

if [ ! -x "$VENV_DIR/bin/python" ]; then
    say "creating $VENV_DIR"
    if ! "$PYTHON" -m venv "$VENV_DIR"; then
        fail "python3 -m venv failed. On Debian or Ubuntu that usually means the
       python3-venv package is missing:

    sudo apt install python3-venv"
    fi
else
    say "reusing $VENV_DIR"
fi

VENV_PYTHON="$VENV_DIR/bin/python"

say "installing $REQUIREMENTS (about 400 MB, mostly PySide6)"
"$VENV_PYTHON" -m pip install --upgrade pip >/dev/null
"$VENV_PYTHON" -m pip install --requirement "$REQUIREMENTS"

# --------------------------------------------------------------------------
# What this script cannot install
# --------------------------------------------------------------------------

# The check runs the app's own resolvers rather than reimplementing them, so
# there is one answer to "is ffmpeg installed" and it is the app's. Each is
# asked independently and none aborts the others: a missing ffmpeg and a
# missing libmpv are two problems, and a user fixing this should see both at
# once rather than one per run.
#
# Its exit status only says *whether* anything was missing. The script has done
# everything it can either way, so the refusal is that something is left to do
# and it is named above.
say ""
say "checking the binaries commcut calls"

"$VENV_PYTHON" - "$PLATFORM" <<'PYTHON_CHECK' || MISSING=1
import sys

sys.path.insert(0, ".")

from shared import environment
from shared import ffmpeg as ffmpeg_module

target = sys.argv[1]

missing = []
unresolved = {}


def check(label, action):
    try:
        value = action()
    except Exception as error:
        missing.append(label)
        unresolved[label] = error
        print(f"[commcut]   MISSING  {label}")
        for line in str(error).strip().splitlines():
            print(f"[commcut]            {line}")
        return None
    print(f"[commcut]   ok       {label}: {value}")
    return value


check("ffmpeg", lambda: environment.get_binary_path("ffmpeg"))
check("ffprobe", lambda: environment.get_binary_path("ffprobe"))
check("libmpv (the client library, not the player)",
      environment.resolve_mpv_library)

# The encoder probe shells out to ffmpeg, so it cannot say anything useful when
# ffmpeg itself did not resolve. Reporting it as a second problem would send the
# user to install an encoder they do not have a problem with.
if "ffmpeg" not in unresolved:

    def encoder():
        if not ffmpeg_module.check_video_encoder():
            raise RuntimeError(
                f"This ffmpeg has no {environment.REQUIRED_VIDEO_ENCODER}, which "
                f"commcut hardcodes for every exported clip.\n"
                f"Many minimal ffmpeg builds leave it out. Homebrew's has it."
            )
        return environment.REQUIRED_VIDEO_ENCODER

    check(f"{environment.REQUIRED_VIDEO_ENCODER} (the encoder every exported clip "
          f"is cut with)", encoder)

if missing:
    print()
    print("[commcut] Install the missing pieces with your package manager:")
    print()
    if target == "macos":
        print("[commcut]   brew install ffmpeg mpv")
    else:
        print("[commcut]   apt install ffmpeg libmpv2     # Debian, Ubuntu")
    print()
    print("[commcut] `brew install mpv` and `apt install mpv` install the mpv")
    print("[commcut] *player*. commcut needs the client *library*, which is a")
    print("[commcut] separate thing -- that is the single most common reason a")
    print("[commcut] source install will not start.")
    print()
    print("[commcut] If yours is installed somewhere commcut does not look, tell")
    print("[commcut] it where:")
    print()
    print(f"[commcut]   export {environment.MPV_LIBRARY_ENV_VAR}"
          "=/full/path/to/libmpv.2.dylib")
    print()
    sys.exit(1)

sys.exit(0)
PYTHON_CHECK

# --------------------------------------------------------------------------
# Done
# --------------------------------------------------------------------------

say ""
if [ "${MISSING:-0}" -ne 0 ]; then
    say "Python is ready, but something commcut calls is not installed."
    say "The list above is what to fix; re-run this script afterwards."
    exit 1
fi

say "Everything is installed."
say ""
say "Start commcut with:"
say ""
say "    ./run.sh"
say ""
if [ "$PLATFORM" = "macos" ]; then
    say "or double-click commcut.command in this folder."
    say ""
fi
say "Your settings, clips and log all live in this folder:"
say "    settings.json  vocabulary.json  import/  export/  commcut.log"
say ""
say "import/ is fixed. export/ is only the default -- Settings has an"
say "\"Export Folder\" row that takes any folder on this computer."
exit 0
