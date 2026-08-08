"""Tax assessment and summary-grounding tests. Standard library only.

The tax rules get the same treatment as the match rules: assert that each
planted VAT error is actually detected, not merely that the arithmetic runs.
A planted error the rule silently ignores is an invoice that looks like it is
testing something and is not -- the exact failure that already happened once
with a sub-de-minimis price variance.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ap_desk.domain import (  # noqa: E402
    TAX_CODES,
    VAT_ERRORS,
    assess_all,
    assess_tax,
    build,
    tax_round,
)
from ap_desk.oracle import summary_is_grounded  # noqa: E402


def invoice_ref(position: int) -> str:
    return f"FA-25{80 + position}"


class Rounding(unittest.TestCase):
    """Tax rounds half-up. Python's built-in round() does not."""

    def test_half_up_not_bankers(self):
        # round(2.675, 2) is 2.67 and round(0.125, 2) is 0.12 under Python's
        # round-half-to-even. On a tax figure that is not a preference, it is a
        # wrong number, and it disagrees with what anyone computes by hand.
        self.assertEqual(tax_round(2.675), 2.68)
        self.assertEqual(tax_round(0.125), 0.13)
        self.assertEqual(tax_round(1.005), 1.01)

    def test_exact_values_are_untouched(self):
        self.assertEqual(tax_round(10.00), 10.00)
        self.assertEqual(tax_round(0.0), 0.0)


class BandedAssessment(unittest.TestCase):
    def setUp(self):
        self.data = build()

    def test_every_line_carries_a_known_tax_code(self):
        for inv in self.data.invoices.values():
            for line in inv.lines:
                with self.subTest(item=line.item):
                    self.assertIn(line.tax_code, TAX_CODES)

    def test_vat_is_computed_per_band_not_on_the_invoice_total(self):
        # The distinction only shows on a multi-band invoice: applying one rate
        # to the whole net would give a different, confidently wrong answer.
        multi = next(
            a for a in assess_all(self.data) if len(a["bands"]) > 1
        )
        naive = tax_round(multi["net_total"] * 0.20)
        self.assertNotEqual(multi["vat_total"], naive)

    def test_band_vat_equals_rate_applied_to_band_net(self):
        for a in assess_all(self.data):
            for band in a["bands"]:
                with self.subTest(invoice=a["invoice"], band=band["code"]):
                    self.assertEqual(
                        band["vat"], tax_round(band["net"] * band["rate"] / 100)
                    )

    def test_band_nets_sum_to_the_invoice_net(self):
        for a in assess_all(self.data):
            with self.subTest(invoice=a["invoice"]):
                self.assertAlmostEqual(
                    sum(b["net"] for b in a["bands"]), a["net_total"], places=2
                )

    def test_zero_rated_lines_attract_no_vat(self):
        for a in assess_all(self.data):
            for band in a["bands"]:
                if band["code"] == "Z":
                    with self.subTest(invoice=a["invoice"]):
                        self.assertEqual(band["vat"], 0.0)

    def test_gross_is_net_plus_vat(self):
        for a in assess_all(self.data):
            with self.subTest(invoice=a["invoice"]):
                self.assertEqual(
                    a["gross_total"], tax_round(a["net_total"] + a["vat_total"])
                )

    def test_the_dataset_spans_all_three_bands(self):
        # If every item fell in one band the tax task would collapse into one
        # multiplication and stop testing anything interesting.
        seen = {b["code"] for a in assess_all(self.data) for b in a["bands"]}
        self.assertEqual(seen, {"S", "R", "Z"})

    def test_at_least_one_invoice_spans_multiple_bands(self):
        self.assertTrue(any(len(a["bands"]) > 1 for a in assess_all(self.data)))


class PlantedVatErrors(unittest.TestCase):
    """The load-bearing tests: a planted error must actually be detectable."""

    def setUp(self):
        self.data = build()

    def test_every_planted_vat_error_is_detected(self):
        for position, delta in VAT_ERRORS.items():
            ref = invoice_ref(position)
            with self.subTest(invoice=ref):
                a = assess_tax(self.data, ref)
                self.assertFalse(a["vendor_vat_correct"])
                self.assertAlmostEqual(a["vat_discrepancy"], delta, places=2)

    def test_invoices_without_a_planted_error_have_correct_vendor_vat(self):
        for position in range(1, 13):
            if position in VAT_ERRORS:
                continue
            ref = invoice_ref(position)
            with self.subTest(invoice=ref):
                a = assess_tax(self.data, ref)
                self.assertTrue(a["vendor_vat_correct"])
                self.assertEqual(a["vat_discrepancy"], 0.0)

    def test_planted_errors_are_visible_not_a_rounding_artefact(self):
        # A 3p discrepancy would test the agent's eyesight rather than its
        # arithmetic, and would be indistinguishable from a rounding choice.
        for position in VAT_ERRORS:
            a = assess_tax(self.data, invoice_ref(position))
            with self.subTest(invoice=a["invoice"]):
                self.assertGreater(abs(a["vat_discrepancy"]), 1.00)

    def test_a_vat_error_can_land_on_a_cleanly_matched_invoice(self):
        # Tax errors and match defects are seeded independently on purpose. If
        # they always coincided, an agent could infer one from the other rather
        # than checking both.
        from ap_desk.domain import match_invoice

        clean_but_taxed_wrong = [
            p for p in VAT_ERRORS
            if not match_invoice(self.data, invoice_ref(p))["findings"]
        ]
        self.assertTrue(clean_but_taxed_wrong)

    def test_the_seed_and_the_oracle_agree_independently(self):
        # The seed computes the vendor's "true" VAT with its own copy of the
        # banding rule, deliberately not importing the oracle's. If a bug made
        # them disagree, an invoice with no planted error would still look
        # wrong -- so assert the two implementations match.
        for position in range(1, 13):
            if position in VAT_ERRORS:
                continue
            ref = invoice_ref(position)
            inv = self.data.invoices[ref]
            with self.subTest(invoice=ref):
                self.assertEqual(inv.claimed_vat, assess_tax(self.data, ref)["vat_total"])


class SummaryGrounding(unittest.TestCase):
    """Grounding is checkable; prose quality is not, and is not graded."""

    def setUp(self):
        self.data = build()
        self.invoice = self.data.invoices["FA-2581"]

    def test_a_summary_naming_the_vendor_is_grounded(self):
        name = self.invoice.vendor_name.split()[0]
        self.assertTrue(summary_is_grounded(f"{name} invoice for goods.", self.invoice))

    def test_a_summary_naming_an_item_code_is_grounded(self):
        code = self.invoice.lines[0].item
        self.assertTrue(summary_is_grounded(f"Covers {code} and related parts.", self.invoice))

    def test_a_summary_describing_the_goods_is_grounded(self):
        word = next(
            w for w in self.invoice.lines[0].description.lower().replace("-", " ").split()
            if len(w) >= 4 and w.isalpha()
        )
        self.assertTrue(summary_is_grounded(f"An order of {word} units.", self.invoice))

    def test_generic_filler_is_not_grounded(self):
        # This is the whole point: an agent that types plausible boilerplate
        # without reading the invoice must not pass the check.
        for filler in (
            "This invoice has been reviewed and appears to be in order.",
            "Standard purchase, nothing unusual, proceeding with the match.",
            "The document was examined against supporting records.",
        ):
            with self.subTest(text=filler):
                self.assertFalse(summary_is_grounded(filler, self.invoice))

    def test_a_summary_about_a_different_invoice_is_not_grounded(self):
        other = self.data.invoices["FA-2590"]
        text = f"{other.vendor_name} supplied {other.lines[0].description}."
        # Only meaningful when the two invoices genuinely share nothing.
        if other.vendor_name.split()[0] != self.invoice.vendor_name.split()[0]:
            self.assertFalse(summary_is_grounded(text, self.invoice))

    def test_absent_and_ungrounded_are_different_answers(self):
        self.assertIsNone(summary_is_grounded(None, self.invoice))
        self.assertIsNone(summary_is_grounded("", self.invoice))
        self.assertIs(summary_is_grounded("nothing specific here", self.invoice), False)


if __name__ == "__main__":
    unittest.main()


class VatVerdictGrading(unittest.TestCase):
    """The two-button VAT check is graded against recomputed truth.

    Typing a figure is where live runs kept stalling, so the tax stage asks for
    a judgement instead: does the vendor's printed VAT agree with yours? The
    arithmetic required is identical -- only the input is a click rather than a
    text entry -- so the grading has to be just as strict.
    """

    def setUp(self):
        self.data = build()

    def _clean_but_taxed_wrong(self) -> str:
        """An invoice that matches cleanly but whose printed VAT is wrong.

        Both conditions are required. VAT is only graded on an invoice the agent
        correctly APPROVED, so a VAT-error invoice that also fails the match
        never reaches the tax check -- picking one of those tests nothing, which
        is exactly the mistake this helper exists to prevent.
        """
        from ap_desk.domain import match_invoice

        for position in VAT_ERRORS:
            ref = invoice_ref(position)
            if match_invoice(self.data, ref)["expected_disposition"] == "APPROVED":
                return ref
        raise AssertionError("the seed has no cleanly-matched invoice with a VAT error")

    def _verdict_score(self, ref: str, verdict: str):
        from ap_desk.oracle import score

        state = {"invoices": {ref: {"disposition": "APPROVED", "vat_verdict": verdict}}}
        return next(s for s in score(self.data, state).scores if s.invoice == ref)

    def test_agreeing_with_a_correct_vendor_figure_is_right(self):
        ref = invoice_ref(1)  # no planted VAT error
        self.assertTrue(assess_tax(self.data, ref)["vendor_vat_correct"])
        self.assertTrue(self._verdict_score(ref, "AGREES").vat_correct)

    def test_agreeing_with_a_wrong_vendor_figure_is_caught(self):
        # The whole point: an agent that does not do the arithmetic will accept
        # whatever is printed, and must be marked down for it.
        ref = self._clean_but_taxed_wrong()
        self.assertFalse(assess_tax(self.data, ref)["vendor_vat_correct"])
        s = self._verdict_score(ref, "AGREES")
        self.assertFalse(s.vat_correct)
        self.assertEqual(s.status, "RIGHT CALL, WRONG VAT")

    def test_disputing_a_wrong_vendor_figure_is_right(self):
        ref = self._clean_but_taxed_wrong()
        self.assertTrue(self._verdict_score(ref, "DISPUTED").vat_correct)

    def test_disputing_a_correct_vendor_figure_is_caught(self):
        ref = invoice_ref(1)
        self.assertFalse(self._verdict_score(ref, "DISPUTED").vat_correct)

    def test_no_verdict_on_an_approved_invoice_is_unfinished(self):
        from ap_desk.oracle import score

        ref = invoice_ref(1)
        state = {"invoices": {ref: {"disposition": "APPROVED"}}}
        s = next(x for x in score(self.data, state).scores if x.invoice == ref)
        self.assertEqual(s.status, "APPROVED, NO VAT CHECK")

    def test_a_held_invoice_needs_no_verdict(self):
        from ap_desk.oracle import score

        ref = invoice_ref(2)  # planted match defect -> should be HELD
        state = {"invoices": {ref: {"disposition": "HELD",
                                    "hold_reason": "PRICE_OVER_PO"}}}
        s = next(x for x in score(self.data, state).scores if x.invoice == ref)
        self.assertEqual(s.status, "CORRECT")
