#!/usr/bin/env python3
"""
Polígono AI Hub - Motor daemon
==============================
Persistent separation process driven by Electron over stdin/stdout.

The process starts once per app session, loads models lazily through
``engine.SeparationEngine`` and keeps them cached between jobs. Commands
arrive as one JSON object per line on stdin; events leave as one JSON object
per line on stdout (see ``utils/protocol.py``). stderr is only for library
noise and crash traces.

Commands (stdin -> motor):
    {"type": "separate", "jobId": "...", "input": "...", "outputDir": "...",
     "mode": "vocal_remover", "preset": "hq", "format": "wav", "device": "auto",
     "model": "htdemucs_ft",            # optional override
     "bitDepth": 32,                    # 16/24/32 (wav), 16/24 (flac)
     "monoOutput": false,               # mono stems for a mono source
     "chunkMinutes": 3,                 # block length for long files, 0 = off
     "chunkThresholdMinutes": 12}       # never split a file shorter than this
    {"type": "cancel", "jobId": "..."}
    {"type": "ping"}
    {"type": "shutdown"}

Events (motor -> stdout): ready, pong, start, step_change, progress,
download_progress, log, warning, error, success, cancelled. Every event
carries "jobId".

Threads:
    main      reads stdin and dispatches commands
    worker    runs one job at a time from a queue
    watchdog  exits when the parent process (--parent-pid) is gone

Cancellation is cooperative: "cancel" sets the engine's threading.Event and
the Demucs callback raises out of the separation loop. No signals, no
process kills; the job's temp directory is removed by the engine.

Usage:
    python motor.py --parent-pid <electron pid> [--ffmpeg-path <ffmpeg.exe>]
    motor.exe --parent-pid <electron pid> [--ffmpeg-path <ffmpeg.exe>]
"""

import argparse
import ctypes
import json
import multiprocessing
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

# Make "engine" and "utils" importable from the script directory. Works the
# same for `python motor.py` and for the PyInstaller bundle.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.protocol import ProcessingStep, Protocol  # noqa: E402

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".flv"}

TEMP_ROOT_NAME = "poligono-ai-hub"
TEMP_JOB_PREFIX = "motor"

WATCHDOG_INTERVAL_S = 2.0
# How long a shutdown waits for the running job to notice its cancel before
# the process exits anyway. Electron gives 3 s before taskkill.
SHUTDOWN_GRACE_S = 2.5

# Engine stage -> protocol step. LOADING_MODEL (2) is emitted separately
# when the model for the job is not cached yet.
STAGE_STEPS = {
    "loading": (ProcessingStep.ANALYZING, 3),
    "separating": (ProcessingStep.SEPARATING, 4),
    "saving": (ProcessingStep.SAVING, 5),
}


# =============================================================================
# Process liveness (watchdog)
# =============================================================================

def process_alive(pid: int) -> bool:
    """
    True if a process with this pid still exists.

    Windows: OpenProcess(SYNCHRONIZE) + WaitForSingleObject(handle, 0).
    A signalled handle means the process has exited. OpenProcess failing with
    ERROR_INVALID_PARAMETER means there is no such pid; any other failure
    (access denied) means it exists but we cannot open it.

    Never uses os.kill on Windows: there it maps to TerminateProcess.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        SYNCHRONIZE = 0x00100000
        ERROR_INVALID_PARAMETER = 87
        WAIT_TIMEOUT = 0x00000102

        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return ctypes.get_last_error() != ERROR_INVALID_PARAMETER
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# =============================================================================
# Temp directory hygiene
# =============================================================================

def temp_job_id(job_id: str) -> str:
    """Directory-safe temp id: motor-<pid>-<jobId>."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", job_id)[:64]
    return f"{TEMP_JOB_PREFIX}-{os.getpid()}-{safe}"


def sweep_stale_temp_dirs() -> int:
    """
    Remove temp dirs left by motor processes that no longer exist (a previous
    session killed from Task Manager, for instance). Dirs of live motors are
    left alone.
    """
    root = Path(tempfile.gettempdir()) / TEMP_ROOT_NAME
    if not root.is_dir():
        return 0
    removed = 0
    for entry in root.glob(f"{TEMP_JOB_PREFIX}-*"):
        match = re.match(rf"{TEMP_JOB_PREFIX}-(\d+)-", entry.name)
        if not match or not entry.is_dir():
            continue
        owner = int(match.group(1))
        if owner == os.getpid() or process_alive(owner):
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed += 1
    return removed


# =============================================================================
# Daemon
# =============================================================================

class MotorDaemon:
    """Owns the engines, the job queue and the three threads."""

    def __init__(self, ffmpeg_path: Optional[str], parent_pid: Optional[int]):
        from engine import find_ffmpeg

        self.ffmpeg_path, _ = find_ffmpeg(ffmpeg_path)
        self.parent_pid = parent_pid
        self.session = Protocol()

        self._engines: Dict[str, Any] = {}          # resolved device -> SeparationEngine
        self._queue: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._pending: Dict[str, dict] = {}          # jobId -> job (queued or running)
        self._lock = threading.Lock()
        self._current_job_id: Optional[str] = None
        self._current_protocol: Optional[Protocol] = None
        self._current_engine: Any = None

        self._stopping = threading.Event()
        self._shutdown_once = threading.Lock()
        self._worker = threading.Thread(target=self._worker_loop, name="job-worker", daemon=True)
        self._watchdog = threading.Thread(target=self._watchdog_loop, name="watchdog", daemon=True)

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        import torch

        removed = sweep_stale_temp_dirs()
        self._worker.start()
        if self.parent_pid:
            self._watchdog.start()

        cuda = {"available": bool(torch.cuda.is_available())}
        if cuda["available"]:
            props = torch.cuda.get_device_properties(0)
            cuda["name"] = props.name
            cuda["vramGb"] = round(props.total_memory / 1024**3, 1)

        from engine import (
            CHUNK_MINUTES,
            CHUNK_OVERLAP_SECONDS,
            CHUNK_THRESHOLD_MINUTES,
            FORMATS,
            MODEL_CONFIGS,
            MODES,
            PRESETS,
        )

        self.session.emit_ready({
            "pid": os.getpid(),
            "parentPid": self.parent_pid,
            "torch": torch.__version__,
            "cuda": cuda,
            "ffmpeg": self.ffmpeg_path,
            # Full tables, not just names: the UI labels presets with their
            # relative cost and builds the bit depth choices from the formats.
            "presets": {
                name: {
                    "model": preset.model,
                    "shifts": preset.shifts,
                    "overlap": preset.overlap,
                    "relativeCost": preset.relative_cost,
                    "description": preset.description,
                }
                for name, preset in PRESETS.items()
            },
            "formats": {
                name: {
                    "extension": spec.extension,
                    "bitDepths": list(spec.bit_depths),
                    "defaultBitDepth": spec.default_bit_depth,
                    "sampleAccurate": spec.sample_accurate,
                }
                for name, spec in FORMATS.items()
            },
            "modes": {name: spec.description for name, spec in MODES.items()},
            "models": {
                name: {
                    "description": config["description"],
                    "stems": list(config["stems"]),
                    "bagSize": config["bag_size"],
                }
                for name, config in MODEL_CONFIGS.items()
            },
            "chunking": {
                "thresholdMinutes": CHUNK_THRESHOLD_MINUTES,
                "chunkMinutes": CHUNK_MINUTES,
                "crossfadeSeconds": CHUNK_OVERLAP_SECONDS,
            },
            "frozen": bool(getattr(sys, "frozen", False)),
        })
        if removed:
            self.session.emit_log(f"Removed {removed} stale temp folder(s) from a previous session", "debug")

    def serve(self) -> int:
        """Read stdin until EOF or a shutdown command. Returns the exit code."""
        while not self._stopping.is_set():
            line = sys.stdin.readline()
            if line == "":
                self._shutdown("stdin closed")
                break
            line = line.strip()
            if not line:
                continue
            try:
                command = json.loads(line)
            except ValueError:
                self.session.emit_error("Command is not valid JSON", code="BAD_COMMAND", fatal=False)
                continue
            if not isinstance(command, dict):
                self.session.emit_error("Command must be a JSON object", code="BAD_COMMAND", fatal=False)
                continue
            if not self._dispatch(command):
                break
        return 0

    def _shutdown(self, reason: str) -> None:
        """
        Stop accepting work, cancel the running job, and let the worker finish
        its cleanup. Idempotent. Never blocks longer than SHUTDOWN_GRACE_S.
        """
        if not self._shutdown_once.acquire(blocking=False):
            return
        self._stopping.set()
        self.session.emit_log(f"Shutting down: {reason}", "info")

        with self._lock:
            for job in self._pending.values():
                job["cancelled"] = True
            if self._current_engine is not None:
                self._current_engine.request_cancel()
        self._queue.put(None)

        if self._worker.is_alive():
            self._worker.join(timeout=SHUTDOWN_GRACE_S)
        try:
            sys.stdout.flush()
        except (OSError, ValueError):
            pass
        if self._worker.is_alive():
            # The job did not reach a cancel check in time. Exit anyway; the
            # next motor start sweeps whatever temp files this leaves.
            os._exit(0)

    # ------------------------------------------------------------- watchdog

    def _watchdog_loop(self) -> None:
        while not self._stopping.is_set():
            time.sleep(WATCHDOG_INTERVAL_S)
            if self._stopping.is_set():
                return
            if not process_alive(self.parent_pid):
                self._shutdown(f"parent process {self.parent_pid} is gone")
                # The main thread may be blocked in stdin.readline(); do not
                # wait for it.
                os._exit(0)

    # ------------------------------------------------------------- commands

    def _dispatch(self, command: dict) -> bool:
        """Handle one command. Returns False when the daemon must stop."""
        kind = command.get("type")

        if kind == "ping":
            self.session.emit_pong()
            return True

        if kind == "shutdown":
            self._shutdown("shutdown command")
            return False

        if kind == "separate":
            self._enqueue(command)
            return True

        if kind == "cancel":
            self._cancel(command.get("jobId"))
            return True

        self.session.emit_error(f"Unknown command type: {kind!r}", code="BAD_COMMAND", fatal=False)
        return True

    def _enqueue(self, command: dict) -> None:
        job_id = command.get("jobId")
        if not isinstance(job_id, str) or not job_id:
            self.session.emit_error("separate: missing jobId", code="BAD_COMMAND", fatal=False)
            return
        proto = Protocol(job_id)

        input_path = command.get("input")
        if not isinstance(input_path, str) or not input_path:
            proto.emit_error("separate: missing input path", code="INVALID_ARGS")
            return
        if not os.path.isfile(input_path):
            proto.emit_error(f"Input file not found: {input_path}", code="FILE_NOT_FOUND")
            return

        with self._lock:
            if job_id in self._pending:
                proto.emit_error(f"Job id already in use: {job_id}", code="DUPLICATE_JOB")
                return
            job = {
                "jobId": job_id,
                "input": input_path,
                "outputDir": command.get("outputDir") or os.path.dirname(input_path),
                "mode": command.get("mode") or "vocal_remover",
                "preset": command.get("preset") or "hq",
                "format": command.get("format") or "wav",
                "device": command.get("device") or "auto",
                "model": command.get("model") or None,
                "bitDepth": command.get("bitDepth"),
                "monoOutput": bool(command.get("monoOutput")),
                "chunkMinutes": command.get("chunkMinutes"),
                "chunkThresholdMinutes": command.get("chunkThresholdMinutes"),
                "cancelled": False,
            }
            self._pending[job_id] = job
        self._queue.put(job)

    def _cancel(self, job_id: Optional[str]) -> None:
        with self._lock:
            job = self._pending.get(job_id) if job_id else None
            if job is None:
                self.session.emit_warning(f"cancel: no such job {job_id!r}", code="UNKNOWN_JOB")
                return
            job["cancelled"] = True
            if job_id == self._current_job_id:
                if self._current_engine is not None:
                    self._current_engine.request_cancel()
                # Otherwise the worker sees the flag before it starts.
                return
            # Queued, not started: report right away and let the worker skip it.
            del self._pending[job_id]
        Protocol(job_id).emit_cancelled("Cancelled before start")

    # --------------------------------------------------------------- engine

    def _engine_log(self, message: str, level: str = "info") -> None:
        """Route engine log lines to the running job's protocol."""
        with self._lock:
            proto = self._current_protocol or self.session
        if level == "warning":
            proto.emit_warning(message)
        else:
            proto.emit_log(message, level)

    def _get_engine(self, device: str):
        from engine import SeparationEngine

        engine = self._engines.get(device)
        if engine is None:
            engine = SeparationEngine(device=device, log=self._engine_log)
            self._engines[engine.device] = engine
            self._engines[device] = engine
        return engine

    # --------------------------------------------------------------- worker

    def _worker_loop(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            with self._lock:
                if job["cancelled"] or self._stopping.is_set():
                    # Cancelled while queued (already reported) or shutting down.
                    self._pending.pop(job["jobId"], None)
                    continue
                self._current_job_id = job["jobId"]
                self._current_protocol = Protocol(job["jobId"])
                self._current_engine = None
            try:
                self._run_job(job, self._current_protocol)
            finally:
                with self._lock:
                    self._pending.pop(job["jobId"], None)
                    self._current_job_id = None
                    self._current_protocol = None
                    self._current_engine = None

    def _run_job(self, job: dict, proto: Protocol) -> None:
        from engine import (
            CHUNK_THRESHOLD_MINUTES,
            FORMATS,
            MODEL_CONFIGS,
            MODES,
            PRESETS,
            AudioIOError,
            SeparationCancelled,
            resolve_bit_depth,
            resolve_device,
        )

        try:
            if job["mode"] not in MODES:
                raise ValueError(f"Unknown mode: {job['mode']}")
            if job["preset"] not in PRESETS:
                raise ValueError(f"Unknown preset: {job['preset']}")
            if job["format"] not in FORMATS:
                raise ValueError(f"Unsupported format: {job['format']}")
            if job["model"] is not None and job["model"] not in MODEL_CONFIGS:
                raise ValueError(f"Unknown model: {job['model']}")
            # Raises for a depth the format cannot carry (e.g. 32 bit FLAC).
            bit_depth = resolve_bit_depth(job["format"], job["bitDepth"])
            chunk_minutes = (
                None if job["chunkMinutes"] is None else float(job["chunkMinutes"])
            )
            chunk_threshold = (
                CHUNK_THRESHOLD_MINUTES
                if job["chunkThresholdMinutes"] is None
                else float(job["chunkThresholdMinutes"])
            )

            preset = PRESETS[job["preset"]]
            model = job["model"] or preset.model
            device, device_warning = resolve_device(job["device"])
            file_type = "video" if Path(job["input"]).suffix.lower() in VIDEO_EXTENSIONS else "audio"

            proto.emit_start(job["input"], file_type, model, device)
            proto.emit_step_change(ProcessingStep.INITIALIZING, 1)
            proto.emit_progress(0.0, 0.0, detail="Initializing")
            if device_warning:
                proto.emit_warning(device_warning, code="CUDA_UNAVAILABLE")

            engine = self._get_engine(device)
            # Order matters: clear the flag, publish the engine, then honour a
            # cancel that arrived while this job was still queued. A cancel
            # that lands after this point calls request_cancel() directly.
            engine.reset_cancel()
            with self._lock:
                self._current_engine = engine
                if job["cancelled"]:
                    engine.request_cancel()

            key = (model, int(preset.shifts), float(preset.overlap), engine.device)
            if key not in engine.loaded_models:
                proto.emit_step_change(ProcessingStep.LOADING_MODEL, 2)
                proto.emit_progress(0.0, 0.0, detail=f"Loading model {model}")

            stage_state = {"stage": None}
            loading_step = ProcessingStep.EXTRACTING_AUDIO if file_type == "video" else ProcessingStep.ANALYZING

            def on_progress(fraction: float, stage: str) -> None:
                if stage != stage_state["stage"]:
                    stage_state["stage"] = stage
                    step, number = STAGE_STEPS[stage]
                    if stage == "loading":
                        step = loading_step
                    proto.emit_step_change(step, number)
                percent = fraction * 100.0
                proto.emit_progress(percent, percent, detail=stage)

            def on_download(info: dict) -> None:
                proto.emit_download_progress(info)

            result = engine.separate_file(
                job["input"],
                job["outputDir"],
                mode=job["mode"],
                preset=job["preset"],
                fmt=job["format"],
                bit_depth=bit_depth,
                ffmpeg_path=self.ffmpeg_path,
                on_progress=on_progress,
                job_id=temp_job_id(job["jobId"]),
                model=job["model"],
                mono_output=job["monoOutput"],
                chunk_minutes=chunk_minutes,
                chunk_threshold_minutes=chunk_threshold,
                on_download=on_download,
            )

            proto.emit_step_change(ProcessingStep.CLEANUP, 6)
            proto.emit_progress(100.0, 100.0, detail="Done")
            proto.emit_success(
                {name: res.path for name, res in result.outputs.items()},
                stats={
                    "model": result.stats.model,
                    "shifts": result.stats.shifts,
                    "overlap": result.stats.overlap,
                    "device": result.stats.device,
                    "preset": job["preset"],
                    "mode": job["mode"],
                    "format": job["format"],
                    "bitDepth": bit_depth,
                    "sampleRate": result.source_samplerate,
                    "sourceFrames": result.source_frames,
                    "sourceChannels": result.source_channels,
                    "channelsOut": result.channels_out,
                    "stemsGenerated": len(result.outputs),
                    "modelLoadedNow": result.stats.model_loaded_now,
                    "separateSeconds": round(result.stats.separate_seconds, 2),
                    "chunks": result.stats.chunks,
                    "crossfadeSeconds": result.stats.crossfade_seconds,
                    "warnings": result.warnings,
                    "frameDeltas": {name: res.frame_delta for name, res in result.outputs.items()},
                },
                output_dir=result.output_dir,
            )

        except SeparationCancelled:
            proto.emit_cancelled()
        except AudioIOError as exc:
            proto.emit_error(str(exc), code="AUDIO_IO_ERROR")
        except (ValueError, KeyError) as exc:
            proto.emit_error(str(exc), code="INVALID_ARGS")
        except Exception as exc:  # noqa: BLE001 - the daemon must survive any job failure
            proto.emit_log(traceback.format_exc(), "debug")
            proto.emit_error(f"{type(exc).__name__}: {exc}", code="ENGINE_ERROR")


# =============================================================================
# Entry point
# =============================================================================

def parse_arguments(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polígono AI Hub - separation daemon")
    parser.add_argument("--parent-pid", type=int, default=None,
                        help="Exit when this process is gone (Electron main process)")
    parser.add_argument("--ffmpeg-path", default=None,
                        help="ffmpeg binary; ffprobe is expected next to it")
    return parser.parse_args(argv)


def configure_streams() -> None:
    """UTF-8 on every pipe, whatever the console code page is."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    multiprocessing.freeze_support()
    configure_streams()
    args = parse_arguments(argv)

    try:
        daemon = MotorDaemon(ffmpeg_path=args.ffmpeg_path, parent_pid=args.parent_pid)
        daemon.start()
    except Exception as exc:  # noqa: BLE001 - report the startup failure, then exit
        Protocol().emit_error(f"Motor failed to start: {type(exc).__name__}: {exc}",
                              code="STARTUP_ERROR", fatal=True)
        traceback.print_exc()
        return 4

    return daemon.serve()


if __name__ == "__main__":
    sys.exit(main())
