"""The oracle: independent ground truth, and the verdict diff.

The agent reads pixels. The oracle reads the portal's JSON state route. Same
underlying records, two paths that never touch, and the diff between them is
the test result.

Two things it deliberately does not do:

* **It does not read an answer key.** The reference catalog stores the expected
  answer as a string in `automation.json` and checks the agent reproduced it.
  That grades recall of a constant. Here the expected disposition is recomputed
  from the purchase orders and receipts every run, so changing the seed changes
  the truth and nothing needs updating by hand.

* **It does not accept the agent's own summary as evidence of a write.** What
  the agent reports and what actually landed in the system are different
  claims. Disposition is read back from the portal's state, so an agent that
  says it held an invoice but never submitted the form is scored as wrong --
  which is the failure mode that matters most in a financial control.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ap_desk.domain import Dataset, assess_tax, match_invoice


@dataclass
class InvoiceScore:
    invoice: str
    expected: str
    actual: str | None
    expected_reason: str | None
    actual_reason: str | None
    correct: bool
    reason_correct: bool | None  # None when no reason was required
    findings: list[dict] = field(default_factory=list)
    # Tax assessment, graded only where a receipt was required.
    expected_vat: float | None = None
    declared_vat: float | None = None
    vat_correct: bool | None = None  # None when no receipt was due
    receipt_ref: str | None = None
    # The agent's written summary.
    summary: str | None = None
    summary_grounded: bool | None = None
    # The agent's judgement on the vendor's printed VAT, and whether it was right.
    vat_verdict: str | None = None
    # Whether the tax stage was switched on for this run. Without it, an
    # invoice would be reported as missing a receipt the terminal never offered.
    tax_graded: bool = True

    @property
    def status(self) -> str:
        if self.actual is None:
            return "NOT ACTIONED"
        if not self.correct:
            return "WRONG DISPOSITION"
        if self.reason_correct is False:
            return "RIGHT HOLD, WRONG REASON"
        if self.vat_correct is False:
            return "RIGHT CALL, WRONG VAT"
        if self.tax_graded and self.vat_correct is None and self.expected == "APPROVED":
            return "APPROVED, NO VAT CHECK"
        if self.summary_grounded is False:
            return "CORRECT, UNGROUNDED SUMMARY"
        return "CORRECT"


@dataclass
class Report:
    scores: list[InvoiceScore]
    grade_tax: bool = True

    @property
    def total(self) -> int:
        return len(self.scores)

    @property
    def actioned(self) -> int:
        return sum(1 for s in self.scores if s.actual is not None)

    @property
    def correct(self) -> int:
        return sum(1 for s in self.scores if s.correct)

    @property
    def fully_correct(self) -> int:
        """Every graded dimension right on this invoice.

        This is the headline number, and it is deliberately the strictest one:
        the right disposition, for the right stated reason, with the right VAT
        on the receipt where one was due, and a summary grounded in the invoice.
        A partial success on a financial control is not a success.
        """
        return sum(
            1 for s in self.scores
            if s.correct
            and s.reason_correct is not False
            and s.vat_correct is not False
            and s.summary_grounded is not False
            # An approved invoice with no receipt raised is unfinished work,
            # not a pass: the tax liability was never recorded.
            and not (self.grade_tax and s.expected == "APPROVED"
                     and s.correct and s.vat_correct is None)
        )

    @property
    def summaries_written(self) -> int:
        return sum(1 for s in self.scores if s.summary)

    @property
    def summaries_grounded(self) -> int:
        return sum(1 for s in self.scores if s.summary_grounded)

    @property
    def receipts_raised(self) -> int:
        return sum(1 for s in self.scores if s.receipt_ref or s.vat_verdict)

    @property
    def vat_correct_count(self) -> int:
        return sum(1 for s in self.scores if s.vat_correct)

    @property
    def vat_due_count(self) -> int:
        """Invoices where a receipt was genuinely required."""
        return sum(1 for s in self.scores if s.expected == "APPROVED")

    @property
    def accuracy(self) -> float:
        return self.fully_correct / self.total if self.total else 0.0

    @property
    def passed(self) -> bool:
        """Every invoice actioned, every graded dimension right.

        Deliberately absolute. A partial credit threshold on a financial control
        invites tuning the threshold until the run passes, which is how a test
        stops measuring anything.
        """
        return self.total > 0 and self.fully_correct == self.total

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "actioned": self.actioned,
            "correct_disposition": self.correct,
            "fully_correct": self.fully_correct,
            "summaries_written": self.summaries_written,
            "summaries_grounded": self.summaries_grounded,
            "receipts_raised": self.receipts_raised,
            "vat_correct": self.vat_correct_count,
            "vat_due": self.vat_due_count,
            "accuracy": round(self.accuracy, 4),
            "passed": self.passed,
            "invoices": [
                {
                    "invoice": s.invoice,
                    "expected": s.expected,
                    "actual": s.actual,
                    "expected_reason": s.expected_reason,
                    "actual_reason": s.actual_reason,
                    "expected_vat": s.expected_vat,
                    "declared_vat": s.declared_vat,
                    "vat_correct": s.vat_correct,
                    "receipt_ref": s.receipt_ref,
                    "vat_verdict": s.vat_verdict,
                    "summary": s.summary,
                    "summary_grounded": s.summary_grounded,
                    "status": s.status,
                    "findings": s.findings,
                }
                for s in self.scores
            ],
        }


def fetch_state(portal_url: str, *, timeout: float = 10.0) -> dict:
    """Read the portal's out-of-band state route."""
    url = portal_url.rstrip("/") + "/_state"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def summary_is_grounded(summary: str | None, invoice) -> bool | None:
    """Did the agent's summary actually come from this invoice?

    Grading free prose is not something a test should pretend to do -- there is
    no single right summary, and scoring style would measure the wrong thing.
    What CAN be checked is grounding: a summary written from the screen will
    name something only that screen showed. A summary that names nothing
    specific is filler, and filler is how an agent passes a "did you write
    something" check without reading anything.

    So the bar is deliberately low and objective: mention the vendor, or at
    least one item code or a distinctive word from this invoice's own line
    descriptions. Returns None when no summary was recorded, since absent and
    ungrounded are different failures.

    Matching is on WHOLE WORDS, not substrings. That is not fussiness: a
    substring test scored "The document was examined against supporting
    records" as grounded, because the item "USB-C DOCK 7-PORT" contributes the
    token "port", which hides inside "supporting". Generic filler passing the
    grounding check defeats the only thing this function is for.
    """
    if not summary:
        return None

    text = summary.lower()
    # Split on anything that is not a letter or digit, so "usb-c" and "7-port"
    # become separate tokens and punctuation never welds two words together.
    tokens = set(re.findall(r"[a-z0-9]+", text))

    # Item codes are distinctive enough to match as a unit: "el-3388" tokenises
    # to {"el", "3388"}, and the numeric part alone is a strong signal.
    for line in invoice.lines:
        if line.item.lower() in text:
            return True
        if set(re.findall(r"[a-z0-9]+", line.item.lower())) <= tokens:
            return True

    if invoice.vendor_name:
        vendor_word = invoice.vendor_name.split()[0].lower()
        if vendor_word in tokens:
            return True

    # Words from the goods descriptions. Four characters or more, alphabetic,
    # and not a word so common that any sentence about an invoice would contain
    # it -- otherwise the check passes on vocabulary rather than on reading.
    for line in invoice.lines:
        for word in re.findall(r"[a-z]+", line.description.lower()):
            if len(word) >= 4 and word not in _GENERIC_WORDS and word in tokens:
                return True
    return False


# Words that appear in goods descriptions but are too generic to prove the
# agent read anything. Kept short and explicit rather than inferred: a
# stop-word list that grows by guesswork stops being auditable.
_GENERIC_WORDS = frozenset({
    "port", "unit", "units", "part", "parts", "item", "items",
    "pack", "set", "type", "size", "line", "case", "box",
})


def score(data: Dataset, state: dict, *, grade_summary: bool = True,
          grade_tax: bool = True) -> Report:
    """Diff what the agent did against what the records say it should have done.

    `grade_summary` and `grade_tax` follow the portal's stage switches. Grading
    a stage the terminal never offered would mark every invoice down for work
    it was never asked to do -- the scorer has to measure the workflow that
    actually ran, not the largest one this repo can express.
    """
    scores: list[InvoiceScore] = []
    observed = state.get("invoices", {})

    for ref in sorted(data.invoices):
        truth = match_invoice(data, ref)
        tax = assess_tax(data, ref)
        actual = observed.get(ref, {})
        actual_disp = actual.get("disposition")
        actual_reason = actual.get("hold_reason")
        expected_disp = truth["expected_disposition"]
        expected_reason = truth["expected_reason"]

        correct = actual_disp == expected_disp

        # A reason is only gradable on a hold that was correctly identified.
        # Scoring the reason on an invoice that should have been approved would
        # double-penalise a single mistake and make the accuracy number mean
        # something other than what it says.
        if correct and expected_disp == "HELD":
            reason_correct = actual_reason == expected_reason
        else:
            reason_correct = None

        # VAT is only gradable where a receipt was actually due -- that is, on
        # an invoice the agent correctly approved. Grading it on a hold would
        # penalise the agent for correctly declining to raise one.
        declared = actual.get("declared_vat")
        verdict = actual.get("vat_verdict")
        if not grade_tax:
            vat_correct = None
        elif correct and expected_disp == "APPROVED":
            # Two ways to have answered, graded the same way. The verdict form
            # ("does the vendor's figure agree?") and the typed form ("what is
            # the VAT?") both require the same arithmetic; only the input
            # differs, so both are checked against the recomputed total.
            if verdict is not None:
                should_agree = tax["vendor_vat_correct"]
                vat_correct = (verdict == "AGREES") == bool(should_agree)
            elif declared is not None:
                vat_correct = abs(declared - tax["vat_total"]) < 0.005
            else:
                vat_correct = None
        else:
            vat_correct = None

        summary = actual.get("summary")
        grounded = (
            summary_is_grounded(summary, data.invoices[ref]) if grade_summary else None
        )
        scores.append(
            InvoiceScore(
                invoice=ref,
                expected=expected_disp,
                actual=actual_disp,
                expected_reason=expected_reason,
                actual_reason=actual_reason,
                correct=correct,
                reason_correct=reason_correct,
                findings=truth["findings"],
                expected_vat=tax["vat_total"],
                declared_vat=declared,
                vat_correct=vat_correct,
                receipt_ref=actual.get("tax_receipt_ref"),
                summary=summary,
                summary_grounded=grounded,
                tax_graded=grade_tax,
                vat_verdict=verdict,
            )
        )
    return Report(scores, grade_tax=grade_tax)


def render(report: Report) -> str:
    """A console table an operator can read at a glance."""
    lines = [
        "",
        f"  {'INVOICE':<10} {'EXPECTED':<9} {'ACTUAL':<9} {'VAT DUE':>9} "
        f"{'DECLARED':>9}  RESULT",
        f"  {'-' * 10} {'-' * 9} {'-' * 9} {'-' * 9} {'-' * 9}  {'-' * 30}",
    ]
    for s in report.scores:
        actual = s.actual or "-"
        vat_due = f"{s.expected_vat:.2f}" if s.expected_vat is not None else "-"
        declared = f"{s.declared_vat:.2f}" if s.declared_vat is not None else "-"
        detail = ""
        if s.status == "RIGHT HOLD, WRONG REASON":
            detail = f"  (said {s.actual_reason}, expected {s.expected_reason})"
        elif s.status == "WRONG DISPOSITION" and s.expected_reason:
            detail = f"  ({s.expected_reason})"
        lines.append(
            f"  {s.invoice:<10} {s.expected:<9} {actual:<9} {vat_due:>9} "
            f"{declared:>9}  {s.status}{detail}"
        )

    lines += [
        "",
        f"  actioned            {report.actioned}/{report.total}",
        f"  summaries grounded  {report.summaries_grounded}/{report.summaries_written} written",
        f"  vat checks          {report.receipts_raised} made, "
        f"{report.vat_correct_count}/{report.vat_due_count} with correct VAT",
        f"  fully correct       {report.fully_correct}/{report.total}"
        f"  ({report.accuracy * 100:.1f}%)",
        f"  verdict             {'PASS' if report.passed else 'FAIL'}",
        "",
    ]
    return "\n".join(lines)
