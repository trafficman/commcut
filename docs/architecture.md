# Architecture

How the app is put together: the repository layout, the one-process-per-window
model, how a source video travels between windows, the shared library, the
mpv bridge, and the splash flow.

Applies to: `main.py`, `mainwindow.py`, `mainwindow.ui`, `shared/environment.py`,
`shared/diagnostics.py`, `shared/mpv.py`, `shared/splash.py`,
`shared/timeline.py`, `shared/ui_loader.py`, `shared/sources.py`.

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
├── bin/                     # Bundled binaries; Windows only. bin/mac and
│   └── win/                 # bin/linux are resolved against but never exist:
│                            # macOS and Linux resolve from the system instead.
├── assets/                  # commcut_banner.png, drawn on the loading splash
├── import/                  # Finished clips to import; not source videos
├── export/                  # Named clips are written here
├── temp/                    # Scratch output (e.g. 2-min scanner preview clips)
├── commcut.log              # Written beside the exe (override with COMMCUT_LOG)
├── install_deps.sh          # Source-release installer: a .venv in the install
├── run.sh                   #   root, then a report on ffmpeg/libmpv/libx264
├── commcut.command          # The launcher pair, and the same for Finder
├── main.py                  # Application entry point: the main menu, and the
│                            # one QApplication every window shares
├── mainwindow.py            # MainWindow: asks for a source video with a file
│                            # dialog, or opens the settings window
├── mainwindow.ui            # Qt Designer file for the main menu
├── packaging/               # The two release builds (see packaging.md)
│   ├── commcut.spec         # PyInstaller onefile (default) and onedir modes
│   ├── build.py             # Windows: pre-flight checks + portable assembly
│   ├── source_release.py    # macOS/Linux: the source-release tarball
│   └── README.md            # Windows build instructions and shipped layout
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
│   ├── sources.py           # what may be opened, and the import/ folder
│   ├── tag_form.py          # the ten tag fields, shared by the editor and the queue
│   ├── tagform.ui           # their layout, promoted into both windows
│   ├── ffmpeg.py            # clip_to_temp, export_named_model, execute_export_plan
│   ├── scheme.py            # Shared tag aliases, AST, parser, strict parser, renderer
│   ├── naming.py            # Filename scheme policy + render_filename()
│   ├── paths.py             # Folder scheme validation, sanitization, safe components
│   ├── exporting.py         # Settings snapshot, destination planner, export preflight
│   ├── catalog.py           # build_catalog (read the library back), sync_vocabulary
│   ├── importing.py         # plan_import / execute_import, find_videos, match_value
│   ├── mesh.py              # MeshSession: the folder-name model, and the alias table
│   ├── values.py            # ValueSession: the tag-value model, and the value table
│   ├── splash.py            # show_splash: the banner on the loading screen
│   └── ui_loader.py         # UiLoader subclass for promoted custom widgets
├── importer/                # The Library Importer's windows
│   ├── mesh.py              # The Untagged Library Mesh
│   ├── queue.py             # The Library Mesh Tag Editor
│   ├── values.py            # The Tagged Library Mesh
│   ├── importrun.py         # The progress dialog and summary both endings share
│   ├── rules.py             # The Manage Autofill Rules dialog
├── tests/                   # pytest suite (see testing.md)
├── experiments/             # Never shipped; code answering what docs could not
│   └── mpv_foreground/      # The mpv-embedding test the process model rests on
├── prototypes/              # Earlier exploration / alternatives
│   ├── BasicUI/             # First prototype
│   └── VideoEditor/         # Pre-rename copy of the editor module
└── core.py                  # Dead: nothing imports it. Kept as history.
```

Three of those are in neither release. `tests/` and `prototypes/` are excluded
from the Windows payload by `build.py:_assert_no_strays`, and both are excluded
from the source release by its allow-list manifest; `experiments/` ships only
its `README.md`, because `AGENTS.md` and three documents link into it. → 
[source-install.md](source-install.md#what-is-in-it-and-what-is-not)

## One process, one event loop, one visible window

Every window in this app is a window, not a process. `main.py` is the only entry
point: it builds the one `QApplication`, constructs `MainWindow`, installs a
`Shell` over it, and runs the event loop. There is no `--window` flag and no
per-window script, and each window module exposes a `create(...)` builder rather
than a `run()` entry point.

`shared/session.py` shows **exactly one** of {menu, scanner, editor, settings, mesh, queue} at a time. Opening a window builds it, takes down whatever was on screen, and
shows the new one. When a non-menu window goes away the menu comes back; when the
menu goes away the app quits. Modal dialogs are not part of this — the main menu's
file dialog and the editor's export progress and summary dialogs are `QDialog`s
owned by the window that opened them, so they travel with it.

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
- **A window that refuses to close is not replaced.** `closeEvent` ignores the
  close while the window still owns a worker thread, and `close()` returns `False`
  when that happens. That return value used to be discarded: the shell set
  `_current` to the incoming window and presented it over a window that was still
  on screen, so two live windows each held a thread that could not be stopped.
  `_stand_down` now hands the refusal back, `open` raises `WindowBusy`, and
  `open_safely` reports it as its own outcome — the incoming window was built
  fine, so the "could not be opened" wording would blame the wrong one.

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

## Ending a worker thread

`create_mpv_player` is not the only thing a window owns that must not outlive it.
Five windows run a `QThread`: the editor's export, the Settings window's
vocabulary sync, the Untagged Library Mesh's load, the Tagged Library Mesh's load,
and the tag editor's duration probe. They share one shape, and two details of it are
load-bearing.

**A thread has to be told to stop.** `thread.started.connect(worker.run)` runs the
worker's slot inside the thread's `exec()` loop, and a slot returning does not
leave that loop. Nothing ends the thread except the worker emitting `finished` and
`thread.quit` being connected to it — so every worker emits `finished` on *every*
exit, including the cancelled and failed paths, and `_start_probe`/`_start_worker`
connect it to `thread.quit`. Without that connection the thread spins forever,
`thread.finished` never fires, and every teardown hung off it silently never
runs.

**Teardown hangs off `thread.finished`, never off the worker's own signal.** A
worker's `finished` is emitted from *inside* the still-running thread, so deleting
the `QThread` on it destroys a live thread. Qt answers that with
`QThread: Destroyed while thread is still running` — a `qFatal`, so it aborts the
process, is invisible in `commcut.log` (a release Qt build on Windows writes it
through `OutputDebugString`, not stderr), and cannot be caught by
`install_excepthook`. This is invariant 14.

The consequence that makes it worth stating: a window's `closeEvent` guard is
usually "refuse to close while `self._thread` is set", because a `QThread` still
running when its owner is destroyed aborts the process. That guard only comes off
if the thread really stopped. So a thread that never ends makes the window
permanently un-closable, and the shell's rule above stops it from being replaced
— the window could not be closed at all, and advancing from it to the tag editor
left two live windows up before the abort.

Tests cover this with real `QThread`s and the real event loop, in
`tests/test_mesh_window.py`, `tests/test_values_window.py` and `tests/test_queue.py`.
See [testing.md](testing.md).

## The main menu

`MainWindow` loads `mainwindow.ui` through the shared `UiLoader` and
`setCentralWidget`, matching the Settings window's structure.

Three buttons: **Editor**, **Import** and **Settings**.

**"Editor" opens a file dialog, not the editor.** The dialog chooses which video
to work on, from anywhere on disk; the scanner is then the pre-process phase of
the Editing Wizard — it detects clip boundaries and hands off to the editor itself
— so the dialog, scanner, and editor are one journey, not three menu items. The
window says so in a hint label and a tooltip, since "Editor" alone does not.

**The menu is also a drop target.** A video dragged onto it and released opens the
scanner exactly as the button does, because both entry points call one method,
`MainWindow.open_source` — the dialog is an input, not the rule. The window calls
`setAcceptDrops(True)` and implements `dragEnterEvent` and `dropEvent`; nothing
inside it needs `acceptDrops`, because Qt hands a drag to the widget under the
cursor and, if it will not take it, up to its parent.

Three decisions in there are the whole of it:

- **The drag-enter answers "is this a file?", not "is this a video?"** — the real
  check needs a filesystem probe of the folder for the `.cmct`, and the drag-enter
  runs on every drag over the window. It is also deliberately not the extension,
  because a drag refused there produces no drop and therefore no message: a
  mistyped container would be answered by nothing happening. Accepting it there and
  refusing it in `open_source` names the file and lists what is supported.
- **A drop may carry many files and the wizard works on one source**, so
  `dropped_source` takes the **first video** and ignores the rest, logging how
  many videos came in. Dragging a folder's worth of rips out of a file manager is
  the case that makes this necessary.
- **A drop with no video in it still yields its first file**, which
  `validate_source_video` then refuses by name — the same refusal the dialog gives
  for the same file. Non-local URLs (a link dragged out of a browser) are not paths
  this app can open and are dropped rather than refused.

The drop needs no modal dialog, which is what makes it the quicker of the two, and
it is confined to the menu — the only window on screen when nobody else is up.

**"Import" opens the Untagged Library Mesh** over `import/`, which asks what each
folder name in the untagged half of it means. That window is also the **router** for
the whole importer: its worker already walks `import/`, so it is the one place that
knows which half the folder is, and a folder where every clip already has a record is
handed to the Tagged Library Mesh rather than asked about folder names that are
somebody install's *rendered* output. See
[importing.md](importing.md#the-flow).

The file dialog is modal, which puts it outside the shell: only the **Settings**
and **Import** buttons call `shell().open_safely(name)` directly, while **Editor**
and a drop both call `open_source`, which calls `open_safely('scanner',
source=...)` once the video is known. `shell().open_safely(name)`
is still the only way a window is opened anywhere in the app. The menu is the one
window the shell reuses: it is
hidden while anything else is up and shown again when that window closes, so
there is only ever one menu and one taskbar entry. Closing the menu itself ends
the app, which the shell watches for through an event filter rather than
`destroyed` — the menu is never destroyed, so `close()` on it only hides it.

`setup_environment` resolves the project root as `install_root()`: the source
tree unfrozen, `dirname(sys.executable)` frozen. `tests/test_main_window.py`
covers the file dialog, both ways into the wizard (button and drop), the three
buttons, and the project-root resolution from every entry point;
`tests/test_session.py` covers the navigation.

## The source video is picked from anywhere

A source video can be any video file on any writable path, and there are two ways
to name one. The main menu's **Editor** button asks with a native `QFileDialog`,
and a video dropped on the menu arrives the same way: both hand a path to
`MainWindow.open_source`, which validates it and opens the scanner. There is no
folder a source has to be in, which is what freed `import/` to be the Library
Importer's staging folder instead of a source-video drop.

Two things about that dialog:

- It is an **instance**, not `QFileDialog.getOpenFileName`, so a test can inspect
  and stand in for the one thing a native dialog cannot do: be answered. It is
  left **native**, because the OS dialog is better than Qt's and is what remembers
  the folder the user was last in — which is why nothing here persists a start
  directory. Adding one would mean a new global state store for a courtesy the
  platform already provides.
- It is a **modal dialog owned by the menu**, not a shell-managed window. The
  shell tracks the one visible *window*; the editor's own modal dialogs are on
  the same footing, and `shared/session.py` says so.

Its name filter is generated from `VIDEO_EXTENSIONS` rather than typed, so the
filter and `is_video_file` cannot drift — a container added to one and missed in
the other is invisible in the dialog and refused after the user picks it, which
is the worse of the two failures.

`shared/sources.py` owns what may be opened, and `validate_source_video` is the
one supported way to turn a selection into a source path. It checks four things,
each refused with a message written for a person: the value is not empty; it
exists and is a file rather than a folder; it has a video extension; and **its
folder is writable**.

That last one is the rule that only became reachable once sources could come from
anywhere. `shared/segments.py:sidecar_path` puts the `.cmct` beside the video,
and the editor rewrites it on every Stage and again before export, so the folder
has to take a write for the whole session — not just at scan time. Nothing checked
it while every source sat in `import/`, which is writable by construction.

The check is a **real probe**: create `.commcut-write-test-<pid>` in the folder,
remove it in a `finally`. `os.access(folder, os.W_OK)` is not an acceptable
substitute — it is advisory, on POSIX it succeeds for root whatever the mode bits
say, and on Windows it is a coarse ACL guess that a full disk or a read-only
share will pass.

It is a pre-flight and not a guarantee, because a folder can stop taking writes
mid-session and a network share can go away. `MediaPlayer._save_sidecar` is the
backstop: every save goes through it and a failure becomes a `ValueError` naming
the file and the folder, rather than a bare `PermissionError` arriving on Stage
with the user's tags unsaved. The editor reports it and deliberately leaves
`dirty` set, so the tags are not mistaken for staged.

**The path is an argument, not shared state.** It travels
`main menu → scanner → editor` as a keyword the shell passes to a window's
builder: `shell().open('scanner', source=path)` reaches `ScannerWindow(source_path)`,
and `shell().open('editor', source=path)` reaches `MediaPlayer(media_path)`.
Both builders **require** `source` — the menu always asks, so there is no
fallback path left to keep, and a missing argument has to fail loudly rather than
landing in a default. Navigation is the one process-wide global and it lives in
`shared/session.py`; the video is not in it.
`tests/test_source_handoff.py` guards the shapes that regressed quietly: a window
that resolves its own source instead of using the one it was given, and a builder
that would swallow the `source` keyword.

The dependency direction is one-way: `shared/sources.py` no longer imports
anything from `shared/segments.py` — it asks the filesystem, not the sidecar
helper — and segments never imports sources.

### What the picker was carrying

The bespoke picker listed everything in one folder and labelled the rips that had
already been scanned *"(already scanned — opens in the editor)"*. The **behaviour**
survived — `scanner._editor_to_launch` raises `OpenInstead` and the shell routes
straight to the editor, so an existing `.cmct` is still never overwritten — but
the *discoverability* did not. A native dialog cannot annotate a file, so resuming
a scanned rip means remembering where it is and picking it again. A recent-sources
list is the obvious answer and needs a persistence decision (`QSettings`, or a new
JSON file beside `settings.json`) that has deliberately not been made.

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
 - `shared/sources.py` — which videos may be opened, above, plus `import_folder()`
  for the Library Importer.
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
   the records it could not read (each with a `reason` code, so a screen can group
   them), and whether it was cancelled. A read-only scan, so a walk error is
   reported rather than raised — deliberately unlike the preflight's walk, which is
   about to write into the same tree. `sync_vocabulary()` uses it to reconcile
   `vocabulary.json` against the library. It holds no Qt types; the Settings
   window's `SyncWorker` is the thin wrapper that runs it off the GUI thread. See
   [naming-and-organization.md](naming-and-organization.md#reading-the-library-back-the-catalog)
   and [tag-vocabulary.md](tag-vocabulary.md#syncing-from-the-library).
 - `shared/importing.py` — the Library Importer's backend. `plan_import()` resolves
   finished clips to destinations through the *same* `plan_clip_destination` and
   `DestinationIndex` the export planner uses, so a foreign clip cannot land
   somewhere an exported one would not; `execute_import()` copies, links or moves
   each one and publishes its record. Also `find_videos()` for untagged discovery
   and `match_value()`, which ranks evidence without choosing. See
   [importing.md](importing.md).
  - `shared/mesh.py` — the Untagged Library Mesh's model, with no Qt: which folder
    name is being asked about, what the two questions are, which path to show, and the
    alias table that comes out. A folder name and an autofill rule are the same
    kind of thing — a literal, and what a person decided it means — so they share
    one table and one collision rule, and a literal already in it is refused
    rather than duplicated. A rule may not target `title`. `importer/mesh.py` and
    `importer/queue.py` render this and decide nothing. See
    [importing.md](importing.md).
  - `shared/values.py` — the Tagged Library Mesh's model, also with no Qt, and
    deliberately **not** a mode of `MeshSession`. A value is keyed by
    `(namespace, value)` and is renamed within its namespace or removed; a folder
    name and a rule, by contrast, share one table because a literal means one thing
    whatever its source. The `accumulate` collision rule is the wrong rule here —
    two different values becoming one is the normal case — so the two tables are
    kept apart deliberately. `importer/values.py` renders it and decides nothing.
    See [importing.md](importing.md#the-tagged-library-mesh).
 - `shared/tag_form.py` — the ten tag fields, one widget, promoted into both the
   editor and the queue. Two forms would be two dropdown configurations, and each
   of those settings decides what a typed value becomes; see
   [tag-vocabulary.md](tag-vocabulary.md).
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

`__main__` in both the editor and the scanner calls `shared/splash.py:show_splash`,
which puts a `QSplashScreen` up with the project's banner and the message
underneath it, then runs `scan_keyframes` (via ffprobe) while it's up. The scan is
currently synchronous on the GUI thread — it's typically sub-second for a 2-minute
preview. `PreScanWorker` is scaffolding in the editor for future off-thread
stages, not yet wired into `__main__`. The editor's other worker,
`ExportWorker`, is the pattern to follow: it holds plain data, emits
`planned`/`advanced`/`finished` signals, and wraps its body so an exception
becomes a reported outcome rather than PySide6's abort path. See
[naming-and-organization.md](naming-and-organization.md#the-export-runs-off-the-gui-thread).

Both windows used to build this by hand and identically — a filled pixmap, a
`QSplashScreen`, `showMessage`, `processEvents`, `close()` — which is one rule with
two copies of it, and a banner on one screen and not the other is exactly what two
copies produce. `show_splash` is the one copy. It returns the splash rather than
being a context manager **on purpose**: the caller closes it immediately before
constructing its mpv-backed widget, which is the next paragraph, and a `with`
block would end at the end of the scan instead.

Three decisions inside it are worth stating:

- **The banner is `assets/commcut_banner.png`, resolved through
  `resource_path()` and bundled by `UI_DATAS` in the spec.** It is a read-only
  payload resource like a `.ui` file, with the same consequence when omitted: fine
  from source, and a splash with no logo in a packaged build. `build.py`'s
  post-build `_verify_payload` checks it landed.
- **The splash's background is white because the banner's is.** The PNG has an
  opaque white background and dark artwork, so the old dark splash framed the logo
  in a white rectangle with a black wordmark on near-black. Filling with the
  banner's own colour makes its edges disappear into the screen, and the caption
  below is dark for the same reason.
- **A banner that cannot be read is logged and dropped, not raised.** Decoration on
  a progress screen must never become a dependency: the worst response to a missing
  PNG would be a window that will not open.

The banner is scaled to fit the space *above* the message band with its aspect
ratio kept and centred in what is left — never stretched to fill, and never allowed
to grow down into the caption. `MESSAGE_BAND` is the room reserved at the bottom
for the text, and it is subtracted from the box the logo is scaled into, so the two
cannot overlap. `tests/test_splash.py` asserts the painted pixels in both
directions of the aspect ratio rather than the calls.

**Hard-won gotcha:** mpv's Direct3D device initialization hangs when
another top-level window (the splash) is the active window at construction
time. Close the splash *before* constructing the mpv-backed widget.
`show_splash` pumps `app.processEvents()` once before returning, which is what
makes the splash actually paint — without it the user sees the previous window for
the length of the ffprobe run, which is the thing it exists to hide.

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
