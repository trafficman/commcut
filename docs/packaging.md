# Packaging and frozen mode

How the Windows build is assembled, and the rules that only bite once the app
is frozen: the two roots, the `.ui` payload layout, child windows as
re-executions, and per-platform library loading.

Applies to: `packaging/commcut.spec`, `packaging/build.py`,
`packaging/README.md`, `shared/environment.py`, `shared/version.py`,
`main.py`'s `--window` dispatch, `.github/workflows/release.yml`,
`tests/test_frozen_mode.py`, `tests/test_release_build.py`.

Related: [source-install.md](source-install.md) (how you run commcut on macOS or
Linux, which this build does not cover), [architecture.md](architecture.md) (the
same code unfrozen — one process per window, diagnostics), [testing.md](testing.md)
(how frozen behavior is tested without building an exe).

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

**A folder named after an installed library cannot be imported.** `packaging/`
has no `__init__.py`, and the `packaging` that PyInstaller depends on is a real
package in site-packages. A regular package beats a namespace portion
*wherever* it is on `sys.path`, so `import packaging.build` binds to the
dependency and then reports no `build` inside it — the mirror image of the
`scanner` case above, and permanent rather than order-dependent, because
PyInstaller means the installed one is always there. `packaging/build.py` is
therefore run as a script, and `tests/test_release_build.py` loads it with
`importlib.util.spec_from_file_location`. PyInstaller's own `import packaging`
is unaffected and gets the dependency, which is what it wants anyway.

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

## Library loading, per platform

python-mpv resolves libmpv **for itself at import time**, via
`ctypes.util.find_library`, and how that lookup works is not the same on every
platform. This is why `create_mpv_player` loads the library itself before
importing python-mpv, and why that import is deferred at all.

**On Windows**, `find_library` scans `%PATH%` for each candidate name and
returns the first absolute hit, which it then loads with
`LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR`. So prepending `bin/<os>` to `%PATH%` is
**load-bearing**. `os.add_dll_directory` is registered too, and its handle is
kept in a module global — a dropped handle unregisters the directory and
surfaces much later as a bare `OSError` from ctypes with nothing pointing at
the cause.

**On macOS, `find_library` ignores `%PATH%` entirely.** It searches a fixed
list of system directories and returns a path only if that exact file exists.
A source install's libmpv is not there, so the `PATH` prepend buys nothing.

**And mapping the library first is not enough.** This is the non-obvious part.
python-mpv 1.0.8's POSIX branch asks `ctypes.util.find_library('mpv')` and
**raises if that returns `None`** — it never checks whether libmpv is already
loaded. `DYLD_LIBRARY_PATH` does not help either, because `find_library` looks
for a *file* at a fixed list of paths rather than asking the dynamic loader. So a
libmpv commcut can find and map is still one python-mpv refuses to load, and the
error tells the user to read the `ctypes.util.find_library` documentation.

`environment.mpv_import_context` therefore does both: it resolves libmpv itself
(`resolve_mpv_library` — `COMMCUT_MPV_LIB` override, then `bin/<os>/`, then the
system prefixes), maps it with `ctypes.CDLL(..., RTLD_GLOBAL)`, and **answers
`find_library` for exactly the names python-mpv asks for** (`_MPV_LOOKUP_NAMES`),
leaving every other name to the real function and removing the override on the
way out. On Windows that is redundant with the `PATH` prepend underneath, and is
applied anyway so the library bound is the one this module validated.

`create_mpv_player` enters the context around the `import` — not
`setup_environment`, so the main-menu process never loads libmpv.

[source-install.md](source-install.md) owns the macOS and Linux end of this:
the search order, the `COMMCUT_MPV_LIB` override, and why `DYLD_LIBRARY_PATH` is
set but `LD_LIBRARY_PATH` is not.

`mpv`'s `vo` comes from `environment.video_output()` (per-OS), not a literal
`'direct3d'`. The `WA_NativeWindow` attribute on the video frame is required on
**every** platform, not just the one whose driver motivated it: `winId()` is what
produces the handle mpv embeds into, and without it `winId()` returns 0.

## Windowed-build diagnostics

The build is `console=False`, so there is no console; `shared/diagnostics.py`
is the only reporting channel and is documented in
[architecture.md](architecture.md#no-console-diagnostics).

The spec sets `disable_windowed_traceback=True` for the same reason: the
default makes the windowed bootloader pop a **modal** traceback dialog that the
process waits on, so an undismissable error looks exactly like a hang.

## No console windows

A windowed build has no console of its own, and on Windows that changes what a
child process gets. A **console** program started by a process that has no
console is handed a new, *visible* console window. ffmpeg and ffprobe are
console programs, so without a flag every one of their calls flashes a console
over the app — the scanner's preview clip, the editor's duration and keyframe
probes, `blackdetect` on Test Scan, the encoder check and every exported clip.

This is invisible from a source run: the developer is in a terminal, the child
joins that terminal's console, and no window ever appears. It only happens in
the packaged build, which is what makes it a packaging rule.

`no_console_kwargs()` in `shared/environment.py` is the single owner. It
returns `{"creationflags": subprocess.CREATE_NO_WINDOW}` on Windows and `{}`
elsewhere, and every `subprocess.run` / `Popen` in the app splats it in:

```python
subprocess.run(command, capture_output=True, text=True, **no_console_kwargs())
```

The flag gives the child a console with **no window**, so captured pipes and the
temporary stderr file in `_run_ffmpeg` keep working unchanged. It is a no-op on
a child that is not a console application, which is why the four window launches
(`launch_command` call sites) can carry it too without a second code path — a
GUI-subsystem `commcut.exe` re-executing itself never allocated a console in the
first place, so the pop-ups seen on a window transition were the *new* window's
own ffmpeg and ffprobe calls, not the launch.

The flag is unconditional rather than applied only when the current process
happens to have no console: every call site already captures its child's
output, so there is nothing to lose by never joining the parent console, and
"does this process have a console" is a ctypes question with a subtle answer
that call sites should not each have to get right.

`tests/test_frozen_mode.py` sweeps the app's modules with `ast` and fails if any
`subprocess.run` / `Popen` lacks the flag, because the failure mode is silent on
every machine a developer has. `core.py` is excluded from the sweep: nothing
imports it, PyInstaller never sees it, and it is kept only as history. Wiring it
back into a window would reintroduce the pop-ups without failing the sweep.

## Resolving the binaries at runtime

`get_binary_path(name)` returns the absolute path of a binary, and it is the
only way any call site gets one — a bare name resolves through a mutated
`PATH` and picks up whatever happens to be installed. The order is data
(`shared/environment.py`), not an if-chain:

1. `bin/<os>/<name><ext>`, always. On Windows this is where the bundled binary
   is, so it wins.
2. Only on a platform **not** in `_BUNDLED_BINARY_PLATFORMS`, the absolute
   prefixes in `_SYSTEM_BIN_PREFIXES` — a candidate has to be an executable file
   to count. macOS and Linux are *source* installs, so this step is their only
   step; see [source-install.md](source-install.md).
3. Otherwise `FileNotFoundError`, naming every location searched.

A bundling platform must **refuse** rather than fall through. A packaged build
that silently used a system ffmpeg would be running a binary nobody tested,
which is a far worse failure than a missing file — and it has a known one
bundled, so there is nothing to gain. macOS and Linux are the mirror image: the
user supplied the ffmpeg, so "whatever the user installed" is the intent.

The libmpv load is separate from this, and happens at `import mpv` rather than at
`get_binary_path` time. See "Library loading, per platform" above.

## What the build checks

Pre-flight, `build.py` refuses to build when the host is not Windows, or when a
bundled binary is a Git LFS pointer file rather than a real binary. The Windows
gate stays because there is no macOS or Linux build: those platforms ship as
source installs, and a frozen macOS build would put `install_root()` inside a
signed `.app` bundle, which is read-only.

Post-build, it asserts `_internal/` is absent, and that `prototypes/` and
`tests/` were not bundled. `docs/` is not bundled either — the spec's `datas`
list names only the `.ui` files, so documentation never reaches the payload. The
`.ui` layout itself cannot be checked on disk for a onefile build (it unpacks at
run time), so `tests/test_frozen_mode.py` asserts it instead.

`--zip` adds one more check, and it is the only one that looks at a *shipping*
artifact. `zip_portable` requires the exe, all three `bin/win/` binaries, and
both placeholder `README.txt` files to be present before it writes anything, and
refuses with the missing paths named. That is load-bearing for onefile, where
nothing else inspects the result: an archive missing its binaries installs
cleanly and then fails the first time it tries to cut a clip, with no useful
error. It holds for `--onedir` too, so one check covers both.

## Releases

`.github/workflows/release.yml` builds this artifact on a tag push and attaches
it to a **draft** GitHub release. Windows only; `check_platform()` is what stops
a build for anything else, and `build.py --version` is the only caller that
passes a version at all.

```
git tag v0.1.0 && git push origin v0.1.0
```

Draft, not published, because the exe has not run on a real machine yet and a
GitHub release cannot be re-cut for a tag that is already published. The assets
are downloadable from a draft, so smoke testing needs nothing changed. Workflow
inputs (`workflow_dispatch` with a `tag`) re-cut a release for a tag that
already exists, and a run that finds a release already there is a green no-op
rather than a failure — re-running a workflow is a normal thing to do.

**The tag is not trusted to be the version.** `shared/version.py` holds
`VERSION`, and `build.py --version` refuses anything that disagrees with it, so
a mistagged release fails before a 400 MB build happens instead of producing a
release page that contradicts the code inside it. A `v` prefix is stripped, so
`v0.1.0` and `0.1.0` are the same release; the constant stores the bare number
and the prefix is a tag convention only. The check is the reason the constant
exists in the codebase rather than living in the workflow.

**The archive is `commcut-<version>-windows-x64.zip`** plus a `.sha256`
sidecar, holding a single `commcut-<version>/` folder rather than a flat tree —
so extracting does not scatter the app across the folder the user extracted
into, and two releases extracted side by side do not merge each other's
`settings.json` and `import/`. Without `--version` it is
`commcut-portable-windows-x64.zip`, which is the local smoke-test form and does
not require editing the source. `tests/test_release_build.py` covers the layout,
the required entries, the sidecar, and the version rule.

Deflate is doing real work here: the ~412 MB folder lands at roughly 170 MB,
just under the 2 GiB per-asset limit, and zipping it takes about twenty
seconds. The bytes are not reproducible — PyInstaller embeds a build timestamp
— but the *entry order* is, so two archives of one tree diff on their contents
rather than on their ordering.

Two things in the workflow are load-bearing. **`lfs: true`**: `bin/win/` is Git
LFS, ~384 MB, and without it the checkout has pointer text where the binaries
go — caught loudly by `check_binaries` rather than shipped. **`ref:`**: on a
manual re-run `github.ref` is the branch the workflow ran from, so without
`ref: ${{ inputs.tag || github.ref }}` the re-cut would build a different commit
than the tag it is releasing. The suite runs first and gates the build; it needs
none of the LFS binaries, since ffmpeg is stubbed and the mpv import is
deferred and mocked.

What CI does **not** do is run the built exe. It cannot: `build.py`'s layout
check and the workflow's sha256 comparison say the archive is complete and
internally consistent, not that the player renders. That is a person on a
Windows machine, which is the same reason `tests/real_ffmpeg_check.py` is run by
hand. The runner is `windows-2022` and pinned, deliberately: `windows-latest`
moves, and a different Visual Studio image means a different bundled
`vcruntime140.dll` in the exe.

The exe is **unsigned**, so SmartScreen shows "Windows protected your PC" and
the user has to choose *More info → Run anyway*. Every release until signing is
added will do this.

