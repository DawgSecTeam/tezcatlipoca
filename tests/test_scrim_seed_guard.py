"""The pre-T0 assume-breach seed must be believed only when the whole range was seeded.

bad-auto's `seed` reports every box failed and still exits 0 (caught live: run-1ea5494a-run2
and run-2652d9bc), so `seed_assume_breach` gates on its per-step stdout report as well as on the
exit code, and refuses T0 on output it cannot interpret rather than passing silently. The current
bad-auto also prints a `steps` tally (counted over the persistent world, so an idempotent re-run
reports full numbers) and an `ok` flag; older output has only the per-pass box lists.
"""

import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from red import red_plant_ops
from scrim import red_stage


def _report(**overrides):
    base = {"depth": 2, "boxes": 2, "access": [], "rooted": [], "implants": [],
            "artifacts": [], "evasion": [], "failed": []}
    base.update(overrides)
    return base


def _steps(access=0, **rest):
    return {"access": access, "root": 0, "implant": 0, "artifacts": 0, "evasion": 0, **rest}


class SeedReportParsing(unittest.TestCase):

    def test_a_pretty_printed_report_parses(self):
        text = json.dumps(_report(boxes=6, failed=[
            {"ip": "192.168.150.3", "step": "access",
             "detail": "192.168.150.3 unreachable over SSH"}]), indent=2)
        report = red_stage.seed_report(text)
        self.assertEqual(report["boxes"], 6)
        self.assertEqual(report["failed"][0]["step"], "access")

    def test_a_report_behind_other_output_parses(self):
        text = "loading intel...\n" + json.dumps(_report(boxes=1, access=["10.0.0.5"]))
        self.assertEqual(red_stage.seed_report(text)["access"], ["10.0.0.5"])

    def test_output_that_is_not_a_report_is_none(self):
        for text in ("", "seeding access on every box\n", "{not json", '{"unrelated": 1}'):
            self.assertIsNone(red_stage.seed_report(text), text)


class SeedVerdict(unittest.TestCase):

    def test_full_steps_tally_passes_even_with_empty_per_pass_lists(self):
        # the idempotent re-run: nothing to do this pass, every persistent record satisfied
        boxes, accessed, refusal = red_stage.seed_verdict(
            _report(boxes=6, steps=_steps(access=6), ok=True))
        self.assertIsNone(refusal)
        self.assertEqual((boxes, accessed), (6, 6))

    def test_a_partial_seed_is_refused_with_the_counts_and_the_failed_boxes(self):
        boxes, accessed, refusal = red_stage.seed_verdict(_report(boxes=6, steps=_steps(access=5),
                                                                  ok=True, failed=[
            {"ip": "192.168.151.3", "step": "access",
             "detail": "192.168.151.3 unreachable over SSH"}]))
        self.assertEqual((boxes, accessed), (6, 5))
        self.assertIn("5 of 6", refusal)
        self.assertIn("bad-auto's steps tally", refusal)
        self.assertIn("192.168.151.3", refusal)

    def test_a_zero_seed_is_refused(self):
        boxes, accessed, refusal = red_stage.seed_verdict(_report(
            boxes=6, steps=_steps(), ok=False, failed=[
                {"ip": "192.168.150.3", "step": "access", "detail": "unreachable over SSH"}]))
        self.assertEqual((boxes, accessed), (6, 0))
        self.assertIn("0 of 6", refusal)

    def test_no_boxes_looked_at_is_refused(self):
        _, _, refusal = red_stage.seed_verdict(_report(boxes=0, steps=_steps(), ok=False))
        self.assertIn("looked at no box", refusal)

    def test_the_older_per_pass_shape_still_decides(self):
        _, accessed, refusal = red_stage.seed_verdict(
            _report(boxes=2, access=["10.0.0.5", "10.0.0.6"]))
        self.assertEqual(accessed, 2)
        self.assertIsNone(refusal)
        _, _, refusal = red_stage.seed_verdict(_report(boxes=2, access=["10.0.0.5"]))
        self.assertIn("the per-pass box lists", refusal)
        self.assertIn("1 of 2", refusal)


class SeedRefusesT0(unittest.TestCase):

    def _args(self, **overrides):
        args = SimpleNamespace(seed_red=True, seed_depth=2, red_ip="10.0.0.196")
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def _seed(self, stdout, returncode=0, args=None):
        proc = subprocess.CompletedProcess(["ssh"], returncode, stdout, "")
        with mock.patch.object(red_plant_ops, "seed_via_red01", return_value=proc):
            red_stage.seed_assume_breach(args or self._args())

    def test_every_box_unreachable_refuses_t0_and_names_the_boxes(self):
        report = _report(boxes=2, steps=_steps(), ok=False, failed=[
            {"ip": "192.168.150.3", "step": "access",
             "detail": "192.168.150.3 unreachable over SSH"},
            {"ip": "192.168.151.3", "step": "access",
             "detail": "192.168.151.3 unreachable over SSH"}])
        with self.assertRaises(RuntimeError) as raised:
            self._seed(json.dumps(report))
        msg = str(raised.exception)
        self.assertIn("0 of 2", msg)
        self.assertIn("192.168.150.3", msg)
        self.assertIn("192.168.151.3", msg)
        self.assertIn("unreachable over SSH", msg)
        self.assertIn("--no-seed-red", msg)

    def test_output_that_cannot_be_interpreted_refuses_rather_than_passing(self):
        with self.assertRaises(RuntimeError) as raised:
            self._seed("seeding access + beacons + persistence\n")
        self.assertIn("printed no per-step report", str(raised.exception))

    def test_a_nonzero_exit_still_refuses(self):
        with self.assertRaises(RuntimeError) as raised:
            self._seed(json.dumps(_report(steps=_steps(access=2), ok=True)), returncode=1)
        self.assertIn("rc=1", str(raised.exception))

    def test_an_already_seeded_re_run_starts_the_event(self):
        self._seed(json.dumps(_report(boxes=2, steps=_steps(access=2), ok=True)))

    def test_a_partial_seed_refuses_even_though_ok_is_true(self):
        report = _report(boxes=2, steps=_steps(access=1), ok=True, failed=[
            {"ip": "192.168.151.3", "step": "access", "detail": "unreachable over SSH"}])
        with self.assertRaises(RuntimeError) as raised:
            self._seed(json.dumps(report))
        msg = str(raised.exception)
        self.assertIn("1 of 2", msg)
        self.assertIn("192.168.151.3", msg)

    def test_no_seed_red_skips_the_guard_entirely(self):
        args = self._args(seed_red=False)
        with mock.patch.object(red_plant_ops, "seed_via_red01") as seed:
            red_stage.seed_assume_breach(args)
        seed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
