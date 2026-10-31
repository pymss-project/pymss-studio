import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from tqdm import tqdm

from .model_registry import (
    auxiliary_paths_for,
    config_path_for,
    get_model_entry,
    model_path_for,
)


HF_REPO = "baicai1145/pymss"
MS_REPO = "baicai1145/pymss"
HF_BASE_URL = f"https://huggingface.co/{HF_REPO}/resolve/main"
MS_BASE_URL = f"https://www.modelscope.cn/models/{MS_REPO}/resolve/master"
MS_FILES_API = f"https://www.modelscope.cn/api/v1/models/{MS_REPO}/repo/files?Revision=master&Recursive=true"
MODEL_FILE_SUFFIXES = {".ckpt", ".th", ".pth", ".chpt", ".safetensors", ".pt", ".yaml", ".yml", ".json"}
ARIA2C_PATH = shutil.which("aria2c")

# Proxy schemes urllib can route through its ProxyHandler. SOCKS needs PySocks
# (the ``pymss[proxy]`` extra) to patch the socket module.
SOCKS_PROXY_SCHEMES = {"socks5", "socks5h", "socks4"}
ARIA2_PROGRESS_PATTERN = re.compile(
    r"\[#\S+\s+(?P<downloaded>[\d.]+\s*[KMGTPE]?i?B)/"
    r"(?P<total>[\d.]+\s*[KMGTPE]?i?B)"
    r"(?:\((?P<percent>\d+)%\))?.*?DL:(?P<speed>[\d.]+\s*[KMGTPE]?i?B)",
    re.IGNORECASE,
)
BYTE_UNITS = {
    "B": 1,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
    "PIB": 1024**5,
    "EIB": 1024**6,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "PB": 1000**5,
    "EB": 1000**6,
}


class DownloadError(RuntimeError):
    """Base exception raised when model download fails."""

    pass


class DownloadValidationError(DownloadError):
    """Exception raised when a downloaded file fails size or hash validation."""

    pass


class ProxyError(DownloadError):
    """Exception raised when a proxy cannot be used for the download.

    Covers malformed proxy URLs and SOCKS proxies used without PySocks
    installed (``pip install pymss[proxy]``).
    """

    pass


def _normalize_proxy(proxy):
    """Resolve the effective proxy URL for a download.

    ``None`` falls back to the standard ``HTTPS_PROXY``/``HTTP_PROXY``
    environment variables (lower-case variants included), matching pip,
    curl, and aria2. An empty string means "no proxy".

    Args:
        proxy (str | None): Explicit proxy URL (``http://``, ``https://``,
            ``socks5://``, ``socks5h://``, or ``socks4://``).

    Returns:
        str | None: The proxy URL to use, or None for a direct connection.

    Raises:
        ProxyError: If the proxy URL lacks a host or uses an unsupported scheme.
    """
    if proxy is None:
        for variable in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            value = os.environ.get(variable)
            if value:
                proxy = value
                break
        else:
            return None
    if not proxy:
        return None
    parsed = urllib.parse.urlparse(proxy)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https", *SOCKS_PROXY_SCHEMES}:
        raise ProxyError(
            f"unsupported proxy scheme {scheme!r} (expected http, https, socks5, socks5h, or socks4)"
        )
    if not parsed.hostname:
        raise ProxyError(f"proxy URL has no host: {proxy!r}")
    return proxy


def _socks_patch(proxy):
    """Return a context manager that routes sockets through a SOCKS proxy.

    PySocks works by replacing :func:`socket.socket`, so the patch is installed
    on enter and fully removed on exit.

    Args:
        proxy (str): SOCKS proxy URL (``socks5://``, ``socks5h://``, ``socks4://``).

    Returns:
        contextlib.AbstractContextManager: Context manager installing the patch.

    Raises:
        ProxyError: If PySocks is not installed.
    """
    try:
        import socks  # provided by the PySocks distribution
    except ImportError as exc:
        raise ProxyError(
            "SOCKS proxy requires PySocks: pip install pymss[proxy] (or pip install PySocks)"
        ) from exc

    import contextlib
    import socket

    parsed = urllib.parse.urlparse(proxy)
    scheme = (parsed.scheme or "").lower()
    proxy_type = socks.SOCKS4 if scheme == "socks4" else socks.SOCKS5
    # socks5h (and socks4a) hand DNS resolution to the proxy.
    remote_dns = scheme == "socks5h"
    hostname = parsed.hostname
    port = parsed.port or 1080
    username = urllib.parse.unquote(parsed.username) if parsed.username else None
    password = urllib.parse.unquote(parsed.password) if parsed.password else None

    @contextlib.contextmanager
    def _patched():
        original_socket = socket.socket
        try:
            socks.set_default_proxy(
                proxy_type,
                hostname,
                port,
                rdns=remote_dns,
                username=username,
                password=password,
            )
            socket.socket = socks.socksocket
            yield
        finally:
            socks.set_default_proxy()
            socket.socket = original_socket

    return _patched()


def _urlopen_with_proxy(url, proxy=None, timeout=30):
    """urlopen that honours an explicit or environment-derived proxy.

    Args:
        url (str): URL to open.
        proxy (str | None, optional): Proxy URL. None consults the standard
            proxy environment variables. Defaults to None.
        timeout (int, optional): Network timeout in seconds. Defaults to 30.

    Returns:
        http.client.HTTPResponse-like: The opened response.

    Raises:
        ProxyError: If the proxy is malformed or SOCKS is used without PySocks.
    """
    proxy = _normalize_proxy(proxy)
    if proxy is None:
        return urllib.request.urlopen(url, timeout=timeout)
    scheme = urllib.parse.urlparse(proxy).scheme.lower()
    if scheme in SOCKS_PROXY_SCHEMES:
        # With the socket patched, a plain urlopen routes through the proxy;
        # an explicit ProxyHandler would try to speak HTTP to the SOCKS port.
        with _socks_patch(proxy):
            return urllib.request.urlopen(url, timeout=timeout)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return opener.open(url, timeout=timeout)


def _aria2_proxy_args(proxy):
    """Translate a proxy URL into aria2c command-line arguments.

    aria2 only speaks HTTP proxies; a SOCKS proxy makes the caller fall back
    to the urllib downloader, so SOCKS URLs never reach this function.

    Args:
        proxy (str | None): Proxy URL, or None for a direct connection.

    Returns:
        list[str]: aria2c arguments (empty when no proxy is configured).

    Raises:
        ProxyError: If the proxy is malformed or uses an unsupported scheme.
    """
    proxy = _normalize_proxy(proxy)
    if proxy is None:
        return []
    parsed = urllib.parse.urlparse(proxy)
    scheme = (parsed.scheme or "").lower()
    if scheme in SOCKS_PROXY_SCHEMES:
        raise ProxyError("aria2c does not support SOCKS proxies; the urllib downloader handles them")
    host = parsed.hostname
    port = f":{parsed.port}" if parsed.port else ""
    args: list[str] = []
    # Credentials ride as dedicated flags so they never appear in a URL copy.
    # Note aria2 spells the password flag --all-proxy-passwd.
    if parsed.username:
        args.append(f"--all-proxy-user={parsed.username}")
        if parsed.password:
            args.append(f"--all-proxy-passwd={parsed.password}")
    args.append(f"--all-proxy={scheme}://{host}{port}")
    return args


def _quote_path(path):
    """Implement the quote path helper.

    Args:
        path (str | os.PathLike): File system path.

    Returns:
        Any: Computed result."""
    return urllib.parse.quote(path, safe="/")


def remote_url(relpath, source="modelscope", endpoint=None):
    """Build the remote download URL for a catalog-relative file path.

    Args:
        relpath (str): Relpath value.
        source (str, optional): Download source name. Defaults to "modelscope".
        endpoint (str | None, optional): Optional custom download endpoint. Defaults to None.

    Returns:
        str: Absolute URL for the requested source."""
    if endpoint:
        return f"{endpoint.rstrip('/')}/{_quote_path(relpath)}"
    if source == "huggingface":
        return f"{HF_BASE_URL}/{_quote_path(relpath)}"
    if source == "hf-mirror":
        return f"https://hf-mirror.com/{HF_REPO}/resolve/main/{_quote_path(relpath)}"
    if source == "modelscope":
        return f"{MS_BASE_URL}/{_quote_path(relpath)}"
    raise ValueError("source must be one of: modelscope, huggingface, hf-mirror")


def _read_json_url(url, timeout=30, proxy=None):
    """Read json url.

    Args:
        url (str): Url value.
        timeout (int, optional): Network timeout in seconds. Defaults to 30.
        proxy (str | None, optional): Proxy URL. Defaults to None (environment).

    Returns:
        Any: Computed result."""
    with _urlopen_with_proxy(url, proxy=proxy, timeout=timeout) as response:
        return json.load(response)


def fetch_modelscope_file_index(timeout=30, proxy=None):
    """Fetch modelscope file index.

    Args:
        timeout (int, optional): Network timeout in seconds. Defaults to 30.
        proxy (str | None, optional): Proxy URL. Defaults to None (environment).

    Returns:
        Any: Computed result."""
    data = _read_json_url(MS_FILES_API, timeout=timeout, proxy=proxy)
    files = data.get("Data", {}).get("Files", [])
    return {item["Path"]: item for item in files if item.get("Type") == "blob"}


def _sha256(path):
    """Implement the sha256 helper.

    Args:
        path (str | os.PathLike): File system path.

    Returns:
        Any: Computed result."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _expected_size_and_hash(relpath, source_index):
    """Implement the expected size and hash helper.

    Args:
        relpath (str): Relpath value.
        source_index (Any): Source index value.

    Returns:
        Any: Computed result."""
    if not source_index:
        return None, ""
    item = source_index.get(relpath, {})
    size = item.get("Size")
    sha256 = item.get("Sha256") or ""
    return int(size) if size else None, sha256


def _already_valid(path, expected_size=None, expected_sha256=""):
    """Implement the already valid helper.

    Args:
        path (str | os.PathLike): File system path.
        expected_size (Any, optional): Expected size value. Defaults to None.
        expected_sha256 (Any, optional): Expected sha256 value. Defaults to ''.

    Returns:
        Any: Computed result."""
    if not path.is_file():
        return False
    if expected_size is not None and path.stat().st_size != expected_size:
        return False
    if expected_sha256 and _sha256(path) != expected_sha256:
        return False
    return True


def _cleanup_partial_download(tmp):
    """Implement the cleanup partial download helper.

    Args:
        tmp (Any): Tmp value.

    Returns:
        None: This callable completes for its side effects."""
    for path in (tmp, Path(str(tmp) + ".aria2")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _validate_downloaded_file(path, dest, expected_size=None, expected_sha256=""):
    """Validate downloaded file.

    Args:
        path (str | os.PathLike): File system path.
        dest (Any): Dest value.
        expected_size (Any, optional): Expected size value. Defaults to None.
        expected_sha256 (Any, optional): Expected sha256 value. Defaults to ''.

    Returns:
        None: This callable completes for its side effects."""
    if expected_size is not None and path.stat().st_size != expected_size:
        raise DownloadValidationError(f"size mismatch for {dest.name}: expected {expected_size}, got {path.stat().st_size}")
    if expected_sha256:
        actual = _sha256(path)
        if actual != expected_sha256:
            raise DownloadValidationError(f"sha256 mismatch for {dest.name}: expected {expected_sha256}, got {actual}")


def _parse_human_bytes(value):
    """Convert aria2 byte strings such as 120MiB or 1.5GB to bytes."""
    match = re.fullmatch(r"\s*([\d.]+)\s*([KMGTPE]?i?B)\s*", str(value or ""), re.IGNORECASE)
    if not match:
        return 0
    try:
        amount = float(match.group(1))
    except ValueError:
        return 0
    return int(amount * BYTE_UNITS.get(match.group(2).upper(), 1))


def _emit_download_progress(progress_callback, done, total, message):
    """Report progress to the caller, without letting it break the download.

    This runs inside the retry loop in ``_download_file``, which treats OSError as a failed
    transfer. A callback that raises one while writing a log or a closed pipe would be
    indistinguishable from a dead connection, and the whole file would be fetched again.

    Args:
        progress_callback (callable | None): Caller's progress callback.
        done (int): Bytes transferred so far.
        total (int): Expected total bytes, or 0 when unknown.
        message (str): Human readable description of the current step.

    Returns:
        None: This callable completes for its side effects."""
    if not progress_callback:
        return
    try:
        progress_callback(int(done), int(total or 0), message)
    except Exception:
        pass


def _parse_aria2_progress(line):
    """Parse one aria2 progress summary line.

    Args:
        line (str): A single line of aria2c console output.

    Returns:
        tuple[int, int, str] | None: Downloaded bytes, total bytes, and the
        transfer rate as aria2 rendered it, or None when the line carries no
        usable progress."""
    match = ARIA2_PROGRESS_PATTERN.search(line or "")
    if not match:
        return None
    total = _parse_human_bytes(match.group("total"))
    done = _parse_human_bytes(match.group("downloaded"))
    return min(done, total) if total else done, total, match.group("speed").strip()


def _download_file_urllib(url, tmp, dest, expected_size=None, expected_sha256="", timeout=30, progress_callback=None, proxy=None):
    """Download file urllib.

    Args:
        url (str): Url value.
        tmp (Any): Tmp value.
        dest (Any): Dest value.
        expected_size (Any, optional): Expected size value. Defaults to None.
        expected_sha256 (Any, optional): Expected sha256 value. Defaults to ''.
        timeout (int, optional): Network timeout in seconds. Defaults to 30.
        proxy (str | None, optional): Proxy URL. Defaults to None (environment).

    Returns:
        Any: Computed result."""
    with _urlopen_with_proxy(url, proxy=proxy, timeout=timeout) as response:
        total = int(response.headers.get("content-length") or expected_size or 0)
        done = 0
        _emit_download_progress(progress_callback, done, total, f"Downloading {dest.name}")
        with open(tmp, "wb") as f, tqdm(total=total, unit="B", unit_scale=True, desc=dest.name) as progress:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                progress.update(len(chunk))
                _emit_download_progress(progress_callback, done, total, f"Downloading {dest.name}")
    _validate_downloaded_file(tmp, dest, expected_size, expected_sha256)
    os.replace(tmp, dest)
    downloaded_size = dest.stat().st_size
    _emit_download_progress(progress_callback, downloaded_size, expected_size or downloaded_size, f"Downloaded {dest.name}")
    return dest


def _download_file_aria2(url, tmp, dest, expected_size=None, expected_sha256="", timeout=30, progress_callback=None, proxy=None):
    """Download file aria2.

    Args:
        url (str): Url value.
        tmp (Any): Tmp value.
        dest (Any): Dest value.
        expected_size (Any, optional): Expected size value. Defaults to None.
        expected_sha256 (Any, optional): Expected sha256 value. Defaults to ''.
        timeout (int, optional): Network timeout in seconds. Defaults to 30.
        proxy (str | None, optional): Proxy URL. Defaults to None (environment).

    Returns:
        Any: Computed result."""
    cmd = [
        ARIA2C_PATH,
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--continue=true",
        "--console-log-level=warn",
        "--summary-interval=1",
        "--show-console-readout=true",
        "--quiet=false",
        "--max-connection-per-server=16",
        "--split=16",
        "--min-split-size=1M",
        "--max-tries=3",
        f"--connect-timeout={timeout}",
        f"--timeout={timeout}",
        *_aria2_proxy_args(proxy),
        "--dir",
        str(tmp.parent),
        "--out",
        tmp.name,
        url,
    ]
    _emit_download_progress(progress_callback, 0, expected_size or 0, f"Downloading {dest.name}")
    output_tail = []
    # aria2 writes its progress summary to stdout. Left alone it would land on the caller's
    # stdout, which a library has no business writing to; capturing it also turns the summary
    # into the progress numbers reported below. tqdm renders to stderr, matching the urllib
    # branch so command line users still see a bar either way.
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    ) as process, tqdm(total=expected_size or 0, unit="B", unit_scale=True, desc=dest.name) as bar:
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    text = line.strip()
                    if text:
                        output_tail.append(text)
                        del output_tail[:-20]
                    parsed = _parse_aria2_progress(line)
                    if parsed is None:
                        continue
                    done, total, speed = parsed
                    # Some servers omit Content-Length; keep their byte updates
                    # and use catalog metadata when the response has no total.
                    total = total or expected_size or 0
                    if total and bar.total != total:
                        bar.total = total
                    bar.update(max(0, done - bar.n))
                    _emit_download_progress(progress_callback, done, total, f"Downloading {dest.name} ({speed}/s)")
            returncode = process.wait()
        except BaseException:
            # subprocess.run killed the child on any exception; Popen's context manager does
            # not, and aria2 does not stop when its stdout closes. Without this it survives a
            # Ctrl+C as an orphan still writing to the file, and any other error would leave
            # __exit__ blocked in wait() until the whole download finished.
            process.kill()
            raise
    if returncode != 0:
        detail = "\n".join(output_tail[-5:])
        if detail:
            detail = f": {detail}"
        raise DownloadError(f"aria2c failed with exit code {returncode}{detail}")
    if not tmp.is_file():
        raise DownloadError("aria2c did not create the expected output file")

    _validate_downloaded_file(tmp, dest, expected_size, expected_sha256)
    os.replace(tmp, dest)
    downloaded_size = dest.stat().st_size
    _emit_download_progress(progress_callback, downloaded_size, expected_size or downloaded_size, f"Downloaded {dest.name}")
    return dest


def _download_file(url, dest, expected_size=None, expected_sha256="", timeout=30, retries=2, progress_callback=None, proxy=None):
    """Download file.

    Args:
        url (str): Url value.
        dest (Any): Dest value.
        expected_size (Any, optional): Expected size value. Defaults to None.
        expected_sha256 (Any, optional): Expected sha256 value. Defaults to ''.
        timeout (int, optional): Network timeout in seconds. Defaults to 30.
        retries (int, optional): Retries value. Defaults to 2.
        proxy (str | None, optional): Proxy URL. Defaults to None (environment).

    Returns:
        Any: Computed result."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    last_error = None

    for attempt in range(retries + 1):
        try:
            # aria2 cannot route through SOCKS proxies; a SOCKS proxy forces the
            # urllib downloader (which needs PySocks for the connection).
            effective_proxy = _normalize_proxy(proxy)
            use_aria2 = ARIA2C_PATH and not (
                effective_proxy and urllib.parse.urlparse(effective_proxy).scheme.lower() in SOCKS_PROXY_SCHEMES
            )
            if use_aria2:
                return _download_file_aria2(
                    url, tmp, dest, expected_size, expected_sha256, timeout=timeout, progress_callback=progress_callback, proxy=proxy
                )
            return _download_file_urllib(
                url, tmp, dest, expected_size, expected_sha256, timeout=timeout, progress_callback=progress_callback, proxy=proxy
            )
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, DownloadError) as exc:
            last_error = exc
            # don't rm temp file if aria2c is being used
            if not use_aria2 or isinstance(exc, DownloadValidationError):
                _cleanup_partial_download(tmp)
            if attempt < retries:
                time.sleep(1.0 + attempt)
    raise DownloadError(f"failed to download {url}: {last_error}")


def files_for_model(model_name, model_dir=None):
    """Return the local file targets required by a catalog model.

    Args:
        model_name (str): Model name or alias from the pymss catalog.
        model_dir (str | os.PathLike | None, optional): Local model cache directory. Uses the package default when None. Defaults to None.

    Returns:
        tuple[ModelEntry, list[tuple[str, Path]]]: Catalog entry and required files."""
    from .user_models import get_user_model_entry

    try:
        get_user_model_entry(model_name)
    except KeyError:
        pass
    else:
        raise ValueError(f"User-registered model {model_name!r} is local-only and cannot be downloaded")

    entry = get_model_entry(model_name)
    if getattr(entry, "source", None) == "user":
        raise ValueError(f"User-registered model {model_name!r} is local-only and cannot be downloaded")
    files = [(entry.relpath, model_path_for(entry, model_dir))]
    config_path = config_path_for(entry, model_dir)
    if entry.config_relpath and config_path is not None:
        files.append((entry.config_relpath, config_path))
    files.extend(zip(entry.auxiliary_relpaths, auxiliary_paths_for(entry, model_dir)))
    return entry, files


def download_model(model_name, model_dir=None, source="modelscope", endpoint=None, verify=True, force=False, timeout=30, progress_callback=None, proxy=None):
    """Download all files required by one catalog model.

    The downloader resolves the model from the pymss catalog, downloads the
    weights, config, and auxiliary files, and skips files that already match
    available size/hash metadata. If ``aria2c`` is available on ``PATH``, pymss
    uses it for resumable multi-connection downloads; otherwise it falls back
    to Python's urllib downloader.

    Args:
        model_name (str): Model name, stem, or alias from the pymss catalog.
        model_dir (str | os.PathLike | None, optional): Local model cache
            directory. When omitted, pymss uses its default model directory.
            Defaults to None.
        source (str, optional): Download source. Supported values are
            ``modelscope``, ``huggingface``, and ``hf-mirror``. Defaults to
            ``"modelscope"``.
        endpoint (str | None, optional): Custom endpoint prefix. When provided,
            the final URL is ``endpoint/relative/catalog/path`` and ``source``
            is ignored for URL construction. Defaults to None.
        verify (bool, optional): Whether to validate downloads with available
            ModelScope size/hash metadata. Custom endpoints skip source index
            lookup. Defaults to True.
        force (bool, optional): Whether to redownload files even when existing
            local files appear valid. Defaults to False.
        timeout (int, optional): Network timeout in seconds. Defaults to 30.
        progress_callback (callable | None, optional): Optional callback called
            as ``callback(done, total, message)`` during each file download.
            Exceptions it raises are ignored so that reporting cannot fail the
            download. Defaults to None.
        proxy (str | None, optional): Proxy URL (``http://``, ``https://``,
            ``socks5://``, ``socks5h://``, ``socks4://``). None consults the
            standard ``HTTPS_PROXY``/``HTTP_PROXY`` environment variables; an
            empty string forces a direct connection. SOCKS proxies require the
            ``pymss[proxy]`` extra (PySocks). Defaults to None.

    Returns:
        dict: Download result with ``entry`` (``ModelEntry``), ``downloaded``
        (list of paths written this call), and ``skipped`` (list of existing
        valid paths).

    Raises:
        KeyError: If ``model_name`` is unknown.
        DownloadError: If downloading fails after retries.
        DownloadValidationError: If a downloaded file fails size/hash checks.

    Example:
        >>> from pymss import download_model
        >>> result = download_model("bs_roformer_voc_hyperacev2", model_dir="models")
        >>> result["entry"].name

    Example:
        >>> download_model(
        ...     "bs_roformer_voc_hyperacev2",
        ...     source="hf-mirror",
        ...     force=True,
        ...     timeout=60,
        ... )"""
    entry, files = files_for_model(model_name, model_dir)
    index = fetch_modelscope_file_index(timeout=timeout, proxy=proxy) if verify and endpoint is None else None
    downloaded = []
    skipped = []

    for relpath, dest in files:
        expected_size, expected_sha256 = _expected_size_and_hash(relpath, index)
        if not force and _already_valid(dest, expected_size, expected_sha256):
            skipped.append(str(dest))
            continue
        url = remote_url(relpath, source=source, endpoint=endpoint)
        _download_file(url, dest, expected_size, expected_sha256, timeout=timeout, progress_callback=progress_callback, proxy=proxy)
        downloaded.append(str(dest))

    return {"entry": entry, "downloaded": downloaded, "skipped": skipped}


def download_all(model_dir=None, source="modelscope", endpoint=None, supported_only=False, force=False, timeout=30, progress_callback=None, proxy=None):
    """Download every catalog model, optionally limited to supported entries.

    Args:
        model_dir (str | os.PathLike | None, optional): Local model cache directory. Uses the package default when None. Defaults to None.
        source (str, optional): Download source name. Defaults to "modelscope".
        endpoint (str | None, optional): Optional custom download endpoint. Defaults to None.
        supported_only (bool, optional): Supported only value. Defaults to False.
        force (bool, optional): Whether to overwrite or redownload existing files. Defaults to False.
        timeout (int, optional): Network timeout in seconds. Defaults to 30.
        progress_callback (callable | None, optional): Optional download progress callback. Defaults to None.
        proxy (str | None, optional): Proxy URL. Defaults to None (environment).

    Returns:
        list[dict]: Per-model download results."""
    from .model_registry import list_models

    results = []
    for entry in list_models(supported=True if supported_only else None):
        try:
            results.append(
                download_model(
                    entry.name,
                    model_dir=model_dir,
                    source=source,
                    endpoint=endpoint,
                    force=force,
                    timeout=timeout,
                    progress_callback=progress_callback,
                    proxy=proxy,
                )
            )
        except Exception as exc:
            results.append({"entry": entry, "error": str(exc)})
    return results
