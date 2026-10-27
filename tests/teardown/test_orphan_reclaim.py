"""Orphan reclaim (teardown/orphan_ops.py): a run whose comp dir is gone, reclaimed by tag.

What is pinned here is the blast radius, not the plumbing: only VMs carrying the exact
{tezcatlipoca, comp-<comp>, <run>} set are addressed, a same-comp VM from another run is a note,
templates need the explicit flag, and the reclaim refuses outright when a comp dir still owns the
run. Offline: the Proxmox API and the destroy primitive are faked.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from infra.ownership import Refusal, bridge_comment  # noqa: E402
from teardown import orphan_ops  # noqa: E402
from estate import state as estate_state  # noqa: E402


class FakeNode:
# The node's VMs; a patched destroy removes the vmid so the reclaim's own re-list sees it gone.

    def __init__(self, vms, bridges=None):
        self.vms = {int(v["vmid"]): dict(v) for v in vms}
        self.bridges = dict(bridges or {})
        self.deleted = []
        self.deleted_bridges = []

    def __call__(self, method, path, **kw):
        assert method == "GET" and path.endswith("/qemu"), (method, path)
        return {"data": list(self.vms.values())}


def vm(vmid, name, tags, description=""):
    return {"vmid": vmid, "name": name, "status": "stopped", "tags": tags,
            "description": description}


def _comp_dir(root, name, run_id):
    comp = Path(root) / name
    comp.mkdir(parents=True)
    estate_state.write_flat(comp, {"run_id": run_id})
    return comp


class Reclaim(unittest.TestCase):
    def setUp(self):
        # The reclaim reports its result to the dashboard: hold the environment to what each
        # test sets so an ambient handoff can never dial out from the suite.
        self._env = mock.patch.dict(os.environ)
        self._env.start()
        self.addCleanup(self._env.stop)
        for key in ("TEZ_DASHBOARD_HOST", "TEZ_DASHBOARD_PORT", "TEZ_DASHBOARD_TOKENS"):
            os.environ.pop(key, None)
        self.node = "pve"
        self.fake = FakeNode([
            vm(100, "engine", "tezcatlipoca;comp-comp;run-aaaa1111"),
            vm(101, "team1-web01", "tezcatlipoca;comp-comp;run-bbbb2222"),
            vm(102, "engine", "tezcatlipoca;comp-other;run-aaaa1111"),
            vm(103, "workshop-9", "workshop;student"),
            vm(104, "mystery", ""),
        ])
        self.calls = []
        self._patches = [
            mock.patch.object(orphan_ops, "proxmox_api", self.fake),
            mock.patch.object(orphan_ops, "destroy_vm_if_exists", self._destroy),
            mock.patch.object(orphan_ops, "node_bridges",
                              side_effect=lambda node: dict(self.fake.bridges)),
            mock.patch.object(orphan_ops, "destroy_bridge_if_exists", self._destroy_bridge),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _destroy(self, node, vmid, expect_tags=None, require_owned=False):
        self.calls.append({"vmid": vmid, "expect_tags": set(expect_tags or ()),
                           "require_owned": require_owned})
        self.fake.vms.pop(int(vmid), None)

    def _destroy_bridge(self, node, iface):
        self.fake.deleted_bridges.append(iface)
        self.fake.bridges.pop(iface, None)
        return True

    def _reclaim(self, **kw):
        return orphan_ops.reclaim("comp", "run-aaaa1111", node=self.node, log=lambda *a: None, **kw)

    def test_only_the_exact_ownership_set_is_addressed(self):
        result = self._reclaim()
        self.assertEqual(result["scanned"], 5)
        self.assertEqual([d["vmid"] for d in result["destroyed"]], [100])
        self.assertEqual([c["vmid"] for c in self.calls], [100])
        self.assertEqual(self.calls[0]["expect_tags"],
                         {"tezcatlipoca", "comp-comp", "run-aaaa1111"})
        self.assertTrue(self.calls[0]["require_owned"],
                        "the orphan reclaim must not adopt a marker-only VM")
        self.assertEqual(result["remaining"], [])
        self.assertTrue(any("run-bbbb2222" in n and "not touched" in n for n in result["notes"]),
                        result["notes"])


    def test_templates_need_the_explicit_flag(self):
        self.fake.vms[110] = vm(110, "golden-web01",
                                "tezcatlipoca;comp-comp;run-aaaa1111;tezcatlipoca-golden")
        self.fake.vms[111] = vm(111, "engine-template",
                                "tezcatlipoca;comp-comp;run-aaaa1111;engine-template")
        result = self._reclaim()
        self.assertEqual([k["vmid"] for k in result["kept_templates"]], [110, 111])
        self.assertNotIn(110, [c["vmid"] for c in self.calls])

        self.fake.vms[100] = vm(100, "engine", "tezcatlipoca;comp-comp;run-aaaa1111")
        self.fake.vms[110] = vm(110, "golden-web01",
                                "tezcatlipoca;comp-comp;run-aaaa1111;tezcatlipoca-golden")
        self.fake.vms[111] = vm(111, "engine-template",
                                "tezcatlipoca;comp-comp;run-aaaa1111;engine-template")
        result = self._reclaim(allow_templates=True)
        self.assertEqual([d["vmid"] for d in result["destroyed"]], [100, 110, 111])
        by_vmid = {c["vmid"]: c for c in self.calls}
        self.assertEqual(by_vmid[110]["expect_tags"],
                         {"tezcatlipoca", "comp-comp", "run-aaaa1111", "tezcatlipoca-golden"})
        self.assertEqual(by_vmid[111]["expect_tags"],
                         {"tezcatlipoca", "comp-comp", "run-aaaa1111", "engine-template"})

    def test_only_this_runs_commented_bridges_come_out(self):
        self.fake.bridges = {
            "vmbr0": "",
            "vmbr101": bridge_comment("comp", "run-aaaa1111", "team 101"),
            "vmbr102": bridge_comment("other", "run-cccc3333", "team 102"),
            "vmbr103": "Quotient team 103 — isolated, no uplink",   # uncommented ownership
        }
        result = self._reclaim()
        self.assertEqual(result["bridges"], ["vmbr101"])
        self.assertEqual(self.fake.deleted_bridges, ["vmbr101"])
        self.assertTrue(any("vmbr102" in n and "another run" in n for n in result["notes"]),
                        result["notes"])
        self.assertTrue(any("vmbr103" in n and "no ownership comment" in n
                            for n in result["notes"]), result["notes"])

    def test_it_refuses_when_a_comp_dir_still_owns_the_run(self):
        with tempfile.TemporaryDirectory() as d:
            _comp_dir(d, "comp", "run-aaaa1111")
            with mock.patch("common.comp_paths.comps_root", return_value=Path(d)):
                with self.assertRaises(Refusal) as ctx:
                    orphan_ops.refuse_when_state_exists("comp", "run-aaaa1111")
        self.assertIn("destroy-competition.py --comp-dir", str(ctx.exception))

    def _retire_call(self, response):
        # The reclaim with a configured handoff; returns (result, requests.request mock, log lines).
        out = []
        with mock.patch.dict(os.environ, {"TEZ_DASHBOARD_HOST": "dash.local",
                                          "TEZ_DASHBOARD_TOKENS": "engine=sekrit"}):
            with mock.patch.object(orphan_ops.requests, "request") as call:
                if isinstance(response, Exception):
                    call.side_effect = response
                else:
                    call.return_value = response
                result = orphan_ops.reclaim("comp", "run-aaaa1111", node=self.node, log=out.append)
        return result, call, out

    def test_a_configured_handoff_retires_the_range(self):
        result, call, out = self._retire_call(
            mock.Mock(status_code=200, json=lambda: {"key": "/ws", "state": "retired"}))
        method, url = call.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(url, "http://dash.local:8787/api/ranges/retire")
        self.assertEqual(call.call_args.kwargs["json"],
                         {"run": "run-aaaa1111", "by": "orphan reclaim"})
        self.assertEqual(call.call_args.kwargs["headers"]["Authorization"], "Bearer sekrit")
        self.assertEqual([d["vmid"] for d in result["destroyed"]], [100])
        self.assertTrue(any("retired /ws" in line for line in out), out)




    def test_a_leftover_is_not_retired(self):
        # A destroy that failed leaves the range in place, so the board must keep showing it.
        self.fake.vms[100] = vm(100, "engine", "tezcatlipoca;comp-comp;run-aaaa1111")
        self._patches.append(mock.patch.object(
            orphan_ops, "destroy_vm_if_exists", lambda *a, **k: None))
        self._patches[-1].start()
        self.addCleanup(self._patches[-1].stop)
        result, call, _ = self._retire_call(
            mock.Mock(status_code=200, json=lambda: {"key": "/ws", "state": "retired"}))
        self.assertEqual(result["remaining"], [100])
        call.assert_not_called()




class Cli(unittest.TestCase):
# destroy-competition.py --orphan-*: the pairing/tag checks and the report, without a node.

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("destroy_orphan_test",
                                                      _REPO / "destroy-competition.py")
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def _args(self, **over):
        base = dict(orphan_comp="comp", orphan_run="run-aaaa1111", allow_templates=False,
                    yes=True)
        base.update(over)
        return SimpleNamespace(**base)

    def _run(self, args, result):
        out = []
        with mock.patch("teardown.orphan_ops.refuse_when_state_exists"), \
                mock.patch("teardown.orphan_ops.reclaim", return_value=result), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            rc = self.mod.orphan_reclaim(args)
        return rc, "\n".join(out)


    def test_a_bare_run_id_is_refused(self):
        rc, out = self._run(self._args(orphan_run="1a2b3c4d"), {})
        self.assertEqual(rc, 1)
        self.assertIn("not a run tag", out)


    def test_leftovers_exit_nonzero(self):
        result = {"destroyed": [{"vmid": 100, "name": "engine", "role": "vm"}],
                  "kept_templates": [], "notes": [], "remaining": [100], "scanned": 7,
                  "nodes": ["pve"]}
        rc, out = self._run(self._args(), result)
        self.assertEqual(rc, 1)
        self.assertIn("NOT CLEAN", out)


if __name__ == "__main__":
    unittest.main()
