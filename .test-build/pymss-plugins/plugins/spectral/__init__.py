"""spectral plugin entry point."""

from .backend.operations import highpass, lowpass, bandpass, notch, peak_eq, lowshelf, highshelf, parametric_eq, baxandall_eq, setup

__all__ = ['highpass', 'lowpass', 'bandpass', 'notch', 'peak_eq', 'lowshelf', 'highshelf', 'parametric_eq', 'baxandall_eq', 'setup']
