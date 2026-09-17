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
    WriteResult,
    load_audio,
    make_temp_dir,
    remove_temp_dir,
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

ProgressCallback = Callable[[float], None]           # fraction 0..1
StageProgressCallback = Callable[[float, str], None]  # fraction 0..1, stage name
LogCallback = Callable[[str, str], None]              # message, level

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
    """Map raw model stems to the outputs a processing mode expects."""
    if mode not in MODES:
        raise ValueError(f"Unknown mode: {mode}")
    outputs: Dict[str, np.ndarray] = {}
    for out_name, sources in MODES[mode].items():
        missing = [s for s in sources if s not in stems]
        if missing:
            raise KeyError(f"Model did not produce stems {missing} needed for '{out_name}'")
        if len(sources) == 1:
            outputs[out_name] = stems[sources[0]]
        else:
            outputs[out_name] = mix_stems(stems, sources)
    return outputs


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

    def get_separator(self, model: str, shifts: int, overlap: float):
        """Return a cached demucs.api.Separator, loading it on first use."""
        if model not in MODEL_CONFIGS:
            raise ValueError(f"Unknown model: {model}")
        key = (model, int(shifts), float(overlap), self.device)
        separator = self._separators.get(key)
        loaded_now = False
        if separator is None:
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
    ) -> Tuple[Dict[str, np.ndarray], int, SeparationStats]:
        """
        Separate a float32 (frames, channels) array into stems.

        Parameters resolve from the preset, then explicit overrides.

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
            separator, loaded_now = self.get_separator(model, shifts, overlap)
            self._check_cancel()

            # (frames, ch) -> (ch, frames) as a contiguous COPY: separate_tensor
            # normalises its input in place and we must not touch the caller's
            # array.
            wav = torch.from_numpy(np.ascontiguousarray(np.asarray(data, dtype=np.float32).T))

            self._progress_ctx = {
                "shifts": shifts,
                "stride": self._segment_stride(separator, overlap),
                "last": 0.0,
                "on_progress": on_progress,
            }
            t0 = time.perf_counter()
            try:
                _, separated = separator.separate_tensor(wav, int(samplerate))
            finally:
                self._progress_ctx = None
            elapsed = time.perf_counter() - t0

            stems = {
                name: np.ascontiguousarray(tensor.detach().cpu().numpy().T, dtype=np.float32)
                for name, tensor in separated.items()
            }
            del separated, wav

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
    ) -> JobResult:
        """
        Load -> separate -> build mode outputs -> write sample-accurate stems.

        Output layout: <output_dir>/<input name>/<stem>.<ext>, every stem at
        the source sample rate with exactly the source frame count.

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
            audio: LoadedAudio = load_audio(input_path, ffmpeg_path, temp_dir)
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
            stems, model_sr, stats = self.separate(
                audio.data,
                audio.samplerate,
                preset=preset,
                model=model,
                shifts=shifts,
                overlap=overlap,
                on_progress=lambda f: progress(STAGE_LOAD_END + span * f, "separating"),
            )
            outputs_arrays = build_outputs(stems, mode)
            del stems

            job_dir.mkdir(parents=True, exist_ok=True)
            results: Dict[str, WriteResult] = {}
            total = len(outputs_arrays)
            for index, (name, array) in enumerate(outputs_arrays.items()):
                self._check_cancel()
                path = str(job_dir / f"{name}{FORMATS[fmt].extension}")
                result = write_stem(
                    array,
                    model_sr,
                    path,
                    target_sr=audio.samplerate,
                    fmt=fmt,
                    bit_depth=bit_depth,
                    ffmpeg_path=ffmpeg_path,
                    expected_frames=audio.source_frames,
                    temp_dir=temp_dir,
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
                progress(STAGE_SEPARATE_END + (1.0 - STAGE_SEPARATE_END) * (index + 1) / total, "saving")

            return JobResult(
                outputs=results,
                output_dir=str(job_dir),
                source_samplerate=audio.samplerate,
                source_frames=audio.source_frames,
                source_channels=audio.channels_in,
                stats=stats,
                elapsed_seconds=time.perf_counter() - t_start,
                warnings=warnings,
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
