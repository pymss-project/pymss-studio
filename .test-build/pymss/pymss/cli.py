import argparse
import json
import sys
import warnings

from .ensemble import ENSEMBLE_ALGORITHMS, save_ensemble_audio
from .logger import get_separation_logger
from .model_download import download_all, download_model
from .model_registry import create_separator, list_models, register_model, resolve_model, unregister_model
from .progress import _CliInferenceProgress
from .user_models import KNOWN_MODEL_TYPES, list_user_models
from .workflow import load_workflow_file, validate_workflow, write_workflow_template

warnings.filterwarnings("ignore", category=UserWarning)


def _parse_key_value(values):
    """Parse key value.

    Args:
        values (Any): Values value.

    Returns:
        Any: Parsed value."""
    result = {}
    for value in values or []:
        if "=" not in value:
            raise argparse.ArgumentTypeError(f"Expected key=value, got {value!r}")
        key, raw = value.split("=", 1)
        lowered = raw.lower()
        if lowered in {"true", "false"}:
            result[key] = lowered == "true"
        else:
            try:
                result[key] = int(raw)
            except ValueError:
                try:
                    result[key] = float(raw)
                except ValueError:
                    result[key] = raw
    return result


def cmd_list(args):
    """Implement the cmd list helper.

    Args:
        args (argparse.Namespace): Parsed command-line arguments.

    Returns:
        Any: Computed result."""
    if getattr(args, "user_only", False):
        rows = list_user_models()
        if args.category:
            category = args.category.lower()
            rows = [
                item
                for item in rows
                if item.primary_category.lower() == category
                or item.secondary_category.lower() == category
                or item.category_path.lower() == category
            ]
        if not args.all:
            rows = [item for item in rows if item.supported]
    else:
        rows = list_models(
            category=args.category,
            supported=None if args.all else True,
            include_user=True,
        )
    if args.json:
        print(json.dumps([item.__dict__ for item in rows], ensure_ascii=False, indent=2))
        return 0
    for item in rows:
        status = "ok" if item.supported else item.unsupported_reason
        category = item.category_path or item.primary_category
        source = getattr(item, "source", "catalog")
        print(f"{item.name}\t{item.model_type or item.architecture}\t{category}\t{item.target_stem}\t{status}\t{source}")
    return 0


def cmd_info(args):
    """Implement the cmd info helper.

    Args:
        args (argparse.Namespace): Parsed command-line arguments.

    Returns:
        Any: Computed result."""
    resolved = resolve_model(args.model, model_dir=args.model_dir, require_supported=False, require_exists=False)
    entry = resolved["entry"]
    data = {
        "name": entry.name,
        "source": resolved.get("source", getattr(entry, "source", "catalog")),
        "model_type": entry.model_type,
        "architecture": entry.architecture,
        "supported": entry.supported,
        "unsupported_reason": entry.unsupported_reason,
        "category": entry.category_path or entry.primary_category,
        "category_cn": " / ".join(filter(None, [entry.primary_category_cn, entry.secondary_category_cn])),
        "target_stem": entry.target_stem,
        "model_path": resolved["model_path"],
        "config_path": resolved["config_path"],
        "size_bytes": entry.size_bytes,
        "aliases": list(getattr(entry, "aliases", ()) or ()),
        "inference_params": dict(resolved.get("inference_params") or getattr(entry, "inference_params", {}) or {}),
    }
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_register(args):
    """Register a local custom model under a reusable name."""
    inference_params = _parse_key_value(getattr(args, "param", None))
    entry = register_model(
        args.name,
        args.type,
        args.model,
        config_path=args.config,
        aliases=args.alias or None,
        force=args.force,
        require_exists=not args.allow_missing,
        overlap_size=args.overlap_size,
        inference_params=inference_params or None,
    )
    print(f"registered {entry.name} ({entry.model_type})")
    print(f"  model:  {entry.model_path}")
    print(f"  config: {entry.config_path or '(none)'}")
    if entry.inference_params:
        print(f"  inference_params: {entry.inference_params}")
    return 0


def cmd_unregister(args):
    """Remove a previously registered user model."""
    entry = unregister_model(args.name)
    print(f"unregistered {entry.name}")
    return 0


def cmd_install(args):
    """Install a plugin by name, URL, or local path."""
    from .plugins.install import install as plugin_install, InstallError

    try:
        result = plugin_install(
            args.target,
            subpath=getattr(args, "subpath", None),
            no_deps=getattr(args, "no_deps", False),
        )
    except InstallError as exc:
        print(f"pymss install: error: {exc}", file=sys.stderr)
        return 1
    print(f"Installed plugin '{result.name}' -> {result.path} (from {result.source})")
    if result.version:
        print(f"  version: {result.version}")
    if result.dependencies_installed:
        deps = ", ".join(result.dependencies_installed)
        print(f"  dependencies installed: {deps}")
    print("It will be loaded on the next pymss run. Run `pymss plugins list` to verify.")
    return 0


def cmd_uninstall(args):
    """Remove an installed plugin."""
    from .plugins.install import uninstall as plugin_uninstall, InstallError

    try:
        removed = plugin_uninstall(args.name)
    except InstallError as exc:
        print(f"pymss uninstall: error: {exc}", file=sys.stderr)
        return 1
    print(f"Removed plugin '{args.name}' ({removed})")
    return 0


def cmd_plugins(args):
    """Handle plugin subcommands: list / dir / available / search / update."""
    from .plugins import bootstrap, get_plugins_dir, get_last_report
    from .plugins.install import (
        list_available, search_available, list_installed,
        check_update, update as plugin_update, InstallError,
    )

    cmd = getattr(args, "plugins_command", None) or "list"

    if cmd == "dir":
        print(get_plugins_dir())
        return 0

    if cmd == "validate":
        from pathlib import Path

        from .plugins.manifest import ManifestError, read_manifest

        try:
            manifest = read_manifest(Path(args.path))
        except ManifestError as exc:
            print(f"pymss plugins validate: error: {exc}", file=sys.stderr)
            return 1
        print(f"{manifest.plugin_id} {manifest.version or '?'} (api_version {manifest.api_version})")
        for operation in manifest.operations:
            print(f"  operation {operation.id}  {operation.title}")
        return 0

    if cmd == "available":
        try:
            entries = list_available()
        except InstallError as exc:
            print(f"pymss plugins available: error: {exc}", file=sys.stderr)
            return 1
        if not entries:
            print("Official registry is empty or unreachable.")
            return 0
        print(f"{len(entries)} plugin(s) in the official registry:")
        print("")
        for e in entries:
            mark = "[installed]" if e.get("installed") else "           "
            desc = e.get("description", "")
            print(f"  {mark} {e['name']:<16} {desc}")
        print("")
        print("Install with: pymss install <name>")
        return 0

    if cmd == "search":
        try:
            entries = search_available(args.query)
        except InstallError as exc:
            print(f"pymss plugins search: error: {exc}", file=sys.stderr)
            return 1
        if not entries:
            print(f"No plugins matched {args.query!r}.")
            return 0
        print(f"{len(entries)} plugin(s) matched {args.query!r}:")
        print("")
        for e in entries:
            mark = "[installed]" if e.get("installed") else "           "
            desc = e.get("description", "")
            print(f"  {mark} {e['name']:<16} {desc}")
        return 0

    if cmd == "update":
        try:
            info = check_update(args.name)
        except InstallError as exc:
            print(f"pymss plugins update: error: {exc}", file=sys.stderr)
            return 1
        if info["up_to_date"]:
            print(f"{args.name} is up to date (version {info['local_version']}).")
            return 0
        print(
            f"Updating {args.name}: {info['local_version']} -> "
            f"{info['remote_version']}"
        )
        try:
            result = plugin_update(args.name)
        except InstallError as exc:
            print(f"pymss plugins update: error: {exc}", file=sys.stderr)
            return 1
        print(f"Updated plugin '{result.name}' -> {result.path} (from {result.source})")
        if result.version:
            print(f"  version: {result.version}")
        return 0

    # Default: list installed.
    plugins_dir = get_plugins_dir()
    report = bootstrap()
    installed = {e["name"]: e for e in list_installed()}
    print(f"Plugins directory: {plugins_dir}")
    if not report.results:
        print("No plugins installed.")
        print("Browse with: pymss plugins available")
        return 0
    print("")
    for r in report.results:
        status = "OK" if r.loaded else f"FAILED ({r.error})"
        meta = installed.get(r.name, {})
        ver = meta.get("version") or "?"
        src = meta.get("source") or "?"
        print(f"  {r.name:<24} {status:<20} v{ver:<10} ({src})")
    failed = report.failed
    if failed:
        print(f"\n{len(failed)} plugin(s) failed to load.", file=sys.stderr)
    return 1 if failed else 0


def cmd_download(args):
    """Implement the cmd download helper.

    Args:
        args (argparse.Namespace): Parsed command-line arguments.

    Returns:
        Any: Computed result."""
    if args.model == "all":
        results = download_all(
            model_dir=args.model_dir,
            source=args.source,
            endpoint=args.endpoint,
            supported_only=args.supported_only,
            force=args.force,
        )
        failed = [item for item in results if item.get("error")]
        print(f"Downloaded/skipped {len(results) - len(failed)} model(s), failed {len(failed)}.")
        for item in failed:
            print(f"ERROR {item['entry'].name}: {item['error']}", file=sys.stderr)
        return 1 if failed else 0

    result = download_model(
        args.model,
        model_dir=args.model_dir,
        source=args.source,
        endpoint=args.endpoint,
        force=args.force,
    )
    _print_download_result(result)
    return 0


def _print_download_result(result):
    """Print download result.

    Args:
        result (Any): Result value.

    Returns:
        None: This callable completes for its side effects."""
    for path in result["skipped"]:
        print(f"exists {path}")
    for path in result["downloaded"]:
        print(f"downloaded {path}")


def _ensure_model_files(args):
    """Ensure model files.

    Args:
        args (argparse.Namespace): Parsed command-line arguments.

    Returns:
        None: This callable completes for its side effects."""
    preview = resolve_model(args.model, model_dir=args.model_dir, require_supported=True, require_exists=False)
    if preview.get("source") == "user":
        resolve_model(args.model, model_dir=args.model_dir, require_supported=True, require_exists=True)
        return
    try:
        resolve_model(args.model, model_dir=args.model_dir, require_supported=True, require_exists=True)
    except FileNotFoundError:
        result = download_model(args.model, model_dir=args.model_dir, source=args.source, endpoint=args.endpoint)
        _print_download_result(result)
    else:
        if args.download:
            result = download_model(args.model, model_dir=args.model_dir, source=args.source, endpoint=args.endpoint)
            _print_download_result(result)


def cmd_infer(args):
    """Run separation inference over an input file or folder."""
    from .plugins import ensure_plugins_loaded

    ensure_plugins_loaded()
    _ensure_model_files(args)
    logger = get_separation_logger()
    inference_progress = _CliInferenceProgress()
    with create_separator(
        args.model,
        model_dir=args.model_dir,
        device=args.device,
        device_ids=args.device_ids or [0],
        output_format=args.output_format,
        audio_params={
            "wav_bit_depth": args.wav_bit_depth,
            "flac_bit_depth": args.flac_bit_depth,
            "mp3_bit_rate": args.mp3_bit_rate,
            "m4a_bit_rate": args.m4a_bit_rate,
            "m4a_aac_at_quality": args.m4a_aac_at_quality,
        },
        use_tta=args.tta,
        store_dirs=args.output,
        save_as_folder=args.save_as_folder,
        logger=logger,
        debug=args.debug,
        progress_callback=inference_progress,
        inference_params=_parse_key_value(args.param),
    ) as separator:
        try:
            files = separator.process_folder(args.input)
        finally:
            inference_progress.close()
    logger.info(f"Processed {len(files)} file(s).")
    return 0


def cmd_ops(args):
    """Handle ``pymss ops list|describe|run`` over the Operation runtime."""
    from .operations import OperationContext, OperationError, default_runtime
    from .plugins import ensure_plugins_loaded

    if getattr(args, "ops_command", None) == "prepare-python":
        from .operations.python_profiles import prepare_python_environment

        try:
            profile = prepare_python_environment(args.directory, args.profiles_dir)
        except (OperationError, OSError, ValueError) as exc:
            print(f"pymss ops prepare-python: error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"profile": str(profile)}, ensure_ascii=False))
        return 0

    # The global loader imports plugin modules; their setup() registers
    # operations into the process default runtime. Using it here (instead of
    # an isolated runtime) means each plugin loads exactly once per process.
    ensure_plugins_loaded()
    runtime = default_runtime()
    cmd = getattr(args, "ops_command", None) or "list"

    if cmd == "list":
        ops = runtime.list_operations()
        if not ops:
            print("No operations registered (builtins arrive in round 7; plugins with setup() count).")
            return 0
        print(f"{len(ops)} operation(s):")
        for op in ops:
            print(f"  {op['id']}  v{op['schemaVersion']}  {op['title']}")
        return 0

    if cmd == "describe":
        try:
            spec = runtime.describe_operation(args.operation)
        except OperationError as exc:
            print(f"pymss ops describe: error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(spec, indent=2, ensure_ascii=False))
        return 0

    if cmd == "run":
        from .operations.host_services import ReadonlyHost
        params = _parse_key_value(args.param)
        inputs = {}
        for entry in args.input or []:
            if "=" not in entry:
                raise argparse.ArgumentTypeError(f"Expected name=path, got {entry!r}")
            name, path = entry.split("=", 1)
            inputs[name] = {"kind": "file", "path": path}
        try:
            result = runtime.run(
                args.operation,
                inputs=inputs,
                params=params,
                context=OperationContext(workdir=getattr(args, "output_dir", None), host_request=ReadonlyHost("cli", args.operation)),
            )
        except OperationError as exc:
            print(f"pymss ops run: error: {exc}", file=sys.stderr)
            detail = getattr(exc, "extra", {}).get("stderr")
            if isinstance(detail, str) and detail.strip():
                print(detail.strip()[-4000:], file=sys.stderr)
            return 1
        from .operations.files import FileArtifact

        print(json.dumps(result, indent=2, ensure_ascii=False, default=lambda value: value.to_dict() if isinstance(value, FileArtifact) else str(value)))
        return 0

    print(f"pymss ops: unknown subcommand {cmd!r}", file=sys.stderr)
    return 1


def cmd_ensemble(args):
    """Run the ensemble CLI command.

    Args:
        args (argparse.Namespace): Parsed command-line arguments.

    Returns:
        Any: Computed result."""
    logger = get_separation_logger()
    output_path = save_ensemble_audio(
        args.files,
        args.output,
        algorithm=args.algorithm,
        weights=args.weights,
        output_format=args.output_format,
        audio_params={
            "wav_bit_depth": args.wav_bit_depth,
            "flac_bit_depth": args.flac_bit_depth,
            "mp3_bit_rate": args.mp3_bit_rate,
            "m4a_bit_rate": args.m4a_bit_rate,
            "m4a_codec": args.m4a_codec,
            "m4a_aac_at_quality": args.m4a_aac_at_quality,
        },
        logger=logger,
    )
    logger.info(f"Saved ensemble audio to {output_path}")
    return 0


def cmd_workflow_init(args):
    """Write a starter workflow file."""
    path = write_workflow_template(args.output, overwrite=args.force)
    print(f"Wrote workflow template to {path}")
    return 0


def cmd_workflow_validate(args):
    """Validate a workflow file without running inference."""
    workflow = load_workflow_file(args.config)
    model_resolver = resolve_model if args.check_models or args.require_files else None
    validate_workflow(
        workflow,
        model_dir=args.model_dir,
        require_model_files=args.require_files,
        model_resolver=model_resolver,
    )
    print(f"Workflow is valid: {len(workflow.steps)} step(s).")
    return 0


def cmd_workflow_run(args):
    """Run an audio workflow from a YAML/JSON file via the unified DAG core."""
    from .plugins import ensure_plugins_loaded

    ensure_plugins_loaded()
    from .graph import LegacyWorkflowRunner

    logger = get_separation_logger()
    workflow = load_workflow_file(args.config)
    runner = LegacyWorkflowRunner(
        workflow,
        model_dir=args.model_dir,
        device=args.device,
        output_format=args.output_format,
        download=args.download,
        source=args.source,
        endpoint=args.endpoint,
        output_layout=args.output_layout,
        audio_params={
            "wav_bit_depth": args.wav_bit_depth,
            "flac_bit_depth": args.flac_bit_depth,
            "mp3_bit_rate": args.mp3_bit_rate,
            "m4a_bit_rate": args.m4a_bit_rate,
            "m4a_codec": args.m4a_codec,
            "m4a_aac_at_quality": args.m4a_aac_at_quality,
        },
        logger=logger,
        debug=args.debug,
    )
    files = runner.run(args.input, args.output)
    logger.info(f"Processed {len(files)} file(s).")
    return 0


def _parse_named_inputs(values):
    """Parse repeated ``--named-input name=path`` arguments into a mapping."""
    inputs = {}
    for value in values or []:
        name, sep, path = value.partition("=")
        if not sep or not name.strip() or not path.strip():
            raise SystemExit(f"invalid --named-input (expected name=path): {value!r}")
        inputs[name.strip()] = path.strip()
    return inputs


def cmd_comfy_run(args):
    """Run a native comfy-mss JSON workflow via the DAG core."""
    from .plugins import ensure_plugins_loaded

    ensure_plugins_loaded()
    from .graph import load_comfy_file, run_dag
    from .operations.host_services import ReadonlyHost

    logger = get_separation_logger()
    dag = load_comfy_file(args.config)
    saved = run_dag(
        dag,
        output_dir=args.output,
        host_request=ReadonlyHost("cli-graph"),
        input_path=args.input,
        inputs=_parse_named_inputs(getattr(args, "named_input", None)),
        logger=logger,
        debug=args.debug,
        strict=args.strict,
        model_dir=args.model_dir,
        download=args.download,
        source=args.source,
        endpoint=args.endpoint,
        device=args.device,
        output_format=args.output_format,
        audio_params={
            "wav_bit_depth": args.wav_bit_depth,
            "flac_bit_depth": args.flac_bit_depth,
            "mp3_bit_rate": args.mp3_bit_rate,
            "m4a_bit_rate": args.m4a_bit_rate,
            "m4a_codec": args.m4a_codec,
            "m4a_aac_at_quality": args.m4a_aac_at_quality,
        },
    )
    logger.info(f"Saved {len(saved)} file(s).")
    return 0


def cmd_serve(args):
    """Start the OpenAI-style HTTP inference server."""
    from .plugins import ensure_plugins_loaded

    # Load once at startup: plugins may provide codecs/capabilities used by
    # later requests; per-request loading would be redundant (idempotent) but
    # surfacing load failures at boot is the useful behavior.
    ensure_plugins_loaded()

    from .server import ServerConfig, run_server

    config = ServerConfig(
        model=args.model,
        model_dir=args.model_dir,
        source=args.source,
        endpoint=args.endpoint,
        device=args.device,
        device_ids=args.device_ids or [0],
        api_key=args.api_key,
        host=args.host,
        port=args.port,
        debug=args.debug,
        inference_params=_parse_key_value(args.param),
        max_audio_seconds=args.max_audio_seconds,
        max_request_bytes=args.max_request_bytes,
        max_queue_size=args.max_queue_size,
        request_timeout_seconds=args.request_timeout_seconds,
        webui=args.webui,
    )
    run_server(config)
    return 0


def build_parser():
    """Build the pymss command-line parser.

    Args:
        None: This callable does not accept user-provided arguments.

    Returns:
        argparse.ArgumentParser: Configured CLI parser."""
    parser = argparse.ArgumentParser(
        prog="pymss",
        description="Command-line interface for the pymss music source separation package.",
        formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, max_help_position=60),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ==========================
    # List models
    # ==========================
    list_parser = subparsers.add_parser(
        "list",
        help="List known models.",
        formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, max_help_position=60),
    )
    list_parser.add_argument("--category", help="Filter by primary or secondary category.")
    list_parser.add_argument("--all", action="store_true", help="Include models that are not supported for inference yet.")
    list_parser.add_argument("--user-only", action="store_true", help="List only locally registered user models.")
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(func=cmd_list)

    # ==========================
    # Show model info
    # ==========================
    info_parser = subparsers.add_parser(
        "info",
        help="Show model metadata.",
        formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, max_help_position=60),
    )
    info_parser.add_argument("model")
    info_parser.add_argument(
        "--model-dir",
        help="Local model cache directory. Defaults to PYMSS_MODEL_DIR, repository all_models if present, or ~/.cache/pymss/models.",
    )
    info_parser.set_defaults(func=cmd_info)

    # ==========================
    # Register / unregister user models
    # ==========================
    register_parser = subparsers.add_parser(
        "register",
        help="Register a local model path+config under a reusable name.",
        formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, max_help_position=60),
    )
    register_parser.add_argument("name", help="Name to use later with infer / from_model_name.")
    register_parser.add_argument(
        "--type",
        required=True,
        choices=sorted(KNOWN_MODEL_TYPES),
        help="Architecture / runtime model type.",
    )
    register_parser.add_argument("--model", required=True, help="Path to model weights.")
    register_parser.add_argument("--config", help="Path to YAML config (required for most model types).")
    register_parser.add_argument(
        "--overlap-size",
        type=int,
        help="Default inference overlap_size stored with this model (samples).",
    )
    register_parser.add_argument(
        "--param",
        action="append",
        default=[],
        help="Default inference override as key=value, for example --param batch_size=4.",
    )
    register_parser.add_argument("--alias", action="append", default=[], help="Optional alias. Can be repeated.")
    register_parser.add_argument("--force", action="store_true", help="Replace an existing user registration.")
    register_parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="Allow registering paths that do not exist yet.",
    )
    register_parser.set_defaults(func=cmd_register)

    unregister_parser = subparsers.add_parser(
        "unregister",
        help="Remove a previously registered user model.",
        formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, max_help_position=60),
    )
    unregister_parser.add_argument("name", help="Registered name or alias to remove.")
    unregister_parser.set_defaults(func=cmd_unregister)

    # ==========================
    # Download models
    # ==========================
    download_parser = subparsers.add_parser(
        "download",
        help="Download a model by name, or use 'all'.",
        formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, max_help_position=60),
    )
    download_parser.add_argument("model")
    download_parser.add_argument(
        "--model-dir",
        help="Local model cache directory. Defaults to PYMSS_MODEL_DIR, repository all_models if present, or ~/.cache/pymss/models.",
    )
    download_parser.add_argument("--source", default="modelscope", choices=["modelscope", "huggingface", "hf-mirror"])
    download_parser.add_argument("--endpoint", help="Custom resolve endpoint. It must serve files by relative path.")
    download_parser.add_argument("--force", action="store_true")
    download_parser.add_argument("--supported-only", action="store_true", help="Only used with model='all'.")
    download_parser.set_defaults(func=cmd_download)

    # ==========================
    # Plugins (install / uninstall / list)
    # ==========================
    install_parser = subparsers.add_parser(
        "install",
        help="Install a plugin by official name, git URL, or local path.",
        formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, max_help_position=60),
    )
    install_parser.add_argument(
        "target",
        help=(
            "One of:\n"
            "  <name>    official plugin name (resolved via the registry)\n"
            "  <url>     git repository URL to clone (append #path for a subdir)\n"
            "  <path>    local directory to copy"
        ),
    )
    install_parser.add_argument(
        "--subpath",
        default=None,
        help="Subdirectory inside the URL/path repo to install as the plugin.",
    )
    install_parser.add_argument(
        "--no-deps",
        action="store_true",
        help="Skip automatic installation of the plugin's Python dependencies.",
    )
    install_parser.set_defaults(func=cmd_install)

    uninstall_parser = subparsers.add_parser(
        "uninstall",
        help="Remove an installed plugin.",
        formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, max_help_position=60),
    )
    uninstall_parser.add_argument("name", help="Installed plugin name to remove.")
    uninstall_parser.set_defaults(func=cmd_uninstall)

    plugins_parser = subparsers.add_parser(
        "plugins",
        help="List installed plugins and their load status.",
        formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, max_help_position=60),
    )
    plugins_subparsers = plugins_parser.add_subparsers(
        dest="plugins_command", required=False
    )
    plugins_subparsers.add_parser("list", help="List installed plugins (default).")
    plugins_subparsers.add_parser("dir", help="Print the plugins directory path.")
    plugins_subparsers.add_parser(
        "available", help="List all plugins in the official registry."
    )
    search_parser = plugins_subparsers.add_parser(
        "search", help="Search the official registry by name/description/tag."
    )
    search_parser.add_argument("query", help="Search query (case-insensitive substring).")
    update_parser = plugins_subparsers.add_parser(
        "update", help="Reinstall a plugin at its latest version."
    )
    update_parser.add_argument("name", help="Plugin id of the installed plugin to update.")
    validate_parser = plugins_subparsers.add_parser(
        "validate", help="Check a plugin directory's v2 manifest without running its code."
    )
    validate_parser.add_argument("path", help="Plugin directory containing pyproject.toml.")
    plugins_parser.set_defaults(func=cmd_plugins, plugins_command="list")

    # ==========================
    # Inference
    # ==========================
    infer_parser = subparsers.add_parser(
        "infer",
        help="Run inference by model name.",
        formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, max_help_position=60),
    )
    infer_parser.add_argument("model")
    infer_parser.add_argument(
        "--model-dir",
        help="Local model cache directory. Defaults to PYMSS_MODEL_DIR, repository all_models if present, or ~/.cache/pymss/models.",
    )
    infer_parser.add_argument(
        "--download",
        action="store_true",
        help="Check/download the model before inference. Missing model files are downloaded automatically.",
    )
    infer_parser.add_argument("--source", default="modelscope", choices=["modelscope", "huggingface", "hf-mirror"])
    infer_parser.add_argument("--endpoint", help="Custom resolve endpoint. It must serve files by relative path.")
    infer_parser.add_argument("-i", "--input", required=True, help="Input audio file or folder.")
    infer_parser.add_argument("-o", "--output", default="results", help="Output folder.")
    infer_parser.add_argument(
        "--save-as-folder",
        action="store_true",
        help="Save each input audio file's separated stems in a subfolder named after the audio file.",
    )
    infer_parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda", "rocm", "mps", "npu", "mlx"])
    infer_parser.add_argument(
        "--device-id", action="append", type=int, dest="device_ids", help="CUDA device id. Can be repeated."
    )
    infer_parser.add_argument("--format", default="wav", choices=["wav", "flac", "mp3", "m4a"], dest="output_format")
    infer_parser.add_argument("--wav-bit-depth", default="FLOAT", choices=["FLOAT", "PCM_16", "PCM_24"])
    infer_parser.add_argument("--flac-bit-depth", default="PCM_16", choices=["PCM_16", "PCM_24"])
    infer_parser.add_argument("--mp3-bit-rate", default="320k")
    infer_parser.add_argument("--m4a-bit-rate", default="512k")
    infer_parser.add_argument("--m4a-codec", default="aac")
    infer_parser.add_argument("--m4a-aac-at-quality", default=2, type=int)
    infer_parser.add_argument("--tta", action="store_true", help="Enable test time augmentation.")
    infer_parser.add_argument("--debug", action="store_true")
    infer_parser.add_argument(
        "--param", action="append", default=[], help="Inference override as key=value, for example --param batch_size=2."
    )
    infer_parser.set_defaults(func=cmd_infer)

    # ==========================
    # Ensemble
    # ==========================
    ensemble_parser = subparsers.add_parser(
        "ensemble",
        help="Combine multiple audio files with an ensemble algorithm.",
        formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, max_help_position=60),
    )
    ensemble_parser.add_argument("files", nargs="+", help="Input audio files. At least two files are required.")
    ensemble_parser.add_argument(
        "-a",
        "--algorithm",
        default="avg_wave",
        choices=ENSEMBLE_ALGORITHMS,
        help="Ensemble algorithm.",
    )
    ensemble_parser.add_argument(
        "-w",
        "--weights",
        nargs="+",
        type=float,
        help="Input weights, for example --weights 1 0.8 1.2. Defaults to all 1.",
    )
    ensemble_parser.add_argument("-o", "--output", required=True, help="Output audio file.")
    ensemble_parser.add_argument("--format", choices=["wav", "flac", "mp3", "m4a"], dest="output_format")
    ensemble_parser.add_argument("--wav-bit-depth", default="FLOAT", choices=["FLOAT", "PCM_16", "PCM_24"])
    ensemble_parser.add_argument("--flac-bit-depth", default="PCM_16", choices=["PCM_16", "PCM_24"])
    ensemble_parser.add_argument("--mp3-bit-rate", default="320k")
    ensemble_parser.add_argument("--m4a-bit-rate", default="512k")
    ensemble_parser.add_argument("--m4a-codec", default="aac")
    ensemble_parser.add_argument("--m4a-aac-at-quality", default=2, type=int)
    ensemble_parser.set_defaults(func=cmd_ensemble)

    # ==========================
    # Workflow
    # ==========================
    workflow_parser = subparsers.add_parser(
        "workflow",
        help="Create, validate, or run an automatic multi-model workflow.",
        formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, max_help_position=60),
    )
    workflow_subparsers = workflow_parser.add_subparsers(dest="workflow_command", required=True)

    workflow_init_parser = workflow_subparsers.add_parser(
        "init",
        help="Write a starter workflow YAML file.",
        formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, max_help_position=60),
    )
    workflow_init_parser.add_argument("-o", "--output", default="workflow.yaml", help="Workflow file to create.")
    workflow_init_parser.add_argument("--force", action="store_true", help="Overwrite the output file if it exists.")
    workflow_init_parser.set_defaults(func=cmd_workflow_init)

    workflow_validate_parser = workflow_subparsers.add_parser(
        "validate",
        help="Validate a workflow YAML/JSON file.",
        formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, max_help_position=60),
    )
    workflow_validate_parser.add_argument("-c", "--config", required=True, help="Workflow YAML/JSON file.")
    workflow_validate_parser.add_argument(
        "--model-dir",
        help="Local model cache directory used when --require-files is set.",
    )
    workflow_validate_parser.add_argument(
        "--check-models",
        action="store_true",
        help="Also check that every referenced model exists in the catalog.",
    )
    workflow_validate_parser.add_argument(
        "--require-files",
        action="store_true",
        help="Also require every referenced catalog model file to exist locally.",
    )
    workflow_validate_parser.set_defaults(func=cmd_workflow_validate)

    workflow_run_parser = workflow_subparsers.add_parser(
        "run",
        help="Run inference through a workflow YAML/JSON file.",
        formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, max_help_position=60),
    )
    workflow_run_parser.add_argument("-c", "--config", required=True, help="Workflow YAML/JSON file.")
    workflow_run_parser.add_argument("-i", "--input", required=True, help="Input audio file or folder.")
    workflow_run_parser.add_argument("-o", "--output", default="results", help="Output folder.")
    workflow_run_parser.add_argument(
        "--output-layout",
        default="folders",
        choices=["folders", "flat"],
        help=(
            "Workflow output layout. 'folders' keeps each input under <output>/<audio>/; "
            "'flat' writes outputs directly under the workflow task folder and save subfolders."
        ),
    )
    workflow_run_parser.add_argument(
        "--model-dir",
        help="Local model cache directory. Workflow step model_dir values take precedence.",
    )
    workflow_run_parser.add_argument(
        "--download",
        action="store_true",
        help="Download missing model files before each workflow step is loaded.",
    )
    workflow_run_parser.add_argument("--source", default="modelscope", choices=["modelscope", "huggingface", "hf-mirror"])
    workflow_run_parser.add_argument("--endpoint", help="Custom resolve endpoint. It must serve files by relative path.")
    workflow_run_parser.add_argument("--device", choices=["auto", "cpu", "cuda", "rocm", "mps", "npu", "mlx"])
    workflow_run_parser.add_argument("--format", choices=["wav", "flac", "mp3", "m4a"], dest="output_format")
    workflow_run_parser.add_argument("--wav-bit-depth", default="FLOAT", choices=["FLOAT", "PCM_16", "PCM_24"])
    workflow_run_parser.add_argument("--flac-bit-depth", default="PCM_16", choices=["PCM_16", "PCM_24"])
    workflow_run_parser.add_argument("--mp3-bit-rate", default="320k")
    workflow_run_parser.add_argument("--m4a-bit-rate", default="512k")
    workflow_run_parser.add_argument("--m4a-codec", default="aac")
    workflow_run_parser.add_argument("--m4a-aac-at-quality", default=2, type=int)
    workflow_run_parser.add_argument("--debug", action="store_true")
    workflow_run_parser.set_defaults(func=cmd_workflow_run)

    # ==========================
    # Comfy (native comfy-mss JSON workflow via the DAG core)
    # ==========================
    comfy_parser = subparsers.add_parser(
        "comfy",
        help="Run a native comfy-mss JSON workflow through the DAG core (no ComfyUI needed).",
        formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, max_help_position=60),
    )
    comfy_subparsers = comfy_parser.add_subparsers(dest="comfy_command", required=True)

    comfy_run_parser = comfy_subparsers.add_parser(
        "run",
        help="Run a comfy-mss JSON workflow file.",
        formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, max_help_position=60),
    )
    comfy_run_parser.add_argument("-c", "--config", required=True, help="comfy-mss workflow JSON file.")
    comfy_run_parser.add_argument(
        "-i", "--input", help="Input audio file. Consumed by pymss_load_audio / input_audio nodes."
    )
    comfy_run_parser.add_argument(
        "--named-input",
        action="append",
        default=None,
        metavar="NAME=PATH",
        help="Named runtime input for pymss_load_audio nodes whose widget is a "
             "logical name (e.g. --named-input vocal=song_vocal.wav). Repeatable.",
    )
    comfy_run_parser.add_argument("-o", "--output", default="results", help="Output folder.")
    comfy_run_parser.add_argument("--model-dir", help="Local model cache directory.")
    comfy_run_parser.add_argument("--download", action="store_true", help="Download missing model files before running.")
    comfy_run_parser.add_argument("--source", default="modelscope", choices=["modelscope", "huggingface", "hf-mirror"])
    comfy_run_parser.add_argument("--endpoint", help="Custom resolve endpoint. It must serve files by relative path.")
    comfy_run_parser.add_argument("--device", choices=["auto", "cpu", "cuda", "rocm", "mps", "npu", "mlx"])
    comfy_run_parser.add_argument("--format", choices=["wav", "flac", "mp3", "m4a", "aac", "opus", "vorbis", "ogg"], dest="output_format")
    comfy_run_parser.add_argument("--strict", dest="strict", action="store_true", default=True, help="Fail on unknown node types (default).")
    comfy_run_parser.add_argument("--no-strict", dest="strict", action="store_false", help="Skip unknown node types with a warning.")
    comfy_run_parser.add_argument("--wav-bit-depth", default="FLOAT", choices=["FLOAT", "PCM_16", "PCM_24"])
    comfy_run_parser.add_argument("--flac-bit-depth", default="PCM_16", choices=["PCM_16", "PCM_24"])
    comfy_run_parser.add_argument("--mp3-bit-rate", default="320k")
    comfy_run_parser.add_argument("--m4a-bit-rate", default="512k")
    comfy_run_parser.add_argument("--m4a-codec", default="aac")
    comfy_run_parser.add_argument("--m4a-aac-at-quality", default=2, type=int)
    comfy_run_parser.add_argument("--debug", action="store_true")
    comfy_run_parser.set_defaults(func=cmd_comfy_run)

    # ==========================
    # Server
    # ==========================
    serve_parser = subparsers.add_parser(
        "serve",
        help="Start an OpenAI-style HTTP inference server.",
        formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, max_help_position=60),
    )
    serve_parser.add_argument("model", nargs="?")
    serve_parser.add_argument(
        "--model-dir",
        help="Local model cache directory. Defaults to PYMSS_MODEL_DIR, repository all_models if present, or ~/.cache/pymss/models.",
    )
    serve_parser.add_argument("--source", default="modelscope", choices=["modelscope", "huggingface", "hf-mirror"])
    serve_parser.add_argument("--endpoint", help="Custom resolve endpoint. It must serve files by relative path.")
    serve_parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda", "rocm", "mps", "npu", "mlx"])
    serve_parser.add_argument(
        "--device-id",
        action="append",
        type=int,
        dest="device_ids",
        help="CUDA device id. Can be repeated.",
    )
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", default=8000, type=int)
    serve_parser.add_argument("--api-key", help="Optional bearer token required for /v1/* endpoints.")
    serve_parser.add_argument("--debug", action="store_true")
    serve_parser.add_argument(
        "--param",
        action="append",
        default=[],
        help="Inference override as key=value, for example --param batch_size=2.",
    )
    serve_parser.add_argument("--max-audio-seconds", default=600.0, type=float)
    serve_parser.add_argument("--max-request-bytes", default=536870912, type=int)
    serve_parser.add_argument("--max-queue-size", default=8, type=int)
    serve_parser.add_argument("--request-timeout-seconds", default=0.0, type=float)
    serve_parser.add_argument("--webui", action="store_true", help="Serve the optional browser WebUI at /ui/.")
    serve_parser.set_defaults(func=cmd_serve)

    # ==========================
    # Operations (round 2)
    # ==========================
    ops_parser = subparsers.add_parser(
        "ops",
        help="List, describe, and run registered operations.",
        formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, max_help_position=60),
    )
    ops_subparsers = ops_parser.add_subparsers(dest="ops_command", required=False)
    ops_subparsers.add_parser("list", help="List registered operations (default).")
    prepare_parser = ops_subparsers.add_parser("prepare-python", help="Prepare an isolated Python process plugin from its local wheel lock.")
    prepare_parser.add_argument("directory", help="Plugin package directory.")
    prepare_parser.add_argument("--profiles-dir", required=True, help="Stable environment store outside the plugin directory.")
    describe_parser = ops_subparsers.add_parser("describe", help="Print one operation's spec as JSON.")
    describe_parser.add_argument("operation", help="Operation id, e.g. pymss.loudnorm.normalize.")
    run_parser = ops_subparsers.add_parser("run", help="Run an operation.")
    run_parser.add_argument("operation", help="Operation id.")
    run_parser.add_argument("--output-dir", help="Output directory for file-producing operations.")
    run_parser.add_argument(
        "--input",
        action="append",
        default=[],
        help="Named file input as name=path. Repeatable.",
    )
    run_parser.add_argument(
        "--param",
        action="append",
        default=[],
        help="Parameter as key=value (bool/int/float parsed automatically).",
    )
    ops_parser.set_defaults(func=cmd_ops, ops_command="list")

    return parser


def main(argv=None):
    """Run the pymss command-line interface.

    Manifest validation skips plugin loading. Other commands load user plugins
    before argument parsing so their registered CLI subcommands are available
    without replacing builtin subparsers.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if arguments[:2] != ["plugins", "validate"]:
        from .plugins import ensure_plugins_loaded

        try:
            ensure_plugins_loaded()
        except Exception as exc:  # noqa: BLE001 - plugin load errors must not kill the CLI
            print(f"pymss: warning: plugin loading failed: {exc}", file=sys.stderr)
        _mount_plugin_cli_commands(parser)
    args = parser.parse_args(arguments)
    try:
        return args.func(args)
    except Exception as exc:
        import traceback
        print(f"pymss error: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1


def _mount_plugin_cli_commands(parser) -> None:
    """Attach registry.cli_commands as argparse subparsers.

    A plugin command whose leading namespace collides with a builtin
    subcommand is skipped with a warning instead of shadowing it. Plugin
    commands are grouped under their source plugin in the help text.
    """
    from .plugins import get_registry

    registry = get_registry()
    if not registry.cli_commands:
        return
    # argparse exposes existing subparsers through _subparsers_action; the
    # choices dict maps the top-level name to its subparser.
    for action in getattr(parser, "_subparsers", None)._group_actions if getattr(parser, "_subparsers", None) else []:
        subparsers = action
        break
    else:
        return
    existing = set(subparsers.choices)
    for reg in registry.cli_commands:
        if not reg.path:
            continue
        top, *rest = reg.path
        if top in existing:
            print(
                f"pymss: warning: plugin '{reg.source}' command '{' '.join(reg.path)}' "
                f"collides with builtin command '{top}'; skipped",
                file=sys.stderr,
            )
            continue
        sub = subparsers.add_parser(
            top,
            help=f"[plugin:{reg.source}] {reg.help}".strip(),
        )
        if reg.add_arguments is not None:
            try:
                reg.add_arguments(sub)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"pymss: warning: plugin '{reg.source}' add_arguments failed: {exc}",
                    file=sys.stderr,
                )
                continue
        sub.set_defaults(func=reg.func)
        existing.add(top)
        if rest:
            print(
                f"pymss: warning: plugin '{reg.source}' command '{' '.join(reg.path)}' "
                f"uses a multi-segment path; only '{top}' is exposed",
                file=sys.stderr,
            )


if __name__ == "__main__":
    raise SystemExit(main())
