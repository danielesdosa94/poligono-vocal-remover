"""
Polígono AI Hub separation engine.

Public surface:
    SeparationEngine     persistent model cache + separate()/separate_file()
    SeparationCancelled  raised when a cancel request is honoured
    load_audio / probe / write_stem   float32 I/O helpers (soundfile + ffmpeg)
    plan_chunks / CrossfadeAssembler  long-file blocks with a linear crossfade
    checkpoints_for / ensure_checkpoints   model weights, with download progress
    PRESETS / MODEL_CONFIGS / MODES / FORMATS   static configuration tables
"""

from .audio_io import (
    AudioInfo,
    AudioIOError,
    LoadedAudio,
    StemStreamWriter,
    WriteResult,
    find_ffmpeg,
    fold_to_channels,
    load_audio,
    probe,
    read_stem,
    subtract_to_file,
    write_stem,
)
from .chunking import (
    Chunk,
    ChunkPlan,
    CrossfadeAssembler,
    crossfade_ramp,
    grid_step,
    plan_chunks,
    to_model_frames,
)
from .models import (
    Checkpoint,
    CheckpointError,
    checkpoints_for,
    ensure_checkpoints,
    missing_checkpoints,
)
from .presets import (
    CHUNK_MINUTES,
    CHUNK_OVERLAP_SECONDS,
    CHUNK_THRESHOLD_MINUTES,
    DEFAULT_FORMAT,
    DEFAULT_MODE,
    DEFAULT_PRESET,
    FORMATS,
    MODEL_CONFIGS,
    MODEL_SAMPLERATE,
    MODES,
    PRESETS,
    ModeSpec,
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
    subtract_from_source,
)

__all__ = [
    "AudioInfo",
    "AudioIOError",
    "LoadedAudio",
    "StemStreamWriter",
    "WriteResult",
    "find_ffmpeg",
    "fold_to_channels",
    "load_audio",
    "probe",
    "read_stem",
    "subtract_to_file",
    "write_stem",
    "Chunk",
    "ChunkPlan",
    "CrossfadeAssembler",
    "crossfade_ramp",
    "grid_step",
    "plan_chunks",
    "to_model_frames",
    "Checkpoint",
    "CheckpointError",
    "checkpoints_for",
    "ensure_checkpoints",
    "missing_checkpoints",
    "CHUNK_MINUTES",
    "CHUNK_OVERLAP_SECONDS",
    "CHUNK_THRESHOLD_MINUTES",
    "DEFAULT_FORMAT",
    "DEFAULT_MODE",
    "DEFAULT_PRESET",
    "FORMATS",
    "MODEL_CONFIGS",
    "MODEL_SAMPLERATE",
    "MODES",
    "PRESETS",
    "ModeSpec",
    "resolve_bit_depth",
    "JobResult",
    "SeparationCancelled",
    "SeparationEngine",
    "SeparationStats",
    "build_outputs",
    "describe_device",
    "mix_stems",
    "resolve_device",
    "subtract_from_source",
]
