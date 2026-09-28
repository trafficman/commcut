"""Run one mpv foreground-construction case and report a verdict.

A case is a combination of video output driver, how many live players exist in
the process, and what kind of top-level window is foreground when the first
player is built. See ``CASES`` below and README.md for what each one decides.

The verdict is written as JSON to ``--result-file``. Before every step that
could block, the current step name is written to ``--phase-file``, so a run the
watchdog kills still says *where* it died rather than only that it did. A probe
killed with no result file is the HANG verdict; the phase file is the evidence.

Run one case:

    python experiments/mpv_foreground/probe.py --case B_d3d_foreground

Requires a real desktop session. With ``QT_QPA_PLATFORM=offscreen`` there is no
foreground window, the claim under test cannot be posed, and the case reports
foreground_hwnd=None.
"""

import argparse
import ctypes
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(HERE))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from shared.environment import mpv_import_context, setup_environment

setup_environment(__file__)

from PySide6.QtCore import QEventLoop, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFrame, QLabel, QMainWindow, QSplashScreen, QVBoxLayout,
    QWidget,
)

from shared.mpv import create_mpv_player

VIDEO = os.path.join(PROJECT_ROOT, "import", "test.mp4")

# Mirrors create_mpv_player's option set, for the cases that need a driver the
# shipped helper cannot be asked for. Only create_mpv_player's own options are
# duplicated; a case with no override calls the shipped function instead.
BASE_OPTS = {
    "osc": False,
    "input_default_bindings": False,
    "input_vo_keyboard": False,
    "keep_open": True,
    "hr_seek": "always",
}

#: ``vo=None`` and ``extra={}`` means "call the shipped create_mpv_player",
#: so those cases test the configuration the app actually runs.
#:
#: ``foreground`` is what owns the foreground at construction time: no other
#: window, a plain window, or a frameless QSplashScreen (the case the codebase
#: documents as deadlocking). ``window`` is "player" for one top-level window
#: per player, or "shared" for every player embedded in one window. ``block``
#: is the instrument's own negative control and blocks on purpose.
CASES = {
    "A_baseline": {
        "vo": None, "extra": {}, "foreground": "none",
        "players": 1, "window": "player",
    },
    "B_d3d_foreground": {
        "vo": None, "extra": {}, "foreground": "plain",
        "players": 1, "window": "player",
    },
    "C_gpu_win_foreground": {
        "vo": "gpu", "extra": {"gpu_context": "win"},
        "foreground": "plain", "players": 1, "window": "player",
    },
    "D_gpu_d3d11_foreground": {
        "vo": "gpu", "extra": {"gpu_context": "d3d11"},
        "foreground": "plain", "players": 1, "window": "player",
    },
    "E_d3d_three_players": {
        "vo": None, "extra": {}, "foreground": "plain",
        "players": 3, "window": "shared",
    },
    "F_d3d_splash": {
        "vo": None, "extra": {}, "foreground": "splash",
        "players": 1, "window": "player",
    },
    "Z_control_block": {
        "vo": None, "extra": {}, "foreground": "plain",
        "players": 1, "window": "player", "block": True,
    },
}

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32


def write_phase(path, name):
    if path:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(name)


def foreground_hwnd():
    """The HWND Windows currently considers foreground, or None."""
    hwnd = _user32.GetForegroundWindow()
    return int(hwnd) if hwnd else None


def _force_foreground(hwnd):
    """Take the foreground from its current owner.

    `raise_()` and `activateWindow()` are subject to Windows' foreground lock: a
    process may only take the foreground if it already holds it. Run from a
    console, that console usually keeps it, so a plain activate is not enough
    and the case would quietly stop testing the claim. Borrowing the foreground
    thread's input queue is the documented way to take it from the owner.
    """
    current_thread = _kernel32.GetCurrentThreadId()
    owner = _user32.GetForegroundWindow()
    owner_thread = _user32.GetWindowThreadProcessId(owner, None) if owner else 0
    attached = bool(owner_thread) and owner_thread != current_thread
    if attached:
        _user32.AttachThreadInput(current_thread, owner_thread, True)
    try:
        _user32.SetForegroundWindow(hwnd)
        _user32.BringWindowToTop(hwnd)
    finally:
        if attached:
            _user32.AttachThreadInput(current_thread, owner_thread, False)


def bring_to_front(window, app, attempts=12):
    """Make `window` foreground if it can be, and report the HWND either way."""
    window.show()
    target = int(window.winId())
    for _ in range(attempts):
        window.raise_()
        window.activateWindow()
        _pump(app, 0.05)
        if foreground_hwnd() == target:
            return target
    _force_foreground(target)
    _pump(app, 0.2)
    return target


def _pump(app, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents(QEventLoop.AllEvents, 10)
        time.sleep(0.002)


def create_player(frame, vo, extra):
    """The shipped player for an unconfigured case, a probe-built one otherwise.

    Delegating to create_mpv_player for the no-override cases is the point: it
    is the function the app runs, so a pass here transfers to the app rather
    than describing this file.
    """
    if vo is None and not extra:
        return create_mpv_player(frame)

    with mpv_import_context():
        import mpv

    frame.setAttribute(Qt.WA_NativeWindow, True)
    handle = int(frame.winId())
    if not handle:
        raise RuntimeError("no native window handle for the video frame")

    options = dict(BASE_OPTS)
    options.update(extra)
    options["wid"] = str(handle)
    if vo is not None:
        options["vo"] = vo
    return mpv.MPV(**options)


class PlayerWindow(QMainWindow):
    """A top-level window with video frames, like the editor and scanner."""

    def __init__(self, title, frame_count=1):
        super().__init__()
        self.setWindowTitle(title)
        container = QWidget()
        layout = QVBoxLayout(container)
        self.frames = []
        for index in range(frame_count):
            frame = QFrame()
            layout.addWidget(QLabel(f"{title} frame {index}"))
            layout.addWidget(frame)
            self.frames.append(frame)
        self.setCentralWidget(container)
        self.resize(640, 180 + 200 * frame_count)


def make_foreground_window(kind):
    """The window that owns the foreground while a player is built."""
    if kind == "none":
        return None
    if kind == "splash":
        pixmap = QPixmap(480, 270)
        pixmap.fill(QColor(30, 30, 30))
        return QSplashScreen(pixmap)
    window = QWidget()
    window.setWindowTitle("probe-foreground")
    window.resize(320, 240)
    return window


def pump_until(app, predicate, timeout):
    """Pump the event loop until `predicate` holds; report whether it did."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        app.processEvents(QEventLoop.AllEvents, 10)
        time.sleep(0.005)
    return bool(predicate())


def _property(player, name):
    """Read an mpv property by its Python name, as the app's bridge does.

    python-mpv exposes properties as attributes, which is how MpvBridge reads
    them; get_property is the fallback for names that are not valid identifiers
    (`vo-configured`, `mpv-version`). Both are tried so a name the property API
    does not know about reads as unknown rather than as a false value.
    """
    identifier = name.replace("-", "_")
    try:
        value = getattr(player, identifier)
    except Exception:
        value = None
    if value is None:
        try:
            value = player.get_property(name)
        except Exception:
            return None
    return value


def run_case(case, phase_file, result_file, play_timeout):
    spec = CASES[case]
    started = time.monotonic()

    write_phase(phase_file, "qapplication")
    app = QApplication(sys.argv[:1])

    result = {
        "case": case,
        "vo": spec["vo"] or "environment.video_output()",
        "extra": spec["extra"],
        "players": spec["players"],
        "window_layout": spec["window"],
        "foreground_kind": spec["foreground"],
        "verdict": "ERROR",
        "detail": "",
        "construct_seconds": [],
        "vo_configured": False,
        "frame_advanced": False,
        "players_state": [],
        "first_frame_seconds": None,
        "foreground_confirmed": None,
        "mpv_version": None,
    }

    players = []
    windows = []
    foreground_window = None
    try:
        write_phase(phase_file, "create_windows")
        if spec["window"] == "shared":
            windows.append(PlayerWindow("probe-holder", spec["players"]))
            frames = windows[0].frames
        else:
            frames = []
            for index in range(spec["players"]):
                window = PlayerWindow(f"probe-player-{index}")
                windows.append(window)
                frames.extend(window.frames)

        last_hwnd = bring_to_front(windows[-1], app)

        foreground_window = make_foreground_window(spec["foreground"])
        if foreground_window is None:
            intended_hwnd = last_hwnd
        else:
            intended_hwnd = bring_to_front(foreground_window, app)
        result["intended_foreground_hwnd"] = intended_hwnd

        for index, frame in enumerate(frames, start=1):
            write_phase(phase_file, f"construct#{index}")
            if spec.get("block"):
                time.sleep(600)
            active = foreground_hwnd()
            result.setdefault("foreground_at_construct", []).append(active)
            if result["foreground_confirmed"] is None:
                result["foreground_confirmed"] = active == intended_hwnd

            construct_started = time.monotonic()
            player = create_player(frame, spec["vo"], spec["extra"])
            result["construct_seconds"].append(
                round(time.monotonic() - construct_started, 3))
            players.append(player)

        player = players[0]
        result["mpv_version"] = _property(player, "mpv-version")

        # Every player is given the file and has to bring its own VO up and
        # present a frame. Playing only the first would leave the rest with a
        # wid and no media, which is not a second player in any meaningful
        # sense -- mpv has no output to have gotten wrong.
        write_phase(phase_file, "first_frame")
        frame_started = time.monotonic()
        for each in players:
            each.play(VIDEO)

        for each in players:
            configured = pump_until(
                app, lambda p=each: bool(_property(p, "vo-configured")),
                play_timeout)
            advanced = False
            if configured:
                advanced = pump_until(
                    app, lambda p=each: (_property(p, "time-pos") or 0) > 0.0,
                    play_timeout)
            result["players_state"].append(
                {"vo_configured": configured, "frame_advanced": advanced})

        result["vo_configured"] = all(
            state["vo_configured"] for state in result["players_state"])
        result["frame_advanced"] = all(
            state["frame_advanced"] for state in result["players_state"])
        result["first_frame_seconds"] = round(
            time.monotonic() - frame_started, 3)

        idle = [index + 1 for index, state in enumerate(result["players_state"])
                if not state["vo_configured"]]
        if idle:
            result["verdict"] = "VO_FAILED"
            result["detail"] = (
                f"player(s) {idle} built but vo-configured stayed false for "
                f"{play_timeout}s")
        elif not result["frame_advanced"]:
            still = [index + 1
                     for index, state in enumerate(result["players_state"])
                     if not state["frame_advanced"]]
            result["verdict"] = "NO_FRAMES"
            result["detail"] = (
                f"vo-configured true but time-pos never advanced past 0 on "
                f"player(s) {still}: the VO came up without presenting")
        else:
            result["verdict"] = "OK"
    except Exception as error:
        result["verdict"] = "ERROR"
        result["detail"] = f"{type(error).__name__}: {error}"
    finally:
        result["total_seconds"] = round(time.monotonic() - started, 3)
        write_phase(phase_file, "cleanup")
        for player in players:
            try:
                player.terminate()
            except Exception:
                pass
        if result_file:
            with open(result_file, "w", encoding="utf-8") as handle:
                json.dump(result, handle, indent=2)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, choices=sorted(CASES))
    parser.add_argument("--phase-file")
    parser.add_argument("--result-file")
    parser.add_argument("--play-timeout", type=float, default=10.0)
    args = parser.parse_args()

    result = run_case(
        args.case, args.phase_file, args.result_file, args.play_timeout)
    print(json.dumps(result))
    return 0 if result["verdict"] == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
