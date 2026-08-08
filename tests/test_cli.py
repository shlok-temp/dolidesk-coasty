"""CLI-level tests: cost gating, console safety, focus checks.

These cover the failure modes that only appear when a real operator runs a real
command on a real Windows desktop -- the ones unit tests of the pure logic will
never reach, and which are the most expensive to discover mid-run.
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ap_desk.cli as cli  # noqa: E402


class ConsoleSafety(unittest.TestCase):
    """Windows consoles default to cp1252 and raise on anything outside it."""

    def test_survives_characters_outside_cp1252(self):
        # The real crash: a spinner glyph in a window title took down the run
        # at the exact moment it was trying to report a focus problem.
        for hostile in ("⠐", "—", "\U0001f600", "中文"):
            with self.subTest(char=repr(hostile)):
                self.assertIsInstance(cli._safe(f"title {hostile} more"), str)

    def test_plain_ascii_is_untouched(self):
        self.assertEqual(cli._safe("DoliDesk worklist"), "DoliDesk worklist")

    def test_active_window_title_never_raises(self):
        # Called on the error path, so it must not itself become the error.
        self.assertIsInstance(cli._active_window_title(), str)


class CostModel(unittest.TestCase):
    """The estimate must stay honest even though it no longer gates the run.

    The exact-cost handshake was removed as friction: the operator already
    chooses the target and the key, and the real bound on a runaway loop is the
    locally-enforced step cap. But the printed figure still has to be right, or
    it is worse than not printing one.
    """

    def test_estimate_includes_the_one_off_session_charge(self):
        # Verified live: session create bills 10 credits before any step runs.
        est = cli._estimate()
        self.assertEqual(
            est["expected_cents"],
            est["session_setup_cents"] + est["expected_steps"] * est["per_step_cents"],
        )
        self.assertEqual(
            est["worst_case_cents"],
            est["session_setup_cents"] + est["max_steps"] * est["per_step_cents"],
        )

    def test_worst_case_fits_under_the_declared_cap(self):
        est = cli._estimate()
        self.assertLessEqual(est["worst_case_cents"], est["cap_cents"])

    def test_expected_never_exceeds_worst_case(self):
        est = cli._estimate()
        self.assertLessEqual(est["expected_cents"], est["worst_case_cents"])
        self.assertLessEqual(est["expected_steps"], est["max_steps"])

    def test_step_budget_clears_the_fifty_step_floor(self):
        # The workflow is 12 invoices x 3 documents + 12 dispositions. If the
        # budget ever drops below the work, the agent runs out mid-queue and
        # the oracle reports NOT ACTIONED for the tail.
        self.assertGreaterEqual(cli._estimate()["expected_steps"], 50)


def _run_options() -> set[str]:
    """Every option flag the `run` subcommand accepts."""
    parser = cli.build_parser()
    subparsers = next(
        action for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    run = subparsers.choices["run"]
    return {opt for action in run._actions for opt in action.option_strings}


class RunArguments(unittest.TestCase):
    def test_the_cost_handshake_is_gone(self):
        # It made --dry-run -- which spends nothing on actions -- refuse to
        # start, which is friction with no safety behind it. The step cap is
        # what actually bounds a runaway loop.
        self.assertNotIn("--confirm-cost-cents", _run_options())

    def test_step_cap_is_overridable(self):
        # A short capped run is how you sanity-check the loop without paying
        # for a full pass.
        self.assertIn("--steps", _run_options())

    def test_browser_control_flags_exist(self):
        options = _run_options()
        for flag in ("--no-browser", "--keep-browser", "--kiosk", "--dry-run", "--live"):
            with self.subTest(flag=flag):
                self.assertIn(flag, options)

    def test_every_subcommand_is_reachable(self):
        parser = cli.build_parser()
        subparsers = next(
            action for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        self.assertEqual(
            set(subparsers.choices),
            {"portal", "doctor", "estimate", "rehearse", "run"},
        )


class TaskPrompt(unittest.TestCase):
    def test_names_the_target_url_verbatim(self):
        # Coasty's own authoring guidance opens with "name the full URL", and
        # none of their eleven published prompts do. This one must.
        prompt = cli.TASK.format(url="http://127.0.0.1:8900")
        self.assertIn("http://127.0.0.1:8900", prompt)

    def test_states_the_tolerances_but_not_the_answers(self):
        prompt = cli.TASK.format(url="http://x")
        self.assertIn("2 percent", prompt)
        # No invoice reference may appear, or the prompt leaks the worklist.
        self.assertNotIn("FA-25", prompt)

    def test_names_every_reason_code_the_portal_accepts(self):
        from ap_desk.portal import HOLD_REASONS

        prompt = cli.TASK.format(url="http://x")
        for code, _ in HOLD_REASONS:
            if code == "NOT_ON_PO":
                continue  # not planted by the seed; the portal offers it anyway
            with self.subTest(code=code):
                self.assertIn(code, prompt)

    def test_states_a_finishing_condition(self):
        # "monitor invoices" never terminates; "report how many you approved"
        # does. Without one the agent burns the whole step cap.
        self.assertIn("report", cli.TASK.lower())


class ReadmeStaysHonest(unittest.TestCase):
    """The README quotes numbers the code owns. They have to agree.

    A README is the only thing most people read, so a cost figure that drifted
    from the code is a straightforward lie -- and it drifts silently, because
    nothing executes prose. These tests are cheap and catch it the moment the
    step budget changes.
    """

    @classmethod
    def setUpClass(cls):
        cls.readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
            encoding="utf-8"
        )

    def test_quotes_the_real_demo_cost(self):
        est = cli._estimate(demo=True)
        self.assertIn(f"{est['expected_cents']}¢", self.readme)
        self.assertIn(f"{est['worst_case_cents']}¢", self.readme)

    def test_quotes_the_real_full_cost(self):
        est = cli._estimate(demo=False)
        self.assertIn(f"{est['expected_cents']}¢", self.readme)
        self.assertIn(f"{est['worst_case_cents']}¢", self.readme)

    def test_quotes_the_real_demo_invoice_count(self):
        self.assertIn(f"{cli.DEMO_LIMIT}-invoice", self.readme)

    def test_says_where_to_get_a_key(self):
        # Telling someone to set COASTY_API_KEY without saying where one comes
        # from is a dead end.
        self.assertIn("coasty.ai/developers/keys", self.readme)

    def test_credits_the_platform_it_is_built_on(self):
        self.assertIn("coasty.ai", self.readme)
        self.assertIn("@coastyai", self.readme)

    def test_the_product_name_is_consistent(self):
        # The rename from the old working title has to be complete, or the
        # README describes a program that no longer exists under that name.
        self.assertNotIn("APEX-3000", self.readme)
        self.assertIn("DoliDesk", self.readme)


class DemoQueueCoverage(unittest.TestCase):
    """The demo queue must still exercise everything the full queue does.

    A five-invoice subset chosen for its runtime is only useful if it happens to
    contain every case. Asserting it beats trusting a comment: if the seed or
    the defect table is ever reordered, this fails rather than quietly shipping
    a demo that never shows a hold.
    """

    def setUp(self):
        from ap_desk.domain import assess_tax, build, match_invoice

        data = build()
        self.refs = sorted(data.invoices)[: cli.DEMO_LIMIT]
        self.verdicts = [match_invoice(data, r) for r in self.refs]
        self.taxes = [assess_tax(data, r) for r in self.refs]

    def test_covers_both_dispositions(self):
        dispositions = {v["expected_disposition"] for v in self.verdicts}
        self.assertEqual(dispositions, {"APPROVED", "HELD"})

    def test_covers_more_than_one_hold_reason(self):
        reasons = {v["expected_reason"] for v in self.verdicts if v["expected_reason"]}
        self.assertGreaterEqual(len(reasons), 2)

    def test_includes_a_multi_band_tax_calculation(self):
        # A single-band invoice collapses the tax task into one multiplication.
        self.assertTrue(any(len(t["bands"]) > 1 for t in self.taxes))

    def test_includes_an_invoice_whose_printed_vat_is_wrong(self):
        self.assertTrue(any(t["vendor_vat_correct"] is False for t in self.taxes))

    def test_includes_at_least_one_tax_receipt(self):
        approvals = [v for v in self.verdicts if v["expected_disposition"] == "APPROVED"]
        self.assertTrue(approvals)


if __name__ == "__main__":
    unittest.main()
