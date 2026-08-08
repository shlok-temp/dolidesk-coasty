"""Portal and oracle tests. Standard library only.

The load-bearing tests here are in `OracleCatchesMistakes`. A scorer exercised
only on correct input is worthless -- it would report PASS just as happily if it
were comparing nothing at all. Each failure mode gets its own test, because they
carry different meanings: a wrong disposition is a reasoning error, a wrong hold
reason is a classification error, and an unactioned invoice is the agent giving
up, which a naive scorer silently reads as "not wrong".
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ap_desk.domain import build, match_invoice  # noqa: E402
from ap_desk.oracle import fetch_state, score  # noqa: E402
from ap_desk.portal import serve  # noqa: E402


class PortalTestCase(unittest.TestCase):
    """Boots a real server on an ephemeral port, per test class."""

    port = 0

    @classmethod
    def setUpClass(cls):
        cls.httpd = serve(port=0)
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        # Each test starts from a clean dataset, or one test's dispositions
        # would leak into the next and the failures would look random.
        urllib.request.urlopen(
            urllib.request.Request(self.base + "/_reset", method="POST"), timeout=5
        ).read()
        self.data = build()

    # -- helpers -------------------------------------------------------- #

    def get(self, path: str) -> str:
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return r.read().decode()

    def summarise(self, ref: str, text: str | None = None) -> int:
        """Record a summary. Grounded by default so it passes the oracle's check."""
        if text is None:
            invoice = self.data.invoices[ref]
            text = (f"{invoice.vendor_name} invoice covering "
                    f"{invoice.lines[0].description}, net {invoice.total:.2f}.")
        try:
            with urllib.request.urlopen(
                self.base + f"/invoice/{ref}/summary",
                data=urllib.parse.urlencode({"summary": text}).encode(),
                timeout=5,
            ) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code

    def dispose(self, ref: str, disposition: str, reason: str | None = None,
                *, summarise_first: bool = True) -> int:
        """Set a disposition, writing the prerequisite summary first by default.

        The portal requires a summary before it will accept a disposition, so
        every test that is about DISPOSITION logic would otherwise fail on a
        precondition it does not care about. `summarise_first=False` is for the
        tests that are specifically about that gate.
        """
        if summarise_first:
            self.summarise(ref)
        form = {"disposition": disposition}
        if reason:
            form["reason"] = reason
        try:
            with urllib.request.urlopen(
                self.base + f"/invoice/{ref}/dispose",
                data=urllib.parse.urlencode(form).encode(),
                timeout=5,
            ) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            # HTTPError is itself a response object and holds an open socket.
            # Leaving it to the garbage collector emits a ResourceWarning that
            # buries real output in noise, so close it explicitly.
            with exc:
                return exc.code

    def raise_receipt(self, ref: str, vat: str) -> int:
        try:
            with urllib.request.urlopen(
                self.base + f"/invoice/{ref}/receipt",
                data=urllib.parse.urlencode({"vat": vat}).encode(),
                timeout=5,
            ) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code

    def state(self) -> dict:
        return fetch_state(self.base)


class Screens(PortalTestCase):
    def test_every_screen_renders(self):
        ref = "FA-2582"
        link = self.data.links[ref]
        for path in ["/", "/menu", "/worklist", "/exceptions",
                     f"/invoice/{ref}", f"/po/{link['po']}", f"/reception/{link['reception']}"]:
            with self.subTest(path=path):
                self.assertIn("DoliDesk", self.get(path))

    def test_worklist_lists_every_invoice(self):
        html = self.get("/worklist")
        for ref in self.data.invoices:
            self.assertIn(ref, html)

    def test_invoice_screen_links_to_its_own_supporting_documents(self):
        # The agent navigates by these links. If they pointed at the wrong
        # documents the agent would compare the wrong numbers and the oracle
        # would blame the agent for the portal's mistake.
        for ref in self.data.invoices:
            link = self.data.links[ref]
            html = self.get(f"/invoice/{ref}")
            with self.subTest(invoice=ref):
                self.assertIn(f'/po/{link["po"]}', html)
                self.assertIn(f'/reception/{link["reception"]}', html)

    def test_unknown_document_is_a_clean_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/invoice/FA-9999")
        with ctx.exception:
            self.assertEqual(ctx.exception.code, 404)

    def test_screens_do_not_leak_the_verdict(self):
        # If a screen displayed the expected disposition, the agent could read
        # the answer instead of deriving it, and every accuracy number in this
        # repo would be meaningless.
        #
        # Reason CODES legitimately appear on the invoice screen: they are the
        # options in the hold dropdown, and the agent has to choose one. What
        # must never appear is a per-invoice recommendation -- so the check is
        # that no screen names the field the oracle grades against.
        for ref in list(self.data.invoices)[:4]:
            link = self.data.links[ref]
            for path in [f"/invoice/{ref}", f"/po/{link['po']}", f"/reception/{link['reception']}"]:
                html = self.get(path)
                with self.subTest(path=path):
                    self.assertNotIn("expected_disposition", html)
                    self.assertNotIn("expected_reason", html)

    def test_supporting_documents_do_not_name_reason_codes(self):
        # The dropdown lives on the invoice screen only. A reason code appearing
        # on a purchase order or receipt would be the portal hinting at the
        # answer on the very screens the comparison is made from.
        for ref in list(self.data.invoices)[:4]:
            link = self.data.links[ref]
            for path in [f"/po/{link['po']}", f"/reception/{link['reception']}"]:
                html = self.get(path)
                with self.subTest(path=path):
                    for code in ("PRICE_OVER_PO", "PRICE_UNDER_PO", "QTY_OVER_RECEIPT"):
                        self.assertNotIn(code, html)


class Disposition(PortalTestCase):
    def test_approve_changes_state(self):
        self.dispose("FA-2581", "APPROVED")
        self.assertEqual(self.state()["invoices"]["FA-2581"]["disposition"], "APPROVED")

    def test_hold_records_its_reason(self):
        self.dispose("FA-2582", "HELD", "PRICE_OVER_PO")
        got = self.state()["invoices"]["FA-2582"]
        self.assertEqual(got["disposition"], "HELD")
        self.assertEqual(got["hold_reason"], "PRICE_OVER_PO")

    def test_hold_without_a_reason_is_refused(self):
        # Silently accepting it would let a vague agent score as precise.
        self.assertEqual(self.dispose("FA-2583", "HELD"), 400)
        self.assertIsNone(self.state()["invoices"]["FA-2583"]["disposition"])

    def test_unknown_disposition_is_refused(self):
        self.assertEqual(self.dispose("FA-2583", "MAYBE"), 400)

    def test_held_invoice_appears_in_the_exception_queue(self):
        self.dispose("FA-2584", "HELD", "QTY_OVER_RECEIPT")
        self.assertIn("FA-2584", self.get("/exceptions"))

    def test_actions_are_logged_in_order(self):
        self.dispose("FA-2581", "APPROVED")
        self.dispose("FA-2582", "HELD", "PRICE_OVER_PO")
        # Summaries are logged too, so filter to dispositions: the assertion is
        # about ORDER, and mixing in the prerequisite writes would test the
        # helper's internals rather than the portal's behaviour.
        dispositions = [
            a["invoice"] for a in self.state()["actions"] if a.get("disposition")
        ]
        self.assertEqual(dispositions, ["FA-2581", "FA-2582"])


class OracleAgreesWithTruth(PortalTestCase):
    def test_a_perfect_pass_scores_full_marks(self):
        # A perfect pass now means all three steps on every invoice: summary,
        # correct disposition, and -- on approvals -- a receipt carrying the
        # correctly computed VAT.
        from ap_desk.domain import assess_tax

        for ref in self.data.invoices:
            truth = match_invoice(self.data, ref)
            self.dispose(ref, truth["expected_disposition"], truth["expected_reason"])
            if truth["expected_disposition"] == "APPROVED":
                vat = assess_tax(self.data, ref)["vat_total"]
                self.assertEqual(self.raise_receipt(ref, f"{vat:.2f}"), 200)

        report = score(self.data, self.state())
        self.assertTrue(report.passed, [s.status for s in report.scores])
        self.assertEqual(report.fully_correct, report.total)
        self.assertEqual(report.accuracy, 1.0)
        self.assertEqual(report.vat_correct_count, report.vat_due_count)


class OracleCatchesMistakes(PortalTestCase):
    """The tests that make the scorer worth trusting."""

    def test_approving_an_invoice_that_should_be_held_is_caught(self):
        self.dispose("FA-2582", "APPROVED")  # planted PRICE_OVER_PO
        s = next(x for x in score(self.data, self.state()).scores if x.invoice == "FA-2582")
        self.assertEqual(s.status, "WRONG DISPOSITION")
        self.assertFalse(s.correct)

    def test_holding_an_invoice_that_should_be_approved_is_caught(self):
        self.dispose("FA-2581", "HELD", "PRICE_OVER_PO")  # clean invoice
        s = next(x for x in score(self.data, self.state()).scores if x.invoice == "FA-2581")
        self.assertEqual(s.status, "WRONG DISPOSITION")

    def test_right_hold_with_the_wrong_reason_is_caught_separately(self):
        # This must NOT read as fully correct: the agent reached the right
        # decision for the wrong stated cause, which in an audit is a finding.
        self.dispose("FA-2584", "HELD", "PRICE_OVER_PO")  # truly QTY_OVER_RECEIPT
        s = next(x for x in score(self.data, self.state()).scores if x.invoice == "FA-2584")
        self.assertTrue(s.correct)
        self.assertFalse(s.reason_correct)
        self.assertEqual(s.status, "RIGHT HOLD, WRONG REASON")

    def test_an_unactioned_invoice_is_not_silently_forgiven(self):
        report = score(self.data, self.state())  # nothing disposed at all
        self.assertEqual(report.actioned, 0)
        self.assertEqual(report.fully_correct, 0)
        self.assertFalse(report.passed)
        self.assertTrue(all(s.status == "NOT ACTIONED" for s in report.scores))

    def test_a_partial_pass_fails_overall(self):
        # Absolute by design: a partial-credit threshold on a financial control
        # invites tuning the threshold until the run passes.
        for ref in list(sorted(self.data.invoices))[:6]:
            truth = match_invoice(self.data, ref)
            self.dispose(ref, truth["expected_disposition"], truth["expected_reason"])
        report = score(self.data, self.state())
        self.assertFalse(report.passed)
        self.assertEqual(report.actioned, 6)

    def test_reason_is_not_graded_on_an_invoice_that_should_be_approved(self):
        # Grading a reason there would double-penalise one mistake and make the
        # accuracy figure mean something other than what it claims.
        self.dispose("FA-2581", "APPROVED")
        s = next(x for x in score(self.data, self.state()).scores if x.invoice == "FA-2581")
        self.assertIsNone(s.reason_correct)


class OracleIsIndependent(PortalTestCase):
    def test_state_route_reports_records_not_verdicts(self):
        # If the portal returned the expected disposition, the oracle would be
        # reading an answer key served by the thing it is meant to audit.
        blob = json.dumps(self.state())
        self.assertNotIn("expected_disposition", blob)
        self.assertNotIn("expected_reason", blob)

    def test_state_reflects_reality_not_what_was_claimed(self):
        # The agent's own summary is not evidence. Only the server's state is.
        before = self.state()["invoices"]["FA-2587"]["disposition"]
        self.assertIsNone(before)
        self.dispose("FA-2587", "HELD", "QTY_OVER_RECEIPT")
        self.assertEqual(self.state()["invoices"]["FA-2587"]["disposition"], "HELD")


if __name__ == "__main__":
    unittest.main()
