"""
Terminal harness for the separation engine (no Electron involved).

Usage (from the repo root, venv active):
    python -m python.tools.separate_cli <input> <outdir> --mode vocal_remover --preset hq --format wav

Emits the same line-delimited JSON events as the app protocol while running,
then prints a human-readable summary: sample rates, subtype, frames in/out
per stem, timing, and the null-test residual peak (sum of stems vs. source).

--keep-alive-demo runs the same file twice through one engine instance so the
log shows the model being loaded only once.
"""

import argparse
import math
import os
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

# Make "engine" and "utils" importable the same way motor.py will import them.
PYTHON_DIR = Path(__file__).resolve().parents[1]
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

from engine import (  # noqa: E402
    FORMATS,
    MODES,
    PRESETS,
    JobResult,
    SeparationCancelled,
    SeparationEngine,
    load_audio,
)
from utils.protocol import ProcessingStep, Protocol  # noqa: E402

STAGE_STEPS = {
    "loading": (ProcessingStep.ANALYZING, 3),
    "separating": (ProcessingStep.SEPARATING, 4),
    "saving": (ProcessingStep.SAVING, 5),
}


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polígono AI Hub - engine harness")
    parser.add_argument("input", help="Audio or video file")
    parser.add_argument("outdir", help="Output directory (a <name>/ folder is created inside)")
    parser.add_argument("--mode", choices=sorted(MODES), default="vocal_remover")
    parser.add_argument("--preset", choices=sorted(PRESETS), default="hq")
    parser.add_argument("--format", choices=sorted(FORMATS), default="wav")
    parser.add_argument("--bit-depth", type=int, default=None, help="16/24/32 (wav), 16/24 (flac)")
    parser.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    parser.add_argument("--ffmpeg", default=None, help="Path to ffmpeg binary")
    parser.add_argument("--shifts", type=int, default=None, help="Override preset shifts")
    parser.add_argument("--overlap", type=float, default=None, help="Override preset overlap")
    parser.add_argument("--keep-alive-demo", action="store_true", help="Run the file twice on one engine")
    parser.add_argument("--no-null-test", action="store_true", help="Skip the null test")
    return parser.parse_args(argv)


def db(value: float) -> float:
    return 20.0 * math.log10(value) if value > 0 else -math.inf


def null_test(input_path: str, result: JobResult, ffmpeg_path) -> dict:
    """Compare sum(written stems) against the source, both at the source rate."""
    source = load_audio(input_path, ffmpeg_path).data
    total = None
    frames = source.shape[0]
    for name, res in result.outputs.items():
        stem, _ = sf.read(res.path, dtype="float32", always_2d=True)
        frames = min(frames, stem.shape[0])
        total = stem if total is None else (total[:frames] + stem[:frames])
    residual = total[:frames] - source[:frames]
    peak = float(np.max(np.abs(residual)))
    rms = float(np.sqrt(np.mean(np.square(residual, dtype=np.float64))))
    source_peak = float(np.max(np.abs(source[:frames])))
    return {
        "frames_compared": int(frames),
        "residual_peak_dbfs": db(peak),
        "residual_rms_dbfs": db(rms),
        "source_peak_dbfs": db(source_peak),
        "stems_sum_peak_dbfs": db(float(np.max(np.abs(total[:frames])))),
        # Half a float32 ULP at the source peak: the floor a residual output
        # can reach, since fl(v + fl(s - v)) cannot always return s.
        "float32_floor_dbfs": db(source_peak * 2 ** -24),
    }


def run_job(engine: SeparationEngine, protocol: Protocol, args, run_index: int) -> JobResult:
    protocol.emit_start(args.input, "audio", PRESETS[args.preset].model, engine.device)
    protocol.emit_step_change(ProcessingStep.INITIALIZING, 1)
    state = {"stage": None}

    def on_progress(fraction: float, stage: str) -> None:
        if stage != state["stage"]:
            state["stage"] = stage
            step, number = STAGE_STEPS[stage]
            protocol.emit_step_change(step, number)
        protocol.emit_progress(fraction * 100.0, global_percent=fraction * 100.0, detail=stage)

    engine.reset_cancel()
    try:
        result = engine.separate_file(
            args.input,
            args.outdir,
            mode=args.mode,
            preset=args.preset,
            fmt=args.format,
            bit_depth=args.bit_depth,
            ffmpeg_path=args.ffmpeg,
            on_progress=on_progress,
            job_id=f"cli-{os.getpid()}-{run_index}",
            shifts=args.shifts,
            overlap=args.overlap,
        )
    except SeparationCancelled:
        protocol.emit_cancelled()
        raise
    except Exception as exc:
        protocol.emit_error(f"{type(exc).__name__}: {exc}", code="ENGINE_ERROR")
        raise

    protocol.emit_success(
        {name: res.path for name, res in result.outputs.items()},
        stats={
            "model": result.stats.model,
            "device": result.stats.device,
            "sampleRate": result.source_samplerate,
            "modelLoadedNow": result.stats.model_loaded_now,
            "separateSeconds": round(result.stats.separate_seconds, 2),
        },
    )
    return result


def print_summary(args, result: JobResult, null: dict, run_index: int) -> None:
    line = "-" * 72
    print(line)
    print(f"SUMMARY (run {run_index}) - {os.path.basename(args.input)}")
    print(line)
    print(f"  mode / preset / format : {args.mode} / {args.preset} / {args.format}")
    print(f"  model                  : {result.stats.model} (shifts={result.stats.shifts}, overlap={result.stats.overlap})")
    print(f"  device                 : {result.stats.device}")
    print(f"  model loaded this run  : {'yes' if result.stats.model_loaded_now else 'no (cached)'}")
    print(f"  source                 : {result.source_samplerate} Hz, {result.source_channels} ch, {result.source_frames} frames")
    print(f"  model rate             : {result.stats.model_samplerate} Hz")
    print(f"  separation time        : {result.stats.separate_seconds:.1f} s")
    print(f"  total job time         : {result.elapsed_seconds:.1f} s")
    print("  outputs:")
    for name, res in result.outputs.items():
        status = "OK" if res.frame_delta == 0 else f"DELTA {res.frame_delta:+d}"
        print(
            f"    {name:<13} {res.samplerate} Hz  {res.subtype:<16} "
            f"{res.frames} frames (in {result.source_frames})  {status}"
        )
        print(f"    {'':<13} {res.path}")
    if null:
        print("  null test (sum of stems - source):")
        print(f"    frames compared      : {null['frames_compared']}")
        print(f"    source peak          : {null['source_peak_dbfs']:.1f} dBFS")
        print(f"    stems sum peak       : {null['stems_sum_peak_dbfs']:.1f} dBFS")
        print(f"    residual peak        : {null['residual_peak_dbfs']:.1f} dBFS")
        print(f"    residual RMS         : {null['residual_rms_dbfs']:.1f} dBFS")
        print(f"    float32 floor (ref)  : {null['float32_floor_dbfs']:.1f} dBFS (half ULP at source peak)")
    for message in result.warnings:
        print(f"  warning: {message}")
    print(line)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = parse_args(argv)
    if not os.path.isfile(args.input):
        print(f"Input not found: {args.input}", file=sys.stderr)
        return 2

    protocol = Protocol()
    engine = SeparationEngine(device=args.device, log=protocol.emit_log)

    runs = 2 if args.keep_alive_demo else 1
    t0 = time.perf_counter()
    try:
        for run_index in range(1, runs + 1):
            result = run_job(engine, protocol, args, run_index)
            null = None
            if not args.no_null_test:
                if FORMATS[args.format].sample_accurate:
                    null = null_test(args.input, result, args.ffmpeg)
                else:
                    protocol.emit_log("Null test skipped: lossy format is not sample-accurate", "info")
            print_summary(args, result, null, run_index)
    except SeparationCancelled:
        return 6
    except KeyboardInterrupt:
        engine.request_cancel()
        protocol.emit_cancelled("Keyboard interrupt")
        return 6
    except Exception as exc:
        print(f"Failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if runs > 1:
        print(f"Keep-alive demo: {runs} runs in {time.perf_counter() - t0:.1f}s, models loaded: {engine.loaded_models}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
