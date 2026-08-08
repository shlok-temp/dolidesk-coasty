"""Action-schema tests, pinned to shapes observed from the live API.

Every fixture here was copied from a real response captured during a live
connectivity check, not invented from the prose docs. That distinction earned
its place: the first version of this driver was written from a plausible
reading of the documentation and got four things wrong at once -- `type` for
`action_type`, coordinates at the top level instead of under `params`, `id` for
`session_id`, and extra per-turn fields that the session endpoint rejects with a
422. All four would have failed against any key.

So these tests exist to make that class of mistake loud rather than silent.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ap_desk.actions import (  # noqa: E402
    VOCABULARY,
    Action,
    UnsupportedAction,
    describe,
    parse_action,
    parse_prediction,
    scale,
)

# Verbatim from POST /v1/predict, request req_5f1c45e836cd61dbaf029e7b.
LIVE_PREDICT = {
    "request_id": "req_5f1c45e836cd61dbaf029e7b",
    "status": "done",
    "reasoning": "",
    "actions": [{"action_type": "done", "params": {}, "description": "", "raw_code": ""}],
    "raw_code": [],
    "cua_version": "v5",
    "screen_width": 1280,
    "screen_height": 720,
    "usage": {
        "input_tokens": 1516,
        "output_tokens": 47,
        "credits_charged": 5,
        "cost_cents": 5,
        "billed": True,
        "breakdown": [{"item": "base", "credits": 5, "count": None}],
    },
}

LIVE_SESSION_PREDICT = {
    "request_id": "req_d036bf2f81e7658f43f4619d",
    "session_id": "ses_da644f70a12d4e5d9d072343d2268087",
    "step": 1,
    "status": "continue",
    "reasoning": "The worklist is on screen. Opening the first invoice.",
    "actions": [
        {
            "action_type": "click",
            "params": {"x": 512, "y": 340, "button": "left", "clicks": 1},
            "description": "Click invoice FA-2581",
            "raw_code": "",
        }
    ],
    "cua_version": "v5",
    "usage": {"credits_charged": 5, "cost_cents": 5},
}


class ParsesLiveShapes(unittest.TestCase):
    def test_parses_a_real_predict_response(self):
        p = parse_prediction(LIVE_PREDICT)
        self.assertEqual(p.status, "done")
        self.assertTrue(p.is_terminal)
        self.assertEqual(p.screen_width, 1280)
        self.assertEqual(p.credits, 5)
        self.assertEqual([a.verb for a in p.actions], ["done"])

    def test_parses_a_real_session_predict_response(self):
        p = parse_prediction(LIVE_SESSION_PREDICT)
        self.assertEqual(p.status, "continue")
        self.assertFalse(p.is_terminal)
        self.assertEqual(p.session_id, "ses_da644f70a12d4e5d9d072343d2268087")
        self.assertEqual(p.step, 1)
        action = p.actions[0]
        self.assertEqual(action.verb, "click")
        self.assertEqual(action.params["x"], 512)
        self.assertEqual(action.params["button"], "left")

    def test_the_verb_key_is_action_type_not_type(self):
        # The exact mistake that broke the first driver. `type` must NOT work.
        with self.assertRaises(UnsupportedAction):
            parse_action({"type": "click", "x": 10, "y": 20})

    def test_coordinates_are_not_read_from_the_top_level(self):
        a = parse_action({"action_type": "click", "x": 999, "y": 999,
                          "params": {"x": 10, "y": 20}})
        self.assertEqual((a.params["x"], a.params["y"]), (10, 20))

    def test_unknown_verbs_are_refused_not_approximated(self):
        with self.assertRaises(UnsupportedAction):
            parse_action({"action_type": "launch_missiles", "params": {}})

    def test_missing_params_becomes_an_empty_dict(self):
        # `done` legitimately arrives with no params; a KeyError here would end
        # every successful run with a crash instead of a result.
        self.assertEqual(parse_action({"action_type": "done"}).params, {})

    def test_unknown_response_keys_are_ignored(self):
        p = parse_prediction({**LIVE_PREDICT, "some_future_field": {"a": 1}})
        self.assertEqual(p.status, "done")


class Vocabulary(unittest.TestCase):
    def test_matches_the_documented_set(self):
        self.assertEqual(
            VOCABULARY,
            {"click", "type_text", "key_press", "key_combo", "scroll",
             "drag", "move", "wait", "done", "fail"},
        )

    def test_legacy_guesses_are_absent(self):
        # These were invented by the first draft. If one reappears in the
        # vocabulary, the driver has started accepting something the API will
        # never send, which hides a real schema drift.
        for wrong in ("type", "hotkey", "double_click", "right_click", "screenshot"):
            self.assertNotIn(wrong, VOCABULARY)


class Scaling(unittest.TestCase):
    def test_identity_when_spaces_agree(self):
        self.assertEqual(scale(100, 200, model_space=(1920, 1080), region=None), (100, 200))

    def test_scales_when_the_server_reports_a_different_space(self):
        # The server echoes the space it actually used, which may differ from
        # the image we sent. Scaling against the capture would misplace clicks.
        self.assertEqual(scale(640, 360, model_space=(1280, 720), region=(0, 0, 2560, 1440)),
                         (1280, 720))

    def test_adds_the_region_offset(self):
        self.assertEqual(scale(10, 10, model_space=(100, 100), region=(500, 300, 100, 100)),
                         (510, 310))

    def test_refuses_to_place_a_click_in_an_unknown_space(self):
        # Guessing here would put a real click at an arbitrary desktop point.
        with self.assertRaises(ValueError):
            scale(10, 10, model_space=(0, 0), region=None)


class Descriptions(unittest.TestCase):
    def test_long_typed_text_is_truncated_for_the_console(self):
        line = describe(Action("type_text", {"text": "x" * 200}))
        self.assertLess(len(line), 60)
        self.assertIn("...", line)

    def test_every_verb_renders_without_raising(self):
        samples = {
            "click": {"x": 1, "y": 2}, "move": {"x": 1, "y": 2},
            "type_text": {"text": "hi"}, "key_press": {"key": "enter"},
            "key_combo": {"keys": ["ctrl", "l"]}, "scroll": {"dy": 300},
            "drag": {"from": [1, 2], "to": [3, 4]}, "wait": {"seconds": 2},
            "done": {}, "fail": {},
        }
        for verb, params in samples.items():
            with self.subTest(verb=verb):
                self.assertTrue(describe(Action(verb, params)))

class SessionPredictOmitsScreenSpace(unittest.TestCase):
    """Verified live: the session variant returns no screen dimensions.

    Stateless /v1/predict echoes `screen_width`/`screen_height`; the session
    form returns neither, because the size was fixed at create time. The driver
    therefore MUST fall back to the captured size -- without it, `scale()`
    refuses to place a click and every session run dies on its first action.
    """

    def test_session_response_has_no_screen_space(self):
        p = parse_prediction({
            "status": "continue",
            "session_id": "ses_x",
            "step": 1,
            "actions": [{"action_type": "click", "params": {"x": 100, "y": 200}}],
            "usage": {"credits_charged": 5},
        })
        self.assertIsNone(p.screen_width)
        self.assertIsNone(p.screen_height)

    def test_the_captured_size_is_a_usable_fallback(self):
        captured = (1920, 1080)
        p = parse_prediction({"status": "continue", "actions": []})
        space = (p.screen_width or captured[0], p.screen_height or captured[1])
        self.assertEqual(scale(960, 540, model_space=space, region=None), (960, 540))

if __name__ == "__main__":
    unittest.main()
