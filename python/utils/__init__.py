"""
Utils Package
=============
Utility modules for the Vocal Remover motor.
"""

from .protocol import Protocol, protocol, EventType, ProcessingStep
from .signal_handler import (
    SignalHandler, 
    signal_handler, 
    CancellationToken, 
    CancelledException,
    ShutdownReason
)

__all__ = [
    'Protocol',
    'protocol',
    'EventType', 
    'ProcessingStep',
    'SignalHandler',
    'signal_handler',
    'CancellationToken',
    'CancelledException',
    'ShutdownReason'
]