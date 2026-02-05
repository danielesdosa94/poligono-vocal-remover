# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for motor.py
Packages Demucs and all dependencies into a standalone executable
"""

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules
import sys
import os

block_cipher = None

# Collect all data files and submodules for complex packages
datas = []
binaries = []
hiddenimports = []

# =============================================================================
# Demucs Package
# =============================================================================
# Collect all Demucs modules and data files (yaml configs, pretrained models info)
demucs_datas, demucs_binaries, demucs_hiddenimports = collect_all('demucs')
datas += demucs_datas
binaries += demucs_binaries
hiddenimports += demucs_hiddenimports

# Explicit Demucs submodules (CRITICAL)
hiddenimports += [
    'demucs',
    'demucs.apply',
    'demucs.separate',
    'demucs.pretrained',
    'demucs.hdemucs',
    'demucs.htdemucs',
    'demucs.model',
    'demucs.states',
    'demucs.repo',
    'demucs.audio',
    'demucs.utils',
]

# =============================================================================
# PyTorch & TorchAudio
# =============================================================================
# Collect PyTorch binaries and submodules
torch_datas, torch_binaries, torch_hiddenimports = collect_all('torch')
binaries += torch_binaries
hiddenimports += torch_hiddenimports

# TorchAudio modules (CRITICAL)
torchaudio_datas, torchaudio_binaries, torchaudio_hiddenimports = collect_all('torchaudio')
datas += torchaudio_datas
binaries += torchaudio_binaries
hiddenimports += torchaudio_hiddenimports

hiddenimports += [
    'torchaudio',
    'torchaudio.lib',
    'torchaudio.lib._torchaudio',
    'torchaudio.pipelines',
    'torchaudio.transforms',
    'torchaudio.functional',
    'torchaudio.backend',
    'torchaudio.backend.soundfile_backend',
]

# =============================================================================
# Scikit-learn (sklearn)
# =============================================================================
sklearn_datas, sklearn_binaries, sklearn_hiddenimports = collect_all('sklearn')
datas += sklearn_datas
binaries += sklearn_binaries
hiddenimports += sklearn_hiddenimports

# Explicit sklearn Cython modules (CRITICAL)
hiddenimports += [
    'sklearn.utils._cython_blas',
    'sklearn.neighbors.typedefs',
    'sklearn.neighbors.quad_tree',
    'sklearn.neighbors._partition_nodes',
    'sklearn.tree',
    'sklearn.tree._utils',
    'sklearn.tree._tree',
    'sklearn.tree._splitter',
    'sklearn.tree._criterion',
]

# =============================================================================
# SciPy
# =============================================================================
scipy_datas, scipy_binaries, scipy_hiddenimports = collect_all('scipy')
datas += scipy_datas
binaries += scipy_binaries
hiddenimports += scipy_hiddenimports

# Explicit scipy Cython modules (CRITICAL)
hiddenimports += [
    'scipy.special.cython_special',
    'scipy.spatial.transform._rotation_groups',
    'scipy._lib.messagestream',
    'scipy.sparse._sparsetools',
    'scipy.sparse.csgraph._tools',
    'scipy.sparse.csgraph._shortest_path',
    'scipy.sparse.csgraph._traversal',
    'scipy.sparse.csgraph._min_spanning_tree',
    'scipy.sparse.csgraph._flow',
]

# =============================================================================
# NumPy
# =============================================================================
numpy_datas, numpy_binaries, numpy_hiddenimports = collect_all('numpy')
datas += numpy_datas
binaries += numpy_binaries
hiddenimports += numpy_hiddenimports

# =============================================================================
# Additional Dependencies
# =============================================================================
# Collect other common dependencies
for package in ['julius', 'openunmix', 'einops', 'diffq', 'soundfile']:
    try:
        pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(package)
        datas += pkg_datas
        binaries += pkg_binaries
        hiddenimports += pkg_hiddenimports
    except Exception:
        # Package might not be installed, skip
        pass

# Additional standard library modules that might be missed
hiddenimports += [
    'json',
    'argparse',
    'subprocess',
    'sys',
    'os',
    'pathlib',
    'tempfile',
    'shutil',
    'hashlib',
]

# =============================================================================
# Analysis
# =============================================================================
a = Analysis(
    ['motor.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude unnecessary packages to reduce size
        'matplotlib',
        'IPython',
        'notebook',
        'jupyter',
        'PIL',
        'tkinter',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# =============================================================================
# PYZ Archive
# =============================================================================
pyz = PYZ(
    a.pure,
    a.zipped_data,
    cipher=block_cipher
)

# =============================================================================
# EXE Configuration
# =============================================================================
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='motor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # Keep console visible for debugging
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

# =============================================================================
# COLLECT (Bundle all files together)
# =============================================================================
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='motor',
)
