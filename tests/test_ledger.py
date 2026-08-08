"""Evidence ledger tests. Standard library only -- no pytest, no install.

    /c/Python314/python -m unittest discover -s tests -v

The point of the ledger is that tampering is DETECTED, so these tests are
mostly adversarial: build a good chain, then break it in each way it could
plausibly be broken, and assert the verifier says so. A ledger that only passes
on well-formed input is decoration.
"""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ap_desk.ledger import Ledger, chain_root, link_hash, verify_evidence  # noqa: E402


def digest(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def build(n: int = 5) -> Ledger:
    """A ledger over n synthetic frames."""
    led = Ledger(run_id="run_test", task="audit the vendor ledger")
    for i in range(n):
        led.add_frame(index=i, sha256=digest(f"frame-{i}"), taken_at=f"2026-08-04T00:0{i}:00Z")
    return led


def check(checks, name):
    return next(c for c in checks if c["name"] == name)


class ChainConstruction(unittest.TestCase):
    def test_sealed_ledger_verifies_clean(self):
        checks = verify_evidence(build().seal())
        self.assertTrue(all(c["ok"] for c in checks), checks)

    def test_root_equals_standalone_rederivation(self):
        # The whole promise is that anyone can recompute the root without this
        # class. If these two ever diverge, the promise is false.
        ev = build(7).seal()
        self.assertEqual(ev["root"], chain_root(f["sha256"] for f in ev["frames"]))

    def test_root_changes_when_a_frame_changes(self):
        a = chain_root([digest("a"), digest("b"), digest("c")])
        b = chain_root([digest("a"), digest("B"), digest("c")])
        self.assertNotEqual(a, b)

    def test_root_changes_when_frames_are_reordered(self):
        # Order is part of what is attested -- a run is a sequence, not a set.
        x, y, z = digest("x"), digest("y"), digest("z")
        self.assertNotEqual(chain_root([x, y, z]), chain_root([x, z, y]))

    def test_link_hashing_is_unambiguous_across_boundaries(self):
        # Bare concatenation would let ("ab","c") and ("a","bc") collide. The
        # separator prevents it, so assert the property rather than the impl.
        self.assertNotEqual(link_hash("ab", "c"), link_hash("a", "bc"))


class TamperDetection(unittest.TestCase):
    def test_substituting_a_frame_digest_breaks_the_chain(self):
        ev = build().seal()
        ev["frames"][2]["sha256"] = digest("forged")
        c = check(verify_evidence(ev), "hash chain intact")
        self.assertFalse(c["ok"])
        self.assertIn("frame 2", c["detail"])

    def test_deleting_a_frame_breaks_the_chain(self):
        ev = build().seal()
        del ev["frames"][2]
        self.assertFalse(check(verify_evidence(ev), "hash chain intact")["ok"])

    def test_forged_root_is_caught_even_when_chain_is_intact(self):
        ev = build().seal()
        ev["root"] = digest("a root I would prefer")
        checks = verify_evidence(ev)
        self.assertTrue(check(checks, "hash chain intact")["ok"])
        self.assertFalse(check(checks, "root matches chain")["ok"])

    def test_digests_not_describing_bytes_on_disk_are_caught(self):
        # Self-consistency is not enough: a chain can be perfectly well-formed
        # and still describe images nobody has. This closes that gap.
        ev = build(3).seal()
        frame_bytes = {
            0: b"frame-0",
            1: b"frame-1",
            2: b"SOMETHING ELSE ENTIRELY",
        }
        checks = verify_evidence(ev, frame_bytes)
        self.assertTrue(check(checks, "hash chain intact")["ok"])
        self.assertFalse(check(checks, "digests match frame bytes")["ok"])


class AppendDiscipline(unittest.TestCase):
    def test_rejects_a_non_hex_digest(self):
        with self.assertRaises(TypeError):
            Ledger().add_frame(index=0, sha256="nope")

    def test_rejects_an_out_of_order_frame(self):
        led = build(3)
        with self.assertRaises(ValueError):
            led.add_frame(index=1, sha256=digest("late"))


class ClaimsAndWrites(unittest.TestCase):
    def test_claim_citing_a_missing_frame_is_caught(self):
        led = build(3)
        led.claim("invoice_total", "1240.00", 99)
        self.assertFalse(check(verify_evidence(led.seal()), "claims cite real frames")["ok"])

    def test_uncited_claims_are_counted_not_dropped(self):
        led = build(3)
        led.claim("cited", "x", 1)
        led.claim("uncited", "y")
        ev = led.seal()
        self.assertEqual(len(ev["claims"]), 2)
        self.assertEqual(ev["summary"]["uncited_claims"], 1)

    def test_unconfirmable_write_stays_none(self):
        # Collapsing "could not check" into False invents failures; into True it
        # hides them. Neither is acceptable in something called evidence.
        led = build(2)
        led.write(op="validate", ref="FA-1", frame_index=1)
        led.write(op="validate", ref="FA-2", frame_index=1)

        def confirm(w):
            if w["ref"] == "FA-2":
                raise RuntimeError("oracle unreachable")
            return True

        led.confirm_writes(confirm)
        ev = led.seal()
        self.assertIs(ev["writes"][0]["confirmed"], True)
        self.assertIsNone(ev["writes"][1]["confirmed"])
        self.assertIn("unreachable", ev["writes"][1]["confirm_error"])
        self.assertEqual(ev["summary"]["unconfirmed_writes"], 1)


if __name__ == "__main__":
    unittest.main()
