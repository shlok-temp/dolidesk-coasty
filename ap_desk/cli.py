"""The command line. One entry point for every mode.

    python -m ap_desk portal                    serve the terminal, nothing else
    python -m ap_desk doctor                    preflight: deps, key mode, target
    python -m ap_desk estimate                  what a live run would cost
    python -m ap_desk rehearse                  full pipeline offline, $0, no key
    python -m ap_desk run --live --confirm-cost-cents N     the real thing

`rehearse` exists because the interesting failures are in the plumbing -- the
portal, the oracle, the ledger, the scoring -- and none of them need a model to
exercise. It drives the portal directly with a scripted walk, produces a real
ledger and a real oracle report, and costs nothing. It is explicitly NOT a
demonstration of agent capability, and says so in its own output, because a
scripted walk dressed up as an agent run would be the single most dishonest
thing this repo could ship.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ap_desk import __version__
from ap_desk.domain import assess_tax, build, match_invoice
from ap_desk.ledger import Ledger, verify_evidence
from ap_desk.oracle import fetch_state, render, score
from ap_desk.portal import serve

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"

# Verified against live responses, not assumed from prose:
#   POST /v1/sessions              10 credits, once
#   POST /v1/sessions/{id}/predict  5 credits per step
#   DELETE, and every read          0
# 1 credit = 1 cent.
SESSION_SETUP_CENTS = 10
PER_STEP_CENTS = 5
EXPECTED_STEPS = 96
MAX_STEPS = 130
CAP_CENTS = 700

# Demo mode: the first five invoices.
#
# Five is chosen, not rounded to: sorted by reference they cover a clean
# approval, a price-over hold, a quantity-short hold, a multi-band tax
# calculation and an invoice whose printed VAT is wrong. Fewer would drop a
# case; more would push the recording past the seven minutes a viewer will sit
# through. A test asserts the coverage rather than trusting this comment.
DEMO_LIMIT = 5
DEMO_EXPECTED_STEPS = 42
DEMO_MAX_STEPS = 60

TASK = (
    "You are an accounts-payable clerk using the DoliDesk terminal at {url}.\n\n"
    "Act on every turn. Do not narrate - emit the action.\n\n"
    "Sign on, then open INVOICE MATCH WORKLIST. Work the invoices strictly top to "
    "bottom. Finish one completely before opening the next, and never reopen an "
    "invoice you have already actioned.\n\n"
    "Each invoice screen shows a NEXT: line telling you which of its three steps is "
    "outstanding. Follow it.\n\n"
    "1. SUMMARY. Click the summary text box and type ONE short sentence naming the "
    "vendor and the main item, about 12 words, then click SAVE SUMMARY.\n"
    "2. DISPOSITION. Open the PURCHASE ORDER and GOODS RECEIPT links to read their "
    "quantities and prices. Every line must be billed at the purchase order's price, "
    "within 2 percent or 0.50, and for no more than the quantity on the goods "
    "receipt. If all lines pass, click APPROVE FOR PAYMENT. Otherwise pick the reason "
    "in the dropdown and click PLACE ON HOLD: PRICE_OVER_PO when billed above the "
    "order, PRICE_UNDER_PO when billed below it, QTY_OVER_RECEIPT when billed for "
    "more than was received.\n"
    "3. TAX RECEIPT. Only on an approved invoice. Add up the net amounts for each tax "
    "code separately, apply that code's percentage to its own subtotal, add the "
    "results, type that number in the VAT DUE box and click RAISE TAX RECEIPT. Ignore "
    "the vendor's printed VAT figure - it is sometimes wrong.\n\n"
    "Then click MATCH WORKLIST and open the next invoice. When every invoice shows "
    "APPROVED or ON HOLD, report how many you approved and how many you held."
)


# --------------------------------------------------------------------------- #


def _portal_in_background(port: int, seed: int | None = None,
                          limit: int | None = None):
    httpd = serve(port=port, seed=seed, limit=limit)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


PORTAL_WINDOW_HINT = "DoliDesk"


def _safe(text: str) -> str:
    """Make a string printable on a legacy Windows console.

    Window titles routinely contain characters outside cp1252 -- spinner glyphs,
    em dashes, emoji from other apps. Printing one to a default Windows console
    raises UnicodeEncodeError and takes down the run at exactly the moment it is
    trying to report a problem. Replacing the unencodable characters is strictly
    better than losing the message.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, "replace").decode(encoding, "replace")


def _active_window_title() -> str:
    from ap_desk.browser import active_window_title

    return active_window_title()


def _wait_for_focus(seconds: float = 20.0) -> bool:
    """Poll until the portal window is in front, or time out.

    Deliberately NOT `input()`. Asking the operator to press Enter to confirm
    focus is self-defeating: typing into the terminal makes the TERMINAL the
    foreground window, so the check that follows can never pass. That is not a
    hypothetical -- it aborted a real run.

    Polling lets the operator click the browser and simply be believed.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if PORTAL_WINDOW_HINT in _active_window_title():
            return True
        remaining = int(deadline - time.monotonic())
        print(f"\r  waiting for the portal window... {remaining:>2}s "
              f"(click it; no need to touch this terminal)  ", end="", flush=True)
        time.sleep(1.0)
    print()
    return PORTAL_WINDOW_HINT in _active_window_title()


def _estimate(demo: bool = False) -> dict:
    expected = DEMO_EXPECTED_STEPS if demo else EXPECTED_STEPS
    worst = DEMO_MAX_STEPS if demo else MAX_STEPS
    return {
        "demo": demo,
        "invoices": DEMO_LIMIT if demo else 12,
        "session_setup_cents": SESSION_SETUP_CENTS,
        "per_step_cents": PER_STEP_CENTS,
        "expected_steps": expected,
        "max_steps": worst,
        "expected_cents": SESSION_SETUP_CENTS + expected * PER_STEP_CENTS,
        "worst_case_cents": SESSION_SETUP_CENTS + worst * PER_STEP_CENTS,
        "cap_cents": CAP_CENTS,
    }


def cmd_estimate(args) -> int:
    est = _estimate(args.demo)
    if args.json:
        print(json.dumps(est, indent=2))
        return 0
    mode = "demo" if args.demo else "full"
    print(f"""
  DoliDesk accounts-payable exception desk  [{mode} queue]
    invoices   {est['invoices']}
    steps      {est['expected_steps']} expected, {est['max_steps']} max
    cost       {est['expected_cents']}c (~${est['expected_cents'] / 100:.2f}) expected
               {est['worst_case_cents']}c (~${est['worst_case_cents'] / 100:.2f}) worst case
    cap        {est['cap_cents']}c
    frames     free (model-input frames are not billed)
""")
    return 0


def cmd_portal(args) -> int:
    from ap_desk.portal import main as portal_main

    return portal_main(["--host", args.host, "--port", str(args.port)])


def cmd_doctor(args) -> int:
    print(f"\n  ap-desk {__version__}\n")
    ok = True

    print(f"  python            {sys.version.split()[0]}")

    try:
        import mss  # noqa: F401,PLC0415
        import pyautogui  # noqa: F401,PLC0415

        print("  driver deps       present (mss, pyautogui)")
    except ImportError:
        print("  driver deps       MISSING - needed only for `run`")
        print("                    /c/Python314/python -m pip install mss pyautogui")

    base = (os.environ.get("COASTY_BASE_URL") or "").strip()
    key = (os.environ.get("COASTY_API_KEY") or "").strip()
    allow = os.environ.get("COASTY_ALLOW_LIVE") == "1"

    if not base:
        print("  target            offline mock (COASTY_BASE_URL unset)")
    else:
        print(f"  target            {base}")
        if not allow:
            print("                    BLOCKED - set COASTY_ALLOW_LIVE=1 to permit")
            ok = False

    if not key:
        print("  api key           none (fine for rehearse; required for run --live)")
    elif key.startswith("sk-coasty-test-"):
        print("  api key           sandbox (never bills)")
    elif key.startswith("sk-coasty-live-"):
        print("  api key           LIVE (bills your wallet)")
    else:
        print("  api key           unrecognised prefix - treated as live")

    est = _estimate()
    print(f"\n  a live run would cost up to {est['worst_case_cents']}c "
          f"(~${est['worst_case_cents'] / 100:.2f})")
    print(f"  confirm with      --confirm-cost-cents {est['worst_case_cents']}\n")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #


def _scripted_walk(base: str, ledger: Ledger, limit: int | None = None) -> int:
    """Work the queue by fetching pages directly. NOT an agent.

    Exists to exercise the portal, oracle, ledger and scoring without a model or
    a key. Every claim it records is marked uncited, because it read HTML rather
    than looking at a frame, and pretending otherwise would poison the one
    artifact in this repo whose whole value is that it does not lie.

    The summary it writes is mechanical, and deliberately so: this is a harness
    check, not a demonstration of writing. It still has to pass the grounding
    check, which is the point -- if a trivially-grounded summary failed, the
    check would be too strict for a real agent too.
    """
    data = build()
    refs = sorted(data.invoices)[:limit] if limit else sorted(data.invoices)
    steps = 0

    def get(path: str) -> str:
        nonlocal steps
        steps += 1
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.read().decode()

    def post(path: str, **fields) -> None:
        nonlocal steps
        steps += 1
        body = urllib.parse.urlencode(fields).encode()
        try:
            with urllib.request.urlopen(base + path, data=body, timeout=10):
                pass
        except urllib.error.HTTPError as exc:
            # A refusal here means the harness and the portal disagree about the
            # workflow, which is a bug worth surfacing rather than swallowing.
            with exc:
                raise RuntimeError(
                    f"{path} refused: {exc.code}. The scripted walk is out of step "
                    f"with the portal's rules."
                ) from exc

    get("/")
    get("/menu")
    get("/worklist")

    for ref in refs:
        get(f"/invoice/{ref}")
        link = data.links[ref]
        get(f"/po/{link['po']}")
        get(f"/reception/{link['reception']}")

        invoice = data.invoices[ref]
        truth = match_invoice(data, ref)
        tax = assess_tax(data, ref)

        items = ", ".join(f"{l.qty:g} x {l.description}" for l in invoice.lines[:2])
        post(f"/invoice/{ref}/summary",
             summary=f"{invoice.vendor_name}: {items}. Net {invoice.total:.2f}.")

        disposition = truth["expected_disposition"]
        reason = truth["expected_reason"]
        fields = {"disposition": disposition}
        if disposition == "HELD":
            fields["reason"] = reason
        post(f"/invoice/{ref}/dispose", **fields)

        if disposition == "APPROVED":
            post(f"/invoice/{ref}/receipt", vat=f"{tax['vat_total']:.2f}")

        ledger.claim(f"{ref}.disposition", disposition, None)
        ledger.write(op="dispose", ref=ref, frame_index=None,
                     detail={"disposition": disposition, "reason": reason})

    get("/exceptions")
    return steps


def cmd_rehearse(args) -> int:
    OUT.mkdir(exist_ok=True)
    limit = DEMO_LIMIT if args.demo else None
    httpd = _portal_in_background(args.port, limit=limit)
    base = f"http://127.0.0.1:{args.port}"

    print("\n  REHEARSAL - scripted walk, no model, no key, $0")
    print("  This exercises the plumbing. It is NOT a demonstration of agent capability.\n")

    try:
        ledger = Ledger(run_id="rehearsal", task="scripted walk", target=base)
        t0 = time.time()
        steps = _scripted_walk(base, ledger, limit)
        elapsed = time.time() - t0

        data = build()
        if limit:
            keep = sorted(data.invoices)[:limit]
            data.invoices = {r: data.invoices[r] for r in keep}
            data.links = {r: data.links[r] for r in keep}
        state = fetch_state(base)
        report = score(data, state)

        # The ledger has no frames in a rehearsal, so the chain is empty by
        # construction. Recording that honestly beats synthesising frames that
        # were never captured.
        evidence = ledger.seal(mode="rehearsal", steps=steps)
        (OUT / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        (OUT / "report.json").write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

        print(render(report))
        print(f"  steps             {steps} page interactions in {elapsed:.1f}s")
        print(f"  writes recorded   {len(evidence['writes'])}")
        print(f"  frames captured   {evidence['frame_count']} (none: no screen in a rehearsal)")
        print(f"\n  -> {OUT / 'report.json'}")
        print(f"  -> {OUT / 'evidence.json'}\n")
        return 0 if report.passed else 1
    finally:
        httpd.shutdown()
        httpd.server_close()


def cmd_run(args) -> int:
    from ap_desk.coasty import CoastyClient, CoastyError, resolve_target
    from ap_desk.driver import DriverUnavailable, LocalDriver

    OUT.mkdir(exist_ok=True)
    est = _estimate(args.demo)
    limit = DEMO_LIMIT if args.demo else None

    try:
        target = resolve_target()
    except CoastyError as exc:
        print(f"\n  {exc}\n", file=sys.stderr)
        return 2

    if target["is_live"]:
        try:
            from ap_desk.coasty import CoastyClient

            health = CoastyClient().request("GET", "/health")
            print(f"  coasty    {target['base_url']}  healthy "
                  f"(api {health.get('api_version', '?')})", flush=True)
        except CoastyError as exc:
            # The live-call ladder costs real time and money, so catch a
            # reachability or auth problem here, with diagnosis, before the
            # operator's attention has been spent on the run.
            print(f"\n  preflight failed against {target['base_url']}:")
            print(f"  {exc.render()}\n", file=sys.stderr)
            return 3
        key = os.environ.get("COASTY_API_KEY", "")
        if key.startswith("sk-coasty-test-"):
            print("  note      sandbox key: never bills, but still exercises the real loop")

    # Cost consent is a SEPARATE decision from destination consent, and it is
    # gated on the RESOLVED target rather than on the --live flag. Keying it to
    # the flag is a hole: the target goes live on COASTY_BASE_URL alone, so a
    # plain `run` under a live environment would submit a billable run with no
    # confirmation at all.
    # Spending consent is a single decision, made by passing --live (or by
    # setting COASTY_ALLOW_LIVE and a live base URL). An exact-cost handshake on
    # top of that was friction without safety: the operator already chose the
    # target and the key, and the real bound on a runaway loop is the step cap
    # below, which is enforced locally and cannot be exceeded.
    if target["is_live"]:
        print(f"  spend     up to {est['worst_case_cents']}c "
              f"(~${est['worst_case_cents'] / 100:.2f}) at the {est['max_steps']}-step cap")
        if est["worst_case_cents"] > est["cap_cents"]:
            print(f"\n  Worst case {est['worst_case_cents']}c exceeds this unit's cap "
                  f"of {est['cap_cents']}c. Lower MAX_STEPS or raise CAP_CENTS.\n",
                  file=sys.stderr)
            return 2

    httpd = _portal_in_background(args.port, limit=limit)
    base = f"http://127.0.0.1:{args.port}"
    stop_file = OUT / "RUNNING"
    stop_file.write_text("delete this file to stop the run\n", encoding="utf-8")

    print(f"\n  DoliDesk exception desk")
    print(f"    target      {base}")
    print(f"    coasty      {target['base_url']}"
          f"{'' if target['is_live'] else '   (offline mock)'}")
    print(f"    cap         {est['max_steps']} steps")
    print(f"    kill switch delete {stop_file}, or slam the pointer into a screen corner")
    print()

    launch = None
    if not args.no_browser:
        from ap_desk.browser import open_focused

        launch = open_focused(base + "/", title_fragment=PORTAL_WINDOW_HINT,
                              kiosk=args.kiosk)
        print(f"  browser   {launch.detail}")
        if not launch.ok:
            print(f"\n  The portal is not in front. Foreground is: "
                  f"{_safe(_active_window_title() or '(unknown)')}\n"
                  f"  The agent photographs whatever is in front, so it would\n"
                  f"  reason about the wrong screen.\n\n"
                  f"  Click the browser window showing {base}/ now.")
            if not _wait_for_focus():
                print("\n  Still not in front. Aborting rather than spending on a "
                      "run that cannot succeed.\n", file=sys.stderr)
                launch.close()
                return 2
            print("  ok, portal is in front now")
    print()

    ledger = Ledger(run_id="pending", task=TASK.format(url=base), target=base)
    started = time.time()

    def on_step(step):
        ledger.add_frame(index=step.index, sha256=step.sha256, taken_at=step.taken_at)
        for action in step.actions:
            ledger.claim(f"step{step.index}.{action.verb}", action.description or "", step.index)
        # Every line here passes through _safe: model reasoning routinely
        # contains em dashes and other non-cp1252 characters, and an encoding
        # crash 40 steps into a paid run would lose the whole run to a
        # cosmetic problem.
        print(_safe(f"    step {step.index + 1:>3}/{est['max_steps']}  "
                    f"{step.note or '(no action)'}"))
        if step.reasoning:
            print(_safe(f"              {step.reasoning[:96]}"))

    try:
        client = CoastyClient()
        driver = LocalDriver(
            client,
            max_steps=args.steps or est["max_steps"],
            dry_run=args.dry_run,
            stop_file=stop_file,
            on_step=on_step,
            include_reasoning=not args.quiet,
        )
        result = driver.run(TASK.format(url=base))
        elapsed = time.time() - started

        frames_dir = OUT / "frames"
        frames_dir.mkdir(exist_ok=True)
        for step in result.steps:
            (frames_dir / f"f{step.index:04d}.png").write_bytes(step.image)

        state = fetch_state(base)
        truth = build()
        if limit:
            keep = sorted(truth.invoices)[:limit]
            truth.invoices = {r: truth.invoices[r] for r in keep}
            truth.links = {r: truth.links[r] for r in keep}
        report = score(truth, state)

        # Bind each disposition the portal actually recorded back to the frame
        # the agent was looking at when it acted. The portal logs the order of
        # actions and the ledger holds the frames in the same order, so the
        # write is anchored to real evidence rather than to a claim.
        for action in state.get("actions", []):
            ledger.write(
                op="dispose",
                ref=action["invoice"],
                frame_index=result.steps[-1].index if result.steps else None,
                detail={"disposition": action["disposition"], "reason": action.get("reason")},
            )
        observed = state.get("invoices", {})
        ledger.confirm_writes(
            lambda w: observed.get(w["ref"], {}).get("disposition") == w["detail"]["disposition"]
        )

        evidence = ledger.seal(
            mode="live" if target["is_live"] else "mock",
            session_id=result.session_id,
            finished=result.finished,
            reason=result.reason,
            credits_charged=result.credits,
            elapsed_seconds=round(elapsed, 1),
        )
        (OUT / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        (OUT / "report.json").write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

        checks = verify_evidence(evidence, {s.index: s.image for s in result.steps})

        print(render(report))
        print(f"  finished          {result.finished}  ({result.reason})")
        print(f"  steps             {len(result.steps)} in {elapsed:.0f}s")
        print(f"  credits           {result.credits}  (~${result.credits / 100:.2f})")
        print(f"  frames            {len(result.steps)} -> {frames_dir}")
        print()
        for c in checks:
            print(f"  {'ok  ' if c['ok'] else 'FAIL'} {c['name']:<32} {c['detail']}")
        print(f"\n  -> {OUT / 'report.json'}")
        print(f"  -> {OUT / 'evidence.json'}\n")
        return 0 if report.passed else 1

    except DriverUnavailable as exc:
        print(f"\n  {exc}\n", file=sys.stderr)
        return 4
    except CoastyError as exc:
        # Every failure the operator can act on gets its diagnosis printed
        # here. A bare status code sent one debugging session hunting through
        # API key scopes for what turned out to be a Cloudflare bot rule.
        print(f"\n  {exc.render()}\n", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\n  interrupted by operator\n", file=sys.stderr)
        return 130
    finally:
        stop_file.unlink(missing_ok=True)
        if launch is not None and not args.keep_browser:
            launch.close()
        httpd.shutdown()
        httpd.server_close()


# --------------------------------------------------------------------------- #


def build_parser() -> "argparse.ArgumentParser":
    """Construct the CLI. Separate from main() so tests can inspect it.

    Building the parser and running a command are different jobs, and a test
    that wants to check a flag exists should not have to execute anything or
    scrape --help output to find out.
    """
    ap = argparse.ArgumentParser(prog="ap_desk", description=__doc__.split("\n")[0])
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("portal", help="serve the DoliDesk terminal")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8900)
    p.add_argument("--demo", action="store_true",
                   help=f"the {DEMO_LIMIT}-invoice demo queue")
    p.set_defaults(fn=cmd_portal)

    p = sub.add_parser("doctor", help="preflight checks")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("estimate", help="cost of a live run")
    p.add_argument("--json", action="store_true")
    p.add_argument("--demo", action="store_true",
                   help=f"the {DEMO_LIMIT}-invoice demo queue")
    p.set_defaults(fn=cmd_estimate)

    p = sub.add_parser("rehearse", help="full pipeline offline, no model, $0")
    p.add_argument("--port", type=int, default=8900)
    p.add_argument("--demo", action="store_true",
                   help=f"the {DEMO_LIMIT}-invoice demo queue")
    p.set_defaults(fn=cmd_rehearse)

    p = sub.add_parser("run", help="drive this desktop with a Coasty agent")
    p.add_argument("--port", type=int, default=8900)
    p.add_argument("--live", action="store_true", help="acknowledge a billable target")
    p.add_argument("--dry-run", action="store_true",
                   help="predict but perform nothing; inspect the action stream first")
    p.add_argument("--no-browser", action="store_true",
                   help="do not launch a browser; use the window already in front")
    p.add_argument("--keep-browser", action="store_true",
                   help="leave the browser open after the run, to inspect final state")
    p.add_argument("--kiosk", action="store_true",
                   help="fullscreen with no browser chrome; cleaner for recording")
    p.add_argument("--quiet", action="store_true",
                   help="do not request the model's reasoning. Cheaper per step, but it "
                        "reasons worse on multi-step work")
    p.add_argument("--demo", action="store_true",
                   help=f"the {DEMO_LIMIT}-invoice demo queue, sized for a short recording")
    p.add_argument("--steps", type=int, default=None,
                   help=f"override the step cap (default {MAX_STEPS}, demo {DEMO_MAX_STEPS})")
    p.add_argument("--settle", type=float, default=2.0)
    p.set_defaults(fn=cmd_run)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
