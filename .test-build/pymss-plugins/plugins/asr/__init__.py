"""asr plugin entry point."""

from .backend.operations import asr_transcribe, setup

__all__ = ['asr_transcribe', 'setup']
