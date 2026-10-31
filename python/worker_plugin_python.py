"""Bind isolated Python profiles through the existing plugin transaction."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tomllib

BINDING = ".pymss-python.json"
_PROFILE = re.compile(r"python-[a-f0-9]{32}\Z")


def declares_python(plugin: Path) -> bool:
    project = tomllib.loads((plugin / "pyproject.toml").read_text(encoding="utf-8"))
    return "python" in project.get("tool", {}).get("pymss", {})


def prepare_python(plugin: Path, root: Path, progress) -> None:
    if not declares_python(plugin):
        return
    try:
        from pymss.operations import OperationContext
        from pymss.operations.python_profiles import prepare_python_environment
    except ImportError as error:
        raise ValueError("The active pymss version does not support isolated Python process plugins") from error

    # Source acquisition lives at transaction/sources/plugin-id. This journal
    # keeps a completed but not yet published profile alive during verification.
    root, plugin = root.resolve(), plugin.resolve()
    transaction = next((parent for parent in plugin.parents if parent.parent == root / "transactions"), None)
    if transaction is None or not transaction.name.startswith("install-"):
        raise ValueError("Python environments must be prepared inside a plugin installation transaction")

    def created(profile: Path):
        journal = transaction / ("python-profile-" + profile.name + ".json")
        temporary = journal.with_suffix(".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump({"profile": str(profile)}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, journal)
        finally:
            temporary.unlink(missing_ok=True)

    context = OperationContext(progress=lambda fraction, message, event=None: progress(message))
    prepare_python_environment(plugin, root / "python-profiles", context=context, on_created=created)


def collect_python_profiles(root: Path) -> None:
    """Collect only this runtime's idle, unreferenced managed environments.

    Call under the existing root writer lock, after transaction recovery. Reads
    do not import pymss or perform this potentially expensive cleanup.
    """
    root = root.resolve()
    store = root / "python-profiles"
    if not store.is_dir():
        return
    # A downgraded kernel can still inspect the old installation. It must not
    # guess how to remove a profile owned by a newer kernel.
    try:
        from pymss.operations import OperationError
        from pymss.operations.python_profiles import remove_python_environment
    except ImportError:
        return

    referenced: set[Path] = set()
    receipts = []
    for name in ("installed", "disabled", "backups", "disabled-backups"):
        directory = root / name
        if directory.is_dir():
            receipts.extend(directory.glob("*/" + BINDING))
    transactions = root / "transactions"
    if transactions.is_dir():
        receipts.extend(transactions.glob("install-*/python-profile-*.json"))
    for receipt in receipts:
        try:
            if receipt.is_symlink() or receipt.stat().st_size > 256 * 1024:
                return
            data = json.loads(receipt.read_bytes())
            profile = Path(data["profile"]).resolve()
            if profile.parent == store and _PROFILE.fullmatch(profile.name):
                referenced.add(profile)
            else:
                return
        except (OSError, ValueError, TypeError, KeyError):
            # A damaged receipt cannot justify deleting a potentially live binding.
            return
    candidates = set()
    for path in store.iterdir():
        name = path.name
        if name.startswith(".creating-"):
            name = name.removeprefix(".creating-")
        elif name.startswith(".python-") and name.endswith(".retired.json"):
            name = name[1:-len(".retired.json")]
        if _PROFILE.fullmatch(name):
            candidates.add(store / name)
    for profile in candidates:
        if profile not in referenced and not profile.is_symlink():
            try:
                remove_python_environment(profile)
            except (OSError, OperationError):
                # Reclamation is retried on the next management action; it
                # cannot undo a successfully published plugin binding.
                from worker_protocol import emit

                emit("plugin_log", {"message": "An unused Python environment could not be removed; cleanup will be retried"})
