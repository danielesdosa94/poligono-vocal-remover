"""
Static configuration tables for the separation engine.

Everything the UI or the daemon needs to enumerate (models, quality presets,
processing modes, output formats) lives here so the rest of the engine never
hard-codes a model name or a stem list.
"""

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class Preset:
    """A quality preset: which model to run and how hard to push it."""

    name: str
    model: str
    shifts: int
    overlap: float
    relative_cost: int  # Rough compute cost relative to "fast" (1x, 4x, 16x)
    description: str


# Cost model: htdemucs_ft is a bag of 4 models (4x), and each extra shift is
# another full pass over the audio. overlap 0.5 doubles the segment count
# versus 0.25. So ultra = 4 models x 2 shifts x 2 (overlap) = 16x fast.
PRESETS: Dict[str, Preset] = {
    "fast": Preset(
        name="fast",
        model="htdemucs",
        shifts=1,
        overlap=0.25,
        relative_cost=1,
        description="Single Hybrid Transformer model, one pass",
    ),
    "hq": Preset(
        name="hq",
        model="htdemucs_ft",
        shifts=1,
        overlap=0.25,
        relative_cost=4,
        description="Fine-tuned bag of 4 models, one pass",
    ),
    "ultra": Preset(
        name="ultra",
        model="htdemucs_ft",
        shifts=2,
        overlap=0.5,
        relative_cost=16,
        description="Fine-tuned bag of 4 models, 2 shifts, 50% overlap",
    ),
}

DEFAULT_PRESET = "hq"

FOUR_STEMS: Tuple[str, ...] = ("drums", "bass", "other", "vocals")

MODEL_CONFIGS: Dict[str, Dict] = {
    "htdemucs": {
        "description": "Demucs v4 Hybrid Transformer (single model)",
        "stems": FOUR_STEMS,
        "bag_size": 1,
    },
    "htdemucs_ft": {
        "description": "Demucs v4 Hybrid Transformer, fine-tuned (bag of 4 models)",
        "stems": FOUR_STEMS,
        "bag_size": 4,
    },
    "mdx_extra": {
        # Not a "fast" model: it is a bag of 4 Demucs v3 hybrid models trained
        # with extra data, and it produces the same 4 stems as htdemucs.
        "description": "Demucs v3 Hybrid, trained with extra data (bag of 4 models)",
        "stems": FOUR_STEMS,
        "bag_size": 4,
    },
}

@dataclass(frozen=True)
class ModeSpec:
    """How a processing mode turns model stems into delivered files."""

    name: str
    # Written straight from the model: output name -> stems that are SUMMED.
    stem_outputs: Dict[str, Tuple[str, ...]]
    # Derived at the source sample rate: output name -> stem outputs that are
    # subtracted from the source.
    residual_outputs: Dict[str, Tuple[str, ...]]
    description: str


# Wherever stems are combined the rule is summation, never averaging. But the
# instrumental is not a combination: it is what is left of the source once the
# vocal is taken out, so it also carries the model's own reconstruction error
# and the two files null against the source. Summing bass+drums+other leaves
# that error behind and nulls about 15 dB worse.
MODES: Dict[str, ModeSpec] = {
    "vocal_remover": ModeSpec(
        name="vocal_remover",
        stem_outputs={"vocals": ("vocals",)},
        residual_outputs={"instrumental": ("vocals",)},
        description="2 tracks: vocals from the model, instrumental as source minus vocals",
    ),
    "splitter": ModeSpec(
        name="splitter",
        stem_outputs={
            "vocals": ("vocals",),
            "drums": ("drums",),
            "bass": ("bass",),
            "other": ("other",),
        },
        residual_outputs={},
        description="4 tracks straight from the model",
    ),
}

DEFAULT_MODE = "vocal_remover"


@dataclass(frozen=True)
class FormatSpec:
    """Container/codec choice for the written stems."""

    name: str
    extension: str
    bit_depths: Tuple[int, ...]  # Empty for lossy formats
    default_bit_depth: Optional[int]
    sample_accurate: bool  # False when the codec pads (mp3)


FORMATS: Dict[str, FormatSpec] = {
    "wav": FormatSpec("wav", ".wav", (16, 24, 32), 32, True),
    "flac": FormatSpec("flac", ".flac", (16, 24), 24, True),
    "mp3": FormatSpec("mp3", ".mp3", (), None, False),
}

DEFAULT_FORMAT = "wav"
MP3_BITRATE = "320k"


def resolve_bit_depth(fmt: str, bit_depth: Optional[int]) -> Optional[int]:
    """Validate a requested bit depth for a format, falling back to its default."""
    spec = FORMATS[fmt]
    if not spec.bit_depths:
        return None
    if bit_depth is None:
        return spec.default_bit_depth
    if bit_depth not in spec.bit_depths:
        raise ValueError(
            f"Bit depth {bit_depth} not supported for {fmt}; "
            f"choose one of {spec.bit_depths}"
        )
    return bit_depth
