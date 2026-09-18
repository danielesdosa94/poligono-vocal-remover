"""
Engine tests. CPU only, synthetic 5 s fixtures, no Electron.

Run from the repo root:  python -m pytest tests -q
"""

import re
import tempfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from engine import (
    MODES,
    SeparationCancelled,
    build_outputs,
    load_audio,
    mix_stems,
    probe,
    subtract_from_source,
    write_stem,
)

ENGINE_DIR = Path(__file__).resolve().parents[1] / "python" / "engine"


# =============================================================================
# Static guarantees
# =============================================================================

def test_engine_has_no_forbidden_audio_backends():
    forbidden = re.compile(r"torch(audio|codec)")
    for source in ENGINE_DIR.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert not forbidden.search(text), f"{source.name} references a forbidden backend"


# =============================================================================
# Mixing math
# =============================================================================

def _fake_stems(frames: int = 1000, seed: int = 1):
    rng = np.random.default_rng(seed)
    return {
        name: rng.normal(0, 0.2, size=(frames, 2)).astype(np.float32)
        for name in ("drums", "bass", "other", "vocals")
    }


def test_mix_stems_is_a_sum_not_an_average():
    stems = _fake_stems()
    mixed = mix_stems(stems, ("drums", "bass", "other"))
    expected = np.zeros_like(stems["drums"])
    for name in ("drums", "bass", "other"):
        expected += stems[name]
    np.testing.assert_array_equal(mixed, expected)
    assert mixed.dtype == np.float32
    average = expected / 3.0
    assert not np.allclose(mixed, average)


def test_build_outputs_vocal_remover_yields_only_the_model_stem():
    """The instrumental is a residual, so it is not built from the stems."""
    stems = _fake_stems()
    outputs = build_outputs(stems, "vocal_remover")
    assert set(outputs) == {"vocals"}
    np.testing.assert_array_equal(outputs["vocals"], stems["vocals"])
    assert MODES["vocal_remover"].residual_outputs == {"instrumental": ("vocals",)}


def test_subtract_from_source_is_an_exact_float32_difference():
    rng = np.random.default_rng(11)
    source = rng.normal(0, 0.3, size=(4096, 2)).astype(np.float32)
    vocals = rng.normal(0, 0.3, size=(4096, 2)).astype(np.float32)
    residual = subtract_from_source(source, {"vocals": vocals}, ("vocals",))
    np.testing.assert_array_equal(residual, (source - vocals).astype(np.float32))
    assert residual.dtype == np.float32


def test_subtract_from_source_rejects_a_missing_dependency():
    with pytest.raises(KeyError):
        subtract_from_source(np.zeros((8, 2), np.float32), {}, ("vocals",))


def test_build_outputs_splitter_returns_four_stems_unchanged():
    stems = _fake_stems()
    outputs = build_outputs(stems, "splitter")
    assert set(outputs) == {"vocals", "drums", "bass", "other"}
    assert MODES["splitter"].residual_outputs == {}
    for name in outputs:
        np.testing.assert_array_equal(outputs[name], stems[name])


def test_build_outputs_rejects_unknown_mode():
    with pytest.raises(ValueError):
        build_outputs(_fake_stems(), "karaoke")


# =============================================================================
# audio_io: load
# =============================================================================

def test_load_audio_reads_float32_stereo(make_fixture):
    path, data, sr = make_fixture(48000, seconds=1.0, subtype="FLOAT")
    loaded = load_audio(path)
    assert loaded.samplerate == sr
    assert loaded.data.dtype == np.float32
    assert loaded.data.shape == data.shape
    assert loaded.source_frames == data.shape[0]
    np.testing.assert_array_equal(loaded.data, data)


def test_load_audio_duplicates_mono_to_stereo(tmp_path):
    sr = 44100
    mono = (0.5 * np.sin(2 * np.pi * 220 * np.arange(sr) / sr)).astype(np.float32)
    path = tmp_path / "mono.wav"
    sf.write(str(path), mono, sr, subtype="FLOAT")
    loaded = load_audio(str(path))
    assert loaded.channels_in == 1
    assert loaded.data.shape == (sr, 2)
    np.testing.assert_array_equal(loaded.data[:, 0], loaded.data[:, 1])
    assert loaded.warnings


def test_load_audio_falls_back_to_ffmpeg_for_m4a(make_fixture, ffmpeg_path, tmp_path):
    import subprocess

    path, data, sr = make_fixture(48000, seconds=1.0, subtype="PCM_16")
    m4a = tmp_path / "src.m4a"
    subprocess.run(
        [ffmpeg_path, "-hide_banner", "-loglevel", "error", "-i", path, "-c:a", "aac", "-y", str(m4a)],
        check=True,
    )
    info = probe(str(m4a), ffmpeg_path)
    assert info.samplerate == sr
    assert info.channels == 2
    loaded = load_audio(str(m4a), ffmpeg_path)
    assert loaded.samplerate == sr
    assert loaded.data.shape[1] == 2
    # AAC adds encoder delay/padding; the length must still be in the right ballpark.
    assert abs(loaded.source_frames - data.shape[0]) < sr * 0.1


# =============================================================================
# audio_io: write
# =============================================================================

def test_write_stem_float_wav_pads_and_trims(tmp_path):
    sr = 44100
    data = np.random.default_rng(3).normal(0, 0.1, size=(sr, 2)).astype(np.float32)

    longer = write_stem(data, sr, str(tmp_path / "pad.wav"), expected_frames=sr + 10)
    assert longer.frames == sr + 10 and longer.frame_delta == 0 and longer.subtype == "FLOAT"
    back, _ = sf.read(longer.path, dtype="float32", always_2d=True)
    np.testing.assert_array_equal(back[:sr], data)
    assert not back[sr:].any()

    shorter = write_stem(data, sr, str(tmp_path / "trim.wav"), expected_frames=sr - 10)
    assert shorter.frames == sr - 10 and shorter.frame_delta == 0
    back, _ = sf.read(shorter.path, dtype="float32", always_2d=True)
    np.testing.assert_array_equal(back, data[: sr - 10])


@pytest.mark.parametrize(
    "fmt,bit_depth,subtype",
    [("wav", 24, "PCM_24"), ("wav", 16, "PCM_16"), ("flac", 24, "PCM_24"), ("wav", 32, "FLOAT")],
)
def test_write_stem_resamples_to_exact_frame_count(tmp_path, ffmpeg_path, fmt, bit_depth, subtype):
    sr_in, sr_out = 44100, 48000
    frames_in = sr_in * 2 + 13
    t = np.arange(frames_in) / sr_in
    tone = (0.25 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    data = np.stack([tone, tone * 0.5], axis=1)
    expected = int(round(frames_in * sr_out / sr_in))

    result = write_stem(
        data, sr_in, str(tmp_path / f"out.{fmt}"),
        target_sr=sr_out, fmt=fmt, bit_depth=bit_depth,
        ffmpeg_path=ffmpeg_path, expected_frames=expected,
    )
    assert result.samplerate == sr_out
    assert result.subtype == subtype
    assert result.frames == expected
    assert result.frame_delta == 0

    back, _ = sf.read(result.path, dtype="float32", always_2d=True)
    # Same peak level after resample (the tone is far from Nyquist).
    assert abs(np.max(np.abs(back[:, 0])) - 0.25) < 0.01


def test_write_stem_dithers_without_resampling(tmp_path, ffmpeg_path):
    """The path a residual output takes: same rate in and out, integer target."""
    sr = 48000
    t = np.arange(sr) / sr
    tone = (0.25 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    data = np.stack([tone, tone * 0.5], axis=1)
    result = write_stem(
        data, sr, str(tmp_path / "same_rate.wav"), target_sr=sr, fmt="wav",
        bit_depth=24, ffmpeg_path=ffmpeg_path, expected_frames=sr,
    )
    assert result.samplerate == sr and result.subtype == "PCM_24"
    assert result.frames == sr and result.frame_delta == 0
    back, _ = sf.read(result.path, dtype="float32", always_2d=True)
    assert abs(np.max(np.abs(back[:, 0])) - 0.25) < 0.01


def test_write_stem_rejects_bad_bit_depth(tmp_path):
    data = np.zeros((100, 2), dtype=np.float32)
    with pytest.raises(ValueError):
        write_stem(data, 44100, str(tmp_path / "x.flac"), fmt="flac", bit_depth=32)


# =============================================================================
# Full pipeline on CPU (htdemucs, no shifts, 5 s)
# =============================================================================

@pytest.mark.parametrize("samplerate", [44100, 48000])
def test_separate_file_is_sample_accurate(engine_cpu, make_fixture, tmp_path, samplerate):
    path, data, sr = make_fixture(samplerate, seconds=5.0)
    out_root = tmp_path / "out"
    progress = []

    job_id = f"test-accurate-{samplerate}"
    engine_cpu.reset_cancel()
    result = engine_cpu.separate_file(
        path, str(out_root), mode="vocal_remover", preset="fast",
        fmt="wav", bit_depth=32, shifts=0, overlap=0.25,
        on_progress=lambda f, stage: progress.append((f, stage)),
        job_id=job_id,
    )

    assert set(result.outputs) == {"vocals", "instrumental"}
    assert result.source_samplerate == sr
    assert result.source_frames == data.shape[0]
    assert result.stats.model_samplerate == 44100

    files = {}
    for name, res in result.outputs.items():
        assert res.samplerate == sr
        assert res.subtype == "FLOAT"
        assert res.frames == data.shape[0], f"{name}: {res.frames} != {data.shape[0]}"
        assert res.frame_delta == 0
        stem, stem_sr = sf.read(res.path, dtype="float32", always_2d=True)
        assert stem_sr == sr and stem.shape == data.shape
        files[name] = stem

    # The reference is the source as the engine read it, not the array the
    # fixture was generated from: the fixture file is 24-bit, so reading it
    # back quantizes by 2**-23.
    source = load_audio(path).data

    # Contract: the instrumental IS the source minus the delivered vocals,
    # bit for bit. This is the part that is exactly reproducible.
    np.testing.assert_array_equal(
        files["instrumental"], (source - files["vocals"]).astype(np.float32)
    )

    # Summing the two files back reconstructs the source to within one float32
    # ULP. Exact equality is impossible, not merely unimplemented: for
    # s = 1e-9 and v = 0.5, fl(s - v) is -0.5 and v + (-0.5) is 0.0, so no
    # float32 instrumental can reconstruct that sample.
    recon = (files["vocals"] + files["instrumental"]).astype(np.float64)
    error = float(np.max(np.abs(recon - source.astype(np.float64))))
    scale = max(float(np.max(np.abs(source))), float(np.max(np.abs(files["vocals"]))))
    assert error <= 2 ** -23 * scale, f"reconstruction error {error:.3e} exceeds one ULP"

    fractions = [f for f, _ in progress]
    assert fractions == sorted(fractions)
    assert fractions[-1] == pytest.approx(1.0)
    assert any(stage == "separating" for _, stage in progress)

    # The job's temp dir must be gone after the job.
    temp_dir = Path(tempfile.gettempdir()) / "poligono-ai-hub" / job_id
    assert not temp_dir.exists()


def test_cancel_from_callback_aborts_and_leaves_no_files(engine_cpu, make_fixture, tmp_path):
    # 12 s -> at least two segments with htdemucs (7.8 s segment, 25% overlap),
    # so a cancel issued after the first segment fires inside the callback.
    path, _, _ = make_fixture(44100, seconds=12.0, name="long")
    out_root = tmp_path / "out"
    seen = []

    def on_progress(fraction, stage):
        seen.append((fraction, stage))
        if stage == "separating" and fraction > 0.05:
            engine_cpu.request_cancel()

    engine_cpu.reset_cancel()
    with pytest.raises(SeparationCancelled):
        engine_cpu.separate_file(
            path, str(out_root), mode="vocal_remover", preset="fast",
            shifts=0, overlap=0.25, on_progress=on_progress, job_id="test-cancel",
        )

    assert any(stage == "separating" for _, stage in seen)
    assert not (out_root / "long_44100").exists()
    assert not out_root.exists() or not any(out_root.rglob("*"))
    temp_dir = Path(tempfile.gettempdir()) / "poligono-ai-hub" / "test-cancel"
    assert not temp_dir.exists()

    # The engine is reusable after a cancel.
    engine_cpu.reset_cancel()
    assert not engine_cpu.cancel_requested
