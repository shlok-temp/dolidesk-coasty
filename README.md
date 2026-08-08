# DoliDesk — autonomous accounts-payable desk

An AI agent works a real ERP-style invoice queue **from pixels alone** — no
selectors, no DOM, no API. It matches every invoice against its purchase order
and goods receipt, writes a summary, approves or holds it, then computes the VAT
and files a tax receipt.

Every decision is checked against an independent oracle that never sees the
screen. There is no answer key.

Built on [Coasty](https://coasty.ai) ([@coastyai](https://x.com/coastyai)). Runs
on **your** desktop, so you can watch it work and record it.

```bash
python -m ap_desk rehearse              # whole pipeline, offline, no key, $0
python -m ap_desk run --live --demo     # the agent drives your desktop
```

---

## The business problem

Before an invoice is paid, someone has to confirm it against two other
documents: the **purchase order** (did we agree this price?) and the **goods
receipt** (did we actually receive this much?). Agree on both → pay it.
Disagree → hold it and say why. Then the tax has to be worked out line by line,
because different goods sit in different VAT bands.

This is the *three-way match*, and it is one of the largest recurring manual
workloads in any finance function. It resists automation because ERP
auto-matching only works when every document is clean and every system is
integrated. In practice invoices arrive as PDFs and portal screens, orders live
in one system, receipts in another, and a human opens three screens and compares
numbers by eye. Industry benchmarks put manual invoice handling around $10–15
each, with 10–25% falling out to exceptions that take 10–20 minutes apiece.

Three screens, no shared API, real judgment, real arithmetic: exactly the shape
of problem a vision agent exists for. And because it is a **financial control**,
being right matters more than being fast — which is why most of this repo is
about proving correctness rather than demonstrating capability.

## What the agent actually does

Twelve vendor invoices sit in a worklist, deliberately unsorted, so the agent
cannot succeed by reading row one. For each invoice it performs three steps in
order.

**Match and dispose** *(always on)*. Opens the invoice, then its purchase order
and its goods receipt, and compares all three line by line. Approves when prices
and quantities agree within tolerance; otherwise holds the invoice and selects a
reason code. Five invoices are clean; seven carry a planted defect — billed above
the agreed price, below it, or for more than was received. Two carry two at once.

**VAT check** *(`--with-tax`)*. Each line carries a VAT band — standard 20%,
reduced 5%, or zero. The agent totals each band separately, applies that band's
rate, and says whether the vendor's printed VAT figure agrees. On three invoices
**it does not**, so accepting what is printed scores as a failure.

**Written summary** *(`--with-summary`)*. A one-line plain-English summary typed
into the terminal before the invoice can be dispositioned, graded for grounding.

The stages are separate switches because they were built up one at a time and
each has to hold on its own. The match flow alone is a complete automation:
**36 documents opened, 12 decisions, ~50 steps** on the full queue.

## Why this is a real test, not a demo

**There is no answer key.** The expected verdict and the correct VAT for every
invoice are recomputed from the underlying records on each run. Change the seed
and the truth changes with it. Nothing is stored for the agent to match against.

**Writes are verified, not trusted.** Disposition, summary and declared VAT are
all read back from the portal's server state, not from the agent's own report.
An agent that *says* it held an invoice but never submitted the form scores as
wrong — the failure mode that matters most in a financial control.

**The scorer is proven to catch mistakes.** It is tested against every way the
agent can be wrong, and each is a distinct outcome:

```
FA-2581    APPROVED   HELD       WRONG DISPOSITION
FA-2582    HELD       APPROVED   WRONG DISPOSITION  (PRICE_OVER_PO)
FA-2583    APPROVED   -          NOT ACTIONED
FA-2584    HELD       HELD       RIGHT HOLD, WRONG REASON
FA-2585    APPROVED   APPROVED   RIGHT CALL, WRONG VAT
FA-2586    APPROVED   APPROVED   APPROVED, NO VAT CHECK
FA-2587    HELD       HELD       CORRECT, UNGROUNDED SUMMARY
```

A scorer only ever shown correct input proves nothing. These are the tests that
make the accuracy figure mean something.

**The summary is graded for grounding, not for style.** Grading prose is not
something a test should pretend to do. What *is* checkable is whether the
summary names something only that invoice showed — its vendor, an item code, a
word from its own line descriptions. Generic filler fails.

**Every claim cites the frame behind it.** `out/evidence.json` hash-chains every
model-input frame and binds each decision to the frame index that justified it.
Tampering breaks the chain, and `verify_evidence` re-derives the root
independently.

## Run it

### Offline — no key, no cost, no network

```bash
python -m ap_desk rehearse                     # all 12 invoices
python -m ap_desk rehearse --demo              # the 5-invoice demo queue
python -m ap_desk rehearse --demo --with-tax   # add the VAT check
```

```
  INVOICE    EXPECTED  ACTUAL      VAT DUE  DECLARED  RESULT
  FA-2581    APPROVED  APPROVED    2655.56         -  CORRECT
  FA-2582    HELD      HELD         256.49         -  CORRECT
  FA-2583    APPROVED  APPROVED    2515.16         -  CORRECT
  FA-2584    HELD      HELD          72.48         -  CORRECT
  FA-2585    APPROVED  APPROVED    1466.08         -  CORRECT

  actioned            5/5
  vat checks          3 made, 3/3 with correct VAT
  fully correct       5/5  (100.0%)
  verdict             PASS
```

This is a **scripted walk, not an agent** — it exists to exercise the portal,
oracle, ledger and scoring without a model or a key, and it says so in its own
output.

Browse the terminal yourself:

```bash
python -m ap_desk portal --demo       # http://localhost:8900
```

### Live — the agent drives your desktop

```bash
python -m pip install mss pyautogui

export COASTY_API_KEY=sk-coasty-live-...     # coasty.ai/developers/keys
export COASTY_BASE_URL=https://coasty.ai/v1
export COASTY_ALLOW_LIVE=1

python -m ap_desk doctor                      # preflight
python -m ap_desk run --dry-run               # predict, perform nothing
python -m ap_desk run --live --demo --kiosk   # the recording run
python -m ap_desk run --live                  # the full 12-invoice queue
```

`run` launches Chrome maximised on the portal, brings it genuinely to the front,
then moves your real mouse and types real keys. Watch it happen; record it with
OBS. The portal stays on `localhost` — nothing is exposed to the internet.

**Two kill switches:** slam the pointer into any screen corner (PyAutoGUI's
failsafe), or delete `out/RUNNING`.

Start with `--dry-run`. It captures and predicts but performs nothing, so you can
read the action stream before anything touches your desktop.

| Flag | Effect |
|---|---|
| `--demo` | the 5-invoice queue, sized to finish inside a short recording |
| `--kiosk` | app-mode window with no browser chrome — records better |
| `--with-tax` | also require the VAT check on approved invoices |
| `--with-summary` | also require a written summary per invoice |
| `--steps N` | lower the step cap for a quick sanity pass |
| `--keep-browser` | leave the browser open afterwards to inspect the queue |
| `--no-browser` | drive whatever window is already in front |

## Cost

Verified against live responses, not assumed from the docs.

| Call | Credits |
|---|---|
| `POST /v1/sessions` | 10, once |
| `POST /v1/sessions/{id}/predict` | 5 per step |
| session delete, all reads | 0 |

1 credit = 1 cent.

| Queue | Expected | Worst case |
|---|---|---|
| `--demo` (5 invoices) | 220¢ (~$2.20) | 310¢ (~$3.10) |
| full (12 invoices) | 490¢ (~$4.90) | 660¢ (~$6.60) |

`python -m ap_desk estimate --demo` prints it before anything runs.

### What bounds the spend

A **step cap**, enforced locally before any request is sent. It cannot be
exceeded by anything the server returns, and `--steps N` lowers it. That is the
real bound on a runaway loop.

An unset `COASTY_BASE_URL` resolves to the offline path, so **production is never
a default** — reaching a billable host needs `COASTY_ALLOW_LIVE=1`.

## Which Coasty layer this uses, and why

Coasty exposes three layers. Their published catalog uses the highest,
`POST /v1/tasks`, which provisions a cloud VM and drives a browser there.

This uses the lowest — `/v1/sessions` + `/v1/sessions/{id}/predict`, which their
docs call *"own the screenshot and action loop"*. We capture, they decide, we
act:

```
capture this screen → POST /v1/sessions/{id}/predict → perform the actions
```

That buys three things: the run happens on a machine you can watch and record,
the target can stay on localhost with nothing exposed, and the prompt names a
real URL that a real browser really navigates.

## Three things that cost real debugging time

Written down because the next person should not have to repeat them.

**Cloudflare blocks `urllib`.** Coasty sits behind bot protection that rejects
Python's default `User-Agent` with a **403 carrying a Cloudflare 1010 body** —
even on `/health`, which is documented as public and unauthenticated. That 403
is indistinguishable at a glance from `INSUFFICIENT_SCOPE` and sends you auditing
API key scopes for a problem that lives entirely in the transport. A
browser-shaped `User-Agent` is the whole fix; this client sends one.

**Session predict takes exactly four fields.** `screenshot`, `instruction`,
`include_reasoning`, `include_raw_code`. Screen size and trajectory belong on
session *create* — sending them per turn is a `422`. And unlike stateless
`/v1/predict`, the session response does **not** echo `screen_width`/
`screen_height`, so the captured size is a required fallback, not a nicety.

**Windows will not give up the foreground on request.** A process that does not
already own the foreground cannot take it: `SetForegroundWindow` returns success
and merely flashes the taskbar button, and `webbrowser.open` makes it worse by
handing the URL to an existing Chrome that opens a tab in a background window.
`pygetwindow.activate()` and PyAutoGUI both go through that same call, so neither
is a fix. What works is launching a separate process with its own
`--user-data-dir`, then `AttachThreadInput` plus a synthetic ALT keypress to
release the foreground lock — and then *verifying* the window really is in front,
because the API will happily claim otherwise. `ap_desk/browser.py` does this and
holds focus.

Every error this client raises carries its own diagnosis:

```
HTTP_403: POST /sessions -> HTTP 403
  request_id: req_x
  Cloudflare blocked the request before it reached Coasty (error 1010).
  This is NOT an auth or scope problem.
```

## Layout

```
ap_desk/
  domain.py     seeded invoices, POs, receipts, VAT bands + the matching rules
  portal.py     DoliDesk — a real HTTP terminal with real forms
  coasty.py     Coasty client: fail-closed target, retry, self-diagnosing errors
  actions.py    the action wire format, pinned to observed live responses
  browser.py    launch Chrome maximised and genuinely in front
  driver.py     capture → predict → act, on this desktop
  oracle.py     independent scoring across disposition, VAT and summary
  ledger.py     tamper-evident frame chain
  cli.py        portal · doctor · estimate · rehearse · run
tools/
  apicheck.py   graded live API diagnostics (free calls first)
tests/          116 tests, standard library only
```

The core is **standard library only** — no install needed for the portal, oracle,
ledger, scoring or tests. `mss` and `pyautogui` are required only by the local
driver and imported lazily, so importing anything else never pulls them in.

## Tests

```bash
python -m unittest discover -s tests -v      # 116 tests, offline, no key
```

- **`test_domain.py`** — every planted defect produces exactly the finding it was
  planted to produce. This caught a real bug: a 17.6% price variance on a £1.25
  line is 22p, below the de-minimis floor, so the rule correctly ignored it and
  that invoice was silently testing nothing.
- **`test_tax.py`** — VAT is banded, half-up rounded, and every planted vendor
  error is detectable. Also asserts the seed and the oracle compute VAT
  independently and still agree.
- **`test_portal_oracle.py`** — the scorer catches all failure modes, and the
  portal never leaks the expected verdict onto a screen the agent can read.
- **`test_actions.py`** — the wire format, pinned to responses captured live. The
  first draft of the driver was written from a plausible reading of the prose
  docs and got four things wrong at once.
- **`test_ledger.py`** — adversarial: forge a frame, delete a frame, reorder
  frames, forge the root, swap the bytes on disk. Each must be caught by a named
  check.

## What this does not claim

The evidence ledger is built by this repo from data Coasty returns, so it
demonstrates **internal consistency** — that the frames, claims and writes agree
with each other and have not been altered since. It does not prove Coasty was
honest about the frames in the first place. That would need a signature from
Coasty over the frame hashes, which they do not currently offer.

Saying so plainly seemed better than letting a hash chain imply more than it
carries.

---

MIT. The DoliDesk terminal and its data are simulated; the operator ID it signs
on with is one the sign-on screen displays itself. No real credential to any real
system appears in this repo.
