# AGENTS.md

Context for AI coding agents working on the **commcut** project. This file
captures the project scope, current architecture, key data models, and the
conventions/gotchas you need to be productive here.

## What this project is

**commcut** is a Python/PySide6 desktop app for managing a personal library
of "filler" clips (commercials, promos, bumps) ripped from television. The
full vision — described in `README.md` — has three pieces:

1. **Library organization** — a folder/naming scheme driven by per-clip tags
   (Title, Network, Block, Filler Type, Year, Time Period, Show, Special,
   Length, Information).
2. **Rename Wizard** — batch-rename already-cut clips using the tag scheme.
3. **Editing Wizard** — load a compilation video (multiple clips back to
   back), auto-detect boundaries, let the user review/adjust, and smart-cut
   each segment out as a separate file.

The working tree currently contains two Wizard modules — the **Editing
Wizard** (`editor/`) and the **Segment Scanner** (`scanner/`) — plus the
`shared/` library they both build on. The Rename Wizard and library/export
layers are not yet built.

## Layout

```
commcut/
├── README.md                # Project spec (source of truth for scope)
├── AGENTS.md                # This file
├── bin/                     # Bundled binaries, one subfolder per OS
│   ├── win/                 # Windows binaries (ffmpeg.exe, ffprobe.exe, libmpv-2.dll)
│   ├── linux/               # Linux binaries (placeholders, none shipped yet)
│   └── mac/                 # macOS binaries (placeholders, none shipped yet)
├── import/                  # Test source videos
├── temp/                    # Scratch output (e.g. 2-min scanner preview clips)
├── editor/                  # The Editing Wizard (current focus)
│   ├── editor.py            # Entry point: Editor window, editing state machine,
│   │                        # splash flow, PreScanWorker scaffolding
│   └── editorwindow.ui      # Qt Designer file; promoted TimelineWidget
├── scanner/                 # The Segment Scanner (detector + review)
│   ├── scanner.py           # Entry point: 2-min preview clip load, mpv playback,
│   │                        # transport, two marker timelines, detector sliders
│   ├── marker_timeline.py   # MarkerTimelineWidget (playhead + vertical marker lines)
│   └── scannerwindow.ui     # Qt Designer file; promoted MarkerTimelineWidget
├── shared/                  # Cross-module library (editor + scanner)
│   ├── environment.py       # setup_environment + get_binary_path (cross-platform)
│   ├── mpv.py               # MpvBridge, create_mpv_player, scan_keyframes
│   ├── timeline.py          # TimelineWidget (segments, zoom/scroll)
│   ├── segments.py          # SegmentModel + .cmct persistence, probe_duration
│   ├── ffmpeg.py            # clip_to_temp (and home of future smart-cut export)
│   └── ui_loader.py         # UiLoader subclass for promoted custom widgets
├── prototypes/              # Earlier exploration / alternatives
│   ├── BasicUI/             # First prototype
│   └── VideoEditor/         # Pre-rename copy of the editor module
```

## The .cmct sidecar format

Segments are stored as a JSON sidecar next to the source video with extension
`.cmct`. The path is derived by swapping the extension (`compilation.mp4`
→ `compilation.cmct`).

**Transition-point model.** The source video is tiled contiguously by
segments. Each segment entry is just its start time and metadata; the end
is derived as the next entry's start (or `duration` for the last one). This
makes overlaps and gaps structurally impossible.

```json
{
  "source": "compilation.mp4",
  "duration": 120.0,
  "segments": [
    {"start": 0.0,   "ignored": false, "tags": {}},
    {"start": 42.5,  "ignored": true,  "tags": {}},
    {"start": 78.2,  "ignored": false, "tags": {}}
  ]
}
```

Tag fields (from the readme's scheme): `title`, `network`, `block`,
`filler_type`, `year`, `time_period`, `show`, `special`, `length`,
`information`. Title must be unique per segment and has **no lock button**;
all other tags do.

The segment data model and persistence live in
`shared/segments.py:SegmentModel`. Operations:
`end_segment(i, pos)`, `start_segment(i, pos)`, `merge_next(i)`,
plus `placeholder(source, duration)` and `save/load` for `.cmct`.

## The editing state machine

A linear left-to-right walk through the segments. The editor window holds:

- `self.segment_model: SegmentModel` — the in-memory working state.
- `self.current_index: int` — the **Active Segment**.
- `self.tag_locks: dict` — locked tag values that carry across unedited
  segments (working state, not persisted to `.cmct`).
- `self.dirty: bool` — True when the in-memory model has changes since the
  last `Stage` (which writes to `.cmct`).

### Operations on the Active Segment

| Button            | Effect                                                                                              |
|-------------------|-----------------------------------------------------------------------------------------------------|
| **End Seg**       | `end_segment` at playhead. Active stays on the left half; right half becomes a new unedited segment. |
| **Start Seg**     | `start_segment` at playhead. Left half marked `ignored=True`; right half becomes the new active segment, inheriting metadata. |
| **Merge Next**    | `merge_next` — absorb the next segment into the active one. Used for false-positive detections.   |
| **Skip** (check)  | Toggle `ignored` on the active segment.                                                           |
| **Stage**         | Read form tags into the active segment, save the whole model to `.cmct`, clear `dirty`, advance `current_index`. |
| **Undo**          | Reload model from `.cmct` (reverts all unstaged changes), clear `dirty`.                            |
| **Toggle Zoom**   | Toggle between zoom-to-active-segment and fit-whole-video.                                         |
| **Active ←/→**    | Move `current_index` by ±1 (clamped). On any active change, snap the playhead to the new segment's start. |

The **dirty indicator**: Stage and Undo are both `QPushButton` with
`checkable=True`. When `self.dirty` is True, both are `checked=True` and
`enabled=True` (colored, clickable). When False, both are `checked=False`
and `enabled=False` (greyed out, unclickable). The `_update_stage_button`
helper drives both from the single `self.dirty` flag.

### Locks (tag carry-over)

Locks are session-level: `self.tag_locks[key] = value` when the user toggles
a lock on. On navigation to an **unedited** segment (all tags empty), the
form pre-fills from `self.tag_locks` and the lock buttons show as checked.
On an **edited** segment, all locks disengage (unchecked) and the form shows
the stored tags. Title is excluded from the lock system.

## Architecture

The editor and scanner share a common library under `shared/`. Both call
`shared/environment.setup_environment(__file__)` at the very top of their
entry point — it puts the project root on `sys.path` (so `shared.*`
resolves when running the script directly) and prepends the per-OS
`bin/<os>/` folder to `PATH` so mpv, ffprobe, and ffmpeg resolve to the
bundled versions.

The shared modules are:

- `shared/environment.py` — `setup_environment(script_path)` (sys.path +
  PATH) and `get_binary_path(name)`, which resolves ffmpeg/ffprobe/mpv per
  OS under `bin/<os>/` (`.exe` on Windows). This is the cross-platform binary
  resolution that used to live in `core.py`.
- `shared/mpv.py` — `MpvBridge` (the single Qt↔libmpv channel),
  `create_mpv_player` (wraps `mpv.MPV` for a `QFrame`, sets
  `WA_NativeWindow`), and `scan_keyframes(path)` (ffprobe I-frame scan
  returning sorted timestamps).
- `shared/timeline.py` — `TimelineWidget`, the editor's zoom/scroll segment
  timeline (red/green/blue, ignored dimming, active highlight).
- `shared/segments.py` — `SegmentModel` + `.cmct` persistence
  (`sidecar_path`, `probe_duration`).
- `shared/ffmpeg.py` — ffmpeg helpers (`clip_to_temp`, and the future home of
  the smart-cut export).
- `shared/ui_loader.py` — `UiLoader(QUiLoader)` subclass that instantiates
  promoted custom widgets reliably; register a class with
  `register_widget` before `load()`.

### The MpvBridge pattern

Both the editor and scanner communicate with libmpv through a single
**`MpvBridge(QObject)`** that owns the mpv player and exposes:

- **Qt signals** (state up): `pauseChanged`, `positionChanged`,
  `durationChanged`, `fileLoaded`, `playbackEnded`. These are fed by
  `player.observe_property(...)` callbacks. **Observers fire on mpv's worker
  thread; emitting Qt signals is thread-safe and lands on the GUI thread.**
- **Methods** (commands down): `toggle_play`, `seek_exact`, `step_frames`,
  `set_keyframes`, `next_keyframe`, `prev_keyframe`, `load_file`,
  `load_and_play`.

The editor/scanner windows and widgets never read mpv state directly — they
only mirror what the bridge announces via signals.

## Splash flow (pre-work before the window appears)

`__main__` in both the editor and the scanner shows a `QSplashScreen`, then
runs `scan_keyframes` (via ffprobe) while it's up. The scan is currently
synchronous on the GUI thread — it's typically sub-second for a 2-minute
preview. `PreScanWorker` is scaffolding in the editor for future off-thread
stages, not yet wired into `__main__`.

**Hard-won gotcha:** mpv's Direct3D device initialization hangs when
another top-level window (the splash) is the active window at construction
time. Close the splash *before* constructing the mpv-backed widget.
`app.processEvents()` is called once after `splash.show()` to ensure the
splash actually paints.

## The Scanner window

The scanner (`scanner/scanner.py`) is the detector + review half of the
Editing Wizard. It mirrors the editor's splash/mpv flow, and loads a
2-minute stream-copied preview of the source via `shared/ffmpeg.clip_to_temp`
into `temp/` (fast, lossless, small clip — the full video isn't loaded until
"Finished").

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
user marker), and Test Scan (`blackdetect` → midpoint markers in the upper
**Scanner Preview** timeline).

What's *not* wired yet: the Finished button (scan the full source video
instead of the 2-minute preview). All detector wiring is complete.

Each scanner run begins by clearing `temp/*.mp4` (`_clear_temp_clips` in
`scanner.py`) so preview clips don't accumulate across runs; the 2-minute
preview is then stream-copied to `temp/` with a deterministic name
(`test_clip120s.mp4`).

## Key conventions and gotchas

- **Bundled binaries.** Both the editor and the scanner call
  `shared/environment.setup_environment` at startup, which prepends the
  per-OS `bin/<os>/` folder to `PATH` via `get_binary_path`. mpv, ffprobe,
  and ffmpeg resolve to the bundled versions. `get_binary_path` raises
  `FileNotFoundError` if a binary is missing at the expected path.
- **Promoted widget.** The timeline is a custom `QWidget` promoted in
  `editorwindow.ui` as `TimelineWidget` / header `timeline`. PySide6's
  `QUiLoader` does not auto-resolve promoted widgets reliably, so
  `editor.py` uses a `UiLoader(QUiLoader)` subclass whose `createWidget`
  is overridden to instantiate `TimelineWidget` directly. Register the
  class with `loader.register_widget(TimelineWidget)` before `loader.load()`.
  The scanner does the same for `MarkerTimelineWidget` (header
  `scanner.marker_timeline`) in `scanner.py`.
- **Frame-accurate step.** mpv's `frame_step` plays a fraction of a second
  of audio (the audio decodes before the mute property takes effect).
  `MpvBridge.step_frames` instead seeks by `1/container_fps` and forces
  `pause = True`. This is silent.
- **mpv D3D gotcha.** Already mentioned — close the splash before
  constructing the player. See "Splash flow" above.
- **`Position` and seek clamping.** Boundary positions in
  `end_segment`/`start_segment` are clamped to `(start, end)` of the active
  segment. A no-op returns `False`.
- **Tag field programmatic updates.** `_write_tags_to_form` uses
  `blockSignals(True/False)` around `setText` so the `textChanged`
  handler (`on_tag_edited`) doesn't fire on initial load and falsely mark
  the model dirty.
- **Keyframe nav epsilon.** `_KEYFRAME_EPSILON = 0.05` seconds. Without
  it, mpv seeking to a keyframe near the current position can re-seek to
  the same spot and the buttons feel broken. Applied symmetrically in
  `next_keyframe` / `prev_keyframe`.

## What's built (and what's next)

**Built:**
- `.cmct` sidecar format and `SegmentModel` (transition-point model).
- Timeline rendering: alternating red/green/blue segments, ignored
  segments dimmed, active-segment highlight, zoom-to-active or
  fit-whole via toggle.
- Editing state machine: End Seg, Start Seg, Merge Next, Skip, Stage,
  Undo, Active navigation, Toggle Zoom.
- Tag form with lock system and dirty indicator.
- Keyframe navigation via ffprobe-scanned I-frames.
- Frame-accurate stepping (silent).
- Cross-platform binary resolution: `shared/environment.get_binary_path`
  resolves ffmpeg/ffprobe/mpv per OS under `bin/<os>/`; both the editor and
  the scanner prepend it to `PATH` via `setup_environment`.
- Shared library layer used by both wizards: `shared/mpv` (MpvBridge,
  create_mpv_player, scan_keyframes), `shared/timeline`, `shared/segments`,
  `shared/ffmpeg`, `shared/environment`, `shared/ui_loader`.
- Scanner skeleton: 2-minute preview clip load (stream copy into `temp/`),
  embedded mpv playback, transport + frame/keyframe stepping, Place
  Boundary + Undo on the User Marked timeline (in-memory, no `.cmct`),
  and two marker timelines driven by the bridge.
- Automated boundary detection (Test Scan): `ffmpeg blackdetect` on the
  2-minute preview, slider-mapped to `d = frames / fps` and
  `pix_th = level / 100`; skips `black_end:N/A` runs; stamps one midpoint
  `(T1 + T2) / 2` per black run into the upper Scanner Preview timeline
  (`timelineWidget1`) via `MarkerTimelineWidget.add_marker`.

**Next:**
- **Finished** — run the same `blackdetect` detector against the *full*
  source video (not the 2-minute preview) and apply the resulting midpoint
  boundaries to the project. (Test Scan on the preview is done.)
- **Export / smart cut** (the `Export` button in the readme's flow):
  per non-ignored segment, find innermost keyframes bracketing the cut
  points, lossless-copy between them, transcode the partial-keyframe
  ends, concatenate. A simpler `clip_to_temp` (stream copy) already lives
  in `shared/ffmpeg.py`; the smart-cut version goes there.

## Gaps to be aware of

- `prototypes/` contains earlier iterations of the editor. Treat them
  as historical — the active code is in `editor/` (and the scanner in
  `scanner/`).
- Automated boundary detection (Test Scan `blackdetect`, midpoint markers
  into the Scanner Preview timeline) is wired. Remaining: Finished (full-
  source scan) and the smart-cut export. The editor still loads a one-
  segment placeholder `.cmct`; the scanner's detected boundaries are not
  yet routed into it.
