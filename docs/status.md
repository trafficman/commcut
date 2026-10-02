# Status: what is built, what is next

A map of what works today, grouped by area with a pointer to the doc that owns
the detail, then the remaining work and the known traps.

Applies to: the whole tree. Start here when you need to know whether something
exists before you build it.

Related: [architecture.md](architecture.md), [segment-model.md](segment-model.md),
[scanner.md](scanner.md), [naming-and-organization.md](naming-and-organization.md),
[tag-vocabulary.md](tag-vocabulary.md),
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
- The clip record (`shared/records.py`): every exported clip gets a `<stem>.cnfo`
  beside its video holding its raw tags and the segment it came from, rendered
  between a successful encode and the commit of the video. The filename and
  folder are a projection of the tags, and nothing parses a filename back.
- Tag suggestions (`shared/vocabulary.py`): nine of the ten tag fields are
  editable combos offering values the user has already used, drawn from
  `vocabulary.json`, leading with the most recently used ones for this source.
  Title stays a plain text box — it is unique per clip, so there is nothing to
  suggest. Seeded with a `filler_type` list. The file is advisory — nothing
  validates against it, so it is allowed to be wrong. →
  [tag-vocabulary.md](tag-vocabulary.md)

Detail in [naming-and-organization.md](naming-and-organization.md).

### Windows and settings

- Main menu and source-video policy (`shared/sources.py`): the native file dialog
  the **Editor** button opens, what a window will accept, the writable-folder rule
  the `.cmct` sidecar requires, and the messages for a refused path.
- Settings (`settings/settings.py`): independent file/folder scheme defaults,
  validation, production-resolver previews, atomic `QSaveFile` persistence,
  cancel/window-close restoration, and the **Sync from Export Library** button
  that reconciles `vocabulary.json` with the clips on disk.
- Library catalog (`shared/catalog.py`): the walk that reads the export library
  back — a record with a sibling video is a clip — plus the sync that uses it.
  No window displays it yet. →
  [naming-and-organization.md](naming-and-organization.md#reading-the-library-back-the-catalog)
- Library Importer **backend** (`shared/importing.py`): planning, execution, the
  occupied-destination rule, the transfer modes, untagged discovery, and the
  evidence-ranked matching the mesh wizard will read. No screens yet — nothing
  lets a user start an import.   → [importing.md](importing.md)

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

- The **Rename Wizard** (batch-rename already-cut clips) does not exist. The walk
  it needs now exists — see "Next".
- **Smart-cut export** does not exist — export re-encodes each whole segment
  instead. See "Next" below.
- The import and export folders are fixed beside the executable (see "Next").
- **Nothing displays the library.** The catalog and one button that reads it are
  built; there is no browser, and the file dialog has no tags.
- **No recent sources.** A source video can be picked from anywhere, and a
  previously scanned rip is routed straight to the editor by its `.cmct` — but a
  native dialog cannot label a file the way the old picker did, so resuming one
  means remembering where it is. A recent-sources list needs a persistence
  decision (`QSettings`, or a new JSON file beside `settings.json`) that has not
  been made. → [architecture.md](architecture.md#the-source-video-is-picked-from-anywhere)

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
  this alpha, and the Settings rows say so. A **source video** is not one of these
  choices — the main menu's file dialog takes any video from any folder, which is
  what freed `import/` for importing finished clips. When the two folders become
  configurable, `shared/sources.py:import_folder()` and
  `shared/exporting.py:export_folder()` are the places that resolve them. Both
  are single functions on purpose: the export root was spelled out in three
  places before `export_folder()` existed — `editor/editor.py` joined it onto
  `PROJECT_ROOT`, and `shared/ffmpeg.py` kept a private `_export_dir()` — and a
  fourth was about to appear for the library walk. They happened to agree, since
  `setup_environment` returns `install_root()` as its `project_root`, but three
  spellings of one path is three places for the configurable version to be missed.
- **Reading the library back, and syncing the vocabulary.** Both are built.
  `shared/catalog.py:build_catalog` walks `export/` and applies the scan rule
  the writer is built around — **a record with a sibling video is a clip**,
  anything under `export/` without a record is ignored — returning the clips, the
  records it could not read, and whether it was cancelled. Settings gained a
  **Sync from Export Library** button that unions the library's tags into
  `vocabulary.json` and prunes what no clip uses, on a worker thread behind a
  progress dialog.

  Two rules in that sync are worth knowing before changing either. **A shipped
  default is never removed** — a default is the project's starter vocabulary
  rather than library residue, so a user who has exported one clip has not
  thereby said anything about the other ten filler types, and pruning on that
  basis would collapse a new user's dropdowns on the first press. The rule lives
  in `Vocabulary.prune_to`, next to `DEFAULT_VALUES`, so the next caller of it
  cannot reintroduce the bug. And **an empty library does not prune at all**,
  which is a separate rule covering the other half: it keeps a fresh install's
  *user* values from being deleted by the first press. A cancelled sync likewise
  writes nothing, because pruning against half a library would delete every value
  the other half uses.

  Still not built: **a library browser**. The walk exists and one button reads
  it; nothing shows a user what is in `export/`. The **Rename Wizard** is the next
  consumer — it reads tags from a record and rewrites the path, never the reverse.
  Counts per value are derivable from `Catalog.clips` when something wants them,
  and deliberately are not cached. →
  [naming-and-organization.md](naming-and-organization.md#reading-the-library-back-the-catalog),
  [tag-vocabulary.md](tag-vocabulary.md#syncing-from-the-library)
- **The Library Importer's screens.** The backend is built and has no window over
  it: there is no way to start an import. What remains is the scan summary, the
  Auto Library Mesh Wizard, the manual tag queue with its mpv preview, and the
  review page — one window, so `_BUILDERS`, a `.ui`, `UI_DATAS`, `REQUIRED_UI` and
  the `WINDOW_UI` table in `tests/test_frozen_mode.py`, which are already held to
  each other. The tag-form helpers have to move out of `editor/editor.py` into
  `shared/` first, or the queue's form will drift from the editor's.
  → [importing.md](importing.md)

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
- **Exactly one window is visible at a time, and the main menu no longer sits
  open in the background.** `shared/session.py` shows one of {menu, scanner,
  editor, settings}; opening a window takes down the one it replaces
  and shows the new one, and a non-menu window closing brings the menu back. The
  menu is hidden and reused rather than rebuilt, so there is only ever one menu
  and one taskbar entry. This replaced a stack in which the menu stayed open
  behind everything else, which was both easy to lose behind another window and
  — as a window permanently behind another one — a standing source of focus and
  activation edge cases. Two details are load-bearing: a replaced window is
  **closed** rather than hidden, because closing is what runs the `closeEvent`
  that shuts its mpv player down; and the new window is built *before* the old
  one is taken down, so a failed build leaves the user where they were and a
  window opened from a button handler is not destroyed from inside its own
  signal. Modal dialogs (the editor's export progress and summary) are owned by
  their window and are not part of this. →
  [architecture.md](architecture.md#one-process-one-event-loop-one-visible-window)
- **A window with an mpv player must shut it down before it is destroyed, and
  that was not being done.** `create_mpv_player` hands mpv the native handle of
  a child frame, so a player left alive when the window closes has outlived the
  handle it renders into. Under the one-process-per-window model this never
  surfaced — closing a window ended the process, and the OS reclaimed the handle
  and libmpv's threads together. It is reported as freezing the app to the mouse
  after closing the scanner or the editor, while the process stays alive:
  timers fire, posted events land, and a synthetic click still opens a window, so
  it is an input lockout rather than a hung thread.
  `MpvBridge.shutdown()` now detaches the observers and terminates the player
  from `closeEvent` on both windows. Detaching first matters on its own — the
  observers fire on mpv's worker thread, and a callback that lands after teardown
  is an mpv command run against a player that has already gone, which is
  observable. **The mechanism behind the freeze is still not identified**: a
  blocked GUI thread, a Win32 mouse capture, a Qt mouse grab, and a dead window
  under the cursor were each measured after a close and each came back negative.
  What is established is that the teardown was missing, that it is wrong to
  release a native resource after the handle it depends on, and that it is the
  one change that separates the reported frozen and working cases. See
  [experiments/README.md](../experiments/README.md#mpv_teardown) — a person is
  still the oracle for the freeze.
- **macOS and Linux are unverified.** The resolution logic is cross-platform and
  the suite covers it on any host, but nothing here has been run on either
  platform. The open assumptions, in the order worth checking: whether a
  loadable `libmpv` exists (a `brew install mpv` gives the *player*, not the
  library — `COMMCUT_MPV_LIB` is the escape hatch); whether `vo=gpu` renders
  into an `NSView*`; and, on Linux, whether `wid` embedding works at all under
  Wayland. A green suite proves none of these, because `FakeBridge` stands in
  for libmpv by design. See [source-install.md](source-install.md#what-has-not-been-verified).
