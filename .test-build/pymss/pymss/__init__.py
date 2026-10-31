"""Public Python API for pymss.

pymss provides model catalog helpers, model downloading, audio I/O, ensemble
utilities, logging helpers, and the ``MSSeparator`` runtime for music source
separation. Most users can import from this top-level package instead of
importing submodules directly.

Exports:
    MSSeparator: Main runtime class for loading separation models and producing
        stems. Prefer ``MSSeparator.from_model_name(...)`` for catalog models.
    ModelTypeDetectionError: RuntimeError subclass for architecture detection failures.
    detect_model_type: Detect an architecture from a loaded YAML configuration.
    get_separation_logger: Create or reuse the package logger.
    create_separator: Create ``MSSeparator`` from a catalog model name.
    get_model_entry: Resolve catalog metadata for one model name or alias.
    list_models: List model catalog entries.
    list_user_models: List locally registered custom models.
    register_model: Register a local model path/config under a reusable name.
    unregister_model: Remove a previously registered custom model.
    resolve_model: Resolve catalog or user model paths without constructing a
        separator.
    download_model: Download all files required by one catalog model.
    ensemble_audios: Load and combine multiple audio files.
    save_ensemble_audio: Ensemble multiple audio files and save the result.
    WorkflowRunner: Run a multi-model audio workflow.
    load_audio: Load an audio file into a NumPy array.
    save_audio: Save a NumPy audio array to wav/flac/mp3/m4a.

Example:
    >>> from pymss import MSSeparator
    >>> separator = MSSeparator.from_model_name(
    ...     "bs_roformer_voc_hyperacev2",
    ...     download=True,
    ...     model_dir="models",
    ... )
    >>> separator.process_folder("song.wav")

Example:
    >>> from pymss import download_model, list_models
    >>> models = list_models(supported=True)
    >>> download_model(models[0].name, model_dir="models")

Example:
    >>> from pymss import ensemble_audios, save_ensemble_audio
    >>> audio, sample_rate = ensemble_audios(["a.wav", "b.wav"], weights=[1, 1])
    >>> save_ensemble_audio(["a.wav", "b.wav"], "ensemble.wav")
"""

from .separator import MSSeparator
from pymss_core import ModelTypeDetectionError, detect_model_type
from .logger import get_separation_logger
from .model_registry import create_separator, get_model_entry, list_models, register_model, resolve_model, unregister_model
from .model_download import ProxyError, download_model
from .user_models import list_user_models
from .ensemble import ensemble_audios, save_ensemble_audio
from .audio_io import load_audio, save_audio
from .workflow import WorkflowRunner, load_workflow_file, run_workflow_file, validate_workflow

# Register built-in capabilities (DSP + channel ops + ensemble) into the plugin
# registry so they can be consumed by nodes, CLI, and library code uniformly.
# ensemble is registered as a side effect of importing .ensemble above.
from .plugins.builtins import register_builtin_capabilities as _register_builtin_caps

_register_builtin_caps()
# Register built-in codec capabilities (wav/flac/mp3/m4a/aac/opus/vorbis/ogg)
# so require_capability('opus_encode') etc. work without first calling save_audio.
from .plugins.codecs import register_builtin_codecs as _register_builtin_codecs

_register_builtin_codecs()

__all__ = (
    "MSSeparator",
    "ModelTypeDetectionError",
    "detect_model_type",
    "get_separation_logger",
    "create_separator",
    "get_model_entry",
    "list_models",
    "list_user_models",
    "register_model",
    "unregister_model",
    "resolve_model",
    "download_model",
    "ensemble_audios",
    "save_ensemble_audio",
    "WorkflowRunner",
    "load_workflow_file",
    "run_workflow_file",
    "validate_workflow",
    "load_audio",
    "save_audio",
)
