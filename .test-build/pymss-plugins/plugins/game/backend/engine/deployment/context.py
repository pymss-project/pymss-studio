from contextlib import contextmanager

_is_export_mode = False


def is_export_mode():
    return _is_export_mode


@contextmanager
def export_mode(enabled: bool = True):
    global _is_export_mode
    prev_mode = _is_export_mode
    _is_export_mode = enabled
    try:
        yield
    finally:
        _is_export_mode = prev_mode
