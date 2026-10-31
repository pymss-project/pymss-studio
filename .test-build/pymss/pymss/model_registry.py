import json
import os
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path


def _default_model_dir():
    """Implement the default model dir helper.

    Args:
        None: This callable does not accept user-provided arguments.

    Returns:
        Any: Computed result."""
    env_value = os.environ.get("PYMSS_MODEL_DIR")
    if env_value:
        return Path(env_value)
    repo_models = Path(__file__).resolve().parent.parent / "all_models"
    if repo_models.is_dir():
        return repo_models
    return Path.home() / ".cache" / "pymss" / "models"


DEFAULT_MODEL_DIR = _default_model_dir()


@dataclass(frozen=True)
class ModelEntry:
    """Catalog metadata for one downloadable pymss model."""

    name: str
    aliases: tuple
    model_type: str | None
    architecture: str
    supported: bool
    unsupported_reason: str
    relpath: str
    config_relpath: str
    auxiliary_relpaths: tuple
    size_bytes: int
    primary_category: str
    primary_category_cn: str
    secondary_category: str
    secondary_category_cn: str
    target_stem: str
    target_instrument_override: str = ""

    @property
    def stem(self):
        """Implement the stem helper.

        Args:
            None: This callable does not accept user-provided arguments.

        Returns:
            Any: Computed result."""
        return Path(self.name).stem

    @property
    def category_path(self):
        """Implement the category path helper.

        Args:
            None: This callable does not accept user-provided arguments.

        Returns:
            Any: Computed result."""
        return "/".join(part for part in (self.primary_category, self.secondary_category) if part)

    @classmethod
    def from_dict(cls, data):
        """Implement the from dict helper.

        Args:
            data (Mapping | None): Data value.

        Returns:
            Any: Computed result."""
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
            primary_category=data.get("primary_category", ""),
            primary_category_cn=data.get("primary_category_cn", ""),
            secondary_category=data.get("secondary_category", ""),
            secondary_category_cn=data.get("secondary_category_cn", ""),
            target_stem=data.get("target_stem", ""),
            target_instrument_override=data.get("target_instrument_override", ""),
        )


@lru_cache(maxsize=1)
def load_model_catalog():
    """Load model catalog.

    Args:
        None: This callable does not accept user-provided arguments.

    Returns:
        Any: Computed result."""
    with resources.files("pymss.resources").joinpath("model_catalog.json").open(encoding="utf-8") as f:
        data = json.load(f)
    models = [ModelEntry.from_dict(item) for item in data["models"]]
    return {**data, "models": models}


@lru_cache(maxsize=1)
def _model_index():
    """Implement the model index helper.

    Args:
        None: This callable does not accept user-provided arguments.

    Returns:
        Any: Computed result."""
    index = {}
    for entry in load_model_catalog()["models"]:
        names = {entry.name, entry.stem, *entry.aliases}
        for name in names:
            key = _normalize_model_name(name)
            if key in index and index[key].name != entry.name:
                continue
            index[key] = entry
    return index


def _normalize_model_name(name):
    """Normalize model name.

    Args:
        name (Any): Name value.

    Returns:
        Any: Computed result."""
    return str(name).strip().lower()


def list_models(category=None, supported=None, include_user=False):
    """List model catalog entries, optionally including user-registered models.

    The catalog contains every model known to pymss, including unsupported
    entries. Use the filters when building model selectors, download tools, or
    validation code.

    Args:
        category (str | None, optional): Optional category filter. The value is
            matched against primary category, secondary category, or combined
            ``primary/secondary`` category path. Matching is case-insensitive.
            Defaults to None.
        supported (bool | None, optional): Support-status filter. ``True``
            returns only models supported by the current inference code,
            ``False`` returns unsupported entries, and ``None`` returns all
            catalog entries. Defaults to None.
        include_user (bool, optional): When True, append locally registered
            user models after catalog entries. Defaults to False.

    Returns:
        list[ModelEntry | UserModelEntry]: Matching entries.

    Example:
        >>> from pymss import list_models
        >>> supported_models = list_models(supported=True)
        >>> supported_models[0].name

    Example:
        >>> vocal_models = list_models(category="vocal", supported=True)
        >>> [model.stem for model in vocal_models[:3]]"""
    from .user_models import list_user_models

    models = list(load_model_catalog()["models"])
    if include_user:
        models.extend(list_user_models())
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


def _catalog_has_name(model_name):
    return _normalize_model_name(model_name) in _model_index()


def get_model_entry(model_name):
    """Return catalog or user-model metadata for one model name or alias.

    User-registered models are checked first, then the built-in catalog.
    Matching is case-insensitive after stripping surrounding whitespace.

    Args:
        model_name (str): Full catalog filename, stem name, alias, or
            user-registered model name.

    Returns:
        ModelEntry | UserModelEntry: Entry containing architecture, support
        status, paths, and related metadata.

    Raises:
        KeyError: If ``model_name`` is unknown.

    Example:
        >>> from pymss import get_model_entry
        >>> entry = get_model_entry("bs_roformer_voc_hyperacev2")
        >>> entry.model_type
        'bs_roformer'

    Example:
        >>> entry.supported, entry.category_path
        (True, entry.category_path)"""
    from .user_models import get_user_model_entry

    try:
        return get_user_model_entry(model_name)
    except KeyError:
        pass
    try:
        return _model_index()[_normalize_model_name(model_name)]
    except KeyError as exc:
        raise KeyError(f"Unknown pymss model: {model_name}") from exc


def model_root(model_dir=None):
    """Implement the model root helper.

    Args:
        model_dir (str | os.PathLike | None, optional): Local model cache directory. Uses the package default when None. Defaults to None.

    Returns:
        Any: Computed result."""
    return Path(model_dir).expanduser() if model_dir else DEFAULT_MODEL_DIR


def model_path_for(entry, model_dir=None):
    """Implement the model path for helper.

    Args:
        entry (ModelEntry): Entry value.
        model_dir (str | os.PathLike | None, optional): Local model cache directory. Uses the package default when None. Defaults to None.

    Returns:
        Any: Computed result."""
    return model_root(model_dir) / entry.relpath


def config_path_for(entry, model_dir=None):
    """Implement the config path for helper.

    Args:
        entry (ModelEntry): Entry value.
        model_dir (str | os.PathLike | None, optional): Local model cache directory. Uses the package default when None. Defaults to None.

    Returns:
        Any: Computed result."""
    return model_root(model_dir) / entry.config_relpath if entry.config_relpath else None


def auxiliary_paths_for(entry, model_dir=None):
    """Implement the auxiliary paths for helper.

    Args:
        entry (ModelEntry): Entry value.
        model_dir (str | os.PathLike | None, optional): Local model cache directory. Uses the package default when None. Defaults to None.

    Returns:
        Any: Computed result."""
    root = model_root(model_dir)
    return [root / relpath for relpath in entry.auxiliary_relpaths]


def resolve_model(model_name, model_dir=None, require_supported=True, require_exists=True):
    """Resolve a catalog or user-registered model to local file paths.

    This function does not instantiate a model. It only translates a model name
    or alias into the local weights/config paths that ``MSSeparator`` will use.
    User-registered models are preferred over built-in catalog names.

    Args:
        model_name (str): Model name, stem, or alias from the pymss catalog or
            the local user model registry.
        model_dir (str | os.PathLike | None, optional): Local model cache
            directory for catalog models. When omitted, pymss uses
            ``PYMSS_MODEL_DIR`` if set, a repository-local ``all_models``
            directory if present, or the user cache under
            ``~/.cache/pymss/models``. Ignored for user-registered models that
            already store absolute paths. Defaults to None.
        require_supported (bool, optional): Whether unsupported catalog entries
            should raise ``ValueError``. Defaults to True.
        require_exists (bool, optional): Whether resolved model, config, and
            auxiliary files must already exist locally. Defaults to True.

    Returns:
        dict: Dictionary with ``entry``, ``model_type``, ``model_path``,
        ``config_path``, and ``source`` (``\"user\"`` or ``\"catalog\"``).

    Raises:
        KeyError: If the model name is unknown.
        ValueError: If the model is unsupported and ``require_supported`` is
            true.
        FileNotFoundError: If required local files are missing and
            ``require_exists`` is true.

    Example:
        >>> from pymss import resolve_model
        >>> resolved = resolve_model("bs_roformer_voc_hyperacev2", require_exists=False)
        >>> resolved["model_type"]
        'bs_roformer'

    Example:
        >>> resolved = resolve_model("bs_roformer_voc_hyperacev2", model_dir="models")
        >>> resolved["model_path"].endswith(".ckpt") or resolved["model_path"].endswith(".pth")
        True"""
    from .user_models import resolve_user_model

    try:
        return resolve_user_model(model_name, require_exists=require_exists)
    except KeyError:
        pass

    entry = _model_index()[_normalize_model_name(model_name)] if _catalog_has_name(model_name) else None
    if entry is None:
        raise KeyError(f"Unknown pymss model: {model_name}")
    if require_supported and not entry.supported:
        reason = entry.unsupported_reason or "unsupported"
        raise ValueError(f"Model {entry.name} cannot be used for inference yet: {reason}")

    model_path = model_path_for(entry, model_dir)
    config_path = config_path_for(entry, model_dir)
    missing = []
    if require_exists and not model_path.is_file():
        missing.append(str(model_path))
    if require_exists and config_path is not None and not config_path.is_file():
        missing.append(str(config_path))
    for path in auxiliary_paths_for(entry, model_dir):
        if require_exists and not path.is_file():
            missing.append(str(path))
    if missing:
        raise FileNotFoundError("Missing model file(s): " + ", ".join(missing))

    return {
        "entry": entry,
        "model_type": entry.model_type,
        "model_path": str(model_path),
        "config_path": str(config_path) if config_path else None,
        "source": "catalog",
        "inference_params": {},
        "target_instrument_override": entry.target_instrument_override or None,
    }


def register_model(
    name,
    model_type,
    model_path,
    config_path=None,
    aliases=None,
    force=False,
    require_exists=True,
    overlap_size=None,
    inference_params=None,
):
    """Register a local custom model for later use by name.

    After registration, the name works with ``resolve_model``,
    ``create_separator``, ``MSSeparator.from_model_name``, and ``pymss infer``.

    Args:
        name (str): Name to register.
        model_type (str): Architecture/runtime type.
        model_path (str | os.PathLike): Path to model weights.
        config_path (str | os.PathLike | None, optional): Path to YAML config.
            Required for most model types. Defaults to None.
        aliases (Sequence[str] | None, optional): Optional alternate names.
            Defaults to None.
        force (bool, optional): Replace an existing user registration.
            Defaults to False.
        require_exists (bool, optional): Require files to exist now.
            Defaults to True.
        overlap_size (int | None, optional): Default ``overlap_size`` stored with
            the registration. Defaults to None.
        inference_params (dict | None, optional): Default inference overrides
            stored with the registration. Defaults to None.

    Returns:
        UserModelEntry: Registered entry.
    """
    from .user_models import register_user_model

    return register_user_model(
        name,
        model_type,
        model_path,
        config_path=config_path,
        aliases=aliases,
        force=force,
        require_exists=require_exists,
        overlap_size=overlap_size,
        inference_params=inference_params,
        catalog_name_checker=_catalog_has_name,
    )


def unregister_model(name):
    """Remove a previously registered user model."""
    from .user_models import unregister_user_model

    return unregister_user_model(name)


def _merge_resolved_inference_params(resolved, inference_params=None):
    merged = dict(resolved.get("inference_params") or {})
    if inference_params:
        merged.update(inference_params)
    return merged


def create_separator(model_name, model_dir=None, **separator_kwargs):
    """Create ``MSSeparator`` from a catalog or user-registered model name.

    This is a convenience wrapper around ``resolve_model(...)`` followed by
    ``MSSeparator(...)``. It expects the model files to already exist locally;
    call ``download_model(...)`` first or use ``MSSeparator.from_model_name`` if
    you want optional downloading in one step.

    Args:
        model_name (str): Model name, stem, or alias from the pymss catalog.
        model_dir (str | os.PathLike | None, optional): Local model cache
            directory. Defaults to None.
        **separator_kwargs: Keyword arguments forwarded to ``MSSeparator``,
            such as ``device``, ``device_ids``, ``output_format``,
            ``store_dirs``, ``save_as_folder``, ``audio_params``, ``logger``,
            ``debug``, ``progress_callback``, and ``inference_params``.
            For user models, registered default ``inference_params`` are applied
            first; values passed here override them.

    Returns:
        MSSeparator: Loaded separator instance ready for inference.

    Raises:
        FileNotFoundError: If required model files are not present locally.

    Example:
        >>> from pymss import create_separator
        >>> separator = create_separator(
        ...     "bs_roformer_voc_hyperacev2",
        ...     model_dir="models",
        ...     output_format="wav",
        ...     inference_params={"normalize": True},
        ... )
        >>> separator.process_folder("song.wav")

    Example:
        >>> separator = create_separator(
        ...     "some_six_stem_model",
        ...     store_dirs={"vocals": "out/vocals", "drums": "out/drums"},
        ... )"""
    from .separator import MSSeparator

    resolved = resolve_model(model_name, model_dir=model_dir, require_supported=True, require_exists=True)
    separator_kwargs = dict(separator_kwargs)
    separator_kwargs["inference_params"] = _merge_resolved_inference_params(
        resolved,
        separator_kwargs.pop("inference_params", None),
    )
    if resolved.get("target_instrument_override") and separator_kwargs.get("target_instrument_override") is None:
        separator_kwargs["target_instrument_override"] = resolved["target_instrument_override"]
    return MSSeparator(
        model_type=resolved["model_type"],
        model_path=resolved["model_path"],
        config_path=resolved["config_path"],
        **separator_kwargs,
    )
