"""Independent Studio sdr tool plugin."""


def _sdr_tool(request):
    from .backend.sdr import _calculate_sdr

    return _calculate_sdr(request)


def setup(plugin):
    from pymss.plugins.kit import register_tool

    register_tool(plugin, "pymss.sdr.tool", _sdr_tool, title="SDR evaluation", tool="sdr")
