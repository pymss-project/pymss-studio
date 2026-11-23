"""Import and manage user-supplied ("custom") separation models.

pymss already owns the registry (``pymss.register_model`` / ``list_user_models`` and friends,
backed by ``user_models.json``). This module is the desktop-facing layer on top of it:

* architecture detection, so the user is offered a suggestion instead of a bare list of 16 types
* an import pipeline that can copy files under app management, and that verifies a registration
  by really loading it before writing anything
* a side-car for the bits pymss's registry does not store (how the model got here)

The registry stays pymss's: a model registered from the command line with ``pymss register``
shows up in the app too, just without side-car detail.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from worker_protocol import debug_log, emit, emit_error

# Weights are loaded with ``torch.load`` (see pymss_core/checkpoint.py), so only formats it reads
# are offered. Notably absent: .safetensors — pymss has no safetensors path at all, and accepting
# one would only fail later with a confusing error.
MODEL_FILE_EXTENSIONS = (".ckpt", ".pth", ".th", ".pt", ".bin")
CONFIG_FILE_EXTENSIONS = (".yaml", ".yml")

SIDECAR_FILENAME = "custom-models-meta.json"


def custom_model_registry_path() -> Path:
    return Path(
        os.environ.get("PYMSS_USER_MODELS") or Path.home() / ".cache" / "pymss" / "user_models.json"
    ).expanduser()


def read_custom_model_registry(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "models": []}
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise ValueError(f"Invalid user model registry: {path}")
    return data


def _sidecar_path() -> Path:
    """Where the app keeps its own per-import metadata.

    Deliberately beside the registry rather than inside it: ``UserModelEntry.to_dict()`` only
    serialises the fields pymss knows about, so anything extra written there is silently dropped
    on the next registration."""
    override = os.environ.get("PYMSS_STUDIO_CUSTOM_MODEL_META")
    if override:
        return Path(override)
    return custom_model_registry_path().parent / SIDECAR_FILENAME


def _read_sidecar() -> dict[str, Any]:
    path = _sidecar_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except Exception:
        return {}
    models = data.get("models") if isinstance(data, dict) else None
    return models if isinstance(models, dict) else {}


def _write_atomic_json(path: Path, value: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write_sidecar(models: dict[str, Any]) -> bool:
    path = _sidecar_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic_json(path, {"version": 1, "models": models})
    except OSError as error:
        debug_log("custom_model.ownership_write_failed", errorType=type(error).__name__)
        return False
    return True


def sidecar_entry(name: str) -> dict[str, Any]:
    entry = _read_sidecar().get(str(name), {})
    return entry if isinstance(entry, dict) else {}


def _remember_import(name: str, import_mode: str, managed_dir: Path | None) -> None:
    models = _read_sidecar()
    models[str(name)] = {
        "importMode": import_mode,
        "importedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "managedDir": str(managed_dir) if managed_dir else None,
    }
    _write_sidecar(models)


def _forget_import(name: str) -> dict[str, Any]:
    models = _read_sidecar()
    removed = models.pop(str(name), {})
    _write_sidecar(models)
    return removed


# --------------------------------------------------------------------------------------
# Architecture detection
# --------------------------------------------------------------------------------------

# Config keys that identify an architecture, taken from the constructor signatures pymss_core
# actually calls in get_model_from_config(). Each key below is unique to its architecture across
# all of them, so a single hit is conclusive:
#
#   BSRoformer(freqs_per_bands=...)          MelBandRoformer(num_bands=...)
#   SCNet(band_SR=...)                       TFC_TDF_net(config.model.bottleneck_factor)
#   MultiMaskMultiSourceBandSplitRNNSimple(band_specs=...)   Apollo(feature_dim=...)
#
# Order matters only for reporting; the key sets are disjoint.
_CONFIG_MODEL_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bs_roformer", ("freqs_per_bands",)),
    ("mel_band_roformer", ("num_bands",)),
    ("scnet", ("band_SR", "band_stride", "band_kernel")),
    ("mdx23c", ("bottleneck_factor", "num_subbands")),
    ("bandit", ("band_specs",)),
    ("apollo", ("feature_dim",)),
)

# State-dict markers. Fewer, because reading weights is best-effort and only two things can be
# told apart this way with certainty:
#   '.segm.' -> pymss itself promotes bs_roformer to bs_roformer_hyperace on this basis
#               (_runtime_model_type in pymss/separator.py), so it is authoritative.
#   'stg1_low_band_net' -> the attribute name of both VR network generations
#               (vr_network/nets.py and nets_new.py), and VR takes no YAML config at all.
_STATE_DICT_SUBSTRING_MARKERS: tuple[tuple[str, str], ...] = (
    ("bs_roformer_hyperace", ".segm."),
    ("vr", "stg1_low_band_net"),
)


def _suggestion(model_type: str, confidence: str, basis_code: str, basis_detail: str = "") -> dict[str, Any]:
    return {
        "modelType": model_type,
        "confidence": confidence,
        # A code rather than a sentence: the UI has to translate this, and an English string
        # baked in here could not be localised.
        "basisCode": basis_code,
        "basisDetail": basis_detail,
    }


def detect_from_config(config: Any) -> list[dict[str, Any]]:
    """Architecture suggestions derived from a parsed YAML config.

    The config is the stronger of the two signals: 11 of the 16 registrable types require one,
    and the keys it must carry are exactly the constructor arguments of a single architecture."""
    if not isinstance(config, dict):
        return []
    # bandit_v2 is the only architecture built from config.kwargs instead of config.model, which
    # makes the top-level shape alone conclusive.
    if isinstance(config.get("kwargs"), dict):
        return [_suggestion("bandit_v2", "high", "config_kwargs_section", "kwargs")]
    model_section = config.get("model")
    if not isinstance(model_section, dict):
        return []
    suggestions: list[dict[str, Any]] = []
    for model_type, markers in _CONFIG_MODEL_MARKERS:
        hit = next((marker for marker in markers if marker in model_section), None)
        if hit:
            suggestions.append(_suggestion(model_type, "high", "config_model_key", hit))
    return suggestions


def detect_from_state_dict_keys(keys: Any) -> list[dict[str, Any]]:
    """Architecture suggestions derived from checkpoint tensor names."""
    names = [str(key) for key in (keys or ())]
    if not names:
        return []
    suggestions: list[dict[str, Any]] = []
    for model_type, needle in _STATE_DICT_SUBSTRING_MARKERS:
        if any(needle in name for name in names):
            suggestions.append(_suggestion(model_type, "high", "state_dict_key", needle))
    return suggestions


def merge_suggestions(
    config_suggestions: list[dict[str, Any]],
    state_dict_suggestions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Combine both signals, most trustworthy first.

    Config findings lead because the YAML describes the architecture pymss will construct.
    State-dict findings remain useful for config-optional models and refinements the config cannot
    express. Duplicates collapse to their first, highest-ranked occurrence."""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*config_suggestions, *state_dict_suggestions]:
        model_type = str(item.get("modelType") or "")
        if not model_type or model_type in seen:
            continue
        seen.add(model_type)
        merged.append(item)
    # A hyperace checkpoint is still a bs_roformer as far as registration goes — pymss promotes
    # it at load time — so keep the family as an explicit second choice rather than dropping it.
    return merged


def _known_model_types() -> tuple[list[str], list[str]]:
    """The registrable architectures, straight from pymss.

    Read at runtime rather than copied: the app must not offer a type this pymss build would
    reject, nor hide one it gained."""
    from pymss.user_models import CONFIG_OPTIONAL_MODEL_TYPES, KNOWN_MODEL_TYPES  # type: ignore
    return sorted(KNOWN_MODEL_TYPES), sorted(CONFIG_OPTIONAL_MODEL_TYPES)


def _read_config(config_path: Path | None) -> dict[str, Any]:
    if not config_path or not config_path.is_file():
        return {}
    try:
        import yaml

        with config_path.open(encoding="utf-8") as handle:
            data = yaml.load(handle, Loader=yaml.FullLoader)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_state_dict_keys(model_path: Path) -> tuple[list[str], str | None]:
    """Tensor names from a checkpoint, or the reason they could not be read.

    Memory-mapped and CPU-bound so that inspecting a multi-GB checkpoint stays cheap. A failure
    here is not fatal: detection falls back to the config, and the real load in the import step
    is what ultimately decides."""
    try:
        from pymss_core.checkpoint import load_state_dict  # type: ignore
        state_dict = load_state_dict(model_path, map_location="cpu", mmap=True)
        if hasattr(state_dict, "keys"):
            return [str(key) for key in state_dict.keys()], None
        return [], f"unexpected checkpoint payload: {type(state_dict).__name__}"
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


def config_stems(config: dict[str, Any]) -> tuple[list[str], str]:
    """Stem names and target stem declared by a config, used to prefill the import form."""
    training = config.get("training") if isinstance(config, dict) else None
    training = training if isinstance(training, dict) else {}
    instruments = training.get("instruments")
    if isinstance(instruments, (list, tuple)):
        names = [str(item).strip() for item in instruments if str(item).strip()]
    elif str(instruments or "").strip():
        names = [str(instruments).strip()]
    else:
        names = []
    return names, str(training.get("target_instrument") or "").strip()


def suggested_model_name(model_path: Path) -> str:
    """A registrable name derived from the filename.

    pymss rejects names containing whitespace, so collapse it rather than handing the user a
    value that cannot be submitted. Leading dots go too: pathlib reports a bare ``.ckpt`` as its
    own stem, and a model listed under a dotted name reads as a mistake."""
    stem = model_path.stem.strip().lstrip(".").strip()
    collapsed = "_".join(part for part in stem.split() if part)
    return collapsed or "custom_model"


def cmd_inspect_custom_model(payload: dict[str, Any]) -> int:
    model_path_value = str(payload.get("modelPath") or "").strip()
    if not model_path_value:
        return emit_error("CUSTOM_MODEL_INVALID", "Missing model file path")
    model_path = Path(model_path_value).expanduser()
    if not model_path.is_file():
        return emit_error("CUSTOM_MODEL_FILE_MISSING", f"Model file not found: {model_path}")

    config_value = str(payload.get("configPath") or "").strip()
    config_path = Path(config_value).expanduser() if config_value else None
    if config_path and not config_path.is_file():
        return emit_error("CUSTOM_MODEL_FILE_MISSING", f"Config file not found: {config_path}")

    try:
        known_types, config_optional_types = _known_model_types()
    except Exception as exc:
        return emit_error("CUSTOM_MODEL_UNAVAILABLE", f"Unable to read pymss model types: {exc}")

    config = _read_config(config_path)
    state_dict_keys, state_dict_error = _read_state_dict_keys(model_path)
    suggestions = [
        item for item in merge_suggestions(detect_from_config(config), detect_from_state_dict_keys(state_dict_keys))
        # Never suggest something this pymss build would refuse to register.
        if item["modelType"] in known_types
    ]
    instruments, target_instrument = config_stems(config)
    top_type = suggestions[0]["modelType"] if suggestions else None

    try:
        size_bytes = model_path.stat().st_size
    except OSError:
        size_bytes = 0

    emit("custom_model_inspected", {
        "modelPath": str(model_path),
        "configPath": str(config_path) if config_path else None,
        "sizeBytes": size_bytes,
        "suggestions": suggestions,
        "suggestedModelType": top_type,
        "suggestedName": suggested_model_name(model_path),
        "instruments": instruments,
        "targetInstrument": target_instrument,
        "knownModelTypes": known_types,
        "configOptionalModelTypes": config_optional_types,
        # Whether the top suggestion needs a YAML config; null when there is no suggestion yet.
        "configRequired": (top_type not in config_optional_types) if top_type else None,
        "stateDictReadable": not state_dict_error,
        "stateDictError": state_dict_error,
    })
    return 0


# --------------------------------------------------------------------------------------
# Import / removal / relink
# --------------------------------------------------------------------------------------

def managed_root(model_dir: str | None = None) -> Path:
    """Where copied weights live.

    Under the model directory so that existing storage accounting and the model-directory
    migration both already cover them.

    The caller's `model_dir` wins over PYMSS_MODEL_DIR: that variable always holds the default
    location, while the directory the user actually configured is passed per command (the same
    way list_models and delete_model receive it). Reading only the environment would drop copies
    into the default directory while every catalog model lived somewhere else."""
    if model_dir:
        root = Path(model_dir).expanduser()
    else:
        env_dir = os.environ.get("PYMSS_MODEL_DIR")
        root = Path(env_dir) if env_dir else Path.home() / ".cache" / "pymss" / "models"
    return root / "custom"


def safe_model_name(name: str) -> bool:
    return bool(name) and name not in {".", ".."} and not (
        any(ch.isspace() or ord(ch) < 32 or ch in '/\\<>:"|?*' for ch in name)
        or name.endswith('.')
        or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name, re.IGNORECASE)
    )


def _managed_dir_for_name(model_dir: str | None, name: str) -> Path:
    if not safe_model_name(name):
        raise ValueError("Model name must be a valid directory name without paths or reserved characters")
    root = managed_root(model_dir).parent.resolve() / "custom"
    target = root / name
    for path in (root, target):
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise ValueError("Managed model directories must not be symbolic links or junctions")
        if path.exists() and not path.is_dir():
            raise ValueError("The managed model path is not a directory")
    return target


def owned_custom_model_directory(entry: Any, model_dir: str | None) -> Path | None:
    meta = sidecar_entry(entry.name)
    managed_dir = meta.get("managedDir")
    if meta.get("importMode") != "copy" or not isinstance(managed_dir, str):
        return None
    try:
        expected = _managed_dir_for_name(model_dir, entry.name)
        registered = (entry.model_path, entry.config_path) if entry.config_path else (entry.model_path,)
        if Path(managed_dir).expanduser().resolve() == expected and all(
            path and Path(path).expanduser().parent.resolve() == expected for path in registered
        ):
            return expected
    except (ValueError, OSError, RuntimeError):
        return None
    return None


def _copy_with_progress(source: Path, target: Path, task_id: str | None, label: str) -> None:
    """Copy one file, reporting progress.

    Written by hand rather than shutil.copy2 because a multi-GB checkpoint would otherwise look
    frozen. The temporary file keeps a cancelled copy from leaving a half-written weight behind
    that would later load as corrupt."""
    total = source.stat().st_size
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_target = target.with_suffix(target.suffix + ".part")
    copied = 0
    last_emitted = -1
    try:
        with source.open("rb") as reader, temp_target.open("wb") as writer:
            while True:
                block = reader.read(4 * 1024 * 1024)
                if not block:
                    break
                writer.write(block)
                copied += len(block)
                progress = int(copied / total * 100) if total else 100
                if progress != last_emitted:
                    last_emitted = progress
                    emit("custom_model_import_progress", {
                        "stage": "copying",
                        "file": label,
                        "progress": progress,
                        "copiedBytes": copied,
                        "totalBytes": total,
                    }, task_id=task_id)
        temp_target.replace(target)
    except BaseException:
        # Includes the process being killed mid-copy, which is how cancellation arrives.
        temp_target.unlink(missing_ok=True)
        raise


def _unique_sibling_dir(path: Path, suffix: str) -> Path:
    parent = path.parent
    for index in range(1000):
        candidate = parent / f".{path.name}{suffix}-{int(time.time() * 1000)}-{index}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Unable to create a temporary directory name for {path}")


def _replace_managed_dir(staging_dir: Path, managed_dir: Path) -> Path | None:
    """Move the verified staged copy into its final managed path.

    A failed re-import must leave the previous copied model usable. Replacing by way of a backup
    keeps the old directory available until the caller has updated the registry; if the final move
    fails, the backup is restored before the error reaches the caller.
    """
    backup_dir: Path | None = None
    managed_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        if managed_dir.exists():
            backup_dir = _unique_sibling_dir(managed_dir, ".backup")
            managed_dir.replace(backup_dir)
        staging_dir.replace(managed_dir)
        return backup_dir
    except Exception:
        if backup_dir is not None and backup_dir.exists() and not managed_dir.exists():
            try:
                backup_dir.replace(managed_dir)
            except Exception:
                pass
        raise


def _restore_managed_dir(managed_dir: Path, backup_dir: Path | None) -> None:
    if managed_dir.exists():
        shutil.rmtree(managed_dir)
    if backup_dir is not None:
        backup_dir.replace(managed_dir)


def _verify_model_loads(model_type: str, model_path: Path, config_path: Path | None, task_id: str | None) -> None:
    """Load the model for real, so a wrong architecture is caught before anything is registered.

    Constructing MSSeparator loads the weights (pymss/separator.py calls load_model() from
    __init__), which is the only check that can prove the architecture and config actually match.

    Built straight from the paths rather than through resolve_model(): verifying must not require
    a registration to exist first. That ordering matters — a re-import with force=True replaces an
    existing registration, so verifying afterwards and unregistering on failure would destroy a
    model that was working until now.

    Pinned to CPU so verification never competes with a running separation for VRAM."""
    emit("custom_model_import_progress", {
        "stage": "verifying",
        "progress": 0,
        "message": model_path.name,
    }, task_id=task_id)
    from pymss import MSSeparator  # type: ignore
    separator = MSSeparator(
        model_type=model_type,
        model_path=str(model_path),
        config_path=str(config_path) if config_path else None,
        device="cpu",
        device_ids=[0],
        store_dirs={},
    )
    try:
        close = getattr(separator, "close", None)
        if callable(close):
            close()
    except Exception:
        # Verification already succeeded by this point; a noisy teardown must not fail the import.
        pass


def cmd_import_custom_model(payload: dict[str, Any]) -> int:
    """Register a local model, optionally copying it under app management and verifying it.

    Ordering is deliberate: copy, verify, then register. Verifying last would mean unwinding a
    registration that may have replaced an existing one (force=True), destroying a model that
    worked until now. Registering only after the model has been proven to load also keeps a
    broken entry out of the model list, where it would otherwise fail at separation time."""
    task_id = str(payload.get("taskId") or f"custom_model_import_{int(time.time() * 1000)}")
    name = str(payload.get("name") or "").strip()
    model_type = str(payload.get("modelType") or "").strip()
    model_path_value = str(payload.get("modelPath") or "").strip()
    config_value = str(payload.get("configPath") or "").strip()
    aliases = [str(item).strip() for item in (payload.get("aliases") or []) if str(item).strip()]
    import_mode = "copy" if str(payload.get("importMode") or "reference") == "copy" else "reference"
    verify = bool(payload.get("verify", True))
    force = bool(payload.get("force", False))

    if not name:
        return emit_error("CUSTOM_MODEL_INVALID", "Missing model name", task_id=task_id)
    if any(ch.isspace() for ch in name):
        return emit_error("CUSTOM_MODEL_INVALID", f"Model name must not contain whitespace: {name!r}", task_id=task_id)
    if not safe_model_name(name):
        return emit_error("CUSTOM_MODEL_INVALID", "Model name must not contain paths or reserved characters", task_id=task_id)
    if not model_type:
        return emit_error("CUSTOM_MODEL_INVALID", "Missing model type", task_id=task_id)
    if not model_path_value:
        return emit_error("CUSTOM_MODEL_INVALID", "Missing model file path", task_id=task_id)

    source_model = Path(model_path_value).expanduser()
    source_config = Path(config_value).expanduser() if config_value else None
    if not source_model.is_file():
        return emit_error("CUSTOM_MODEL_FILE_MISSING", f"Model file not found: {source_model}", task_id=task_id)
    if source_config and not source_config.is_file():
        return emit_error("CUSTOM_MODEL_FILE_MISSING", f"Config file not found: {source_config}", task_id=task_id)

    emit("custom_model_import_started", {
        "name": name,
        "modelType": model_type,
        "importMode": import_mode,
        "verify": verify,
    }, task_id=task_id)

    managed_dir: Path | None = None
    staging_dir: Path | None = None
    model_path = source_model
    config_path = source_config
    try:
        if import_mode == "copy":
            managed_dir = _managed_dir_for_name(str(payload.get("modelDir") or "").strip() or None, name)
            staging_dir = _unique_sibling_dir(managed_dir, ".importing")
            model_path = staging_dir / source_model.name
            _copy_with_progress(source_model, model_path, task_id, source_model.name)
            if source_config:
                config_path = staging_dir / source_config.name
                _copy_with_progress(source_config, config_path, task_id, source_config.name)

        if verify:
            try:
                _verify_model_loads(model_type, model_path, config_path, task_id)
            except Exception as verify_exc:
                # Nothing is registered yet, so there is no registration to unwind — only the
                # copy this import made, if any.
                if staging_dir is not None:
                    shutil.rmtree(staging_dir, ignore_errors=True)
                return emit_error(
                    "CUSTOM_MODEL_VERIFY_FAILED",
                    f"{type(verify_exc).__name__}: {verify_exc}",
                    task_id=task_id,
                )

        emit("custom_model_import_progress", {"stage": "registering", "progress": 100}, task_id=task_id)
        # The top-level register_model, not user_models.register_user_model: only the former wires
        # in the catalog name check, which is what stops a custom model from shadowing a built-in.
        from pymss.model_registry import register_model  # type: ignore
        backup_dir: Path | None = None
        if staging_dir is not None and managed_dir is not None:
            backup_dir = _replace_managed_dir(staging_dir, managed_dir)
            model_path = managed_dir / source_model.name
            config_path = (managed_dir / source_config.name) if source_config else None

        try:
            entry = register_model(
                name,
                model_type,
                str(model_path),
                config_path=str(config_path) if config_path else None,
                aliases=aliases or None,
                force=force,
                require_exists=True,
            )
        except Exception as registration_error:
            if managed_dir is not None:
                try:
                    _restore_managed_dir(managed_dir, backup_dir)
                except Exception as restore_error:
                    recovery = f" Previous model files are preserved at {backup_dir}." if backup_dir is not None and backup_dir.exists() else ""
                    raise RuntimeError(
                        f"Model registration failed: {registration_error}. "
                        f"Unable to restore the model directory: {restore_error}.{recovery}"
                    ) from restore_error
            raise
        else:
            if backup_dir is not None:
                shutil.rmtree(backup_dir, ignore_errors=True)

        _remember_import(name, import_mode, managed_dir)
        emit("custom_model_import_finished", {
            "name": entry.name,
            "modelType": entry.model_type,
            "modelPath": entry.model_path,
            "configPath": entry.config_path,
            "importMode": import_mode,
            "verified": verify,
        }, task_id=task_id)
        return 0
    except Exception as exc:
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)
        return emit_error("CUSTOM_MODEL_IMPORT_FAILED", f"{type(exc).__name__}: {exc}", task_id=task_id)


def cmd_unregister_custom_model(payload: dict[str, Any]) -> int:
    """Remove a custom model from the registry, and its files only when the app owns them."""
    name = str(payload.get("name") or payload.get("model") or "").strip()
    if not name:
        return emit_error("CUSTOM_MODEL_INVALID", "Missing model name")
    delete_files = bool(payload.get("deleteFiles", False))

    model_dir = str(payload.get("modelDir") or "").strip() or None

    try:
        from pymss.model_registry import unregister_model  # type: ignore
        entry = unregister_model(name)
    except KeyError as exc:
        return emit_error("CUSTOM_MODEL_NOT_FOUND", str(exc))
    except Exception as exc:
        return emit_error("CUSTOM_MODEL_REMOVE_FAILED", f"{type(exc).__name__}: {exc}")

    managed_dir = owned_custom_model_directory(entry, model_dir)
    deleted: list[str] = []
    errors: list[str] = []
    if delete_files and managed_dir is not None:
        try:
            if managed_dir.is_dir():
                shutil.rmtree(managed_dir)
                deleted.append(str(managed_dir))
        except Exception as exc:
            errors.append(f"{managed_dir}: {exc}")

    _forget_import(name)
    emit("custom_model_unregistered", {
        "name": getattr(entry, "name", name),
        "filesDeleted": deleted,
        # True only when files were both requested and eligible, so the UI never claims to have
        # deleted a referenced model's weights.
        "deletedFiles": bool(deleted),
        "fileDeletionSupported": managed_dir is not None,
        "errors": errors,
    })
    return 0


def cmd_relink_custom_model(payload: dict[str, Any]) -> int:
    """Point an existing registration at moved files, keeping its name and type.

    Implemented as a forced re-registration because that is the only mutation pymss's registry
    offers; the type is carried over from the existing entry unless the caller overrides it."""
    name = str(payload.get("name") or "").strip()
    if not name:
        return emit_error("CUSTOM_MODEL_INVALID", "Missing model name")
    model_path_value = str(payload.get("modelPath") or "").strip()
    if not model_path_value:
        return emit_error("CUSTOM_MODEL_INVALID", "Missing model file path")
    model_path = Path(model_path_value).expanduser()
    if not model_path.is_file():
        return emit_error("CUSTOM_MODEL_FILE_MISSING", f"Model file not found: {model_path}")
    config_value = str(payload.get("configPath") or "").strip()
    config_path = Path(config_value).expanduser() if config_value else None
    if config_path and not config_path.is_file():
        return emit_error("CUSTOM_MODEL_FILE_MISSING", f"Config file not found: {config_path}")

    try:
        from pymss.user_models import get_user_model_entry  # type: ignore
        existing = get_user_model_entry(name)
    except KeyError as exc:
        return emit_error("CUSTOM_MODEL_NOT_FOUND", str(exc))
    except Exception as exc:
        return emit_error("CUSTOM_MODEL_RELINK_FAILED", f"{type(exc).__name__}: {exc}")

    model_type = str(payload.get("modelType") or existing.model_type or "").strip()
    try:
        from pymss.model_registry import register_model  # type: ignore
        entry = register_model(
            name,
            model_type,
            str(model_path),
            config_path=str(config_path) if config_path else None,
            aliases=list(existing.aliases) or None,
            force=True,
            require_exists=True,
        )
    except Exception as exc:
        return emit_error("CUSTOM_MODEL_RELINK_FAILED", f"{type(exc).__name__}: {exc}")

    emit("custom_model_relinked", {
        "name": entry.name,
        "modelType": entry.model_type,
        "modelPath": entry.model_path,
        "configPath": entry.config_path,
    })
    return 0


def _normalized(path: str) -> str:
    """Comparable form of a path. normcase matters on Windows, where the same file is routinely
    spelled with different casing and separators."""
    return os.path.normcase(os.path.normpath(str(path)))


def _remapped_path(path: str, from_root: str, to_root: str) -> str | None:
    """`path` rebased from `from_root` onto `to_root`, or None when it is not under `from_root`.

    Compares whole path components, so a sibling directory like `models-backup` is never treated
    as living inside `models`."""
    normalized_path = _normalized(path)
    normalized_from = _normalized(from_root)
    if normalized_path == normalized_from:
        return str(Path(to_root))
    prefix = normalized_from.rstrip(os.sep) + os.sep
    if not normalized_path.startswith(prefix):
        return None
    relative = Path(str(path)).relative_to(Path(from_root))
    return str(Path(to_root) / relative)


def _migrated_managed_dir(name: str, managed_dir: str, from_root: str, to_root: str,
                          paths: set[Path]) -> str | None:
    moved_dir = _remapped_path(managed_dir, from_root, to_root)
    if not moved_dir:
        return None
    try:
        expected = _managed_dir_for_name(to_root, name)
        # Unregistering a managed model removes the entire directory.
        if Path(moved_dir) == expected and set(expected.iterdir()) == paths:
            return str(expected)
    except (ValueError, OSError, RuntimeError):
        return None
    return None


def cmd_remap_custom_model_paths(payload: dict[str, Any]) -> int:
    """Commit registered paths for verified copies before the source files are removed."""
    from_root = str(payload.get("fromRoot") or "").strip()
    to_root = str(payload.get("toRoot") or "").strip()
    if not from_root or not to_root:
        return emit_error("CUSTOM_MODEL_INVALID", "Missing fromRoot or toRoot")
    if _normalized(from_root) == _normalized(to_root):
        emit("custom_models_remapped", {"committed": True, "count": 0, "unchanged": True})
        return 0

    copied_files = payload.get("copiedFiles")
    if not isinstance(copied_files, list):
        return emit_error("CUSTOM_MODEL_INVALID", "Expected a list of copied files")
    moved_paths: dict[str, str] = {}
    for value in copied_files:
        if not isinstance(value, str):
            return emit_error("CUSTOM_MODEL_INVALID", "Expected a relative migration path")
        relative = Path(value)
        if relative.anchor or not relative.parts or ".." in relative.parts:
            return emit_error("CUSTOM_MODEL_INVALID", "Expected a relative migration path")
        moved_paths[_normalized(str(Path(from_root) / relative))] = str(Path(to_root) / relative)

    try:
        count, ownership_update_failed = _remap_registered_models(from_root, to_root, moved_paths)
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        return emit_error("CUSTOM_MODEL_REMAP_FAILED", f"Could not update custom model paths: {exc}")
    emit("custom_models_remapped", {
        "committed": True,
        "count": count,
        "ownershipUpdateFailed": ownership_update_failed,
    })
    return 0


def _remap_registered_models(from_root: str, to_root: str, moved_paths: dict[str, str]) -> tuple[int, bool]:
    registry = custom_model_registry_path()
    data = read_custom_model_registry(registry)
    sidecar = _read_sidecar()
    updated = 0
    ownership_changed = False
    for entry in data["models"]:
        if not isinstance(entry, dict):
            raise ValueError("Invalid user model entry")
        name, model_path, config_path = entry.get("name"), entry.get("model_path"), entry.get("config_path")
        if not isinstance(name, str) or not isinstance(model_path, str) or (config_path is not None and not isinstance(config_path, str)):
            raise ValueError("Invalid user model entry")
        new_model_path = moved_paths.get(_normalized(os.path.expanduser(model_path)))
        new_config_path = moved_paths.get(_normalized(os.path.expanduser(config_path))) if config_path else None
        if new_model_path is None and new_config_path is None:
            continue
        paths = {Path(path) for path in (new_model_path, new_config_path) if path is not None}
        for path in paths:
            if not path.is_file():
                raise FileNotFoundError(f"Copied model file is missing: {path}")
        if new_model_path is not None:
            entry["model_path"] = new_model_path
        if new_config_path is not None:
            entry["config_path"] = new_config_path
        info = sidecar.get(name)
        if isinstance(info, dict) and info.get("importMode") == "copy" and info.get("managedDir"):
            moved_dir = None
            if new_model_path is not None and (not config_path or new_config_path is not None):
                moved_dir = _migrated_managed_dir(name, str(info["managedDir"]), from_root, to_root, paths)
            info["managedDir"] = moved_dir
            if moved_dir is None:
                info["importMode"] = "reference"
            ownership_changed = True
        updated += 1

    if updated:
        _write_atomic_json(registry, data)
    return updated, ownership_changed and not _write_sidecar(sidecar)
