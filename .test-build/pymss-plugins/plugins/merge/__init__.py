"""Independent Studio merge tool plugin."""


def _merge_tool(request):
    from .backend.merge import _merge_audio

    return _merge_audio(request)


def setup(plugin):
    from pymss.plugins.kit import register_tool

    register_tool(plugin, "pymss.merge.tool", _merge_tool, title="Audio merge", tool="merge")
