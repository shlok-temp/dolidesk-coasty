"""Tamper-evident evidence ledger. Standard library only.

Coasty's own framing is that the demo video cannot disagree with the run,
because every frame in it is a model-input frame pulled back from the run.
That is true as far as it goes, but it only binds the *pixels* to the run. It
says nothing about the relationship between what the agent SAW and what the
agent CLAIMED, or between what it claimed and what it CHANGED.

For a read-only lookup that gap is academic. For an agent that validates
supplier invoices it is the entire question an auditor would ask: on what
evidence did you approve this document? So this ledger binds three things that
are normally recorded separately, if at all:

1. FRAMES -- every model-input frame, hash-chained in order, so removing,
   reordering or substituting one breaks every link after it.
2. CLAIMS -- each value the agent reported, bound to the index of the frame it
   was read from. A claim with no frame behind it is an assertion; a claim with
   one is a citation.
3. WRITES -- each mutation the agent made, bound both to the frame that
   justified it and to the oracle's independent confirmation that it landed.

The chain is a rolling hash rather than a Merkle tree on purpose. The property
needed here is append-only ordering ("frame 34 came after frame 33 and neither
has changed"), not efficient membership proofs for a subset. A rolling chain
gives exactly that in a form anyone can recompute in ten lines, and being
independently recomputable matters far more than being clever.

What this does NOT claim: the ledger is built by this repo from data Coasty
returns, so it proves internal consistency, not that Coasty was honest. A
signature from Coasty over the frame hashes would be needed for that, and they
do not offer one. Overstating this would be worse than not having it.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping

# Domain separator. Keeps these hashes from colliding with any other scheme.
GENESIS = "coasty-ap-exception-desk/evidence/v1"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def link_hash(prev: str, sha: str) -> str:
    """Fold one link into the chain.

    The separator is not decorative. Hashing ``prev + sha`` by bare
    concatenation lets two different ``(prev, sha)`` pairs produce identical
    input -- a textbook boundary ambiguity. A separator that cannot occur in a
    hex digest removes it.
    """
    return sha256_hex(f"{prev}|{sha}")


def chain_root(frame_digests: Iterable[str]) -> str:
    """Reduce an ordered list of frame digests to a single root.

    Standalone by design: a verifier can recompute the root without
    instantiating anything, because the whole point is that re-deriving it is
    trivial.
    """
    head = sha256_hex(GENESIS)
    for sha in frame_digests:
        head = link_hash(head, sha)
    return head


class Ledger:
    """Accumulates frames, claims and writes into a sealed evidence document."""

    def __init__(self, **meta: Any) -> None:
        self.meta = meta
        self.frames: list[dict[str, Any]] = []
        self.claims: list[dict[str, Any]] = []
        self.writes: list[dict[str, Any]] = []
        self.head = sha256_hex(GENESIS)

    # ------------------------------------------------------------------ #

    def add_frame(
        self,
        *,
        index: int,
        sha256: str,
        taken_at: str | None = None,
        degraded: bool = False,
    ) -> "Ledger":
        """Append a model-input frame.

        ``sha256`` must be the digest of the actual image bytes, re-derived
        locally rather than copied from the API response. Copying it would make
        the chain attest to what Coasty *said* the frame was, which is a
        materially weaker claim and an easy one to make by accident.
        """
        if not isinstance(sha256, str) or not _HEX64.match(sha256):
            raise TypeError(f"frame {index}: sha256 must be a 64-char lowercase hex digest")
        if self.frames and index <= self.frames[-1]["index"]:
            # The chain encodes order. Accepting an out-of-order append would
            # make the root depend on insertion sequence rather than on run
            # sequence, which is precisely the property being attested.
            raise ValueError(
                f"frame index must increase: got {index} after {self.frames[-1]['index']}"
            )
        self.head = link_hash(self.head, sha256)
        self.frames.append(
            {
                "index": index,
                "sha256": sha256,
                "taken_at": taken_at,
                "degraded": degraded,
                "chain": self.head,
            }
        )
        return self

    def claim(self, field: str, value: Any, frame_index: int | None = None) -> "Ledger":
        """Record a value the agent reported, and the frame it read it from.

        ``frame_index`` may be None when the agent gave no citation. That is
        recorded honestly as uncited rather than quietly dropped, because an
        uncited claim is exactly what a reviewer needs to see.
        """
        self.claims.append(
            {
                "field": field,
                "value": value,
                "frame_index": frame_index,
                "cited": frame_index is not None,
            }
        )
        return self

    def write(
        self,
        *,
        op: str,
        ref: str,
        frame_index: int | None = None,
        detail: Any = None,
    ) -> "Ledger":
        """Record a mutation the agent made in the target system."""
        self.writes.append(
            {
                "op": op,
                "ref": ref,
                "frame_index": frame_index,
                "detail": detail,
                "confirmed": None,
            }
        )
        return self

    def confirm_writes(self, confirm: Callable[[Mapping[str, Any]], bool | None]) -> "Ledger":
        """Attach the oracle's independent confirmation for each recorded write.

        ``confirm(write)`` returns True, False or None. A write the oracle could
        not check stays None and is reported as unverified: collapsing unknown
        into False would manufacture failures, and into True would hide them.
        """
        for w in self.writes:
            try:
                w["confirmed"] = confirm(w)
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                w["confirmed"] = None
                w["confirm_error"] = str(exc)
        return self

    def seal(self, **extra: Any) -> dict[str, Any]:
        """The finished, serialisable evidence document."""
        uncited = sum(1 for c in self.claims if not c["cited"])
        unconfirmed = sum(1 for w in self.writes if w["confirmed"] is not True)
        return {
            "schema": GENESIS,
            **self.meta,
            "sealed_at": datetime.now(timezone.utc).isoformat(),
            "root": self.head,
            "frame_count": len(self.frames),
            "degraded_frames": sum(1 for f in self.frames if f["degraded"]),
            "frames": self.frames,
            "claims": self.claims,
            "writes": self.writes,
            "summary": {
                "claims": len(self.claims),
                "uncited_claims": uncited,
                "writes": len(self.writes),
                "unconfirmed_writes": unconfirmed,
            },
            **extra,
        }


def verify_evidence(
    evidence: Mapping[str, Any],
    frame_bytes: Mapping[int, bytes] | None = None,
) -> list[dict[str, Any]]:
    """Re-derive everything checkable in a sealed evidence document.

    Returns a list of ``{name, ok, detail}`` rather than a bare boolean, so a
    failure says which property broke.

    ``frame_bytes`` is optional. When supplied as ``{index: bytes}`` the digests
    are re-derived from the actual image bytes, which is the difference between
    verifying the ledger is self-consistent and verifying that it describes the
    images sitting on disk.
    """
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    frames = list(evidence.get("frames") or [])
    add("has frames", len(frames) > 0, f"{len(frames)} frames")

    # 1. Every link recomputes, in order.
    prev = sha256_hex(GENESIS)
    broke_at: int | None = None
    for f in frames:
        prev = link_hash(prev, f["sha256"])
        if f.get("chain") != prev:
            broke_at = f["index"]
            break
    add(
        "hash chain intact",
        broke_at is None,
        f"{len(frames)} links" if broke_at is None else f"broken at frame {broke_at}",
    )

    # 2. The published root is the chain head.
    add(
        "root matches chain",
        broke_at is None and evidence.get("root") == prev,
        f"{str(evidence.get('root'))[:16]}..."
        if broke_at is None
        else "chain broken, root not checked",
    )

    # 3. Indices strictly increase -- order is part of what the chain attests.
    ordered = all(
        f["index"] > frames[i - 1]["index"] for i, f in enumerate(frames) if i > 0
    )
    add("frame order strictly increasing", ordered, "ok" if ordered else "out-of-order index")

    # 4. Optionally: the digests describe the bytes actually held.
    if frame_bytes is not None:
        mismatched = 0
        for f in frames:
            buf = frame_bytes.get(f["index"])
            if buf is None or hashlib.sha256(buf).hexdigest() != f["sha256"]:
                mismatched += 1
        add(
            "digests match frame bytes",
            mismatched == 0,
            f"{mismatched} mismatched" if mismatched else f"{len(frames)} verified",
        )

    # 5. Every claim cites a frame that exists in the chain.
    known = {f["index"] for f in frames}
    claims = list(evidence.get("claims") or [])
    dangling = sum(1 for c in claims if c.get("cited") and c.get("frame_index") not in known)
    add(
        "claims cite real frames",
        dangling == 0,
        f"{dangling} dangling" if dangling else f"{len(claims)} claims",
    )

    return checks
