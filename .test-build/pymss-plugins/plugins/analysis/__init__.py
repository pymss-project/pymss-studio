"""analysis plugin entry point."""

from .backend.operations import measure_lufs, measure_true_peak, measure_rms, measure_dynamic_range, spectrum_analyze, setup

__all__ = ['measure_lufs', 'measure_true_peak', 'measure_rms', 'measure_dynamic_range', 'spectrum_analyze', 'setup']
