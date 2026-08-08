"""Graded live API check. Free calls first, billing calls only on request.

Diagnoses a failing key without spending anything by default. A 403 on one
endpoint says very little on its own -- it could be the key, the scope, the
account, or a feature flag -- so this walks a ladder of increasingly privileged
calls and prints the exact status, error code and message for each. The first
rung that fails localises the problem.

    /c/Python314/python tools/apicheck.py              # free calls only
    /c/Python314/python tools/apicheck.py --spend      # adds billing calls

Reads COASTY_API_KEY from the environment. Never hardcode a key here -- this
file is committed, and a key in a repo is a key that leaks.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
import zlib

BASE = os.environ.get("COASTY_BASE_URL", "https://coasty.ai/v1").rstrip("/")

# Coasty sits behind Cloudflare bot protection, which rejects urllib's default
# `Python-urllib/3.x` signature with a 403 and a Cloudflare error 1010 body --
# BEFORE the request reaches Coasty at all. That 403 is indistinguishable at a
# glance from an auth or scope failure, and sends you hunting through key
# settings for a problem that is entirely in the transport.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Headers Coasty documents as carrying the useful diagnostic signal.
ECHO = [
    "X-Coasty-Request-Id",
    "X-Coasty-Key-Kind",
    "X-Coasty-Test-Mode",
    "X-Credits-Charged",
    "X-Credits-Remaining",
]


def call(method: str, path: str, body=None, *, key: str, timeout: float = 45.0) -> dict:
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "X-API-Key": key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"status": r.status, "body": _parse(r.read()), "headers": dict(r.headers)}
    except urllib.error.HTTPError as e:
        with e:
            return {"status": e.code, "body": _parse(e.read()), "headers": dict(e.headers)}
    except urllib.error.URLError as e:
        return {"status": None, "body": {"_transport": str(e.reason)}, "headers": {}}


def _parse(raw: bytes):
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {"_raw": text[:500]}


def test_png(width: int = 1280, height: int = 720) -> str:
    """A valid PNG of a realistic size, built here so the check needs no fixture.

    Deliberately NOT 1x1. The documented screen bounds are 320-3840 by 240-2160,
    and dimensions are measured from the image when not supplied -- so a tiny
    placeholder fails validation for a reason that has nothing to do with the
    thing being tested.
    """
    row = b"\x00" + b"\x20\x24\x2c" * width  # filter byte + a dark grey row
    raw = row * height
    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            len(data).to_bytes(4, "big")
            + tag
            + data
            + (zlib.crc32(tag + data) & 0xFFFFFFFF).to_bytes(4, "big")
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode()


def show(label: str, result: dict, *, note: str = "") -> bool:
    status = result["status"]
    ok = status is not None and status < 400
    mark = "ok  " if ok else "FAIL"
    print(f"  {mark} {label:<34} {status}")

    err = (result["body"] or {}).get("error") if isinstance(result["body"], dict) else None
    if err:
        print(f"       code    {err.get('code')}")
        print(f"       message {err.get('message')}")
        # `details` names the offending field on a 422. Without it the message
        # is just "validation failed", which localises nothing.
        details = err.get("details")
        if details:
            print(f"       details {json.dumps(details)[:400]}")
        for extra in ("required_scope", "required_scopes", "scope", "allowed_from"):
            if err.get(extra):
                print(f"       {extra:<7} {err[extra]}")
    elif not ok:
        print(f"       body    {json.dumps(result['body'])[:300]}")

    for h in ECHO:
        for actual, value in result["headers"].items():
            if actual.lower() == h.lower():
                print(f"       {h:<22} {value}")
    if note:
        print(f"       {note}")
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spend", action="store_true", help="also run the billing calls")
    args = ap.parse_args(argv)

    key = (os.environ.get("COASTY_API_KEY") or "").strip()
    if not key:
        print("COASTY_API_KEY is not set.", file=sys.stderr)
        return 2

    kind = (
        "LIVE (bills)" if key.startswith("sk-coasty-live-")
        else "sandbox (free)" if key.startswith("sk-coasty-test-")
        else "unrecognised prefix"
    )
    print(f"\n  base   {BASE}")
    print(f"  key    {key[:18]}...{key[-4:]}  [{kind}]\n")

    print("  --- free calls -------------------------------------------------")
    show("GET  /health", call("GET", "/health", key=key))
    models = call("GET", "/models", key=key)
    show("GET  /models", models)
    if models["status"] == 200 and isinstance(models["body"], dict):
        vocab = json.dumps(models["body"])[:220]
        print(f"       {vocab}")
    show("GET  /usage", call("GET", "/usage", key=key))
    show("GET  /sessions  (list)", call("GET", "/sessions", key=key))
    show("GET  /runs      (list)", call("GET", "/runs?limit=1", key=key))

    if not args.spend:
        print("\n  --- billing calls skipped --------------------------------------")
        print("  Re-run with --spend to test /predict (5 credits) and")
        print("  /sessions (10 credits). Roughly $0.15 total on a live key.\n")
        return 0

    print("\n  --- billing calls ----------------------------------------------")

    # Stateless predict first: it is the cheaper of the two and needs a
    # different scope, so it separates "no inference access at all" from
    # "sessions specifically are unavailable".
    predict = call(
        "POST",
        "/predict",
        {
            "screenshot": test_png(),
            "instruction": "Reply with a single done action. This is a connectivity check.",
            "max_actions": 1,
        },
        key=key,
    )
    show("POST /predict", predict, note="scope: predict")
    if predict["status"] == 200:
        b = predict["body"]
        print(f"       status  {b.get('status')}")
        print(f"       actions {json.dumps(b.get('actions'))[:220]}")
        print(f"       space   {b.get('screen_width')}x{b.get('screen_height')}")
        print(f"       usage   {json.dumps(b.get('usage'))[:220]}")

    session = call("POST", "/sessions", {"cua_version": "v5"}, key=key)
    show("POST /sessions", session, note="scope: session")
    if session["status"] == 200:
        sid = session["body"].get("session_id")
        print(f"       session_id {sid}")
        if sid:
            sp = call(
                "POST",
                f"/sessions/{sid}/predict",
                {
                    "screenshot": test_png(),
                    "instruction": "Reply with a single done action. Connectivity check.",
                },
                key=key,
            )
            show("POST /sessions/{id}/predict", sp)
            if sp["status"] == 200:
                print(f"       status  {sp['body'].get('status')}")
                print(f"       actions {json.dumps(sp['body'].get('actions'))[:220]}")
            show("DEL  /sessions/{id}", call("DELETE", f"/sessions/{sid}", key=key))

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
