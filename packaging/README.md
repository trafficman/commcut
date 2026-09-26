# Building commcut

Two artifacts, both from `packaging/commcut.spec`:

- **`commcut.exe`** — a PyInstaller *onefile* executable. Carries Python, PySide6,
  the app, and the four `.ui` files. It unpacks itself to a temp folder and runs.
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

The app opens every window as a **separate process** — the main menu stays
open while the scanner runs, and the scanner launches the editor (this is what
avoids the Windows mpv Direct3D deadlock; see AGENTS.md).

A onefile build re-extracts its entire payload on *every* launch, so that means
once per window. With `bin/win/` bundled that is ~366 MB of extraction each
time. Kept outside, the exe carries only ~46 MB and `bin/win/` is found
immediately by `shared/environment.py:install_root()`, which resolves to
`dirname(sys.executable)` when frozen.

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

Requires Python **3.11** and Windows. `requirements.txt` is pinned exactly
because a PyInstaller output is not portable across PySide6 minor versions.

Verified with Python 3.11.1, PySide6 6.11.1, python-mpv 1.0.8,
PyInstaller 6.22.3, 7-Zip n/a (no longer used).

## Pre-flight checks

`build.py` refuses to build on a few specific conditions, all of which produce
a build that installs cleanly and then fails at runtime:

- **Not Windows.** `bin/linux/` and `bin/mac/` hold no binaries, so the result
  would be an app that cannot cut a clip.
- **A bundled binary is a Git LFS pointer.** The repo tracks `bin/**/*.exe` and
  `*.dll` through Git LFS. A checkout without `git lfs pull` leaves ~130-byte
  ASCII pointer text in their place, which a file copy would ship without
  complaint. Each binary must be over 1 MB.

Post-build, the tree is checked for: the four `.ui` files in the right
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
  window exists (missing video, unwritable install folder, bad `--window`
  argument) and returns a real exit code.

`packaging/commcut.spec` sets `disable_windowed_traceback=True` on purpose.
The default makes the windowed bootloader pop its own **modal** traceback
dialog, which the process waits on — an error the user cannot dismiss looks
exactly like a hang.

## Layout requirements

Two things in the spec are load-bearing. Changing either breaks the app only
in a packaged build, not from source:

1. **The `.ui` files keep their source-tree subfolders** (`editor/`, `scanner/`,
   `settings/`), rather than being flattened into the payload root.
   `shared/environment.resource_path()` is called with one expression whether
   or not the app is frozen, so the payload has to mirror the source layout.
   `tests/test_frozen_mode.py` asserts the spec's `datas` list agrees with the
   code.
2. **`contents_directory="."` in onedir mode.** PyInstaller 6 would otherwise
   put the payload in `dist/commcut/_internal/` while leaving the exe at
   `dist/commcut/`, and every runtime path resolution would miss.

`bin/win/` is *not* in the spec at all — `build.py` copies it in beside the
exe, which is what makes the layout above work.

## Not an installer

This is a portable smoke-test build, not a packaged release. There is no
uninstaller, no Start Menu shortcut, no code signing, and no icon. If it ever
needs to be a real installer, the options are Inno Setup (handles
`{localappdata}` natively, adds shortcuts and an uninstaller) or a 7-Zip SFX
built from **`7zSD.sfx`** — note that the plain `7z.sfx` in the standard 7-Zip
install ignores the whole config and prompts for a folder on every run.
