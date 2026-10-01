# Architecture

How the app is put together: the repository layout, the one-process-per-window
model, how a source video travels between windows, the shared library, the
mpv bridge, and the splash flow.

Applies to: `main.py`, `mainwindow.py`, `mainwindow.ui`, `shared/environment.py`,
`shared/diagnostics.py`, `shared/mpv.py`, `shared/timeline.py`,
`shared/ui_loader.py`, `shared/sources.py`.

Related: [packaging.md](packaging.md) (the same layout, frozen),
[segment-model.md](segment-model.md) (the Editing Wizard),
[scanner.md](scanner.md), [naming-and-organization.md](naming-and-organization.md),
[testing.md](testing.md).

## Repository layout

```
commcut/
├── README.md                # Project spec (source of truth for scope)
├── AGENTS.md                # Agent orientation + index into docs/
├── docs/                    # This documentation set
├── bin/                     # Bundled binaries, one subfolder per OS
│   ├── win/                 # Windows binaries (ffmpeg.exe, ffprobe.exe, libmpv-2.dll)
│   ├── linux/               # Linux binaries (placeholders, none shipped yet)
│   └── mac/                 # macOS binaries (placeholders, none shipped yet)
├── import/                  # Source videos; the picker offers what is in here
├── export/                  # Named clips are written here
├── temp/                    # Scratch output (e.g. 2-min scanner preview clips)
├── commcut.log              # Written beside the exe (override with COMMCUT_LOG)
├── main.py                  # Application entry point: argv dispatcher + main menu
├── mainwindow.py            # MainWindow: launches the picker / settings as
│                            # child processes
├── mainwindow.ui            # Qt Designer file for the main menu
├── packaging/               # PyInstaller build (see packaging.md)
│   ├── commcut.spec         # onefile (default) and onedir modes
│   ├── build.py             # pre-flight checks + portable folder assembly
│   └── README.md            # build instructions and the shipped layout
├── picker/                  # Source video picker (front door of the wizard)
│   ├── picker.py            # Lists import/, launches the scanner on the choice
│   └── pickerwindow.ui      # List + status line + Refresh/Open/Cancel
├── settings/                # Standalone Settings window
│   ├── settings.py          # Scheme persistence, validation, previews, atomic save
│   └── settingswindow.ui    # File/folder scheme editors and live previews
├── editor/                  # The Editing Wizard (current focus)
│   ├── editor.py            # Entry point: Editor window, editing state machine,
│   │                        # splash flow, PreScanWorker scaffolding, ExportWorker
│   └── editorwindow.ui      # Qt Designer file; promoted TimelineWidget
├── scanner/                 # The Segment Scanner (detector + review)
│   ├── scanner.py           # Entry point: 2-min preview clip load, mpv playback,
│   │                        # transport, two marker timelines, detector sliders
│   ├── marker_timeline.py   # MarkerTimelineWidget (playhead + vertical marker lines)
│   └── scannerwindow.ui     # Qt Designer file; promoted MarkerTimelineWidget
├── shared/                  # Cross-module library (editor + scanner + settings)
│   ├── environment.py       # frozen-aware roots, per-OS binaries, mpv_import_context
│   ├── session.py           # the QApplication's one visible window: Shell, _BUILDERS
│   ├── diagnostics.py       # log file, excepthook, fatal() startup reporting
│   ├── mpv.py               # MpvBridge + its shutdown, create_mpv_player, scan_keyframes
│   ├── timeline.py          # TimelineWidget (segments, zoom/scroll)
│   ├── segments.py          # SegmentModel + .cmct persistence, probe_duration
│   ├── sources.py           # import/ policy: what can be opened, what is offered
│   ├── ffmpeg.py            # clip_to_temp, export_named_model, execute_export_plan
│   ├── scheme.py            # Shared tag aliases, AST, parser, strict parser, renderer
│   ├── naming.py            # Filename scheme policy + render_filename()
│   ├── paths.py             # Folder scheme validation, sanitization, safe components
│   ├── exporting.py         # Settings snapshot, destination planner, export preflight
│   ├── catalog.py           # build_catalog (read the library back), sync_vocabulary
│   └── ui_loader.py         # UiLoader subclass for promoted custom widgets
├── tests/                   # pytest suite (see testing.md)
└── prototypes/              # Earlier exploration / alternatives
    ├── BasicUI/             # First prototype
    └── VideoEditor/         # Pre-rename copy of the editor module
```

## One process, one event loop, one visible window

Every window in this app is a window, not a process. `main.py` is the only entry
point: it builds the one `QApplication`, constructs `MainWindow`, installs a
`Shell` over it, and runs the event loop. There is no `--window` flag and no
per-window script, and each window module exposes a `create(...)` builder rather
than a `run()` entry point.

`shared/session.py` shows **exactly one** of {menu, picker, scanner, editor,
settings} at a time. Opening a window builds it, takes down whatever was on
screen, and shows the new one. When a non-menu window goes away the menu comes
back; when the menu goes away the app quits. Modal dialogs are not part of this —
the editor's export progress and summary dialogs are `QDialog`s owned by the
editor, so they travel with whatever window opened them.

This replaced a stack in which the main menu stayed open behind everything else.
The stack was carrying two costs that were not obvious at the time: the menu
could be lost behind another window and put a second entry in the taskbar, so the
app did not present as one thing; and a window permanently behind another one is
a permanent source of edge cases about focus and activation, which is the sort of
thing that produces bugs nobody can attribute. With one window there are still no
guarantees, but there is one rule instead of a set of interactions.

Two details are load-bearing:

- **A window the shell opens replaces the one on screen by closing it**, not by
  hiding it. Closing is what runs `closeEvent`, and for the scanner and the
  editor that is the only place their mpv player gets shut down while its video
  frame still has a native handle. Hiding one of those would keep a live player
  attached to a window the user cannot see. The menu is the exception: it is
  hidden and reused, because it holds two buttons and rebuilding it would
  re-parse `mainwindow.ui`.
- **The new window is built before the old one is taken down.** A build pumps the
  event loop to drive its splash, so closing first would mean destroying the
  outgoing window from inside the button handler that opened the new one. Built
  second, `WA_DeleteOnClose` only posts a deferred delete, which cannot run until
  that handler has returned. The order also means a build that fails leaves the
  user exactly where they were.

`Shell.open_safely` is the only way a window is opened. Every window opens the
next one from inside a button handler, and an exception escaping one of those
reaches the event loop: with one process that takes down the window the button
belonged to, where before it only killed a child. It logs the failure and shows
a `QMessageBox` instead, and returns `None`.

A builder has a two-way contract: return a window, or raise. One exception to
the second is `shared/session.py:OpenInstead`, which a builder raises to say
"I am not opening; open *this* instead", and the shell resolves it. The scanner
is the case: a video that already has a `.cmct` must not be re-scanned, so the
rule is the scanner's, but the routing is not — it has no window to show. This
was a `None` return originally, and it failed the way a sentinel does: the shell
took the `None` for a window and reported the already-scanned video as
unopenable instead of opening the editor. `tests/test_session.py` guards it.

The builders are imported lazily inside `Shell.open`, so the main menu — the
first thing that runs — does not pull in libmpv.

## Why there is one process

One process per window used to be the rule, because constructing an mpv player
(direct3d) while another top-level window is foreground was believed to deadlock
on Windows. There was never a recorded reproduction, and
`experiments/mpv_foreground/` ran the claim directly: 120 runs across six cases,
no hang, with the foreground window verified rather than assumed — including the
frameless-splash case and three concurrent presenting players in one process. See
[experiments/README.md](../experiments/README.md) for the method and its limits.
One GPU, one driver, one mpv build, and bare windows rather than the real editor,
so it does not prove the hazard absent everywhere; the splash-closing precaution
in the scanner and the editor is kept for that reason.

What the change cost is worth stating plainly: a hard fault inside
`libmpv-2.dll` used to kill one window and leave the main menu running. Now it
kills the app. That was a deliberate trade, made while the project is pre-release
and the packaged build has never run on a user's machine.

## Tearing down a player

`create_mpv_player` hands mpv a `wid` — the native handle of the video frame's
child `QFrame` — and that handle belongs to the window. Under the old
process-per-window arrangement this never surfaced: closing a window ended the
*process*, so the OS reclaimed the handle and libmpv's threads died with it, in
an order the OS enforced. That path is not exercised any more.

Left alone, a player outlives the handle it is rendering into, and a window that
is closed without shutting it down has been reported as freezing the app to the
mouse — see invariant 4 and [experiments/README.md](../experiments/README.md).
`MpvBridge.shutdown()` detaches the observers and terminates the player, and it
runs from `closeEvent` on both the scanner and the editor, because `destroyed`
is already too late. The observers are detached first: they fire on mpv's worker
thread and emit Qt signals, so leaving them attached lets a callback land in a
half-destroyed QObject — which is observable as an mpv command failing on a
player that has already gone.

## The main menu

`MainWindow` loads `mainwindow.ui` through the shared `UiLoader` and
`setCentralWidget`, matching the Settings window's structure.

Two buttons: **Editor** and **Settings**.

**"Editor" opens the picker, not the editor.** The picker chooses which video
in `import/` to work on; the scanner is then the pre-process phase of the
Editing Wizard — it detects clip boundaries and hands off to the editor itself
— so picker, scanner, and editor are one journey, not three menu items. The
window says so in a hint label and a tooltip, since "Editor" alone does not.

Both buttons call `shell().open_safely(name)`, which is the only way a window is
opened anywhere in the app. The menu is the one window the shell reuses: it is
hidden while anything else is up and shown again when that window closes, so
there is only ever one menu and one taskbar entry. Closing the menu itself ends
the app, which the shell watches for through an event filter rather than
`destroyed` — the menu is never destroyed, so `close()` on it only hides it.

`setup_environment` resolves the project root as `install_root()`: the source
tree unfrozen, `dirname(sys.executable)` frozen. `tests/test_main_window.py`
covers the two buttons and the project-root resolution from every entry point;
`tests/test_session.py` covers the navigation.

## The source video is picked from the import folder

This is an alpha: the app deliberately does not let anyone point it at an
arbitrary path. The only videos that can be opened are the ones in the app's
own `import/` folder, and the user picks one in the **picker** window
(`picker/picker.py`), which the main menu's **Editor** button opens.

`shared/sources.py` owns the whole policy, and it is deliberately the only way
to turn an argument into a source path:

- `list_source_videos(folder=None)` — what the picker offers. Filters on
  `VIDEO_EXTENSIONS`, because `import/` ships a `README.txt` placeholder (empty
  folders do not survive a zip) and it must never appear as a selectable video.
  Sorted by name, case-insensitively, and reports `size_bytes` and
  `has_sidecar`.
- `resolve_import_video(value, folder)` — the *policy* check: the path must
  resolve inside `import/` (real paths, so a symlink in the folder cannot reach
  a file outside it, and `commonpath` so `import_backup` is not "inside"
  `import`) and must be a video extension. This is what actually enforces the
  restriction — the picker is only a convenience over it, and a hand-edited
  call still has to pass.
- `require_source_video(value, folder)` — the same check plus existence, and it
  owns the "nothing to open" message. Without it a missing video fails as a
  *codec* problem: ffprobe returns nothing, a placeholder `.cmct` is written
  with `duration=0.0`, and mpv then reports an opaque load failure.
- `DEFAULT_SOURCE_NAME` is the no-argument fallback (`import/test.mp4`) so a
  hand-edited call still has something to fall back on. The picker always
  supplies an explicit path.

**The path is an argument, not shared state.** It travels
`main menu → picker → scanner → editor` as a keyword the shell passes to a
window's builder: `shell().open('scanner', source=path)` reaches
`ScannerWindow(source_path)`, and `shell().open('editor', source=path)` reaches
`MediaPlayer(media_path)`. Navigation is the one process-wide global and it
lives in `shared/session.py`; the video is not in it.
`tests/test_source_handoff.py` guards the shapes that regressed quietly: a
window that resolves its own source instead of using the one it was given, and a
builder that would swallow the `source` keyword. The dependency direction is
one-way —
`shared/sources.py` imports `sidecar_path` from `shared/segments.py`; segments
never imports sources.

## The shared library

The editor, scanner, and Settings window use the common library under
`shared/`. Each entry point calls `shared.environment.setup_environment(__file__)`
near the top — it puts the install root on `sys.path` (so `shared.*` resolves
when running the script directly) and makes the per-OS `bin/<os>/` folder
discoverable. How each *binary* is then resolved is per-platform: bundled on
Windows, from the system on macOS and Linux (see
[source-install.md](source-install.md)).

The shared modules are:

- `shared/environment.py` — the two roots (`resource_root()` for bundled
  read-only data, `install_root()` for user data and `bin/<os>/`),
  `resource_path(*parts)`, `setup_environment(script_path)` (sys.path + making
  the bundled binaries discoverable), `is_frozen()`, `bin_dir()` /
  `get_binary_path(name)` (per-platform resolution: `bin/<os>/` first, then the
  system prefixes on platforms that do not bundle),   `resolve_mpv_library()` / `load_mpv_library()` / `mpv_import_context()` (resolve
  libmpv by absolute path, map it, and answer python-mpv's own lookup for it
  before `import mpv`), `video_output()` (per-OS mpv `vo`), and
  `ensure_app_folders()`. This is the cross-platform binary resolution that
  used to live in `core.py`. It used to also own `launch_command()` and
  `WINDOW_NAMES`; both went with the process model.
- `shared/session.py` — the `Shell`: the one `QApplication`'s single visible
  window, the `_BUILDERS` registry mapping a window name to its module and
  builder, `OpenInstead` for a builder that declines, and `shell()` /
  `set_shell()`. It is the only way a window is opened, and the only
  process-wide piece of state. Imports no window module and no mpv at module
  level, so the main menu does not pull in libmpv.
- `shared/mpv.py` — `MpvBridge` (the single Qt↔libmpv channel) and
  `MpvBridge.shutdown()` (detach the observers, then terminate the player,
  while the window that owns its handle is still alive), `create_mpv_player`
  (loads libmpv, then wraps `mpv.MPV` for a `QFrame` with `WA_NativeWindow`),
  `scan_keyframes(path)` (ffprobe I-frame scan returning sorted timestamps), and
  `BoundaryPreview` (the editor's boundary peek).
- `shared/diagnostics.py` — `log()`, `log_exception()`, `install_excepthook()`,
  and `fatal()`. Everything diagnostic, because a windowed build has no
  console.
- `shared/mpv.py` — `MpvBridge` (the single Qt↔libmpv channel),
  `create_mpv_player` (loads libmpv, then wraps `mpv.MPV` for a `QFrame` with
  `WA_NativeWindow`), `scan_keyframes(path)` (ffprobe I-frame scan
  returning sorted timestamps), and `BoundaryPreview` (the editor's boundary
  peek).
- `shared/timeline.py` — `TimelineWidget`, the editor's zoom/scroll segment
  timeline (red/green/blue, ignored dimming, active highlight). Also owns the
  zoom mode (`ZOOM_FIT` / `ZOOM_SEGMENT`) that survives a resize.
 - `shared/segments.py` — `SegmentModel` + `.cmct` persistence
   (`sidecar_path`, `probe_duration`). See [segment-model.md](segment-model.md).
 - `shared/sources.py` — the import/ policy, above.
  - `shared/ffmpeg.py` — ffmpeg helpers: `clip_to_temp` (the scanner's preview),
    the named-export executor (`export_named_model`, `execute_export_plan`,
    `ExportExecutionResult`, `ExportClipFailure`, `ExportCancelled`), and the
    legacy numeric `export_segment_clips()` that the editor no longer uses. It is
    the future home of the smart-cut export. The executor takes
    `on_progress`/`should_cancel` so a caller off the GUI thread can drive a
    progress bar and stop the batch; it holds no Qt types. See
    [naming-and-organization.md](naming-and-organization.md#export-pipeline).
  - `shared/scheme.py` — shared tag aliases and canonical-name resolution plus
   the public AST (`LiteralNode`, `TagNode`, `PipeNode`, `GroupNode`), lenient
   filename parsing, strict diagnostic parsing for path validation, and
   conditional node rendering. Existing filename syntax remains tolerant.
 - `shared/naming.py` — filename policy and `render_filename()`. It re-exports
   the shared tag helpers, keeps `{title}` as the filename requirement, and
   supports `{a,b}` fallback, `[...]` AND groups, `[{a}|{b}]` OR groups,
   nested groups, and `\`-escaping.
 - `shared/paths.py` — restricted folder-scheme compiler and resolver.
   `compile_folder_scheme()` validates the path-specific grammar;
   `render_folder_components()` sanitizes tag leaves and returns safe,
   display-cased relative components; `format_folder_components()` is for
   previews. Portable component sanitation also lives here for reuse by
   filename export.
 - `shared/exporting.py` — non-Qt settings snapshot and pure named-export
   planner. It validates the segment model and every destination, enforces the
   four required tags, compiles both schemes, materializes session tag locks,
   and rejects duplicate, existing, case-variant, reparse-point, traversal,
   and byte-limit conflicts before ffmpeg starts. It also owns
   **`export_folder()`**, the single place the export root is spelled out —
   `plan_export` still takes the root as an argument, and that function is the
   default for a caller that does not.
 - `shared/catalog.py` — the walk that reads the export library back:
   `build_catalog()` returns the clips it holds (a record with a sibling video),
   the records it could not read, and whether it was cancelled. A read-only
   scan, so a walk error is reported rather than raised — deliberately unlike
   the preflight's walk, which is about to write into the same tree.
   `sync_vocabulary()` uses it to reconcile `vocabulary.json` against the
   library. It holds no Qt types; the Settings window's `SyncWorker` is the
   thin wrapper that runs it off the GUI thread. See
   [naming-and-organization.md](naming-and-organization.md#reading-the-library-back-the-catalog)
   and [tag-vocabulary.md](tag-vocabulary.md#syncing-from-the-library).
 - `shared/ui_loader.py` — `UiLoader(QUiLoader)` subclass that instantiates
   promoted custom widgets reliably; register a class with
   `register_widget` before `load()`.

## The MpvBridge pattern

Both the editor and scanner communicate with libmpv through a single
**`MpvBridge(QObject)`** that owns the mpv player and exposes:

- **Qt signals** (state up): `pauseChanged`, `positionChanged`,
  `durationChanged`, `fileLoaded`, `playbackEnded`. These are fed by
  `player.observe_property(...)` callbacks. **Observers fire on mpv's worker
  thread; emitting Qt signals is thread-safe and lands on the GUI thread.**
- **Methods** (commands down): `toggle_play`, `seek_exact`, `step_frames`,
  `set_keyframes`, `next_keyframe`, `prev_keyframe`, `load_file`,
  `load_and_play`.
- **Read-only properties** (state up, on demand rather than by signal):
  `video_fps`, `position`, `paused`, `duration`. These exist so the editor
  does not have to reach into `player` for a one-off read; `BoundaryPreview`
  is the first user of `position`/`paused`/`duration`.

The editor/scanner windows and widgets never read mpv state directly — they
only mirror what the bridge announces via signals.

`shared/mpv.py` also owns **`BoundaryPreview(QObject)`**, the boundary peek
described in [segment-model.md](segment-model.md). It is bridge-driven rather than
mpv-driven, which keeps its timer and cancel logic testable without libmpv or
a video — `tests/test_boundary_preview.py` drives it through a `FakeBridge`
that records the seek sequence.

### Bridge conventions

- **Frame-accurate step.** mpv's `frame_step` plays a fraction of a second
  of audio (the audio decodes before the mute property takes effect).
  `MpvBridge.step_frames` instead seeks by `1/container_fps` and forces
  `pause = True`. This is silent.
- **Keyframe nav epsilon.** `_KEYFRAME_EPSILON = 0.05` seconds. Without
  it, mpv seeking to a keyframe near the current position can re-seek to
  the same spot and the buttons feel broken. Applied symmetrically in
  `next_keyframe` / `prev_keyframe`.

## Splash flow (pre-work before the window appears)

`__main__` in both the editor and the scanner shows a `QSplashScreen`, then
runs `scan_keyframes` (via ffprobe) while it's up. The scan is currently
synchronous on the GUI thread — it's typically sub-second for a 2-minute
preview. `PreScanWorker` is scaffolding in the editor for future off-thread
stages, not yet wired into `__main__`. The editor's other worker,
`ExportWorker`, is the pattern to follow: it holds plain data, emits
`planned`/`advanced`/`finished` signals, and wraps its body so an exception
becomes a reported outcome rather than PySide6's abort path. See
[naming-and-organization.md](naming-and-organization.md#the-export-runs-off-the-gui-thread).

**Hard-won gotcha:** mpv's Direct3D device initialization hangs when
another top-level window (the splash) is the active window at construction
time. Close the splash *before* constructing the mpv-backed widget.
`app.processEvents()` is called once after `splash.show()` to ensure the
splash actually paints.

## No console: diagnostics

The build is `console=False`, so there is no console. `shared/diagnostics.py`
owns reporting:

- `log()` appends to `commcut.log` next to the exe, replacing the bare
  `print()` calls that were the only channel and were invisible in a packaged
  build. Override the path with `COMMCUT_LOG`.
- `install_excepthook()` writes a traceback to the log and shows a
  `QMessageBox` naming it.
- `fatal()` handles failures *before* a window exists (an unwritable install
  root) and returns a real exit code. Once the menu is up, a window that will
  not build is a `QMessageBox` from `Shell.open_safely` instead.

The spec sets `disable_windowed_traceback=True` for the same reason: the
default makes the windowed bootloader pop a **modal** traceback dialog that the
process waits on, so an undismissable error looks exactly like a hang. See
[packaging.md](packaging.md).

## Qt conventions

- **Promoted widget.** The timeline is a custom `QWidget` promoted in
  `editorwindow.ui` as `TimelineWidget` / header `timeline`. PySide6's
  `QUiLoader` does not auto-resolve promoted widgets reliably, so
  `editor.py` uses a `UiLoader(QUiLoader)` subclass whose `createWidget`
  is overridden to instantiate `TimelineWidget` directly. Register the
  class with `loader.register_widget(TimelineWidget)` before `loader.load()`.
  The scanner does the same for `MarkerTimelineWidget` (header
  `scanner.marker_timeline`) in `scanner.py`.
