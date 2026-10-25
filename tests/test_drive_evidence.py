#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The parked/moving verdict, and what counts as evidence of driving.

    python3 tests/test_drive_evidence.py
    python3 -m unittest discover -s tests -v

Why this exists. Issue #3 (capture of 2026-10-21): during a drive the
integration sent 45 minutes of passive queries and received only 309-byte
stubs, while the speed sensor and GPS tracker read `interval.drive.*` and the
classifier read `instant.drive.*`. The capture carried neither field, so it
could not say which one had decided the car was parked.

These tests pin the classifier's rules (they moved here from the coordinator,
unchanged, so they can be tested without Home Assistant) and the separate
question the escalation policy asks: was the car moving the last time it
reported? `parked is None` is not evidence of parking, and a positive
`interval` speed is evidence of driving even when the instant drive block is
missing - that same mismatch is what the capture could not resolve.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _component  # noqa: E402

COMPONENT = _component.component_dir()
_MODULE_PATH = _component.module_path("drive_evidence.py")
_spec = importlib.util.spec_from_file_location("drive_evidence_under_test", _MODULE_PATH)
assert _spec and _spec.loader
evidence = importlib.util.module_from_spec(_spec)
sys.modules["drive_evidence_under_test"] = evidence
_spec.loader.exec_module(evidence)


def _state(drive=None, *, interval=None):
    content = {"instant": {}}
    if drive is not None:
        content["instant"]["drive"] = drive
    if interval is not None:
        content["interval"] = {"drive": interval}
    return {"msg_content": content}


class TheVerdictRulesAreUnchanged(unittest.TestCase):
    """The rules that were inside XPengDataCoordinator.vehicle_parked."""

    def test_no_state_at_all_is_no_evidence(self):
        self.assertIsNone(evidence.view(None)["parked"])

    def test_a_missing_or_empty_drive_block_is_no_evidence(self):
        for state in (_state(), _state({})):
            self.assertIsNone(evidence.view(state)["parked"])

    def test_any_speed_above_zero_is_moving(self):
        self.assertFalse(evidence.view(_state({"speed": 20}))["parked"])
        self.assertFalse(evidence.view(_state({"speed": 0.5}))["parked"])
        self.assertFalse(evidence.view(_state({"speed": "20"}))["parked"])

    def test_speed_zero_falls_through_to_the_shift(self):
        self.assertTrue(evidence.view(_state({"speed": 0, "shift_state": "P"}))["parked"])
        self.assertFalse(evidence.view(_state({"speed": 0, "shift_state": "D"}))["parked"])

    def test_shift_strings(self):
        for shift, parked in (("P", True), ("p", True), (" P ", True),
                              ("D", False), ("R", False), ("N", False)):
            self.assertIs(
                evidence.view(_state({"shift_state": shift}))["parked"], parked, shift
            )

    def test_shift_codes(self):
        for shift, parked in ((3, True), (4, True), (5, False), (2, False)):
            self.assertIs(
                evidence.view(_state({"shift_state": shift}))["parked"], parked, shift
            )

    def test_a_missing_shift_is_no_evidence_not_parking(self):
        self.assertIsNone(evidence.view(_state({"speed": 0}))["parked"])

    def test_an_unreadable_shift_is_no_evidence(self):
        self.assertIsNone(evidence.view(_state({"shift_state": None}))["parked"])

    def test_an_unreadable_speed_still_falls_through(self):
        view = evidence.view(_state({"speed": "n/a", "shift_state": "P"}))
        self.assertTrue(view["parked"])

    def test_the_reason_is_recorded(self):
        self.assertIn("speed", evidence.view(_state({"speed": 20}))["reason"])
        self.assertIn("shift_state", evidence.view(_state({"shift_state": "P"}))["reason"])
        self.assertIn("no instant.drive", evidence.view(_state())["reason"])


class WhatCountsAsMotionEvidence(unittest.TestCase):
    """The question the escalation policy asks: was it moving when it stopped?"""

    def test_moving_when_the_instant_speed_says_so(self):
        self.assertTrue(evidence.view(_state({"speed": 30}))["moving"])

    def test_moving_when_the_shift_says_so(self):
        self.assertTrue(evidence.view(_state({"shift_state": "D"}))["moving"])

    def test_not_moving_when_parked(self):
        view = evidence.view(_state({"speed": 0, "shift_state": "P"}))
        self.assertFalse(view["moving"])

    def test_no_evidence_is_not_motion(self):
        self.assertFalse(evidence.view(_state())["moving"])

    def test_interval_speed_is_evidence_even_without_an_instant_drive_block(self):
        """The issue #3 shape: classifier has nothing, the car's speed field says 30."""
        view = evidence.view(_state(interval={"speed": 30}))
        self.assertIsNone(view["parked"])
        self.assertTrue(view["moving"])

    def test_interval_speed_of_zero_is_not_motion(self):
        view = evidence.view(_state(interval={"speed": 0}))
        self.assertFalse(view["moving"])

    def test_a_string_interval_speed_is_read(self):
        self.assertTrue(evidence.view(_state(interval={"speed": "30.5"}))["moving"])

    def test_motion_wins_over_a_contradictory_parked_verdict(self):
        view = evidence.view(_state({"shift_state": "P"}, interval={"speed": 12}))
        self.assertTrue(view["parked"])
        self.assertTrue(view["moving"])


class GpsIsReportedByPresenceOnly(unittest.TestCase):
    def test_coordinates_come_from_the_interval_block(self):
        view = evidence.view(_state(interval={"latitude": -32.0, "longitude": 152.0}))
        self.assertEqual(view["gps"], (-32.0, 152.0))

    def test_half_a_position_is_not_a_position(self):
        self.assertIsNone(evidence.view(_state(interval={"latitude": -32.0}))["gps"])

    def test_no_interval_block_means_no_position(self):
        self.assertIsNone(evidence.view(_state({"speed": 30}))["gps"])


class GpsFixValidity(unittest.TestCase):
    """`coordValid` says whether the coordinates are current - honour it."""

    def test_validity_encodings(self):
        cases = ((1, True), ("1", True), ("true", True), ("valid", True),
                 (0, False), ("0", False), ("false", False), ("", False))
        for value, expected in cases:
            with self.subTest(value=value):
                frame = _state(interval={"coordValid": value})
                self.assertIs(evidence.coord_valid(frame), expected)

    def test_a_missing_or_unreadable_flag_is_unknown(self):
        self.assertIsNone(evidence.coord_valid(_state(interval={"speed": 5})))
        self.assertIsNone(evidence.coord_valid(_state(interval={"coordValid": "maybe"})))
        self.assertIsNone(evidence.coord_valid(None))

    def test_the_view_carries_the_flag(self):
        self.assertIs(
            evidence.view(_state(interval={"coordValid": 0}))["coord_valid"], False
        )

    def test_zero_is_not_a_position(self):
        self.assertIsNone(
            evidence.position(_state(interval={"latitude": 0, "longitude": 0}))
        )
        self.assertEqual(
            evidence.position(_state(interval={"latitude": -32.1, "longitude": 152.5})),
            (-32.1, 152.5),
        )


class AnUnusableFixIsNotPublished(unittest.TestCase):
    """The G9 report: a car showing its old position as if it were current."""

    previous = _state(interval={"latitude": -32.07, "longitude": 152.48, "speed": 0})

    def test_an_invalid_frame_keeps_the_last_valid_position(self):
        frame = _state(
            interval={"coordValid": 0, "latitude": -33.0, "longitude": 151.0, "speed": 42}
        )
        repaired = evidence.with_previous_fix(frame, self.previous)
        drive = repaired["msg_content"]["interval"]["drive"]
        self.assertEqual((drive["latitude"], drive["longitude"]), (-32.07, 152.48))
        # The rest of the frame is untouched: only the position is held.
        self.assertEqual(drive["speed"], 42)

    def test_with_no_previous_fix_the_coordinates_are_dropped(self):
        frame = _state(interval={"coordValid": 0, "latitude": -33.0, "longitude": 151.0})
        repaired = evidence.with_previous_fix(frame, None)
        drive = repaired["msg_content"]["interval"]["drive"]
        self.assertNotIn("latitude", drive)
        self.assertNotIn("longitude", drive)

    def test_a_valid_frame_passes_through_untouched(self):
        frame = _state(interval={"coordValid": 1, "latitude": -33.0, "longitude": 151.0})
        self.assertIs(evidence.with_previous_fix(frame, self.previous), frame)

    def test_a_frame_that_never_mentions_validity_is_left_alone(self):
        frame = _state(interval={"latitude": -33.0, "longitude": 151.0})
        self.assertIs(evidence.with_previous_fix(frame, self.previous), frame)

    def test_zero_coordinates_carry_the_previous_fix(self):
        frame = _state(interval={"latitude": 0, "longitude": 0})
        repaired = evidence.with_previous_fix(frame, self.previous)
        drive = repaired["msg_content"]["interval"]["drive"]
        self.assertEqual((drive["latitude"], drive["longitude"]), (-32.07, 152.48))

    def test_a_frame_without_an_interval_block_is_left_alone(self):
        frame = {"msg_content": {"instant": {"drive": {"speed": 0}}}}
        self.assertIs(evidence.with_previous_fix(frame, self.previous), frame)


class TheFixTracker(unittest.TestCase):
    """Age is the whole point: a held position must not look fresh."""

    def test_a_valid_fix_records_position_and_time(self):
        tracker = evidence.FixTracker()
        tracker.observe(
            _state(interval={"coordValid": 1, "latitude": -32.1, "longitude": 152.5}),
            now=1000.0,
        )
        self.assertEqual(tracker.position, (-32.1, 152.5))
        self.assertIs(tracker.coord_valid, True)
        self.assertEqual(tracker.age_seconds(1060.0), 60.0)

    def test_an_invalid_frame_does_not_make_the_fix_younger(self):
        tracker = evidence.FixTracker()
        tracker.observe(
            _state(interval={"coordValid": 1, "latitude": -32.1, "longitude": 152.5}),
            now=1000.0,
        )
        tracker.observe(
            _state(interval={"coordValid": 0, "latitude": -32.1, "longitude": 152.5}),
            now=2000.0,
        )
        self.assertIs(tracker.coord_valid, False)
        self.assertEqual(tracker.age_seconds(2000.0), 1000.0)

    def test_a_restored_position_with_no_timestamp_has_no_age(self):
        tracker = evidence.FixTracker()
        tracker.observe(_state(interval={"latitude": -32.1, "longitude": 152.5}))
        self.assertEqual(tracker.position, (-32.1, 152.5))
        self.assertIsNone(tracker.age_seconds(2000.0))

    def test_a_frame_without_a_position_leaves_the_held_fix_alone(self):
        tracker = evidence.FixTracker()
        tracker.observe(
            _state(interval={"latitude": -32.1, "longitude": 152.5}), now=1000.0
        )
        tracker.observe(_state(interval={"speed": 72}), now=1001.0)
        self.assertEqual(tracker.position, (-32.1, 152.5))
        self.assertEqual(tracker.age_seconds(1100.0), 100.0)


class TheRestFallbackSaysTheCarMoved(unittest.TestCase):
    """Issue #3, 2026-10-23: REST odometer +67 km while the push fan-out wedged.

    The fallback merge is the only source that can say "the car is moving" when
    the state topic is silent, and the watchdog's bounded motion escalation is
    what turns that into one force-upload instead of 426 minutes (or a 12 h
    wait) of frozen telemetry.
    """

    @staticmethod
    def _held(odometer=62234):
        return {"msg_content": {"instant": {"odometer": {"total_mileage": odometer}}}}

    def test_an_odometer_ahead_of_the_held_state_is_evidence(self):
        is_moving, reason = evidence.rest_motion_evidence(
            {"odometer": {"total_mileage": 62301}}, self._held(62234)
        )
        self.assertTrue(is_moving)
        self.assertIn("67", reason)

    def test_an_equal_odometer_is_not_evidence(self):
        self.assertEqual(
            evidence.rest_motion_evidence(
                {"odometer": {"total_mileage": 62234}}, self._held(62234)
            ),
            (False, None),
        )

    def test_rounding_noise_is_not_evidence(self):
        self.assertFalse(
            evidence.rest_motion_evidence(
                {"odometer": {"total_mileage": 62233.6}}, self._held(62234)
            )[0]
        )

    def test_a_gear_that_is_not_park_is_evidence(self):
        for shift in (0, 1, 2, "D", "R", "N"):  # N, D, R ints and strings
            with self.subTest(shift=shift):
                is_moving, reason = evidence.rest_motion_evidence(
                    {"drive": {"shift_state": shift}}, self._held()
                )
                self.assertTrue(is_moving)
                self.assertIn("not P", reason)

    def test_park_is_not_evidence(self):
        for shift in (3, 4, "P", "p"):
            with self.subTest(shift=shift):
                self.assertEqual(
                    evidence.rest_motion_evidence(
                        {"drive": {"shift_state": shift}}, self._held()
                    ),
                    (False, None),
                )

    def test_vehicle_mode_driving_is_evidence(self):
        is_moving, reason = evidence.rest_motion_evidence(
            {"ext": {"vehicleMode": 2}}, self._held()
        )
        self.assertTrue(is_moving)
        self.assertIn("Driving", reason)

    def test_standby_is_not_evidence(self):
        """1 is Standby: the car is awake but parked, so the budget still applies."""
        self.assertEqual(
            evidence.rest_motion_evidence(
                {"ext": {"vehicleMode": 1}}, self._held()
            ),
            (False, None),
        )

    def test_missing_or_unreadable_snapshots_are_no_evidence(self):
        for widget in ({}, None, {"drive": {"shift_state": None}}, {"ext": {}}):
            with self.subTest(widget=widget):
                self.assertFalse(evidence.rest_motion_evidence(widget, self._held())[0])
        self.assertFalse(
            evidence.rest_motion_evidence(
                {"odometer": {"total_mileage": 62301}}, None
            )[0]
        )


class TheIntegrationActuallyUsesIt(unittest.TestCase):
    """Pinned at the call sites, so the module cannot become dead code."""

    def test_the_coordinator_classifies_and_tracks_the_fix_through_it(self):
        coordinator = (COMPONENT / "coordinator.py").read_text(encoding="utf-8")
        self.assertIn("view as drive_view", coordinator)
        self.assertIn("FixTracker", coordinator)
        self.assertIn("with_previous_fix", coordinator)
        self.assertIn('return drive_view(self._last_full_data)["parked"]', coordinator)
        self.assertIn("def parking_view", coordinator)
        self.assertIn("self.note_drive_evidence(data, fresh_drive=fresh_drive)", coordinator)
        self.assertIn("data = with_previous_fix(data, self._last_full_data)", coordinator)
        self.assertIn("self.gps_fix.observe(", coordinator)

    def test_the_tracker_exposes_fix_quality_and_the_log_records_it(self):
        tracker = (COMPONENT / "device_tracker.py").read_text(encoding="utf-8")
        self.assertIn("coordinator.gps_fix.position", tracker)
        self.assertIn('"coord_valid"', tracker)
        self.assertIn('"fix_age_s"', tracker)
        init = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("Drive evidence: parked=%s", init)
        self.assertIn("coord_valid=%r", init)
        self.assertIn("should_escalate_moving(", init)
        self.assertIn("coordinator.last_motion_at", init)
        self.assertIn("Periodic query: kind=%s", init)


if __name__ == "__main__":
    unittest.main(verbosity=2)
