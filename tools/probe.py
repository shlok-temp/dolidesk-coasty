"""Interrogate a live Dolibarr instance and write down what is actually there.

Why this exists as its own step, before a line of oracle code:

Dolibarr's REST surface drifts between versions -- collection names, whether
document lines come back on the list call or only on the single-record call,
and which modules expose routes at all. Writing an oracle against remembered or
documented endpoint names produces something that half-works: plausible data
for the endpoints that happen to be right, and silently empty results for the
ones that are not. That reads as "the agent found nothing" rather than "the
test is broken", which is expensive to debug later and free to prevent now.

So: ask the instance, record the answer, build against the recording.

    /c/Python314/python tools/probe.py

Reads DOLI_URL and DOLI_API_KEY from the environment. Writes a machine-readable
report next to the adapter and prints a human summary.

This tool is READ-ONLY. It never creates, modifies or deletes anything, and it
records field NAMES only -- never field values, which could be real data.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ap_desk.dolibarr import DolibarrClient, DolibarrError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "target" / "dolibarr" / "probe-report.json"

# Collections to look for.
#
# `need` marks the ones the accounts-payable workflow cannot run without, so the
# summary can distinguish "this module is off, go switch it on" from "this route
# does not exist in your version, which is fine".
#
# `lines` marks documents whose line items the three-way match depends on. For
# those we fetch one full record, because Dolibarr commonly omits lines from
# list responses and returns them only on a single-record GET.
COLLECTIONS = [
    {"path": "/thirdparties", "label": "Third Parties", "need": True, "module": "Third Parties"},
    {"path": "/products", "label": "Products", "need": True, "module": "Products"},
    {"path": "/supplierorders", "label": "Purchase Orders", "need": True, "lines": True,
     "module": "Vendors"},
    {"path": "/supplierinvoices", "label": "Vendor Invoices", "need": True, "lines": True,
     "module": "Vendors + Invoices"},
    {"path": "/receptions", "label": "Receptions", "need": True, "lines": True,
     "module": "Receptions (needs Stocks)"},
    {"path": "/warehouses", "label": "Warehouses", "need": False, "module": "Stocks"},
    {"path": "/bankaccounts", "label": "Bank Accounts", "need": False, "module": "Banks & Cash"},
    {"path": "/users", "label": "Users", "need": False, "module": "Users & Groups"},
    {"path": "/invoices", "label": "Customer Invoices", "need": False, "module": "Invoices"},
    {"path": "/orders", "label": "Sales Orders", "need": False, "module": "Sales Orders"},
    {"path": "/tickets", "label": "Tickets", "need": False, "module": "Tickets"},
]

_EMPTY_HINT = re.compile(r"not found|no .{0,30}found", re.I)


def shape_of(obj) -> list[str] | None:
    """Keys are the useful part of a shape. Values may be real data -- never record them."""
    return sorted(obj.keys()) if isinstance(obj, dict) else None


def probe_collection(client: DolibarrClient, spec: dict) -> dict:
    out = {**spec, "ok": False, "count": None, "keys": None, "line_keys": None, "error": None}

    status, body, _ = client.raw(spec["path"], query={"limit": "3"})

    # Dolibarr answers an empty collection with 404 "No ... found". That means
    # the route EXISTS and the module is on -- it just holds no records yet.
    # Conflating that with a genuine 404 would send you to the modules page to
    # enable something that is already enabled.
    empty_but_present = status == 404 and bool(_EMPTY_HINT.search(json.dumps(body or "")))

    if status == 200 and isinstance(body, list):
        out["ok"] = True
        out["count"] = len(body)
        out["keys"] = shape_of(body[0]) if body else None
    elif empty_but_present:
        out["ok"] = True
        out["count"] = 0
        out["empty"] = True
    else:
        message = f"HTTP {status}"
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict) and err.get("message"):
                message = err["message"]
            elif body.get("_non_json"):
                message = str(body["_non_json"])[:160]
        out["error"] = {"status": status, "message": message}
        return out

    # Line items: fetch one full record, since list responses usually omit them.
    if spec.get("lines") and out["count"]:
        _, first, _ = client.raw(spec["path"], query={"limit": "1"})
        record_id = first[0].get("id") if isinstance(first, list) and first else None
        if record_id is not None:
            _, single, _ = client.raw(f"{spec['path']}/{record_id}")
            lines = single.get("lines") if isinstance(single, dict) else None
            out["line_keys"] = shape_of(lines[0]) if isinstance(lines, list) and lines else None
            out["lines_on_list_call"] = isinstance(
                first[0].get("lines") if isinstance(first[0], dict) else None, list
            )
            out["sample_id"] = record_id
    return out


def main() -> int:
    try:
        client = DolibarrClient()
    except DolibarrError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        print("Expected environment:", file=sys.stderr)
        print("  DOLI_URL      http://localhost/dolibarr", file=sys.stderr)
        print("  DOLI_API_KEY  <the key from your user card>\n", file=sys.stderr)
        return 2

    # Flushed: diagnostics below go to stderr, and an unflushed stdout buffer
    # makes them surface out of order, which reads as the tool reporting a
    # failure before it said what it was probing.
    print(f"probing {client.base}\n", flush=True)

    # /status is the cheapest call proving URL and key are BOTH right, and it
    # distinguishes the two failures, which otherwise look identical.
    try:
        status_body = client.status()
        success = status_body.get("success") if isinstance(status_body, dict) else None
        version = (success or status_body or {}).get("dolibarr_version", "unknown")
        print(f"  ok   reachable - Dolibarr {version}\n")
    except DolibarrError as exc:
        print(f"  FAIL {exc}\n", file=sys.stderr)
        if exc.code in ("NOT_JSON", "UNREACHABLE"):
            print("  Check DOLI_URL points at the install root (the folder that", file=sys.stderr)
            print("  contains htdocs), e.g. http://localhost/dolibarr\n", file=sys.stderr)
        elif exc.status == 401 or exc.code == 401:
            print("  The URL resolved but the key was rejected. Regenerate it at", file=sys.stderr)
            print("  Home > Users & Groups > (your user) > API key.\n", file=sys.stderr)
        return 3

    results = []
    for spec in COLLECTIONS:
        r = probe_collection(client, spec)
        results.append(r)
        mark = "ok  " if r["ok"] else "FAIL"
        if r["ok"]:
            detail = "empty" if r["count"] == 0 else f"{r['count']} record(s)"
            if r.get("line_keys"):
                detail += " - lines present"
        else:
            detail = r["error"]["message"]
        flag = "   <- REQUIRED" if not r["ok"] and r["need"] else ""
        print(f"  {mark} {r['label']:<18} {detail}{flag}")

    missing = [r for r in results if not r["ok"] and r["need"]]
    report = {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "base": client.base,
        "dolibarr_version": version,
        "collections": results,
        "missing_required": [{"label": m["label"], "module": m["module"]} for m in missing],
    }

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\n  report -> {REPORT.relative_to(ROOT)}")

    if missing:
        print("\n  Enable these modules (Home > Setup > Modules), then re-run:")
        for m in missing:
            print(f"    - {m['module']}   (for {m['label']})")
        return 1

    print("\n  All required collections present.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
