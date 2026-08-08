"""Deterministic seed data for the DoliDesk accounts-payable terminal.

The whole testcase rests on one property: **ground truth is derived, never
stored.** This module generates purchase orders, goods receipts and vendor
invoices that disagree with each other in specific, planted ways. It does NOT
record what the right answer is. The oracle recomputes verdicts from these same
records, and the agent reads them off rendered screens. Two independent paths
to the same conclusion, with no answer key in between.

That is the difference between this and the reference catalog, where each
`automation.json` carries an `answer` string the demo is checked against. An
answer key tests whether the agent reproduces a constant. Deriving the verdict
tests whether it did the arithmetic.

Determinism comes from a fixed seed, so every run of every clone produces the
same ledger. Change SEED and every downstream expectation changes with it --
which is the point: the oracle follows the data, not a hardcoded list.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterator

SEED = 20260808

# Match tolerances. An invoice inside BOTH bands is payable without review.
#
# These are deliberately generous enough that floating-point noise cannot flip a
# verdict, and tight enough that the planted variances land clearly outside.
# A tolerance a rounding error can cross is a flaky test wearing a business rule
# as a disguise.
PRICE_TOLERANCE_PCT = 2.0
PRICE_TOLERANCE_ABS = 0.50
QTY_TOLERANCE = 0  # billing for more than was received is never acceptable

# VAT treatment by item class.
#
# Rates are per goods category, which is what makes the tax assessment a real
# second task rather than one multiplication: the agent has to read the tax code
# on each LINE and total the bands separately, so a two-line invoice spanning
# two bands cannot be settled by applying one rate to the invoice total.
#
# The vendor's own claimed VAT total is printed on the invoice and is sometimes
# WRONG -- that is the point. The agent must recompute rather than transcribe.
TAX_CODES = {
    "S": ("STANDARD", 20.0),
    "R": ("REDUCED", 5.0),
    "Z": ("ZERO", 0.0),
}

# Which band each stock item falls in. Electrical goods standard-rated,
# safety-critical parts reduced, packaging consumables zero -- a plausible
# spread that guarantees multi-band invoices.
ITEM_TAX_CODE = {
    "EL-3388": "S", "EL-2210": "S", "EL-9001": "S",
    "MC-7741": "R", "MC-7802": "R", "MC-3320": "R",
    "PK-1150": "Z", "PK-1188": "Z",
}

# Rounding the tax authority requires: half-up per band, to the penny.
# Python's round() is banker's rounding (round-half-to-EVEN), so round(2.675, 2)
# is 2.67 and round(0.125, 2) is 0.12. On a tax figure that is not a rounding
# preference, it is a wrong number -- and it disagrees with what any finance
# team, and the agent reading the screen, would compute by hand.
def tax_round(value: float) -> float:
    from decimal import Decimal, ROUND_HALF_UP

    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

VENDORS = [
    ("V-1001", "NORTHGATE SUPPLY CO", "NET 30", "GB2847 1122 33"),
    ("V-1002", "MERIDIAN INDUSTRIAL LTD", "NET 45", "GB9930 4471 08"),
    ("V-1003", "CALDWELL FASTENERS", "NET 30", "GB1174 8823 51"),
    ("V-1004", "ATLAS PACKAGING GROUP", "NET 60", "GB6621 0094 77"),
    ("V-1005", "PENNINE ELECTRICAL", "NET 30", ""),  # missing VAT number, on purpose
]

ITEMS = [
    ("EL-3388", "USB-C DOCK 7-PORT", 129.95),
    ("EL-2210", "CAT6A PATCH LEAD 3M", 8.40),
    ("MC-7741", "HEX BOLT M10x50 (100)", 42.10),
    ("MC-7802", "LOCK WASHER M10 (500)", 18.75),
    ("PK-1150", "CORRUGATED CARTON 400MM", 1.32),
    ("PK-1188", "PALLET WRAP 500MM", 24.60),
    ("EL-9001", "RCD CONSUMER UNIT 10-WAY", 187.00),
    ("MC-3320", "STAINLESS SOCKET SET", 96.25),
]

# The planted defects, one per invoice position. `None` means a clean match.
#
# Spelled out as a literal table rather than generated at random because the
# SHAPE of the test matters: a reviewer should be able to see at a glance that
# clean invoices outnumber exceptions, that every exception type is represented,
# and that two invoices carry more than one fault at once. Random generation
# would leave that distribution to chance and could produce a run with no clean
# invoices at all, which would silently stop testing the approve path.
DEFECTS = [
    None,
    "price_over",
    None,
    "qty_over",
    None,
    "price_under",
    "qty_over",
    None,
    "both",
    None,
    "price_over",
    "both",
]

# Invoice positions where the VENDOR's printed VAT figure is wrong, and by how
# much. Independent of DEFECTS on purpose: a correctly-matched invoice can still
# carry bad tax arithmetic, and an exception invoice can have its VAT right.
# Coupling them would let an agent infer one from the other instead of checking.
#
# The amounts are large enough to be unmistakable on screen -- a 3p discrepancy
# would test the agent's eyesight rather than its arithmetic.
VAT_ERRORS = {
    2: 14.60,    # overstated
    5: -8.25,    # understated
    9: 31.40,    # overstated, on an invoice that also fails the match
}


@dataclass(frozen=True)
class Line:
    item: str
    description: str
    qty: float
    unit_price: float

    @property
    def extended(self) -> float:
        return round(self.qty * self.unit_price, 2)

    @property
    def tax_code(self) -> str:
        """The VAT band for this item. Derived from the item, never stored.

        Keeping this a lookup rather than a field means a line cannot carry a
        tax code that disagrees with its own item number -- which would be an
        inconsistency the agent could not resolve from the screen, and so an
        unfair test rather than a hard one.
        """
        return ITEM_TAX_CODE.get(self.item, "S")

    @property
    def tax_rate(self) -> float:
        return TAX_CODES[self.tax_code][1]

    @property
    def tax_amount(self) -> float:
        return tax_round(self.extended * self.tax_rate / 100)


@dataclass
class Document:
    ref: str
    kind: str  # 'po' | 'reception' | 'invoice'
    vendor_code: str
    vendor_name: str
    doc_date: str
    lines: list[Line] = field(default_factory=list)
    status: str = "OPEN"
    # Mutable workflow state. The agent changes these; the oracle checks them.
    disposition: str | None = None  # None | 'APPROVED' | 'HELD'
    hold_reason: str | None = None
    touched_by: str | None = None
    # What the VENDOR claims the VAT is. Printed on the invoice, and sometimes
    # wrong -- the agent must recompute from the lines rather than transcribe.
    claimed_vat: float | None = None
    # Set when a tax receipt has been raised against this invoice.
    tax_receipt_ref: str | None = None
    # The VAT figure the agent DECLARED on that receipt, which is the number
    # the oracle grades. Kept separate from `claimed_vat` so a transcription of
    # the vendor's wrong figure is visibly distinct from an independent
    # calculation that happens to agree with it.
    declared_vat: float | None = None
    # The agent's own written summary of the invoice, recorded before it rules.
    summary: str | None = None

    @property
    def total(self) -> float:
        return round(sum(line.extended for line in self.lines), 2)

    @property
    def net_total(self) -> float:
        """Alias for total. Named for the tax screens, where 'net' is the term."""
        return self.total


@dataclass
class Dataset:
    vendors: dict[str, dict]
    purchase_orders: dict[str, Document]
    receptions: dict[str, Document]
    invoices: dict[str, Document]
    links: dict[str, dict[str, str]]  # invoice_ref -> {po, reception}

    def invoice_list(self) -> list[Document]:
        """Worklist order. Deliberately NOT sorted by anything useful.

        The reference catalog leans on this trick and it is a good one: a list
        already ordered by the answer lets an agent succeed by reading row one.
        Scrambling the order forces it to actually open every document, which is
        both a harder test and the reason the step count reaches fifty.
        """
        return [self.invoices[r] for r in sorted(self.invoices, key=lambda r: self.invoices[r].ref)]


def _money(x: float) -> float:
    return round(x + 1e-9, 2)


def _variance_delta(agreed_price: float, rng: random.Random) -> float:
    """A price movement guaranteed to breach BOTH tolerance bands.

    The matching rule ignores a variance unless it exceeds the percentage band
    AND the absolute de-minimis floor -- which is correct, because no AP team
    holds an invoice over twenty pence. But it means a defect planted as a bare
    percentage is invisible on a cheap line: 17.6% of a 1.25 carton is 22p, so
    the rule correctly declines to flag it and the invoice silently stops
    testing anything.

    Deriving the delta from both thresholds keeps the planted fault detectable
    at every price point. The multipliers give clear headroom rather than
    sitting on the boundary, because a defect that only just breaches a band is
    a test that fails the day someone re-tunes the tolerance.
    """
    by_pct = agreed_price * (PRICE_TOLERANCE_PCT / 100) * rng.uniform(3.0, 9.0)
    by_abs = PRICE_TOLERANCE_ABS * rng.uniform(1.5, 3.0)
    return _money(max(by_pct, by_abs))


def build(seed: int = SEED) -> Dataset:
    """Generate the full dataset. Same seed in, same bytes out."""
    rng = random.Random(seed)
    start = date(2026, 6, 1)

    vendors = {
        code: {"code": code, "name": name, "terms": terms, "vat": vat}
        for code, name, terms, vat in VENDORS
    }

    purchase_orders: dict[str, Document] = {}
    receptions: dict[str, Document] = {}
    invoices: dict[str, Document] = {}
    links: dict[str, dict[str, str]] = {}

    for n, defect in enumerate(DEFECTS, start=1):
        vendor_code, vendor_name, _, _ = VENDORS[(n - 1) % len(VENDORS)]
        po_ref = f"PO-4{400 + n}"
        rc_ref = f"RC-2{200 + n}"
        in_ref = f"FA-25{80 + n}"

        po_date = start + timedelta(days=rng.randint(0, 20))
        rc_date = po_date + timedelta(days=rng.randint(2, 9))
        in_date = rc_date + timedelta(days=rng.randint(1, 6))

        # 2-3 lines per document, drawn without replacement so an item cannot
        # appear twice on one order (which would make line matching ambiguous).
        chosen = rng.sample(ITEMS, rng.randint(2, 3))

        po_lines: list[Line] = []
        rc_lines: list[Line] = []
        in_lines: list[Line] = []

        for idx, (code, desc, list_price) in enumerate(chosen):
            qty_ordered = float(rng.choice([10, 20, 25, 40, 50, 100]))
            agreed_price = _money(list_price * rng.uniform(0.92, 1.0))

            qty_received = qty_ordered
            qty_billed = qty_ordered
            billed_price = agreed_price

            # Plant the defect on the FIRST line only. Concentrating it keeps the
            # expected verdict unambiguous: if two lines each drifted a little,
            # "which line is wrong" stops having one answer, and a reviewer
            # cannot tell a real disagreement from an arithmetic slip.
            if idx == 0 and defect:
                if defect in ("price_over", "both"):
                    billed_price = _money(agreed_price + _variance_delta(agreed_price, rng))
                if defect == "price_under":
                    # Floor at 1p: a non-positive unit price is not a variance,
                    # it is a different bug, and it would not exercise this rule.
                    billed_price = max(0.01, _money(agreed_price - _variance_delta(agreed_price, rng)))
                if defect in ("qty_over", "both"):
                    # Cap the shortfall so a receipt never lands at zero. A
                    # reception document recording nothing received is a
                    # different exception class (goods never arrived) and would
                    # muddy what this invoice is meant to test.
                    shortfall = min(rng.choice([2, 5, 10]), max(1, int(qty_ordered * 0.4)))
                    qty_received = qty_ordered - shortfall
                    qty_billed = qty_ordered  # billed for the full order, received less

            po_lines.append(Line(code, desc, qty_ordered, agreed_price))
            rc_lines.append(Line(code, desc, qty_received, agreed_price))
            in_lines.append(Line(code, desc, qty_billed, billed_price))

        purchase_orders[po_ref] = Document(
            ref=po_ref, kind="po", vendor_code=vendor_code, vendor_name=vendor_name,
            doc_date=po_date.isoformat(), lines=po_lines, status="CONFIRMED",
        )
        receptions[rc_ref] = Document(
            ref=rc_ref, kind="reception", vendor_code=vendor_code, vendor_name=vendor_name,
            doc_date=rc_date.isoformat(), lines=rc_lines, status="RECEIVED",
        )
        invoice = Document(
            ref=in_ref, kind="invoice", vendor_code=vendor_code, vendor_name=vendor_name,
            doc_date=in_date.isoformat(), lines=in_lines, status="AWAITING MATCH",
        )

        # What the vendor claims the VAT is, printed on the invoice.
        #
        # Computed from the invoice's OWN lines, so a correct claim is genuinely
        # correct rather than coincidentally so. On the positions listed in
        # VAT_ERRORS the claim is then shifted by a visible amount, which is the
        # tax equivalent of a planted match defect: the agent has to recompute to
        # notice, and transcribing the printed figure scores as wrong.
        true_vat = _true_vat(in_lines)
        error = VAT_ERRORS.get(n)
        invoice.claimed_vat = tax_round(true_vat + error) if error else true_vat

        invoices[in_ref] = invoice
        links[in_ref] = {"po": po_ref, "reception": rc_ref}

    return Dataset(vendors, purchase_orders, receptions, invoices, links)


def _true_vat(lines: list[Line]) -> float:
    """VAT on a set of lines, banded then summed.

    Duplicated deliberately rather than importing `assess_tax`: the seed must
    not depend on the oracle, or a bug in the oracle would silently produce seed
    data that agrees with it and the two would validate each other into a wrong
    answer. They compute the same thing by the same rule, independently, and a
    test asserts they agree.
    """
    bands: dict[str, float] = {}
    for line in lines:
        bands[line.tax_code] = round(bands.get(line.tax_code, 0.0) + line.extended, 2)
    return tax_round(
        sum(tax_round(net * TAX_CODES[code][1] / 100) for code, net in bands.items())
    )


# ---------------------------------------------------------------------------
# The matching rules.
#
# These live here, beside the data, and are imported by the oracle. They are
# NOT imported by anything that renders a screen: the agent has to derive the
# same conclusions from pixels. If a renderer ever imports this function, the
# test has quietly started grading the agent on a number it was shown.
# ---------------------------------------------------------------------------


def match_invoice(data: Dataset, invoice_ref: str) -> dict:
    """Three-way match one invoice against its purchase order and receipt.

    Returns the verdict plus the per-line evidence behind it, so a disagreement
    with the agent can be traced to a specific line rather than argued about in
    the abstract.
    """
    inv = data.invoices[invoice_ref]
    link = data.links[invoice_ref]
    po = data.purchase_orders[link["po"]]
    rc = data.receptions[link["reception"]]

    po_by_item = {line.item: line for line in po.lines}
    rc_by_item = {line.item: line for line in rc.lines}

    findings: list[dict] = []
    for line in inv.lines:
        po_line = po_by_item.get(line.item)
        rc_line = rc_by_item.get(line.item)
        if po_line is None:
            findings.append({"item": line.item, "code": "NOT_ON_PO", "detail": "billed item is not on the order"})
            continue

        qty_received = rc_line.qty if rc_line else 0.0
        if line.qty - qty_received > QTY_TOLERANCE:
            findings.append({
                "item": line.item,
                "code": "QTY_OVER_RECEIPT",
                "detail": f"billed {line.qty:g}, received {qty_received:g}",
                "billed": line.qty,
                "received": qty_received,
            })

        delta = line.unit_price - po_line.unit_price
        pct = (abs(delta) / po_line.unit_price * 100) if po_line.unit_price else 0.0
        if abs(delta) > PRICE_TOLERANCE_ABS and pct > PRICE_TOLERANCE_PCT:
            findings.append({
                "item": line.item,
                "code": "PRICE_OVER_PO" if delta > 0 else "PRICE_UNDER_PO",
                "detail": f"billed {line.unit_price:.2f}, ordered {po_line.unit_price:.2f} ({pct:.1f}%)",
                "billed": line.unit_price,
                "ordered": po_line.unit_price,
                "pct": round(pct, 2),
            })

    return {
        "invoice": invoice_ref,
        "po": po.ref,
        "reception": rc.ref,
        "vendor": inv.vendor_name,
        "invoice_total": inv.total,
        "po_total": po.total,
        "findings": findings,
        # The expected disposition, which is what the agent's action is graded
        # against -- derived here, never stored in the dataset.
        "expected_disposition": "APPROVED" if not findings else "HELD",
        "expected_reason": None if not findings else findings[0]["code"],
    }


def match_all(data: Dataset) -> Iterator[dict]:
    for ref in sorted(data.invoices):
        yield match_invoice(data, ref)


# ---------------------------------------------------------------------------
# VAT assessment.
#
# The second half of the workflow, and a genuinely different kind of task from
# the match: three-way matching is a COMPARISON across documents, while the tax
# assessment is a CALCULATION within one. An agent can be good at one and bad at
# the other, which is exactly why both are here.
#
# Like the match rules, this lives beside the data and is imported by the oracle
# only. Nothing that renders a screen may import it, or the agent would be shown
# the answer it is being asked to compute.
# ---------------------------------------------------------------------------


def assess_tax(data: Dataset, invoice_ref: str) -> dict:
    """Compute the VAT due on an invoice, banded by tax code.

    Returns the per-band breakdown as well as the total, because a wrong total
    with the right bands is a different mistake from a wrong total with the
    wrong bands -- the first is arithmetic, the second is misreading the tax
    code column, and a scorer that cannot tell them apart teaches nothing.
    """
    inv = data.invoices[invoice_ref]

    bands: dict[str, dict] = {}
    for line in inv.lines:
        band = bands.setdefault(
            line.tax_code,
            {"code": line.tax_code, "label": TAX_CODES[line.tax_code][0],
             "rate": line.tax_rate, "net": 0.0, "vat": 0.0},
        )
        band["net"] = round(band["net"] + line.extended, 2)

    # VAT is computed on each band's NET TOTAL, not summed from per-line
    # roundings. Those differ by a penny or two on multi-line bands, and the
    # band total is what a tax authority expects -- so rounding per line then
    # summing produces a figure that is defensible-looking and wrong.
    for band in bands.values():
        band["vat"] = tax_round(band["net"] * band["rate"] / 100)

    ordered = [bands[c] for c in ("S", "R", "Z") if c in bands]
    vat_total = tax_round(sum(b["vat"] for b in ordered))
    net_total = inv.net_total

    claimed = inv.claimed_vat
    discrepancy = None if claimed is None else tax_round(claimed - vat_total)

    return {
        "invoice": invoice_ref,
        "bands": ordered,
        "net_total": net_total,
        "vat_total": vat_total,
        "gross_total": tax_round(net_total + vat_total),
        "claimed_vat": claimed,
        "vat_discrepancy": discrepancy,
        # A vendor's arithmetic is wrong often enough that this is a real
        # control, not a contrived one.
        "vendor_vat_correct": discrepancy == 0 if claimed is not None else None,
    }


def assess_all(data: Dataset) -> Iterator[dict]:
    for ref in sorted(data.invoices):
        yield assess_tax(data, ref)
