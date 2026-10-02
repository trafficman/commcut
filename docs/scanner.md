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
Editing Wizard. It mirrors the editor's splash/mpv flow, and loads a
2-minute stream-copied preview of the source via `shared/ffmpeg.clip_to_temp`
into `temp/` (fast, lossless, small clip — the full video isn't loaded until
"Finished").

Each scanner run begins by clearing `temp/*.mp4` (`_clear_temp_clips` in
`scanner.py`) so preview clips don't accumulate across runs; the 2-minute
preview is then stream-copied to `temp/` under a name derived from the source
(`clip_to_temp` writes `<source>_clip120s.mp4`, so two videos never share one
preview).

Its UI (`scannerwindow.ui`) has: an embedded video frame, two
`MarkerTimelineWidget`s ("Scanner Preview" and "User Marked"), transport
controls (play/pause, frame ±, keyframe ±), a segment-controls row
(Undo, Place Boundary), detector sliders (Minimum Black Frames 0–40,
Black Levels 0–100), and a scan row (Test Scan, Finished — Scan Full
Source Video).

What's wired in `ScannerWindow.__init__`: loading the clipped preview,
transport + frame/keyframe stepping, feeding both timelines the bridge's
position/duration/seek, the slider value labels, Place Boundary (playhead
→ lower **User Marked** timeline via `add_marker`), Undo (pops the last
user marker), Test Scan (`blackdetect` → midpoint markers in the upper
**Scanner Preview** timeline), and Finished (`blackdetect` on the full
source → midpoint boundaries written to a `.cmct`, then the Video Editor
launched for manual fixes + tags).

## Detection

Test Scan runs `ffmpeg blackdetect` on the 2-minute preview and stamps one
midpoint `(T1 + T2) / 2` per black run into the upper Scanner Preview
timeline (`timelineWidget1`) via `MarkerTimelineWidget.add_marker`. The two
sliders map straight onto the filter: `d = frames / fps` and
`pix_th = level / 100`; `black_end:N/A` runs (a black span that runs to the
end of the clip) are skipped, since there is no boundary to take a midpoint of.

The two timelines are separate on purpose: scanner-detected markers land on
the upper timeline, and the user's own Place Boundary presses land on the
lower **User Marked** timeline, which is in-memory only — no `.cmct` is
written until Finished.

## Finished, and the hand-off to the editor

When no `.cmct` exists, the Finished button runs `blackdetect` on the full
source, writes the midpoint boundaries to `<name>.cmct` next to the source,
and then opens the editor on that same source. The midpoints become segment
starts through `SegmentModel` (`_model_from_midpoints`).

If a `.cmct` sidecar already exists next to the source video, the scanner
builds no window at all and raises `shared/session.py:OpenInstead` naming the
editor, so an existing project is never overwritten. The rule is the scanner's;
the routing is the shell's, because there is no scanner window to show. The
source there is the one the main menu's file dialog handed this window, not a
re-resolved default (`_editor_to_launch` is that decision).

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

Automated boundary detection is fully wired: Test Scan (preview, in-memory
midpoints), Finished (full-source `blackdetect` → `.cmct` → editor), and the
Scanner→Editor handoff when a `.cmct` already exists. What the scanner does
*not* decide is tags or which detected segments are real — that review happens
in the editor (see [segment-model.md](segment-model.md), including Skip for
false positives).
