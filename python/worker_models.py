from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import traceback
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from audio_tools.asr_cache import is_complete_asr_model_cache

import yaml

from worker_inference_constraints import chunk_size_constraint
from worker_protocol import WORKER_VERSION, _as_bool, _as_float, _as_int, emit, emit_error, import_available


def package_version(distribution: str) -> str | None:
    try:
        from importlib.metadata import version
        return version(distribution)
    except Exception:
        return None


@dataclass(frozen=True)
class ModelEntry:
    name: str
    aliases: tuple[str, ...]
    model_type: str | None
    architecture: str
    supported: bool
    unsupported_reason: str
    relpath: str
    config_relpath: str
    auxiliary_relpaths: tuple[str, ...]
    size_bytes: int
    sha256: str
    primary_category: str
    primary_category_cn: str
    secondary_category: str
    secondary_category_cn: str
    target_stem: str
    config_instruments: str
    config_target_instrument: str
    classification_confidence: str
    classification_basis: str
    debug_source: str = ""
    target_instrument_override: str = ""

    @property
    def stem(self) -> str:
        return Path(self.name).stem

    @property
    def category_path(self) -> str:
        return "/".join(part for part in (self.primary_category, self.secondary_category) if part)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelEntry":
        return cls(
            name=data["name"],
            aliases=tuple(data.get("aliases", ())),
            model_type=data.get("model_type"),
            architecture=data.get("architecture", ""),
            supported=bool(data.get("supported", False)),
            unsupported_reason=data.get("unsupported_reason", ""),
            relpath=data["relpath"],
            config_relpath=data.get("config_relpath", ""),
            auxiliary_relpaths=tuple(data.get("auxiliary_relpaths", ())),
            size_bytes=int(data.get("size_bytes", 0)),
            sha256=data.get("sha256", ""),
            primary_category=data.get("primary_category", ""),
            primary_category_cn=data.get("primary_category_cn", ""),
            secondary_category=data.get("secondary_category", ""),
            secondary_category_cn=data.get("secondary_category_cn", ""),
            target_stem=data.get("target_stem", ""),
            config_instruments=data.get("config_instruments", ""),
            config_target_instrument=data.get("config_target_instrument", ""),
            target_instrument_override=data.get("target_instrument_override", ""),
            classification_confidence=data.get("classification_confidence", ""),
            classification_basis=data.get("classification_basis", ""),
            debug_source=data.get("debug_source", ""),
        )


@dataclass(frozen=True)
class RegisteredUserModelEntry:
    """Lightweight reader for pymss's persisted custom-model registry.

    Listing models is a startup path. Importing ``pymss.user_models`` first executes
    ``pymss.__init__``, which imports the inference stack and Torch even though the registry is
    just JSON. Keep this reader aligned with the stable on-disk fields and leave mutations to
    pymss's public API.
    """

    name: str
    model_type: str
    model_path: str
    config_path: str | None = None
    aliases: tuple[str, ...] = ()
    source: str = "user"
    architecture: str = ""
    supported: bool = True
    unsupported_reason: str = ""
    relpath: str = ""
    config_relpath: str = ""
    auxiliary_relpaths: tuple[str, ...] = ()
    size_bytes: int = 0
    primary_category: str = "user"
    primary_category_cn: str = "用户"
    secondary_category: str = "custom"
    secondary_category_cn: str = "自定义"
    target_stem: str = ""
    inference_params: dict[str, Any] = field(default_factory=dict)

    @property
    def stem(self) -> str:
        return Path(self.name).stem

    @property
    def category_path(self) -> str:
        return "/".join(part for part in (self.primary_category, self.secondary_category) if part)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RegisteredUserModelEntry":
        name = str(data.get("name") or "").strip()
        model_type = str(data.get("model_type") or "").strip()
        model_path = str(data.get("model_path") or "").strip()
        if not name or not model_type or not model_path:
            raise ValueError("Invalid user model registry entry")
        raw_aliases = data.get("aliases")
        raw_params = data.get("inference_params")
        return cls(
            name=name,
            model_type=model_type,
            model_path=str(Path(model_path).expanduser()),
            config_path=(str(Path(str(data["config_path"])).expanduser()) if data.get("config_path") else None),
            aliases=(
                tuple(str(alias) for alias in raw_aliases)
                if isinstance(raw_aliases, (list, tuple))
                else ()
            ),
            architecture=str(data.get("architecture") or model_type),
            supported=bool(data.get("supported", True)),
            unsupported_reason=str(data.get("unsupported_reason") or ""),
            primary_category=str(data.get("primary_category") or "user"),
            primary_category_cn=str(data.get("primary_category_cn") or "用户"),
            secondary_category=str(data.get("secondary_category") or "custom"),
            secondary_category_cn=str(data.get("secondary_category_cn") or "自定义"),
            target_stem=str(data.get("target_stem") or ""),
            inference_params=(dict(raw_params) if isinstance(raw_params, dict) else {}),
        )


@lru_cache(maxsize=1)
def _model_catalog_path() -> Path:
    try:
        # Reading the catalog must not execute pymss.__init__: that imports the inference stack
        # and Torch, adding seconds to every app start merely to locate one JSON file.
        from importlib.metadata import distribution

        package = distribution("pymss")
        for file in package.files or ():
            if str(file).replace("\\", "/") == "pymss/resources/model_catalog.json":
                catalog = Path(package.locate_file(file)).resolve()
                if catalog.is_file():
                    return catalog
    except Exception:
        pass
    try:
        # Source checkouts may be importable without installed distribution metadata.
        import pymss  # type: ignore
        package_dir = Path(pymss.__file__).resolve().parent
        direct = package_dir / "resources" / "model_catalog.json"
        if direct.is_file():
            return direct
    except Exception:
        pass
    raise FileNotFoundError("Unable to locate pymss/resources/model_catalog.json")


def _debug_dir() -> Path:
    return Path(
        os.environ.get("PYMSS_STUDIO_DEBUG_DIR")
        or Path.home() / ".cache" / "pymss-studio" / "debug"
    )


def _debug_catalog_path() -> Path:
    return _debug_dir() / "model-catalog.json"


def _json_stable(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _entry_to_catalog_dict(entry: ModelEntry) -> dict[str, Any]:
    return {
        "name": entry.name,
        "aliases": list(entry.aliases),
        "model_type": entry.model_type,
        "architecture": entry.architecture,
        "supported": entry.supported,
        "unsupported_reason": entry.unsupported_reason,
        "relpath": entry.relpath,
        "config_relpath": entry.config_relpath,
        "auxiliary_relpaths": list(entry.auxiliary_relpaths),
        "size_bytes": entry.size_bytes,
        "sha256": entry.sha256,
        "primary_category": entry.primary_category,
        "primary_category_cn": entry.primary_category_cn,
        "secondary_category": entry.secondary_category,
        "secondary_category_cn": entry.secondary_category_cn,
        "target_stem": entry.target_stem,
        "config_instruments": entry.config_instruments,
        "config_target_instrument": entry.config_target_instrument,
        "target_instrument_override": entry.target_instrument_override,
        "classification_confidence": entry.classification_confidence,
        "classification_basis": entry.classification_basis,
    }


_DEBUG_CATALOG_STORAGE_FORMAT = "overlay-v1"
_DEBUG_CATALOG_RUNTIME_FIELDS = {"debug_source"}
_DEBUG_CATALOG_CONTROL_FIELDS = {
    "models",
    "removed",
    "storage_format",
    "overrides",
    "catalog",
    "catalog_removed",
    "model_order",
}


def _canonical_catalog_model(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize known fields while preserving fields added by newer pymss releases."""
    source = {key: value for key, value in item.items() if key not in _DEBUG_CATALOG_RUNTIME_FIELDS}
    return {**source, **_entry_to_catalog_dict(ModelEntry.from_dict(source))}


def _canonical_catalog_data(data: dict[str, Any]) -> dict[str, Any]:
    models = data.get("models", [])
    return {
        **data,
        "models": [
            _canonical_catalog_model(item)
            for item in models
            if isinstance(item, dict) and item.get("name")
        ],
    }


def _catalog_models_by_name(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item["name"]): item
        for item in data.get("models", [])
        if isinstance(item, dict) and item.get("name")
    }


def _apply_debug_catalog(base_data: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    base = _canonical_catalog_data(base_data)
    if override.get("storage_format") != _DEBUG_CATALOG_STORAGE_FORMAT:
        legacy_models = override.get("models", [])
        models = [
            _canonical_catalog_model(item)
            for item in legacy_models
            if isinstance(item, dict) and item.get("name")
        ]
        removed = {str(name) for name in override.get("removed", []) if str(name).strip()}
        return {
            **base,
            **{key: value for key, value in override.items() if key not in {"models", "removed"}},
            "models": [item for item in models if str(item["name"]) not in removed],
        }

    result = dict(base)
    for key in override.get("catalog_removed", []):
        if isinstance(key, str) and key not in _DEBUG_CATALOG_CONTROL_FIELDS:
            result.pop(key, None)
    catalog_changes = override.get("catalog", {})
    if isinstance(catalog_changes, dict):
        result.update({
            key: value
            for key, value in catalog_changes.items()
            if key not in _DEBUG_CATALOG_CONTROL_FIELDS
        })

    changes = override.get("overrides", {})
    changes = changes if isinstance(changes, dict) else {}
    removed = {str(name) for name in override.get("removed", []) if str(name).strip()}
    added_by_name = {
        str(item["name"]): item
        for item in override.get("models", [])
        if isinstance(item, dict) and item.get("name")
    }
    models: list[dict[str, Any]] = []
    for item in base["models"]:
        name = str(item["name"])
        if name in removed:
            continue
        patch = changes.get(name, {})
        added_item = added_by_name.pop(name, {})
        merged = {
            **item,
            **(added_item if isinstance(added_item, dict) else {}),
            **(patch if isinstance(patch, dict) else {}),
            "name": name,
        }
        models.append(_canonical_catalog_model(merged))
    for name, item in added_by_name.items():
        if name not in removed:
            models.append(_canonical_catalog_model(item))
    requested_order = override.get("model_order", [])
    if isinstance(requested_order, list):
        models_by_name = {str(item["name"]): item for item in models}
        ordered_models = [
            models_by_name.pop(str(name))
            for name in requested_order
            if str(name) in models_by_name
        ]
        models = [*ordered_models, *models_by_name.values()]
    result["models"] = models
    return result


def _build_debug_catalog_overlay(
    desired_data: dict[str, Any],
    base_data: dict[str, Any],
) -> dict[str, Any]:
    desired = _canonical_catalog_data(desired_data)
    base = _canonical_catalog_data(base_data)
    desired_by_name = _catalog_models_by_name(desired)
    base_by_name = _catalog_models_by_name(base)
    explicitly_removed = {
        str(name).strip()
        for name in desired.get("removed", [])
        if str(name).strip()
    }

    overrides: dict[str, dict[str, Any]] = {}
    for name, desired_item in desired_by_name.items():
        if name in explicitly_removed:
            continue
        base_item = base_by_name.get(name)
        if base_item is None:
            continue
        changes = {
            key: value
            for key, value in desired_item.items()
            if key != "name"
            and (key not in base_item or _json_stable(value) != _json_stable(base_item[key]))
        }
        if changes:
            overrides[name] = changes

    added = [
        item
        for name, item in desired_by_name.items()
        if name not in base_by_name and name not in explicitly_removed
    ]
    removed = sorted(
        {name for name in base_by_name if name not in desired_by_name}
        | {name for name in explicitly_removed if name in base_by_name}
    )
    desired_order = [
        str(item["name"])
        for item in desired["models"]
        if str(item["name"]) not in explicitly_removed
    ]
    natural_order = [name for name in base_by_name if name not in removed]
    natural_order.extend(str(item["name"]) for item in added)
    desired_metadata = {
        key: value for key, value in desired.items() if key not in _DEBUG_CATALOG_CONTROL_FIELDS
    }
    base_metadata = {
        key: value for key, value in base.items() if key not in _DEBUG_CATALOG_CONTROL_FIELDS
    }
    catalog_changes = {
        key: value
        for key, value in desired_metadata.items()
        if key not in base_metadata or _json_stable(value) != _json_stable(base_metadata[key])
    }
    catalog_removed = [key for key in base_metadata if key not in desired_metadata]

    overlay: dict[str, Any] = {
        "schema_version": 1,
        "storage_format": _DEBUG_CATALOG_STORAGE_FORMAT,
        "models": added,
        "overrides": overrides,
        "removed": removed,
    }
    if catalog_changes:
        overlay["catalog"] = catalog_changes
    if catalog_removed:
        overlay["catalog_removed"] = catalog_removed
    if desired_order != natural_order:
        overlay["model_order"] = desired_order
    return overlay


def _debug_catalog_has_changes(overlay: dict[str, Any]) -> bool:
    return any(
        bool(overlay.get(key))
        for key in ("models", "overrides", "removed", "catalog", "catalog_removed", "model_order")
    )


def _base_catalog_data() -> dict[str, Any]:
    with _model_catalog_path().open(encoding="utf-8") as handle:
        return json.load(handle)


def _debug_catalog_raw() -> dict[str, Any]:
    path = _debug_catalog_path()
    if not path.is_file():
        return {"schema_version": 1, "models": []}
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {"schema_version": 1, "models": []}


def _inactive_debug_status() -> dict[str, Any]:
    return {
        "active": False,
        "catalogActive": False,
        "changedCount": 0,
        "addedCount": 0,
        "removedCount": 0,
        "changedModels": [],
        "addedModels": [],
        "removedModels": [],
        "debugDir": str(_debug_dir()),
        "debugCatalogPath": str(_debug_catalog_path()),
    }


def _debug_catalog_status(base_data: dict[str, Any] | None = None) -> dict[str, Any]:
    base_data = base_data or _base_catalog_data()
    path = _debug_catalog_path()
    if not path.is_file():
        return _inactive_debug_status()
    override = _debug_catalog_raw()
    base_by_name = _catalog_models_by_name(_canonical_catalog_data(base_data))
    effective = _apply_debug_catalog(base_data, override)
    effective_by_name = _catalog_models_by_name(effective)
    base_order = list(base_by_name)
    effective_order = [str(item["name"]) for item in effective["models"]]
    removed = sorted(name for name in base_by_name if name not in effective_by_name)
    changed = sorted(
        name
        for name, item in effective_by_name.items()
        if name in base_by_name and _json_stable(item) != _json_stable(base_by_name[name])
    )
    added = sorted(name for name in effective_by_name if name not in base_by_name)
    base_metadata = {key: value for key, value in base_data.items() if key != "models"}
    effective_metadata = {key: value for key, value in effective.items() if key != "models"}
    metadata_changed = _json_stable(base_metadata) != _json_stable(effective_metadata)
    active = bool(changed or added or removed or metadata_changed or base_order != effective_order)
    return {
        "active": active,
        "catalogActive": active,
        "changedCount": len(changed),
        "addedCount": len(added),
        "removedCount": len(removed),
        "changedModels": changed,
        "addedModels": added,
        "removedModels": removed,
        "debugDir": str(_debug_dir()),
        "debugCatalogPath": str(_debug_catalog_path()),
    }


def _default_model_dir() -> Path:
    env_value = os.environ.get("PYMSS_MODEL_DIR")
    if env_value:
        return Path(env_value)
    repo_models = _model_catalog_path().parent.parent.parent / "all_models"
    if repo_models.is_dir():
        return repo_models
    return Path.home() / ".cache" / "pymss" / "models"


def _load_yaml_config(config_path: Path) -> dict[str, Any]:
    with config_path.open(encoding="utf-8") as handle:
        data = yaml.load(handle, Loader=yaml.FullLoader)
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=1)
def load_model_catalog() -> dict[str, Any]:
    data = _base_catalog_data()
    override_path = _debug_catalog_path()
    if override_path.is_file():
        effective = _apply_debug_catalog(data, _debug_catalog_raw())
        status = _debug_catalog_status(data)
        debug_models = set(status["changedModels"]) | set(status["addedModels"])
        models = []
        for item in effective["models"]:
            next_item = dict(item)
            if str(item["name"]) in debug_models:
                next_item["debug_source"] = "debug"
            models.append(ModelEntry.from_dict(next_item))
        return {**effective, "models": models, "debug_status": status}

    models = [ModelEntry.from_dict(item) for item in data.get("models", [])]
    return {**data, "models": models, "debug_status": _inactive_debug_status()}


@lru_cache(maxsize=1)
def _model_index() -> dict[str, ModelEntry]:
    index: dict[str, ModelEntry] = {}
    for entry in load_model_catalog()["models"]:
        names = {entry.name, entry.stem, *entry.aliases}
        for name in names:
            key = str(name).strip().lower()
            if key in index and index[key].name != entry.name:
                continue
            index[key] = entry
    return index


def list_catalog_models(category: str | None = None, supported: bool | None = None) -> list[ModelEntry]:
    models = load_model_catalog()["models"]
    if category:
        category = category.lower()
        models = [
            item
            for item in models
            if item.primary_category.lower() == category
            or item.secondary_category.lower() == category
            or item.category_path.lower() == category
        ]
    if supported is not None:
        models = [item for item in models if item.supported is bool(supported)]
    return models


def get_catalog_model_entry(model_name: str) -> ModelEntry:
    try:
        return _model_index()[str(model_name).strip().lower()]
    except KeyError as exc:
        raise KeyError(f"Unknown pymss model: {model_name}") from exc


def model_root(model_dir: str | None = None) -> Path:
    return Path(model_dir).expanduser() if model_dir else _default_model_dir()


def is_user_model_entry(entry: Any) -> bool:
    """Whether `entry` is a pymss UserModelEntry rather than a catalog ModelEntry.

    The two describe their files in incompatible ways: a catalog entry stores a `relpath`
    relative to the model directory, while a user entry stores absolute `model_path` /
    `config_path` and leaves `relpath` empty. Feeding a user entry through the catalog
    computation therefore yields the model directory itself — a path that silently exists
    and points at the wrong thing, rather than failing."""
    return str(getattr(entry, "source", "") or "") == "user"


def model_path_for(entry: Any, model_dir: str | None = None) -> Path:
    if is_user_model_entry(entry):
        return Path(str(entry.model_path))
    return model_root(model_dir) / str(getattr(entry, "relpath", "") or "")


def config_path_for(entry: Any, model_dir: str | None = None) -> Path | None:
    if is_user_model_entry(entry):
        config_path = getattr(entry, "config_path", None)
        return Path(str(config_path)) if config_path else None
    config_relpath = str(getattr(entry, "config_relpath", "") or "")
    return model_root(model_dir) / config_relpath if config_relpath else None


def base_config_path_for(entry: Any, model_dir: str | None = None) -> Path | None:
    if is_user_model_entry(entry):
        config_path = getattr(entry, "config_path", None)
        return Path(str(config_path)) if config_path else None
    config_relpath = str(getattr(entry, "config_relpath", "") or "")
    return model_root(model_dir) / config_relpath if config_relpath else None


def effective_source_for(entry: Any) -> str:
    if is_user_model_entry(entry):
        return "user"
    if str(getattr(entry, "debug_source", "") or "") == "debug":
        return "debug"
    return "catalog"


def auxiliary_paths_for(entry: Any, model_dir: str | None = None) -> list[Path]:
    if is_user_model_entry(entry):
        # User registrations name their files outright, so anything here is already absolute.
        return [Path(str(relpath)) for relpath in getattr(entry, "auxiliary_relpaths", ()) or ()]
    root = model_root(model_dir)
    return [root / relpath for relpath in getattr(entry, "auxiliary_relpaths", ()) or ()]

def _derive_overlap_size_from_num_overlap(chunk_size: Any, num_overlap: Any) -> int | None:
    chunk_value = _as_int(chunk_size)
    overlap_count = _as_int(num_overlap)
    if chunk_value is None or overlap_count is None:
        return None
    if chunk_value <= 0 or overlap_count <= 0:
        return None
    if overlap_count == 1:
        return 0
    step = int(chunk_value // overlap_count)
    overlap_size = int(chunk_value - step)
    if overlap_size < 0 or overlap_size >= chunk_value:
        return None
    return overlap_size


def _positive_config_int(value: Any) -> int | None:
    parsed = _as_int(value)
    return parsed if parsed is not None and parsed > 0 else None


def _nested_config_value(config: dict[str, Any], section: str, key: str) -> Any:
    value = config.get(section)
    return value.get(key) if isinstance(value, dict) else None


def resolve_inference_param_meta(
    entry: Any,
    config_path: Path | None,
    config_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    model_type = str(getattr(entry, "model_type", "") or "").strip().lower()
    if model_type == "vr" or not config_path or not config_path.is_file():
        return {}
    try:
        config = config_data if config_data is not None else _load_yaml_config(config_path)
    except Exception:
        return {}

    candidates: list[tuple[str, Any]] = []
    if model_type in {"mel_band_roformer", "mel_band_conformer", "bs_roformer", "bs_conformer", "bs_roformer_hyperace"}:
        candidates.append(("model.stft_hop_length", _nested_config_value(config, "model", "stft_hop_length")))
        candidates.append(("audio.hop_length", _nested_config_value(config, "audio", "hop_length")))
    elif model_type == "mdx23c":
        candidates.append(("audio.hop_length", _nested_config_value(config, "audio", "hop_length")))
    elif model_type == "scnet":
        candidates.append(("model.hop_size", _nested_config_value(config, "model", "hop_size")))
    elif model_type in {"bandit", "bandit_v2"}:
        section = "kwargs" if model_type == "bandit_v2" else "model"
        candidates.append((f"{section}.hop_length", _nested_config_value(config, section, "hop_length")))
    elif model_type == "apollo":
        sample_rate = _positive_config_int(_nested_config_value(config, "model", "sr"))
        window_ms = _as_float(_nested_config_value(config, "model", "win"))
        if sample_rate is not None and window_ms is not None and window_ms > 0:
            candidates.append(("model.sr+win", int(sample_rate * window_ms // 1000) // 2))
    else:
        for section in ("model", "kwargs", "audio", "inference", "features"):
            for key in ("stft_hop_length", "hop_length", "hop_size"):
                candidates.append((f"{section}.{key}", _nested_config_value(config, section, key)))
            section_value = config.get(section)
            stft = section_value.get("stft") if isinstance(section_value, dict) else None
            if isinstance(stft, dict):
                for key in ("hop_length", "hop_size"):
                    candidates.append((f"{section}.stft.{key}", stft.get(key)))
        for key in ("stft_hop_length", "hop_length", "hop_size"):
            candidates.append((key, config.get(key)))

    for source, value in candidates:
        step = _positive_config_int(value)
        if step is not None:
            metadata = {"recommendedSampleStep": step, "source": source}
            constraint = chunk_size_constraint(model_type, config)
            if constraint:
                metadata["chunkSizeConstraint"] = constraint
            return metadata
    return {}


def resolve_default_inference_params(
    entry: Any,
    model_path: Path,
    config_path: Path | None,
    config_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    model_type = str(getattr(entry, "model_type", "") or "").strip().lower()
    defaults: dict[str, Any] = {}

    if not config_path or not config_path.is_file():
        if model_type == "vr":
            return {
                "batch_size": 2,
                "window_size": 512,
                "aggression": 5,
                "enable_post_process": False,
                "post_process_threshold": 0.2,
                "high_end_process": False,
                "normalize": False,
            }
        return {
            "batch_size": 1,
            "overlap_size": 0,
            "chunk_size": 0,
            "normalize": False,
        }

    try:
        config = config_data if config_data is not None else _load_yaml_config(config_path)
    except Exception:
        return defaults

    inference = config.get("inference") if isinstance(config, dict) else None
    audio = config.get("audio") if isinstance(config, dict) else None
    inference = inference if isinstance(inference, dict) else {}
    audio = audio if isinstance(audio, dict) else {}

    if model_type == "vr":
        batch_size = _as_int(inference.get("batch_size"))
        window_size = _as_int(inference.get("window_size"))
        aggression = _as_int(inference.get("aggression"))
        enable_post_process = _as_bool(inference.get("enable_post_process"))
        post_process_threshold = _as_float(inference.get("post_process_threshold"))
        high_end_process = _as_bool(inference.get("high_end_process"))

        if batch_size is not None:
            defaults["batch_size"] = batch_size
        if window_size is not None:
            defaults["window_size"] = window_size
        if aggression is not None:
            defaults["aggression"] = aggression
        if enable_post_process is not None:
            defaults["enable_post_process"] = enable_post_process
        if post_process_threshold is not None:
            defaults["post_process_threshold"] = post_process_threshold
        if high_end_process is not None:
            defaults["high_end_process"] = high_end_process
        return defaults

    batch_size = _as_int(inference.get("batch_size"))
    overlap_size = _as_int(inference.get("overlap_size"))
    num_overlap = _as_int(inference.get("num_overlap"))
    # Match pymss's MSST compatibility: inference settings override audio defaults.
    chunk_size = _as_int(inference.get("chunk_size"))
    if chunk_size is None:
        chunk_size = _as_int(audio.get("chunk_size"))
    normalize = _as_bool(inference.get("normalize"))

    if batch_size is not None:
        defaults["batch_size"] = batch_size
    if overlap_size is not None:
        defaults["overlap_size"] = overlap_size
    if model_type != "apollo" and num_overlap is not None:
        defaults["num_overlap"] = num_overlap
    if chunk_size is not None:
        defaults["chunk_size"] = chunk_size
    if normalize is not None:
        defaults["normalize"] = normalize
    return defaults


def resolve_config_stems(
    config_path: Path | None,
    config_data: dict[str, Any] | None = None,
) -> tuple[str, str]:
    if not config_path or not config_path.is_file():
        return "", ""
    try:
        config = config_data if config_data is not None else _load_yaml_config(config_path)
    except Exception:
        return "", ""
    training = config.get("training") if isinstance(config, dict) else None
    training = training if isinstance(training, dict) else {}
    instruments = training.get("instruments")
    target_instrument = training.get("target_instrument")
    if isinstance(instruments, (list, tuple)):
        config_instruments = "|".join(str(item).strip() for item in instruments if str(item).strip())
    else:
        config_instruments = str(instruments or "").strip()
    return config_instruments, str(target_instrument or "").strip()


def _user_model_import_mode(entry: Any) -> str | None:
    """How an imported model got here: 'copy' (the app holds the files) or 'reference'.

    Carried on the model itself so the UI can say exactly what removing it will do. Models
    registered outside the app (`pymss register`) have no record, and 'reference' is the safe
    reading — it is the one under which no files are deleted."""
    if not is_user_model_entry(entry):
        return None
    try:
        from worker_custom_models import sidecar_entry
        return str(sidecar_entry(str(entry.name)).get("importMode") or "reference")
    except Exception:
        return "reference"


def _entry_size_bytes(entry: Any, model_path: Path) -> int:
    """Size to report for a model.

    Catalog entries carry the published size, which is what lets the UI show a download size
    before anything exists locally. User registrations never record one, so measure the file
    they point at — otherwise every imported model would read as 0 bytes."""
    recorded = int(getattr(entry, "size_bytes", 0) or 0)
    if recorded or not is_user_model_entry(entry):
        return recorded
    try:
        return model_path.stat().st_size
    except OSError:
        return 0


def model_to_dict(entry: Any, model_dir: str | None = None, include_local_state: bool = True) -> dict[str, Any]:
    model_path = model_path_for(entry, model_dir)
    config_path = config_path_for(entry, model_dir)
    auxiliary_paths = auxiliary_paths_for(entry, model_dir)
    required_paths = [model_path]
    if config_path is not None:
        required_paths.append(config_path)
    required_paths.extend(auxiliary_paths)
    missing_paths = [str(path) for path in required_paths if not path.is_file()]
    downloaded = include_local_state and not missing_paths
    config_instruments = str(getattr(entry, "config_instruments", "") or "").strip()
    config_target_instrument = str(getattr(entry, "config_target_instrument", "") or "").strip()
    config_data: dict[str, Any] | None = None
    if config_path and config_path.is_file():
        try:
            config_data = _load_yaml_config(config_path)
        except Exception:
            config_data = {}
        resolved_instruments, resolved_target = resolve_config_stems(config_path, config_data)
        config_instruments = resolved_instruments or config_instruments
        config_target_instrument = resolved_target or config_target_instrument
    default_inference_params = resolve_default_inference_params(entry, model_path, config_path, config_data)
    inference_param_meta = resolve_inference_param_meta(entry, config_path, config_data)
    default_inference_params_source = "config" if config_path and config_path.is_file() else "runtime_fallback"
    return {
        "name": str(getattr(entry, "name", "") or ""),
        "aliases": list(getattr(entry, "aliases", ()) or ()),
        "modelType": getattr(entry, "model_type", None),
        "architecture": str(getattr(entry, "architecture", "") or ""),
        "supported": bool(getattr(entry, "supported", False)),
        "unsupportedReason": str(getattr(entry, "unsupported_reason", "") or ""),
        "category": str(getattr(entry, "category_path", "") or getattr(entry, "primary_category", "") or ""),
        "categoryCn": " / ".join(filter(None, [
            str(getattr(entry, "primary_category_cn", "") or ""),
            str(getattr(entry, "secondary_category_cn", "") or ""),
        ])),
        "primaryCategory": str(getattr(entry, "primary_category", "") or ""),
        "primaryCategoryCn": str(getattr(entry, "primary_category_cn", "") or ""),
        "secondaryCategory": str(getattr(entry, "secondary_category", "") or ""),
        "secondaryCategoryCn": str(getattr(entry, "secondary_category_cn", "") or ""),
        "targetStem": str(getattr(entry, "target_stem", "") or ""),
        "configInstruments": config_instruments,
        "configTargetInstrument": config_target_instrument,
        "classificationConfidence": str(getattr(entry, "classification_confidence", "") or ""),
        "classificationBasis": str(getattr(entry, "classification_basis", "") or ""),
        "sizeBytes": _entry_size_bytes(entry, model_path),
        "sha256": getattr(entry, "sha256", "") or "",
        # 'user' models are local-only: they cannot be downloaded (pymss.download_model rejects
        # them outright), so the UI has to offer relink/remove instead of download/delete.
        "source": effective_source_for(entry),
        "baseConfigPath": str(base_config_path_for(entry, model_dir)) if base_config_path_for(entry, model_dir) else None,
        "importMode": _user_model_import_mode(entry),
        "downloaded": downloaded,
        "missingPaths": missing_paths if include_local_state else [],
        "modelPath": str(model_path),
        "configPath": str(config_path) if config_path else None,
        "auxiliaryPaths": [str(path) for path in auxiliary_paths],
        "defaultInferenceParams": default_inference_params,
        "defaultInferenceParamsSource": default_inference_params_source,
        "inferenceParamMeta": inference_param_meta,
    }


def cmd_health() -> int:
    emit("health", {"ok": True, "workerVersion": WORKER_VERSION})
    return 0


def cmd_env_info() -> int:
    payload: dict[str, Any] = {
        "pythonVersion": sys.version.split()[0],
        "platform": platform.platform(),
        "workerVersion": WORKER_VERSION,
        "pymssAvailable": False,
        "pymssPath": None,
        "pymssVersion": package_version("pymss"),
        # Importing local models needs pymss's user-model registry, which only exists from
        # 2.0.15. Probed by import rather than by comparing version strings, so a repackaged or
        # patched build is judged on what it actually provides.
        "customModelsSupported": import_available("pymss.user_models"),
        "torchAvailable": False,
        "torchVersion": None,
        "torchBackend": "missing",
        "hipVersion": None,
        "cudaAvailable": False,
        "cudaAvailableError": None,
        "cudaDeviceCount": 0,
        "cudaDevices": [],
        "cudaDeviceCountError": None,
        "cudaDeviceNamesError": None,
        "mpsAvailable": False,
        "mlxAvailable": import_available("mlx"),
        "avAvailable": import_available("av"),
        "librosaAvailable": import_available("librosa"),
    }

    try:
        import pymss  # type: ignore
        payload["pymssAvailable"] = True
        payload["pymssPath"] = str(Path(pymss.__file__).resolve()) if getattr(pymss, "__file__", None) else None
        payload["pymssVersion"] = payload.get("pymssVersion") or getattr(pymss, "__version__", None)
    except Exception as exc:
        payload["pymssError"] = str(exc)

    try:
        import torch  # type: ignore
        payload["torchAvailable"] = True
        payload["torchVersion"] = getattr(torch, "__version__", None)
        payload["hipVersion"] = getattr(torch.version, "hip", None)
        payload["torchBackend"] = "rocm" if payload["hipVersion"] else "cuda" if getattr(torch.version, "cuda", None) else "cpu"
        try:
            payload["cudaAvailable"] = bool(torch.cuda.is_available())
        except Exception as exc:
            payload["cudaAvailableError"] = str(exc)
        cuda_devices: list[dict[str, Any]] = []
        try:
            device_count = max(0, int(torch.cuda.device_count()))
            payload["cudaDeviceCount"] = device_count
        except Exception as exc:
            device_count = 0
            payload["cudaDeviceCountError"] = str(exc)
        device_name_errors: list[str] = []
        for index in range(device_count):
            try:
                item: dict[str, Any] = {"id": index, "name": torch.cuda.get_device_name(index)}
            except Exception as exc:
                device_name_errors.append(f"device {index}: {exc}")
                continue
            try:
                props = torch.cuda.get_device_properties(index)
                item["totalMemoryBytes"] = int(getattr(props, "total_memory", 0) or 0)
                item["major"] = int(getattr(props, "major", 0) or 0)
                item["minor"] = int(getattr(props, "minor", 0) or 0)
            except Exception:
                pass
            cuda_devices.append(item)
        if device_name_errors:
            payload["cudaDeviceNamesError"] = "; ".join(device_name_errors)
        payload["cudaDevices"] = cuda_devices
        mps = getattr(torch.backends, "mps", None)
        payload["mpsAvailable"] = bool(mps and mps.is_available())
    except Exception as exc:
        payload["torchError"] = str(exc)

    emit("env_info", payload)
    return 0


def _load_registered_user_models() -> list[RegisteredUserModelEntry]:
    registry = Path(
        os.environ.get("PYMSS_USER_MODELS")
        or Path.home() / ".cache" / "pymss" / "user_models.json"
    ).expanduser()
    if not registry.is_file():
        return []
    data = json.loads(registry.read_text(encoding="utf-8"))
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        raise ValueError(f"Invalid user model registry: {registry}")
    return [RegisteredUserModelEntry.from_dict(item) for item in models if isinstance(item, dict)]


def list_registered_user_models(category: str | None = None) -> list[Any]:
    """Locally registered custom models, or an empty list when none can be read.

    Never raises: an unreadable registry must not take the whole model list down with it, since
    the catalog is what the app primarily needs. Filtering mirrors list_catalog_models() so a
    category selection applies to both halves of the list.

    `supported` is deliberately not filtered on — pymss registers every user model as supported
    (it has no catalog verdict to consult), so filtering would be a no-op that reads as a check."""
    try:
        entries = _load_registered_user_models()
    except Exception:
        return []
    if category:
        wanted = category.lower()
        entries = [
            entry for entry in entries
            if str(getattr(entry, "primary_category", "")).lower() == wanted
            or str(getattr(entry, "secondary_category", "")).lower() == wanted
            or str(getattr(entry, "category_path", "")).lower() == wanted
        ]
    return entries


def _validate_catalog_payload(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Catalog payload must be a JSON object")
    models = data.get("models", [])
    removed = data.get("removed", [])
    if not isinstance(models, list):
        raise ValueError("Catalog field 'models' must be an array")
    if not isinstance(removed, list):
        raise ValueError("Catalog field 'removed' must be an array")
    seen: set[str] = set()
    normalized_models: list[dict[str, Any]] = []

    def safe_relpath(model_name: str, field: str, value: Any, *, required: bool = False) -> str:
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValueError(f"{model_name}.{field} must be a string")
        relpath = value.strip().replace("\\", "/")
        if required and not relpath:
            raise ValueError(f"{model_name}.{field} is required")
        if relpath and (Path(relpath).is_absolute() or ".." in Path(relpath).parts):
            raise ValueError(f"{model_name}.{field} must be a safe relative path")
        return relpath

    for index, item in enumerate(models):
        if not isinstance(item, dict):
            raise ValueError(f"models[{index}] must be an object")
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError(f"models[{index}].name is required")
        if name in seen:
            raise ValueError(f"Duplicated model name: {name}")
        seen.add(name)
        normalized_item = dict(item)
        normalized_item["name"] = name
        normalized_item["relpath"] = safe_relpath(name, "relpath", item.get("relpath"), required=True)
        normalized_item["config_relpath"] = safe_relpath(name, "config_relpath", item.get("config_relpath"))
        auxiliary_relpaths = item.get("auxiliary_relpaths", [])
        if not isinstance(auxiliary_relpaths, list):
            raise ValueError(f"{name}.auxiliary_relpaths must be an array")
        normalized_auxiliary_relpaths: list[str] = []
        for auxiliary_index, auxiliary_relpath in enumerate(auxiliary_relpaths):
            normalized_auxiliary_relpaths.append(
                safe_relpath(name, f"auxiliary_relpaths[{auxiliary_index}]", auxiliary_relpath, required=True)
            )
        normalized_item["auxiliary_relpaths"] = normalized_auxiliary_relpaths
        normalized_models.append(normalized_item)
    normalized = dict(data)
    normalized["schema_version"] = int(data.get("schema_version") or 1)
    normalized["models"] = normalized_models
    removed_models = [str(item).strip() for item in removed if str(item).strip()]
    if removed_models:
        normalized["removed"] = removed_models
    else:
        normalized.pop("removed", None)
    return normalized


def cmd_debug_catalog_info(payload: dict[str, Any] | None = None) -> int:
    try:
        base_data = _base_catalog_data()
        override = _debug_catalog_raw()
        effective = (
            _apply_debug_catalog(base_data, override)
            if _debug_catalog_path().is_file()
            else _canonical_catalog_data(base_data)
        )
        emit("debug_catalog_info", {
            "baseCatalogPath": str(_model_catalog_path()),
            "debugCatalogPath": str(_debug_catalog_path()),
            "debugDir": str(_debug_dir()),
            "baseCatalog": _canonical_catalog_data(base_data),
            "debugCatalog": override,
            "effectiveCatalog": effective,
            "status": _debug_catalog_status(base_data),
        })
        return 0
    except Exception as exc:
        return emit_error("DEBUG_CATALOG_INFO_FAILED", str(exc), traceback.format_exc())


def cmd_debug_catalog_save(payload: dict[str, Any]) -> int:
    try:
        data = payload.get("catalog", payload)
        normalized = _validate_catalog_payload(data)
        overlay = _build_debug_catalog_overlay(normalized, _base_catalog_data())
        path = _debug_catalog_path()
        if _debug_catalog_has_changes(overlay):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(overlay, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        elif path.is_file():
            path.unlink()
        load_model_catalog.cache_clear()
        _model_index.cache_clear()
        return cmd_debug_catalog_info({})
    except Exception as exc:
        return emit_error("DEBUG_CATALOG_SAVE_FAILED", str(exc), traceback.format_exc())


def cmd_debug_catalog_reset(payload: dict[str, Any] | None = None) -> int:
    try:
        path = _debug_catalog_path()
        if path.is_file():
            path.unlink()
        load_model_catalog.cache_clear()
        _model_index.cache_clear()
        return cmd_debug_catalog_info({})
    except Exception as exc:
        return emit_error("DEBUG_CATALOG_RESET_FAILED", str(exc), traceback.format_exc())


def _read_text(path: Path | None) -> str:
    if not path or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def cmd_debug_model_config(payload: dict[str, Any]) -> int:
    try:
        action = str(payload.get("action") or "read")
        model_name = str(payload.get("model") or "").strip()
        if not model_name:
            raise ValueError("Missing model name")
        entry = get_any_model_entry(model_name)
        model_dir = payload.get("modelDir") or None
        if is_user_model_entry(entry) and action != "read":
            raise ValueError("Imported user models are read-only in Debug")

        base_path = base_config_path_for(entry, model_dir)

        if action in {"save", "save_downloaded"}:
            content = str(payload.get("content") or "")
            yaml.load(content, Loader=yaml.FullLoader)
            if base_path is None:
                raise ValueError("This model does not have a config path")
            base_path.parent.mkdir(parents=True, exist_ok=True)
            base_path.write_text(content, encoding="utf-8", newline="\n")
        elif action == "reset":
            pass
        elif action != "read":
            raise ValueError(f"Unsupported debug model config action: {action}")

        load_model_catalog.cache_clear()
        _model_index.cache_clear()
        next_entry = get_any_model_entry(model_name)
        next_base_path = base_config_path_for(next_entry, model_dir)
        effective_path = config_path_for(next_entry, model_dir)
        emit("debug_model_config", {
            "model": str(getattr(next_entry, "name", model_name)),
            "source": effective_source_for(next_entry),
            "readOnly": is_user_model_entry(next_entry),
            "baseConfigPath": str(next_base_path) if next_base_path else None,
            "effectiveConfigPath": str(effective_path) if effective_path else None,
            "baseContent": _read_text(next_base_path),
            "effectiveContent": _read_text(effective_path),
            "downloadedConfigExists": bool(next_base_path and next_base_path.is_file()),
            "status": _debug_catalog_status(),
        })
        return 0
    except Exception as exc:
        return emit_error("DEBUG_MODEL_CONFIG_FAILED", str(exc), traceback.format_exc())


def cmd_list_models(payload: dict[str, Any]) -> int:
    category = payload.get("category") or None
    supported_only = bool(payload.get("supportedOnly", True))
    include_local_state = bool(payload.get("includeLocalState", True))
    include_custom = bool(payload.get("includeCustom", True))
    model_dir = payload.get("modelDir") or None

    try:
        catalog_state = load_model_catalog()
    except Exception:
        catalog_state = {}
    entries: list[Any] = list(list_catalog_models(category=category, supported=True if supported_only else None))
    if include_custom:
        # Appended after the catalog so the default ordering keeps imported models together at
        # the end; the UI sorts on top of this anyway.
        entries.extend(list_registered_user_models(category=category))
    models = [model_to_dict(entry, model_dir, include_local_state) for entry in entries]
    category_pairs = sorted({
        (m["category"], m.get("categoryCn") or m["category"])
        for m in models
        if m.get("category")
    }, key=lambda item: item[1] or item[0])
    debug_status = catalog_state.get("debug_status") if isinstance(catalog_state, dict) else None
    emit("models", {
        "models": models,
        "categories": [item[0] for item in category_pairs],
        "categoriesCn": [item[1] for item in category_pairs],
        "count": len(models),
        "modelDir": str(model_root(model_dir)),
        "debugStatus": debug_status if isinstance(debug_status, dict) else _inactive_debug_status(),
    })
    return 0


def get_any_model_entry(model_name: str) -> Any:
    """Resolve a name against imported models first, then the catalog.

    Same precedence as pymss's own get_model_entry(), which is what inference resolves through —
    so a name means the same thing everywhere in the app."""
    try:
        from pymss.user_models import get_user_model_entry  # type: ignore
        return get_user_model_entry(model_name)
    except Exception:
        pass
    return get_catalog_model_entry(model_name)


def cmd_model_info(payload: dict[str, Any]) -> int:
    model_name = payload.get("model")
    if not model_name:
        return emit_error("MODEL_NOT_FOUND", "Missing model name")
    try:
        entry = get_any_model_entry(model_name)
    except KeyError as exc:
        return emit_error("MODEL_NOT_FOUND", str(exc))

    model_dir = payload.get("modelDir") or None
    emit("model_info", model_to_dict(entry, model_dir, include_local_state=True))
    return 0


def cmd_delete_model(payload: dict[str, Any]) -> int:
    task_id = payload.get("taskId") or None
    model_name = payload.get("model")
    if not model_name:
        emit("model_delete_failed", {
            "model": "",
            "deleted": [],
            "errors": ["Missing model name"],
            "completedFiles": 0,
            "totalFiles": 0,
            "progress": 0,
            "message": "Missing model name",
        }, task_id=task_id)
        return 1

    model_dir = payload.get("modelDir") or None

    def fail(message: str) -> int:
        emit("model_delete_failed", {
            "model": model_name,
            "deleted": [],
            "errors": [message],
            "completedFiles": 0,
            "totalFiles": 0,
            "progress": 0,
            "message": message,
        }, task_id=task_id)
        return 1

    # Deliberately NOT importing pymss's path helpers: they are catalog-only and would shadow
    # the source-aware ones in this module. get_model_entry is imported for its user-model
    # lookup, which is what lets the guard below recognise an imported model.
    try:
        from pymss.model_registry import get_model_entry  # type: ignore
    except Exception as exc:
        return fail(str(exc))

    try:
        entry = get_model_entry(model_name)
    except KeyError as exc:
        return fail(str(exc))

    # An imported model's weights are the user's own file, often outside the app entirely.
    # Deleting it here would be an unrecoverable surprise, so removal goes through
    # unregister_custom_model, which unregisters by default and only touches files it copied.
    if is_user_model_entry(entry):
        return fail(
            f"{model_name} is an imported custom model; remove it from the custom model list instead"
        )

    model_path = model_path_for(entry, model_dir)
    config_path = config_path_for(entry, model_dir)
    auxiliary_paths = auxiliary_paths_for(entry, model_dir)

    def expand_cleanup_paths(path: Path) -> list[Path]:
        part_path = path.with_name(path.name + ".part")
        return [
            path,
            Path(str(path) + ".aria2"),
            part_path,
            Path(str(part_path) + ".aria2"),
        ]

    candidate_roots = [model_path, *([config_path] if config_path is not None else []), *auxiliary_paths]
    all_paths: list[Path] = []
    for path in candidate_roots:
        for candidate in expand_cleanup_paths(path):
            if candidate not in all_paths:
                all_paths.append(candidate)

    if task_id is None:
        deleted: list[str] = []
        errors: list[str] = []
        for path in all_paths:
            if not path.is_file():
                continue
            try:
                path.unlink()
                deleted.append(str(path))
            except Exception as exc:
                errors.append(f"{path}: {exc}")
        emit("model_deleted", {
            "model": entry.name,
            "deleted": deleted,
            "errors": errors,
            "modelInfo": model_to_dict(entry, model_dir, include_local_state=True),
        })
        return 0

    existing_paths = [path for path in all_paths if path.is_file()]
    total_files = len(existing_paths)
    deleted: list[str] = []
    errors: list[str] = []

    emit("model_delete_started", {
        "model": entry.name,
        "totalFiles": total_files,
        "completedFiles": 0,
        "progress": 0,
        "message": "Deleting model files",
    }, task_id=task_id)

    try:
        for index, path in enumerate(existing_paths, start=1):
            try:
                path.unlink()
                deleted.append(str(path))
            except Exception as exc:
                detail = f"{path}: {exc}"
                errors.append(detail)
                emit("model_delete_failed", {
                    "model": entry.name,
                    "deleted": deleted,
                    "errors": errors,
                    "path": str(path),
                    "completedFiles": len(deleted),
                    "totalFiles": total_files,
                    "progress": int((len(deleted) / total_files) * 100) if total_files > 0 else 0,
                    "message": str(exc),
                    "modelInfo": model_to_dict(entry, model_dir, include_local_state=True),
                }, task_id=task_id)
                return 1

            emit("model_delete_progress", {
                "model": entry.name,
                "path": str(path),
                "completedFiles": index,
                "totalFiles": total_files,
                "progress": int((index / total_files) * 100) if total_files > 0 else 100,
                "message": "Deleting model files",
            }, task_id=task_id)

        emit("model_delete_done", {
            "model": entry.name,
            "deleted": deleted,
            "errors": errors,
            "completedFiles": total_files,
            "totalFiles": total_files,
            "progress": 100,
            "message": "Deleting model files",
            "modelInfo": model_to_dict(entry, model_dir, include_local_state=True),
        }, task_id=task_id)
        return 0
    except Exception as exc:
        errors.append(str(exc))
        emit("model_delete_failed", {
            "model": entry.name,
            "deleted": deleted,
            "errors": errors,
            "completedFiles": len(deleted),
            "totalFiles": total_files,
            "progress": int((len(deleted) / total_files) * 100) if total_files > 0 else 0,
            "message": str(exc),
            "modelInfo": model_to_dict(entry, model_dir, include_local_state=True),
        }, task_id=task_id)
        return 1


def _path_size(path: Path) -> int:
    try:
        if path.is_file():
            return int(path.stat().st_size)
        if path.is_dir():
            return sum(_path_size(child) for child in path.rglob("*") if child.is_file())
    except Exception:
        return 0
    return 0


def _normalized_path_key(path: Path) -> str:
    return os.path.normcase(str(path.absolute()))


def _scan_root_file_sizes(root: Path) -> dict[str, tuple[Path, int]]:
    scanned: dict[str, tuple[Path, int]] = {}
    if not root.exists():
        return scanned
    for dirpath, _, filenames in os.walk(root):
        base = Path(dirpath)
        for filename in filenames:
            file_path = base / filename
            try:
                size = int(file_path.stat().st_size)
            except Exception:
                continue
            scanned[_normalized_path_key(file_path)] = (file_path, size)
    return scanned


def _is_tool_model_file(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root / "_tool_models")
        return True
    except ValueError:
        return False


def _prune_empty_tool_model_cache_dirs(root: Path) -> None:
    """Remove empty managed tool-cache children while retaining each cache root."""

    tool_models_root = root / "_tool_models"
    if not tool_models_root.is_dir():
        return
    try:
        cache_roots = list(tool_models_root.glob("*/models"))
    except OSError:
        return
    for models_root in cache_roots:
        if not models_root.is_dir():
            continue
        for dirpath, _, _ in os.walk(models_root, topdown=False):
            directory = Path(dirpath)
            if directory == models_root:
                continue
            try:
                directory.rmdir()
            except OSError:
                # Non-empty and concurrently used directories must remain untouched.
                continue


def _modelscope_model_name(cache_name: str) -> str:
    owner, separator, model = cache_name.partition("--")
    return f"{owner}/{model}" if separator and owner and model else cache_name


def _asr_model_role(name: str) -> str:
    lowered = name.lower()
    if "vad" in lowered:
        return "vad"
    if "punc" in lowered:
        return "punctuation"
    return "recognition"


def _tool_model_storage_items(
    root: Path,
    scanned_files: dict[str, tuple[Path, int]],
) -> list[dict[str, Any]]:
    models_root = root / "_tool_models" / "asr" / "models"
    grouped: dict[str, dict[str, Any]] = {}
    for file_path, size in scanned_files.values():
        try:
            relative = file_path.relative_to(models_root)
        except ValueError:
            continue
        if len(relative.parts) < 2:
            continue
        cache_name = relative.parts[0]
        item = grouped.setdefault(cache_name, {"sizeBytes": 0, "fileCount": 0})
        item["sizeBytes"] += size
        item["fileCount"] += 1

    items = []
    for cache_name, usage in grouped.items():
        path = models_root / cache_name
        if not is_complete_asr_model_cache(path):
            continue
        name = _modelscope_model_name(cache_name)
        items.append({
            "id": cache_name,
            "name": name,
            "tool": "asr",
            "role": _asr_model_role(name),
            "path": str(path),
            "sizeBytes": usage["sizeBytes"],
            "fileCount": usage["fileCount"],
        })
    return sorted(items, key=lambda item: item["name"].casefold())


def _required_model_paths(entry: Any, model_dir: str | None) -> list[Path]:
    from pymss.model_registry import auxiliary_paths_for, config_path_for, model_path_for  # type: ignore

    paths = [model_path_for(entry, model_dir)]
    config = config_path_for(entry, model_dir)
    if config is not None:
        paths.append(config)
    paths.extend(auxiliary_paths_for(entry, model_dir))
    return paths


def _storage_summary_payload(model_dir: str | None = None) -> dict[str, Any]:
    from pymss.model_registry import list_models, model_root  # type: ignore

    root = model_root(model_dir)
    scanned_files = _scan_root_file_sizes(root)
    tool_models = _tool_model_storage_items(root, scanned_files)
    complete_tool_model_roots = [Path(item["path"]).absolute() for item in tool_models]
    known_file_keys: set[str] = set()
    models: list[dict[str, Any]] = []
    total_bytes = 0
    tool_models_bytes = 0
    downloaded_count = 0

    for entry in list_models(supported=None):
        required_paths = _required_model_paths(entry, model_dir)
        files = []
        model_size = 0
        downloaded = True
        for path in required_paths:
            normalized_key = _normalized_path_key(path)
            known_file_keys.add(normalized_key)
            scanned = scanned_files.get(normalized_key)
            if scanned is not None:
                exists = True
                size = scanned[1]
            elif path.is_file():
                exists = True
                size = _path_size(path)
            else:
                exists = False
                size = 0
            if not exists:
                downloaded = False
            model_size += size
            files.append({"path": str(path), "sizeBytes": size, "exists": exists})
        if downloaded:
            downloaded_count += 1
        if model_size > 0:
            total_bytes += model_size
        models.append({
            "name": entry.name,
            "downloaded": downloaded,
            "sizeBytes": model_size,
            "expectedSizeBytes": entry.size_bytes,
            "files": files,
        })

    residual_files: list[dict[str, Any]] = []
    residual_bytes = 0
    for normalized_key, (file_path, size) in scanned_files.items():
        if normalized_key in known_file_keys:
            continue
        # Tool-specific model caches share the configured model root so directory migration
        # and storage placement remain consistent. Complete caches are managed by their owning
        # tool; incomplete caches remain residual files so space cleanup can remove them.
        if _is_tool_model_file(file_path, root):
            absolute_path = file_path.absolute()
            if any(
                absolute_path == model_root or model_root in absolute_path.parents
                for model_root in complete_tool_model_roots
            ):
                tool_models_bytes += size
                continue
        residual_files.append({"path": str(file_path), "sizeBytes": size})
        residual_bytes += size

    residual_files.sort(key=lambda item: item["sizeBytes"], reverse=True)
    models.sort(key=lambda item: item["sizeBytes"], reverse=True)
    return {
        "modelDir": str(root),
        "totalBytes": total_bytes + tool_models_bytes,
        "toolModelsBytes": tool_models_bytes,
        "downloadedCount": downloaded_count + len(tool_models),
        "models": models,
        "toolModels": tool_models,
        "residualFiles": residual_files,
        "residualBytes": residual_bytes,
    }


def cmd_model_storage_summary(payload: dict[str, Any]) -> int:
    model_dir = payload.get("modelDir") or None
    try:
        emit("model_storage_summary", _storage_summary_payload(model_dir))
        return 0
    except Exception as exc:
        return emit_error("MODEL_STORAGE_SUMMARY_FAILED", str(exc), traceback.format_exc())


def cmd_delete_tool_model(payload: dict[str, Any]) -> int:
    model_dir = payload.get("modelDir") or None
    tool = str(payload.get("tool") or "").strip().lower()
    model_id = str(payload.get("id") or "").strip()
    missing_ok = bool(payload.get("missingOk"))
    try:
        if tool != "asr":
            raise ValueError("Unsupported tool model type")
        if not model_id or model_id in {".", ".."} or "/" in model_id or "\\" in model_id:
            raise ValueError("Invalid tool model identifier")

        from pymss.model_registry import model_root  # type: ignore

        root = model_root(model_dir)
        models_root = (root / "_tool_models" / "asr" / "models").resolve()
        target = (models_root / model_id).resolve()
        if target.parent != models_root:
            raise ValueError("Tool model path escapes the managed cache")
        if not target.is_dir() and not missing_ok:
            raise FileNotFoundError("Tool model is not installed")

        if target.is_dir():
            shutil.rmtree(target)
        summary = _storage_summary_payload(model_dir)
        emit("tool_model_deleted", {
            "id": model_id,
            "tool": tool,
            "modelStorageSummary": summary,
        })
        return 0
    except Exception as exc:
        return emit_error("TOOL_MODEL_DELETE_FAILED", str(exc), traceback.format_exc())


def cmd_cleanup_model_residual_files(payload: dict[str, Any]) -> int:
    model_dir = payload.get("modelDir") or None
    task_id = payload.get("taskId") or None
    try:
        summary = _storage_summary_payload(model_dir)
        if task_id is None:
            deleted: list[str] = []
            errors: list[str] = []
            for item in summary.get("residualFiles", []):
                path = Path(item.get("path", ""))
                if not path.is_file():
                    continue
                try:
                    path.unlink()
                    deleted.append(str(path))
                except Exception as exc:
                    errors.append(f"{path}: {exc}")
            _prune_empty_tool_model_cache_dirs(Path(summary["modelDir"]))
            emit("model_residual_cleaned", {
                "deleted": deleted,
                "errors": errors,
                "modelStorageSummary": _storage_summary_payload(model_dir),
            })
            return 0
        residual_items = [item for item in summary.get("residualFiles", []) if Path(item.get("path", "")).is_file()]
        total_files = len(residual_items)
        deleted: list[str] = []
        errors: list[str] = []

        emit("model_residual_cleanup_started", {
            "totalFiles": total_files,
            "completedFiles": 0,
            "progress": 0,
            "message": "Cleaning residual files",
        }, task_id=task_id)

        for index, item in enumerate(residual_items, start=1):
            path = Path(item.get("path", ""))
            try:
                path.unlink()
                deleted.append(str(path))
            except Exception as exc:
                detail = f"{path}: {exc}"
                errors.append(detail)
                _prune_empty_tool_model_cache_dirs(Path(summary["modelDir"]))
                emit("model_residual_cleanup_failed", {
                    "deleted": deleted,
                    "errors": errors,
                    "path": str(path),
                    "completedFiles": len(deleted),
                    "totalFiles": total_files,
                    "progress": int((len(deleted) / total_files) * 100) if total_files > 0 else 0,
                    "message": str(exc),
                    "modelStorageSummary": _storage_summary_payload(model_dir),
                }, task_id=task_id)
                return 1
            emit("model_residual_cleanup_progress", {
                "path": str(path),
                "completedFiles": index,
                "totalFiles": total_files,
                "progress": int((index / total_files) * 100) if total_files > 0 else 100,
                "message": "Cleaning residual files",
            }, task_id=task_id)
        _prune_empty_tool_model_cache_dirs(Path(summary["modelDir"]))
        next_summary = _storage_summary_payload(model_dir)
        emit("model_residual_cleanup_done", {
            "deleted": deleted,
            "errors": errors,
            "completedFiles": total_files,
            "totalFiles": total_files,
            "progress": 100,
            "message": "Cleaning residual files",
            "modelStorageSummary": next_summary,
        }, task_id=task_id)
        return 0
    except Exception as exc:
        emit("model_residual_cleanup_failed", {
            "deleted": [],
            "errors": [str(exc)],
            "completedFiles": 0,
            "totalFiles": 0,
            "progress": 0,
            "message": str(exc),
        }, task_id=task_id)
        return 1
