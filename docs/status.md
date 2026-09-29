# Status: what is built, what is next

A map of what works today, grouped by area with a pointer to the doc that owns
the detail, then the remaining work and the known traps.

Applies to: the whole tree. Start here when you need to know whether something
exists before you build it.

Related: [architecture.md](architecture.md), [segment-model.md](segment-model.md),
[scanner.md](scanner.md), [naming-and-organization.md](naming-and-organization.md),
[packaging.md](packaging.md), [source-install.md](source-install.md),
[testing.md](testing.md).

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

- Per-platform binary resolution: `shared/environment.get_binary_path` searches
  `bin/<os>/` first and, only on a platform that does not bundle binaries
  (macOS, Linux), the absolute system prefixes. Windows bundles and **refuses**
  rather than falling through.   `resolve_mpv_library` / `load_mpv_library` / `mpv_import_context` resolve
  libmpv by absolute path, map it, and **answer python-mpv's own lookup** for it
  before `import mpv`, which is what makes the source installs possible:
  python-mpv scans `%PATH%` on Windows, and on macOS it scans system
  directories and raises rather than falling back to a loaded image.
  Every call site uses the resolved absolute path rather than a bare binary
  name.
- `shared/ffmpeg.check_video_encoder` probes `ffmpeg -encoders` for `libx264`
  once and refuses an export batch up front if it is missing, so a source install
  on a minimal ffmpeg reports one named problem rather than one failed clip per
  segment. The export commit falls back from `os.link` to an exclusive create
  plus copy, so a library on exFAT or a network mount exports rather than
  refuses.
- Shared library layer used by the wizards and Settings: `shared/mpv`
  (MpvBridge, create_mpv_player, scan_keyframes), `shared/timeline`,
  `shared/segments`, `shared/ffmpeg`, `shared/environment`, `shared/scheme`,
  `shared/naming`, `shared/paths`, `shared/exporting`, and `shared/ui_loader`.
- Portable packaging (`packaging/commcut.spec` + `packaging/build.py`): a
  self-extracting onefile `commcut.exe` (Python + PySide6 + the app + the `.ui`
  files) assembled into `dist/commcut-portable/` alongside the `bin/win/`
  binaries and the `import/`+`export/` placeholders. Every window is built
  in-process by `shared/session.py`; there is one entry point and nothing is
  re-executed, so the payload is extracted once per run rather than once per
  window. Pre-flight rejects a
  non-Windows host and Git LFS pointer binaries; post-build asserts the `.ui`
  layout and that `prototypes/`/`tests/` were not bundled. `--zip` adds the
  distributable archive plus a sha256 sidecar, refused unless the exe, all
  three `bin/win/` binaries and both placeholders are present.
- Releases are tag-driven: a `v*` push runs
  `.github/workflows/release.yml`, which runs the suite, builds, and attaches
  the archive to a **draft** release. `shared/version.py` holds the version and
  the tag is refused if it disagrees with it. Windows only, unsigned (SmartScreen
  warns), and CI does not launch the exe — see
  [packaging.md](packaging.md#releases).

- **macOS and Linux run from source**, not from a build — see
  [source-install.md](source-install.md). No frozen build exists for them, and
  none is planned: a frozen macOS build would resolve `install_root()` inside a
  signed `.app` bundle, which is read-only.

Detail in [packaging.md](packaging.md) and [source-install.md](source-install.md).

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
  when it lands. It replaces the transcode inside the existing worker, not the
  worker itself: `shared/ffmpeg.py:execute_export_plan` already takes
  `on_progress`/`should_cancel` and `editor/editor.py:ExportWorker` already runs
  it off the GUI thread behind a progress dialog.
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
- The end of the Editing Wizard is finished: staging the last segment reports
  that the set is complete and names **Finished - Export**, and the export
  summary screen then reports what became of it. Both were `print()`s, which
  go nowhere in a windowed build. Detail in
  [segment-model.md](segment-model.md#the-end-of-editing) and
  [naming-and-organization.md](naming-and-organization.md#the-summary-screen).
- Export runs on a worker `QThread` behind a non-modal `QProgressDialog`, and
  the editor is disabled for the duration, so a long batch is legible but not
  interruptible by editing; the progress bar counts clips, so it sits still for
  the length of one long segment. A run that finishes — clean or with per-clip
  failures — then gets a summary screen saying what was written, where, and what
  failed, with **Back to main menu** closing the editor and handing the user back
  to the main menu, which is still open underneath it. **Export the
  rest** retries a partial run's unwritten clips, which is what makes a
  per-clip failure recoverable: the preflight refuses a destination that exists,
  so a retry needs the clips the run already wrote skipped — see
  [naming-and-organization.md](naming-and-organization.md#the-export-runs-off-the-gui-thread).
  The remaining threading work is the scanner's
  `scan_keyframes` pre-pass, which is still called inline from `__main__` in
  `editor/editor.py` (the `PreScanWorker` next to it is scaffolding, unwired).
- The boundary peek has no settings toggle; it is always on at 15 frames /
  450ms.
- `End Seg` only works forward. Moving the *previous* segment's end back to the
  playhead is the natural next addition and is a small block in
  `place_end_boundary` (see [segment-model.md](segment-model.md#end-seg)).
- The scanner's detector is `blackdetect` only. No silence detection, no
  heuristics for rapid concurrent detections or long spans without one.
- **The one-process-per-window model has been removed.** Every window is now a
  window in one process, on one event loop, with `shared/session.py` owning the
  stack. `launch_command`, the `--window` dispatcher, the per-window `run()`
  entry points and the `scanner/` folder-shadowing guard are all gone with it.
  The trigger for that rule — constructing an mpv player while another top-level
  window is foreground — was tested directly in `experiments/mpv_foreground/`:
  120 runs across six cases on Windows with mpv `v0.41.0-39-ga58dd8ac4`, the
  shipped `direct3d` driver, a verified-foreground window, a frameless splash,
  and three concurrent presenting players, produced **no hang**. The foreground
  was verified rather than assumed, and a negative control proved the harness
  detects a block at that exact step. So the stated trigger does not reproduce
  on the development machine. It is not proven absent elsewhere: one GPU, one
  driver, one mpv build, and bare windows rather than the real editor or
  scanner, so the splash-closing precaution stays. The trade made is that a hard
  fault inside `libmpv-2.dll` now takes down the whole app rather than one
  window; that is a real cost, accepted while the project is pre-release, and
  worth revisiting once the packaged build has run on a user's machine. The
  method and its limits are in
  [experiments/README.md](../experiments/README.md); the architecture is in
  [architecture.md](architecture.md#one-process-one-event-loop-a-stack-of-windows).
- **macOS and Linux are unverified.** The resolution logic is cross-platform and
  the suite covers it on any host, but nothing here has been run on either
  platform. The open assumptions, in the order worth checking: whether a
  loadable `libmpv` exists (a `brew install mpv` gives the *player*, not the
  library — `COMMCUT_MPV_LIB` is the escape hatch); whether `vo=gpu` renders
  into an `NSView*`; and, on Linux, whether `wid` embedding works at all under
  Wayland. A green suite proves none of these, because `FakeBridge` stands in
  for libmpv by design. See [source-install.md](source-install.md#what-has-not-been-verified).
