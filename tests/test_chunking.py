"""
Tests for long-file chunking: block geometry, the crossfade, and an
end-to-end run that forces a short file through the chunked path.

The geometry tests are pure arithmetic and run in milliseconds; only the last
two touch the model (CPU, 5 s fixtures, the session-scoped engine).
"""

import math

import numpy as np
import pytest
import soundfile as sf

from engine import (
    MODEL_SAMPLERATE,
    CrossfadeAssembler,
    SeparationEngine,
    crossfade_ramp,
    grid_step,
    plan_chunks,
    to_model_frames,
)
from engine.chunking import MIN_OVERLAP_FRAMES, align_down


# =============================================================================
# The resampling grid
# =============================================================================

@pytest.mark.parametrize(
    "samplerate, expected",
    [(44100, 1), (48000, 160), (96000, 320), (32000, 320), (22050, 1), (88200, 2)],
)
def test_grid_step_matches_the_reduced_resampling_fraction(samplerate, expected):
    assert grid_step(samplerate) == expected


@pytest.mark.parametrize("samplerate", [44100, 48000, 96000, 32000])
def test_grid_aligned_offsets_map_to_exact_model_positions(samplerate):
    """
    The whole point of the grid: a grid-aligned source offset lands on a whole
    model-rate sample, so nothing drifts from block to block.
    """
    step = grid_step(samplerate)
    for multiple in (1, 7, 1000, 12345):
        frames = step * multiple
        exact = frames * MODEL_SAMPLERATE / samplerate
        assert exact == int(exact)
        assert to_model_frames(frames, samplerate) == int(exact)


# =============================================================================
# Block layout
# =============================================================================

def test_short_files_are_not_split():
    plan = plan_chunks(44100 * 60, 44100)
    assert not plan.enabled
    assert plan.count == 1
    assert plan.chunks[0].start == 0
    assert plan.chunks[0].end == 44100 * 60
    assert plan.chunks[0].head_overlap == 0
    assert plan.chunks[0].tail_overlap == 0


def test_chunking_can_be_disabled_outright():
    assert not plan_chunks(48000 * 3600, 48000, chunk_minutes=0).enabled


@pytest.mark.parametrize("samplerate", [44100, 48000, 96000])
@pytest.mark.parametrize("minutes", [37, 90, 121.5])
def test_blocks_tile_the_model_grid_without_gaps_or_drift(samplerate, minutes):
    """
    Block i emits from its own start up to where block i+1 starts, and the
    last one runs to the end: the emissions must tile [0, model_frames).
    """
    source_frames = int(minutes * 60 * samplerate)
    plan = plan_chunks(source_frames, samplerate, chunk_minutes=12.0)
    assert plan.enabled

    assert plan.chunks[0].start == 0
    assert plan.chunks[-1].end == source_frames
    assert plan.model_frames == to_model_frames(source_frames, samplerate)

    for previous, current in zip(plan.chunks, plan.chunks[1:]):
        # Shared region: the tail of one is the head of the next, same samples.
        assert current.start == previous.end - plan.overlap_frames
        assert current.head_overlap == previous.tail_overlap == plan.overlap_frames
        # And the emitted regions meet exactly at the model rate.
        emitted_end = previous.model_start + previous.model_frames - previous.model_tail_overlap
        assert emitted_end == current.model_start

    last = plan.chunks[-1]
    assert last.model_start + last.model_frames == plan.model_frames


@pytest.mark.parametrize("samplerate", [44100, 48000])
def test_every_block_boundary_sits_on_the_grid(samplerate):
    plan = plan_chunks(int(90 * 60 * samplerate), samplerate, chunk_minutes=12.0)
    for chunk in plan.chunks:
        assert chunk.start % plan.grid == 0
        assert chunk.head_overlap % plan.grid == 0
        assert chunk.tail_overlap % plan.grid == 0
    assert plan.chunk_frames % plan.grid == 0
    assert plan.overlap_frames % plan.grid == 0


@pytest.mark.parametrize("extra_seconds", [0.001, 0.5, 2, 30, 119, 600])
def test_the_last_block_is_always_longer_than_the_crossfade(extra_seconds):
    """
    A tail block shorter than the overlap would have nothing left to fade
    into. The layout has to make that impossible for any file length.
    """
    samplerate = 48000
    for blocks in range(1, 6):
        source_frames = int((blocks * 12 * 60 + extra_seconds) * samplerate)
        plan = plan_chunks(source_frames, samplerate, chunk_minutes=12.0)
        last = plan.chunks[-1]
        assert last.frames > plan.overlap_frames
        assert last.model_frames > last.model_head_overlap


def test_a_block_shorter_than_two_crossfades_disables_chunking():
    # 3 s blocks with a 2 s crossfade would leave 1 s of actual audio.
    assert not plan_chunks(48000 * 600, 48000, chunk_minutes=0.05).enabled


def test_the_threshold_keeps_short_files_on_the_one_pass_path():
    """
    The block length and the threshold answer different questions. A song is
    shorter than the threshold, so it is separated in one pass exactly as
    before, even though the default block is only 3 minutes.
    """
    song = 48000 * 60 * 5          # 5 minutes
    film = 48000 * 60 * 90         # 90 minutes
    assert not plan_chunks(song, 48000, chunk_minutes=3, threshold_minutes=12).enabled
    assert plan_chunks(film, 48000, chunk_minutes=3, threshold_minutes=12).enabled
    # A threshold of 0 means "split as soon as it does not fit in one block".
    assert plan_chunks(song, 48000, chunk_minutes=3, threshold_minutes=0).enabled


def test_the_shipped_defaults_split_a_feature_but_not_a_song():
    from engine import CHUNK_MINUTES, CHUNK_THRESHOLD_MINUTES

    def plan(minutes):
        return plan_chunks(
            int(minutes * 60 * 48000), 48000,
            chunk_minutes=CHUNK_MINUTES,
            threshold_minutes=CHUNK_THRESHOLD_MINUTES,
        )

    assert not plan(4).enabled, "a song must not be split"
    assert not plan(11).enabled, "an 11 minute file is still under the threshold"
    assert plan(90).enabled, "a 90 minute feature must be split"
    assert plan(90).count > 20, "3 minute blocks over 90 minutes"


# =============================================================================
# The crossfade itself
# =============================================================================

@pytest.mark.parametrize("frames", [2, 3, 64, 88200, 96000])
def test_the_ramps_are_complementary_to_the_last_bit(frames):
    rising = crossfade_ramp(frames)
    falling = 1.0 - rising
    # Linear and complementary: correlated material keeps its level, and the
    # stems still sum back to the source across the seam.
    assert np.max(np.abs(rising + falling - 1.0)) == 0.0
    assert rising[0] == 0.0
    assert rising[-1] == 1.0
    assert np.all(np.diff(rising) > 0)


def test_the_ramp_zeroes_each_blocks_own_edge():
    """
    The outgoing block's last sample and the incoming block's first sample are
    exactly where julius pads with 'replicate' and the model runs out of
    context. Both must be multiplied by zero.
    """
    rising = crossfade_ramp(1024)
    falling = 1.0 - rising
    assert falling[-1] == 0.0   # outgoing block's own edge
    assert rising[0] == 0.0     # incoming block's own edge


def test_a_crossfade_needs_room_to_ramp():
    with pytest.raises(ValueError):
        crossfade_ramp(MIN_OVERLAP_FRAMES - 1)


def test_align_down_never_overshoots():
    assert align_down(1000, 160) == 960
    assert align_down(960, 160) == 960
    assert align_down(159, 160) == 0


# =============================================================================
# Assembly
# =============================================================================

def _assemble(plan, signal, names=("a",)):
    """Feed each block its own slice of `signal` and stitch the result back."""
    assembler = CrossfadeAssembler(plan, names)
    pieces = []
    for chunk in plan.chunks:
        block = signal[chunk.model_start:chunk.model_start + chunk.model_frames]
        pieces.append(assembler.push(chunk, {name: block.copy() for name in names})["a"])
    assembler.finish()
    return np.concatenate(pieces, axis=0), assembler


@pytest.mark.parametrize("samplerate", [44100, 48000])
def test_assembly_reconstructs_the_signal_sample_for_sample(samplerate):
    """
    If every block returns exactly the audio it was given, the crossfade must
    put it back together unchanged. Any misalignment, any weight pair that
    does not sum to 1, shows up here.
    """
    plan = plan_chunks(int(40 * 60 * samplerate), samplerate, chunk_minutes=12.0)
    assert plan.count >= 3

    rng = np.random.default_rng(11)
    signal = rng.standard_normal((plan.model_frames, 2)).astype(np.float32)

    assembled, assembler = _assemble(plan, signal)

    assert assembled.shape == signal.shape
    assert assembler.emitted_frames == plan.model_frames
    assert assembler.warnings == []
    # float32 arithmetic in the seam, so not bit-exact, but at the ULP floor.
    assert np.max(np.abs(assembled - signal)) < 1e-6


def test_the_seams_carry_no_level_dip_or_bump():
    """
    A constant signal is the sharpest test of the weights: anything other
    than wA + wB == 1 would show as a notch or a bump at every seam.
    """
    samplerate = 48000
    plan = plan_chunks(int(40 * 60 * samplerate), samplerate, chunk_minutes=12.0)
    signal = np.full((plan.model_frames, 2), 0.5, dtype=np.float32)

    assembled, _ = _assemble(plan, signal)

    for start, end in plan.overlap_ranges():
        model_start = to_model_frames(start, samplerate)
        model_end = to_model_frames(end, samplerate)
        seam = assembled[model_start:model_end]
        assert np.max(np.abs(seam - 0.5)) < 1e-6


def test_the_null_test_survives_a_seam_between_disagreeing_blocks():
    """
    The blocks are normalised separately by separate_tensor, so two blocks can
    split the same audio slightly differently. What must hold is that their
    stems each sum back to the source: then the crossfade of the stems still
    sums to the source, because the weights sum to 1.
    """
    samplerate = 48000
    plan = plan_chunks(int(40 * 60 * samplerate), samplerate, chunk_minutes=12.0)
    rng = np.random.default_rng(3)
    source = rng.standard_normal((plan.model_frames, 2)).astype(np.float32)

    assembler = CrossfadeAssembler(plan, ("vocals", "rest"))
    vocals_out, rest_out = [], []
    for chunk in plan.chunks:
        block = source[chunk.model_start:chunk.model_start + chunk.model_frames]
        # A different, arbitrary split per block; the two always sum to block.
        bias = 0.1 + 0.3 * chunk.index
        vocals = (block * bias).astype(np.float32)
        finalized = assembler.push(chunk, {"vocals": vocals, "rest": block - vocals})
        vocals_out.append(finalized["vocals"])
        rest_out.append(finalized["rest"])
    assembler.finish()

    total = np.concatenate(vocals_out, axis=0) + np.concatenate(rest_out, axis=0)
    residual = total - source
    peak = float(np.max(np.abs(residual)))
    assert 20 * math.log10(peak) < -120 if peak > 0 else True


def test_blocks_must_be_pushed_in_order():
    plan = plan_chunks(int(40 * 60 * 48000), 48000, chunk_minutes=12.0)
    assembler = CrossfadeAssembler(plan, ("a",))
    with pytest.raises(ValueError, match="in order"):
        assembler.push(plan.chunks[1], {"a": np.zeros((plan.chunks[1].model_frames, 2), np.float32)})


def test_a_block_of_the_wrong_length_is_fixed_and_reported():
    plan = plan_chunks(int(40 * 60 * 48000), 48000, chunk_minutes=12.0)
    assembler = CrossfadeAssembler(plan, ("a",))
    first = plan.chunks[0]
    assembler.push(first, {"a": np.zeros((first.model_frames - 5, 2), np.float32)})
    assert any("came back with" in w for w in assembler.warnings)


def test_finish_rejects_an_incomplete_stream():
    plan = plan_chunks(int(40 * 60 * 48000), 48000, chunk_minutes=12.0)
    assembler = CrossfadeAssembler(plan, ("a",))
    first = plan.chunks[0]
    assembler.push(first, {"a": np.zeros((first.model_frames, 2), np.float32)})
    with pytest.raises(ValueError, match="blocks were assembled"):
        assembler.finish()


# =============================================================================
# End to end, through the real model
# =============================================================================

@pytest.mark.parametrize("samplerate", [44100, 48000])
def test_chunked_separation_is_sample_accurate_and_nulls(
    engine_cpu: SeparationEngine, make_fixture, tmp_path, samplerate
):
    """
    Force a short fixture through the chunked path and check what the
    acceptance criterion checks: same frame count as the source, and
    vocals + instrumental == source, crossfade zones included.
    """
    path, data, _ = make_fixture(samplerate, seconds=9.0)
    out_dir = tmp_path / "chunked"

    engine_cpu.reset_cancel()
    result = engine_cpu.separate_file(
        path,
        str(out_dir),
        mode="vocal_remover",
        preset="fast",
        fmt="wav",
        # 3 s blocks with a 0.5 s crossfade: 4 blocks over a 9 s file.
        chunk_minutes=3.0 / 60.0,
        chunk_overlap_seconds=0.5,
        chunk_threshold_minutes=0,
        job_id="test-chunked",
    )

    assert result.stats.chunks > 1, "the fixture did not actually get chunked"

    for name, written in result.outputs.items():
        assert written.samplerate == samplerate
        assert written.frames == data.shape[0], f"{name} is not sample-accurate"
        assert written.frame_delta == 0

    vocals, _ = sf.read(result.outputs["vocals"].path, dtype="float32", always_2d=True)
    instrumental, _ = sf.read(
        result.outputs["instrumental"].path, dtype="float32", always_2d=True
    )
    residual = (vocals + instrumental) - data[: vocals.shape[0]]
    peak = float(np.max(np.abs(residual)))
    # instrumental is built as source - vocals from the delivered file, so the
    # two null at the float32 floor no matter how many seams there were.
    assert peak < 1e-5, f"null test peak {peak}"


def test_chunked_and_unchunked_agree_on_length(
    engine_cpu: SeparationEngine, make_fixture, tmp_path
):
    """Chunking must not change the shape of what gets delivered."""
    path, data, samplerate = make_fixture(48000, seconds=9.0)

    engine_cpu.reset_cancel()
    whole = engine_cpu.separate_file(
        path, str(tmp_path / "whole"), mode="vocal_remover", preset="fast",
        fmt="wav", chunk_minutes=0, job_id="test-whole",
    )
    engine_cpu.reset_cancel()
    split = engine_cpu.separate_file(
        path, str(tmp_path / "split"), mode="vocal_remover", preset="fast",
        fmt="wav", chunk_minutes=3.0 / 60.0, chunk_overlap_seconds=0.5,
        chunk_threshold_minutes=0, job_id="test-split",
    )

    assert whole.stats.chunks == 1
    assert split.stats.chunks > 1
    assert set(whole.outputs) == set(split.outputs)
    for name in whole.outputs:
        assert whole.outputs[name].frames == split.outputs[name].frames == data.shape[0]
        assert whole.outputs[name].samplerate == split.outputs[name].samplerate == samplerate


def test_mono_source_can_be_delivered_as_mono(engine_cpu: SeparationEngine, tmp_path):
    """Point 6: a mono source stays mono when the setting asks for it."""
    samplerate = 44100
    frames = samplerate * 5
    t = np.arange(frames, dtype=np.float64) / samplerate
    mono = (0.3 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)[:, np.newaxis]
    path = tmp_path / "mono.wav"
    sf.write(str(path), mono, samplerate, subtype="PCM_24")

    engine_cpu.reset_cancel()
    result = engine_cpu.separate_file(
        str(path), str(tmp_path / "out"), mode="vocal_remover", preset="fast",
        fmt="wav", mono_output=True, job_id="test-mono",
    )

    assert result.source_channels == 1
    assert result.channels_out == 1
    assert any("mono" in w.lower() for w in result.warnings)
    for written in result.outputs.values():
        info = sf.info(written.path)
        assert info.channels == 1
        assert info.frames == frames


def test_mono_source_is_dual_mono_by_default(engine_cpu: SeparationEngine, tmp_path):
    samplerate = 44100
    frames = samplerate * 5
    t = np.arange(frames, dtype=np.float64) / samplerate
    mono = (0.3 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)[:, np.newaxis]
    path = tmp_path / "mono.wav"
    sf.write(str(path), mono, samplerate, subtype="PCM_24")

    engine_cpu.reset_cancel()
    result = engine_cpu.separate_file(
        str(path), str(tmp_path / "out"), mode="vocal_remover", preset="fast",
        fmt="wav", mono_output=False, job_id="test-dual-mono",
    )

    assert result.channels_out == 2
    for written in result.outputs.values():
        assert sf.info(written.path).channels == 2
