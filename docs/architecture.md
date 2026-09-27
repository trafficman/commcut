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
│   │                        # splash flow, PreScanWorker scaffolding
│   └── editorwindow.ui      # Qt Designer file; promoted TimelineWidget
├── scanner/                 # The Segment Scanner (detector + review)
│   ├── scanner.py           # Entry point: 2-min preview clip load, mpv playback,
│   │                        # transport, two marker timelines, detector sliders
│   ├── marker_timeline.py   # MarkerTimelineWidget (playhead + vertical marker lines)
│   └── scannerwindow.ui     # Qt Designer file; promoted MarkerTimelineWidget
├── shared/                  # Cross-module library (editor + scanner + settings)
│   ├── environment.py       # frozen-aware roots, per-OS binaries, launch_command
│   ├── diagnostics.py       # log file, excepthook, fatal() startup reporting
│   ├── mpv.py               # MpvBridge, create_mpv_player, scan_keyframes
│   ├── timeline.py          # TimelineWidget (segments, zoom/scroll)
│   ├── segments.py          # SegmentModel + .cmct persistence, probe_duration
│   ├── sources.py           # import/ policy: what can be opened, what is offered
│   ├── ffmpeg.py            # clip_to_temp, export_named_model, execute_export_plan
│   ├── scheme.py            # Shared tag aliases, AST, parser, strict parser, renderer
│   ├── naming.py            # Filename scheme policy + render_filename()
│   ├── paths.py             # Folder scheme validation, sanitization, safe components
│   ├── exporting.py         # Settings snapshot, destination planner, export preflight
│   └── ui_loader.py         # UiLoader subclass for promoted custom widgets
├── tests/                   # pytest suite (see testing.md)
└── prototypes/              # Earlier exploration / alternatives
    ├── BasicUI/             # First prototype
    └── VideoEditor/         # Pre-rename copy of the editor module
```

## One process per window

Every window is a separate process, and that is deliberate: constructing an mpv
player (direct3d) while another top-level window is foreground deadlocks on
Windows. See [packaging.md](packaging.md) for how that shapes the frozen build
(`--window <name>` re-execution, argv forwarding).

`main.py` is the application entry point. It calls
`shared.environment.setup_environment(__file__)`, constructs `MainWindow` from
`mainwindow.py`, and runs the event loop. It is also the argv dispatcher: the same
binary, re-executed as `--window <name> [args]`, routes to that window's `run()`
(`_run_window`, importing each window lazily so the main menu does not pull in mpv).

## The main menu

`MainWindow` loads `mainwindow.ui` through the shared `UiLoader` and
`setCentralWidget`, matching the Settings window's structure.

Two buttons: **Editor** and **Settings**.

**"Editor" launches the picker, not the editor.** The picker chooses which video
in `import/` to work on; the scanner is then the pre-process phase of the
Editing Wizard — it detects clip boundaries and hands off to the editor itself
— so picker, scanner, and editor are one journey, not three menu items. The
window says so in a hint label and a tooltip, since "Editor" alone does not.

Children are launched with `shared.environment.launch_command(name, *args)`, the
same mechanism the picker uses to hand the chosen video to the scanner and the
scanner uses to hand it to the editor. Three
reasons: the menu **stays open in the background** (it never waits on or
observes the child, so there is no need to reopen it on child exit), each
window gets its own Qt event loop and its own mpv instance, and it sidesteps
the Windows mpv D3D hazard where constructing a player while another
top-level window is foreground can deadlock. A launch that fails — an unknown
window name, or a missing script when running from source — is reported with a
`QMessageBox` rather than allowed to escape into the event loop.

`setup_environment` resolves the project root as `install_root()`: the source
tree unfrozen, `dirname(sys.executable)` frozen. `tests/test_main_window.py`
covers the launcher, the failure paths, and the project-root resolution from
every entry point.

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
  `--window scanner <path>` still has to pass.
- `require_source_video(value, folder)` — the same check plus existence, and it
  owns the "nothing to open" message. Without it a missing video fails as a
  *codec* problem: ffprobe returns nothing, a placeholder `.cmct` is written
  with `duration=0.0`, and mpv then reports an opaque load failure.
- `DEFAULT_SOURCE_NAME` is the no-argument fallback (`import/test.mp4`) so a
  direct `python scanner/scanner.py` and any build predating the picker still
  work. The picker always supplies an explicit path.

**The path is an argument, not shared state.** It travels
`main menu → picker → scanner → editor`, because each window is its own
process: `launch_command(name, *args)` appends it, `main.py`'s
`--window <name> [args]` dispatcher forwards it, and each `run(source=None)`
passes it to its window constructor (`ScannerWindow(source_path)`,
`MediaPlayer(media_path)`). `tests/test_source_handoff.py` guards the shapes
that regressed quietly: a window that resolves its own source instead of using
the one it was given. The dependency direction is one-way —
`shared/sources.py` imports `sidecar_path` from `shared/segments.py`; segments
never imports sources.

## The shared library

The editor, scanner, and Settings window use the common library under
`shared/`. Each entry point calls `shared.environment.setup_environment(__file__)`
near the top — it puts the install root on `sys.path` (so `shared.*` resolves
when running the script directly) and makes the per-OS `bin/<os>/` folder
discoverable, which is how mpv finds `libmpv` and how the ffmpeg/ffprobe call
sites resolve to the bundled versions.

The shared modules are:

- `shared/environment.py` — the two roots (`resource_root()` for bundled
  read-only data, `install_root()` for user data and `bin/<os>/`),
  `resource_path(*parts)`, `setup_environment(script_path)` (sys.path + making
  the bundled binaries discoverable), `is_frozen()`, `bin_dir()` /
  `get_binary_path(name)` (per-OS resolution under `bin/<os>/`, `.exe` on
  Windows), `launch_command(name, *args)` (argv to open a child window),
  `WINDOW_NAMES`, `video_output()` (per-OS mpv `vo`), and
  `ensure_app_folders()`. This is the cross-platform binary resolution that
  used to live in `core.py`.
- `shared/diagnostics.py` — `log()`, `log_exception()`, `install_excepthook()`,
  and `fatal()`. Everything diagnostic, because a windowed build has no
  console.
- `shared/mpv.py` — `MpvBridge` (the single Qt↔libmpv channel),
  `create_mpv_player` (wraps `mpv.MPV` for a `QFrame`, sets
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
   `ExportExecutionResult`, `ExportClipFailure`), and the legacy numeric
   `export_segment_clips()` that the editor no longer uses. It is the future home
   of the smart-cut export. See
   [naming-and-organization.md](naming-and-organization.md).
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
   and byte-limit conflicts before ffmpeg starts.
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
stages, not yet wired into `__main__`.

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
- `fatal()` handles failures *before* a window exists (missing source video,
  unwritable install root, bad `--window` argument) and returns a real exit
  code. `main()` wraps dispatch in it so nothing reaches the bootloader.

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
