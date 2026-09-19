"""
Protocol Module - JSON Communication with Electron
===================================================
Handles all communication between the Python motor and the Electron main
process. Every message is a single-line JSON object written to stdout.

The daemon runs one Protocol instance per job (bound to that job's id) plus a
job-less instance for session events (ready, pong, command errors). All of
them share one write lock so lines never interleave between threads.
"""

import json
import sys
import threading
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

# Bump when the command/event contract changes in a way Electron must know about.
# 3: added download_progress; separate accepts bitDepth / monoOutput / chunkMinutes.
PROTOCOL_VERSION = 3

_WRITE_LOCK = threading.Lock()


class EventType(Enum):
    """Event types for the communication protocol (motor -> Electron)."""
    READY = "ready"
    PONG = "pong"
    START = "start"
    DOWNLOAD_PROGRESS = "download_progress"
    PROGRESS = "progress"
    STEP_CHANGE = "step_change"
    LOG = "log"
    WARNING = "warning"
    ERROR = "error"
    SUCCESS = "success"
    CANCELLED = "cancelled"


class ProcessingStep(Enum):
    """Processing pipeline steps."""
    INITIALIZING = "initializing"
    LOADING_MODEL = "loading_model"
    EXTRACTING_AUDIO = "extracting_audio"  # For video files
    ANALYZING = "analyzing"
    SEPARATING = "separating"
    SAVING = "saving"
    CLEANUP = "cleanup"


class Protocol:
    """
    JSON protocol emitter.

    All output goes to stdout as single-line JSON objects, flushed immediately.
    Every event carries "jobId" (None for session-level events).
    """

    def __init__(self, job_id: Optional[str] = None):
        self.job_id = job_id
        self._start_time: Optional[datetime] = None
        self._current_step: Optional[ProcessingStep] = None
        self._total_steps: int = 6
        self._step_weights: Dict[ProcessingStep, float] = {
            ProcessingStep.INITIALIZING: 0.02,
            ProcessingStep.LOADING_MODEL: 0.08,
            ProcessingStep.EXTRACTING_AUDIO: 0.05,
            ProcessingStep.ANALYZING: 0.05,
            ProcessingStep.SEPARATING: 0.75,
            ProcessingStep.SAVING: 0.04,
            ProcessingStep.CLEANUP: 0.01,
        }

    def _emit(self, event_type: EventType, data: Dict[str, Any]) -> None:
        """
        Emit a JSON message to stdout.

        Each message is a single line so parsing stays trivial; flush=True so
        Electron sees it immediately. A dead stdout pipe (parent gone) is
        swallowed: the watchdog / stdin EOF path takes care of exiting.
        """
        message = {
            "event": event_type.value,
            "jobId": self.job_id,
            "timestamp": datetime.now().isoformat(),
            **data,
        }
        try:
            line = json.dumps(message, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            line = json.dumps({
                "event": EventType.ERROR.value,
                "jobId": self.job_id,
                "message": f"Protocol error: {exc}",
                "code": "PROTOCOL_ERROR",
                "fatal": False,
            })
        with _WRITE_LOCK:
            try:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
            except (OSError, ValueError):
                pass

    # ------------------------------------------------------------ session

    def emit_ready(self, info: Dict[str, Any]) -> None:
        """Signal that the daemon is up and accepting commands."""
        self._emit(EventType.READY, {"protocolVersion": PROTOCOL_VERSION, **info})

    def emit_pong(self) -> None:
        self._emit(EventType.PONG, {})

    # ---------------------------------------------------------------- job

    def emit_start(self, file_path: str, file_type: str, model: str, device: str) -> None:
        """Signal that processing has started."""
        self._start_time = datetime.now()
        self._emit(EventType.START, {
            "file": file_path,
            "fileType": file_type,  # "audio" or "video"
            "model": model,
            "device": device,
            "totalSteps": self._total_steps,
        })

    def emit_step_change(self, step: ProcessingStep, step_number: int) -> None:
        """Signal transition to a new processing step."""
        self._current_step = step
        self._emit(EventType.STEP_CHANGE, {
            "step": step.value,
            "stepNumber": step_number,
            "totalSteps": self._total_steps,
            "stepWeight": self._step_weights.get(step, 0.1),
        })

    def emit_progress(
        self,
        step_percent: float,
        global_percent: Optional[float] = None,
        eta_seconds: Optional[int] = None,
        detail: Optional[str] = None,
    ) -> None:
        """
        Emit a progress update.

        Args:
            step_percent: Progress within the current step (0-100)
            global_percent: Overall job progress (0-100), if known
            eta_seconds: Estimated time remaining in seconds
            detail: Optional detail message
        """
        data = {
            "stepPercent": round(step_percent, 1),
            "globalPercent": round(global_percent, 1) if global_percent is not None else None,
            "currentStep": self._current_step.value if self._current_step else None,
        }
        if eta_seconds is not None:
            data["etaSeconds"] = eta_seconds
        if detail:
            data["detail"] = detail
        self._emit(EventType.PROGRESS, data)

    def emit_download_progress(self, info: Dict[str, Any]) -> None:
        """
        Report model weights being fetched.

        Sent while the job sits in LOADING_MODEL, so the UI can show a real
        download instead of a bar frozen at "Loading model". `info` carries
        name, signature, fileIndex, fileCount, bytesDone, bytesTotal and
        percent (across the whole set of files, not just the current one).
        """
        self._emit(EventType.DOWNLOAD_PROGRESS, dict(info))

    def emit_log(self, message: str, level: str = "info") -> None:
        """Emit a log message (shown in the app's debug console)."""
        self._emit(EventType.LOG, {"message": message, "level": level})

    def emit_warning(self, message: str, code: Optional[str] = None) -> None:
        """Emit a warning (non-fatal issue)."""
        data: Dict[str, Any] = {"message": message}
        if code:
            data["code"] = code
        self._emit(EventType.WARNING, data)

    def emit_error(self, message: str, code: Optional[str] = None, fatal: bool = True) -> None:
        """
        Emit an error.

        Args:
            message: Human-readable error message
            code: Machine-readable error code (e.g. "FILE_NOT_FOUND")
            fatal: True when this error ends the job
        """
        self._emit(EventType.ERROR, {
            "message": message,
            "code": code,
            "fatal": fatal,
            "elapsedSeconds": self._elapsed(),
        })

    def emit_success(
        self,
        output_paths: Dict[str, str],
        stats: Optional[Dict] = None,
        output_dir: Optional[str] = None,
    ) -> None:
        """
        Signal successful completion.

        Args:
            output_paths: Output name -> file path
            stats: Optional processing statistics
            output_dir: Folder that holds the outputs (so the UI never has to
                derive it from a file path)
        """
        data: Dict[str, Any] = {
            "outputs": output_paths,
            "outputDir": output_dir,
            "elapsedSeconds": self._elapsed(),
        }
        if stats:
            data["stats"] = stats
        self._emit(EventType.SUCCESS, data)

    def emit_cancelled(self, reason: str = "User requested cancellation") -> None:
        """Signal that processing was cancelled."""
        self._emit(EventType.CANCELLED, {
            "reason": reason,
            "elapsedSeconds": self._elapsed(),
            "lastStep": self._current_step.value if self._current_step else None,
        })

    def _elapsed(self) -> Optional[float]:
        if self._start_time is None:
            return None
        return (datetime.now() - self._start_time).total_seconds()


# Job-less instance for callers that only need session-level events.
protocol = Protocol()
