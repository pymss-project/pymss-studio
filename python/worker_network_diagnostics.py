"""Small metadata requests using the application's existing proxy and source rules."""
from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from worker_bootstrap import PYPI_MIRROR_URLS, _resolve_pypi_mirror
from worker_connection import _test_url_for_source
from worker_protocol import _redact_sensitive_text, emit, emit_error
from worker_proxy import configure_process_proxy, load_proxy_config, proxy_urlopen

TIMEOUT = 15
READ_LIMIT = 1024
MODEL_SOURCES = {"modelscope", "huggingface", "hf-mirror"}


def _check(identifier: str, source: str, url: str, config) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {"id": identifier, "source": source, "host": urlsplit(url).hostname, "kind": "http"}
    try:
        with proxy_urlopen(url, TIMEOUT, config) as response:
            status = int(response.status)
            response.read(READ_LIMIT)
        result.update(status="ok" if 200 <= status < 300 else "failed", httpStatus=status)
    except HTTPError as error:
        # HTTPError is also a response object; release its body without reading it.
        result.update(status="failed", httpStatus=error.code, errorCode="HTTPError",
                      error=_redact_sensitive_text(str(error))[:600])
        error.close()
    except Exception as error:
        timeout = isinstance(error, TimeoutError) or isinstance(error, URLError) and isinstance(error.reason, TimeoutError)
        result.update(status="timeout" if timeout else "failed", errorCode=type(error).__name__, error=_redact_sensitive_text(str(error))[:600])
    result["elapsedMs"] = round((time.monotonic() - started) * 1000)
    return result


def cmd_network_diagnostics(payload: dict[str, Any]) -> int:
    try:
        source = payload.get("modelSource", "modelscope")
        mirror = payload.get("mirror", "auto")
        locale = payload.get("locale", "")
        if source not in MODEL_SOURCES or mirror not in {"auto", *PYPI_MIRROR_URLS} or not isinstance(locale, str):
            raise ValueError("Invalid network diagnostic source")
        config = load_proxy_config()
        configure_process_proxy(config)
        selected, index = _resolve_pypi_mirror(mirror, locale)
        assert index is not None
        checks = [_check("models", source, _test_url_for_source(source), config),
                  _check("pypi", selected, index.rstrip("/") + "/pymss/", config)]
        emit("network_diagnostics", {"checkedAt": datetime.now(timezone.utc).isoformat(),
            "proxyMode": config.mode, "checks": checks, "timeoutSeconds": TIMEOUT, "readLimitBytes": READ_LIMIT})
        return 0
    except Exception as error:
        return emit_error("NETWORK_DIAGNOSTICS_FAILED", _redact_sensitive_text(str(error)), recoverable=True)
