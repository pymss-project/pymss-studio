from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


class ConfigNode(dict[str, Any]):
    """Dictionary with attribute access for GAME's model constructors."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


def _to_config_node(value: Any) -> Any:
    if isinstance(value, dict):
        return ConfigNode({key: _to_config_node(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_to_config_node(item) for item in value]
    return value


def _required_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"GAME {label} must be a mapping")
    return value


def load_model_bundle(model_path: Path) -> tuple[ConfigNode, ConfigNode, dict[str, int] | None]:
    config_path = model_path.parent / "config.yaml"
    if not config_path.is_file():
        raise FileNotFoundError(
            "GAME config.yaml was not found beside model.pt; extract the complete release archive"
        )
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    root = _required_mapping(raw, "configuration")
    model = _to_config_node(_required_mapping(root.get("model"), "model configuration"))
    inference = _to_config_node(_required_mapping(root.get("inference"), "inference configuration"))

    features = getattr(inference, "features", None)
    if not isinstance(features, ConfigNode):
        raise ValueError("GAME inference.features is missing from config.yaml")
    sample_rate = int(getattr(features, "audio_sample_rate", 0))
    hop_size = int(getattr(features, "hop_size", 0))
    if sample_rate <= 0 or hop_size <= 0:
        raise ValueError("GAME audio sample rate and hop size must be positive")
    features.timestep = hop_size / sample_rate

    language_map: dict[str, int] | None = None
    if bool(getattr(model, "use_languages", False)):
        language_path = model_path.parent / "lang_map.json"
        if not language_path.is_file():
            raise FileNotFoundError(
                "GAME lang_map.json was not found beside model.pt; extract the complete release archive"
            )
        language_raw = json.loads(language_path.read_text(encoding="utf-8"))
        if not isinstance(language_raw, dict):
            raise ValueError("GAME lang_map.json must contain an object")
        language_map = {str(key): int(value) for key, value in language_raw.items()}

    return model, inference, language_map
