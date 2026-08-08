"""DoliDesk -- a real web application that behaves like a legacy AP terminal.

This is the system the agent drives. It is deliberately NOT a pre-rendered
sequence of images: it is an HTTP server with real pages, real links and real
forms, so a real browser navigates it and a POST actually mutates state.

Why that distinction is the whole point
---------------------------------------
The reference catalog renders its "terminal" as a PNG sequence for the offline
demo, and no prompt in it names a URL -- so there is nothing for a live agent to
navigate to. Serving real HTML costs no more effort (markup is far cheaper to
author than a bitmap font) and buys three things that matter:

  * the prompt can name a real address, which the catalog's own AGENTS.md
    demands as its first rule of prompt writing;
  * the agent can be wrong in realistic ways -- misread a row, click the wrong
    link, submit the wrong form -- which is what makes the test meaningful;
  * disposition is a real POST that changes server state, so the oracle can
    confirm what actually happened rather than trusting a summary.

The screens are styled as a monochrome terminal on purpose. That is the honest
case for computer use: systems old enough to have no API are exactly the ones
worth driving by sight. It is a real HTTP application wearing a 1980s coat.

Two access paths, deliberately separate
---------------------------------------
  HTML routes  (/, /menu, /worklist, /invoice/..., POST /dispose)
      The agent's only view. Everything it knows must come from these pixels.

  JSON routes  (/_state, /_reset)
      The oracle's view, and the test harness's reset hook. Underscore-prefixed
      so they read as out-of-band. The agent is never told these exist, and
      nothing in the HTML links to them.

Both read the same in-memory dataset. That is the point: two independent paths
to one truth, with no answer key in between.

Zero dependencies -- http.server and html.escape only.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from urllib.parse import parse_qs, urlparse

from ap_desk.domain import TAX_CODES, Dataset, build

TITLE = "DoliDesk"
SUBTITLE = "ACCOUNTS PAYABLE - MATCH & TAX ASSESSMENT"

# The operator ID the sign-on screen displays to itself. Not a credential to
# anything real -- the same convention the reference catalog uses, and the only
# kind of sign-on that belongs in a public repo.
OPERATOR_ID = "AP0417"

HOLD_REASONS = [
    ("PRICE_OVER_PO", "Unit price above purchase order"),
    ("PRICE_UNDER_PO", "Unit price below purchase order"),
    ("QTY_OVER_RECEIPT", "Quantity billed exceeds goods received"),
    ("NOT_ON_PO", "Item not present on purchase order"),
]

# Palette: neon blue on silver, in the spirit of Dolibarr's own light theme.
#
# The foreground shades are deliberately dark. A neon accent that reads well on
# black becomes illegible on silver, and an agent that cannot resolve a figure
# from a screenshot fails for a reason that has nothing to do with its
# reasoning -- so text sits at high contrast and the neon is reserved for
# chrome, borders and accents.
STYLE = """
*{box-sizing:border-box}
body{margin:0;background:#e8ebf0;color:#16233a;
     font:15px/1.55 'Segoe UI','Helvetica Neue',Arial,sans-serif}
a{color:#0b5cd8;text-decoration:none;border-bottom:1px solid #9dc4f5}
a:hover{color:#00317f;border-bottom-color:#0b5cd8}
.wrap{max-width:1120px;margin:0 auto;padding:18px 22px 60px}
.bar{background:linear-gradient(180deg,#12325e,#0d2547);border-bottom:3px solid #1e9bff;
     padding:11px 22px;display:flex;justify-content:space-between;align-items:baseline;
     box-shadow:0 1px 6px rgba(12,40,80,.35)}
.bar h1{margin:0;font-size:18px;letter-spacing:1.5px;color:#eaf4ff;font-weight:600}
.bar .sub{font-size:12px;color:#7fd0ff;letter-spacing:1.2px}
h2{font-size:15px;letter-spacing:.8px;color:#0d2547;border-bottom:2px solid #1e9bff;
   padding-bottom:6px;margin:22px 0 14px;text-transform:uppercase;font-weight:600}
h3{font-size:13px;letter-spacing:.6px;color:#12325e;margin:16px 0 8px;text-transform:uppercase}
table{width:100%;border-collapse:collapse;margin:10px 0 18px;background:#fff;
      box-shadow:0 1px 3px rgba(20,40,70,.13)}
th{text-align:left;font-size:11.5px;letter-spacing:.9px;color:#0d2547;background:#d3dcea;
   border-bottom:2px solid #1e9bff;padding:7px 10px;text-transform:uppercase}
td{padding:7px 10px;border-bottom:1px solid #dfe5ee;color:#16233a}
tr:hover td{background:#eff6ff}
.num{text-align:right;font-variant-numeric:tabular-nums;
     font-family:'Consolas','DejaVu Sans Mono',monospace}
.kv{display:grid;grid-template-columns:220px 1fr;gap:5px 16px;margin:10px 0 18px}
.kv dt{color:#3d5a80;font-size:12.5px;letter-spacing:.7px;text-transform:uppercase}
.kv dd{margin:0;color:#0d2547;font-weight:600}
.panel{border:1px solid #b9c8dd;border-left:4px solid #1e9bff;padding:14px 18px;
       margin:16px 0;background:#f6f9fd;box-shadow:0 1px 3px rgba(20,40,70,.10)}
.tag{display:inline-block;padding:2px 9px;border:1px solid currentColor;font-size:11.5px;
     letter-spacing:.8px;font-weight:600;border-radius:2px}
.ok{color:#0a6b3d}.warn{color:#8a5a00}.held{color:#b3231c}.info{color:#0b5cd8}
.nav{margin:14px 0;font-size:13px;padding-bottom:10px;border-bottom:1px solid #cdd8e8}
.nav a{margin-right:20px}
form{margin:8px 0}
button{font:600 14px 'Segoe UI',Arial,sans-serif;background:linear-gradient(180deg,#1e9bff,#0b5cd8);
  color:#fff;border:1px solid #0947a8;padding:8px 18px;cursor:pointer;border-radius:3px;
  box-shadow:0 1px 2px rgba(9,71,168,.3)}
button:hover{background:linear-gradient(180deg,#3aa9ff,#0d68e8)}
button.hold{background:linear-gradient(180deg,#e8663c,#c33f1c);border-color:#a32f10}
button.hold:hover{background:linear-gradient(180deg,#f07a52,#d44a26)}
select,input[type=text]{font:14px 'Segoe UI',Arial,sans-serif;background:#fff;color:#16233a;
  border:1px solid #94aac8;padding:7px 10px;border-radius:3px}
input[type=text]{width:240px}
.foot{margin-top:26px;font-size:12px;color:#3d5a80;border-top:2px solid #1e9bff;padding-top:8px}
.hint{color:#3d5a80;font-size:13px}
.total-row td{border-top:2px solid #1e9bff;font-weight:700;background:#eff6ff}
"""


def _page(title: str, body: str, crumb: str = "") -> bytes:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{escape(title)} - {TITLE}</title><style>{STYLE}</style></head><body>
<div class="bar"><h1>{TITLE}</h1><span class="sub">{escape(SUBTITLE)}</span></div>
<div class="wrap">{f'<div class="nav">{crumb}</div>' if crumb else ''}{body}
<div class="foot">{TITLE} &middot; OPERATOR {OPERATOR_ID} &middot; SESSION ACTIVE</div>
</div></body></html>""".encode("utf-8")


def _money(x: float) -> str:
    return f"{x:,.2f}"


def _lines_table(lines, *, qty_label: str = "QTY") -> str:
    rows = "".join(
        f"<tr><td>{escape(l.item)}</td><td>{escape(l.description)}</td>"
        f'<td class="num">{l.qty:g}</td>'
        f'<td class="num">{_money(l.unit_price)}</td>'
        f'<td class="num">{_money(l.extended)}</td></tr>'
        for l in lines
    )
    return (
        f"<table><tr><th>ITEM</th><th>DESCRIPTION</th><th class='num'>{escape(qty_label)}</th>"
        f"<th class='num'>UNIT PRICE</th><th class='num'>EXTENDED</th></tr>{rows}</table>"
    )


def _lines_table_taxed(lines) -> str:
    """Invoice lines including the tax code column.

    The tax code is shown, the tax AMOUNT is not. Which band a line sits in is a
    fact about the goods and appears on any real invoice, so withholding it
    would be artificial. Computing the VAT from those bands is the task, so
    printing the per-line amount would hand over the answer.
    """
    rows = "".join(
        f"<tr><td>{escape(l.item)}</td><td>{escape(l.description)}</td>"
        f'<td class="num">{l.qty:g}</td>'
        f'<td class="num">{_money(l.unit_price)}</td>'
        f"<td>{escape(l.tax_code)} - {escape(TAX_CODES[l.tax_code][0])} "
        f"({l.tax_rate:g}%)</td>"
        f'<td class="num">{_money(l.extended)}</td></tr>'
        for l in lines
    )
    return (
        "<table><tr><th>ITEM</th><th>DESCRIPTION</th><th class='num'>QTY BILLED</th>"
        "<th class='num'>UNIT PRICE</th><th>TAX CODE</th>"
        f"<th class='num'>NET</th></tr>{rows}</table>"
    )


class Portal:
    """Owns the mutable dataset. One instance per server."""

    def __init__(self, seed: int | None = None, limit: int | None = None) -> None:
        self._lock = threading.Lock()
        # Remembered so /_reset rebuilds the same shape it started with. A reset
        # that silently restored all twelve invoices mid-demo would leave the
        # agent working a queue the operator did not ask for.
        self.limit = limit
        self.reset(seed, limit)

    def reset(self, seed: int | None = None, limit: int | None = None) -> None:
        """Rebuild the dataset. `limit` keeps only the first N invoices.

        The limit exists for demo mode. It trims the WORKLIST but leaves every
        supporting document in place, so a kept invoice always has its purchase
        order and receipt -- trimming those too would produce broken links and
        an agent failing for the harness's reasons rather than its own.
        """
        with self._lock:
            data = build() if seed is None else build(seed)
            if limit is not None and limit > 0:
                keep = sorted(data.invoices)[:limit]
                data.invoices = {r: data.invoices[r] for r in keep}
                data.links = {r: data.links[r] for r in keep}
            self.data: Dataset = data
            self.actions: list[dict] = []
            self.receipt_seq = 0

    def summarise(self, ref: str, text: str) -> tuple[bool, str]:
        """Store the agent's written summary of an invoice.

        Deliberately permissive about content and strict about emptiness. What
        makes a good summary is not something a form can adjudicate, but a blank
        one is unambiguously a skipped step, and accepting it would let an agent
        appear to have done the work.
        """
        with self._lock:
            inv = self.data.invoices.get(ref)
            if inv is None:
                return False, f"NO SUCH INVOICE: {ref}"
            cleaned = " ".join(str(text or "").split())
            if len(cleaned) < 20:
                return False, "SUMMARY MUST BE AT LEAST 20 CHARACTERS"
            inv.summary = cleaned[:600]
            self.actions.append({"invoice": ref, "action": "summary", "chars": len(inv.summary)})
            return True, f"SUMMARY SAVED FOR {ref}"

    def raise_tax_receipt(self, ref: str, vat_declared: str) -> tuple[bool, str]:
        """Raise a tax receipt against an approved invoice.

        Two refusals matter here, and both mirror a real control:

        * an invoice on hold has no agreed value, so it cannot be taxed;
        * the declared VAT must parse as money. Coercing a malformed figure to
          zero would turn an agent's mistake into a silently plausible receipt.

        The figure is NOT validated against the correct answer. That is the
        oracle's job, and checking it here would tell the agent whether it was
        right -- letting it retry until it passed, which is not a test.
        """
        with self._lock:
            inv = self.data.invoices.get(ref)
            if inv is None:
                return False, f"NO SUCH INVOICE: {ref}"
            if inv.disposition != "APPROVED":
                return False, "TAX RECEIPT REQUIRES AN APPROVED INVOICE"
            if inv.tax_receipt_ref:
                return False, f"TAX RECEIPT {inv.tax_receipt_ref} ALREADY RAISED"

            raw = str(vat_declared or "").strip().replace(",", "").replace("£", "")
            try:
                amount = round(float(raw), 2)
            except ValueError:
                return False, f"VAT AMOUNT NOT A NUMBER: {vat_declared!r}"
            if amount < 0:
                return False, "VAT AMOUNT CANNOT BE NEGATIVE"

            self.receipt_seq += 1
            receipt_ref = f"TR-{9000 + self.receipt_seq}"
            inv.tax_receipt_ref = receipt_ref
            inv.declared_vat = amount
            self.actions.append(
                {"invoice": ref, "action": "tax_receipt",
                 "receipt": receipt_ref, "vat_declared": amount}
            )
            return True, f"TAX RECEIPT {receipt_ref} RAISED FOR {ref}"

    def dispose(self, ref: str, disposition: str, reason: str | None) -> tuple[bool, str]:
        """Record a disposition. Returns (ok, message).

        Rejects unknown refs and unknown dispositions rather than coercing them.
        An agent that submits nonsense should see the system refuse it, exactly
        as a real terminal would -- silently accepting a bad value would hide a
        genuine agent error behind a green tick.
        """
        with self._lock:
            inv = self.data.invoices.get(ref)
            if inv is None:
                return False, f"NO SUCH INVOICE: {ref}"
            if disposition not in ("APPROVED", "HELD"):
                return False, f"INVALID DISPOSITION: {disposition}"
            if disposition == "HELD" and not reason:
                return False, "HOLD REQUIRES A REASON CODE"
            # The summary is a prerequisite, not an optional extra: it forces
            # the agent to have read the invoice before ruling on it, and the
            # order is what makes the written summary evidence rather than an
            # afterthought composed once the answer was already known.
            if not inv.summary:
                return False, "RECORD THE INVOICE SUMMARY BEFORE SETTING A DISPOSITION"
            inv.disposition = disposition
            inv.hold_reason = reason if disposition == "HELD" else None
            inv.status = "APPROVED FOR PAYMENT" if disposition == "APPROVED" else "ON HOLD"
            inv.touched_by = OPERATOR_ID
            self.actions.append({"invoice": ref, "disposition": disposition, "reason": reason})
            return True, f"{ref} SET TO {disposition}"

    def state(self) -> dict:
        """The oracle's view. Records only -- never a verdict.

        Returning a computed verdict here would collapse the two independent
        paths into one and quietly turn the oracle into an answer key.
        """
        with self._lock:
            return {
                "invoices": {
                    ref: {
                        "ref": d.ref,
                        "vendor_code": d.vendor_code,
                        "vendor_name": d.vendor_name,
                        "doc_date": d.doc_date,
                        "status": d.status,
                        "disposition": d.disposition,
                        "hold_reason": d.hold_reason,
                        "summary": d.summary,
                        "tax_receipt_ref": d.tax_receipt_ref,
                        "declared_vat": d.declared_vat,
                        "claimed_vat": d.claimed_vat,
                        "total": d.total,
                        "lines": [asdict(l) for l in d.lines],
                    }
                    for ref, d in self.data.invoices.items()
                },
                "links": self.data.links,
                "actions": list(self.actions),
            }


# --------------------------------------------------------------------------- #
# Screens
# --------------------------------------------------------------------------- #

CRUMB = (
    '<a href="/menu">FUNCTION MENU</a><a href="/worklist">MATCH WORKLIST</a>'
    '<a href="/exceptions">EXCEPTION QUEUE</a>'
)


def screen_signon() -> bytes:
    body = f"""
<h2>SIGN ON</h2>
<div class="panel">
<form method="post" action="/signon">
<div class="kv"><dt>OPERATOR ID</dt><dd><input type="text" name="operator" value="{OPERATOR_ID}"></dd></div>
<button type="submit">SIGN ON</button>
</form>
<p style="color:#3f9c6b;font-size:13px">Operator ID is pre-filled for this terminal. Press SIGN ON to continue.</p>
</div>"""
    return _page("Sign on", body)


def screen_menu() -> bytes:
    body = """
<h2>FUNCTION MENU</h2>
<div class="panel">
<table>
<tr><th>CODE</th><th>FUNCTION</th><th>DESCRIPTION</th></tr>
<tr><td>01</td><td><a href="/worklist">INVOICE MATCH WORKLIST</a></td>
    <td>Vendor invoices awaiting three-way match</td></tr>
<tr><td>02</td><td><a href="/exceptions">EXCEPTION QUEUE</a></td>
    <td>Invoices placed on hold, with reason codes</td></tr>
</table>
</div>"""
    return _page("Function menu", body, CRUMB)


def screen_worklist(portal: Portal) -> bytes:
    rows = []
    for inv in portal.data.invoice_list():
        link = portal.data.links[inv.ref]
        state = inv.disposition or "AWAITING MATCH"
        cls = {"APPROVED": "ok", "HELD": "held"}.get(inv.disposition or "", "warn")
        rows.append(
            f'<tr><td><a href="/invoice/{escape(inv.ref)}">{escape(inv.ref)}</a></td>'
            f"<td>{escape(inv.vendor_name)}</td><td>{escape(inv.doc_date)}</td>"
            f'<td class="num">{_money(inv.total)}</td>'
            f"<td>{escape(link['po'])}</td><td>{escape(link['reception'])}</td>"
            f'<td><span class="tag {cls}">{escape(state)}</span></td></tr>'
        )
    pending = sum(1 for i in portal.data.invoices.values() if i.disposition is None)
    body = f"""
<h2>INVOICE MATCH WORKLIST</h2>
<p>RECORDS SELECTED: {len(portal.data.invoices)} &nbsp;&middot;&nbsp; AWAITING DISPOSITION: {pending}</p>
<table>
<tr><th>INVOICE</th><th>VENDOR</th><th>DATE</th><th class="num">TOTAL</th>
    <th>ORDER</th><th>RECEIPT</th><th>STATUS</th></tr>
{''.join(rows)}
</table>
<p style="color:#3f9c6b;font-size:13px">Open each invoice to compare it against its order and receipt.</p>"""
    return _page("Match worklist", body, CRUMB)


def screen_invoice(portal: Portal, ref: str, message: str = "",
                   error: str = "") -> bytes | None:
    inv = portal.data.invoices.get(ref)
    if inv is None:
        return None
    link = portal.data.links[ref]
    opts = "".join(f'<option value="{c}">{c} - {escape(d)}</option>' for c, d in HOLD_REASONS)

    banner = ""
    if message:
        banner = f'<div class="panel"><span class="tag ok">{escape(message)}</span></div>'
    if error:
        banner = (f'<div class="panel" style="border-left-color:#c33f1c">'
                  f'<span class="tag held">{escape(error)}</span></div>')

    state = inv.disposition or "AWAITING MATCH"
    cls = {"APPROVED": "ok", "HELD": "held"}.get(inv.disposition or "", "warn")
    reason = f'<dt>HOLD REASON</dt><dd>{escape(inv.hold_reason)}</dd>' if inv.hold_reason else ""
    claimed = (f'<dt>VAT CLAIMED BY VENDOR</dt><dd>{_money(inv.claimed_vat)}</dd>'
               if inv.claimed_vat is not None else "")

    # Step 1 -- the written summary. Shown as done once recorded, so an agent
    # re-reading the screen can tell what it has already completed.
    if inv.summary:
        summary_panel = f"""
<div class="panel">
<h3>1. Invoice summary <span class="tag ok">RECORDED</span></h3>
<p>{escape(inv.summary)}</p>
</div>"""
    else:
        summary_panel = f"""
<div class="panel">
<h3>1. Invoice summary</h3>
<p class="hint">Write a short plain-English summary of what this invoice covers:
the vendor, what was bought, and anything notable. Required before a disposition
can be set.</p>
<form method="post" action="/invoice/{escape(inv.ref)}/summary">
<textarea name="summary" rows="3" style="width:100%;font:14px 'Segoe UI',Arial;
 padding:8px;border:1px solid #94aac8;border-radius:3px"
 placeholder="e.g. Northgate Supply, 40 USB-C docks and 100 patch leads, standard-rated..."></textarea>
<button type="submit">SAVE SUMMARY</button>
</form>
</div>"""

    # Step 3 -- the tax receipt, only reachable once approved.
    if inv.tax_receipt_ref:
        tax_panel = f"""
<div class="panel">
<h3>3. Tax receipt <span class="tag ok">RAISED</span></h3>
<dl class="kv">
<dt>RECEIPT</dt><dd><a href="/receipt/{escape(inv.tax_receipt_ref)}">{escape(inv.tax_receipt_ref)}</a></dd>
<dt>VAT DECLARED</dt><dd>{_money(inv.declared_vat or 0)}</dd>
</dl>
</div>"""
    elif inv.disposition == "APPROVED":
        tax_panel = f"""
<div class="panel">
<h3>3. Raise tax receipt</h3>
<p class="hint">Work out the VAT actually due by applying each line's tax rate to
its net amount, band by band. Do not copy the vendor's figure &mdash; it is not
always right.</p>
<form method="post" action="/invoice/{escape(inv.ref)}/receipt">
<label>VAT DUE &nbsp;<input type="text" name="vat" placeholder="0.00" style="width:140px"></label>
<button type="submit">RAISE TAX RECEIPT</button>
</form>
</div>"""
    else:
        tax_panel = """
<div class="panel">
<h3>3. Tax receipt</h3>
<p class="hint">Available once the invoice is approved for payment. An invoice on
hold has no agreed value, so no receipt can be raised against it.</p>
</div>"""

    body = f"""
{banner}
<h2>VENDOR INVOICE {escape(inv.ref)}</h2>
<dl class="kv">
<dt>VENDOR</dt><dd>{escape(inv.vendor_name)} ({escape(inv.vendor_code)})</dd>
<dt>INVOICE DATE</dt><dd>{escape(inv.doc_date)}</dd>
<dt>NET TOTAL</dt><dd>{_money(inv.total)}</dd>
{claimed}
<dt>STATUS</dt><dd><span class="tag {cls}">{escape(state)}</span></dd>
{reason}
</dl>
{_lines_table_taxed(inv.lines)}
<div class="panel">
<strong>SUPPORTING DOCUMENTS</strong><br>
<a href="/po/{escape(link['po'])}">PURCHASE ORDER {escape(link['po'])}</a> &nbsp;&middot;&nbsp;
<a href="/reception/{escape(link['reception'])}">GOODS RECEIPT {escape(link['reception'])}</a>
</div>
{summary_panel}
<div class="panel">
<h3>2. Disposition</h3>
<form method="post" action="/invoice/{escape(inv.ref)}/dispose">
<input type="hidden" name="disposition" value="APPROVED">
<button type="submit">APPROVE FOR PAYMENT</button>
</form>
<form method="post" action="/invoice/{escape(inv.ref)}/dispose">
<input type="hidden" name="disposition" value="HELD">
<select name="reason">{opts}</select>
<button type="submit" class="hold">PLACE ON HOLD</button>
</form>
</div>
{tax_panel}"""
    return _page(f"Invoice {ref}", body, CRUMB)


def screen_receipt(portal: Portal, receipt_ref: str) -> bytes | None:
    """A raised tax receipt, as its own document."""
    inv = next(
        (i for i in portal.data.invoices.values() if i.tax_receipt_ref == receipt_ref),
        None,
    )
    if inv is None:
        return None

    # Bands are recomputed for DISPLAY from the same line data the agent saw.
    # This is presentation, not grading: the oracle computes the correct figure
    # independently, and what is shown here is the agent's own declaration
    # beside the arithmetic anyone can check by hand.
    bands: dict[str, dict] = {}
    for line in inv.lines:
        b = bands.setdefault(line.tax_code, {"net": 0.0, "rate": line.tax_rate})
        b["net"] = round(b["net"] + line.extended, 2)

    rows = "".join(
        f"<tr><td>{escape(code)} - {escape(TAX_CODES[code][0])}</td>"
        f'<td class="num">{b["rate"]:g}%</td>'
        f'<td class="num">{_money(b["net"])}</td></tr>'
        for code, b in sorted(bands.items())
    )
    body = f"""
<h2>TAX RECEIPT {escape(receipt_ref)}</h2>
<dl class="kv">
<dt>AGAINST INVOICE</dt><dd><a href="/invoice/{escape(inv.ref)}">{escape(inv.ref)}</a></dd>
<dt>VENDOR</dt><dd>{escape(inv.vendor_name)} ({escape(inv.vendor_code)})</dd>
<dt>ISSUED BY</dt><dd>{escape(OPERATOR_ID)}</dd>
</dl>
<h3>Net by tax band</h3>
<table><tr><th>BAND</th><th class="num">RATE</th><th class="num">NET</th></tr>{rows}
<tr class="total-row"><td>NET TOTAL</td><td></td>
    <td class="num">{_money(inv.total)}</td></tr></table>
<dl class="kv">
<dt>VAT DECLARED</dt><dd>{_money(inv.declared_vat or 0)}</dd>
<dt>GROSS PAYABLE</dt><dd>{_money(round(inv.total + (inv.declared_vat or 0), 2))}</dd>
</dl>"""
    return _page(f"Tax receipt {receipt_ref}", body, CRUMB)


def screen_document(portal: Portal, kind: str, ref: str) -> bytes | None:
    store = portal.data.purchase_orders if kind == "po" else portal.data.receptions
    doc = store.get(ref)
    if doc is None:
        return None
    heading = "PURCHASE ORDER" if kind == "po" else "GOODS RECEIPT"
    qty_label = "QTY ORDERED" if kind == "po" else "QTY RECEIVED"
    total_row = (
        f"<dt>ORDER TOTAL</dt><dd>{_money(doc.total)}</dd>" if kind == "po" else ""
    )
    body = f"""
<h2>{heading} {escape(doc.ref)}</h2>
<dl class="kv">
<dt>VENDOR</dt><dd>{escape(doc.vendor_name)} ({escape(doc.vendor_code)})</dd>
<dt>DOCUMENT DATE</dt><dd>{escape(doc.doc_date)}</dd>
<dt>STATUS</dt><dd>{escape(doc.status)}</dd>
{total_row}
</dl>
{_lines_table(doc.lines, qty_label=qty_label)}"""
    return _page(f"{heading} {ref}", body, CRUMB)


def screen_exceptions(portal: Portal) -> bytes:
    held = [i for i in portal.data.invoices.values() if i.disposition == "HELD"]
    if held:
        rows = "".join(
            f"<tr><td><a href='/invoice/{escape(i.ref)}'>{escape(i.ref)}</a></td>"
            f"<td>{escape(i.vendor_name)}</td><td class='num'>{_money(i.total)}</td>"
            f"<td>{escape(i.hold_reason or '')}</td></tr>"
            for i in held
        )
        table = (
            "<table><tr><th>INVOICE</th><th>VENDOR</th><th class='num'>TOTAL</th>"
            f"<th>REASON</th></tr>{rows}</table>"
        )
    else:
        table = "<p>NO INVOICES ON HOLD.</p>"
    body = f"<h2>EXCEPTION QUEUE</h2><p>RECORDS: {len(held)}</p>{table}"
    return _page("Exception queue", body, CRUMB)


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #


class Handler(BaseHTTPRequestHandler):
    portal: Portal  # injected by serve()

    server_version = "DoliDesk/1.0"

    def log_message(self, fmt, *args):  # noqa: A003 - quiet by default
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # -- helpers ---------------------------------------------------------- #

    def _send(self, body: bytes, status: int = 200, ctype: str = "text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # No caching: an agent re-reading a screen must see current state, and a
        # cached worklist after a disposition would look like a failed write.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200):
        self._send(json.dumps(obj, indent=2).encode(), status, "application/json")

    def _redirect(self, to: str):
        self.send_response(303)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _not_found(self, what: str):
        self._send(_page("Not found", f"<h2>NOT FOUND</h2><p>{escape(what)}</p>", CRUMB), 404)

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw).items()}

    # -- routes ----------------------------------------------------------- #

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        p = self.portal

        if path == "/":
            return self._send(screen_signon())
        if path == "/menu":
            return self._send(screen_menu())
        if path == "/worklist":
            return self._send(screen_worklist(p))
        if path == "/exceptions":
            return self._send(screen_exceptions(p))
        if path == "/_state":
            return self._json(p.state())

        parts = path.strip("/").split("/")
        if len(parts) == 2 and parts[0] in ("invoice", "po", "reception", "receipt"):
            ref = parts[1]
            if parts[0] == "invoice":
                page = screen_invoice(p, ref)
            elif parts[0] == "receipt":
                page = screen_receipt(p, ref)
            else:
                page = screen_document(p, parts[0], ref)
            return self._send(page) if page else self._not_found(f"{parts[0]} {ref}")

        return self._not_found(path)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        p = self.portal

        if path == "/signon":
            return self._redirect("/menu")

        if path == "/_reset":
            p.reset(limit=p.limit)
            return self._json({"reset": True})

        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "invoice":
            ref, verb = parts[1], parts[2]
            handlers = {
                "dispose": lambda f: p.dispose(
                    ref, f.get("disposition", ""), f.get("reason") or None
                ),
                "summary": lambda f: p.summarise(ref, f.get("summary", "")),
                "receipt": lambda f: p.raise_tax_receipt(ref, f.get("vat", "")),
            }
            handler = handlers.get(verb)
            if handler is not None:
                ok, message = handler(self._form())
                # A refusal is rendered on the invoice screen as an error, not
                # as a bare 400 body: the agent's next screenshot is how it
                # learns what went wrong, so the reason has to be visible on
                # the page it is already looking at.
                page = screen_invoice(
                    p, ref, message if ok else "", "" if ok else message
                )
                return self._send(page, 200 if ok else 400) if page else self._not_found(ref)

        return self._not_found(path)


def serve(host: str = "127.0.0.1", port: int = 8900, *, seed: int | None = None,
          limit: int | None = None, verbose: bool = False) -> ThreadingHTTPServer:
    """Start the portal. Returns the server; caller owns shutdown.

    Binds to loopback by default. Exposing this to a non-loopback interface is
    an egress decision the operator has to make deliberately, so it is a caller
    argument rather than a default.
    """
    portal = Portal(seed, limit=limit)
    handler = type("BoundHandler", (Handler,), {"portal": portal})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.verbose = verbose
    return httpd


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Serve the DoliDesk terminal.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8900)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    httpd = serve(args.host, args.port, seed=args.seed, verbose=args.verbose)
    url = f"http://{args.host}:{args.port}/"
    print(f"{TITLE} {SUBTITLE}")
    print(f"  serving  {url}")
    print(f"  oracle   {url}_state")
    print("  Ctrl-C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
