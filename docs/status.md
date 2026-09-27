# Status: what is built, what is next

A map of what works today, grouped by area with a pointer to the doc that owns
the detail, then the remaining work and the known traps.

Applies to: the whole tree. Start here when you need to know whether something
exists before you build it.

Related: [architecture.md](architecture.md), [segment-model.md](segment-model.md),
[scanner.md](scanner.md), [naming-and-organization.md](naming-and-organization.md),
[packaging.md](packaging.md), [testing.md](testing.md).

## Built

### Editing Wizard (`editor/`, `shared/segments.py`, `shared/timeline.py`)

- `.cmct` sidecar format and `SegmentModel` (transition-point model) — see
  [segment-model.md](segment-model.md).
- Timeline rendering: alternating red/green/blue segments, ignored segments
  dimmed, active-segment highlight, zoom-to-active or fit-whole via toggle.
- Editing state machine: End Seg, Start Seg, Merge Next, Skip, Stage, Undo,
  Active navigation, Toggle Zoom.
- Boundary peek: an active-segment change flashes 15 frames past the boundary
  (450ms) and returns to the exact cut point, so stepping through black
  boundaries shows the clip. Always on; `BoundaryPreview.frames = 0` is the off
  switch a future settings key would drive.
- Tag form with lock system and dirty indicator.
- Keyframe navigation via ffprobe-scanned I-frames, and silent frame-accurate
  stepping.

### Scanner (`scanner/`, `shared/ffmpeg.py:clip_to_temp`)

- 2-minute preview clip load (stream copy into `temp/`), embedded mpv playback,
  transport + frame/keyframe stepping, Place Boundary + Undo on the User Marked
  timeline (in-memory, no `.cmct`), and two marker timelines driven by the
  bridge.
- Automated boundary detection (Test Scan): `ffmpeg blackdetect` on the
  2-minute preview, slider-mapped to `d = frames / fps` and
  `pix_th = level / 100`; skips `black_end:N/A` runs; stamps one midpoint
  `(T1 + T2) / 2` per black run into the upper Scanner Preview timeline.
- Finished (full-source scan): the same detector run against the full source
  video (not the 2-minute preview); midpoint boundaries are written as `.cmct`
  segment starts via `SegmentModel` and the Video Editor is launched
  automatically.
- Scanner→Editor handoff: if `sidecar_path(source)` already exists, the scanner
  launches the editor and exits, so an existing `.cmct` is never overwritten (the
  source used is `shared.segments.source_video_path()`, the single definition
  both the scanner and the editor go through).

Detail in [scanner.md](scanner.md).

### Naming, organization, and export (`shared/scheme.py`, `shared/naming.py`,
`shared/paths.py`, `shared/exporting.py`, `shared/ffmpeg.py`)

- File naming scheme parser: `render_filename(scheme, tags)` produces a
  filename stem from a template.
- Folder organization resolver: strict folder grammar, required
  Network/Type/Time Period placeholders, optional multi-folder groups, tag
  sanitation, portable component validation, and safe relative component output.
- Export planning (`shared/exporting.py`): strict model/settings validation,
  four-tag requirements, lock materialization, normalized within-batch and
  existing-destination collision checks, complete relative-path limits, and
  reparse-point/traversal defenses.
- Named export (full-segment transcode in `shared/ffmpeg.export_named_model`,
  wired to the `exportButton` in `editor/editor.py`): persists the in-memory
  model, applies session locks, loads both schemes, plans all keep-segments,
  creates their directory trees, and frame-accurately re-encodes each named MP4
  (libx264/aac, not `-c copy`). Unique temporary files and no-clobber commits
  prevent overwrites; per-clip failures are returned as a partial result.

Detail in [naming-and-organization.md](naming-and-organization.md).

### Windows and settings

- Main menu, picker, and source-video policy (`shared/sources.py`): what the
  picker offers, what a window will accept, and the "nothing to open" messages.
- Settings (`settings/settings.py`): independent file/folder scheme defaults,
  validation, production-resolver previews, atomic `QSaveFile` persistence, and
  cancel/window-close restoration.

Detail in [architecture.md](architecture.md) and
[naming-and-organization.md](naming-and-organization.md).

### Shared library and packaging

- Cross-platform binary resolution: `shared/environment.get_binary_path`
  resolves ffmpeg/ffprobe/mpv per OS under `bin/<os>/`; both the editor and
  the scanner reach them through `setup_environment`. Every call site uses the
  resolved absolute path rather than a bare binary name.
- Shared library layer used by the wizards and Settings: `shared/mpv`
  (MpvBridge, create_mpv_player, scan_keyframes), `shared/timeline`,
  `shared/segments`, `shared/ffmpeg`, `shared/environment`, `shared/scheme`,
  `shared/naming`, `shared/paths`, `shared/exporting`, and `shared/ui_loader`.
- Portable packaging (`packaging/commcut.spec` + `packaging/build.py`): a
  self-extracting onefile `commcut.exe` (Python + PySide6 + the app + the `.ui`
  files) assembled into `dist/commcut-portable/` alongside the `bin/win/`
  binaries and the `import/`+`export/` placeholders. Child windows are
  re-executions of the same binary via `--window <name>`. Pre-flight rejects a
  non-Windows host and Git LFS pointer binaries; post-build asserts the `.ui`
  layout and that `prototypes/`/`tests/` were not bundled.

Detail in [packaging.md](packaging.md).

### Not built

The full vision in `README.md` has three pieces; two are not started:

- The **Rename Wizard** (batch-rename already-cut clips) does not exist.
- **Smart-cut export** does not exist — export re-encodes each whole segment
  instead. See "Next" below.
- The import and export folders are fixed beside the executable (see "Next").

## Next

- **Smart-cut export**: per non-ignored segment, find the innermost
  keyframes bracketing the two cut points, lossless-copy between them,
  transcode only the partial-keyframe ends, then concat. The placeholder
  `clip_to_temp` (stream copy) still lives in `shared/ffmpeg.py`; the
  keyframe-bracketed smart-cut version replaces/augments `export_named_model`
  when it lands. Also wire export into a background `QThread` (today it runs on
  the GUI thread, which blocks the editor while cutting).
- **Choosing folders**: import/ and export/ are fixed beside the executable for
  this alpha, and the Settings rows say so. When they become configurable,
  `shared/sources.py:import_folder()` and the export root in
  `shared/exporting.py` are the two places that resolve them, and the picker's
  folder label follows `import_folder()` automatically.

## Known gaps and traps

- `prototypes/` contains earlier iterations of the editor. Treat them
  as historical — the active code is in `editor/` (and the scanner in
  `scanner/`).
- Automated boundary detection is fully wired: Test Scan (preview, in-memory
  midpoints), Finished (full-source `blackdetect` → `.cmct` → editor), and
  the Scanner→Editor handoff when a `.cmct` already exists.
- Export is present as a named full-segment transcode; the smart-cut
  (keyframe-bracketed copy+transcode+concat) version is the remaining piece.
  The legacy numeric `export_segment_clips()` helper still exists for
  compatibility, but Editor export uses the named planner/executor path.
- Export runs synchronously on the GUI thread; move to a worker `QThread`
  (see `PreScanWorker` in `editor/editor.py`) for the smart-cut step so the
  editor stays responsive.
- The boundary peek has no settings toggle; it is always on at 15 frames /
  450ms.
- `End Seg` only works forward. Moving the *previous* segment's end back to the
  playhead is the natural next addition and is a small block in
  `place_end_boundary` (see [segment-model.md](segment-model.md#end-seg)).
- The scanner's detector is `blackdetect` only. No silence detection, no
  heuristics for rapid concurrent detections or long spans without one.
