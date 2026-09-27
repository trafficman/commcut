# Packaging and frozen mode

How the portable build is assembled, and the rules that only bite once the app
is frozen: the two roots, the `.ui` payload layout, child windows as
re-executions, and Windows DLL loading.

Applies to: `packaging/commcut.spec`, `packaging/build.py`,
`packaging/README.md`, `shared/environment.py`, `main.py`'s `--window`
dispatch, `tests/test_frozen_mode.py`.

Related: [architecture.md](architecture.md) (the same code unfrozen — one
process per window, diagnostics), [testing.md](testing.md) (how frozen behavior
is tested without building an exe).

## The distributable

`packaging/build.py` produces `dist/commcut-portable/`:

```
commcut.exe   46 MB  self-extracting (Python + PySide6 + app + the .ui files)
bin/win/            ffmpeg.exe, ffprobe.exe, libmpv-2.dll -- NOT inside the exe
import/             drop compilation videos in here; the picker lists them
export/             named clips are written here
```

~412 MB total. It is a **portable smoke-test build**, not a release: no
installer, no shortcuts, no uninstaller, no signing. See
`packaging/README.md` for build instructions and the smoke-test checklist.

`python packaging/build.py` is the whole build; `--onedir` produces the faster
folder form, and `--check-only` runs just the pre-flight checks.

**`bin/win/` is deliberately not bundled into the exe.** The app opens every
window as a separate process, and a onefile build re-extracts its whole
payload per launch, so bundling ~366 MB of binaries would mean re-extracting a
third of a gigabyte every time a window opened. Kept beside the exe, the
payload is only ~46 MB and every window reaches ready in **~1.3 s**.
`build.py` copies `bin/win/` in beside the exe, which is what makes the layout
below work.

Every runtime path resolution in the app therefore has to work from two
different places — the source tree and the frozen install folder. The next four
sections are how that is arranged; each of them is load-bearing and none of them
fail from source.

## The two roots

Unfrozen there is one root and the distinction is academic. Frozen there are
two, and conflating them is the most common way this app breaks in a build:

| | Resolves to (frozen) | Holds |
|---|---|---|
| `resource_root()` | `sys._MEIPASS` | the five `.ui` files |
| `install_root()` | `dirname(sys.executable)` | `bin/<os>/`, `settings.json`, `import/`, `export/`, `temp/`, `commcut.log` |

The payload directory is **wiped on exit**, so it is never the place for
anything that has to survive. `install_root()` is what `settings.json` and
every export resolve against.

`_bin_dir()` used to derive from `__file__`, which frozen points into the
payload — it now branches on `sys.frozen` and resolves against
`install_root()` instead.

## `contents_directory="."` (onedir only)

PyInstaller 6 onedir splits its output: the exe lands in `dist/commcut/` but
data goes to `dist/commcut/_internal/`. Since `bin/<os>/` is resolved against
`dirname(sys.executable)`, the default layout would make the app look for
ffmpeg next to the exe and not find it. The spec sets `contents_directory="."`
so the payload sits beside the exe. Onefile has no contents directory and
ignores it. `packaging/build.py` asserts `_internal/` is absent.

## `.ui` files keep their source subfolders

`resource_path()` takes a project-root-relative path and is called with the
same expression whether or not the app is frozen — `resource_path("settings",
"settingswindow.ui")`. Flattening the `.ui` files into the payload root
would make that correct only in a packaged build and wrong from source, so the
spec mirrors the source layout instead: `mainwindow.ui` at the payload root and
`editor/`, `scanner/`, `settings/`, `picker/` beside it.
`tests/test_frozen_mode.py::test_source_and_payload_layouts_agree` reads the
spec's `datas` list and compares it against the code's view, so a `.ui` file
that moves cannot be silently mis-bundled.

**Do not use `SCRIPT_DIR` for a resource.** It is
`dirname(os.path.abspath(__file__))`, which is only meaningful unfrozen; frozen
it points into the payload. Use `resource_path()`.

## Child windows are re-executions of the same binary

Every window is a separate process, and that is deliberate: constructing an
mpv player (direct3d) while another top-level window is foreground deadlocks
on Windows. From source each window is its own `.py` script; frozen the scripts
do not exist on disk, so `shared/environment.launch_command(name, *args)`
returns `[sys.executable, "--window", name, *args]` and `main.py` dispatches it.
`main.py` is therefore the only entry point in the spec, and all five windows
expose a `run()` function that both paths share — so the packaged build cannot
drift from the source build.

Any `*args` are forwarded verbatim into that window's `run(*args)`, which is
how the source video reaches the scanner and the editor. Dropping that
forwarding is silent: the windows would fall back to their default source and
open a different video than the one that was scanned. A window's own
`if __name__ == "__main__":` block must therefore pass `sys.argv[1:]` through —
`sys.exit(run(*sys.argv[1:]))`, which is exactly what `main.py` does with the
text after `--window <name>`. `sys.exit(run())` throws the argument away, and
because the fallback is `import/test.mp4` — which usually has a `.cmct` beside
it — the window silently hands off to the editor on a *different* video rather
than failing.

`launch_command` is the only place that knows how to open a window. Do not
hand-assemble argv elsewhere.

**A window folder that shadows its own package.** `scanner/` has no
`__init__.py`, so `scanner` is only a *namespace* portion, and CPython ranks a
regular module found **anywhere** on `sys.path` above a namespace portion
collected elsewhere. Running `python scanner/scanner.py` puts `scanner/` at
`sys.path[0]`, where `scanner.py` sits — so `scanner` resolves to that file and
`from scanner.marker_timeline import ...` fails with *"'scanner' is not a
package"*. That is precisely the command `launch_command` builds from source,
which is how the picker starts the scanner, and it does not reproduce when the
same module is imported as `scanner.scanner`. `scanner/scanner.py` therefore
branches on `__package__` to import its sibling by whichever name is actually
reachable. Adding a sibling import to any other window needs the same guard, and
the test has to run in a **fresh interpreter**: once `scanner` is in
`sys.modules` as the package, an in-process reproduction succeeds against broken
code.

## Windows DLL loading

`ctypes.util.find_library` — which python-mpv calls at *import* time to find
libmpv — scans `%PATH%` for each candidate name and returns the first absolute
hit, which it then loads with `LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR`. So
prepending `bin/<os>` to `%PATH%` is **load-bearing**, and it has to happen
before `import mpv`. python-mpv's import is deferred inside
`create_mpv_player` precisely so that ordering can be guaranteed.
`os.add_dll_directory` is registered too, and its handle is kept in a module
global — a dropped handle unregisters the directory and surfaces much later as a
bare `OSError` from ctypes with nothing pointing at the cause.

`mpv`'s `vo` comes from `environment.video_output()` (per-OS), not a literal
`'direct3d'`. The `WA_NativeWindow` attribute on the video frame exists for
that Windows driver, so changing it there is a break, not a portability tweak.

## Windowed-build diagnostics

The build is `console=False`, so there is no console; `shared/diagnostics.py`
is the only reporting channel and is documented in
[architecture.md](architecture.md#no-console-diagnostics).

The spec sets `disable_windowed_traceback=True` for the same reason: the
default makes the windowed bootloader pop a **modal** traceback dialog that the
process waits on, so an undismissable error looks exactly like a hang.

## Resolving the bundled binaries at runtime

Both the editor and the scanner call
`shared.environment.setup_environment` at startup, which prepends the
per-OS `bin/<os>/` folder to `PATH` via `get_binary_path`. mpv, ffprobe,
and ffmpeg resolve to the bundled versions. `get_binary_path` raises
`FileNotFoundError` if a binary is missing at the expected path. Every call
site uses the resolved absolute path rather than a bare binary name.

The ordering is load-bearing, not incidental: see "Windows DLL loading" above.

## What the build checks

Pre-flight, `build.py` refuses to build when the host is not Windows (no
`bin/linux`/`bin/mac` binaries exist) or when a bundled binary is a Git LFS
pointer file rather than a real binary.

Post-build, it asserts `_internal/` is absent, and that `prototypes/` and
`tests/` were not bundled. `docs/` is not bundled either — the spec's `datas`
list names only the `.ui` files, so documentation never reaches the payload. The
`.ui` layout itself cannot be checked on disk for a onefile build (it unpacks at
run time), so `tests/test_frozen_mode.py` asserts it instead.
