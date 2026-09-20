# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the motor daemon.

Build from the REPO ROOT, not from python/:

    pyinstaller python\\motor.spec --workpath build\\pyinstaller

PyInstaller resolves --distpath and --workpath against the current working
directory, and package.json expects the result at <root>\\dist\\motor. The
explicit --workpath keeps PyInstaller's scratch files out of <root>\\build,
which electron-builder reads as its buildResources directory.

Collection policy
-----------------
Almost nothing is collected by hand. pyinstaller-hooks-contrib ships hooks
for torch, torchaudio and soundfile that already do it properly - hook-torch
in particular excludes **/*.lib, and torch/lib holds 2.7 GB of static import
libraries that would otherwise be dragged in as "data". A collect_all() here
would override that judgement with a worse one.

What has no hook is demucs, and it needs its package data: engine/models.py
reads demucs/remote/files.txt and demucs/remote/<model>.yaml to resolve a
model name into checkpoint URLs. Without those two the frozen exe cannot
work out which weights to fetch.

The runtime import set was measured, not guessed, by importing engine plus
demucs.api in the venv and reading sys.modules:

    antlr4 cloudpickle colorama demucs dora einops julius lameenc numpy
    omegaconf openunmix retrying soundfile submitit torch torchaudio torio
    tqdm treetable typing_extensions yaml

torchaudio is on that list and must stay: demucs/api.py:26 does
`import torchaudio as ta` at module level, so excluding it breaks the exe
even though our own I/O never touches it. scipy, sklearn, PIL and matplotlib
are NOT on that list and are excluded.

Console
-------
console=True with hide_console='hide-early'. The whole protocol lives on
stdin/stdout, and PyInstaller's windowed (console=False) builds are the
classic reason a frozen child process goes silent: without a console
subsystem the standard streams can end up as null writers. 'hide-early'
keeps real streams and still never shows a window; Electron also spawns with
windowsHide: true.

To get a visible console for debugging, set MOTOR_DEBUG_CONSOLE=1 before
building:

    $env:MOTOR_DEBUG_CONSOLE = "1"; pyinstaller python\\motor.spec
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# =============================================================================
# Build-time options
# =============================================================================

DEBUG_CONSOLE = os.environ.get("MOTOR_DEBUG_CONSOLE", "").lower() not in ("", "0", "false", "no")

datas = []
binaries = []
hiddenimports = []

# =============================================================================
# Demucs (no hook exists for it)
# =============================================================================

# remote/files.txt and remote/*.yaml: the model registry. Required.
datas += collect_data_files("demucs")

# Model classes are reached through the checkpoint pickles rather than by a
# visible import, so the module graph cannot find them on its own.
hiddenimports += collect_submodules("demucs")

# =============================================================================
# numpy legacy pickle shims
# =============================================================================

# Demucs' .th checkpoints were pickled when numpy still called its core
# `numpy.core`; numpy 2.0 renamed it to `numpy._core` and left `numpy/core/`
# behind as a shim whose own source says the entries "must import without
# warning or error from numpy.core.multiarray to support old pickle files".
#
# Verified against the installed checkpoints: 5 of the 9 reference
# `numpy.core.multiarray`, and torch.load() imports it while unpickling. So
# the failure is not at startup but at the first separation, with
#
#     ModuleNotFoundError: No module named 'numpy.core.multiarray'
#
# PyInstaller's bundled hook-numpy.py only adds `numpy._core._dtype_ctypes`
# and `numpy._core._multiarray_tests` for numpy >= 2.0, and nothing in our
# code imports the shim by name, so the module graph never sees it: the build
# collected only `numpy.core` and `numpy.core._utils`.
#
# The whole shim package goes in rather than just `multiarray`. It is 19 tiny
# pure-Python modules, and naming one would leave the same trap set for the
# next legacy pickle (another model, a future checkpoint). No runtime hook is
# needed to alias numpy.core -> numpy._core: numpy's own shim does that
# correctly once it is actually present.
hiddenimports += collect_submodules("numpy.core")

# =============================================================================
# Analysis
# =============================================================================

a = Analysis(
    ["motor.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Superseded by our own float32 I/O; torchaudio >= 2.9 would delegate
        # save/load to it, which is exactly what this project does not want.
        "torchcodec",
        # Not imported by anything on the runtime list, and not installed in
        # the venv either. Listed so a transitive dependency cannot sneak
        # them back in and add hundreds of MB.
        "scipy",
        "sklearn",
        "matplotlib",
        "PIL",
        "IPython",
        "notebook",
        "jupyter",
        "tkinter",
        "pytest",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# =============================================================================
# EXE / COLLECT
# =============================================================================

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="motor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX corrupts CUDA DLLs. Never turn this on.
    upx=False,
    console=True,
    hide_console=None if DEBUG_CONSOLE else "hide-early",
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="motor",
)
