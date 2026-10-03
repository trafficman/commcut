# AGENTS.md

Orientation for AI coding agents working on the **commcut** project: what it is,
where things live, the rules that silently break a build, and an index into
`docs/`. The detail lives in `docs/` — start at
[docs/README.md](docs/README.md) and read the one document that owns the area you
are changing. Keep this file short; it is loaded into every session.

## What this project is

**commcut** is a Python/PySide6 desktop app for managing a personal library of
"filler" clips (commercials, promos, bumps) ripped from television. The full
vision, described in [README.md](README.md), has three pieces:

1. **Library organization** — a folder/naming scheme driven by per-clip tags
   (Title, Network, Block, Filler Type, Year, Time Period, Show, Special,
   Length, Information).
2. **Rename Wizard** — batch-rename already-cut clips. Not built.
3. **Editing Wizard** — load a compilation video, auto-detect boundaries, let
   the user review/adjust, and cut each segment out as a separate file. This is
   the current focus.

The tree holds the Editing Wizard (`editor/`), its Segment Scanner pre-process
(`scanner/`), the Settings window, and the `shared/` library they build on. The
main menu asks for a source video with a native file dialog, so a compilation can
live anywhere. [docs/status.md](docs/status.md) says exactly what exists today
and what is next — read it before building something that may already exist.

## Commands

Run from the project root.

| Task | Command |
|---|---|
| Run the app | `python main.py` |
| Tests | `python -m pytest` — one file: `python -m pytest tests/test_paths.py` |
| Build the portable app | `python packaging/build.py` — Windows only; see [packaging/README.md](packaging/README.md) |
| Cut a release | set `VERSION` in `shared/version.py`, then `git tag v<version> && git push origin v<version>` — CI builds it and attaches a draft release; see [docs/packaging.md](docs/packaging.md#releases) |
| Run on macOS or Linux | Same, from a clone, with ffmpeg/mpv installed — see [docs/source-install.md](docs/source-install.md) |

There is no pytest config: `tests/conftest.py` puts the project root on
`sys.path` and redirects the log out of the source tree. Widget tests set
`QT_QPA_PLATFORM=offscreen` themselves.

## Layout

```
commcut/
├── main.py                  # the entry point: the QApplication, the menu, the shell
├── mainwindow.py/.ui        # main menu (Editor, Settings)├── bin/<os>/                # bundled ffmpeg, ffprobe, libmpv (Windows only)
├── import/                  # finished clips to import; NOT source videos
├── export/                  # named clips are written here
├── temp/                    # scratch (the scanner's 2-minute preview)
├── commcut.log              # beside the exe; override with COMMCUT_LOG
├── packaging/               # PyInstaller spec + build script (docs/packaging.md)
├── .github/workflows/       # a pushed tag builds the Windows release zip
├── scanner/                 # Segment Scanner: detect boundaries, hand off
├── editor/                  # Editing Wizard: segments, tags, export
├── settings/                # file + folder scheme editors
├── shared/                  # the library all three windows build on
├── tests/                   # pytest suite (docs/testing.md)
├── docs/                    # the documents indexed below
├── experiments/             # code answering a question the docs could not;
│                            # never shipped, never bundled
└── prototypes/              # historical; the active code is editor/ and scanner/
```

`shared/` in one line each: `environment` (roots, binaries, `mpv_import_context`),
`session` (the `QApplication` and the one visible window), `version` (the release
number `packaging/build.py` checks a tag against), `diagnostics`
(log/excepthook/fatal), `mpv` (MpvBridge, `BoundaryPreview`, and
`MpvBridge.shutdown`), `timeline` (editor timeline), `segments` (`SegmentModel`,
`.cmct`), `sources` (which videos may be opened, and the `import/` path), `ffmpeg` (preview clip + named
export), `scheme`/`naming`/`paths` (the two schemes), `exporting` (the export
planner and `export_folder()`), `catalog` (reading the library back),
`mesh` (the Mesh Wizard's model), `tag_form` (the tag fields both windows
show), `ui_loader` (promoted widgets). Per-module detail:
[docs/architecture.md](docs/architecture.md).

## Documentation

| Document | Read it when you need to know... |
|---|---|
| [docs/README.md](docs/README.md) | the doc index and the conventions this set follows |
| [docs/architecture.md](docs/architecture.md) | the annotated layout, the one-process-per-window model, the main menu, the source hand-off, the shared library, the MpvBridge pattern, the splash flow, diagnostics |
| [docs/segment-model.md](docs/segment-model.md) | the `.cmct` format, `SegmentModel`, the editor state machine and its buttons, the boundary peek, tag locks, End Seg, required record fields |
| [docs/scanner.md](docs/scanner.md) | the scanner: preview clip, marker timelines, `blackdetect`, Test Scan / Finished, the hand-off to the editor |
| [docs/naming-and-organization.md](docs/naming-and-organization.md) | file naming scheme, folder organization scheme, the parser, sanitation and path safety, the export pipeline, the `.cnfo` clip record, the Settings scheme UI |
| [docs/importing.md](docs/importing.md) | the Library Importer's backend: reading somebody else's library, the occupied-destination rule, per-clip skipping, the transfer modes, and why a path can only ever propose a tag |
| [docs/tag-vocabulary.md](docs/tag-vocabulary.md) | the tag dropdowns: `vocabulary.json`, its shipped defaults, the dedup rule, the most-recently-used ordering, what counts as "used" |
| [docs/packaging.md](docs/packaging.md) | the portable Windows build, everything that only breaks when frozen, and the tag-driven release pipeline |
| [docs/source-install.md](docs/source-install.md) | running from source on macOS or Linux: where the binaries come from, the `COMMCUT_MPV_LIB` override, and what is unverified |
| [docs/testing.md](docs/testing.md) | how to run the suite, the widget/`FakeBridge` harness, which test file covers what, the Qt/import gotchas |
| [docs/status.md](docs/status.md) | what is built, what is next, known gaps |
| [docs/design_legacy.md](docs/design_legacy.md) | the original scope/design notes |
| [docs/guides/filler_and_you.md](docs/guides/filler_and_you.md) | user-facing guide to acquiring and organizing filler |

## Non-negotiable invariants

Each of these fails silently in a packaged build or in a way the user cannot
diagnose. The linked document has the full reasoning.

1. **Resources resolve through `resource_path()`.** Never `SCRIPT_DIR` or
   `__file__` — frozen they point into the payload, which is wiped on exit.
   → [docs/packaging.md](docs/packaging.md)
2. **Frozen has two roots.** `resource_root()` is the read-only payload;
   `install_root()` is beside the exe and holds `bin/<os>/`, `settings.json`,
   `import/`, `export/`, `temp/`, `commcut.log`. Nothing that must survive goes
   in the payload. Unfrozen they are the same folder, so a source install on
   macOS or Linux writes to the clone. → [docs/packaging.md](docs/packaging.md)
3. **The shell is the only way to open a window, the source path is a
   constructor argument, and exactly one window is visible at a time.**
   `shared/session.py:Shell.open(name, **kwargs)` builds a window through the
   registry in `_BUILDERS`, takes down whatever was on screen, and shows the
   new one. There is no `--window` flag, no per-window script, and no
   `run(*args)`. A window that will not open is reported by `Shell.open_safely`,
   never raised into the event loop. The main menu is the one window that is
   *hidden* rather than closed; every other window is **closed** when replaced,
   because closing is what runs its `closeEvent`. →
   [docs/architecture.md](docs/architecture.md)
4. **An mpv-backed window must shut its player down before it is destroyed.**
   `create_mpv_player` hands mpv a `wid` — the native handle of a child frame —
   so a player still alive when the window dies leaves libmpv attached to a
   window that no longer exists. `MpvBridge.shutdown()` detaches the observers
   and terminates the player, and it runs from `closeEvent` on both the scanner
   and the editor, because `destroyed` is already too late. Never hide a window
   that owns a player, and never let a `MediaPlayer` be collected without it. →
   [docs/architecture.md](docs/architecture.md)
5. **Binaries resolve by absolute path, per platform, and libmpv is loaded
   and named before `import mpv`.** `get_binary_path` searches `bin/<os>/` and
   then, only on platforms that do not bundle (`_BUNDLED_BINARY_PLATFORMS`), the
   absolute prefixes in `_SYSTEM_BIN_PREFIXES`. A bundling platform must
   **refuse** rather than fall through to a system copy. On Windows the `PATH`
   prepend and the `os.add_dll_directory` handle stay load-bearing, and the
   handle must stay alive (a dropped one surfaces later as a bare `OSError` from
   ctypes). On macOS `find_library` ignores `PATH` and **raises** rather than
   falling back to an already-loaded image, so `mpv_import_context` loads the
   resolved library and answers the lookup for it.
   → [docs/packaging.md](docs/packaging.md),
   [docs/source-install.md](docs/source-install.md)
6. **Close the splash before constructing any mpv-backed widget.** A splash is a
   top-level window, and constructing a player while another one is foreground
   was *reported* to deadlock mpv's renderers. It never reproduced — see
   [experiments/README.md](experiments/README.md) — but closing a splash is
   three lines and is the only thing standing between a driver update and a
   frozen window. Kept as a precaution, not as a proven requirement. →
   [docs/architecture.md](docs/architecture.md)
7. **The mpv foreground-construction hazard is unverified, and no longer the
   reason for the architecture.** One process per window existed because
   constructing a player while another top-level window is foreground was
   believed to deadlock on Windows with direct3d. There was never a recorded
   reproduction, and `experiments/mpv_foreground/` ran the claim directly: 120
   runs, six cases, no hang, foreground verified rather than assumed, including
   a frameless splash and three concurrent presenting players. That is one GPU,
   one driver, one mpv build, and bare windows rather than the real editor — it
   does not prove the hazard absent everywhere, so invariant 6 stands as a
   precaution. But the process boundary it justified is gone, and the windows
   share one event loop and one libmpv. →
   [experiments/README.md](experiments/README.md)
8. **The source video path is an argument, not shared state, and its folder must
   be writable.** It travels `main menu → scanner → editor` as a constructor
   argument; a window never re-resolves a default of its own — `scanner.create`
   and `editor.create` both *require* `source`, so a missing one fails loudly
   rather than opening something. Navigation is the one global, and it lives in
   the shell — the path is not. The writable half is new since sources can come
   from anywhere: the `.cmct` is written *beside* the video and rewritten on
   every Stage, so `shared/sources.py:validate_source_video` refuses a folder
   that cannot take a write, and `MediaPlayer._save_sidecar` is the backstop for
   one that stops mid-session. Never write beside a source video from anywhere
   else. → [docs/architecture.md](docs/architecture.md)
9. **One owner per rule.** Required tags live in
   `shared/exporting.py:missing_required_tags`; the filename and folder
   grammars live in `shared/naming.py` and `shared/paths.py`. Never add a second
   implementation of a rule that already has one. →
   [docs/naming-and-organization.md](docs/naming-and-organization.md)
10. **Editing writes are all-or-nothing.** Stage and Export refuse rather than
    persist an incomplete record, and ignored segments are exempt from the
    required tags. A run that did not finish — cancelled, or left with failed
    clips — keeps the clips it committed, and the only destinations it may later
    skip are the ones that same session's own run wrote. →
    [docs/segment-model.md](docs/segment-model.md)
11. **Every child process carries `**no_console_kwargs()`.** A `console=False`
    build has no console, so Windows gives each ffmpeg, ffprobe, and window
    launch a *visible* console window — and it is invisible from a source run,
    where the child joins the developer's terminal. The rule's one owner is
    `shared/environment.py:no_console_kwargs`; `tests/test_frozen_mode.py` sweeps
    the app's modules and fails without it. → [docs/packaging.md](docs/packaging.md)
12. **A clip's tags live in its record; the filename is a projection of them.**
    Every export writes a `<stem>.cnfo` beside the video, and rendering the name
    under the two schemes is a one-way function — sanitation, `{a,b}` fallbacks
    and `[{a}|{b}]` OR groups all make it lossy. Nothing parses a filename back
    into tags, ever; the Rename Wizard reads the record and rewrites the path.
    The record is published between a successful encode and the commit of the
    video, and it replaces rather than refuses, so `video present ⟹ record
    present` holds without any reconciliation. →
    [docs/naming-and-organization.md](docs/naming-and-organization.md#the-clip-record)
13. **The tag vocabulary is advisory; nothing validates a tag against it.**
    `shared/vocabulary.py` records values the user has used and the editor offers
    them back as dropdowns, but a value absent from the file is always accepted
    — the fields are editable combos, not closed lists. That is what lets the
    file be an imperfect cache instead of a catalog, and it is the reason the
    shipped defaults live in code: deleting `vocabulary.json` must stay a safe
    troubleshooting step. Turning the list into a validator is the one change
    that would make a tag untypeable. Only the nine suggestable tags go in the
    file at all — Title is unique per clip, so it has no dropdown and no lock. →
    [docs/tag-vocabulary.md](docs/tag-vocabulary.md)
14. **A worker thread must actually terminate, and its teardown hangs off
    `QThread.finished`.** `thread.started.connect(worker.run)` runs the slot inside
    the thread's `exec()` loop and a slot returning does not leave it, so only
    `worker.finished → thread.quit` ends a thread; teardown on the *worker's*
    signal destroys a live one — a `qFatal`, uncatchable and invisible in the log.
    → [docs/architecture.md](docs/architecture.md)

## Working agreements

- **Run the suite** (`python -m pytest`) before reporting a task done, and say
  what you ran.
- **A bug fix ships with a test** that drives the shipped code. For editor
  behavior that means the real `MediaPlayer` through
  `tests/editor_stub.py`, not a re-implementation —
  [docs/testing.md](docs/testing.md) has the harness and the gotchas.
- **Update the owning document in the same change** as any behavior it
  describes. If a doc and the code disagree, one of the two is a bug; find out
  which before editing either.
- **Do not add comments to code** unless asked. The code documents itself and
  the docs carry the rationale.
- **Do not commit** unless asked.

## Documentation conventions

- **Markdown links between documents; backticked repo-root paths for source
  files.** `tests/test_docs.py` resolves the links (so a moved or renamed
  document breaks a test) and deliberately does not resolve source paths, so
  moving a file does not force a documentation edit. `tests/test_*.py` is the
  one exception: it is a coverage claim, and it is checked.
- **Each document opens with a purpose line and an `Applies to:` list** of the
  source paths it describes, so you can tell whether it is the right one before
  reading it.
- **`AGENTS.md` stays under 240 lines.** The ceiling is enforced by
  `tests/test_docs.py`; new detail goes in `docs/`, not here.
