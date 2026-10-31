"""Read the Studio host requirements shared by catalog and page builds."""

from __future__ import annotations

import re


def studio_requirements(studio: dict) -> dict:
    sdk_version = studio.get("sdk_version")
    if (studio.get("views") or sdk_version is not None) and (
        type(sdk_version) is not int or sdk_version < 1
    ):
        raise ValueError("Studio sdk_version must be a positive integer")
    result = {"sdkVersion": sdk_version}
    for field, output, allowed in (
        ("required_capabilities", "requiredCapabilities", None),
        ("platforms", "platforms", {"win32", "linux", "darwin"}),
        ("architectures", "architectures", {"x86_64", "arm64"}),
        ("backends", "backends", {"torch", "mlx"}),
    ):
        values = studio.get(field, [])
        if not isinstance(values, list) or not all(
            isinstance(value, str)
            and (value in allowed if allowed else re.fullmatch(r"[a-z][A-Za-z0-9]*(?:\.[a-z][A-Za-z0-9]*)+", value))
            for value in values
        ):
            raise ValueError(f"Invalid Studio {field}")
        if len(values) != len(set(values)):
            raise ValueError(f"Duplicate Studio {field} entries")
        result[output] = values
    return result
