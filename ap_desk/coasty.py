"""Coasty API client. Standard library only.

Structured around the safety property the reference catalog gets right and
which is worth keeping verbatim: **production is never a default.** An unset
COASTY_BASE_URL resolves to the bundled offline mock, and reaching a billable
host requires two separate, deliberate consents:

    destination   COASTY_ALLOW_LIVE=1        guards a forgotten base URL
    cost          --confirm-cost-cents N     guards an unbounded run

Conflating those is how a single forgotten environment variable bills a real
account. They are separate decisions and stay separate here.

Which layer this uses, and why
------------------------------
Coasty exposes three layers. The catalog's eleven repos all use the highest
one, `POST /v1/tasks`, which provisions a cloud VM and drives the browser there.
That is the right choice when you want submit-and-forget, but it means the run
happens somewhere you cannot watch and the target must be reachable from the
public internet.

This client is built for the *lowest* layer instead -- `/v1/sessions` and
`/v1/sessions/{id}/predict`, which Coasty's own docs describe as "own the
screenshot and action loop". We send a screenshot and get back the next action;
the loop, the machine and the screen stay local. That means the agent drives the
operator's real desktop against a target on localhost, with nothing exposed.

`create_task` and the run/frame readers are kept too, so the cloud path remains
available without a rewrite.
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator, Mapping

MOCK_BASE_URL = "http://127.0.0.1:4010/v1"
LIVE_BASE_URL = "https://coasty.ai/v1"

# Coasty sits behind Cloudflare bot protection, which rejects urllib's default
# `Python-urllib/3.x` signature with a 403 carrying a Cloudflare error 1010 body
# -- BEFORE the request reaches Coasty at all. Even `/health`, which the spec
# marks public and unauthenticated, is refused.
#
# That 403 is indistinguishable at a glance from INSUFFICIENT_SCOPE, so it sends
# you auditing key permissions for a problem that lives entirely in the
# transport. Sending a browser-shaped User-Agent is the whole fix.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

TERMINAL = frozenset({"succeeded", "failed", "cancelled", "timed_out"})
_RETRYABLE = frozenset({408, 429, 500, 502, 503, 504})


class CoastyError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        retryable: bool = False,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id
        self.retryable = retryable
        self.body = body

    def advice(self) -> str:
        """What to actually DO about this error.

        A bare "HTTP 403" sends you auditing API key scopes for what turned out
        to be a Cloudflare bot rule, and a bare "validation failed" localises
        nothing at all. Every failure mode that cost real debugging time gets a
        line here, because the next person to hit it should not have to repeat
        the diagnosis.
        """
        raw = json.dumps(self.body) if self.body else ""

        if "error-1010" in raw or "Access denied" in raw:
            return (
                "Cloudflare blocked the request before it reached Coasty (error 1010).\n"
                "  This is NOT an auth or scope problem. A browser-shaped User-Agent\n"
                "  is required; this client sends one, so a proxy is probably\n"
                "  rewriting it."
            )

        details = ""
        if isinstance(self.body, dict):
            err = self.body.get("error") or {}
            if err.get("details"):
                details = f"\n  offending field(s): {json.dumps(err['details'])[:300]}"

        return {
            "VALIDATION_ERROR": (
                "The request body was rejected." + details + "\n"
                "  Session predict accepts ONLY screenshot, instruction,\n"
                "  include_reasoning and include_raw_code. Screen size and\n"
                "  trajectory belong on session create, not on each turn.\n"
                "  Screenshots must be 320-3840 by 240-2160."
            ),
            "INSUFFICIENT_SCOPE": (
                "The key authenticated but lacks a scope this endpoint needs.\n"
                "  /v1/predict needs `predict`; /v1/sessions needs `session`.\n"
                "  Re-mint the key at https://coasty.ai/developers/keys."
            ),
            "INVALID_API_KEY": (
                "The key is wrong, revoked or malformed.\n"
                "  Check COASTY_API_KEY, then https://coasty.ai/developers/keys."
            ),
            "INSUFFICIENT_CREDITS": (
                "The wallet cannot cover this call. Top up, or use a\n"
                "  sk-coasty-test- sandbox key, which never bills."
            ),
            "RATE_LIMITED": "Rate limited. The client already backs off; try again shortly.",
            "LIVE_NOT_ALLOWED": (
                "Refusing to reach a billable host without explicit consent.\n"
                "  Set COASTY_ALLOW_LIVE=1, or unset COASTY_BASE_URL to stay offline."
            ),
            "NO_API_KEY": "Set COASTY_API_KEY, or unset COASTY_BASE_URL to use the offline mock.",
            "UNREACHABLE": "Could not reach the host at all. Check the network and COASTY_BASE_URL.",
        }.get(self.code or "", details.strip() or "")

    def render(self) -> str:
        """The full operator-facing block: what failed, why, and what to do."""
        lines = [f"{self.code or 'ERROR'}: {self}"]
        if self.request_id:
            lines.append(f"  request_id: {self.request_id}")
        advice = self.advice()
        if advice:
            lines.append(f"  {advice}")
        return "\n".join(lines)


def is_loopback(url: str) -> bool:
    """True for a URL pointing at this machine. Fails CLOSED: unparseable => remote."""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return False
    return host.strip("[]") in ("127.0.0.1", "localhost", "::1")


def resolve_target(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Decide where requests go, and refuse to guess.

    Destination consent is gated on the HOST, not on the key. Gating it on the
    key kind lets a remote base URL with no key sail through to a third-party
    host -- and a client that then invents a placeholder credential to send it.
    Any host that is not the bundled loopback mock is an egress decision the
    operator has to make explicitly.
    """
    env = os.environ if env is None else env
    base = (env.get("COASTY_BASE_URL") or "").strip().rstrip("/") or MOCK_BASE_URL
    key = (env.get("COASTY_API_KEY") or "").strip()

    if key.startswith("sk-coasty-live-"):
        kind = "live"
    elif key.startswith("sk-coasty-test-") or key.startswith("cua_sk_"):
        kind = "sandbox"
    elif key:
        kind = "live"  # unknown prefix: assume the expensive interpretation
    else:
        kind = "none"

    remote = not is_loopback(base)
    if remote and env.get("COASTY_ALLOW_LIVE") != "1":
        raise CoastyError(
            f"Refusing to call {base}. That is not the bundled offline mock.\n"
            "Set COASTY_ALLOW_LIVE=1 to allow it, or unset COASTY_BASE_URL to stay offline.",
            code="LIVE_NOT_ALLOWED",
        )
    return {"base_url": base, "is_live": remote and kind == "live", "key_kind": kind}


class CoastyClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
        max_attempts: int = 4,
        env: Mapping[str, str] | None = None,
        on_log=None,
    ) -> None:
        env = os.environ if env is None else env
        resolved = None if base_url else resolve_target(env)
        self.base_url = (base_url or resolved["base_url"]).rstrip("/")

        # The bundled mock ignores the key, so a fresh clone needs no account.
        # A REMOTE host never gets a placeholder: fabricating a key and shipping
        # it to a third-party host is worse than failing loudly.
        supplied = api_key if api_key is not None else (env.get("COASTY_API_KEY") or "").strip()
        self.api_key = supplied or ("sk-coasty-test-offline" if is_loopback(self.base_url) else "")
        if not self.api_key:
            raise CoastyError(
                f"No COASTY_API_KEY set for {self.base_url}.\n"
                "Set one, or unset COASTY_BASE_URL to use the offline mock.",
                code="NO_API_KEY",
            )
        self.is_live = (not is_loopback(self.base_url)) if base_url else resolved["is_live"]
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.on_log = on_log or (lambda _: None)

    # ------------------------------------------------------------------ #

    def _attempt(self, method: str, path: str, *, body=None, idempotency_key=None):
        req = urllib.request.Request(
            self.base_url + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "X-API-Key": self.api_key,
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
                **({"Content-Type": "application/json"} if body is not None else {}),
                **({"Idempotency-Key": idempotency_key} if idempotency_key else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status, text = resp.status, resp.read().decode("utf-8", "replace")
                request_id = resp.headers.get("X-Coasty-Request-Id")
        except urllib.error.HTTPError as exc:
            status = exc.code
            text = exc.read().decode("utf-8", "replace")
            request_id = exc.headers.get("X-Coasty-Request-Id")
        except urllib.error.URLError as exc:
            raise CoastyError(
                f"{method} {path} - network failure: {exc.reason}",
                code="UNREACHABLE",
                retryable=True,
            ) from exc

        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError:
            parsed = {"raw": text[:400]}

        if status >= 400:
            err = parsed.get("error", {}) if isinstance(parsed, dict) else {}
            raise CoastyError(
                err.get("message") or f"{method} {path} -> HTTP {status}",
                status=status,
                code=err.get("code") or f"HTTP_{status}",
                request_id=err.get("request_id") or request_id,
                retryable=status in _RETRYABLE,
                body=parsed,
            )
        return parsed, request_id

    def request(self, method: str, path: str, **kw) -> Any:
        """Request with the retry policy.

        A POST is retried ONLY when the caller supplied an Idempotency-Key.
        An unkeyed retry can provision a second machine and bill twice.
        """
        safe = method in ("GET", "DELETE") or bool(kw.get("idempotency_key"))
        attempts = self.max_attempts if safe else 1
        last: CoastyError | None = None

        for attempt in range(1, attempts + 1):
            try:
                body, request_id = self._attempt(method, path, **kw)
                self.on_log({"evt": "http", "method": method, "path": path, "ok": True,
                             "attempt": attempt, "request_id": request_id})
                return body
            except CoastyError as err:
                last = err
                self.on_log({"evt": "http", "method": method, "path": path, "ok": False,
                             "attempt": attempt, "code": err.code, "status": err.status,
                             "request_id": err.request_id})
                if not err.retryable or attempt == attempts:
                    break
                retry_after = float((err.body or {}).get("error", {}).get("retry_after", 0) or 0)
                backoff = min(8.0, 0.5 * 2 ** (attempt - 1))
                time.sleep(max(retry_after, random.random() * backoff))
        raise last  # type: ignore[misc]

    # ---- primitives: own the screenshot and action loop ---------------- #

    def create_session(self, **kw) -> Any:
        """POST /v1/sessions -- a stateful loop we drive ourselves. 10 credits."""
        return self.request("POST", "/sessions", body=kw or {})

    def session_predict(
        self,
        session_id: str,
        *,
        screenshot: str,
        instruction: str,
        include_reasoning: bool = True,
        include_raw_code: bool = False,
    ) -> Any:
        """POST /v1/sessions/{id}/predict -- screenshot in, next actions out.

        Takes EXACTLY the four documented per-turn fields and no more. The
        session already holds the screen size, trajectory and policy from
        create; re-sending any of them is rejected with 422 VALIDATION_ERROR.
        Keeping the signature explicit rather than **kw means that mistake
        cannot be made by a caller.

        `include_raw_code` defaults off: it is a debug aid the guidance says
        never to evaluate, and it costs prompt space on every turn.
        """
        return self.request(
            "POST",
            f"/sessions/{urllib.parse.quote(session_id)}/predict",
            body={
                "screenshot": screenshot,
                "instruction": instruction,
                "include_reasoning": include_reasoning,
                "include_raw_code": include_raw_code,
            },
        )

    def predict(self, **kw) -> Any:
        """POST /v1/predict -- stateless variant; caller owns the trajectory.

        Unlike the session form this DOES accept screen_width/screen_height,
        trajectory and max_actions.
        """
        return self.request("POST", "/predict", body=kw)

    def delete_session(self, session_id: str) -> Any:
        return self.request("DELETE", f"/sessions/{urllib.parse.quote(session_id)}")

    # ---- managed path: Coasty owns the machine ------------------------- #

    def create_task(self, task: str, *, idempotency_key: str | None = None, **rest) -> Any:
        return self.request("POST", "/tasks", body={"task": task, **rest},
                            idempotency_key=idempotency_key)

    def get_run(self, run_id: str) -> Any:
        return self.request("GET", f"/runs/{urllib.parse.quote(run_id)}")

    def wait_for_run(self, run_id: str, *, interval: float = 2.0,
                     timeout: float = 900.0, on_tick=None) -> Any:
        deadline = time.monotonic() + timeout
        while True:
            run = self.get_run(run_id)
            if on_tick:
                on_tick(run)
            if run.get("status") in TERMINAL:
                return run
            if time.monotonic() > deadline:
                raise CoastyError(f"Run {run_id} did not finish within {timeout}s",
                                  code="WAIT_TIMEOUT")
            time.sleep(interval)

    def frames(self, run_id: str, *, include_image: bool = True) -> Iterator[dict]:
        """GET /v1/runs/{id}/screenshots -- the model-input frames, oldest first.

        Two documented traps, both handled here: `include_image=true` clamps a
        page to 10 frames so paging is mandatory even for short runs, and
        `index` is the only safe cursor -- `step` restarts on a retried attempt.
        """
        after = -1
        while True:
            q = {}
            if include_image:
                q["include_image"] = "true"
            if after >= 0:
                q["after_index"] = str(after)
            qs = ("?" + urllib.parse.urlencode(q)) if q else ""
            body = self.request("GET", f"/runs/{urllib.parse.quote(run_id)}/screenshots{qs}")
            page = body.get("data") or []
            if not page:
                return
            yield from page
            after = page[-1]["index"]
            if not body.get("has_more"):
                return
