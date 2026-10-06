#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _load(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def runtime_base_compatibility(
    previous: dict[str, Any],
    current: dict[str, Any],
    *,
    base_layout_changed: bool = False,
) -> tuple[bool, str]:
    """Compare the base contract; layout/ABI changes must increment baseGeneration.

    Preparation scripts can add dependencies or probe backends without changing
    that contract. Pruning changes still require a complete base replacement.
    """
    previous_generation = int(previous.get("baseGeneration", 1))
    current_generation = int(current.get("baseGeneration", 1))
    if previous_generation != current_generation:
        return False, f"base generation changed: {previous_generation} -> {current_generation}"
    # Releases predating baseGeneration form the one-time bridge into generation 1.
    if base_layout_changed and "baseGeneration" in previous:
        return False, "base runtime build scripts changed without increasing baseGeneration"
    if previous.get("python") != current.get("python"):
        return False, f"Python ABI changed: {previous.get('python')} -> {current.get('python')}"

    previous_backends = previous.get("backends") or {}
    current_backends = current.get("backends") or {}
    for backend, previous_spec in previous_backends.items():
        current_spec = current_backends.get(backend)
        if not isinstance(current_spec, dict):
            return False, f"installed backend support was removed: {backend}"
        previous_spec = previous_spec if isinstance(previous_spec, dict) else {}
        if previous_spec.get("python") != current_spec.get("python"):
            return False, f"{backend} Python ABI changed"
        if previous_spec.get("torch") != current_spec.get("torch"):
            return False, f"{backend} base accelerator packages changed"

    return True, "base runtime is reusable; core dependencies can migrate through an overlay"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=Path(argv[0]).name)
    parser.add_argument("--base-layout-changed", action="store_true")
    parser.add_argument("previous_manifest")
    parser.add_argument("current_manifest")
    try:
        args = parser.parse_args(argv[1:])
        compatible, reason = runtime_base_compatibility(
            _load(args.previous_manifest),
            _load(args.current_manifest),
            base_layout_changed=args.base_layout_changed,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Unable to compare runtime manifests: {exc}", file=sys.stderr)
        return 2
    print(reason)
    return 0 if compatible else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
