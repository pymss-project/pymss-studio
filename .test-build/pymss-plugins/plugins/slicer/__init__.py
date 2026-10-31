"""Independent Studio slicer tool plugin."""


def _slicer_tool(request):
    from .backend.slicer import _slice_audio

    return _slice_audio(request)


def setup(plugin):
    from pymss.plugins.kit import register_tool

    register_tool(plugin, "pymss.slicer.tool", _slicer_tool, title="Audio slicing", tool="slicer")
