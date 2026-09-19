"""
Long-file chunking with a linear crossfade at the seams.

A 90 minute 48 kHz file is a 2 GB float32 array, and its four stems another
7.6 GB. Past a threshold the audio is separated in blocks that overlap by a
couple of seconds, and each finished block is streamed to disk so only one
block is ever in memory.

Layout (source frames):

    source:   |=====================================================|
    chunk 0:  |------------ core ------------|~~ov~~|
    chunk 1:                            |~~ov~~|------------ core ------------|
    weight A: ---------------------------------1.0\\
    weight B:                              0.0/----/      wA + wB == 1.0

Four properties make the seam inaudible and the output sample-accurate:

1. Every chunk's own edge sits exactly where its weight is zero. julius pads
   with 'replicate' at the edges and the model has its own edge behaviour;
   both land on samples that are multiplied by 0.0 and thrown away.
2. The crossfade is LINEAR, not equal-power. The two blocks carry the same
   audio through the same model, so they are correlated: amplitudes add, and
   linear weights that sum to 1 preserve level. Equal-power would put a +3 dB
   bump on every seam.
3. The null test survives. In the shared region sum_stems(out) =
   (1-w)*sum_stems(A) + w*sum_stems(B), and since both reconstruct the same
   source samples the result is that source, exactly. In vocal_remover mode
   the guarantee is stronger still: the instrumental is built as
   source - vocals AFTER assembly, so the two files null by construction.
4. Chunk boundaries land on exact positions of the 44.1 kHz model grid. With
   q = samplerate // gcd(samplerate, 44100), any offset that is a multiple of
   q maps to an integer model-rate offset (160 source frames at 48 kHz are
   exactly 147 at 44.1 kHz), so nothing drifts from block to block.

Known trade-off: demucs.api.Separator.separate_tensor() normalises by the
mean and standard deviation of the tensor it is handed, so each block is
normalised on its own statistics. Output level is unaffected (the operation
is undone on the way out) but the model's separation decisions can differ
slightly between blocks. The crossfade is what smooths that over, and it is
why blocks are long (12 minutes by default) rather than short.
"""

from dataclasses import dataclass
from math import gcd
from typing import Dict, List, Optional, Tuple

import numpy as np

from .presets import CHUNK_MINUTES, CHUNK_OVERLAP_SECONDS, MODEL_SAMPLERATE

# A crossfade needs at least two samples to ramp between.
MIN_OVERLAP_FRAMES = 2


def grid_step(samplerate: int, model_samplerate: int = MODEL_SAMPLERATE) -> int:
    """
    Source frames per step of the common source/model resampling grid.

    Any offset that is a multiple of this maps to an exact integer offset at
    the model rate, which is what keeps chunk boundaries from drifting.
    48 kHz -> 160 (160 frames at 48k are 147 at 44.1k); 44.1 kHz -> 1.
    """
    if samplerate <= 0:
        raise ValueError(f"Invalid sample rate: {samplerate}")
    return samplerate // gcd(samplerate, model_samplerate)


def align_down(frames: int, step: int) -> int:
    """Largest multiple of `step` that is <= frames."""
    return (frames // step) * step


def to_model_frames(
    frames: int, samplerate: int, model_samplerate: int = MODEL_SAMPLERATE
) -> int:
    """
    Frame count at the model rate for `frames` source frames.

    Matches julius.ResampleFrac, which returns floor(new_sr * length / old_sr)
    on the gcd-reduced fraction. Exact (no floor taken) when `frames` is a
    multiple of grid_step().
    """
    if samplerate == model_samplerate:
        return frames
    return frames * model_samplerate // samplerate


@dataclass(frozen=True)
class Chunk:
    """One block of the source, and where its output belongs."""

    index: int
    start: int              # First source frame (inclusive)
    end: int                # Last source frame (exclusive)
    model_start: int        # Same position on the model-rate grid
    model_frames: int       # Frames this block should come back with
    head_overlap: int       # Source frames shared with the previous chunk
    tail_overlap: int       # Source frames shared with the next chunk
    model_head_overlap: int
    model_tail_overlap: int

    @property
    def frames(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class ChunkPlan:
    """The full block layout for one job."""

    chunks: List[Chunk]
    samplerate: int
    model_samplerate: int
    grid: int
    chunk_frames: int
    overlap_frames: int
    source_frames: int
    model_frames: int

    @property
    def count(self) -> int:
        return len(self.chunks)

    @property
    def enabled(self) -> bool:
        """True when the job is actually split (more than one block)."""
        return len(self.chunks) > 1

    @property
    def overlap_seconds(self) -> float:
        return self.overlap_frames / self.samplerate if self.samplerate else 0.0

    @property
    def chunk_seconds(self) -> float:
        return self.chunk_frames / self.samplerate if self.samplerate else 0.0

    def describe(self) -> str:
        return (
            f"{self.count} block(s) of {self.chunk_seconds / 60:.1f} min "
            f"with {self.overlap_seconds:.1f}s crossfade "
            f"(grid {self.grid} frames @ {self.samplerate} Hz)"
        )

    def overlap_ranges(self) -> List[Tuple[int, int]]:
        """Source-frame ranges where two blocks are crossfaded together."""
        return [
            (chunk.start, chunk.start + chunk.head_overlap)
            for chunk in self.chunks
            if chunk.head_overlap
        ]


def plan_chunks(
    source_frames: int,
    samplerate: int,
    chunk_minutes: Optional[float] = None,
    overlap_seconds: float = CHUNK_OVERLAP_SECONDS,
    threshold_minutes: Optional[float] = None,
    model_samplerate: int = MODEL_SAMPLERATE,
) -> ChunkPlan:
    """
    Lay out the blocks for a source of `source_frames` frames.

    Args:
        source_frames: Length of the source at `samplerate`.
        chunk_minutes: Core length of one block. None uses CHUNK_MINUTES;
            0 or less disables chunking entirely.
        overlap_seconds: Width of the crossfade between blocks.
        threshold_minutes: Files shorter than this are never split. None
            follows `chunk_minutes`, i.e. a file is split as soon as it does
            not fit in a single block.

    Returns:
        A ChunkPlan. `enabled` is False when the whole file fits in one block,
        in which case `chunks` holds that single block.
    """
    if source_frames <= 0:
        raise ValueError(f"Invalid frame count: {source_frames}")

    grid = grid_step(samplerate, model_samplerate)
    minutes = CHUNK_MINUTES if chunk_minutes is None else float(chunk_minutes)
    # The threshold follows the block length: a file is split exactly when it
    # does not fit in one block. Passing it explicitly raises the bar (keep
    # files under N minutes whole even though the blocks are shorter).
    threshold = minutes if threshold_minutes is None else float(threshold_minutes)

    chunk_frames = align_down(int(minutes * 60 * samplerate), grid)
    # The overlap must sit on the grid and still be wide enough to ramp over.
    min_overlap = grid * max(1, -(-MIN_OVERLAP_FRAMES // grid))
    overlap_frames = max(align_down(int(overlap_seconds * samplerate), grid), min_overlap)

    threshold_frames = int(threshold * 60 * samplerate)
    # A block has to be long enough to hold a head and a tail crossfade plus
    # some audio of its own, or the layout degenerates.
    too_small = chunk_frames <= 2 * overlap_frames
    single = (
        minutes <= 0
        or too_small
        or source_frames <= threshold_frames
        or source_frames <= chunk_frames
    )

    if single:
        return ChunkPlan(
            chunks=[
                Chunk(
                    index=0,
                    start=0,
                    end=source_frames,
                    model_start=0,
                    model_frames=to_model_frames(source_frames, samplerate, model_samplerate),
                    head_overlap=0,
                    tail_overlap=0,
                    model_head_overlap=0,
                    model_tail_overlap=0,
                )
            ],
            samplerate=samplerate,
            model_samplerate=model_samplerate,
            grid=grid,
            chunk_frames=source_frames,
            overlap_frames=0,
            source_frames=source_frames,
            model_frames=to_model_frames(source_frames, samplerate, model_samplerate),
        )

    hop = chunk_frames - overlap_frames
    starts = [0]
    while starts[-1] + chunk_frames < source_frames:
        starts.append(starts[-1] + hop)

    # The loop guarantees the last block is longer than the overlap: it was
    # only appended because the previous block ended before the source did,
    # i.e. start[-1] + overlap < source_frames.
    chunks: List[Chunk] = []
    last = len(starts) - 1
    for index, start in enumerate(starts):
        end = min(start + chunk_frames, source_frames)
        head = overlap_frames if index > 0 else 0
        tail = overlap_frames if index < last else 0
        chunks.append(
            Chunk(
                index=index,
                start=start,
                end=end,
                model_start=to_model_frames(start, samplerate, model_samplerate),
                model_frames=to_model_frames(end - start, samplerate, model_samplerate),
                head_overlap=head,
                tail_overlap=tail,
                model_head_overlap=to_model_frames(head, samplerate, model_samplerate),
                model_tail_overlap=to_model_frames(tail, samplerate, model_samplerate),
            )
        )

    return ChunkPlan(
        chunks=chunks,
        samplerate=samplerate,
        model_samplerate=model_samplerate,
        grid=grid,
        chunk_frames=chunk_frames,
        overlap_frames=overlap_frames,
        source_frames=source_frames,
        model_frames=to_model_frames(source_frames, samplerate, model_samplerate),
    )


def crossfade_ramp(frames: int) -> np.ndarray:
    """
    Rising linear ramp of `frames` samples, from exactly 0.0 to exactly 1.0.

    The falling ramp is 1 - this, so the pair sums to 1.0 at every sample and
    the outgoing block's very last sample (and the incoming block's very
    first) are multiplied by zero.
    """
    if frames < MIN_OVERLAP_FRAMES:
        raise ValueError(f"A crossfade needs at least {MIN_OVERLAP_FRAMES} frames, got {frames}")
    return np.linspace(0.0, 1.0, frames, dtype=np.float32)


class CrossfadeAssembler:
    """
    Turns per-block outputs into one continuous stream.

    Call push() with each block's outputs in order; it returns the samples
    that are final and may be appended to disk, keeping only the tail of the
    current block (a couple of seconds) in memory for the next seam.

    The blocks returned by successive push() calls tile [0, plan.model_frames)
    exactly: block i emits everything from its own start up to where block
    i+1 starts.
    """

    def __init__(self, plan: ChunkPlan, names: Tuple[str, ...]):
        self.plan = plan
        self.names = tuple(names)
        self.warnings: List[str] = []
        self._tail: Dict[str, np.ndarray] = {}
        self._emitted = 0
        self._next_index = 0

    @property
    def emitted_frames(self) -> int:
        return self._emitted

    def push(self, chunk: Chunk, outputs: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
        """
        Feed one block's outputs; get back the samples that are now final.

        Args:
            chunk: The block these outputs came from, in plan order.
            outputs: name -> float32 (model_frames, channels) at the model rate.

        Returns:
            name -> float32 block to append, all of the same length.
        """
        if chunk.index != self._next_index:
            raise ValueError(
                f"Chunks must be pushed in order: expected {self._next_index}, got {chunk.index}"
            )
        missing = [name for name in self.names if name not in outputs]
        if missing:
            raise KeyError(f"Chunk {chunk.index} is missing outputs {missing}")

        head = chunk.model_head_overlap
        tail = chunk.model_tail_overlap
        fade_in = crossfade_ramp(head)[:, np.newaxis] if head else None

        final: Dict[str, np.ndarray] = {}
        for name in self.names:
            array = self._fit(name, chunk, outputs[name])
            body_end = array.shape[0] - tail

            if head:
                previous = self._tail.get(name)
                if previous is None or previous.shape[0] != head:
                    raise ValueError(f"No tail kept for '{name}' before chunk {chunk.index}")
                # Linear, complementary: the weights sum to 1.0 at every
                # sample, so correlated material keeps its level and the
                # stems still sum back to the source across the seam.
                seam = previous * (1.0 - fade_in) + array[:head] * fade_in
                final[name] = np.concatenate([seam, array[head:body_end]], axis=0)
            else:
                final[name] = np.ascontiguousarray(array[:body_end])

            if tail:
                self._tail[name] = np.ascontiguousarray(array[body_end:])
            else:
                self._tail.pop(name, None)

        self._next_index += 1
        if final:
            self._emitted += next(iter(final.values())).shape[0]
        return final

    def _fit(self, name: str, chunk: Chunk, array: np.ndarray) -> np.ndarray:
        """
        Guard the grid arithmetic at runtime.

        The block should come back with exactly chunk.model_frames frames. If
        the resampler ever disagrees, pad or trim and say so rather than
        silently sliding every later block.
        """
        array = np.asarray(array, dtype=np.float32)
        delta = array.shape[0] - chunk.model_frames
        if delta == 0:
            return array
        self.warnings.append(
            f"Block {chunk.index} of '{name}' came back with {array.shape[0]} frames, "
            f"expected {chunk.model_frames} ({delta:+d}); padded/trimmed to keep the seams aligned"
        )
        if delta > 0:
            return array[: chunk.model_frames]
        pad = np.zeros((-delta, array.shape[1]), dtype=np.float32)
        return np.concatenate([array, pad], axis=0)

    def finish(self) -> None:
        """Check that every block was pushed and the stream came out whole."""
        if self._next_index != self.plan.count:
            raise ValueError(
                f"Only {self._next_index} of {self.plan.count} blocks were assembled"
            )
        if self._emitted != self.plan.model_frames:
            self.warnings.append(
                f"Assembled {self._emitted} frames at {self.plan.model_samplerate} Hz, "
                f"expected {self.plan.model_frames}"
            )
