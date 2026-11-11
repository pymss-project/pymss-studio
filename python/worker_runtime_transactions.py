"""Recoverable ROCm directory swaps. Callers must hold the runtime management lock."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any


class RuntimeRecoveryError(RuntimeError):
    pass


class RuntimeSpaceError(RuntimeError):
    pass


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeRecoveryError(f"Invalid runtime transaction metadata: {path.name}")
    return value


def _atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write(path: Path, value: dict[str, Any]) -> None:
    _atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _snapshot(path: Path) -> str | None:
    return base64.b64encode(path.read_bytes()).decode("ascii") if path.exists() else None


def _restore(path: Path, content: str | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
    else:
        _atomic(path, base64.b64decode(content, validate=True))


class RocmTransaction:
    def __init__(self, root: Path, active_file: Path):
        self._configured_root = root
        self.root = root.resolve()
        self.active = active_file.absolute()
        self.current = self.root / "rocm"
        self.previous = self.root / ".rocm-previous"
        self.journal = self.root / ".rocm-transaction.json"
        self.record: dict[str, Any] | None = None

    def checked(self, path: Path) -> Path:
        """Only direct owned paths; never traverse junctions or directory symlinks."""
        # Keep the configured spelling for lexical validation: Windows may expand
        # an 8.3 root in resolve(). Resolve only after checking child links below.
        for root in (self.root, self._configured_root):
            try:
                relative = path.relative_to(root)
                break
            except ValueError:
                continue
        else:
            raise RuntimeRecoveryError("Runtime transaction path escapes the managed root")
        candidate = self.root
        for part in relative.parts:
            candidate /= part
            if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
                raise RuntimeRecoveryError("Runtime transaction paths cannot contain links or junctions")
        if not path.resolve().is_relative_to(self.root):
            raise RuntimeRecoveryError("Runtime transaction path escapes the managed root")
        return candidate

    def _active_checked(self) -> Path:
        # The active pointer may be explicitly configured outside the runtime root.
        # Its location comes from process configuration, never from the journal.
        if self.active.is_symlink():
            raise RuntimeRecoveryError("The active runtime pointer cannot be a symbolic link")
        return self.active

    def _slot(self, kind: str) -> Path:
        if not self.record or not re.fullmatch(r"[0-9a-f]{32}", str(self.record.get("id"))):
            raise RuntimeRecoveryError("Invalid runtime transaction identifier")
        return self.checked(self.root / f".rocm-{kind}-{self.record['id']}")

    def _selections(self) -> list[Path]:
        directory = self.checked(self.root / ".overlays/rocm")
        return [self.checked(path) for path in directory.glob("active-overlay*.json")
                if re.fullmatch(r"active-overlay(?:-[0-9a-f]{24})?\.json", path.name)]

    def snapshot(self) -> dict[str, Any]:
        return {
            "active": _snapshot(self._active_checked()),
            "selections": {path.relative_to(self.root).as_posix(): _snapshot(path) for path in self._selections()},
        }

    def restore_selections(self, snapshot: dict[str, Any]) -> None:
        desired = snapshot.get("selections")
        if not isinstance(desired, dict):
            raise RuntimeRecoveryError("Invalid runtime overlay snapshot")
        paths = {}
        for name, content in desired.items():
            if not re.fullmatch(r"\.overlays/rocm/active-overlay(?:-[0-9a-f]{24})?\.json", name):
                raise RuntimeRecoveryError("Invalid runtime overlay snapshot path")
            if not isinstance(content, str):
                raise RuntimeRecoveryError("Invalid runtime overlay snapshot content")
            base64.b64decode(content, validate=True)
            paths[self.checked(self.root / name)] = content
        for path in self._selections():
            if path not in paths:
                path.unlink()
        for path, content in paths.items():
            _restore(path, content)

    def previous_manifest(self) -> dict[str, Any] | None:
        if not self.checked(self.previous).exists():
            return None
        try:
            manifest = _json(self.checked(self.previous / "manifest.json"))
        except (OSError, ValueError) as error:
            raise RuntimeRecoveryError("The previous ROCm environment metadata could not be read") from error
        if manifest.get("version") != 1 or manifest.get("originalPath") != "rocm" or not re.fullmatch(r"[0-9a-f]{32}", str(manifest.get("slotId"))):
            raise RuntimeRecoveryError("The previous ROCm environment manifest is not recognized")
        if not self.checked(self.previous / "environment").is_dir():
            raise RuntimeRecoveryError("The previous ROCm environment is incomplete")
        return manifest

    def preflight(self, current_bytes: int, previous_bytes: int, estimated_bytes: int | None = None) -> dict[str, int]:
        # Free space already excludes both existing environments. Budget a fresh
        # environment plus download/unpack space without deleting either one.
        candidate = max(current_bytes, estimated_bytes or 0, 1024 ** 3)
        required = candidate * 2 + 128 * 1024 ** 2
        free = shutil.disk_usage(self.root).free
        if free < required:
            raise RuntimeSpaceError(f"Insufficient disk space for a recoverable ROCm installation: required={required}, available={free}")
        return {"requiredFreeBytes": required, "availableBytes": free,
                "estimatedPeakBytes": current_bytes + previous_bytes + required}

    def begin(self, operation: str, *, activate: bool, profile: dict[str, Any] | None = None,
              replace_previous: bool = False) -> dict[str, Any]:
        if operation not in {"install", "rollback"}:
            raise ValueError("Unsupported runtime environment operation")
        if self.checked(self.journal).exists():
            raise RuntimeRecoveryError("A runtime transaction must be recovered before starting another operation")
        previous = self.previous_manifest()
        if operation == "rollback" and previous is None:
            raise RuntimeRecoveryError("No previous ROCm environment is available")
        if operation == "install" and previous and not replace_previous:
            raise ValueError("The existing rollback version must be explicitly selected for replacement")
        if self.checked(self.current).exists() and not self.current.is_dir():
            raise RuntimeRecoveryError("The ROCm environment path is not a directory")
        self.record = {
            "version": 1, "id": uuid.uuid4().hex, "operation": operation,
            "activate": activate, "hadCurrent": self.current.exists(),
            "snapshot": self.snapshot(), "phase": "prepared", "profile": profile,
            "previousSource": previous,
        }
        self.root.mkdir(parents=True, exist_ok=True)
        _write(self.checked(self.journal), self.record)
        backup = self._slot("backup")
        backup.mkdir()
        state = self.current / "pymss-runtime-state.json"
        manifest = {
            "version": 1, "slotId": self.record["id"], "originalPath": "rocm",
            "snapshot": self.record["snapshot"], "profile": profile,
            "state": _json(state) if state.is_file() else None,
            "stateSnapshot": _snapshot(state),
        }
        _write(backup / "manifest.json", manifest)
        if self.record["hadCurrent"]:
            _write(self.checked(self.current / ".pymss-runtime-origin.json"), {"transactionId": self.record["id"]})
            self.current.rename(backup / "environment")
        self.record["phase"] = "building"
        _write(self.journal, self.record)
        if operation == "rollback":
            (self.previous / "environment").rename(self.current)
            self.restore_selections(previous["snapshot"])
        return self.record

    def copy_current_candidate(self) -> None:
        """Build an independent candidate; pip must never mutate rollback hardlinks."""
        if not self.record or not self.record["hadCurrent"] or self.record["operation"] != "install":
            raise RuntimeRecoveryError("An existing runtime is required for a target addition")
        source = self.checked(self._slot("backup") / "environment")
        if self.checked(self.current).exists():
            raise RuntimeRecoveryError("The runtime candidate directory already exists")
        # Reject directory links instead of accidentally copying outside the owned base.
        for root, directories, files in os.walk(source, followlinks=False):
            for name in [*directories, *files]:
                path = Path(root) / name
                if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                    raise RuntimeRecoveryError("The source runtime contains links that cannot be copied safely")
        shutil.copytree(source, self.current, copy_function=shutil.copy2)
        (self.current / ".pymss-runtime-origin.json").unlink(missing_ok=True)
        (self.current / ".pymss-runtime-commit.json").unlink(missing_ok=True)

    def _candidate_matches(self) -> bool:
        try:
            state = _json(self.checked(self.current / "pymss-runtime-state.json"))
            return state.get("environmentTransactionId") == self.record["id"]
        except (OSError, ValueError):
            return False

    def _committed(self) -> bool:
        if not self._candidate_matches():
            return False
        try:
            if not self.record["activate"]:
                marker = _json(self.checked(self.current / ".pymss-runtime-commit.json"))
                return marker.get("transactionId") == self.record["id"]
            pointer = _json(self._active_checked())
            python = Path(pointer["pythonPath"])
            if not python.is_absolute():
                python = self.active.parent / python
            return (pointer.get("environmentTransactionId") == self.record["id"]
                    and pointer.get("backend") == "rocm" and python.is_file()
                    and python.parent.name.lower() in {"scripts", "bin"}
                    and python.parent.parent.samefile(self.current))
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def commit(self, active: dict[str, Any] | None) -> None:
        if not self.record or not self._candidate_matches():
            raise RuntimeRecoveryError("The candidate runtime state does not match its transaction")
        if self.record["activate"]:
            if not active or active.get("environmentTransactionId") != self.record["id"]:
                raise RuntimeRecoveryError("The runtime pointer does not match its transaction")
            _write(self._active_checked(), active)
        else:
            _write(self.checked(self.current / ".pymss-runtime-commit.json"), {"transactionId": self.record["id"]})
        if not self._committed():
            raise RuntimeRecoveryError("The runtime commit could not be verified")
        self.record["phase"] = "committed"
        _write(self.journal, self.record)
        self._finish_commit()

    def commit_prepared(self) -> bool:
        """Commit a first dependency-ready generation while preserving selection.

        False leaves the transaction open when an old dangling pointer would
        otherwise start selecting the new canonical environment automatically.
        """
        if (not self.record or self.record["operation"] != "install" or self.record["hadCurrent"]
                or not self._candidate_matches()):
            raise RuntimeRecoveryError("Only a first validated runtime can be retained without activation")
        if _snapshot(self._active_checked()) != self.record["snapshot"]["active"]:
            raise RuntimeRecoveryError("The active pointer changed outside the transaction; all copies were preserved")
        try:
            pointer = _json(self._active_checked())
            raw = pointer.get("pythonPath")
            if isinstance(raw, str) and raw:
                if raw.startswith("\\\\?\\UNC\\"):
                    raw = "\\\\" + raw[len("\\\\?\\UNC\\"):]
                elif raw.startswith("\\\\?\\"):
                    raw = raw[len("\\\\?\\"):]
                python = Path(raw)
                if not python.is_absolute():
                    python = self.active.parent / python
                if python.resolve().is_relative_to(self.current.resolve()):
                    return False
        except (FileNotFoundError, ValueError, TypeError):
            # Missing/malformed pointers do not select an executable environment.
            pass
        # Persist the changed commit contract before writing its marker. Recovery
        # must never interpret a prepared generation as an interrupted activation.
        self.restore_selections(self.record["snapshot"])
        self.record["activate"] = False
        _write(self.journal, self.record)
        self.commit(None)
        return True

    def _finish_commit(self) -> None:
        backup, retired = self._slot("backup"), self._slot("retired")
        if self.record["hadCurrent"]:
            if self.checked(backup / "environment").is_dir():
                if self.checked(self.previous).exists():
                    if retired.exists():
                        raise RuntimeRecoveryError("Both the previous and retired rollback slots exist")
                    self.previous.rename(retired)
                backup.rename(self.previous)
            else:
                manifest = self.previous_manifest()
                if not manifest or manifest.get("slotId") != self.record["id"]:
                    raise RuntimeRecoveryError("The committed runtime's previous environment is missing")
        elif backup.exists():
            shutil.rmtree(backup)
        if not self.record["hadCurrent"] and self.record["operation"] == "rollback" and self.previous.exists():
            if self.checked(self.previous / "environment").exists():
                raise RuntimeRecoveryError("The rollback source was not consumed")
            shutil.rmtree(self.previous)
        if retired.exists():
            shutil.rmtree(retired)
        self.checked(self.journal).unlink()
        self.record = None

    def _restore_rollback_source(self) -> None:
        destination = self.checked(self.previous / "environment")
        if destination.exists():
            raise RuntimeRecoveryError("The rollback source is ambiguous; all copies were preserved")
        source = self.record.get("previousSource") or {}
        if "stateSnapshot" in source:
            _restore(self.checked(self.current / "pymss-runtime-state.json"), source["stateSnapshot"])
        self.current.rename(destination)

    def abort(self) -> None:
        if not self.record:
            return
        if self._committed():
            # Pointer replacement is the commit point, even if the following
            # journal update or backup rotation failed.
            self._finish_commit()
            return
        if self.record.get("phase") == "committed":
            raise RuntimeRecoveryError("The committed runtime has an inconsistent pointer; all copies were preserved")
        if _snapshot(self._active_checked()) != self.record["snapshot"]["active"]:
            raise RuntimeRecoveryError("The active pointer changed outside the transaction; all copies were preserved")
        backup, failed = self._slot("backup"), self._slot("failed")
        original = self.checked(backup / "environment")
        if original.exists():
            if self.checked(self.current).exists():
                if self.record["operation"] == "rollback":
                    self._restore_rollback_source()
                else:
                    if failed.exists():
                        raise RuntimeRecoveryError("The failed runtime slot is already occupied")
                    self.current.rename(failed)
            original.rename(self.current)
        elif self.record["hadCurrent"]:
            # A crash may occur just after restoring the original directory.
            # The marker proves which generation now occupies the canonical path.
            marker = self.current / ".pymss-runtime-origin.json"
            if self.record["phase"] != "prepared" and (
                not marker.is_file() or _json(self.checked(marker)).get("transactionId") != self.record["id"]
            ):
                raise RuntimeRecoveryError("The original runtime backup is missing; all copies were preserved")
        elif self.current.exists():
            if self.record["operation"] == "rollback":
                self._restore_rollback_source()
            else:
                self.current.rename(failed)
        self.restore_selections(self.record["snapshot"])
        _restore(self._active_checked(), self.record["snapshot"]["active"])
        # Remove only owned candidate directories after the old runtime and
        # pointer have been restored. Failed cleanup leaves the journal retryable.
        if failed.exists():
            shutil.rmtree(failed)
        if backup.exists():
            shutil.rmtree(backup)
        self.checked(self.journal).unlink()
        self.record = None

    def recover(self) -> bool:
        if not self.checked(self.journal).exists():
            return False
        try:
            self.record = _json(self.journal)
        except (OSError, ValueError) as error:
            raise RuntimeRecoveryError("The runtime transaction metadata could not be read") from error
        snapshot = self.record.get("snapshot")
        if (self.record.get("version") != 1 or self.record.get("operation") not in {"install", "rollback"}
                or not isinstance(self.record.get("activate"), bool)
                or not isinstance(self.record.get("hadCurrent"), bool)
                or not isinstance(snapshot, dict) or "active" not in snapshot
                or not isinstance(snapshot.get("active"), (str, type(None)))
                or not isinstance(snapshot.get("selections"), dict)):
            raise RuntimeRecoveryError("The runtime transaction format is not recognized")
        self._slot("backup")  # Validate the identifier before reading any owned slot.
        self.abort()
        return True
