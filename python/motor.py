#!/usr/bin/env python3
"""
Polígono Vocal Remover - Motor de IA
====================================
Main processing engine for vocal separation.

Communication Protocol:
- All output is JSON, one object per line via stdout
- Electron reads stdout line-by-line and parses JSON
- stderr is reserved for critical errors only

Usage:
    python motor.py <input_path> <output_dir> [options]
    
Options:
    --model <name>       Model to use (default: htdemucs_ft)
    --device <cpu|cuda>  Processing device (default: auto-detect)
    --quality <fast|hq>  Quality preset (default: hq)
    --shifts <n>         Number of random shifts for prediction (default: 2)
    --output-format <wav|mp3|flac>  Output format (default: wav)

Exit Codes:
    0 - Success
    1 - General error
    2 - File not found
    3 - Invalid arguments
    4 - Model loading error
    5 - Processing error
    6 - Cancelled by user
"""

import sys
import os
import io
import argparse
import shutil
import multiprocessing
import time
import subprocess
import re
from pathlib import Path
from typing import Optional, Dict, Tuple

# Force UTF-8 encoding for stdout/stderr
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.protocol import protocol, ProcessingStep
from utils.signal_handler import signal_handler, CancellationToken, CancelledException


# =============================================================================
# DEMUCS PROGRESS INTERCEPTOR (for compiled .exe mode)
# =============================================================================

class DemucsProgressInterceptor:
    """
    File-like object that intercepts stderr to capture Demucs/tqdm progress.

    When running as a compiled .exe, we call demucs API directly (no subprocess),
    so tqdm progress goes to stderr in-process. This interceptor:
    1. Passes all output to the original stderr (for debug logs)
    2. Parses percentage patterns and emits JSON progress to Electron via protocol

    Demucs 0-100% is scaled to 0-90% of the app bar (last 10% = saving files).
    """

    _PROGRESS_RE = re.compile(r'(\d{1,3})%')

    # Mapping: Demucs 0% → app 5%, Demucs 100% → app 90%
    _FLOOR = 5
    _CEILING = 90

    def __init__(self, original_stderr, protocol_ref):
        self._original = original_stderr
        self._protocol = protocol_ref
        self._last_emitted = self._FLOOR
        self._buffer = ""

    def write(self, text):
        # Always pass through to original stderr
        if self._original:
            try:
                self._original.write(text)
            except Exception:
                pass

        # Buffer text and process complete lines
        self._buffer += text
        while '\n' in self._buffer or '\r' in self._buffer:
            for sep in ['\n', '\r']:
                if sep in self._buffer:
                    line, self._buffer = self._buffer.split(sep, 1)
                    self._parse_line(line)
                    break

        # Also check the buffer itself for progress (tqdm uses \r without \n)
        if self._buffer:
            self._parse_line(self._buffer)

    def _parse_line(self, line):
        match = self._PROGRESS_RE.search(line)
        if match:
            demucs_pct = int(match.group(1))
            # Interpolate: 0% → 5%, 100% → 90%
            visual_pct = self._FLOOR + int(demucs_pct * (self._CEILING - self._FLOOR) / 100)
            # Monotonic: never go below the last value emitted
            if visual_pct > self._last_emitted:
                self._last_emitted = visual_pct
                self._protocol.emit_progress(
                    visual_pct,
                    detail=f"Processing: {demucs_pct}%"
                )

    def flush(self):
        if self._original:
            try:
                self._original.flush()
            except Exception:
                pass

    def fileno(self):
        if self._original:
            return self._original.fileno()
        raise io.UnsupportedOperation("fileno")

    # Needed so tqdm and other libs treat this as a valid stream
    def isatty(self):
        return False

    @property
    def encoding(self):
        return getattr(self._original, 'encoding', 'utf-8')


# =============================================================================
# CONFIGURATION
# =============================================================================

# Model configurations
MODEL_CONFIGS = {
    "htdemucs_ft": {
        "name": "htdemucs_ft",
        "description": "Demucs v4 Fine-tuned (Best Quality)",
        "stems": ["vocals", "drums", "bass", "other"],
        "default_shifts": 2,
    },
    "htdemucs": {
        "name": "htdemucs", 
        "description": "Demucs v4 Standard",
        "stems": ["vocals", "drums", "bass", "other"],
        "default_shifts": 1,
    },
    "mdx_extra": {
        "name": "mdx_extra",
        "description": "MDX-Net Extra (Fast)",
        "stems": ["vocals", "other"],
        "default_shifts": 0,
    }
}

# Quality presets
QUALITY_PRESETS = {
    "fast": {
        "shifts": 1,
        "overlap": 0.25,
        "segment": None,  # Use default
    },
    "hq": {
        "shifts": 2,
        "overlap": 0.5,
        "segment": None,
    },
    "ultra": {
        "shifts": 5,
        "overlap": 0.75,
        "segment": None,
    }
}

# Supported file extensions
AUDIO_EXTENSIONS = {'.mp3', '.wav', '.flac', '.m4a', '.ogg', '.wma', '.aac'}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.wmv', '.flv'}

# Demucs v4 models were trained at 44.1kHz and always emit stems at that rate,
# whatever the input was. I compare against this to decide whether the finished
# stems need converting back to the source rate.
DEMUCS_NATIVE_SAMPLE_RATE = 44100


# =============================================================================
# ARGUMENT PARSING
# =============================================================================

def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Polígono Vocal Remover - AI Audio Separation Engine"
    )
    
    parser.add_argument(
        "input_path",
        help="Path to input audio or video file"
    )
    
    parser.add_argument(
        "output_dir",
        help="Directory for output files"
    )
    
    parser.add_argument(
        "--model",
        choices=list(MODEL_CONFIGS.keys()),
        default="htdemucs_ft",
        help="Model to use for separation"
    )
    
    parser.add_argument(
        "--device",
        choices=["cpu", "cuda", "auto"],
        default="auto",
        help="Processing device"
    )
    
    parser.add_argument(
        "--quality",
        choices=list(QUALITY_PRESETS.keys()),
        default="hq",
        help="Quality preset"
    )
    
    parser.add_argument(
        "--shifts",
        type=int,
        default=None,
        help="Override number of random shifts"
    )
    
    parser.add_argument(
        "--output-format",
        choices=["wav", "mp3", "flac"],
        default="wav",
        help="Output audio format"
    )

    parser.add_argument(
        "--mode",
        choices=["vocal_remover", "splitter"],
        default="vocal_remover",
        help="Processing mode: vocal_remover (2 tracks) or splitter (4 tracks)"
    )

    parser.add_argument(
        "--ffmpeg-path",
        default=None,
        help="Path to FFmpeg binary (for video processing)"
    )

    return parser.parse_args()


def validate_arguments(args: argparse.Namespace) -> Tuple[bool, Optional[str]]:
    """
    Validate command line arguments.
    
    Returns:
        Tuple of (is_valid, error_message)
    """
    # Check input file exists
    if not os.path.exists(args.input_path):
        return False, f"Input file not found: {args.input_path}"
    
    # Check file extension is supported
    ext = Path(args.input_path).suffix.lower()
    if ext not in AUDIO_EXTENSIONS and ext not in VIDEO_EXTENSIONS:
        return False, f"Unsupported file format: {ext}"
    
    # Check output directory can be created
    try:
        os.makedirs(args.output_dir, exist_ok=True)
    except OSError as e:
        return False, f"Cannot create output directory: {e}"
    
    return True, None


# =============================================================================
# DEVICE DETECTION
# =============================================================================

def detect_device(requested: str = "auto") -> str:
    """
    Detect the best available device for processing.
    
    Args:
        requested: "cpu", "cuda", or "auto"
    
    Returns:
        "cpu" or "cuda"
    """
    if requested == "cpu":
        return "cpu"
    
    if requested == "cuda" or requested == "auto":
        try:
            import torch
            if torch.cuda.is_available():
                # Log GPU info
                gpu_name = torch.cuda.get_device_name(0)
                vram = torch.cuda.get_device_properties(0).total_memory / (1024**3)
                protocol.emit_log(f"GPU detected: {gpu_name} ({vram:.1f} GB VRAM)")
                return "cuda"
        except ImportError:
            pass
        except Exception as e:
            protocol.emit_warning(f"CUDA detection failed: {e}", code="CUDA_ERROR")
    
    return "cpu"


# =============================================================================
# VIDEO PROCESSING (FFmpeg)
# =============================================================================

def extract_audio_from_video(
    video_path: str, 
    output_path: str,
    ffmpeg_path: Optional[str] = None,
    cancellation_token: Optional[CancellationToken] = None
) -> bool:
    """
    Extract audio track from video file using FFmpeg.
    
    Args:
        video_path: Path to input video
        output_path: Path for extracted audio (wav)
        ffmpeg_path: Optional path to FFmpeg binary
        cancellation_token: Token to check for cancellation
    
    Returns:
        True if successful, False otherwise
    """
    import subprocess
    
    # Find FFmpeg
    ffmpeg = ffmpeg_path or shutil.which("ffmpeg")
    if not ffmpeg:
        # Try common locations
        common_paths = [
            os.path.join(os.path.dirname(__file__), "..", "resources", "bin", "ffmpeg", "ffmpeg.exe"),
            os.path.join(os.path.dirname(__file__), "ffmpeg", "ffmpeg.exe"),
        ]
        for path in common_paths:
            if os.path.exists(path):
                ffmpeg = path
                break
    
    if not ffmpeg:
        protocol.emit_error("FFmpeg not found", code="FFMPEG_NOT_FOUND")
        return False
    
    protocol.emit_log(f"Using FFmpeg: {ffmpeg}")
    
    # Build command.
    # I deliberately DON'T pass "-ar" here: forcing a sample rate would resample
    # the source before Demucs even sees it (a 48kHz film would silently become
    # 44.1kHz). FFmpeg keeps the original rate when the flag is absent, so the
    # true source rate survives all the way to the final resample step.
    cmd = [
        ffmpeg,
        "-i", video_path,
        "-vn",              # No video
        # 32-bit float: the whole chain stays float from here on. Extracting
        # to 16 bit would quantize the audio before Demucs even sees it.
        "-acodec", "pcm_f32le",
        "-ac", "2",         # Stereo
        "-y",               # Overwrite output
        output_path
    ]
    
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        
        # Wait for completion, checking for cancellation
        while process.poll() is None:
            if cancellation_token and cancellation_token.is_cancelled:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                return False
            time.sleep(0.1)
        
        if process.returncode != 0:
            stderr = process.stderr.read().decode('utf-8', errors='replace')
            protocol.emit_error(f"FFmpeg failed: {stderr[:200]}", code="FFMPEG_ERROR")
            return False
        
        return True
        
    except Exception as e:
        protocol.emit_error(f"FFmpeg execution error: {e}", code="FFMPEG_EXEC_ERROR")
        return False


# =============================================================================
# AUDIO SEPARATION (Main Processing)
# =============================================================================

def mix_instrumental_track(output_dir: str, output_format: str, stems: list) -> bool:
    """
    Mix bass, drums, and other stems into a single instrumental track.

    Uses chunked streaming to avoid loading entire files into RAM.
    Memory usage stays constant (~300KB) regardless of file length.

    Args:
        output_dir: Directory containing separated stems
        output_format: Output format (wav, mp3, flac)
        stems: List of all stems from the model

    Returns:
        True if successful, False otherwise
    """
    BLOCKSIZE = 131072  # 128K frames per chunk (~3s at 44.1kHz stereo)

    try:
        import soundfile as sf

        ext = f".{output_format}" if output_format != "wav" else ".wav"
        stems_to_mix = ["bass", "drums", "other"]

        # Collect existing stem paths
        stem_paths = []
        for stem_name in stems_to_mix:
            stem_path = os.path.join(output_dir, f"{stem_name}{ext}")
            if os.path.exists(stem_path):
                stem_paths.append(stem_path)
            else:
                protocol.emit_log(f"Stem not found: {stem_name}{ext}", level="debug")

        if not stem_paths:
            protocol.emit_error("No stems found to mix", code="NO_STEMS")
            return False

        # Read metadata from first file to get sample rate, channels, total frames
        info = sf.info(stem_paths[0])
        sample_rate = info.samplerate
        total_frames = info.frames
        num_stems = len(stem_paths)

        protocol.emit_log(
            f"Mixing {num_stems} stems: {total_frames} frames @ {sample_rate}Hz",
            level="debug"
        )

        # Determine output format params
        instrumental_path = os.path.join(output_dir, f"instrumental{ext}")
        write_kwargs = {"samplerate": sample_rate}
        if output_format == "mp3":
            write_kwargs["format"] = "MP3"
        elif output_format == "flac":
            write_kwargs["format"] = "FLAC"
            # FLAC has no float subtype; 24-bit integer is its ceiling.
            write_kwargs["subtype"] = "PCM_24"
        else:
            # Without an explicit subtype soundfile writes PCM_16 for .wav,
            # re-quantizing the float32 stems we just asked Demucs for.
            write_kwargs["subtype"] = "FLOAT"

        # Open all source files + destination, stream in chunks
        readers = []
        writer = None
        try:
            for sp in stem_paths:
                readers.append(sf.SoundFile(sp, mode='r'))

            writer = sf.SoundFile(
                instrumental_path,
                mode='w',
                channels=readers[0].channels,
                **write_kwargs
            )

            frames_written = 0
            last_progress_pct = 90  # We map mixing progress to 90-99%

            while True:
                blocks = []
                min_read = BLOCKSIZE

                for reader in readers:
                    block = reader.read(BLOCKSIZE, dtype='float32')
                    if len(block) == 0:
                        min_read = 0
                        break
                    min_read = min(min_read, len(block))
                    blocks.append(block)

                if min_read == 0 or not blocks:
                    break

                # Trim all blocks to the same length and SUM them. Demucs
                # stems are additive (bass+drums+other+vocals ~= the mix), so
                # the instrumental is their sum. Averaging divides by 3, which
                # drops it ~9.5 dB and breaks the null test against the source.
                # The sum can exceed 1.0 on a hot mix; float32 WAV holds that
                # without clipping.
                trimmed = [b[:min_read] for b in blocks]
                mixed_chunk = trimmed[0]
                for b in trimmed[1:]:
                    mixed_chunk = mixed_chunk + b

                writer.write(mixed_chunk)
                frames_written += min_read

                # Emit progress: map 0-100% of mixing to app 90-99%
                if total_frames > 0:
                    mix_pct = frames_written / total_frames
                    visual_pct = 90 + int(mix_pct * 9)  # 90 → 99
                    if visual_pct > last_progress_pct:
                        last_progress_pct = visual_pct
                        protocol.emit_progress(
                            visual_pct,
                            detail=f"Mixing instrumental: {int(mix_pct * 100)}%"
                        )

        finally:
            for r in readers:
                r.close()
            if writer is not None:
                writer.close()

        protocol.emit_log(f"Instrumental track saved: {instrumental_path}", level="debug")

        # Delete original stems that were mixed
        for stem_path in stem_paths:
            try:
                os.remove(stem_path)
                protocol.emit_log(f"Removed {os.path.basename(stem_path)}", level="debug")
            except Exception as e:
                protocol.emit_log(f"Failed to remove {os.path.basename(stem_path)}: {e}", level="debug")

        return True

    except ImportError:
        protocol.emit_error("soundfile library not found", code="IMPORT_ERROR")
        return False
    except Exception as e:
        protocol.emit_error(f"Failed to mix instrumental: {str(e)}", code="MIX_ERROR")
        return False


def probe_sample_rate(
    media_path: str,
    ffmpeg_path: Optional[str] = None
) -> Optional[int]:
    """
    Read the sample rate of a media file without decoding the whole thing.

    Tries soundfile first (instant, works for wav/flac/ogg). Falls back to
    ffprobe for anything soundfile can't open (mp4, mov, mkv, m4a, wma...).

    Args:
        media_path: Path to the audio or video file
        ffmpeg_path: Path to ffmpeg.exe, used to locate ffprobe.exe beside it

    Returns:
        Sample rate in Hz, or None if it couldn't be determined
    """
    # Fast path: soundfile reads the header of plain audio containers directly
    try:
        import soundfile as sf
        return int(sf.info(media_path).samplerate)
    except Exception:
        pass

    # Fallback: ask ffprobe, which handles video containers and exotic codecs
    import subprocess

    ffprobe = None
    if ffmpeg_path:
        # ffprobe.exe ships next to ffmpeg.exe in our bundled resources
        candidate = os.path.join(os.path.dirname(ffmpeg_path), "ffprobe.exe")
        if os.path.exists(candidate):
            ffprobe = candidate
    if not ffprobe:
        ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None

    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=sample_rate",
                "-of", "default=noprint_wrappers=1:nokey=1",
                media_path
            ],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        value = result.stdout.strip().splitlines()
        if value and value[0].isdigit():
            return int(value[0])
    except Exception:
        pass

    return None


def probe_frame_count(media_path: str) -> Optional[int]:
    """
    Read the frame count of an audio file from its header.

    Returns:
        Number of frames, or None if soundfile cannot open the file
    """
    try:
        import soundfile as sf
        return int(sf.info(media_path).frames)
    except Exception:
        return None


def resample_outputs_to_rate(
    output_paths: Dict[str, str],
    target_rate: int,
    ffmpeg_path: Optional[str] = None,
    output_format: str = "wav",
    expected_frames: Optional[int] = None
) -> None:
    """
    Resample every finished stem to the source file's sample rate.

    Demucs v4 models are trained at 44.1kHz, so no matter what I feed them the
    stems always come out at 44.1kHz. For post-production work I want the stems
    to drop straight into a 48kHz session without the DAW doing its own silent
    conversion, so I convert them back here.

    This does NOT recover detail that Demucs discarded - the audio is still
    band-limited to what 44.1kHz carried. It's a workflow convenience so the
    files match the timeline they're going into.

    Each stem is converted to a temp file and then swapped in, so a failure on
    one stem leaves the original 44.1kHz file intact rather than a corrupt one.

    Args:
        output_paths: Mapping of stem name -> file path (modified in place)
        target_rate: Desired sample rate in Hz
        ffmpeg_path: Path to ffmpeg binary
        output_format: "wav", "flac" or "mp3", used to pick the encoder
        expected_frames: Frame count of the source, for a sample-accuracy check
    """
    import subprocess

    ffmpeg = ffmpeg_path or shutil.which("ffmpeg")
    if not ffmpeg:
        protocol.emit_warning(
            "FFmpeg not found, keeping stems at Demucs native 44.1kHz",
            code="RESAMPLE_NO_FFMPEG"
        )
        return

    # Pin the codec explicitly. Left to itself ffmpeg picks the container
    # default (pcm_s16le for wav, 16-bit flac, 128k mp3), which would undo the
    # float32 / 24-bit stems we just produced.
    if output_format == "flac":
        codec_args = ["-sample_fmt", "s32", "-c:a", "flac"]
    elif output_format == "mp3":
        codec_args = ["-c:a", "libmp3lame", "-b:a", "320k"]
    else:
        codec_args = ["-c:a", "pcm_f32le"]

    for stem_name, stem_path in list(output_paths.items()):
        if not stem_path or not os.path.exists(stem_path):
            continue

        # Skip files that already sit at the target rate
        current_rate = probe_sample_rate(stem_path, ffmpeg_path)
        if current_rate == target_rate:
            continue

        base, ext = os.path.splitext(stem_path)
        temp_path = f"{base}.resample_tmp{ext}"

        # Use the built-in swr resampler rather than soxr: the bundled
        # gyan.dev "essentials" FFmpeg is compiled without soxr and errors out
        # with "Requested resampling engine is unavailable". swr with a wide
        # filter and full precision is transparent for this conversion.
        cmd = [
            ffmpeg,
            "-i", stem_path,
            "-af", f"aresample={target_rate}:filter_size=256:phase_shift=10",
            *codec_args,
            "-y",
            temp_path
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=600,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )

            if result.returncode == 0 and os.path.exists(temp_path):
                os.replace(temp_path, stem_path)
                protocol.emit_log(
                    f"Resampled {stem_name}: {current_rate}Hz -> {target_rate}Hz",
                    level="debug"
                )
                # Sample-accurate check: the stems must line up frame for
                # frame with the source, or they drift against picture in the
                # DAW. Skipped for mp3, whose encoder always pads the tail.
                if expected_frames and output_format != "mp3":
                    new_frames = probe_frame_count(stem_path)
                    if new_frames is not None and new_frames != expected_frames:
                        protocol.emit_warning(
                            f"{stem_name}: {new_frames} frames vs "
                            f"{expected_frames} in source "
                            f"(delta {new_frames - expected_frames})",
                            code="FRAME_COUNT_MISMATCH"
                        )
            else:
                stderr = result.stderr.decode("utf-8", errors="replace")[:200]
                protocol.emit_warning(
                    f"Could not resample {stem_name}, keeping 44.1kHz: {stderr}",
                    code="RESAMPLE_FAILED"
                )
                if os.path.exists(temp_path):
                    os.remove(temp_path)

        except Exception as e:
            protocol.emit_warning(
                f"Resample error on {stem_name}, keeping 44.1kHz: {e}",
                code="RESAMPLE_ERROR"
            )
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass


def separate_audio(
    input_path: str,
    output_dir: str,
    model_name: str,
    device: str,
    quality_preset: str,
    shifts_override: Optional[int],
    output_format: str,
    processing_mode: str,
    cancellation_token: CancellationToken,
    output_name: Optional[str] = None
) -> Optional[Dict[str, str]]:
    """
    Perform audio separation using Demucs.

    This is the core processing function that:
    1. Loads the model
    2. Processes the audio
    3. Saves the separated stems
    4. Optionally mixes stems based on processing mode

    Args:
        input_path: Path to audio file
        output_dir: Directory for outputs
        model_name: Model identifier
        device: "cpu" or "cuda"
        quality_preset: "fast", "hq", or "ultra"
        shifts_override: Override for number of shifts
        output_format: "wav", "mp3", or "flac"
        processing_mode: "vocal_remover" (2 tracks) or "splitter" (4 tracks)
        cancellation_token: For checking cancellation
        output_name: Name to use for the output folder. For video inputs this
            is the original video's name, because input_path points at a temp
            wav and would otherwise give "separated_.temp_audio_<name>"

    Returns:
        Dict mapping stem names to file paths, or None on failure
    """
    # Get configuration
    model_config = MODEL_CONFIGS.get(model_name)
    quality_config = QUALITY_PRESETS.get(quality_preset)

    if not model_config or not quality_config:
        protocol.emit_error("Invalid model or quality configuration", code="CONFIG_ERROR")
        return None

    # Determine shifts
    shifts = shifts_override if shifts_override is not None else quality_config["shifts"]

    # Create temp working directory
    input_filename = Path(input_path).stem
    temp_dir = os.path.join(output_dir, f".temp_{input_filename}_{int(time.time())}")
    os.makedirs(temp_dir, exist_ok=True)

    # Register cleanup
    def cleanup_temp():
        if os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass

    signal_handler.register_cleanup(cleanup_temp)

    try:
        # Step: Loading Model
        protocol.emit_step_change(ProcessingStep.LOADING_MODEL, step_number=2)
        protocol.emit_progress(0, detail="Loading neural network weights...")

        # Check cancellation
        if cancellation_token.is_cancelled:
            raise CancelledException()

        # Build Demucs-specific arguments (without python executable prefix)
        demucs_cli_args = [
            "-n", model_name,
            "-o", temp_dir,
            "-j", "0",  # Workers = 0 to avoid subprocess issues in frozen apps
            "--shifts", str(shifts),
            "--overlap", str(quality_config["overlap"]),
        ]

        # Add device flag
        if device == "cpu":
            demucs_cli_args.extend(["-d", "cpu"])

        # Output format and bit depth. Demucs defaults to int16 WAV, which
        # quantizes every stem before we mix them. --float32 keeps the wav
        # path in 32-bit float; --int24 lifts the flac path from 16 to 24 bit.
        # Note: --clip-mode has no "none" option, so Demucs still rescales a
        # stem peaking above ~0.99. Phase 1 (own I/O) removes that for good.
        if output_format == "mp3":
            demucs_cli_args.extend(["--mp3"])
        elif output_format == "flac":
            demucs_cli_args.extend(["--flac", "--int24"])
        else:
            demucs_cli_args.append("--float32")

        # Add input file
        demucs_cli_args.append(input_path)

        protocol.emit_progress(50, detail="Model configuration ready")

        # Step: Analyzing
        protocol.emit_step_change(ProcessingStep.ANALYZING, step_number=3)
        protocol.emit_progress(0, detail="Analyzing audio structure...")

        # Check cancellation
        if cancellation_token.is_cancelled:
            raise CancelledException()

        # Step: Separating (Main processing)
        protocol.emit_step_change(ProcessingStep.SEPARATING, step_number=4)
        protocol.emit_progress(0, detail=f"Separating with {shifts} shift(s)...")

        is_frozen = getattr(sys, 'frozen', False)

        if is_frozen:
            # ============================================================
            # PRODUCTION (compiled .exe):
            # sys.executable points to motor.exe, NOT python.exe,
            # so we CANNOT use subprocess. Call demucs API directly.
            # We intercept stderr to capture tqdm progress from Demucs.
            # ============================================================
            original_stderr = sys.stderr
            interceptor = DemucsProgressInterceptor(original_stderr, protocol)
            try:
                sys.stderr = interceptor
                from demucs.separate import main as demucs_main
                protocol.emit_progress(5, detail="Running Demucs engine...")
                demucs_main(demucs_cli_args)
            except SystemExit as e:
                if e.code != 0:
                    protocol.emit_error(
                        f"Demucs failed with exit code {e.code}",
                        code="DEMUCS_ERROR"
                    )
                    return None
            except CancelledException:
                raise
            except Exception as e:
                protocol.emit_error(
                    f"Demucs processing error: {str(e)}",
                    code="DEMUCS_ERROR"
                )
                return None
            finally:
                sys.stderr = original_stderr
        else:
            # ============================================================
            # DEVELOPMENT (python script):
            # Use subprocess for real-time progress tracking via stderr.
            # ============================================================
            demucs_args = [sys.executable, "-m", "demucs.separate"] + demucs_cli_args

            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            process = subprocess.Popen(
                demucs_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                bufsize=1,  # Line buffered
                creationflags=creationflags
            )

            # Regex pattern to match progress percentages (e.g., "45%", "100%")
            progress_pattern = re.compile(r'(\d+)%')

            # Read stderr line by line for progress updates
            # Map Demucs 0-100% to visual 0-90% (reserve 90-100% for post-processing)
            last_progress = 0
            while True:
                # Check cancellation
                if cancellation_token.is_cancelled:
                    protocol.emit_log("Cancellation requested, terminating Demucs process...", level="debug")
                    process.kill()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    raise CancelledException()

                # Read line from stderr (Demucs outputs progress to stderr)
                line = process.stderr.readline()

                # If no more output and process finished, break
                if not line and process.poll() is not None:
                    break

                if line:
                    line = line.strip()

                    # Try to extract progress percentage
                    match = progress_pattern.search(line)
                    if match:
                        demucs_percent = int(match.group(1))
                        # Map Demucs 0-100% to visual 0-90%
                        visual_percent = int(demucs_percent * 0.90)
                        # Only emit if progress increased (avoid spam)
                        if visual_percent > last_progress:
                            protocol.emit_progress(visual_percent, detail=f"Processing: {demucs_percent}%")
                            last_progress = visual_percent
                    else:
                        # Log non-progress lines as debug for visibility
                        if line:
                            protocol.emit_log(line, level="debug")

            # Check exit code
            return_code = process.wait()
            if return_code != 0:
                # Read any remaining stderr
                stderr_output = process.stderr.read()
                error_msg = stderr_output[:500] if stderr_output else "Unknown error"
                protocol.emit_error(
                    f"Demucs process failed with code {return_code}: {error_msg}",
                    code="DEMUCS_ERROR"
                )
                return None

        # Post-processing phase: Show 95% while finalizing
        protocol.emit_progress(95, detail="Reconstructing audio & Saving...")

        # Check cancellation after processing
        if cancellation_token.is_cancelled:
            raise CancelledException()

        # Step: Saving
        protocol.emit_step_change(ProcessingStep.SAVING, step_number=5)
        protocol.emit_progress(97, detail="Organizing output files...")

        # Find and move output files
        demucs_output = os.path.join(temp_dir, model_name, input_filename)

        if not os.path.exists(demucs_output):
            protocol.emit_error(
                "Demucs did not produce expected output",
                code="OUTPUT_MISSING"
            )
            return None

        # Create final output directory, preferring the caller-supplied name so
        # video jobs are named after the video rather than the temp wav
        final_output_dir = os.path.join(
            output_dir,
            f"separated_{output_name or input_filename}"
        )
        if os.path.exists(final_output_dir):
            shutil.rmtree(final_output_dir)

        protocol.emit_progress(98, detail="Moving files...")
        shutil.move(demucs_output, final_output_dir)

        # Build output paths dict
        output_paths = {}
        ext = f".{output_format}" if output_format != "wav" else ".wav"

        for stem in model_config["stems"]:
            stem_path = os.path.join(final_output_dir, f"{stem}{ext}")
            if os.path.exists(stem_path):
                output_paths[stem] = stem_path

        # Process based on mode
        if processing_mode == "vocal_remover":
            protocol.emit_progress(99, detail="Mixing instrumental track...")
            protocol.emit_log("Mixing bass, drums, and other into instrumental track", level="info")

            # Mix bass, drums, and other into instrumental
            success = mix_instrumental_track(
                final_output_dir,
                output_format,
                model_config["stems"]
            )

            if success:
                # Update output_paths to reflect only vocals and instrumental
                instrumental_path = os.path.join(final_output_dir, f"instrumental{ext}")
                vocals_path = output_paths.get("vocals")

                output_paths = {
                    "vocals": vocals_path,
                    "instrumental": instrumental_path
                }
                protocol.emit_log("Instrumental track created successfully", level="info")
            else:
                protocol.emit_warning("Failed to mix instrumental track, keeping all stems", code="MIX_WARNING")

        protocol.emit_progress(100, detail="Files saved successfully")

        # Cleanup
        protocol.emit_step_change(ProcessingStep.CLEANUP, step_number=6)
        cleanup_temp()

        return output_paths

    except CancelledException:
        cleanup_temp()
        raise

    except Exception as e:
        protocol.emit_error(f"Separation failed: {str(e)}", code="SEPARATION_ERROR")
        cleanup_temp()
        return None


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def main() -> int:
    """
    Main entry point for the motor.
    
    Returns:
        Exit code (0 for success, non-zero for errors)
    """
    # Enable multiprocessing support for frozen executables
    multiprocessing.freeze_support()
    
    # Register signal handlers for graceful shutdown
    signal_handler.register()
    
    # Create cancellation token
    cancellation_token = CancellationToken(signal_handler)
    
    try:
        # Parse arguments
        args = parse_arguments()
        
        # Validate arguments
        is_valid, error_msg = validate_arguments(args)
        if not is_valid:
            protocol.emit_error(error_msg, code="INVALID_ARGS", fatal=True)
            return 3 if "not found" in error_msg.lower() else 2
        
        # Determine file type
        ext = Path(args.input_path).suffix.lower()
        is_video = ext in VIDEO_EXTENSIONS
        file_type = "video" if is_video else "audio"
        
        # Detect device
        device = detect_device(args.device)
        
        # Emit start event
        protocol.emit_start(
            file_path=args.input_path,
            file_type=file_type,
            model=args.model,
            device=device
        )
        
        # Step 1: Initializing
        protocol.emit_step_change(ProcessingStep.INITIALIZING, step_number=1)
        protocol.emit_progress(50, detail="Validating input file...")
        
        # Check cancellation
        if cancellation_token.check_and_report(protocol):
            return 6
        
        # Read the source sample rate up front, before any processing touches
        # the file. Demucs always outputs 44.1kHz, so I keep this around to
        # restore the original rate on the finished stems.
        source_sample_rate = probe_sample_rate(args.input_path, args.ffmpeg_path)
        if source_sample_rate:
            protocol.emit_log(f"Source sample rate: {source_sample_rate}Hz", level="info")
        else:
            protocol.emit_log("Could not detect source sample rate", level="debug")

        # Handle video files - extract audio first
        audio_path = args.input_path
        temp_audio = None

        if is_video:
            protocol.emit_step_change(ProcessingStep.EXTRACTING_AUDIO, step_number=2)
            protocol.emit_progress(0, detail="Extracting audio from video...")
            
            temp_audio = os.path.join(
                args.output_dir, 
                f".temp_audio_{Path(args.input_path).stem}.wav"
            )
            
            success = extract_audio_from_video(
                args.input_path,
                temp_audio,
                args.ffmpeg_path,
                cancellation_token
            )
            
            if not success:
                return 5
            
            if cancellation_token.check_and_report(protocol):
                return 6
            
            audio_path = temp_audio
            protocol.emit_progress(100, detail="Audio extracted successfully")

        # Frame count of the audio actually fed to Demucs, taken before the
        # temp wav of a video job gets deleted. The finished stems must match
        # it sample for sample after the final resample.
        source_frames = probe_frame_count(audio_path)
        if source_frames:
            protocol.emit_log(f"Source frames: {source_frames}", level="debug")

        # Perform separation
        output_paths = separate_audio(
            input_path=audio_path,
            output_dir=args.output_dir,
            model_name=args.model,
            device=device,
            quality_preset=args.quality,
            shifts_override=args.shifts,
            output_format=args.output_format,
            processing_mode=args.mode,
            cancellation_token=cancellation_token,
            output_name=Path(args.input_path).stem
        )
        
        # Cleanup temp audio if it was created
        if temp_audio and os.path.exists(temp_audio):
            try:
                os.remove(temp_audio)
            except Exception:
                pass
        
        # Check result
        if output_paths is None:
            return 5
        
        if cancellation_token.check_and_report(protocol):
            return 6

        # Restore the source sample rate. Demucs hands back 44.1kHz regardless
        # of what went in, so a 48kHz film would otherwise come out at 44.1kHz
        # and force the DAW to convert it on import.
        if source_sample_rate and source_sample_rate != DEMUCS_NATIVE_SAMPLE_RATE:
            protocol.emit_progress(
                99,
                detail=f"Restoring {source_sample_rate}Hz sample rate..."
            )
            resample_outputs_to_rate(
                output_paths,
                source_sample_rate,
                args.ffmpeg_path,
                output_format=args.output_format,
                expected_frames=source_frames
            )

        # Success!
        protocol.emit_success(
            output_paths=output_paths,
            stats={
                "model": args.model,
                "device": device,
                "quality": args.quality,
                "stemsGenerated": len(output_paths),
                "sampleRate": source_sample_rate or DEMUCS_NATIVE_SAMPLE_RATE
            }
        )
        
        return 0
        
    except CancelledException:
        protocol.emit_cancelled()
        signal_handler.run_cleanup()
        return 6
        
    except KeyboardInterrupt:
        protocol.emit_cancelled(reason="Keyboard interrupt")
        signal_handler.run_cleanup()
        return 6
        
    except Exception as e:
        protocol.emit_error(
            message=f"Unexpected error: {str(e)}",
            code="UNEXPECTED_ERROR",
            fatal=True
        )
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
