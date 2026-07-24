# edsdk-python

Python wrapper for Canon EOS Digital Software Development Kit, aka EDSDK.

Supported Python versions: 3.8 – 3.13 (CPython, Windows 64-bit). Python 3.13 での動作を確認済みです。

Currently, it supports Windows only. But it shouldn't be difficult to adapt it for macOS.

By default, the wheel built from this fork does not bundle Canon's proprietary DLLs.
Install Canon EDSDK separately and point the package to the DLL folder with either
`EDSDK_PYTHON_DLL_DIR` or `CANON_EDSDK_DLL_DIR`.

## Install from GitHub Releases (consumer projects)

Prebuilt Windows wheels (64-bit, Python 3.11 / 3.12 / 3.13) are attached to
[GitHub Releases](https://github.com/maedayoshiaki/edsdk-python/releases).
The wheels contain only this project's code — Canon's EDSDK is **not** included.

Add to your `pyproject.toml` (works with both pip and uv):

```toml
dependencies = [
  "edsdk-python @ https://github.com/maedayoshiaki/edsdk-python/releases/download/v0.1.9/edsdk_python-0.1.9-cp313-cp313-win_amd64.whl ; python_version == '3.13'",
  "edsdk-python @ https://github.com/maedayoshiaki/edsdk-python/releases/download/v0.1.9/edsdk_python-0.1.9-cp312-cp312-win_amd64.whl ; python_version == '3.12'",
  "edsdk-python @ https://github.com/maedayoshiaki/edsdk-python/releases/download/v0.1.9/edsdk_python-0.1.9-cp311-cp311-win_amd64.whl ; python_version == '3.11'",
]
```

Or install a single wheel directly:

```cmd
pip install https://github.com/maedayoshiaki/edsdk-python/releases/download/v0.1.9/edsdk_python-0.1.9-cp313-cp313-win_amd64.whl
```

You still need Canon EDSDK itself: apply for it through Canon's developer
programme for your region, then point this package at the DLLs before
importing:

```cmd
set EDSDK_PYTHON_DLL_DIR=C:\path\to\EDSDK_64\Dll
```

Vendoring a wheel file into your repository (the previous workflow) keeps
working — the release URLs simply serve the same wheel.

## Obtain the EDSDK from Canon

Before you can use this library you need to obtain the EDSDK library from Canon. You can do so via their developers program:

- [Canon Europe](https://www.canon-europe.com/business/imaging-solutions/sdk/)
- [Canon Americas](https://developercommunity.usa.canon.com)
- [Canon Asia](https://asia.canon/en/campaign/developerresources)
- [Canon Oceania](https://www.canon.com.au/support/support-news/support-news/digital-slr-camera-software-developers-kit)
- [Canon China](https://www.canon.com.cn/supports/sdk/index.html)
- [Canon Korea](https://www.canon-ci.co.kr/support/sdk/sdkMain)
- [Canon Japan](https://cweb.canon.jp/eos/info/api-package/)

Once you were granted access - this may take a few days - download the latest version of their library.

## Copy the EDSDK Headers and Libraries

You should now have access to a zip file containing the file you need to build this library.

Unzip the file and copy the following folders inside the `dependencies` folder of this project:

1. `EDSDK` - Containing headers and 32-bit libraries (we only use the headers!)
2. `EDSDK_64` - Containing 64-bit version of the .lib and .dlls

Your dependencies folder structure should now look like this:

```text
dependencies/EDSDK/Header/EDSDK.h
dependencies/EDSDK/Header/EDSDKErrors.h
dependencies/EDSDK/Header/EDSDKTypes.h

dependencies/EDSDK_64/Library/EDSDK.lib
```

For local source checkouts, keeping these files is still useful:

```text
dependencies/EDSDK_64/Dll/EDSDK.dll
dependencies/EDSDK_64/Dll/EdsImage.dll
```

The build only needs the import library under `dependencies/EDSDK_64/Library`.
The wheel produced by this fork does not copy Canon DLLs into `site-packages`.


## Modify EDSDKTypes.h

This file contains an enum definition, called `Unknown`, which collides with a DEFINE in the `Windows.h` header.

Therefore needs to be renamed.

```c
typedef enum
{
    Unknown   = 0x00000000,
    Jpeg      = 0x3801,
    CR2       = 0xB103,
    MP4       = 0xB982,
    CR3       = 0xB108,
    HEIF_CODE = 0xB10B,
} EdsObjectFormat;
```

You can comment out `Unknown` or rename it to `UNKNOWN` (or whatever you want) or it won't compile on Windows.

## Build the library

Run (this will compile the C++ extension against your current Python, e.g. 3.13):

```cmd
pip install .
```

If Canon DLLs are not on `PATH`, point the package to them before importing:

```cmd
set EDSDK_PYTHON_DLL_DIR=C:\Canon\EDSDK\Dll
```

To use example programs:
```cmd
pip install .[examples]
or
uv sync --extra examples
```

To generate a wheel (recommended for distribution / reuse):

```cmd
pip install build
python -m build --wheel
```

You will find the wheel under `dist/` (e.g. `edsdk_python-0.1.1-cp313-cp313-win_amd64.whl`). Install it with:

```cmd
pip install dist\edsdk_python-0.1.1-cp313-cp313-win_amd64.whl
```

## Build a non-bundled wheel for client delivery

This fork is intended for redistribution without Canon SDK binaries.

1. Obtain Canon EDSDK from Canon's official developer program.
2. Copy headers into `dependencies/EDSDK/Header`.
3. Copy the import library into `dependencies/EDSDK_64/Library/EDSDK.lib`.
4. Keep `EDSDK.dll` and `EdsImage.dll` outside the wheel payload.
5. Build the wheel with `python -m build --wheel` or `uv build --wheel`.
6. Deliver the wheel by itself.
7. On the client machine, install Canon EDSDK separately and set `EDSDK_PYTHON_DLL_DIR`
   or `CANON_EDSDK_DLL_DIR` to the DLL folder.

## Releasing (maintainers)

Releases are built locally on a machine that has the Canon SDK under
`dependencies/`, MSVC, `uv`, and an authenticated `gh` CLI. Canon SDK files
are never uploaded — only the wheels containing this project's code. Run the
script from PowerShell or cmd, not Git Bash/MSYS — MSVC discovery fails
under MSYS on this machine.

1. Verify the build on your branch:
   `.venv\Scripts\python.exe scripts\release.py --dry-run`
   (builds Python 3.11/3.12/3.13 wheels and runs the unit tests against each
   wheel in a throwaway venv).
2. Merge to `main`. The released version must match `pyproject.toml` on
   `origin/main` — the script checks this.
3. Create the release: `.venv\Scripts\python.exe scripts\release.py`
   (creates tag `v{version}`, uploads the wheels from `dist/`, and prints the
   consumer `pyproject.toml` snippet).

## Exposure control (Av / Tv / ISO)

`CameraController` exposes camera-aware exposure APIs. Values can be given
as numbers (f-number / seconds / ISO value), strings (`"1/125"`, `"f/5.6"`,
`"auto"`), or table entries obtained from `supported_*()`.

```python
from edsdk.camera_controller import CameraController

with CameraController() as cam:
    cam.supported_av()             # lens-dependent list, e.g. [f/3.5 ... f/22]
    cam.set_av(5.6)                # strict: ValueError if not supported (±1/4 EV absorbed)
    cam.set_tv(1 / 125)            # returns the value the camera reports back
    cam.set_iso(400)
    cam.set_av(2.8, nearest=True)  # snaps to nearest supported (e.g. f/3.5)

    # Legacy API keeps working and uses the same engine:
    cam.set_properties(av="5", tv="1/30", iso="500", nearest=True)
```

Unsupported values raise `ValueError` listing the supported range and the
nearest candidate. Numeric Av/Tv inputs within a quarter stop (0.25 EV) of a
supported value are treated as that value even in strict mode; ISO requires
an exact value. Pass `nearest=True` to snap automatically (matching is
done in EV / log2 space). `Bulb` and ISO `Auto` are never chosen by
`nearest`; request them explicitly (`"bulb"` / `"auto"`).

## Capture transfer safety

`capture()` waits for every file produced by one shutter release. JPEG-only
and RAW-only modes produce one path; RAW+JPEG modes complete after both paths
have arrived. `shots`, timeout, interval, and retry values are validated
before the shutter command is sent.

Retrying a transfer timeout means issuing another shutter command even though
the first image may only be delayed. This can create duplicate photos, so a
non-zero `retry` requires an explicit opt-in:

```python
paths = cam.capture(
    retry=1,
    retry_delay=0.5,
    retry_on_timeout=True,
)
```

Without `retry_on_timeout=True`, `retry > 0` raises `ValueError`. A partial
RAW+JPEG transfer is never retried automatically.

## Safe shutdown and protected camera sessions

`CameraController.close()` is idempotent and is also called by the context
manager and during normal interpreter shutdown. If opening a camera fails
halfway through, the controller still attempts to close the session and
terminate EDSDK.

On Windows, opt into a separate EDSDK worker when the application process may
be force-terminated:

```python
from edsdk.camera_controller import CameraController

with CameraController(protected=True, save_dir="out") as cam:
    cam.set_tv("1/125")
    paths = cam.capture()
```

The worker owns the EDSDK session. If only the parent application is terminated
with `taskkill /F`, `Popen.kill()`, or an equivalent `TerminateProcess` call,
the worker detects the broken control pipe and closes the camera session before
exiting. Do not use `taskkill /T /F`: killing the worker itself prevents any
software cleanup. A permanently blocked EDSDK call must return before the
worker can close the session.

Protected mode supports property/exposure operations, capture to paths or
bytes, NumPy/RAW processing, profiles, and live view. RAW processors and NumPy
conversion run in the parent process. Native `EdsObject` values cannot cross
the process boundary, so use serializable events instead of `on_object()` or
`on_property()`:

```python
with CameraController(protected=True) as cam:
    cam.on_event(lambda event: print(event))
    cam.capture()
```

`on_event()` receives dictionaries containing `kind`, `event`, and optional
`path`, `property`, and `param` fields. In protected mode the callback runs on
the parent-side IPC receiver thread; keep it short or forward work to another
thread/async queue. `enable_async()` receives the same events.

## Troubleshooting

If you see errors like:

- C2365: 'Unknown': redefinition; previous definition was 'enumerator'
    Follow [Modify EDSDKTypes.h](#modify-edsdktypesh).

- "OpenCV (cv2) not found" when running examples
    Install extras: `pip install edsdk-python[examples]` or `pip install -r requirements-examples.txt`.

- "imageio (imageio.v3) is required" when using `capture_numpy()`
    Install extras: `pip install edsdk-python[examples]` or install `imageio` directly.
