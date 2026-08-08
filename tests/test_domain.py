"""Seed and matching-rule tests. Standard library only.

    /c/Python314/python -m unittest discover -s tests -v

The load-bearing test here is `test_every_planted_defect_is_detected`. Without
it, the seed and the matching rules can drift apart silently: a defect gets
planted, the rule correctly declines to flag it, and the invoice still *looks*
like an exception case in the table while actually testing nothing. That already
happened once -- a 17.6% price variance on a 1.25 carton is 22p, below the
de-minimis floor -- and it was invisible until the findings were counted per
invoice rather than in aggregate.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ap_desk.domain import (  # noqa: E402
    DEFECTS,
    PRICE_TOLERANCE_ABS,
    PRICE_TOLERANCE_PCT,
    build,
    match_all,
    match_invoice,
)

# What each planted defect must produce. A defect that yields anything else is
# either a broken rule or a broken seed; both are bugs and both must fail loudly.
EXPECTED_CODES = {
    None: set(),
    "price_over": {"PRICE_OVER_PO"},
    "price_under": {"PRICE_UNDER_PO"},
    "qty_over": {"QTY_OVER_RECEIPT"},
    "both": {"QTY_OVER_RECEIPT", "PRICE_OVER_PO"},
}


def invoice_ref(position: int) -> str:
    return f"FA-25{80 + position}"


class Determinism(unittest.TestCase):
    def test_same_seed_yields_identical_documents(self):
        a, b = build(), build()
        for ref in a.invoices:
            self.assertEqual(
                [(l.item, l.qty, l.unit_price) for l in a.invoices[ref].lines],
                [(l.item, l.qty, l.unit_price) for l in b.invoices[ref].lines],
                f"{ref} differs between two builds at the same seed",
            )

    def test_a_different_seed_yields_different_documents(self):
        # If the seed were ignored, every "deterministic" claim above would be
        # vacuously true. This is the control.
        a, b = build(), build(seed=99)
        self.assertNotEqual(
            [(l.item, l.qty, l.unit_price) for r in sorted(a.invoices) for l in a.invoices[r].lines],
            [(l.item, l.qty, l.unit_price) for r in sorted(b.invoices) for l in b.invoices[r].lines],
        )


class PlantedDefects(unittest.TestCase):
    def setUp(self):
        self.data = build()

    def test_every_planted_defect_is_detected(self):
        for position, defect in enumerate(DEFECTS, start=1):
            ref = invoice_ref(position)
            with self.subTest(invoice=ref, defect=defect):
                verdict = match_invoice(self.data, ref)
                got = {f["code"] for f in verdict["findings"]}
                self.assertEqual(
                    got,
                    EXPECTED_CODES[defect],
                    f"{ref} planted as {defect!r} produced {sorted(got)}",
                )

    def test_clean_invoices_are_approvable(self):
        for position, defect in enumerate(DEFECTS, start=1):
            if defect is not None:
                continue
            verdict = match_invoice(self.data, invoice_ref(position))
            self.assertEqual(verdict["expected_disposition"], "APPROVED")
            self.assertIsNone(verdict["expected_reason"])

    def test_defective_invoices_are_held_with_a_reason(self):
        for position, defect in enumerate(DEFECTS, start=1):
            if defect is None:
                continue
            verdict = match_invoice(self.data, invoice_ref(position))
            self.assertEqual(verdict["expected_disposition"], "HELD")
            self.assertIn(verdict["expected_reason"], EXPECTED_CODES[defect])


class DistributionShape(unittest.TestCase):
    """The test is only as good as the spread of cases it contains."""

    def setUp(self):
        self.verdicts = list(match_all(build()))

    def test_both_dispositions_are_represented(self):
        # A run with no clean invoices would never exercise the approve path;
        # a run with no exceptions would never exercise hold. Either would look
        # like a pass while testing half the workflow.
        dispositions = {v["expected_disposition"] for v in self.verdicts}
        self.assertEqual(dispositions, {"APPROVED", "HELD"})

    def test_every_finding_code_appears(self):
        codes = {f["code"] for v in self.verdicts for f in v["findings"]}
        self.assertEqual(codes, {"PRICE_OVER_PO", "PRICE_UNDER_PO", "QTY_OVER_RECEIPT"})

    def test_at_least_one_invoice_carries_two_findings(self):
        self.assertTrue(any(len(v["findings"]) >= 2 for v in self.verdicts))

    def test_worklist_is_not_pre_sorted_by_the_answer(self):
        # If exceptions clustered at one end, an agent could score well by
        # reading only the first few rows -- which is the failure mode the
        # scrambled ordering exists to prevent.
        flags = [v["expected_disposition"] == "HELD" for v in self.verdicts]
        self.assertNotEqual(flags, sorted(flags), "worklist is sorted by disposition")
        self.assertNotEqual(flags, sorted(flags, reverse=True))

    def test_enough_documents_to_clear_the_step_floor(self):
        # The hackathon floor is 50 steps. Three documents per invoice plus a
        # disposition action each is the structural reason this reaches it, so
        # assert the structure rather than trusting a step count later.
        docs = len(self.verdicts) * 3
        actions = len(self.verdicts)
        self.assertGreaterEqual(docs + actions, 48)


class ToleranceBands(unittest.TestCase):
    def test_planted_price_variances_clear_both_bands(self):
        # The exact bug this suite exists to catch: a variance can breach the
        # percentage band and still sit under the absolute floor.
        data = build()
        for position, defect in enumerate(DEFECTS, start=1):
            if defect not in ("price_over", "price_under", "both"):
                continue
            ref = invoice_ref(position)
            inv = data.invoices[ref]
            po = data.purchase_orders[data.links[ref]["po"]]
            billed, ordered = inv.lines[0].unit_price, po.lines[0].unit_price
            delta = abs(billed - ordered)
            pct = delta / ordered * 100
            with self.subTest(invoice=ref, defect=defect):
                self.assertGreater(delta, PRICE_TOLERANCE_ABS, f"{ref}: {delta:.2f} under abs floor")
                self.assertGreater(pct, PRICE_TOLERANCE_PCT, f"{ref}: {pct:.1f}% under pct band")


if __name__ == "__main__":
    unittest.main()
