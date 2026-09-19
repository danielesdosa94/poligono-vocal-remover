"""
Audio I/O for the separation engine.

Demucs never touches files: this module loads the source into a float32
numpy array, and writes the finished stems back out. soundfile handles plain
containers directly; anything it cannot open (mp4, mkv, m4a, wma...) is
decoded by ffmpeg into a temporary float32 WAV first.

Writing is a single final step per stem: float32 temp -> ONE ffmpeg call that
resamples (swr, wide filter), dithers only when the target is integer PCM
(TPDF at 24 bit, noise-shaped at 16 bit), pads/trims to the exact source
frame count, and encodes.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import soundfile as sf

from .presets import FORMATS, MP3_BITRATE, STREAM_BLOCK_FRAMES, resolve_bit_depth

CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
TEMP_ROOT_NAME = "poligono-ai-hub"

# A frame delta this large after the final encode means something went wrong
# upstream (wrong sample rate, truncated decode), not a rounding artefact.
FRAME_DELTA_WARN = 64

# swr resampler settings: 256-tap filter with full phase precision is
# transparent for 44.1k <-> 48k conversions (measured round trip < -110 dBFS).
RESAMPLE_OPTS = "filter_size=256:phase_shift=10"

# Dither choice, measured on this ffmpeg build (8.0.1):
# - Shibata noise shaping at 24 bit (s32 container) overflows in swresample
#   and clips the signal to full scale. Plain high-passed TPDF is correct at
#   24 bit, and noise shaping buys nothing at a -144 dBFS floor anyway.
# - At 16 bit Shibata is fine, but its filters only exist for 44.1k/48k.
DITHER_24_BIT = "triangular_hp"
DITHER_16_BIT_SHAPED = "shibata"
DITHER_16_BIT_PLAIN = "triangular_hp"
SHAPED_DITHER_RATES = (44100, 48000)


class AudioIOError(RuntimeError):
    """Raised when a file cannot be probed, decoded or encoded."""


@dataclass
class AudioInfo:
    """Header-level description of a media file's first audio stream."""

    samplerate: int
    channels: int
    frames: int
    subtype: str
    format: str
    frames_exact: bool = True  # False when estimated from ffprobe duration

    @property
    def duration(self) -> float:
        return self.frames / self.samplerate if self.samplerate else 0.0


@dataclass
class LoadedAudio:
    """Decoded source audio, float32, shape (frames, channels)."""

    data: np.ndarray
    samplerate: int
    source_frames: int
    channels_in: int
    warnings: List[str] = field(default_factory=list)


@dataclass
class WriteResult:
    """What actually landed on disk for one stem."""

    path: str
    samplerate: int
    subtype: str
    frames: int
    expected_frames: Optional[int]
    frame_delta: int
    warnings: List[str] = field(default_factory=list)


# =============================================================================
# Helpers
# =============================================================================

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _exe(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def find_ffmpeg(ffmpeg_path: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Locate ffmpeg and ffprobe.

    Order: explicit path -> bundled resources/bin/ffmpeg -> PATH.
    ffprobe is expected next to ffmpeg; falls back to PATH.

    Returns:
        (ffmpeg_path, ffprobe_path); either may be None if not found.
    """
    candidates: List[str] = []
    if ffmpeg_path:
        candidates.append(ffmpeg_path)
    candidates.append(str(_repo_root() / "resources" / "bin" / "ffmpeg" / _exe("ffmpeg")))
    on_path = shutil.which("ffmpeg")
    if on_path:
        candidates.append(on_path)

    ffmpeg = next((c for c in candidates if os.path.isfile(c)), None)

    ffprobe = None
    if ffmpeg:
        sibling = os.path.join(os.path.dirname(ffmpeg), _exe("ffprobe"))
        if os.path.isfile(sibling):
            ffprobe = sibling
    if not ffprobe:
        ffprobe = shutil.which("ffprobe")

    return ffmpeg, ffprobe


def _require_ffmpeg(ffmpeg_path: Optional[str]) -> str:
    ffmpeg, _ = find_ffmpeg(ffmpeg_path)
    if not ffmpeg:
        raise AudioIOError("ffmpeg not found (expected in resources/bin/ffmpeg or on PATH)")
    return ffmpeg


def _run(cmd: List[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    """Run an external tool with an argument list (never a shell string)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioIOError(f"{os.path.basename(cmd[0])} timed out after {timeout}s") from exc
    except OSError as exc:
        raise AudioIOError(f"Cannot execute {cmd[0]}: {exc}") from exc

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise AudioIOError(
            f"{os.path.basename(cmd[0])} failed (code {result.returncode}): {stderr[-400:]}"
        )
    return result


def make_temp_dir(job_id: Optional[str] = None) -> Path:
    """Create a private temp directory under <system temp>/poligono-ai-hub/<job>."""
    root = Path(tempfile.gettempdir()) / TEMP_ROOT_NAME
    path = root / (job_id or uuid.uuid4().hex)
    path.mkdir(parents=True, exist_ok=True)
    return path


def remove_temp_dir(path: Optional[Path]) -> None:
    """Best-effort removal of a temp directory created by make_temp_dir."""
    if path and Path(path).exists():
        shutil.rmtree(path, ignore_errors=True)


def _as_float32_2d(data: np.ndarray) -> np.ndarray:
    """Coerce to contiguous float32 (frames, channels)."""
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr[:, np.newaxis]
    if arr.ndim != 2:
        raise ValueError(f"Expected (frames, channels) array, got shape {arr.shape}")
    return np.ascontiguousarray(arr)


def fit_frames(data: np.ndarray, expected_frames: Optional[int]) -> Tuple[np.ndarray, int]:
    """
    Pad with silence or trim so the array has exactly expected_frames rows.

    Returns:
        (adjusted array, delta) where delta = original frames - expected.
    """
    if expected_frames is None:
        return data, 0
    delta = data.shape[0] - expected_frames
    if delta > 0:
        data = data[:expected_frames]
    elif delta < 0:
        pad = np.zeros((-delta, data.shape[1]), dtype=data.dtype)
        data = np.concatenate([data, pad], axis=0)
    return data, delta


def fold_to_channels(data: np.ndarray, channels: int) -> np.ndarray:
    """
    Reduce a stem to `channels` by averaging, or pass it through unchanged.

    Only used for mono delivery of a mono source: Demucs always works in
    stereo, so a mono file is expanded on the way in and its stems come back
    as two nearly identical channels. The MEAN is the correct fold here (a
    sum would be +6 dB); this is a channel downmix, not a stem combination,
    where the rule is always summation.
    """
    data = _as_float32_2d(data)
    if channels <= 0 or data.shape[1] == channels:
        return data
    if channels == 1:
        return np.ascontiguousarray(data.mean(axis=1, keepdims=True, dtype=np.float32))
    raise ValueError(f"Cannot fold {data.shape[1]} channels to {channels}")


class StemStreamWriter:
    """
    Append-only float32 WAV writer, for assembling a stem block by block.

    A 90 minute stem is 1.9 GB at the model rate; the chunked path writes it
    out as it goes instead of holding it. The file it produces is a plain
    float32 WAV that write_stem() can take as `source_path`.
    """

    def __init__(self, path: Path, samplerate: int, channels: int):
        self.path = Path(path)
        self.samplerate = int(samplerate)
        self.channels = int(channels)
        self.frames = 0
        self._file = sf.SoundFile(
            str(self.path),
            mode="w",
            samplerate=self.samplerate,
            channels=self.channels,
            subtype="FLOAT",
            format="WAV",
        )

    def append(self, block: np.ndarray) -> None:
        block = _as_float32_2d(block)
        if block.shape[1] != self.channels:
            raise ValueError(
                f"{self.path.name}: expected {self.channels} channels, got {block.shape[1]}"
            )
        if block.shape[0] == 0:
            return
        self._file.write(block)
        self.frames += block.shape[0]

    def close(self) -> None:
        if self._file is not None and not self._file.closed:
            self._file.flush()
            self._file.close()

    def __enter__(self) -> "StemStreamWriter":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def subtract_to_file(
    source: np.ndarray,
    stem_paths: List[str],
    out_path: Path,
    samplerate: int,
    expected_frames: int,
    block_frames: int = STREAM_BLOCK_FRAMES,
) -> Path:
    """
    Stream `source` minus the named stem files into a float32 WAV.

    The in-memory twin of this is subtract_from_source(); this version never
    holds a whole stem, which is what keeps a 90 minute job inside its RAM
    budget. Stems are read from disk (not from the arrays that produced them)
    so the residual nulls against the files actually delivered.

    Frames beyond the end of a stem file count as silence, and frames past
    `expected_frames` are ignored, matching read_stem(path, expected_frames).
    """
    source = _as_float32_2d(source)
    channels = source.shape[1]
    handles = [sf.SoundFile(path, mode="r") for path in stem_paths]
    try:
        with StemStreamWriter(out_path, samplerate, channels) as writer:
            offset = 0
            while offset < expected_frames:
                count = min(block_frames, expected_frames - offset)
                residual = np.array(source[offset:offset + count], dtype=np.float32, copy=True)
                if residual.shape[0] < count:
                    pad = np.zeros((count - residual.shape[0], channels), dtype=np.float32)
                    residual = np.concatenate([residual, pad], axis=0)
                for handle in handles:
                    block = handle.read(count, dtype="float32", always_2d=True)
                    if block.shape[0]:
                        residual[:block.shape[0]] -= block
                writer.append(residual)
                offset += count
    finally:
        for handle in handles:
            try:
                handle.close()
            except Exception:  # noqa: BLE001 - closing must never mask the real error
                pass
    return Path(out_path)


def read_stem(path: str, expected_frames: Optional[int] = None) -> np.ndarray:
    """
    Read a written stem back as float32 (frames, channels).

    Residual outputs are built from the bytes that actually landed on disk, so
    the delivered files null against each other rather than against an
    in-memory copy that was resampled or dithered on the way out.
    """
    data, _ = sf.read(path, dtype="float32", always_2d=True)
    data, _ = fit_frames(_as_float32_2d(data), expected_frames)
    return data


def expected_subtype(fmt: str, bit_depth: Optional[int]) -> str:
    """The soundfile subtype name a given format/bit depth should produce."""
    if fmt == "mp3":
        return "MPEG_LAYER_III"
    if bit_depth == 32:
        return "FLOAT"
    return f"PCM_{bit_depth}"


# =============================================================================
# Probe
# =============================================================================

def probe(path: str, ffmpeg_path: Optional[str] = None) -> AudioInfo:
    """
    Read sample rate, channels and length from a file header.

    soundfile first (exact frame count); ffprobe for containers it cannot
    open, where the frame count is estimated from the stream duration.
    """
    if not os.path.isfile(path):
        raise AudioIOError(f"File not found: {path}")

    try:
        info = sf.info(path)
        return AudioInfo(
            samplerate=int(info.samplerate),
            channels=int(info.channels),
            frames=int(info.frames),
            subtype=str(info.subtype),
            format=str(info.format),
            frames_exact=True,
        )
    except Exception:
        pass

    _, ffprobe = find_ffmpeg(ffmpeg_path)
    if not ffprobe:
        raise AudioIOError(f"Cannot read {os.path.basename(path)}: unsupported by soundfile and ffprobe not found")

    result = _run(
        [
            ffprobe,
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,channels,codec_name,sample_fmt,duration",
            "-show_entries", "format=duration,format_name",
            "-of", "json",
            path,
        ],
        timeout=60,
    )
    try:
        payload = json.loads(result.stdout.decode("utf-8", errors="replace"))
        stream = payload["streams"][0]
        fmt = payload.get("format", {})
    except (ValueError, KeyError, IndexError) as exc:
        raise AudioIOError(f"No audio stream found in {os.path.basename(path)}") from exc

    samplerate = int(stream["sample_rate"])
    duration = float(stream.get("duration") or fmt.get("duration") or 0.0)
    return AudioInfo(
        samplerate=samplerate,
        channels=int(stream.get("channels", 0)),
        frames=int(round(duration * samplerate)),
        subtype=str(stream.get("sample_fmt") or stream.get("codec_name") or "unknown"),
        format=str(fmt.get("format_name", "unknown")),
        frames_exact=False,
    )


# =============================================================================
# Load
# =============================================================================

def _decode_with_ffmpeg(
    path: str,
    ffmpeg_path: Optional[str],
    temp_dir: Optional[Path],
    channels: Optional[int] = None,
) -> Tuple[np.ndarray, int]:
    """Decode the first audio stream to a float32 WAV and read it back."""
    ffmpeg = _require_ffmpeg(ffmpeg_path)
    own_temp = temp_dir is None
    temp_dir = temp_dir or make_temp_dir()
    temp_wav = temp_dir / f"decode_{uuid.uuid4().hex}.wav"

    # No "-ar": the source rate must survive untouched so the stems can be
    # written back at exactly that rate.
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", path, "-vn", "-map", "0:a:0"]
    if channels:
        cmd += ["-ac", str(channels)]
    cmd += ["-c:a", "pcm_f32le", "-f", "wav", "-y", str(temp_wav)]

    try:
        _run(cmd)
        data, samplerate = sf.read(str(temp_wav), dtype="float32", always_2d=True)
    finally:
        try:
            temp_wav.unlink(missing_ok=True)
        except OSError:
            pass
        if own_temp:
            remove_temp_dir(temp_dir)
    return data, int(samplerate)


def load_audio(
    path: str,
    ffmpeg_path: Optional[str] = None,
    temp_dir: Optional[Path] = None,
    mono_output: bool = False,
) -> LoadedAudio:
    """
    Load a media file as float32 stereo (frames, 2) at its native sample rate.

    - Mono is duplicated to both channels (with a warning), unless
      `mono_output` is set, in which case it stays single-channel and the
      stems are folded back to mono on the way out.
    - More than 2 channels is downmixed to stereo by ffmpeg (with a warning).
    """
    if not os.path.isfile(path):
        raise AudioIOError(f"File not found: {path}")

    warnings: List[str] = []
    try:
        data, samplerate = sf.read(path, dtype="float32", always_2d=True)
        samplerate = int(samplerate)
    except Exception:
        data, samplerate = _decode_with_ffmpeg(path, ffmpeg_path, temp_dir)

    channels_in = int(data.shape[1])
    if channels_in == 1 and mono_output:
        # Kept at one channel: Demucs expands it internally and the stems are
        # folded back with fold_to_channels() before they are written.
        warnings.append("Mono source: stems will be delivered as mono")
    elif channels_in == 1:
        data = np.repeat(data, 2, axis=1)
        warnings.append("Mono source: duplicated to both channels, output stems will be dual-mono")
    elif channels_in > 2:
        data, samplerate = _decode_with_ffmpeg(path, ffmpeg_path, temp_dir, channels=2)
        warnings.append(
            f"{channels_in}-channel source downmixed to stereo; "
            "for dialogue, extract the center channel first"
        )

    data = _as_float32_2d(data)
    return LoadedAudio(
        data=data,
        samplerate=samplerate,
        source_frames=int(data.shape[0]),
        channels_in=channels_in,
        warnings=warnings,
    )


# =============================================================================
# Write
# =============================================================================

def _build_filter(
    target_sr: int,
    fmt: str,
    bit_depth: Optional[int],
    expected_frames: Optional[int],
    sr_in: Optional[int] = None,
) -> str:
    """
    Compose the ffmpeg -af chain: resample [+ dither] -> pad -> trim.

    Dither is only added for integer PCM targets, and the output sample
    format is pinned (osf) so the dither is applied inside aresample at the
    final word length rather than by an implicit conversion later on.

    When the rate does not change and the target is float32 there is nothing
    for the resampler to do, so it is left out and the samples pass through
    bit for bit. That is the path a chunk-assembled stem takes when the
    source already runs at the model rate.
    """
    parts: List[str] = []
    passthrough = fmt == "wav" and bit_depth == 32 and sr_in is not None and sr_in == target_sr
    if not passthrough:
        resample = f"aresample={target_sr}:{RESAMPLE_OPTS}"
        if fmt in ("wav", "flac") and bit_depth == 16:
            method = DITHER_16_BIT_SHAPED if target_sr in SHAPED_DITHER_RATES else DITHER_16_BIT_PLAIN
            resample += f":dither_method={method}:osf=s16"
        elif fmt in ("wav", "flac") and bit_depth == 24:
            resample += f":dither_method={DITHER_24_BIT}:osf=s32"
        parts.append(resample)

    if expected_frames is not None:
        parts.append(f"apad=whole_len={expected_frames}")
        parts.append(f"atrim=end_sample={expected_frames}")
    return ",".join(parts) if parts else "anull"


def _codec_args(fmt: str, bit_depth: Optional[int]) -> List[str]:
    if fmt == "wav":
        return ["-c:a", {32: "pcm_f32le", 24: "pcm_s24le", 16: "pcm_s16le"}[bit_depth]]
    if fmt == "flac":
        return ["-sample_fmt", "s32" if bit_depth == 24 else "s16", "-c:a", "flac"]
    if fmt == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", MP3_BITRATE]
    raise ValueError(f"Unsupported format: {fmt}")


def write_stem(
    data: Optional[np.ndarray],
    sr_in: int,
    path: str,
    target_sr: Optional[int] = None,
    fmt: str = "wav",
    bit_depth: Optional[int] = None,
    ffmpeg_path: Optional[str] = None,
    expected_frames: Optional[int] = None,
    temp_dir: Optional[Path] = None,
    source_path: Optional[Path] = None,
) -> WriteResult:
    """
    Write one stem to disk, sample-accurate against the source.

    Args:
        data: float32 (frames, channels) at sr_in. None when `source_path`
            is given instead.
        sr_in: Sample rate of the input (the model's rate, 44100 for Demucs v4).
        path: Destination file (extension should match fmt).
        target_sr: Desired output rate; defaults to sr_in.
        fmt: "wav", "flac" or "mp3".
        bit_depth: 16/24/32 for wav, 16/24 for flac; None = format default.
        ffmpeg_path: Explicit ffmpeg binary (optional).
        expected_frames: Frame count the output must have at target_sr.
        temp_dir: Where to put the intermediate float32 WAV (optional).
        source_path: A float32 WAV at sr_in to encode from, instead of `data`.
            The chunked path assembles stems straight to such a file, so this
            skips holding the whole stem in memory and skips a copy.

    Fast path: float32 WAV at the same rate is written directly by soundfile.
    Everything else goes through a single ffmpeg call.
    """
    fmt = fmt.lower()
    if fmt not in FORMATS:
        raise ValueError(f"Unsupported format: {fmt}")
    if (data is None) == (source_path is None):
        raise ValueError("write_stem needs exactly one of `data` or `source_path`")
    spec = FORMATS[fmt]
    bit_depth = resolve_bit_depth(fmt, bit_depth)
    target_sr = int(target_sr or sr_in)
    warnings: List[str] = []

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)

    if data is not None and fmt == "wav" and bit_depth == 32 and target_sr == sr_in:
        data = _as_float32_2d(data)
        data, _ = fit_frames(data, expected_frames)
        sf.write(path, data, sr_in, subtype="FLOAT")
    else:
        ffmpeg = _require_ffmpeg(ffmpeg_path)
        own_temp = temp_dir is None
        temp_dir = temp_dir or make_temp_dir()
        temp_in: Optional[Path] = None
        # Encode next to the destination, then atomically swap in, so a
        # failed encode never leaves a half-written stem behind.
        temp_out = f"{path}.part{spec.extension}"
        try:
            if source_path is not None:
                ffmpeg_input = str(source_path)
            else:
                temp_in = temp_dir / f"stem_{uuid.uuid4().hex}.f32.wav"
                sf.write(str(temp_in), _as_float32_2d(data), sr_in, subtype="FLOAT")
                ffmpeg_input = str(temp_in)
            cmd = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-i", ffmpeg_input,
                "-af", _build_filter(target_sr, fmt, bit_depth, expected_frames, sr_in),
                *_codec_args(fmt, bit_depth),
                "-y", temp_out,
            ]
            _run(cmd)
            os.replace(temp_out, path)
        finally:
            leftovers = [temp_out] + ([str(temp_in)] if temp_in else [])
            for leftover in leftovers:
                try:
                    os.remove(leftover)
                except OSError:
                    pass
            if own_temp:
                remove_temp_dir(temp_dir)

    info = sf.info(path)
    frames = int(info.frames)
    delta = frames - expected_frames if expected_frames is not None else 0

    if expected_frames is not None and delta != 0:
        if not spec.sample_accurate:
            warnings.append(
                f"{os.path.basename(path)}: {fmt} encoder padding, {delta:+d} frames vs source (expected for lossy)"
            )
        elif abs(delta) > FRAME_DELTA_WARN:
            warnings.append(
                f"{os.path.basename(path)}: {frames} frames vs {expected_frames} expected ({delta:+d})"
            )
        else:
            warnings.append(f"{os.path.basename(path)}: frame delta {delta:+d} after encode")

    return WriteResult(
        path=path,
        samplerate=int(info.samplerate),
        subtype=str(info.subtype),
        frames=frames,
        expected_frames=expected_frames,
        frame_delta=delta,
        warnings=warnings,
    )
