"""
Signal Handler Module - Graceful Shutdown Support
=================================================
Handles SIGTERM/SIGINT signals for clean cancellation of processing.
Works on both Windows and Unix systems.
"""

import signal
import sys
import threading
from typing import Callable, Optional, List
from enum import Enum


class ShutdownReason(Enum):
    """Reasons for shutdown."""
    USER_CANCELLED = "user_cancelled"
    SIGTERM = "sigterm"
    SIGINT = "sigint"
    TIMEOUT = "timeout"
    PARENT_DIED = "parent_died"


class SignalHandler:
    """
    Manages graceful shutdown of the processing pipeline.
    
    Usage:
        handler = SignalHandler()
        handler.register()
        
        # In your processing loop:
        if handler.should_stop:
            cleanup_and_exit()
    """
    
    def __init__(self):
        self._should_stop: bool = False
        self._shutdown_reason: Optional[ShutdownReason] = None
        self._cleanup_callbacks: List[Callable] = []
        self._lock = threading.Lock()
        self._registered: bool = False
    
    @property
    def should_stop(self) -> bool:
        """Check if processing should stop. Thread-safe."""
        with self._lock:
            return self._should_stop
    
    @property
    def shutdown_reason(self) -> Optional[ShutdownReason]:
        """Get the reason for shutdown, if any."""
        with self._lock:
            return self._shutdown_reason
    
    def request_stop(self, reason: ShutdownReason = ShutdownReason.USER_CANCELLED) -> None:
        """
        Request graceful stop of processing.
        
        This sets the flag that processing loops should check.
        Does NOT immediately terminate - allows cleanup.
        """
        with self._lock:
            if not self._should_stop:
                self._should_stop = True
                self._shutdown_reason = reason
    
    def register_cleanup(self, callback: Callable) -> None:
        """
        Register a cleanup callback to run on shutdown.
        
        Callbacks are run in LIFO order (last registered, first called).
        """
        with self._lock:
            self._cleanup_callbacks.append(callback)
    
    def run_cleanup(self) -> None:
        """Execute all registered cleanup callbacks."""
        with self._lock:
            callbacks = list(reversed(self._cleanup_callbacks))
        
        for callback in callbacks:
            try:
                callback()
            except Exception as e:
                # Log but don't fail on cleanup errors
                sys.stderr.write(f"Cleanup callback error: {e}\n")
    
    def _handle_signal(self, signum: int, frame) -> None:
        """Internal signal handler."""
        reason = ShutdownReason.SIGTERM if signum == signal.SIGTERM else ShutdownReason.SIGINT
        self.request_stop(reason)
        
        # Note: We don't exit here - we let the main loop detect should_stop
        # and perform graceful cleanup
    
    def register(self) -> None:
        """
        Register signal handlers for SIGTERM and SIGINT.
        
        On Windows, SIGTERM might not be available, so we handle that gracefully.
        """
        if self._registered:
            return
        
        try:
            # SIGINT (Ctrl+C) - available on all platforms
            signal.signal(signal.SIGINT, self._handle_signal)
        except (OSError, ValueError):
            pass  # May fail in some embedded contexts
        
        try:
            # SIGTERM - standard termination signal (Unix) 
            # On Windows, this is available but rarely used
            signal.signal(signal.SIGTERM, self._handle_signal)
        except (OSError, ValueError, AttributeError):
            pass  # SIGTERM might not be available on Windows
        
        # Windows-specific: Handle CTRL_BREAK_EVENT and CTRL_CLOSE_EVENT
        if sys.platform == "win32":
            try:
                self._register_windows_handlers()
            except Exception:
                pass  # Non-critical, signal.SIGINT usually works
        
        self._registered = True
    
    def _register_windows_handlers(self) -> None:
        """Register Windows-specific console control handlers."""
        import ctypes
        from ctypes import wintypes
        
        # Define the handler type
        HANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        
        # Control event types
        CTRL_C_EVENT = 0
        CTRL_BREAK_EVENT = 1
        CTRL_CLOSE_EVENT = 2
        
        def console_handler(event: int) -> bool:
            if event in (CTRL_C_EVENT, CTRL_BREAK_EVENT, CTRL_CLOSE_EVENT):
                self.request_stop(ShutdownReason.USER_CANCELLED)
                return True  # Signal handled
            return False
        
        # Keep reference to prevent garbage collection
        self._windows_handler = HANDLER_ROUTINE(console_handler)
        
        # Register the handler
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleCtrlHandler(self._windows_handler, True)
    
    def check_parent_alive(self) -> bool:
        """
        Check if parent process (Electron) is still alive.
        
        If parent dies, we should terminate to avoid orphan processes.
        Returns True if parent is alive, False otherwise.
        """
        if sys.platform == "win32":
            return self._check_parent_windows()
        else:
            return self._check_parent_unix()
    
    def _check_parent_windows(self) -> bool:
        """Windows-specific parent check."""
        try:
            import ctypes
            from ctypes import wintypes
            
            kernel32 = ctypes.windll.kernel32
            
            # Get parent process ID
            # Note: This is simplified - in production you'd pass parent PID as argument
            return True  # Assume alive if we can't check
        except Exception:
            return True
    
    def _check_parent_unix(self) -> bool:
        """Unix-specific parent check using ppid."""
        import os
        try:
            # On Unix, if parent dies, we get reparented to init (pid 1)
            return os.getppid() != 1
        except Exception:
            return True


class CancellationToken:
    """
    A token that can be passed to long-running operations
    to allow them to check for cancellation.
    
    Similar to .NET's CancellationToken pattern.
    """
    
    def __init__(self, signal_handler: SignalHandler):
        self._handler = signal_handler
    
    @property
    def is_cancelled(self) -> bool:
        """Check if cancellation was requested."""
        return self._handler.should_stop
    
    def throw_if_cancelled(self) -> None:
        """Raise CancelledException if cancellation was requested."""
        if self.is_cancelled:
            raise CancelledException(self._handler.shutdown_reason)
    
    def check_and_report(self, protocol) -> bool:
        """
        Check cancellation and emit protocol message if cancelled.
        
        Returns True if cancelled, False otherwise.
        """
        if self.is_cancelled:
            reason = self._handler.shutdown_reason
            protocol.emit_cancelled(
                reason=reason.value if reason else "Unknown"
            )
            return True
        return False


class CancelledException(Exception):
    """Exception raised when operation is cancelled."""
    
    def __init__(self, reason: Optional[ShutdownReason] = None):
        self.reason = reason
        message = f"Operation cancelled: {reason.value if reason else 'Unknown'}"
        super().__init__(message)


# Global signal handler instance
signal_handler = SignalHandler()
