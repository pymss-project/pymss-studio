"""Local user-registered models for reuse by name."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


KNOWN_MODEL_TYPES = frozenset(
    {
        "apollo",
        "bandit",
        "bandit_v2",
        "bs_conformer",
        "bs_roformer",
        "bs_roformer_hyperace",
        "demucs",
        "htdemucs",
        "legacy_demucs",
        "legacy_tasnet",
        "mdx23c",
        "mel_band_conformer",
        "mel_band_roformer",
        "scnet",
        "tasnet",
        "vr",
    }
)

CONFIG_OPTIONAL_MODEL_TYPES = frozenset({"vr", "demucs", "tasnet", "legacy_demucs", "legacy_tasnet"})


def _default_user_models_path():
    env_value = os.environ.get("PYMSS_USER_MODELS")
    if env_value:
        return Path(env_value).expanduser()
    return Path.home() / ".cache" / "pymss" / "user_models.json"


DEFAULT_USER_MODELS_PATH = _default_user_models_path()


@dataclass(frozen=True)
class UserModelEntry:
    """User-registered model metadata stored outside the package catalog."""

    name: str
    model_type: str
    model_path: str
    config_path: str | None = None
    aliases: tuple = ()
    source: str = "user"
    architecture: str = ""
    supported: bool = True
    unsupported_reason: str = ""
    relpath: str = ""
    config_relpath: str = ""
    auxiliary_relpaths: tuple = ()
    size_bytes: int = 0
    primary_category: str = "user"
    primary_category_cn: str = "用户"
    secondary_category: str = "custom"
    secondary_category_cn: str = "自定义"
    target_stem: str = ""
    inference_params: dict = field(default_factory=dict)

    @property
    def stem(self):
        return Path(self.name).stem

    @property
    def category_path(self):
        return "/".join(part for part in (self.primary_category, self.secondary_category) if part)

    @classmethod
    def from_dict(cls, data):
        return cls(
            name=data["name"],
            model_type=data["model_type"],
            model_path=str(Path(data["model_path"]).expanduser()),
            config_path=(str(Path(data["config_path"]).expanduser()) if data.get("config_path") else None),
            aliases=tuple(data.get("aliases") or ()),
            architecture=data.get("architecture") or data.get("model_type", ""),
            supported=bool(data.get("supported", True)),
            unsupported_reason=data.get("unsupported_reason", ""),
            target_stem=data.get("target_stem", ""),
            primary_category=data.get("primary_category", "user"),
            primary_category_cn=data.get("primary_category_cn", "用户"),
            secondary_category=data.get("secondary_category", "custom"),
            secondary_category_cn=data.get("secondary_category_cn", "自定义"),
            inference_params=_normalize_inference_params(data.get("inference_params")),
        )

    def to_dict(self):
        payload = {
            "name": self.name,
            "model_type": self.model_type,
            "model_path": self.model_path,
            "config_path": self.config_path,
            "aliases": list(self.aliases),
            "architecture": self.architecture or self.model_type,
            "target_stem": self.target_stem,
        }
        if self.inference_params:
            payload["inference_params"] = dict(self.inference_params)
        return payload


def user_models_path(path=None):
    return Path(path).expanduser() if path else DEFAULT_USER_MODELS_PATH


def _normalize_name(name):
    return str(name).strip().lower()


def _validate_name(name):
    name = str(name).strip()
    if not name:
        raise ValueError("model name must be non-empty")
    if any(ch.isspace() for ch in name):
        raise ValueError(f"model name must not contain whitespace: {name!r}")
    return name


def _normalize_inference_params(params):
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ValueError("inference_params must be a dict")
    return {str(key): value for key, value in params.items() if value is not None}


def _load_raw(path=None):
    store = user_models_path(path)
    if not store.is_file():
        return {"version": 1, "models": []}
    with store.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or "models" not in data:
        raise ValueError(f"invalid user models file: {store}")
    return data


def _save_raw(data, path=None):
    store = user_models_path(path)
    store.parent.mkdir(parents=True, exist_ok=True)
    tmp = store.with_suffix(store.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(store)
    clear_user_model_caches()


@lru_cache(maxsize=8)
def load_user_models(path=None):
    store = user_models_path(path)
    raw = _load_raw(store)
    models = [UserModelEntry.from_dict(item) for item in raw.get("models", [])]
    return {"version": int(raw.get("version", 1)), "models": models, "path": str(store.resolve())}


@lru_cache(maxsize=8)
def _user_model_index(path=None):
    index = {}
    for entry in load_user_models(path)["models"]:
        names = {entry.name, entry.stem, *entry.aliases}
        for name in names:
            index[_normalize_name(name)] = entry
    return index


def clear_user_model_caches():
    load_user_models.cache_clear()
    _user_model_index.cache_clear()


def list_user_models(path=None):
    """List locally registered user models."""
    return list(load_user_models(path)["models"])


def get_user_model_entry(model_name, path=None):
    """Return one user model by name or alias."""
    try:
        return _user_model_index(path)[_normalize_name(model_name)]
    except KeyError as exc:
        raise KeyError(f"Unknown user model: {model_name}") from exc


def resolve_user_model(model_name, path=None, require_exists=True):
    """Resolve a user model to absolute local paths."""
    entry = get_user_model_entry(model_name, path=path)
    model_path = Path(entry.model_path)
    config_path = Path(entry.config_path) if entry.config_path else None
    missing = []
    if require_exists and not model_path.is_file():
        missing.append(str(model_path))
    if require_exists and config_path is not None and not config_path.is_file():
        missing.append(str(config_path))
    if missing:
        raise FileNotFoundError("Missing model file(s): " + ", ".join(missing))
    return {
        "entry": entry,
        "model_type": entry.model_type,
        "model_path": str(model_path),
        "config_path": str(config_path) if config_path else None,
        "source": "user",
        "inference_params": dict(entry.inference_params or {}),
    }


def register_user_model(
    name,
    model_type,
    model_path,
    config_path=None,
    aliases=None,
    *,
    overlap_size=None,
    inference_params=None,
    force=False,
    require_exists=True,
    path=None,
    catalog_name_checker=None,
):
    """Register a custom model name pointing at local weights/config.

    Args:
        name: Name used later with ``from_model_name`` / ``pymss infer``.
        model_type: Architecture key, for example ``bs_conformer``.
        model_path: Path to weights.
        config_path: Path to YAML config when required by the model type.
        aliases: Optional alternate names.
        overlap_size: Optional default ``inference.overlap_size`` stored with
            the registration and applied on later loads.
        inference_params: Optional default inference overrides (for example
            ``{\"overlap_size\": 44100, \"batch_size\": 4}``). Explicit
            ``overlap_size`` wins over a key inside this mapping.
        force: Replace an existing user model with the same name.
        require_exists: Require files to exist at registration time.
        path: Optional custom registry file path.
        catalog_name_checker: Optional callable ``(name) -> bool`` that returns
            True when ``name`` already exists in the built-in catalog.

    Returns:
        UserModelEntry: The registered entry.
    """
    name = _validate_name(name)
    model_type = str(model_type).strip()
    if model_type not in KNOWN_MODEL_TYPES:
        raise ValueError(f"unsupported model_type: {model_type!r}; expected one of {sorted(KNOWN_MODEL_TYPES)}")

    model_file = Path(model_path).expanduser().resolve()
    config_file = Path(config_path).expanduser().resolve() if config_path else None
    if require_exists and not model_file.is_file():
        raise FileNotFoundError(f"model file not found: {model_file}")
    if model_type not in CONFIG_OPTIONAL_MODEL_TYPES:
        if config_file is None:
            raise ValueError(f"config_path is required for model_type={model_type!r}")
        if require_exists and not config_file.is_file():
            raise FileNotFoundError(f"config file not found: {config_file}")
    elif config_file is not None and require_exists and not config_file.is_file():
        raise FileNotFoundError(f"config file not found: {config_file}")

    stored_params = _normalize_inference_params(inference_params)
    if overlap_size is not None:
        stored_params["overlap_size"] = int(overlap_size)

    aliases = tuple(_validate_name(alias) for alias in (aliases or ()))
    names = (name, *aliases)
    if catalog_name_checker:
        conflicts = [item for item in names if catalog_name_checker(item)]
        if conflicts:
            raise ValueError(
                "name/alias conflicts with built-in catalog: "
                + ", ".join(conflicts)
                + "; choose another name"
            )

    entry = UserModelEntry(
        name=name,
        model_type=model_type,
        model_path=str(model_file),
        config_path=str(config_file) if config_file else None,
        aliases=aliases,
        architecture=model_type,
        inference_params=stored_params,
    )

    data = _load_raw(path)
    models = []
    for item in data.get("models") or []:
        existing = UserModelEntry.from_dict(item)
        same_name = _normalize_name(existing.name) == _normalize_name(name)
        alias_overlap = bool(
            {_normalize_name(a) for a in (existing.name, *existing.aliases)}
            & {_normalize_name(a) for a in names}
        )
        if same_name or alias_overlap:
            if not force:
                raise ValueError(
                    f"name/alias already registered as user model {existing.name!r}; pass force=True to replace"
                )
            continue
        models.append(item)

    models.append(entry.to_dict())
    data["version"] = int(data.get("version", 1) or 1)
    data["models"] = models
    _save_raw(data, path)
    return entry


def unregister_user_model(name, path=None):
    """Remove a previously registered user model by name or alias."""
    entry = get_user_model_entry(name, path=path)
    data = _load_raw(path)
    before = len(data.get("models") or [])
    models = [
        item
        for item in data.get("models") or []
        if _normalize_name(item.get("name", "")) != _normalize_name(entry.name)
    ]
    if len(models) == before:
        raise KeyError(f"Unknown user model: {name}")
    data["models"] = models
    _save_raw(data, path)
    return entry
