"""
Protocol Module - JSON Communication with Electron
===================================================
Handles all communication between Python motor and Electron main process.
All messages are JSON objects sent line-by-line through stdout.
"""

import json
import sys
from enum import Enum
from typing import Optional, Dict, Any
from datetime import datetime


class EventType(Enum):
    """Event types for the communication protocol."""
    START = "start"
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
    Handles JSON protocol communication with Electron.
    
    All output goes to stdout as single-line JSON objects.
    Electron reads these line-by-line and parses them.
    """
    
    def __init__(self):
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
        
        Each message is a single line to make parsing reliable.
        flush=True ensures immediate delivery to Electron.
        """
        message = {
            "event": event_type.value,
            "timestamp": datetime.now().isoformat(),
            **data
        }
        
        try:
            print(json.dumps(message, ensure_ascii=False), flush=True)
        except Exception as e:
            # Fallback: if JSON encoding fails, send error as plain text
            fallback = {"event": "error", "message": f"Protocol error: {str(e)}"}
            print(json.dumps(fallback), flush=True)
    
    def emit_start(self, file_path: str, file_type: str, model: str, device: str) -> None:
        """Signal that processing has started."""
        self._start_time = datetime.now()
        self._emit(EventType.START, {
            "file": file_path,
            "fileType": file_type,  # "audio" or "video"
            "model": model,
            "device": device,
            "totalSteps": self._total_steps
        })
    
    def emit_step_change(self, step: ProcessingStep, step_number: int) -> None:
        """Signal transition to a new processing step."""
        self._current_step = step
        self._emit(EventType.STEP_CHANGE, {
            "step": step.value,
            "stepNumber": step_number,
            "totalSteps": self._total_steps,
            "stepWeight": self._step_weights.get(step, 0.1)
        })
    
    def emit_progress(
        self,
        step_percent: float,
        global_percent: Optional[float] = None,
        eta_seconds: Optional[int] = None,
        detail: Optional[str] = None
    ) -> None:
        """
        Emit progress update.
        
        Args:
            step_percent: Progress within current step (0-100)
            global_percent: Overall progress (0-100), calculated if not provided
            eta_seconds: Estimated time remaining in seconds
            detail: Optional detail message (e.g., "Processing chunk 3/10")
        """
        data = {
            "stepPercent": round(step_percent, 1),
            "globalPercent": round(global_percent, 1) if global_percent else None,
            "currentStep": self._current_step.value if self._current_step else None,
        }
        
        if eta_seconds is not None:
            data["etaSeconds"] = eta_seconds
        
        if detail:
            data["detail"] = detail
        
        self._emit(EventType.PROGRESS, data)
    
    def emit_log(self, message: str, level: str = "info") -> None:
        """Emit a log message (for debugging, shown in console)."""
        self._emit(EventType.LOG, {
            "message": message,
            "level": level  # "debug", "info", "verbose"
        })
    
    def emit_warning(self, message: str, code: Optional[str] = None) -> None:
        """Emit a warning (non-fatal issue)."""
        data = {"message": message}
        if code:
            data["code"] = code
        self._emit(EventType.WARNING, data)
    
    def emit_error(self, message: str, code: Optional[str] = None, fatal: bool = True) -> None:
        """
        Emit an error.
        
        Args:
            message: Human-readable error message
            code: Machine-readable error code (e.g., "FILE_NOT_FOUND")
            fatal: Whether this error stops processing
        """
        elapsed = None
        if self._start_time:
            elapsed = (datetime.now() - self._start_time).total_seconds()
        
        self._emit(EventType.ERROR, {
            "message": message,
            "code": code,
            "fatal": fatal,
            "elapsedSeconds": elapsed
        })
    
    def emit_success(self, output_paths: Dict[str, str], stats: Optional[Dict] = None) -> None:
        """
        Signal successful completion.
        
        Args:
            output_paths: Dict of output type to file path
                         e.g., {"vocals": "/path/vocals.wav", "instrumental": "/path/inst.wav"}
            stats: Optional processing statistics
        """
        elapsed = None
        if self._start_time:
            elapsed = (datetime.now() - self._start_time).total_seconds()
        
        data = {
            "outputs": output_paths,
            "elapsedSeconds": elapsed
        }
        
        if stats:
            data["stats"] = stats
        
        self._emit(EventType.SUCCESS, data)
    
    def emit_cancelled(self, reason: str = "User requested cancellation") -> None:
        """Signal that processing was cancelled."""
        elapsed = None
        if self._start_time:
            elapsed = (datetime.now() - self._start_time).total_seconds()
        
        self._emit(EventType.CANCELLED, {
            "reason": reason,
            "elapsedSeconds": elapsed,
            "lastStep": self._current_step.value if self._current_step else None
        })


# Global protocol instance
protocol = Protocol()
