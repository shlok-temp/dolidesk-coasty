"""Translating Coasty actions into things that happen on this desktop.

Kept apart from the loop in `driver.py` because this is the layer that has to
match Coasty's wire contract exactly, and it is the layer most likely to need
adjusting when that contract moves. Isolating it means a schema change is a
small edit here rather than surgery on the control flow.

The contract, from the published reference
------------------------------------------
An action is ``{action_type, params, description, raw_code}``. The verb is
`action_type` -- NOT `type` -- and every argument lives under `params`, never at
the top level. The vocabulary is fixed::

    click  type_text  key_press  key_combo  scroll  drag  move  wait  done  fail

Two rules worth stating because getting either wrong produces a run that looks
plausible and is silently wrong:

* **Termination comes from the response `status`**, which is `continue`, `done`
  or `fail`. A `done` action carries no OS effect, so a loop that watches for
  the action rather than the status can miss the end of a run.
* **Coordinates are in the pixel space the response reports**, top-left origin,
  no normalisation. The response echoes `screen_width`/`screen_height` as the
  dimensions the server actually used, which may differ from the image we sent.
  Scaling against the captured size instead of the reported size puts every
  click slightly off -- a failure that reads as the model being careless.

`raw_code` is a debug aid. The published guidance is never to evaluate it, and
this module does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The documented vocabulary. An action outside this set is refused rather than
# approximated: a driver that guesses at an instruction it does not understand
# is more dangerous than one that stops and says so.
VOCABULARY = frozenset(
    {"click", "type_text", "key_press", "key_combo", "scroll", "drag", "move", "wait", "done", "fail"}
)

TERMINAL_STATUS = frozenset({"done", "fail"})


class UnsupportedAction(ValueError):
    """An action verb outside the documented vocabulary."""


@dataclass(frozen=True)
class Action:
    """One normalised action, ready to execute."""

    verb: str
    params: dict[str, Any]
    description: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.verb in TERMINAL_STATUS


@dataclass(frozen=True)
class Prediction:
    """A whole predict response, normalised."""

    status: str  # continue | done | fail
    actions: list[Action]
    reasoning: str = ""
    step: int | None = None
    session_id: str | None = None
    # The coordinate space the server used. Authoritative for scaling.
    screen_width: int | None = None
    screen_height: int | None = None
    credits: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUS


def parse_prediction(body: Any) -> Prediction:
    """Normalise a predict / session-predict response.

    Both endpoints return the same action objects; the session variant adds
    `session_id` and `step`. Unknown extra keys are ignored rather than
    rejected, so a server-side addition does not break a working client.
    """
    if not isinstance(body, dict):
        raise ValueError(f"predict response was {type(body).__name__}, expected an object")

    raw_actions = body.get("actions") or []
    if not isinstance(raw_actions, list):
        raise ValueError("predict response `actions` was not a list")

    actions = [parse_action(a) for a in raw_actions if isinstance(a, dict)]

    usage = body.get("usage") or {}
    return Prediction(
        status=str(body.get("status") or "continue").lower(),
        actions=actions,
        reasoning=str(body.get("reasoning") or ""),
        step=body.get("step"),
        session_id=body.get("session_id"),
        screen_width=body.get("screen_width"),
        screen_height=body.get("screen_height"),
        credits=int(usage.get("credits_charged") or 0),
    )


def parse_action(raw: dict) -> Action:
    """One action object -> Action, or raise UnsupportedAction."""
    verb = str(raw.get("action_type") or "").strip().lower()
    if not verb:
        raise UnsupportedAction("action object has no action_type")
    if verb not in VOCABULARY:
        raise UnsupportedAction(f"unknown action_type {verb!r}")
    params = raw.get("params")
    return Action(
        verb=verb,
        params=params if isinstance(params, dict) else {},
        description=str(raw.get("description") or ""),
    )


def scale(
    x: float,
    y: float,
    *,
    model_space: tuple[int, int],
    region: tuple[int, int, int, int] | None,
) -> tuple[int, int]:
    """Map a model coordinate onto a real desktop coordinate.

    `model_space` is the width/height the *response* reported, not the size of
    the image we captured. When a region was captured rather than the whole
    screen, its offset has to be added back or every click lands relative to
    the wrong origin.
    """
    mw, mh = model_space
    if not mw or not mh:
        raise ValueError("model coordinate space is unknown; cannot place a click safely")
    ox, oy, rw, rh = region if region else (0, 0, mw, mh)
    return int(ox + x * rw / mw), int(oy + y * rh / mh)


def describe(action: Action) -> str:
    """A short console line for an action. Used in the live step log."""
    p = action.params
    if action.verb in ("click", "move"):
        return f"{action.verb} ({p.get('x')},{p.get('y')})"
    if action.verb == "type_text":
        text = str(p.get("text", ""))
        shown = text if len(text) <= 32 else text[:29] + "..."
        return f'type_text "{shown}"'
    if action.verb == "key_press":
        return f"key_press {p.get('key')}"
    if action.verb == "key_combo":
        keys = p.get("keys") or []
        return f"key_combo {'+'.join(str(k) for k in keys)}"
    if action.verb == "scroll":
        return f"scroll dx={p.get('dx', 0)} dy={p.get('dy', 0)}"
    if action.verb == "drag":
        return f"drag {p.get('from') or (p.get('x1'), p.get('y1'))} -> {p.get('to') or (p.get('x2'), p.get('y2'))}"
    if action.verb == "wait":
        return f"wait {p.get('seconds', 1)}s"
    return action.verb
