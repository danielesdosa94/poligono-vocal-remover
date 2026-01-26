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
import numpy as np
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
    
    # Build command
    cmd = [
        ffmpeg,
        "-i", video_path,
        "-vn",              # No video
        "-acodec", "pcm_s16le",  # PCM 16-bit
        "-ar", "44100",     # 44.1kHz sample rate
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

    Args:
        output_dir: Directory containing separated stems
        output_format: Output format (wav, mp3, flac)
        stems: List of all stems from the model

    Returns:
        True if successful, False otherwise
    """
    try:
        import soundfile as sf

        ext = f".{output_format}" if output_format != "wav" else ".wav"

        # Define stems to mix
        stems_to_mix = ["bass", "drums", "other"]

        # Read all stems to mix
        audio_data = []
        sample_rate = None

        for stem_name in stems_to_mix:
            stem_path = os.path.join(output_dir, f"{stem_name}{ext}")
            if not os.path.exists(stem_path):
                protocol.emit_log(f"Stem not found: {stem_name}{ext}", level="debug")
                continue

            try:
                data, sr = sf.read(stem_path)
                if sample_rate is None:
                    sample_rate = sr
                elif sr != sample_rate:
                    protocol.emit_warning(f"Sample rate mismatch for {stem_name}", code="SAMPLE_RATE_MISMATCH")
                    continue

                audio_data.append(data)
                protocol.emit_log(f"Loaded {stem_name} for mixing: shape={data.shape}", level="debug")
            except Exception as e:
                protocol.emit_log(f"Error reading {stem_name}: {e}", level="debug")
                continue

        if not audio_data:
            protocol.emit_error("No stems found to mix", code="NO_STEMS")
            return False

        # Mix by averaging (prevents clipping)
        mixed = np.mean(audio_data, axis=0)

        # Save instrumental track
        instrumental_path = os.path.join(output_dir, f"instrumental{ext}")

        # For MP3 and FLAC, we need to handle format-specific parameters
        if output_format == "mp3":
            sf.write(instrumental_path, mixed, sample_rate, format='MP3')
        elif output_format == "flac":
            sf.write(instrumental_path, mixed, sample_rate, format='FLAC')
        else:  # wav
            sf.write(instrumental_path, mixed, sample_rate)

        protocol.emit_log(f"Instrumental track saved: {instrumental_path}", level="debug")

        # Delete original stems
        for stem_name in stems_to_mix:
            stem_path = os.path.join(output_dir, f"{stem_name}{ext}")
            if os.path.exists(stem_path):
                try:
                    os.remove(stem_path)
                    protocol.emit_log(f"Removed {stem_name}{ext}", level="debug")
                except Exception as e:
                    protocol.emit_log(f"Failed to remove {stem_name}{ext}: {e}", level="debug")

        return True

    except ImportError:
        protocol.emit_error("soundfile library not found", code="IMPORT_ERROR")
        return False
    except Exception as e:
        protocol.emit_error(f"Failed to mix instrumental: {str(e)}", code="MIX_ERROR")
        return False


def separate_audio(
    input_path: str,
    output_dir: str,
    model_name: str,
    device: str,
    quality_preset: str,
    shifts_override: Optional[int],
    output_format: str,
    processing_mode: str,
    cancellation_token: CancellationToken
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

        # Build Demucs command line arguments
        demucs_args = [
            sys.executable,  # Use current Python interpreter
            "-m", "demucs.separate",  # Run demucs as module
            "-n", model_name,
            "-o", temp_dir,
            "-j", "0",  # Workers = 0 to avoid subprocess issues in frozen apps
            "--shifts", str(shifts),
            "--overlap", str(quality_config["overlap"]),
        ]

        # Add device flag
        if device == "cpu":
            demucs_args.extend(["-d", "cpu"])

        # Add output format if not wav
        if output_format == "mp3":
            demucs_args.extend(["--mp3"])
        elif output_format == "flac":
            demucs_args.extend(["--flac"])

        # Add input file
        demucs_args.append(input_path)

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

        # Run Demucs as subprocess to capture progress
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

        # Create final output directory
        final_output_dir = os.path.join(output_dir, f"separated_{input_filename}")
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
            cancellation_token=cancellation_token
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
        
        # Success!
        protocol.emit_success(
            output_paths=output_paths,
            stats={
                "model": args.model,
                "device": device,
                "quality": args.quality,
                "stemsGenerated": len(output_paths)
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
