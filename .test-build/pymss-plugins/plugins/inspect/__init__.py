"""Independent Studio inspect tool plugin."""


def _inspect_tool(request):
    from .backend.inspect import _inspect_audio

    return _inspect_audio(request)


def setup(plugin):
    from pymss.plugins.kit import register_tool

    register_tool(plugin, "pymss.inspect.tool", _inspect_tool, title="Audio information", tool="inspect")
