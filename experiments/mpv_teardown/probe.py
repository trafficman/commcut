"""Close a real player window through the real shell, and see if we come back.

One case per process, because the failure is a process that never returns. The
verdict goes to ``--result-file``; the step name goes to ``--phase-file`` before
anything that can block, so a run the watchdog kills says where it died.

    python experiments/mpv_teardown/probe.py --case fix

Requires a real desktop session: the video frame needs a native HWND for mpv to
embed into, and ``QT_QPA_PLATFORM=offscreen`` has no such thing.

A first attempt at this used a bare QWidget with an MpvBridge in it, and the
control did not hang -- so the teardown ordering alone is not the whole story.
This version drives the shipped window and the shipped shell instead, because
the reported freeze happened on the path that goes through both:

    window.close() -> closeEvent -> WA_DeleteOnClose -> destroyed
                    -> Shell._remove -> _present(menu)

and which of those blocks is exactly what this is trying to find out.
"""

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(HERE))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from shared.environment import setup_environment

setup_environment(__file__)

from PySide6.QtCore import QEvent, QEventLoop, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

import shared.mpv as mpv_module
from mainwindow import MainWindow
from shared.session import Shell, set_shell

VIDEO = os.path.join(PROJECT_ROOT, "import", "test.mp4")

#: The windows under test. The report named both, and they differ in
#: everything except owning a player, so both are worth driving.
WINDOWS = ("editor", "scanner")


def write_phase(path, name):
    if path:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(name)


def pump(app, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents(QEventLoop.AllEvents)
        time.sleep(0.005)


_user32 = None


def _win32():
    global _user32
    if _user32 is None:
        import ctypes
        _user32 = ctypes.windll.user32
    return _user32


def _describe(hwnd):
    """What this HWND is, so a capture can be read at a glance."""
    user32 = _win32()
    if not hwnd:
        return None
    is_window = bool(user32.IsWindow(hwnd))
    return {
        "hwnd": int(hwnd),
        "is_window": is_window,
        "class": _class_name(hwnd) if is_window else None,
        "ours": _is_one_of_ours(hwnd),
    }


def _class_name(hwnd):
    import ctypes
    user32 = _win32()
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


_our_handles = set()


def _note_handles(*windows):
    for window in windows:
        if window is None:
            continue
        for candidate in (window, *window.findChildren(object)):
            try:
                _our_handles.add(int(candidate.winId()))
            except Exception:
                pass


def _is_one_of_ours(hwnd):
    return int(hwnd) in _our_handles


def _capture_report():
    """Who holds the mouse capture, and is that still a real window."""
    return _describe(_win32().GetCapture())


def _window_under_cursor():
    """Which HWND Windows would actually deliver a click to right now."""
    import ctypes
    user32 = _win32()
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    point = POINT()
    user32.GetCursorPos(ctypes.byref(point))
    hwnd = user32.WindowFromPoint(point)
    described = _describe(hwnd)
    if described:
        described["cursor"] = [point.x, point.y]
    return described


def run_case(case, window_name, phase_file, result_file, play_timeout,
             hold=0.0):
    started = time.monotonic()
    result = {
        "case": case,
        "window": window_name,
        "verdict": "ERROR",
        "detail": "",
        "built": False,
        "frame_advanced": False,
        "timer_fired_after_close": False,
        "menu_is_current": False,
        "menu_is_enabled": False,
        "menu_is_visible": False,
        "menu_responded": False,
        "widget_at_menu_centre": None,
        "close_seconds": None,
    }

    if case == "control":
        # Reproduce the pre-fix state: the player is never shut down before the
        # window goes. Done by neutering the one method the fix added, so the
        # rest of the shipped path is untouched.
        mpv_module.MpvBridge.shutdown = lambda self: None

    write_phase(phase_file, "qapplication")
    app = QApplication(sys.argv[:1])

    menu = MainWindow()
    shell = Shell(app, menu)
    set_shell(shell)
    menu.show()
    app.processEvents(QEventLoop.AllEvents)

    try:
        write_phase(phase_file, "open_window")
        window = shell.open_safely(window_name, source=VIDEO)
        if window is None:
            result["verdict"] = "ERROR"
            result["detail"] = f"the {window_name} window did not open"
            return result
        result["built"] = True
        _note_handles(window, menu)

        write_phase(phase_file, "play")
        bridge = getattr(window, "bridge", None)
        if bridge is not None:
            # Deliberately no load call here. The window's constructor has
            # already loaded the file, and asking mpv to load it a second time
            # while its VO is up produced a hard access violation in an earlier
            # version of this probe -- that was the probe misusing the player,
            # not the app. Start from whatever state the window left it in and
            # make sure it is actually playing, so the teardown happens with a
            # live VO attached to a live handle.
            deadline = time.monotonic() + play_timeout
            while time.monotonic() < deadline:
                app.processEvents(QEventLoop.AllEvents, 20)
                if bridge.position is not None:
                    break
            if bridge.paused is False or bridge.paused is None:
                bridge.toggle_play()
            deadline = time.monotonic() + play_timeout
            while time.monotonic() < deadline:
                app.processEvents(QEventLoop.AllEvents, 20)
                if (bridge.position or 0.0) > 0.0:
                    break
            result["frame_advanced"] = (bridge.position or 0.0) > 0.0

        # The close, through the shipped path: closeEvent, WA_DeleteOnClose,
        # the deferred delete, destroyed, and the shell returning to the menu.
        fired = []
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: fired.append(time.monotonic()))
        timer.start(200)

        write_phase(phase_file, "close")
        close_started = time.monotonic()
        window.close()
        app.processEvents(QEventLoop.AllEvents)
        app.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents(QEventLoop.AllEvents)
        result["close_seconds"] = round(time.monotonic() - close_started, 3)
        result["menu_is_current"] = shell.windows == (menu,)
        result["timer_fired_after_close"] = bool(fired)

        # The actual assertion, and the first version of this harness got it
        # wrong: it checked that a QTimer still fired, which only proves the GUI
        # thread is alive. It is -- the app is not hung. The report is that the
        # menu stops responding, so what has to be measured is whether the menu
        # *reacts to input*, which is a different thing entirely and is what
        # distinguishes a stuck grab or a disabled window from a dead process.
        #
        # QTest.mouseClick delivers a real mouse event through the window system
        # rather than calling the slot, so a stuck grab stops it reaching the
        # button. A programmatic .click() would have "worked" either way.
        write_phase(phase_file, "responds")
        pump(app, 1.0)

        result["menu_is_enabled"] = bool(menu.isEnabled())
        result["menu_is_visible"] = bool(menu.isVisible())
        result["widget_at_menu_centre"] = type(
            QApplication.widgetAt(menu.mapToGlobal(menu.rect().center())) or object()
        ).__name__

        # The objective half of the measurement, and the part that does not need
        # a person.
        #
        # A reported human symptom is that after the window closes the app is
        # alive -- timers fire, posted events land, a synthetic click opens the
        # picker -- but no real mouse input reaches any window. Posted events
        # bypass the OS input path, so that combination is the signature of a
        # mouse capture left behind: Windows still routing every click to a
        # window handle that no longer exists.
        #
        # GetCapture() names that handle, and IsWindow says whether it is still
        # a window at all. A live handle that is not one of ours is the bug, and
        # it is a direct observation rather than an inference from a freeze.
        result["mouse_capture"] = _capture_report()
        result["window_under_cursor"] = _window_under_cursor()
        # The Qt-level equivalent of GetCapture, and the remaining candidate.
        # A widget grab routes real mouse events to the grabber and no one else,
        # while leaving posted events (and so a synthetic click) working -- which
        # is exactly the reported combination. mouseGrabber() is None when
        # nothing holds it.
        grabber = QWidget.mouseGrabber()
        result["qt_mouse_grabber"] = (
            None if grabber is None else {
                "class": type(grabber).__name__,
                "is_window": _describe(int(grabber.winId())) if grabber is not None else None,
            })
        result["qt_focus_window"] = type(
            QApplication.focusWindow() or object()).__name__

        before = len(shell.windows)
        QTest.mouseClick(menu.ui.editorButton, Qt.LeftButton)
        pump(app, 1.5)
        result["menu_responded"] = len(shell.windows) > before

        result["verdict"] = "OK" if result["menu_responded"] else "NO_RESPONSE"
        # Read this before trusting an OK. "menu_responded" is a *posted* click
        # landing on a button, so it says the GUI thread is alive and the shell
        # is still routing events. It does not say real mouse input arrives, and
        # a person has reported cases where it was OK and the app was
        # nevertheless frozen to the mouse. Everything below is an attempt to
        # close that gap from the inside; as of the last run all four came back
        # negative (see experiments/README.md). Until one of them can go
        # positive, a human is the oracle for the freeze and this harness is the
        # oracle for the teardown.
        result["detects_input_lockout"] = False
        result["note"] = (
            "OK means the process is responsive and the menu took a posted "
            "click. It is not evidence that the mouse works.")

        if hold > 0:
            # Leave the menu on screen for a human to hover and click. The
            # programmatic check above reports the menu responsive in cases a
            # person reports as frozen, so a person is the better oracle for
            # the symptom that started this: QTest delivers its event without
            # needing the window to be the OS foreground window, which is
            # exactly the part a synthetic click cannot judge.
            result["hold_seconds"] = hold
            print(f"HOLDING for {hold}s -- hover and click the main menu now",
                  flush=True)
            pump(app, hold)

    except Exception as error:
        result["verdict"] = "ERROR"
        result["detail"] = f"{type(error).__name__}: {error}"
    finally:
        result["total_seconds"] = round(time.monotonic() - started, 3)
        write_phase(phase_file, "done")
        if result_file:
            with open(result_file, "w", encoding="utf-8") as handle:
                json.dump(result, handle, indent=2)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, choices=["control", "fix"])
    parser.add_argument("--window", default=WINDOWS[0], choices=list(WINDOWS))
    parser.add_argument("--phase-file")
    parser.add_argument("--result-file")
    parser.add_argument("--play-timeout", type=float, default=10.0)
    parser.add_argument(
        "--hold", type=float, default=0.0,
        help="leave the menu on screen this many seconds after the close, so a "
             "person can hover and click it; see the note in run_case()")
    args = parser.parse_args()

    result = run_case(
        args.case, args.window, args.phase_file, args.result_file,
        args.play_timeout, args.hold)
    print(json.dumps(result))
    return 0 if result["verdict"] == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
