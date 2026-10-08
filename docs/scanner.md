# The Segment Scanner

The scanner is the detector + review half of the Editing Wizard: it previews a
short clip of the source, detects clip boundaries with `blackdetect`, and hands
the result to the editor.

Applies to: `scanner/scanner.py`, `scanner/marker_timeline.py`,
`scanner/scannerwindow.ui`, `shared/ffmpeg.py:clip_to_temp`.

Related: [architecture.md](architecture.md) (one process, the one visible window,
the source hand-off), [segment-model.md](segment-model.md) (what the boundaries
become).

## What it does

The scanner (`scanner/scanner.py`) is the detector + review half of the
Editing Wizard. It loads a 2-minute stream-copied preview of the source via
`shared/ffmpeg.clip_to_temp` into `temp/` (fast, lossless, small clip — the full
video isn't loaded until "Finished"), and plays it back in an embedded mpv
player.

Each scanner run begins in `scanner.create()`, which clears `temp/*.mp4`
(`_clear_temp_clips`) so preview clips don't accumulate across runs; the
2-minute preview is then stream-copied to `temp/` under a name derived from the
source (`clip_to_temp` writes `<source>_clip120s.mp4`, so two videos never share
one preview). The pre-scan — the stream copy and the ffprobe keyframe scan —
runs on `ScannerPreScanWorker` on a `QThread`, behind a modal
`LoadingDialog.exec()` so `create()` stays synchronous (the shell expects it to
return the window) while the GUI thread pumps the event loop. The dialog closes
before `ScannerWindow` is constructed, so no top-level window is foreground when
the mpv player is built (the splash/mpv hazard, docs/architecture.md).

Its UI (`scannerwindow.ui`) has: an embedded video frame, two
`MarkerTimelineWidget`s ("Scanner Preview" and "User Marked"), transport
controls (play/pause, frame ±, keyframe ±), a segment-controls row
(Undo, Place Boundary), detector sliders (Minimum Black Frames 0–40,
Black Levels 0–100), and a scan row (Test Scan, Finished — Scan Full
Source Video).

What's wired in `ScannerWindow.__init__`: loading the preview clip (passed in
from `create()`), mpv player construction, transport + frame/keyframe stepping,
feeding both timelines the bridge's position/duration/seek, the slider value
labels, Place Boundary (playhead → lower **User Marked** timeline via
`add_marker`), Undo (pops the last user marker), Test Scan (`blackdetect` →
midpoint markers in the upper **Scanner Preview** timeline), and Finished
(`blackdetect` on the full source → midpoint boundaries written to a `.cmct`,
then the Video Editor launched for manual fixes + tags).

## Detection

Test Scan runs `ffmpeg blackdetect` on the 2-minute preview via
`TestScanWorker` on a `QThread`, and stamps one midpoint `(T1 + T2) / 2` per
black run into the upper Scanner Preview timeline (`timelineWidget1`) via
`MarkerTimelineWidget.add_marker`. The two sliders map straight onto the filter:
`d = frames / fps` and `pix_th = level / 100`; `black_end:N/A` runs (a black
span that runs to the end of the clip) are skipped, since there is no boundary
to take a midpoint of. Previous markers are cleared first so re-scanning reflects
the current slider settings.

The two timelines are separate on purpose: scanner-detected markers land on
the upper timeline, and the user's own Place Boundary presses land on the
lower **User Marked** timeline, which is in-memory only — no `.cmct` is
written until Finished.

## Finished, and the hand-off to the editor

When no `.cmct` exists, the Finished button runs `blackdetect` on the full
source via `FinishedScanWorker` on a `QThread`. The worker first probes the
source duration with ffprobe (`probe_duration`, inlined into `run()` so it is
off the GUI thread), then runs blackdetect; the `scanned(midpoints, duration)`
signal carries both results back to `_on_scan_complete`, which writes the
midpoint boundaries as `.cmct` segment starts via `SegmentModel`
(`_model_from_midpoints`) and then opens the editor on that same source.

If a `.cmct` sidecar already exists next to the source video, the scanner
builds no window at all and raises `shared/session.py:OpenInstead` naming the
editor, so an existing project is never overwritten. The rule is the scanner's;
the routing is the shell's, because there is no scanner window to show. The
source there is the one the main menu handed this window — by its file dialog or
by a drop — not a re-resolved default (`_editor_to_launch` is that decision).

That shortcut used to be **visible**: the picker listed everything in `import/`
and labelled a rip that had been scanned *"(already scanned — opens in the
editor)"*. A native file dialog cannot annotate a file, so the behaviour remains
and the warning does not — resuming a scanned rip means remembering where it is
and picking it again. A recent-sources list is the obvious fix and is not built;
see [status.md](status.md).

Because the source is now picked from anywhere rather than from the app's own
folder, the sidecar is written to a folder the app does not control.
`shared/sources.py:validate_source_video` refuses a source whose folder cannot
take a write before the scanner is built, and `MediaPlayer._save_sidecar` turns a
later failure into a message naming the file and the folder.

## Thread lifecycle and closeEvent

All three worker threads — the pre-scan worker (in `create`), the test-scan
worker (in `on_test_scan`), and the finished-scan worker (in `on_finished`) —
follow the same shape documented in [architecture.md](architecture.md#ending-a-worker-thread):

1. The worker is a `QObject` whose `run()` slot is connected to
   `thread.started`. It emits `result`/`scanned` on success and `failed` on
   error.
2. Both `result`/`scanned` and `failed` are connected to `thread.quit`, so the
   thread's event loop exits after the worker produces its outcome.
3. `thread.finished` is connected to the window's teardown method
   (`_on_test_scan_stopped` or `_on_scan_stopped`), which calls
   `deleteLater()` on the dialog, thread, and worker, and re-enables the
   disabled controls.

The window's `closeEvent` refuses to close while either thread is active
(`_scan_thread` or `_test_scan_thread` is not `None`), asking the user to
cancel. If they agree, `_close_after_worker` is set and both workers are
cancelled; the teardown method closes the window once all threads have actually
stopped — so a `QThread` is never destroyed while still running (invariant 14).

Automated boundary detection is fully wired: Test Scan (preview, in-memory
midpoints), Finished (full-source `blackdetect` → `.cmct` → editor), and the
Scanner→Editor handoff when a `.cmct` already exists. What the scanner does
*not* decide is tags or which detected segments are real — that review happens
in the editor (see [segment-model.md](segment-model.md), including Skip for
false positives).
