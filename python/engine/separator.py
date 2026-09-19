"""
Separation engine built on demucs.api.Separator.

- One Separator per (model, shifts, overlap, device), cached for the whole
  session so the weights load once.
- Progress comes from the Demucs callback, not from parsing tqdm output.
- Cancellation is cooperative: a threading.Event checked inside the callback;
  when set, SeparationCancelled is raised out of separate_tensor().
- The audio chain is float32 end to end; the instrumental is the SUM of the
  non-vocal stems.
"""

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import torch

from .audio_io import (
    LoadedAudio,
    StemStreamWriter,
    WriteResult,
    fold_to_channels,
    load_audio,
    make_temp_dir,
    read_stem,
    remove_temp_dir,
    subtract_to_file,
    write_stem,
)
from .chunking import ChunkPlan, CrossfadeAssembler, plan_chunks
from .models import CheckpointError, ensure_checkpoints
from .presets import (
    CHUNK_OVERLAP_SECONDS,
    CHUNK_THRESHOLD_MINUTES,
    DEFAULT_FORMAT,
    DEFAULT_MODE,
    DEFAULT_PRESET,
    FORMATS,
    MODEL_CONFIGS,
    MODES,
    PRESETS,
    resolve_bit_depth,
)

ProgressCallback = Callable[[float], None]           # fraction 0..1
StageProgressCallback = Callable[[float, str], None]  # fraction 0..1, stage name
LogCallback = Callable[[str, str], None]              # message, level
DownloadCallback = Callable[[dict], None]             # checkpoint download tick

# Share of the job's progress bar given to each stage of separate_file().
STAGE_LOAD_END = 0.05
STAGE_SEPARATE_END = 0.90


class SeparationCancelled(Exception):
    """Raised when a cancel request is honoured inside the separation loop."""


@dataclass
class SeparationStats:
    model: str
    shifts: int
    overlap: float
    device: str
    model_samplerate: int
    model_loaded_now: bool
    separate_seconds: float
    chunks: int = 1
    chunk_seconds: float = 0.0
    crossfade_seconds: float = 0.0


@dataclass
class JobResult:
    outputs: Dict[str, WriteResult]
    output_dir: str
    source_samplerate: int
    source_frames: int
    source_channels: int
    stats: SeparationStats
    elapsed_seconds: float
    warnings: List[str] = field(default_factory=list)
    channels_out: int = 2


def resolve_device(requested: str = "auto") -> Tuple[str, Optional[str]]:
    """
    Pick the compute device.

    Returns:
        (device, warning) where warning explains a fallback to CPU, if any.
    """
    requested = (requested or "auto").lower()
    if requested == "cpu":
        return "cpu", None
    if torch.cuda.is_available():
        return "cuda", None
    if requested == "cuda":
        return "cpu", "CUDA requested but not available; falling back to CPU"
    return "cpu", None


def describe_device(device: str) -> str:
    if device == "cuda" and torch.cuda.is_available():
        props = torch.cuda.get_device_properties(0)
        return f"{props.name} ({props.total_memory / 1024**3:.1f} GB VRAM)"
    return "CPU"


def mix_stems(stems: Dict[str, np.ndarray], names: Tuple[str, ...]) -> np.ndarray:
    """Sum the named stems in float32. Never averages."""
    first = stems[names[0]]
    out = np.zeros_like(first, dtype=np.float32)
    for name in names:
        out += stems[name]
    return out


def build_outputs(stems: Dict[str, np.ndarray], mode: str) -> Dict[str, np.ndarray]:
    """
    Map raw model stems to the outputs that come straight from the model.

    Residual outputs (see ModeSpec.residual_outputs) are NOT built here: they
    need the source at its own sample rate and are built while writing.
    """
    if mode not in MODES:
        raise ValueError(f"Unknown mode: {mode}")
    outputs: Dict[str, np.ndarray] = {}
    for out_name, sources in MODES[mode].stem_outputs.items():
        missing = [s for s in sources if s not in stems]
        if missing:
            raise KeyError(f"Model did not produce stems {missing} needed for '{out_name}'")
        if len(sources) == 1:
            outputs[out_name] = stems[sources[0]]
        else:
            outputs[out_name] = mix_stems(stems, sources)
    return outputs


def subtract_from_source(
    source: np.ndarray, written: Dict[str, np.ndarray], names: Tuple[str, ...]
) -> np.ndarray:
    """
    Build a residual output: the source minus the named outputs, in float32.

    Exact reconstruction of the source from the two files is not achievable in
    float32: fl(v + fl(s - v)) differs from s when |s| << |v|, by at most half
    an ULP (about -150 dBFS at full scale, below a 24-bit LSB). What IS exact
    is this subtraction, so instrumental == source - vocals sample for sample.
    """
    missing = [n for n in names if n not in written]
    if missing:
        raise KeyError(f"Residual output needs outputs {missing}, which were not written")
    residual = np.array(source, dtype=np.float32, copy=True)
    for name in names:
        residual -= written[name]
    return residual


class SeparationEngine:
    """
    Long-lived separation engine: load models once, run many jobs.

    Thread model: one job at a time (guarded by a lock). request_cancel() may
    be called from any thread; the running job notices at the next Demucs
    callback (a few hundred ms at most on GPU).
    """

    def __init__(self, device: str = "auto", log: Optional[LogCallback] = None):
        self.device, warning = resolve_device(device)
        self._log = log or (lambda message, level="info": None)
        if warning:
            self._log(warning, "warning")
        self._separators: Dict[tuple, object] = {}
        self._cancel = threading.Event()
        self._job_lock = threading.Lock()
        self._progress_ctx: Optional[dict] = None

        if self.device == "cuda":
            # TF32 is plenty for inference and roughly doubles matmul
            # throughput on Ampere and newer.
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True
        self._log(f"Compute device: {describe_device(self.device)}", "info")

    # ------------------------------------------------------------------ cancel

    def request_cancel(self) -> None:
        self._cancel.set()

    def reset_cancel(self) -> None:
        self._cancel.clear()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise SeparationCancelled()

    # ------------------------------------------------------------------ models

    @property
    def loaded_models(self) -> List[tuple]:
        return list(self._separators.keys())

    def get_separator(
        self,
        model: str,
        shifts: int,
        overlap: float,
        on_download: Optional[DownloadCallback] = None,
    ):
        """Return a cached demucs.api.Separator, loading it on first use."""
        if model not in MODEL_CONFIGS:
            raise ValueError(f"Unknown model: {model}")
        key = (model, int(shifts), float(overlap), self.device)
        separator = self._separators.get(key)
        loaded_now = False
        if separator is None:
            # Fetch the weights first, with progress. Left to Demucs, this
            # would happen inside Separator() with no callback at all and an
            # announcement written straight to stdout, which is the daemon's
            # JSON channel.
            self._fetch_checkpoints(model, on_download)

            import demucs.api  # Deferred: importing torch models is slow

            self._log(f"Loading model {model} (shifts={shifts}, overlap={overlap})...", "info")
            t0 = time.perf_counter()
            separator = demucs.api.Separator(
                model=model,
                device=self.device,
                shifts=int(shifts),
                overlap=float(overlap),
                split=True,
                jobs=0,
                progress=False,
                callback=self._on_callback,
            )
            self._separators[key] = separator
            loaded_now = True
            self._log(f"Model loaded: {model} in {time.perf_counter() - t0:.1f}s", "info")
        return separator, loaded_now

    def _fetch_checkpoints(self, model: str, on_download: Optional[DownloadCallback]) -> None:
        """
        Download whatever weights `model` is missing, reporting progress.

        A failure here is not fatal: we fall through and let Demucs try its
        own download path, which still works, just without a progress bar.
        A cancel, on the other hand, must propagate.
        """
        try:
            ensure_checkpoints(
                model,
                on_progress=on_download,
                cancel_check=self._check_cancel,
                log=self._log,
            )
        except SeparationCancelled:
            raise
        except CheckpointError as exc:
            self._log(f"Checkpoint pre-download failed ({exc}); letting Demucs fetch it", "warning")
        except Exception as exc:  # noqa: BLE001 - never block a job over the progress bar
            self._log(
                f"Checkpoint pre-download skipped ({type(exc).__name__}: {exc})", "warning"
            )

    def _segment_stride(self, separator, overlap: float) -> int:
        """Frames between consecutive segments, for progress estimation."""
        model = separator.model
        sub_model = model.models[0] if hasattr(model, "models") else model
        segment = getattr(separator, "_segment", None) or float(sub_model.segment)
        segment_length = int(separator.samplerate * segment)
        return max(1, int((1.0 - overlap) * segment_length))

    # ---------------------------------------------------------------- callback

    def _on_callback(self, info: dict) -> None:
        """
        Demucs progress hook. Called at the start and end of every segment.

        Keys (always present): state, model_idx_in_bag, shift_idx,
        segment_offset, audio_length, models.
        """
        self._check_cancel()

        ctx = self._progress_ctx
        if ctx is None or info.get("state") != "end":
            return

        models = max(1, int(info.get("models", 1)))
        shifts = max(1, int(ctx["shifts"]))
        audio_length = max(1, int(info.get("audio_length", 1)))
        segment_done = min(1.0, (int(info.get("segment_offset", 0)) + ctx["stride"]) / audio_length)
        passes_done = int(info.get("model_idx_in_bag", 0)) * shifts + int(info.get("shift_idx", 0))
        fraction = (passes_done + segment_done) / (models * shifts)
        fraction = min(1.0, max(0.0, fraction))

        if fraction > ctx["last"]:
            ctx["last"] = fraction
            on_progress = ctx.get("on_progress")
            if on_progress:
                on_progress(fraction)

    # ---------------------------------------------------------------- separate

    def separate(
        self,
        data: np.ndarray,
        samplerate: int,
        preset: Optional[str] = None,
        model: Optional[str] = None,
        shifts: Optional[int] = None,
        overlap: Optional[float] = None,
        on_progress: Optional[ProgressCallback] = None,
        on_download: Optional[DownloadCallback] = None,
        keep: Optional[Tuple[str, ...]] = None,
    ) -> Tuple[Dict[str, np.ndarray], int, SeparationStats]:
        """
        Separate a float32 (frames, channels) array into stems.

        Parameters resolve from the preset, then explicit overrides.

        A single-channel input is expanded to stereo here, before the model
        sees it: demucs.api only fixes the channel count on its way through
        the resampler (api.py:265), so a mono array at exactly 44.1 kHz would
        otherwise reach the first convolution with one channel and blow up.
        The stems always come back with the model's channel count; folding
        them back to mono is the caller's job.

        Args:
            keep: Only copy out these stems. The others are still computed by
                the model (it emits all four in one tensor) but are never
                copied into numpy, which on a long file is hundreds of MB the
                caller was going to throw away anyway.

        Returns:
            (stems, model_samplerate, stats). Stems are float32
            (frames_at_model_rate, channels) arrays at the model's sample rate
            (44100 for Demucs v4), NOT at the input rate.
        """
        chosen = PRESETS[preset or DEFAULT_PRESET]
        model = model or chosen.model
        shifts = chosen.shifts if shifts is None else int(shifts)
        overlap = chosen.overlap if overlap is None else float(overlap)

        with self._job_lock:
            self._check_cancel()
            separator, loaded_now = self.get_separator(model, shifts, overlap, on_download)
            self._check_cancel()

            block = np.asarray(data, dtype=np.float32)
            if block.ndim == 2 and block.shape[1] == 1:
                model_channels = getattr(separator, "audio_channels", 2) or 2
                block = np.repeat(block, model_channels, axis=1)

            self._progress_ctx = {
                "shifts": shifts,
                "stride": self._segment_stride(separator, overlap),
                "last": 0.0,
                "on_progress": on_progress,
            }
            t0 = time.perf_counter()
            try:
                # (frames, ch) -> (ch, frames) as a contiguous COPY:
                # separate_tensor normalises its input in place and we must
                # not touch the caller's array. The tensor is passed inline
                # and never bound to a local: separate_tensor rebinds its own
                # parameter when it resamples, so with no reference of ours
                # the copy is freed right there instead of sitting on hundreds
                # of MB for the whole model run.
                _, separated = separator.separate_tensor(
                    torch.from_numpy(np.ascontiguousarray(block.T)), int(samplerate)
                )
            finally:
                self._progress_ctx = None
                del block
            elapsed = time.perf_counter() - t0

            # One stem at a time, and only the ones the caller asked for: the
            # model emits all four whatever we do, but copying the unwanted
            # ones into numpy is hundreds of MB per block thrown away.
            stems: Dict[str, np.ndarray] = {}
            for name in list(separated):
                tensor = separated.pop(name)
                if keep is None or name in keep:
                    stems[name] = np.ascontiguousarray(
                        tensor.detach().cpu().numpy().T, dtype=np.float32
                    )
                del tensor
            del separated

            if on_progress:
                on_progress(1.0)

            stats = SeparationStats(
                model=model,
                shifts=shifts,
                overlap=overlap,
                device=self.device,
                model_samplerate=int(separator.samplerate),
                model_loaded_now=loaded_now,
                separate_seconds=elapsed,
            )
            return stems, int(separator.samplerate), stats

    # --------------------------------------------------------------- chunked

    def _separate_chunked(
        self,
        audio: LoadedAudio,
        plan: ChunkPlan,
        mode: str,
        temp_dir: Path,
        preset: Optional[str],
        model: Optional[str],
        shifts: Optional[int],
        overlap: Optional[float],
        on_progress: Optional[ProgressCallback],
        on_download: Optional[DownloadCallback],
    ) -> Tuple[Dict[str, Path], int, SeparationStats, List[str]]:
        """
        Separate a long file block by block, streaming the result to disk.

        Each block is separated on its own, crossfaded with the previous one
        over the shared region (see engine/chunking.py) and appended to a
        float32 WAV per output at the model rate. Only one block and a
        couple of seconds of tail are ever in memory, which is what keeps a
        90 minute job from needing the ~10 GB the one-shot path would.

        The mode's outputs are built per block rather than afterwards, so
        only the stems the mode actually delivers ever reach the disk. That
        is safe because build_outputs() and the crossfade are both linear and
        therefore commute.

        Returns:
            (output name -> assembled float32 WAV, model rate, stats, warnings)
        """
        names: Tuple[str, ...] = tuple(MODES[mode].stem_outputs)
        # The model stems this mode actually consumes. In vocal_remover that
        # is just "vocals"; the other three are dropped before they are ever
        # copied out of torch.
        needed: Tuple[str, ...] = tuple(
            {source for sources in MODES[mode].stem_outputs.values() for source in sources}
        )
        channels_out = int(audio.data.shape[1])
        assembler = CrossfadeAssembler(plan, names)
        writers: Dict[str, StemStreamWriter] = {}
        paths: Dict[str, Path] = {}

        model_sr = plan.model_samplerate
        stats: Optional[SeparationStats] = None
        separate_seconds = 0.0
        loaded_now = False
        step = 1.0 / plan.count

        try:
            for chunk in plan.chunks:
                self._check_cancel()
                self._log(
                    f"Block {chunk.index + 1}/{plan.count}: "
                    f"frames {chunk.start}-{chunk.end} "
                    f"({chunk.frames / audio.samplerate / 60:.1f} min)",
                    "debug",
                )
                block = np.ascontiguousarray(audio.data[chunk.start:chunk.end])
                base = chunk.index * step

                chunk_stems, chunk_model_sr, chunk_stats = self.separate(
                    block,
                    audio.samplerate,
                    preset=preset,
                    model=model,
                    shifts=shifts,
                    overlap=overlap,
                    on_progress=(
                        (lambda fraction, b=base: on_progress(b + step * fraction))
                        if on_progress
                        else None
                    ),
                    # Only the first block can find the weights missing.
                    on_download=on_download if chunk.index == 0 else None,
                    keep=needed,
                )
                del block
                model_sr = chunk_model_sr
                separate_seconds += chunk_stats.separate_seconds
                loaded_now = loaded_now or chunk_stats.model_loaded_now
                stats = chunk_stats

                outputs = build_outputs(chunk_stems, mode)
                del chunk_stems
                if channels_out == 1:
                    outputs = {n: fold_to_channels(a, 1) for n, a in outputs.items()}

                finalized = assembler.push(chunk, outputs)
                del outputs

                if not writers:
                    for name in names:
                        path = temp_dir / f"assembled_{name}.f32.wav"
                        writers[name] = StemStreamWriter(path, model_sr, channels_out)
                        paths[name] = path
                for name, finished in finalized.items():
                    writers[name].append(finished)
                del finalized

            assembler.finish()
        finally:
            for writer in writers.values():
                writer.close()

        if stats is None:
            raise RuntimeError("Chunk plan produced no blocks")

        merged = SeparationStats(
            model=stats.model,
            shifts=stats.shifts,
            overlap=stats.overlap,
            device=stats.device,
            model_samplerate=model_sr,
            model_loaded_now=loaded_now,
            separate_seconds=separate_seconds,
            chunks=plan.count,
            chunk_seconds=plan.chunk_seconds,
            crossfade_seconds=plan.overlap_seconds,
        )
        return paths, model_sr, merged, list(assembler.warnings)

    # ------------------------------------------------------------ full pipeline

    def separate_file(
        self,
        input_path: str,
        output_dir: str,
        mode: str = DEFAULT_MODE,
        preset: str = DEFAULT_PRESET,
        fmt: str = DEFAULT_FORMAT,
        bit_depth: Optional[int] = None,
        ffmpeg_path: Optional[str] = None,
        on_progress: Optional[StageProgressCallback] = None,
        job_id: Optional[str] = None,
        model: Optional[str] = None,
        shifts: Optional[int] = None,
        overlap: Optional[float] = None,
        mono_output: bool = False,
        chunk_minutes: Optional[float] = None,
        chunk_overlap_seconds: float = CHUNK_OVERLAP_SECONDS,
        chunk_threshold_minutes: Optional[float] = CHUNK_THRESHOLD_MINUTES,
        on_download: Optional[DownloadCallback] = None,
    ) -> JobResult:
        """
        Load -> separate -> build mode outputs -> write sample-accurate stems.

        Output layout: <output_dir>/<input name>/<stem>.<ext>, every stem at
        the source sample rate with exactly the source frame count.

        Long files (past CHUNK_THRESHOLD_MINUTES, or `chunk_minutes`) are
        separated in overlapping blocks and streamed to disk; see
        engine/chunking.py. Files below the threshold take exactly the same
        one-shot path as before.

        Args:
            mono_output: Deliver mono stems when the source is mono, instead
                of dual-mono stereo.
            chunk_minutes: Block length for long files; 0 disables chunking.
            chunk_overlap_seconds: Width of the crossfade between blocks.
            chunk_threshold_minutes: Files shorter than this are never split,
                whatever the block length. That is what keeps a song or a
                scene on exactly the one-pass path it took before. Pass 0 to
                split as soon as the file does not fit in one block.
            on_download: Called with progress ticks while model weights are
                being fetched.

        Cancellation: the caller is responsible for reset_cancel() before a
        new job; a cancel raises SeparationCancelled and removes every file
        this job wrote plus its temp directory.
        """
        if mode not in MODES:
            raise ValueError(f"Unknown mode: {mode}")
        fmt = fmt.lower()
        if fmt not in FORMATS:
            raise ValueError(f"Unsupported format: {fmt}")
        bit_depth = resolve_bit_depth(fmt, bit_depth)

        def progress(fraction: float, stage: str) -> None:
            if on_progress:
                on_progress(min(1.0, max(0.0, fraction)), stage)

        t_start = time.perf_counter()
        temp_dir = make_temp_dir(job_id)
        job_dir = Path(output_dir) / Path(input_path).stem
        written: List[str] = []
        warnings: List[str] = []

        try:
            progress(0.0, "loading")
            self._check_cancel()
            audio: LoadedAudio = load_audio(
                input_path, ffmpeg_path, temp_dir, mono_output=mono_output
            )
            warnings.extend(audio.warnings)
            for message in audio.warnings:
                self._log(message, "warning")
            self._log(
                f"Loaded {os.path.basename(input_path)}: {audio.samplerate} Hz, "
                f"{audio.channels_in} ch, {audio.source_frames} frames",
                "info",
            )
            progress(STAGE_LOAD_END, "loading")
            self._check_cancel()

            span = STAGE_SEPARATE_END - STAGE_LOAD_END

            def separate_progress(fraction: float) -> None:
                progress(STAGE_LOAD_END + span * fraction, "separating")

            plan = plan_chunks(
                audio.source_frames,
                audio.samplerate,
                chunk_minutes=chunk_minutes,
                overlap_seconds=chunk_overlap_seconds,
                threshold_minutes=chunk_threshold_minutes,
            )
            # sources: output name -> in-memory array (short files) or an
            # assembled float32 WAV at the model rate (chunked files).
            sources: Dict[str, object]
            if plan.enabled:
                self._log(f"Long file, splitting into {plan.describe()}", "info")
                sources, model_sr, stats, chunk_warnings = self._separate_chunked(
                    audio,
                    plan,
                    mode,
                    temp_dir,
                    preset=preset,
                    model=model,
                    shifts=shifts,
                    overlap=overlap,
                    on_progress=separate_progress,
                    on_download=on_download,
                )
                warnings.extend(chunk_warnings)
                for message in chunk_warnings:
                    self._log(message, "warning")
            else:
                stems, model_sr, stats = self.separate(
                    audio.data,
                    audio.samplerate,
                    preset=preset,
                    model=model,
                    shifts=shifts,
                    overlap=overlap,
                    on_progress=separate_progress,
                    on_download=on_download,
                )
                outputs_arrays = build_outputs(stems, mode)
                del stems
                if audio.data.shape[1] == 1:
                    outputs_arrays = {
                        name: fold_to_channels(array, 1)
                        for name, array in outputs_arrays.items()
                    }
                sources = dict(outputs_arrays)
                del outputs_arrays

            job_dir.mkdir(parents=True, exist_ok=True)
            spec = MODES[mode]
            results: Dict[str, WriteResult] = {}
            written_arrays: Dict[str, np.ndarray] = {}
            total = len(sources) + len(spec.residual_outputs)
            index = 0

            def emit_output(name: str, source: object, sr_in: int) -> None:
                nonlocal index
                self._check_cancel()
                path = str(job_dir / f"{name}{FORMATS[fmt].extension}")
                from_file = isinstance(source, Path)
                result = write_stem(
                    None if from_file else source,
                    sr_in,
                    path,
                    target_sr=audio.samplerate,
                    fmt=fmt,
                    bit_depth=bit_depth,
                    ffmpeg_path=ffmpeg_path,
                    expected_frames=audio.source_frames,
                    temp_dir=temp_dir,
                    source_path=source if from_file else None,
                )
                written.append(path)
                results[name] = result
                warnings.extend(result.warnings)
                for message in result.warnings:
                    self._log(message, "warning")
                self._log(
                    f"Wrote {name}: {result.samplerate} Hz {result.subtype}, {result.frames} frames",
                    "info",
                )
                index += 1
                progress(STAGE_SEPARATE_END + (1.0 - STAGE_SEPARATE_END) * index / total, "saving")

            # Pass 1: model stems, resampled to the source rate on the way out.
            for name, source in sources.items():
                emit_output(name, source, model_sr)
                if spec.residual_outputs and not plan.enabled:
                    # Read back what landed on disk so the residual nulls
                    # against the delivered file, not against this array.
                    written_arrays[name] = read_stem(results[name].path, audio.source_frames)
            sources.clear()

            # Pass 2: residuals, already at the source rate (no resample). On
            # a chunked job these are streamed block by block so a 90 minute
            # stem never has to be held next to the source array.
            for name, residual_sources in spec.residual_outputs.items():
                if plan.enabled:
                    residual_path = subtract_to_file(
                        audio.data,
                        [results[s].path for s in residual_sources],
                        temp_dir / f"residual_{name}.f32.wav",
                        audio.samplerate,
                        audio.source_frames,
                    )
                    emit_output(name, residual_path, audio.samplerate)
                else:
                    array = subtract_from_source(audio.data, written_arrays, residual_sources)
                    emit_output(name, array, audio.samplerate)
                    del array
            del written_arrays

            return JobResult(
                outputs=results,
                output_dir=str(job_dir),
                source_samplerate=audio.samplerate,
                source_frames=audio.source_frames,
                source_channels=audio.channels_in,
                stats=stats,
                elapsed_seconds=time.perf_counter() - t_start,
                warnings=warnings,
                channels_out=int(audio.data.shape[1]),
            )

        except BaseException:
            # Cancelled or failed: leave nothing behind from this job.
            for path in written:
                try:
                    os.remove(path)
                except OSError:
                    pass
            try:
                if job_dir.is_dir() and not any(job_dir.iterdir()):
                    job_dir.rmdir()
            except OSError:
                pass
            raise
        finally:
            remove_temp_dir(temp_dir)
