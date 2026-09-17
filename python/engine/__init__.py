"""
Polígono AI Hub separation engine.

Public surface:
    SeparationEngine     persistent model cache + separate()/separate_file()
    SeparationCancelled  raised when a cancel request is honoured
    load_audio / probe / write_stem   float32 I/O helpers (soundfile + ffmpeg)
    PRESETS / MODEL_CONFIGS / MODES / FORMATS   static configuration tables
"""

from .audio_io import (
    AudioInfo,
    AudioIOError,
    LoadedAudio,
    WriteResult,
    find_ffmpeg,
    load_audio,
    probe,
    write_stem,
)
from .presets import (
    DEFAULT_FORMAT,
    DEFAULT_MODE,
    DEFAULT_PRESET,
    FORMATS,
    MODEL_CONFIGS,
    MODES,
    PRESETS,
    resolve_bit_depth,
)
from .separator import (
    JobResult,
    SeparationCancelled,
    SeparationEngine,
    SeparationStats,
    build_outputs,
    describe_device,
    mix_stems,
    resolve_device,
)

__all__ = [
    "AudioInfo",
    "AudioIOError",
    "LoadedAudio",
    "WriteResult",
    "find_ffmpeg",
    "load_audio",
    "probe",
    "write_stem",
    "DEFAULT_FORMAT",
    "DEFAULT_MODE",
    "DEFAULT_PRESET",
    "FORMATS",
    "MODEL_CONFIGS",
    "MODES",
    "PRESETS",
    "resolve_bit_depth",
    "JobResult",
    "SeparationCancelled",
    "SeparationEngine",
    "SeparationStats",
    "build_outputs",
    "describe_device",
    "mix_stems",
    "resolve_device",
]
