"""loudnorm plugin entry point."""

from .backend.operations import lufs_normalize, setup

__all__ = ['lufs_normalize', 'setup']
