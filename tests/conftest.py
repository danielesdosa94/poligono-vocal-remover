"""Shared pytest fixtures for the engine tests."""

import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_DIR = REPO_ROOT / "python"
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

from engine import SeparationEngine, find_ffmpeg  # noqa: E402


def synth_stereo(samplerate: int, seconds: float, seed: int = 7) -> np.ndarray:
    """Deterministic test signal: a few tones plus low-level noise, float32."""
    rng = np.random.default_rng(seed)
    frames = int(round(samplerate * seconds))
    t = np.arange(frames, dtype=np.float64) / samplerate
    left = (
        0.30 * np.sin(2 * np.pi * 110.0 * t)
        + 0.15 * np.sin(2 * np.pi * 440.0 * t)
        + 0.05 * np.sin(2 * np.pi * 3000.0 * t)
    )
    right = (
        0.30 * np.sin(2 * np.pi * 110.0 * t + 0.3)
        + 0.15 * np.sin(2 * np.pi * 660.0 * t)
        + 0.05 * np.sin(2 * np.pi * 2500.0 * t)
    )
    noise = rng.normal(0.0, 0.01, size=(frames, 2))
    data = np.stack([left, right], axis=1) + noise
    return np.clip(data, -0.99, 0.99).astype(np.float32)


@pytest.fixture(scope="session")
def ffmpeg_path():
    ffmpeg, _ = find_ffmpeg()
    if not ffmpeg:
        pytest.skip("ffmpeg not found in resources/bin/ffmpeg or on PATH")
    return ffmpeg


@pytest.fixture(scope="session")
def engine_cpu():
    """One CPU engine for the whole session so the model loads once."""
    return SeparationEngine(device="cpu")


@pytest.fixture
def make_fixture(tmp_path):
    """Factory: write a synthetic WAV and return (path, data, samplerate)."""

    def _make(samplerate: int, seconds: float = 5.0, subtype: str = "PCM_24", name: str = "src"):
        data = synth_stereo(samplerate, seconds)
        path = tmp_path / f"{name}_{samplerate}.wav"
        sf.write(str(path), data, samplerate, subtype=subtype)
        return str(path), data, samplerate

    return _make
