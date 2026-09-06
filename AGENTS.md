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

The working tree currently contains the **Editing Wizard's** video editor
module. The Rename Wizard and library/export layers are not yet built.

## Layout

```
commcut/
├── README.md                # Project spec (source of truth for scope)
├── AGENTS.md                # This file
├── core.py                  # Cross-platform ffmpeg/ffprobe helpers
│                            # (NOT currently used by editor/ — see "Gaps")
├── bin/win/                 # Bundled Windows binaries
│   ├── ffmpeg.exe
│   ├── ffprobe.exe
│   └── libmpv-2.dll
├── editor/                  # The Editing Wizard (current focus)
│   ├── editor.py            # Entry point: MpvBridge, PreScanWorker, Editor
│   │                        # window, editing state machine, splash flow
│   ├── segments.py          # SegmentModel + .cmct sidecar persistence
│   ├── timeline.py          # TimelineWidget (custom QWidget, paintEvent-based)
│   └── editorwindow.ui      # Qt Designer file; promoted TimelineWidget
├── prototypes/              # Earlier exploration / alternatives
│   ├── BasicUI/             # First prototype
│   └── VideoEditor/         # Pre-rename copy of the editor module
└── import/                  # Test source videos
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
`editor/segments.py:SegmentModel`. Operations:
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

## Architecture: the MpvBridge pattern

The editor communicates with libmpv through a single **`MpvBridge(QObject)`**
that owns the mpv player and exposes:

- **Qt signals** (state up): `pauseChanged`, `positionChanged`,
  `durationChanged`, `fileLoaded`, `playbackEnded`. These are fed by
  `player.observe_property(...)` callbacks. **Observers fire on mpv's worker
  thread; emitting Qt signals is thread-safe and lands on the GUI thread.**
- **Methods** (commands down): `toggle_play`, `seek_exact`, `step_frames`,
  `set_keyframes`, `next_keyframe`, `prev_keyframe`, `load_file`,
  `load_and_play`.

The editor window and widgets never read mpv state directly — they only
mirror what the bridge announces via signals.

## Splash flow (pre-work before editor appears)

`__main__` shows a `QSplashScreen`, then runs background work on a
`QThread` (currently just `scan_keyframes` via ffprobe). On the worker's
`finished` signal, the editor window is constructed, keyframes are
injected, and the splash is closed.

**Hard-won gotcha:** mpv's Direct3D device initialization hangs when
another top-level window (the splash) is the active window at construction
time. If you ever bring the splash back, close it *before* constructing
the mpv-backed widget. `app.processEvents()` is called once after
`splash.show()` to ensure the splash actually paints.

The `PreScanWorker` class is the place to add more pre-work stages
(scene detection, waveform, etc.) — emit a progress message between each
stage. For now, scanning is sub-second so a `QThread` is overkill; the
editor uses a synchronous `scan_keyframes` call inside the splash.

## Key conventions and gotchas

- **Bundled binaries.** `editor/editor.py` prepends `bin/win` to `PATH` so
  `python-mpv` and `ffprobe` resolve. The editor is **Windows-only** until
  someone replaces this with a cross-platform path lookup (see `core.py`).
- **Promoted widget.** The timeline is a custom `QWidget` promoted in
  `editorwindow.ui` as `TimelineWidget` / header `timeline`. PySide6's
  `QUiLoader` does not auto-resolve promoted widgets reliably, so
  `editor.py` uses a `UiLoader(QUiLoader)` subclass whose `createWidget`
  is overridden to instantiate `TimelineWidget` directly. Register the
  class with `loader.register_widget(TimelineWidget)` before `loader.load()`.
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

**Next:**
- **Automated boundary detection** (the whole point of the Editing
  Wizard). ffprobe `blackdetect` outputs to **stderr** in key=value
  format — `black_start:T` and `black_end:T black_duration:D` per black
  region. The plan is to emit both T1 and T2 as transition points (one
  per black edge), creating an explicit "black gap" segment that gets
  marked `ignored: true`. The user then edits to merge/remove as needed.
- **Export / smart cut** (the `Export` button in the readme's flow):
  per non-ignored segment, find innermost keyframes bracketing the cut
  points, lossless-copy between them, transcode the partial-keyframe
  ends, concatenate. `core.py:basic_transcode_cut` is a simpler version
  already; the smart-cut version goes here.
- **Cross-platform binary resolution** via `core.py:get_binary_path`.

## Gaps to be aware of

- `core.py` is currently unused by `editor/`. It contains
  `get_binary_path` (cross-platform ffmpeg/ffprobe resolution),
  `get_video_specs`, and `basic_transcode_cut` — all of which the editor
  will need for export and a real cross-platform story. The editor's
  `probe_duration` in `segments.py` duplicates part of
  `get_video_specs`; the import path would be
  `from core import get_binary_path, get_video_specs`.
- `prototypes/` contains earlier iterations of the editor. Treat them
  as historical — the active code is in `editor/`.
- No automated boundary detection yet — the editor currently loads a
  one-segment placeholder `.cmct` and relies entirely on manual editing
  to produce ground-truth data. The detection step is the next major
  feature.
