# Building commcut

This is the **Windows** build. macOS and Linux have no build and are not meant
to: they run from a clone with the system's ffmpeg and mpv, per
[docs/source-install.md](../docs/source-install.md).

Two artifacts, both from `packaging/commcut.spec`:

- **`commcut.exe`** — a PyInstaller *onefile* executable. Carries Python, PySide6,
  the app, and the five `.ui` files. It unpacks itself to a temp folder and runs.
  No Python required on the target machine.
- **`commcut-portable/`** — the folder to actually distribute. The exe plus
  everything that must live *beside* it.

## The distributable layout

```
commcut-portable/
├── commcut.exe        46 MB   self-extracting; unpacks Python + Qt and runs
├── bin/win/                  the bundled binaries, NOT inside the exe
│   ├── ffmpeg.exe     128 MB
│   ├── ffprobe.exe    128 MB
│   └── libmpv-2.dll   110 MB
├── import/                    drop a compilation video in here as test.mp4
│   └── README.txt
├── export/                    named clips are written here
│   └── README.txt
├── temp/                      created on first run; scratch preview clips
├── commcut.log                written on first run
└── settings.json              written when you save in Settings
```

~412 MB total, dominated by the three binaries.

Zip that folder and it works on any 64-bit Windows machine with no Python
installed. There is no installer, no elevation prompt, and nothing is written
outside the folder.

## Why the binaries are not inside the exe

A onefile build re-extracts its entire payload on *every* launch. With
`bin/win/` bundled that is ~366 MB of extraction each time commcut starts. Kept
outside, the exe carries only ~46 MB and `bin/win/` is found immediately by
`shared/environment.py:install_root()`, which resolves to
`dirname(sys.executable)` when frozen.

This used to be paid once per *window* — the app ran each of its windows as a
separate process, so one editing session extracted the payload four times. There
is one process now, so it is paid once. The layout is unchanged, and the reason
for it is if anything stronger; see [docs/packaging.md](../docs/packaging.md).

Measured on this machine, every window including the main menu reaches a
ready state in **~1.3 s**.

## Build it

```powershell
# once
python -m pip install -r requirements.txt
python -m pip install -r requirements-build.txt

# build
python packaging/build.py
```

Options:

| Flag | Effect |
|---|---|
| *(none)* | onefile `commcut.exe` — the shipping artifact |
| `--onedir` | folder form, `dist/commcut/` → assembled into `commcut-portable/`. Starts faster and is the one to debug against. |
| `--check-only` | run the pre-flight checks and stop |
| `--zip` | also write the distributable as a zip, with a `.sha256` sidecar |
| `--version <tag>` | name the archive for a release, e.g. `v0.1.0`. Refused unless it matches `shared/version.py` |

Requires Python **3.11** and Windows. `requirements.txt` is pinned exactly
because a PyInstaller output is not portable across PySide6 minor versions.

Verified with Python 3.11.1, PySide6 6.11.1, python-mpv 1.0.8,
PyInstaller 6.22.3, 7-Zip n/a (no longer used).

## The distributable zip

`--zip` writes `dist/commcut-<version>-windows-x64.zip` (or
`commcut-portable-windows-x64.zip` with no `--version`) beside a `.sha256`
sidecar. The archive holds a single `commcut-<version>/` folder, not a flat
tree, so extracting it does not scatter the app across your Downloads folder
and two releases extracted side by side do not share a `settings.json`.

It is refused rather than written if the portable folder is missing the exe,
any of the three `bin/win/` binaries, or either placeholder `README.txt`.
Nothing is left behind when it refuses.

## Cutting a release

```powershell
# 1. set VERSION in shared/version.py
# 2. commit, then tag that commit with v<that version>
git tag v0.1.0
git push origin v0.1.0
```

`.github/workflows/release.yml` builds and attaches the zip to a **draft**
release. Download it from the Releases page, extract, and run `commcut.exe` —
then publish the draft once you are happy with it.

The tag has to match `shared/version.py`, or the build refuses. That check is
deliberate: a mistagged release is otherwise indistinguishable from a good one
until somebody reads the page. The workflow re-runnable by hand
(*Actions → release → Run workflow → tag*) for a tag that already exists, and a
run that finds a release already there does nothing rather than failing.


## Pre-flight checks

`build.py` refuses to build on a few specific conditions, all of which produce
a build that installs cleanly and then fails at runtime:

- **Not Windows.** There is no macOS or Linux build. `bin/linux/` and `bin/mac/`
  hold no binaries, and those platforms resolve ffmpeg from the system instead —
  see [docs/source-install.md](../docs/source-install.md). A build made here
  would be an app that cannot cut a clip.
- **A bundled binary is a Git LFS pointer.** The repo tracks `bin/**/*.exe` and
  `*.dll` through Git LFS. A checkout without `git lfs pull` leaves ~130-byte
  ASCII pointer text in their place, which a file copy would ship without
  complaint. Each binary must be over 1 MB.

Post-build, the tree is checked for: the five `.ui` files in the right
subfolders, `prototypes/` and `tests/` absent, and (onedir only) no
`_internal/` directory.

## Using the built app

1. Unzip anywhere.
2. Put a video in `import/` and name it `test.mp4`. The filename is
   `DEFAULT_SOURCE_NAME` in `shared/segments.py` — that is the only name the
   smoke-test build looks for.
3. Run `commcut.exe`.

| Problem | Where to look |
|---|---|
| Nothing happens on launch | `commcut.log` in the same folder |
| "No source video found" | the message names the exact path to put the video at |
| ffmpeg/ffprobe/libmpv errors | `commcut.log`; the ffmpeg stderr is captured there |
| Settings won't save | the install folder is not writable — `QSaveFile` needs write access to the *directory*, not just the file |

Set `COMMCUT_LOG` to redirect the log somewhere else.

## Diagnostics

The build is `console=False`, so there is no console output. Instead:

- `shared/diagnostics.py:log()` appends to `commcut.log` next to the exe. It
  replaced the bare `print()` calls that used to be the only diagnostic
  channel and were invisible in a windowed build.
- `shared/diagnostics.py:install_excepthook()` writes a full traceback to the
  log and shows a `QMessageBox` naming it.
- `shared/diagnostics.py:fatal()` handles anything that fails *before* a
  window exists (an unwritable install folder) and returns a real exit code.
  Once the menu is up, a window that will not open is a `QMessageBox` from
  `shared/session.py:Shell.open_safely`.

`packaging/commcut.spec` sets `disable_windowed_traceback=True` on purpose.
The default makes the windowed bootloader pop its own **modal** traceback
dialog, which the process waits on — an error the user cannot dismiss looks
exactly like a hang.

## Layout requirements

Three things in the spec are load-bearing. Changing any of them breaks the app
only in a packaged build, not from source:

1. **The `.ui` files keep their source-tree subfolders** (`editor/`, `scanner/`,
   `settings/`), rather than being flattened into the payload root.
   `shared/environment.resource_path()` is called with one expression whether
   or not the app is frozen, so the payload has to mirror the source layout.
   `tests/test_frozen_mode.py` asserts the spec's `datas` list agrees with the
   code.
2. **`contents_directory="."` in onedir mode.** PyInstaller 6 would otherwise
   put the payload in `dist/commcut/_internal/` while leaving the exe at
   `dist/commcut/`, and every runtime path resolution would miss.
3. **The four window modules are in `hiddenimports`.** Nothing imports them in a
   shape PyInstaller's analysis can follow: `shared/session.py:Shell._resolve`
   loads each one with `importlib.import_module(module_name)` on a variable, and
   modulegraph discards every `Call` node. They were bundled by construction
   before the app became one process, because they were `Analysis()` entry
   points; deleting those is what made this necessary.
   Without them the build still succeeds and the exe still starts — the menu is
   a normal import and its `.ui` is in `datas` — but both of its buttons fail
   with `ModuleNotFoundError`, reported as *"The settings window could not
   start"*.
   Neither `build.py` nor CI can see that, because CI does not run the exe.
   `tests/test_frozen_mode.py::test_every_window_the_shell_can_open_is_bundled`
   parses the spec and requires every module in `_BUILDERS` to be listed.

`bin/win/` is *not* in the spec at all — `build.py` copies it in beside the
exe, which is what makes the layout above work.

## Not an installer

This is a portable smoke-test build, not a packaged release. There is no
uninstaller, no Start Menu shortcut, and no code signing. If it ever
needs to be a real installer, the options are Inno Setup (handles
`{localappdata}` natively, adds shortcuts and an uninstaller) or a 7-Zip SFX
built from **`7zSD.sfx`** — note that the plain `7z.sfx` in the standard 7-Zip
install ignores the whole config and prompts for a folder on every run.

The exe does carry an icon, from `assets/commcut_icon.ico`. The pre-flight says
so, and says the opposite just as clearly on a checkout that does not have the
artwork, because nothing at run time can tell you afterwards.

Not being signed means every release warns: Windows SmartScreen says "Windows
protected your PC" and the run is *More info → Run anyway*. That is expected,
not a broken download.

