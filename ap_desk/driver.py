"""The local agent loop: capture this screen, ask Coasty what to do, do it.

This is the layer Coasty's docs call "own the screenshot and action loop", and
it is what makes the run visible. The reference catalog submits a goal to
`POST /v1/tasks`, Coasty provisions a VM in a datacentre, and the browser opens
somewhere nobody is watching; the video is rendered afterwards from the frames
the model saw. Useful, but you cannot watch it happen and the target has to be
reachable from the public internet.

Here the loop runs on the operator's own machine::

    capture the screen  ->  POST /v1/sessions/{id}/predict  ->  perform the actions

So the browser being driven is the real one on the real desktop. You can watch
it, screen-record it, and the target can sit on localhost with nothing exposed.

Safety
------
This moves the real mouse and presses real keys, so it is bounded deliberately:

* a step cap enforced locally, independent of anything the server says;
* a kill switch -- move the pointer into any screen corner and PyAutoGUI's own
  failsafe aborts, or delete the sentinel file the loop watches;
* a dry-run mode that captures and predicts but performs nothing, so the action
  stream can be read before anything touches the desktop.

Dependencies
------------
Screen capture and input synthesis are the only things the standard library
cannot do, so they live behind the `[driver]` extra and are imported lazily.
Everything else here -- oracle, ledger, portal, tests -- runs on a bare
interpreter, and importing this module must not change that.
"""

from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ap_desk.actions import (
    Action,
    Prediction,
    UnsupportedAction,
    describe,
    parse_prediction,
    scale,
)


class DriverUnavailable(RuntimeError):
    """The optional capture/input dependencies are missing."""


def _load_backends():
    """Import the optional backends, with an actionable message if absent."""
    try:
        import mss  # noqa: PLC0415
        import mss.tools  # noqa: PLC0415  -- not pulled in by `import mss` alone
        import pyautogui  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise DriverUnavailable(
            "The local driver needs screen capture and input synthesis:\n"
            "    python -m pip install mss pyautogui\n"
            "Everything else in this project runs without them."
        ) from exc

    # Corner-of-screen abort. This is the operator's physical kill switch and
    # must never be disabled, however much a stray movement costs a run.
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    return mss, pyautogui


def _screenshotter(mss_module):
    """The capture class, across the mss 9.x / 10.x rename.

    mss 10 renamed `mss.mss` to `mss.MSS`, deprecating the old spelling now and
    removing it later. Resolving at call time keeps both working, which matters
    because this is the one dependency an operator may already have pinned.
    """
    return getattr(mss_module, "MSS", None) or mss_module.mss


@dataclass
class Step:
    """One turn of the loop, kept for the ledger and the video."""

    index: int
    image: bytes
    sha256: str
    taken_at: str
    actions: list[Action] = field(default_factory=list)
    reasoning: str = ""
    note: str = ""
    performed: bool = False
    credits: int = 0
    # True when the session had to be reopened before this step could run.
    recovered: bool = False
    # The unparsed response. A run that ends in `fail` is almost impossible to
    # diagnose without it: the console shows a truncated reasoning string and
    # nothing about why the model produced no action.
    raw: dict | None = None


@dataclass
class DriverResult:
    steps: list[Step] = field(default_factory=list)
    finished: bool = False
    reason: str = ""
    session_id: str | None = None
    credits: int = 0

    @property
    def actions_performed(self) -> int:
        return sum(len(s.actions) for s in self.steps if s.performed)


class LocalDriver:
    """Drives this machine's screen against a Coasty session."""

    def __init__(
        self,
        client,
        *,
        max_steps: int = 90,
        dry_run: bool = False,
        region: tuple[int, int, int, int] | None = None,
        stop_file: Path | None = None,
        on_step: Callable[[Step], None] | None = None,
        settle_seconds: float = 0.7,
        include_reasoning: bool = True,
    ) -> None:
        self.client = client
        self.max_steps = max_steps
        self.dry_run = dry_run
        self.region = region
        self.stop_file = stop_file
        self.on_step = on_step or (lambda _: None)
        self.settle_seconds = settle_seconds
        # Reasoning ON. Turning it off was tried and made things measurably
        # worse -- the model stalled at step 4 instead of step 11, because a
        # multi-step task needs somewhere to work out what it is doing. The
        # truncation that ends a run in `fail` is not caused by reasoning
        # existing; it is caused by the model having nothing useful on screen
        # and narrating at length about it.
        self.include_reasoning = include_reasoning
        self._mss = None
        self._gui = None

    # ------------------------------------------------------------------ #

    def _backends(self):
        if self._mss is None:
            self._mss, self._gui = _load_backends()
        return self._mss, self._gui

    def capture(self) -> tuple[bytes, tuple[int, int]]:
        """Grab the screen as PNG bytes plus its pixel size."""
        mss, _ = self._backends()
        with _screenshotter(mss)() as sct:
            monitor = (
                {"left": self.region[0], "top": self.region[1],
                 "width": self.region[2], "height": self.region[3]}
                if self.region
                else sct.monitors[1]
            )
            shot = sct.grab(monitor)
            png = mss.tools.to_png(shot.rgb, shot.size)
        return png, (shot.size.width, shot.size.height)

    def perform(self, action: Action, model_space: tuple[int, int]) -> str:
        """Execute one action on the real desktop. Returns a human note.

        `model_space` is the coordinate space the SERVER reported, not the size
        of the image we captured. They usually agree, but when they do not,
        scaling against the capture puts every click slightly off -- a failure
        that reads as the model being careless rather than the driver wrong.
        """
        _, gui = self._backends()
        p = action.params

        if action.is_terminal:
            return action.verb
        if self.dry_run:
            return f"[dry-run] {describe(action)}"

        def point(px: Any, py: Any) -> tuple[int, int]:
            return scale(float(px or 0), float(py or 0),
                         model_space=model_space, region=self.region)

        if action.verb == "click":
            x, y = point(p.get("x"), p.get("y"))
            button = str(p.get("button") or "left")
            clicks = int(p.get("clicks") or 1)
            gui.click(x, y, clicks=clicks, button=button, duration=0.15,
                      interval=0.08 if clicks > 1 else 0.0)
            return f"click ({x},{y}){'' if clicks == 1 else f' x{clicks}'}"

        if action.verb == "move":
            x, y = point(p.get("x"), p.get("y"))
            gui.moveTo(x, y, duration=0.15)
            return f"move ({x},{y})"

        if action.verb == "type_text":
            text = str(p.get("text", ""))
            gui.typewrite(text, interval=0.02)
            return f"type {len(text)} chars"

        if action.verb == "key_press":
            key = str(p.get("key") or p.get("keys") or "")
            gui.press(key)
            return f"key {key}"

        if action.verb == "key_combo":
            keys = p.get("keys") or []
            if isinstance(keys, str):
                keys = keys.split("+")
            gui.hotkey(*[str(k).strip().lower() for k in keys])
            return f"combo {'+'.join(str(k) for k in keys)}"

        if action.verb == "scroll":
            # Positive dy means scrolling DOWN the page. PyAutoGUI's sign is the
            # opposite, so this negates deliberately -- getting it backwards
            # scrolls away from the content and looks like the model being lost.
            dy = int(p.get("dy") or p.get("amount") or 0)
            if p.get("x") is not None and p.get("y") is not None:
                gui.moveTo(*point(p.get("x"), p.get("y")), duration=0.1)
            gui.scroll(-dy if dy else -400)
            return f"scroll {dy or -400}"

        if action.verb == "drag":
            start = p.get("from") or (p.get("x1"), p.get("y1"))
            end = p.get("to") or (p.get("x2"), p.get("y2"))
            sx, sy = point(*(start if isinstance(start, (list, tuple)) else (start.get("x"), start.get("y"))))
            ex, ey = point(*(end if isinstance(end, (list, tuple)) else (end.get("x"), end.get("y"))))
            gui.moveTo(sx, sy, duration=0.15)
            gui.dragTo(ex, ey, duration=0.4, button="left")
            return f"drag ({sx},{sy})->({ex},{ey})"

        if action.verb == "wait":
            seconds = min(5.0, float(p.get("seconds") or 1))
            time.sleep(seconds)
            return f"wait {seconds}s"

        raise UnsupportedAction(f"no handler for {action.verb!r}")

    def _stop_requested(self) -> str | None:
        if self.stop_file is not None and not self.stop_file.exists():
            return "stop file removed by operator"
        return None

    def _new_session(self, size: tuple[int, int], session_kwargs: dict | None) -> tuple[str | None, int]:
        """Open a session and return (id, credits charged)."""
        created = self.client.create_session(
            **{"screen_width": size[0], "screen_height": size[1], **(session_kwargs or {})}
        )
        # The documented field is `session_id`; `id` is accepted as a fallback
        # so a future rename does not hard-fail the loop.
        sid = created.get("session_id") or created.get("id")
        return sid, int((created.get("usage") or {}).get("credits_charged") or 0)

    def run(self, instruction: str, *, session_kwargs: dict | None = None) -> DriverResult:
        """Drive until the model says done, the cap is hit, or the operator stops."""
        from ap_desk.coasty import CoastyError

        result = DriverResult()
        _, size = self.capture()

        session_id, credits = self._new_session(size, session_kwargs)
        result.session_id = session_id
        result.credits += credits
        if not session_id:
            result.reason = "session create returned no session_id"
            return result

        # A session can disappear mid-run. Observed live: created fine, served
        # two steps, then SESSION_NOT_FOUND with zero sessions on the account.
        # Losing a 60-step run to that is not acceptable, so a lost session is
        # replaced and the step retried. The trajectory context is gone, but
        # the agent re-reads the screen every turn anyway, so it recovers.
        #
        # Bounded, because an unbounded retry against a genuinely broken
        # account would spend the whole budget re-creating sessions.
        max_recoveries = 3
        recoveries = 0

        try:
            for index in range(self.max_steps):
                stop = self._stop_requested()
                if stop:
                    result.reason = stop
                    return result

                png, captured = self.capture()
                step = Step(
                    index=index,
                    image=png,
                    sha256=hashlib.sha256(png).hexdigest(),
                    taken_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
                shot = base64.b64encode(png).decode("ascii")

                try:
                    raw = self.client.session_predict(
                        session_id,
                        screenshot=shot,
                        instruction=instruction,
                        include_reasoning=self.include_reasoning,
                    )
                except CoastyError as exc:
                    lost = exc.code == "SESSION_NOT_FOUND" or exc.status == 404
                    if not lost or recoveries >= max_recoveries:
                        raise
                    recoveries += 1
                    session_id, credits = self._new_session(captured, session_kwargs)
                    result.credits += credits
                    result.session_id = session_id
                    if not session_id:
                        result.reason = "could not reopen a session after it was lost"
                        return result
                    # Kept on the step itself, not just echoed to the console.
                    # A recovery that happened but left no trace in the record
                    # is indistinguishable afterwards from a run that never
                    # hit trouble, which is the wrong story to tell.
                    step.recovered = True
                    print(f"    session lost, reopened ({recoveries}/{max_recoveries})",
                          flush=True)
                    raw = self.client.session_predict(
                        session_id,
                        screenshot=shot,
                        instruction=instruction,
                        include_reasoning=self.include_reasoning,
                    )

                prediction: Prediction = parse_prediction(raw)
                step.raw = raw if isinstance(raw, dict) else None
                step.actions = prediction.actions
                step.reasoning = prediction.reasoning
                step.credits = prediction.credits
                result.credits += prediction.credits

                # The coordinate space to scale clicks against.
                #
                # Stateless /v1/predict echoes screen_width/screen_height; the
                # SESSION variant does not -- it returns null for both, because
                # the size was fixed at session-create time. Verified against a
                # live response. So the captured size is the authoritative
                # fallback, not a defensive nicety: without it every click on a
                # session run would be placed in an unknown space and refused.
                space = (
                    prediction.screen_width or captured[0],
                    prediction.screen_height or captured[1],
                )

                # Terminal status ends the run. Read `status`, never scan for a
                # `done` action: `done` carries no OS effect and a loop watching
                # for the action alone can sail past the end of the run.
                if prediction.is_terminal:
                    step.note = prediction.reasoning[:160] or prediction.status
                    result.steps.append(step)
                    self.on_step(step)
                    result.finished = prediction.status == "done"
                    result.reason = f"model reported {prediction.status}"
                    return result

                if not prediction.actions:
                    step.note = "no actions returned"
                    result.steps.append(step)
                    self.on_step(step)
                    result.reason = "model returned no actions and did not finish"
                    return result

                notes = []
                try:
                    for action in prediction.actions:
                        notes.append(self.perform(action, space))
                        if action.is_terminal:
                            break
                except UnsupportedAction as exc:
                    step.note = f"refused: {exc}"
                    result.steps.append(step)
                    self.on_step(step)
                    result.reason = str(exc)
                    return result

                step.note = "; ".join(notes)
                step.performed = not self.dry_run
                result.steps.append(step)
                self.on_step(step)
                time.sleep(self.settle_seconds)

            result.reason = f"step cap reached ({self.max_steps})"
            return result
        finally:
            try:
                self.client.delete_session(session_id)
            except Exception:  # noqa: BLE001 - cleanup must not mask the result
                pass
