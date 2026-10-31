"""Shared string identifiers for plugin manifests and executable operations."""

import re

_SEGMENT = r"[a-z0-9][a-z0-9_-]*"
PLUGIN_ID_RE = re.compile(rf"{_SEGMENT}(?:\.{_SEGMENT})*\Z")
OPERATION_ID_RE = re.compile(rf"{_SEGMENT}(?:\.{_SEGMENT})+\Z")
