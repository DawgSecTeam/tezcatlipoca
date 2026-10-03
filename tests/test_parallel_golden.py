"""build_golden_set's per-box passes run on a bounded pool — without weakening the
boot-smoke gate or the snapshot/conversion ordering.

Why this file exists: the SNAP_BASE snapshot loop and the "delete snapshot, POST
/template" conversion loop were serial, so an 8-box-type golden build paid ~60-100s of
snapshot wall plus ~30-60s of conversion wall for work that is independent per box.
Measured snapshot wall across 13 competitions (.deploy-timings.jsonl, 460 successful
records, 16 sub-second warn-path records excluded): median 7.4s, mean 12.1s, p95 33.8s,
max 103.3s — bench-parallel-2026-09-24 phase 4 alone was 12 snapshots / 135s. deploy.py
has run this exact op on a 4-worker pool since M2.1.

What a concurrent rewrite can silently break is not speed but ordering and failure
semantics, so that is what these tests pin:
  * a failing boot smoke must still prevent EVERY POST /template (the boot-smoke gate
    the 2026-09-24 systemd-system-masked incident exists for);
  * every SNAP_BASE snapshot must exist before the conversion loop deletes them;
  * within a box, the snapshot is deleted before that box's POST /template (qm template
    refuses a VM holding snapshots);
  * full-clone loops stay SERIAL (crash-consistent copies / datastore saturation);
  * a failure aborts with the serial loop's exact message, and the warn-and-continue
    paths stay warn-and-continue.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import golden_ops  # noqa: E402
from constants import SNAP_BASE  # noqa: E402
from nakon_ops import NakonResult  # noqa: E402


def _proc(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _serial_run_concurrent(items, fn, max_workers=8):
    """The pre-W11 shape: one item at a time, collecting exceptions per slot exactly as
    utils.run_concurrent would. Used as the control that proves the wall-time threshold
    actually discriminates a parallel implementation from a re-serialised one."""
    out = []
    for item in items:
        try:
            out.append(fn(item))
        except Exception as exc:  # noqa: BLE001 - mirrors run_concurrent's contract
            out.append(exc)
    return out


class _Events:
    """Thread-safe op log plus peak-concurrency counters for the patched ops."""

    def __init__(self):
        self.lock = threading.Lock()
        self.log = []
        self.active = {}
        self.peak = {}

    def record(self, kind, key, sleep=0.0):
        with self.lock:
            self.log.append((kind, key))
            self.active[kind] = self.active.get(kind, 0) + 1
            self.peak[kind] = max(self.peak.get(kind, 0), self.active[kind])
        if sleep:
            time.sleep(sleep)
        with self.lock:
            self.active[kind] -= 1

    def kinds(self):
        return [k for k, _ in self.log]

    def index_of(self, kind):
        return [i for i, (k, _) in enumerate(self.log) if k == kind]

    def keys(self, kind):
        return [key for k, key in self.log if k == kind]


class GoldenBuildHarness:
    """Runs the real build_golden_set with every remote op faked and the interesting
    ones instrumented. `n` booted box types, all Linux, all already-planted (so the
    clone loops are skipped) unless `missing=True`."""

    def __init__(self, n=8, snapshot_sleep=0.0, template_sleep=0.0, clone_sleep=0.0,
                 missing=False, unbooted=(), smoke_raises=(), template_raises=(),
                 password_fail=(), snapshot_false=(), run_concurrent=None,
                 golden_hashes=None, snapshots_empty=(), all_templates=False,
                 slot=0, anchor=None):
        self.n = n
        self.snapshot_sleep = snapshot_sleep
        self.template_sleep = template_sleep
        self.clone_sleep = clone_sleep
        self.missing = missing
        self.unbooted = set(unbooted)
        self.smoke_raises = set(smoke_raises)
        self.template_raises = set(template_raises)
        self.password_fail = set(password_fail)
        self.snapshot_false = set(snapshot_false)
        self.run_concurrent = run_concurrent
        self.golden_hashes = golden_hashes
        self.snapshots_empty = {str(v) for v in snapshots_empty}
        self.all_templates = all_templates
        self.slot = slot
        self.anchor = anchor
        self.events = _Events()
        # Capturing recorder for the coverage hook — build_golden_set invokes it on
        # every completion path, and the tests below assert on what it captured.
        self.coverage_calls = []
        self.tmp = tempfile.TemporaryDirectory()
        self.comp_dir = Path(self.tmp.name)
        self.boxes = [{"name": f"box{i}", "template": "base-linux"} for i in range(n)]
        self.vmids = [golden_ops.golden_vmid_for(1000, i) for i in range(n)]
        self.ips = [golden_ops.golden_ip_for("104", i) for i in range(n)]
        # A golden stage with no plantable configurations skips the nakon plant
        # outright (the score-only lineup path) — no nakon in these tests.
        self.golden_config = self.comp_dir / "golden-config.json"
        self.golden_config.write_text(json.dumps(
            {"machines": [{"name": f"box{i}-golden", "configurations": []}
                          for i in range(n)]}))

    # ---------------------------------------------------------------- fakes
    def _ssh(self, ctx, ip, cmd, timeout=60, user=None):
        if ip in self.password_fail and "chpasswd" in cmd:
            return _proc(1, "", "chpasswd: Authentication token manipulation error")
        return _proc(0, "JOINED=1\n")

    def _proxmox(self, method, path, **kwargs):
        parts = path.strip("/").split("/")
        vmid = parts[3] if len(parts) > 3 else "?"
        if method == "POST" and path.endswith("/template"):
            if vmid in self.template_raises:
                self.events.record("template", vmid)
                raise RuntimeError(f"Proxmox API POST {path} failed: 500 converting")
            self.events.record("template", vmid, sleep=self.template_sleep)
            return {"data": "UPID:test"}
        if method == "POST" and path.endswith("/clone"):
            newid = str(kwargs.get("data", {}).get("newid", "?"))
            self.events.record("clone", newid, sleep=self.clone_sleep)
            return {"data": "UPID:test"}
        if method == "GET" and path.endswith("/qemu"):
            return {"data": []}
        return {"data": {}}

    def _snapshot(self, node, vmid, name, description="", timeout=900):
        if str(vmid) in self.snapshot_false:
            return False  # take_snapshot's warn-and-continue path
        self.events.record("snapshot", str(vmid), sleep=self.snapshot_sleep)
        return True

    def _smoke(self, node, target, ctx, comp_dir, timeout=None, poll=0, planted_configs=(), run_id=None):
        name = target["box"]["name"]
        self.events.record("smoke", name)
        if name in self.smoke_raises:
            raise RuntimeError(
                f"golden boot smoke FAILED (verified-unbootable) for box type '{name}': "
                f"the throwaway clone did not reach multi-user — refusing to convert")
        return True

    # ---------------------------------------------------------------- driver
    @contextlib.contextmanager
    def _patched(self):
        rc = self.run_concurrent or golden_ops.run_concurrent
        with contextlib.ExitStack() as stack:
            def P(name, **kw):
                stack.enter_context(patch.object(golden_ops, name, **kw))
            P("_template_vmid_map", return_value={"base-linux": 900})
            P("_vm_exists", return_value=not self.missing)
            P("_is_template", return_value=self.all_templates)
            P("list_snapshots",
              side_effect=lambda _node, vmid: set() if str(vmid) in self.snapshots_empty
              else {SNAP_BASE})
            P("rollback_snapshot",
              side_effect=lambda _n, vmid, _s: self.events.record("rollback", str(vmid)))
            P("retag_ownership")
            P("destroy_vm_if_exists")
            P("gc_orphan_volumes")
            P("wait_for_proxmox_task")
            P("ensure_golden_disk_size")
            P("write_template_hash")
            P("start_vm")
            P("stop_vm")
            P("ensure_nat_forwarding")
            P("wait_for_boxes_ssh")
            P("wait_for_cloud_init")
            P("setup_ubuntu_auth")
            P("expand_guest_root_disks")
            P("fix_dns_on_boxes")
            P("prep_apt_on_boxes")
            P("ssh_via_gateway", side_effect=self._ssh)
            P("proxmox_api", side_effect=self._proxmox)
            P("take_snapshot", side_effect=self._snapshot)
            P("delete_snapshot",
              side_effect=lambda node, vmid, name, **kw: self.events.record("delete", str(vmid)))
            P("golden_boot_smoke", side_effect=self._smoke)
            P("run_concurrent", side_effect=rc)
            stack.enter_context(patch.dict(os.environ, {
                "TF_VAR_ssh_public_key": "ssh-rsa AAAA test", "TF_VAR_proxmox_node": "pve"}))
            yield

    def run(self):
        """Returns (result, events, exception)."""
        coverage = (lambda machines, result:
                    self.coverage_calls.append((machines, result)))
        with self._patched(), contextlib.redirect_stdout(io.StringIO()):
            try:
                result = golden_ops.build_golden_set(
                    "pve", {"team1": {"identifier": 104}}, self.boxes, {"box_username": "ubuntu"},
                    self.comp_dir, 1000, "Box-Pass-1", self.golden_config,
                    "test-key", "scoring", "10.0.0.1", unbooted=self.unbooted,
                    golden_hashes=self.golden_hashes, slot=self.slot,
                    anchor_identifier=self.anchor, coverage=coverage)
                return result, self.events, None
            except Exception as exc:  # noqa: BLE001 - tests assert on what escapes
                return None, self.events, exc


class GoldenParallelism(unittest.TestCase):
    def test_snapshots_and_conversions_overlap(self):
        """A serialised implementation would fail this: 8 x 0.2s in EACH of the two
        loops is a 3.2s floor; a 4-worker pool over 8 units is ~0.8s."""
        harness = GoldenBuildHarness(n=8, snapshot_sleep=0.2, template_sleep=0.2)
        started = time.monotonic()
        result, events, exc = harness.run()
        elapsed = time.monotonic() - started

        self.assertIsNone(exc)
        self.assertEqual(len(result), 8)
        # Generous margin for a loaded CI box, still far below the serial floor.
        self.assertLess(elapsed, 1.8,
                        f"per-box passes did not overlap ({elapsed:.2f}s for 8x0.2s x2)")
        self.assertGreater(events.peak.get("snapshot", 0), 1, "snapshots were not concurrent")
        self.assertGreater(events.peak.get("template", 0), 1, "conversions were not concurrent")

    def test_reserialised_control_exceeds_the_threshold(self):
        """The control that gives the threshold teeth: with run_concurrent replaced by a
        serial runner (the pre-W11 shape) the same build must blow the 1.8s threshold.
        This is the executable form of 'verified to FAIL against a re-serialised
        implementation'."""
        harness = GoldenBuildHarness(n=8, snapshot_sleep=0.2, template_sleep=0.2,
                                     run_concurrent=_serial_run_concurrent)
        started = time.monotonic()
        _, events, exc = harness.run()
        elapsed = time.monotonic() - started

        self.assertIsNone(exc)
        self.assertGreater(elapsed, 1.8,
                           f"serial control came in under the parallel threshold "
                           f"({elapsed:.2f}s) — the timing test proves nothing")
        self.assertEqual(events.peak.get("snapshot"), 1)
        self.assertEqual(events.peak.get("template"), 1)

    def test_pool_is_bounded_at_four(self):
        """8 box types must not open 8 concurrent converting Proxmox tasks."""
        harness = GoldenBuildHarness(n=8, snapshot_sleep=0.02, template_sleep=0.02)
        _, events, exc = harness.run()
        self.assertIsNone(exc)
        self.assertLessEqual(events.peak.get("snapshot"), 4, "snapshot pool exceeded its bound")
        self.assertLessEqual(events.peak.get("template"), 4, "conversion pool exceeded its bound")

    def test_failed_boot_smoke_blocks_every_template_conversion(self):
        """The gate the 2026-09-24 systemd-system-masked incident exists for: one
        unbootable golden must stop the whole conversion pass, not just its own."""
        harness = GoldenBuildHarness(n=8, smoke_raises={"box3"})
        result, events, exc = harness.run()

        self.assertIsInstance(exc, RuntimeError)
        self.assertIn("refusing to convert", str(exc))
        self.assertEqual(events.keys("template"), [],
                         "a POST /template happened despite a failed boot smoke")
        # The smoke pass itself is deliberately still serial and stop-on-first-failure
        # (it boots one throwaway guest at a time), so box3 aborts it at the 4th box.
        self.assertEqual(events.kinds().count("smoke"), 4)
        self.assertIsNone(result)

    def test_every_smoke_runs_before_any_conversion(self):
        harness = GoldenBuildHarness(n=8)
        _, events, exc = harness.run()
        self.assertIsNone(exc)
        smoked = events.index_of("smoke")
        converted = events.index_of("template")
        self.assertEqual(len(smoked), 8)
        self.assertEqual(len(converted), 8)
        self.assertLess(max(smoked), min(converted),
                        "a conversion ran before all boot smokes finished")

    def test_all_snapshots_exist_before_conversion_starts(self):
        """SNAP_BASE is the pre-plant rollback guard; the pool joins before the plant
        even begins, so no conversion can race a snapshot."""
        harness = GoldenBuildHarness(n=8, snapshot_sleep=0.1)
        _, events, exc = harness.run()
        self.assertIsNone(exc)
        snapped = events.index_of("snapshot")
        converted = events.index_of("template")
        self.assertEqual(len(snapped), 8)
        self.assertLess(max(snapped), min(converted),
                        "a conversion started before every snapshot existed")

    def test_snapshot_is_deleted_before_its_template_post(self):
        """qm template refuses a VM holding snapshots — per box, delete must come first."""
        harness = GoldenBuildHarness(n=8)
        _, events, exc = harness.run()
        self.assertIsNone(exc)
        deletes = {vmid: i for i, (k, vmid) in enumerate(events.log) if k == "delete"}
        templates = {vmid: i for i, (k, vmid) in enumerate(events.log) if k == "template"}
        self.assertEqual(set(deletes), set(templates))
        for vmid, idx in templates.items():
            self.assertLess(deletes[vmid], idx,
                            f"vmid {vmid} was converted before its SNAP_BASE was deleted")

    def test_cold_dc_golden_short_circuits_before_the_smoke(self):
        """An unbooted (DC) golden is generalized and converted in the cold pass — it
        must not be snapshotted, started, or smoke-tested."""
        harness = GoldenBuildHarness(n=4, unbooted={"box0"})
        result, events, exc = harness.run()
        self.assertIsNone(exc)
        self.assertEqual(len(result), 4)
        self.assertNotIn("box0", events.keys("smoke"))
        self.assertEqual(events.kinds().count("smoke"), 3)
        # box0 was converted in the cold pass, and no snapshot was taken for it.
        cold_vmid = str(result["box0"])
        self.assertIn(cold_vmid, events.keys("template"))
        self.assertNotIn(cold_vmid, events.keys("snapshot"))

    def test_full_clone_loop_stays_serial(self):
        """Full clones are crash-consistent disk copies onto one datastore — deploy.py
        runs terraform at -parallelism=1 for the same reason. Nothing here may fan out."""
        harness = GoldenBuildHarness(n=8, missing=True, clone_sleep=0.05)
        _, events, exc = harness.run()
        self.assertIsNone(exc)
        self.assertEqual(len(events.keys("clone")), 8)
        self.assertEqual(events.peak.get("clone"), 1, "full clones were made concurrent")

    def test_conversion_failure_aborts_the_build(self):
        harness = GoldenBuildHarness(n=8, template_raises={str(golden_ops.golden_vmid_for(1000, 4))})
        _, events, exc = harness.run()
        self.assertIsInstance(exc, RuntimeError)
        self.assertIn("500 converting", str(exc))

    def test_snapshot_warn_path_still_continues(self):
        """take_snapshot never raises (it warns and returns False); the parallel pass
        must keep that warn-and-continue behaviour, not turn it into an abort."""
        harness = GoldenBuildHarness(
            n=8, snapshot_false={str(golden_ops.golden_vmid_for(1000, 2))})
        result, events, exc = harness.run()
        self.assertIsNone(exc)
        self.assertEqual(len(result), 8)
        self.assertEqual(len(events.keys("template")), 8)

    def test_password_reset_failure_keeps_the_serial_message(self):
        harness = GoldenBuildHarness(n=4, password_fail={golden_ops.golden_ip_for("104", 1)})
        _, _, exc = harness.run()
        self.assertIsInstance(exc, RuntimeError)
        self.assertIn("password reset failed on golden 192.168.104.241", str(exc))
        self.assertIn("Authentication token manipulation error", str(exc))


class GoldenCheckpoint(unittest.TestCase):
    """A golden that already passed its plant AND its boot smoke is not redone.

    Measured on cde-2026: `Golden re-entry: rolling golden-<name> back to 'tz-base'`
    appears 33 times — 11 each for web01, ftp01 and db01 — across 10 runs, while only
    ftp01 was ever the problem. The checkpoint stops that multiplication; the
    conversion barrier (a failed smoke blocks EVERY POST /template) must survive it.
    """

    def _seed(self, harness, mapping):
        (harness.comp_dir / ".template-hashes.json").write_text(
            json.dumps({"golden_planted": mapping}))

    def test_a_checkpointed_golden_is_not_rolled_back_or_resmoked(self):
        harness = GoldenBuildHarness(n=2, golden_hashes={"box0": "h0", "box1": "h1"})
        self._seed(harness, {"box0": "h0"})
        _result, events, exc = harness.run()
        self.assertIsNone(exc)
        self.assertEqual(events.keys("smoke"), ["box1"], "box0 was re-smoked")
        self.assertNotIn(str(harness.vmids[0]), events.keys("rollback"))
        self.assertEqual(events.keys("rollback"), [str(harness.vmids[1])])

    def test_the_conversion_barrier_still_covers_every_target(self):
        harness = GoldenBuildHarness(n=2, golden_hashes={"box0": "h0", "box1": "h1"})
        self._seed(harness, {"box0": "h0"})
        _result, events, exc = harness.run()
        self.assertIsNone(exc)
        self.assertEqual(sorted(events.keys("template")),
                         sorted(str(v) for v in harness.vmids),
                         "conversion must not be scoped to `work`")

    def test_a_changed_hash_invalidates_the_checkpoint(self):
        harness = GoldenBuildHarness(n=2, golden_hashes={"box0": "h0-NEW", "box1": "h1"})
        self._seed(harness, {"box0": "h0-OLD"})
        _result, events, exc = harness.run()
        self.assertIsNone(exc)
        self.assertIn("box0", events.keys("smoke"), "stale hash must be re-verified")
        self.assertIn(str(harness.vmids[0]), events.keys("rollback"))

    def test_a_golden_with_no_tz_base_is_not_treated_as_checkpointed(self):
        """A fresh clone has no tz-base snapshot, so a leftover checkpoint entry must
        not make it look planted — that is the destroy-and-redeploy path."""
        harness = GoldenBuildHarness(n=2, golden_hashes={"box0": "h0", "box1": "h1"},
                                     snapshots_empty=[golden_ops.golden_vmid_for(1000, 0)])
        self._seed(harness, {"box0": "h0"})
        _result, events, exc = harness.run()
        self.assertIsNone(exc)
        self.assertIn("box0", events.keys("smoke"))

    def test_a_pass_is_recorded_immediately_not_at_the_end_of_the_set(self):
        """If the NEXT box's smoke fails, the box that already passed must keep its
        checkpoint — otherwise the next re-entry pays for it again."""
        harness = GoldenBuildHarness(n=2, golden_hashes={"box0": "h0", "box1": "h1"},
                                     smoke_raises=["box1"])
        _result, _events, exc = harness.run()
        self.assertIsNotNone(exc, "the smoke barrier must still raise")
        recorded = json.loads((harness.comp_dir / ".template-hashes.json").read_text())
        self.assertEqual(recorded.get("golden_planted"), {"box0": "h0"})

    def test_no_hashes_means_no_checkpointing(self):
        """Without golden hashes there is no identity to key on, so nothing is skipped —
        the conservative default."""
        harness = GoldenBuildHarness(n=1)
        harness.run()
        self.assertEqual(len(harness.events.keys("smoke")), 1)
        self.assertFalse((harness.comp_dir / ".template-hashes.json").exists())


class GoldenCoverageRecords(unittest.TestCase):
    """Phase 4 emits its plant-coverage verdict through the `coverage` callback on
    EVERY completion path — keyed for verify's '-golden' mapping, slot-qualified on
    satellite slots. The alpine_services tolerated-failure path is the load-bearing
    one: those failures used to vanish (warn-only), so verify's coverage gate could
    not see a golden disk that provably lacked the failed configs."""

    STAGE = [{"name": "box0-golden", "configurations": ["apache", "roundcube"]},
             {"name": "box1-golden", "configurations": ["Enable WinRM"]}]

    def _harness(self, **kw):
        harness = GoldenBuildHarness(n=2, **kw)
        harness.golden_config.write_text(json.dumps({"machines": self.STAGE}))
        return harness

    def _run_plant(self, harness, result, alpine=False):
        """Run a harness whose nakon plant is patched to return `result`."""
        (harness.comp_dir / "Compfile").write_text(
            "alpine_services 1\n" if alpine else "")
        with patch.object(golden_ops, "run_nakon", return_value=result) as p_nakon, \
                patch.object(golden_ops, "build_nakon_bundle", return_value="bundle"), \
                patch.object(golden_ops, "ensure_alpine_services") as p_shim:
            _r, _e, exc = harness.run()
        return p_nakon, p_shim, exc

    def test_a_clean_plant_records_slot0_golden_keys_with_the_result(self):
        harness = self._harness()
        clean = NakonResult(
            [], [{"name": "box0-golden", "steps": [{"name": "apache", "rc": 0}]},
                 {"name": "box1-golden", "steps": [{"name": "Enable WinRM", "rc": 0}]}])
        _p, _s, exc = self._run_plant(harness, clean)
        self.assertIsNone(exc)
        self.assertEqual(len(harness.coverage_calls), 1)
        machines, seen = harness.coverage_calls[0]
        self.assertIs(seen, clean)
        self.assertEqual([m["name"] for m in machines],
                         ["box0-golden", "box1-golden"])
        self.assertEqual(machines[0]["configurations"], ["apache", "roundcube"])

    def test_an_alpine_tolerated_failure_still_reaches_the_record(self):
        """The point of the hook: a non-strict plant whose steps FAILED must reach the
        coverage callback with the result carrying the failures — not vanish as a
        WARNING — and ensure_alpine_services must run after the record lands."""
        harness = self._harness()
        partial = NakonResult(
            ["box0-golden: apache rc=1 (FAILED)"],
            [{"name": "box0-golden", "steps": [{"name": "apache", "rc": 1},
                                               {"name": "roundcube", "rc": 0}]},
             {"name": "box1-golden", "steps": [{"name": "Enable WinRM", "rc": 0}]}])
        p_nakon, p_shim, exc = self._run_plant(harness, partial, alpine=True)
        self.assertIsNone(exc)
        self.assertEqual(len(harness.coverage_calls), 1)
        machines, seen = harness.coverage_calls[0]
        self.assertIs(seen, partial)
        self.assertTrue(seen.failed)
        self.assertEqual([m["name"] for m in machines],
                         ["box0-golden", "box1-golden"])
        # the shim owns the failed service AFTER the verdict is on record
        p_shim.assert_called_once()
        self.assertEqual(p_nakon.call_args.kwargs["strict"], False)

    def test_score_only_lineup_records_a_skip_verdict(self):
        """The harness default: no plantable configurations — the plant is skipped
        and the verdict is (stage machines, None)."""
        harness = GoldenBuildHarness(n=2)
        _r, _e, exc = harness.run()
        self.assertIsNone(exc)
        self.assertEqual(len(harness.coverage_calls), 1)
        machines, seen = harness.coverage_calls[0]
        self.assertIsNone(seen)
        self.assertEqual([m["name"] for m in machines],
                         ["box0-golden", "box1-golden"])

    def test_a_fully_converted_resume_records_a_skip_verdict(self):
        """The M4 all-templates re-entry returns before the plant section ever reads
        the stage config — the coverage verdict must still fire."""
        harness = self._harness(all_templates=True)
        _r, _e, exc = harness.run()
        self.assertIsNone(exc)
        self.assertEqual(len(harness.coverage_calls), 1)
        machines, seen = harness.coverage_calls[0]
        self.assertIsNone(seen)
        self.assertEqual([m["name"] for m in machines],
                         ["box0-golden", "box1-golden"])

    def test_a_fully_checkpointed_plant_records_a_skip_verdict(self):
        """Every golden checkpointed (planted + smoke-passed on this hash): the plant
        must not run, and the verdict must be a skip, not an absent record."""
        harness = self._harness(golden_hashes={"box0": "h0", "box1": "h1"})
        (harness.comp_dir / ".template-hashes.json").write_text(json.dumps(
            {"golden_planted": {"box0": "h0", "box1": "h1"}}))
        with patch.object(golden_ops, "run_nakon") as p_nakon:
            _r, _e, exc = harness.run()
        self.assertIsNone(exc)
        p_nakon.assert_not_called()
        self.assertEqual(len(harness.coverage_calls), 1)
        _machines, seen = harness.coverage_calls[0]
        self.assertIsNone(seen)

    def test_satellite_slot_keys_are_slot_qualified(self):
        """Every slot's stage config names its golden machine identically, so a
        satellite slot's coverage keys must carry the slot suffix — otherwise one
        slot's clean replant would pop another slot's recorded failure."""
        harness = self._harness(slot=2, anchor="105")
        clean = NakonResult(
            [], [{"name": "box0-golden", "steps": [{"name": "apache", "rc": 0}]}])
        _p, _s, exc = self._run_plant(harness, clean)
        self.assertIsNone(exc)
        machines, _seen = harness.coverage_calls[0]
        self.assertEqual([m["name"] for m in machines],
                         ["box0-golden-slot2", "box1-golden-slot2"])

    def test_a_strict_failure_raises_before_any_verdict_is_recorded(self):
        """Strict mode (no alpine shim): a failed step aborts the build — the deploy
        dies here, so no coverage verdict fires and verify never runs against it."""
        harness = self._harness()
        failed = NakonResult(
            ["box0-golden: apache rc=1 (FAILED)"],
            [{"name": "box0-golden", "steps": [{"name": "apache", "rc": 1}]}])
        _p, _s, exc = self._run_plant(harness, failed)
        self.assertIsInstance(exc, RuntimeError)
        self.assertIn("strict mode requires green", str(exc))
        self.assertEqual(harness.coverage_calls, [])


if __name__ == "__main__":
    unittest.main()
