"""Thin Dolibarr REST client. Standard library only.

This is the ORACLE's transport, and nothing else. The agent never touches it:
the agent works from screenshots of the Dolibarr UI, exactly as a person would,
while this module reads the same underlying records over the API to compute
what the agent *should* have found. Two independent views of one dataset is the
whole basis of the test, so keeping them strictly separated matters more than
any convenience it would buy to blur them.

Dolibarr specifics worth knowing:

* Every route hangs off ``<root>/api/index.php``, not ``<root>/api``.
* Auth is the ``DOLAPIKEY`` header. Not a bearer token, not basic auth.
* Errors come back as ``{"error": {"code": N, "message": "..."}}`` alongside a
  matching HTTP status, but a few paths return 200 with an error body, so the
  body is inspected independently of the status.
* An empty collection is a 404 with "No ... found", not a 200 with ``[]``.
  That is a normal answer and must not be treated as a failure.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping


class DolibarrError(Exception):
    """Any non-success answer from Dolibarr, transport or application level."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: Any = None,
        path: str | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.path = path
        self.body = body


def api_base(root: str | None) -> str:
    """Normalise whatever the user set as the instance root into an API base.

    DoliWamp installs land at a variety of roots depending on the port and
    whether the install went into a subdirectory, and people paste all of them::

        http://localhost/dolibarr
        http://localhost/dolibarr/
        http://localhost:8080/dolibarr
        http://localhost/dolibarr/api/index.php    # already the API base

    All four have to resolve to the same place, because a confusing 404 here
    looks identical to "the REST module is switched off" and sends you hunting
    in entirely the wrong place.
    """
    trimmed = (root or "").strip().rstrip("/")
    if not trimmed:
        raise DolibarrError("No Dolibarr URL given. Set DOLI_URL.", code="NO_URL")
    if trimmed.lower().endswith("/api/index.php"):
        return trimmed
    return f"{trimmed}/api/index.php"


class DolibarrClient:
    """Read-oriented client over the Dolibarr REST API."""

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = 20.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        env = os.environ if env is None else env
        self.base = api_base(url if url is not None else env.get("DOLI_URL"))
        key = api_key if api_key is not None else env.get("DOLI_API_KEY", "")
        self.api_key = (key or "").strip()
        if not self.api_key:
            raise DolibarrError(
                "No Dolibarr API key. Set DOLI_API_KEY.\n"
                "Generate one at: Home > Users & Groups > (your user) > API key.",
                code="NO_API_KEY",
            )
        self.timeout = timeout

    # ------------------------------------------------------------------ #

    def raw(
        self,
        path: str,
        *,
        method: str = "GET",
        query: Mapping[str, str] | None = None,
        body: Any = None,
    ) -> tuple[int, Any, str]:
        """One request. Returns ``(status, parsed_body, url)``.

        Deliberately does NOT raise on an HTTP error status: the probe needs to
        *record* which endpoints are missing rather than abort on the first one
        a given Dolibarr version happens not to expose.
        """
        url = self.base + path
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        data = None
        headers = {"DOLAPIKEY": self.api_key, "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            text = exc.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            raise DolibarrError(
                f"{method} {path} - cannot reach Dolibarr: {exc.reason}",
                code="UNREACHABLE",
                path=path,
            ) from exc

        try:
            parsed = json.loads(text) if text else None
        except json.JSONDecodeError:
            # An HTML body here almost always means the URL resolved to the
            # Dolibarr web UI rather than the API, i.e. a wrong root or the
            # REST module switched off.
            parsed = {"_non_json": text[:400]}
        return status, parsed, url

    def request(self, path: str, **kwargs: Any) -> Any:
        """Request that raises on anything that is not a clean success."""
        status, body, url = self.raw(path, **kwargs)
        app_error = body.get("error") if isinstance(body, dict) else None

        if status >= 400 or app_error:
            code = app_error.get("code") if isinstance(app_error, dict) else status
            message = (
                app_error.get("message")
                if isinstance(app_error, dict)
                else f"HTTP {status}"
            )
            raise DolibarrError(
                f"{kwargs.get('method', 'GET')} {path} -> {message}",
                status=status,
                code=code,
                path=path,
                body=body,
            )

        if isinstance(body, dict) and "_non_json" in body:
            raise DolibarrError(
                f"{path} returned HTML, not JSON. That usually means DOLI_URL points at "
                f"the web UI rather than the install root, or the REST API module is "
                f"disabled.\n  tried: {url}",
                status=status,
                code="NOT_JSON",
                path=path,
            )
        return body

    def list_records(
        self,
        path: str,
        *,
        limit: int = 100,
        page: int = 0,
        sqlfilters: str | None = None,
    ) -> list[Any]:
        """List a collection, tolerating Dolibarr's "empty means 404" convention.

        Returns ``[]`` where the collection exists but holds nothing, so callers
        can distinguish empty from absent -- which the seed check depends on.
        """
        query: dict[str, str] = {"limit": str(limit), "page": str(page)}
        if sqlfilters:
            query["sqlfilters"] = sqlfilters
        try:
            body = self.request(path, query=query)
        except DolibarrError as exc:
            if exc.status == 404 or exc.code == 404:
                return []
            raise
        return body if isinstance(body, list) else []

    def status(self) -> Any:
        """Liveness and version. The cheapest call proving URL and key are both right."""
        return self.request("/status")
