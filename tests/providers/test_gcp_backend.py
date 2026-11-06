"""GCP provider backend: naming, image resolution, the network stack, restore points,
teardown ownership verdicts and the preflight refusals — all against an in-memory
compute API (no network, no mocks of internals beyond the gapi seam)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from estate import config as estate_config
from providers.gcp import compute, image_ops, instance_ops, network_ops, naming, pipeline, preflight
from providers.gcp.naming import (LABEL_COMP, LABEL_RANGE, LABEL_ROLE, LABEL_RUN, LABEL_UNIT)


class FakeGcp:
    """A synchronous, in-memory compute API: network/subnet/instance/image/address
    collections with label filtering on list, and operations that answer DONE."""

    def __init__(self, project="t-proj", region="us-central1", zone="us-central1-a"):
        self.project, self.region, self.zone = project, region, zone
        self.networks, self.subnets, self.instances = {}, {}, {}
        self.images, self.machine_images = {}, {}
        self.addresses, self.firewalls, self.routers, self.routes = {}, {}, {}, {}
        self.calls = []

    # --- fake plumbing ---
    def _op(self):
        return {"status": "DONE", "selfLink": f"https://fake/{len(self.calls)}"}

    def _404(self, what):
        raise RuntimeError(f"GCP GET .../{what} -> 404: not found")

    def __call__(self, creds, method, path, params=None, data=None, timeout=120):
        self.calls.append((method, path, data))
        params = params or {}
        p = path.split("compute/v1/", 1)[-1]
        ours = f"projects/{self.project}/"
        if p.startswith(ours):              # our project's paths go bare (regions/…, global/…)
            p = p[len(ours):]
        if method == "GET":
            return self._get(p, params)
        if method == "DELETE":
            return self._delete(p)
        return self._post(p, data)

    def _get(self, p, params):
        if "/operations/" in p:
            return {"status": "DONE"}
        if p == "":
            return {"quotas": [{"metric": "NETWORKS", "limit": 5, "usage": 1},
                               {"metric": "IMAGES", "limit": 100, "usage": 0},
                               {"metric": "MACHINE_IMAGES", "limit": 100, "usage": 0}]}
        if p == f"regions/{self.region}":
            return {"quotas": [{"metric": "CPUS", "limit": 200, "usage": 0},
                               {"metric": "DISKS_TOTAL_GB", "limit": 4096, "usage": 0},
                               {"metric": "IN_USE_ADDRESSES", "limit": 8, "usage": 0}]}
        if p == f"regions/{self.region}/addresses":
            return {"items": list(self.addresses.values())}
        if p == "global/images":
            items = self._labeled(list(self.images.values()), params)
            return {"items": items}
        if "/global/images/" in p or p.startswith("global/images/"):
            if "/family/" in p:
                family = p.rsplit("/", 1)[-1]
                found = [i for i in self.images.values()
                         if i.get("family") == family and i.get("status") != "DEPRECATED"]
                if not found:
                    return self._404(p)
                return sorted(found, key=lambda i: i["name"])[-1]
            name = p.rsplit("/", 1)[-1]
            return self.images.get(name) or self._404(p)
        if p == f"regions/{self.region}/machineImages":
            return {"items": list(self.machine_images.values())}
        if p == "global/networks":
            return {"items": list(self.networks.values())}
        if p.startswith("global/networks/"):
            name = p.rsplit("/", 1)[-1]
            return self.networks.get(name) or self._404(p)
        if p == f"regions/{self.region}/subnetworks":
            return {"items": list(self.subnets.values())}
        if p.startswith(f"regions/{self.region}/subnetworks/"):
            name = p.rsplit("/", 1)[-1]
            return self.subnets.get(name) or self._404(p)
        if p == f"regions/{self.region}/addresses/":
            return {"items": list(self.addresses.values())}
        if p.startswith(f"regions/{self.region}/addresses/"):
            name = p.rsplit("/", 1)[-1]
            return self.addresses.get(name) or self._404(p)
        if p == "global/firewalls":
            return {"items": list(self.firewalls.values())}
        if p == "global/routes":
            return {"items": list(self.routes.values())}
        if p.startswith("global/routes/"):
            name = p.rsplit("/", 1)[-1]
            return self.routes.get(name) or self._404(p)
        if p == f"regions/{self.region}/routers":
            return {"items": list(self.routers.values())}
        if p.startswith(f"zones/{self.zone}/instances"):
            if p == f"zones/{self.zone}/instances":
                return {"items": self._labeled(list(self.instances.values()), params)}
            name = p.rsplit("/", 1)[-1]
            return self.instances.get(name) or self._404(p)
        if p.startswith(f"regions/{self.region}/machineImages/"):
            name = p.rsplit("/", 1)[-1]
            return self.machine_images.get(name) or self._404(p)
        raise AssertionError(f"unfake'd GET {p}")

    def _post(self, p, data):
        data = data or {}
        if p == "global/networks":
            self.networks[data["name"]] = {
                "name": data["name"],
                "selfLink": f"https://compute.googleapis.com/compute/v1/projects/"
                            f"{self.project}/global/networks/{data['name']}"}
            return self._op()
        if p == f"regions/{self.region}/subnetworks":
            self.subnets[data["name"]] = {
                "name": data["name"], "network": data["network"],
                "selfLink": f"https://compute.googleapis.com/compute/v1/projects/"
                            f"{self.project}/regions/{self.region}/subnetworks/{data['name']}"}
            return self._op()
        if p == f"regions/{self.region}/addresses":
            self.addresses[data["name"]] = {"name": data["name"],
                                            "address": "192.0.2.10"}
            return self._op()
        if p == "global/firewalls":
            self.firewalls[data["name"]] = {"name": data["name"], **data}
            return self._op()
        if p == "global/routes":
            self.routes[data["name"]] = {
                "name": data["name"], "network": data.get("network"), **data,
                "selfLink": f"https://compute.googleapis.com/compute/v1/projects/"
                            f"{self.project}/global/routes/{data['name']}"}
            return self._op()
        if p == f"regions/{self.region}/routers" or p.startswith(f"regions/{self.region}/routers/"):
            name = data.get("name") or p.rsplit("/", 1)[-1]
            if p == f"regions/{self.region}/routers" and name in self.routers:
                raise RuntimeError(f"GCP POST ... -> 409: alreadyExists: {name}")
            self.routers[name] = {
                "name": name,
                "selfLink": f"https://compute.googleapis.com/compute/v1/projects/"
                            f"{self.project}/regions/{self.region}/routers/{name}"}
            return self._op()
        if p == f"regions/{self.region}/machineImages":
            self.machine_images[data["name"]] = {"name": data["name"], **data}
            return self._op()
        if p == "global/images":
            self.images[data["name"]] = {"name": data["name"], "labels": data.get("labels") or {},
                                         "status": "READY"}
            return self._op()
        if p.startswith(f"zones/{self.zone}/instances") and p.endswith("/start"):
            name = p.removeprefix(f"zones/{self.zone}/instances/").removesuffix("/start")
            self.instances[name]["status"] = "RUNNING"
            return self._op()
        if p.startswith(f"zones/{self.zone}/instances") and p.endswith("/stop"):
            name = p.removeprefix(f"zones/{self.zone}/instances/").removesuffix("/stop")
            self.instances[name]["status"] = "TERMINATED"
            return self._op()
        if p == f"zones/{self.zone}/instances":
            nics = []
            for index, n in enumerate(data["networkInterfaces"]):
                nic = dict(n)
                access = nic.get("accessConfigs") or []
                if access:
                    # GCE auto-assigns the ephemeral external IP when natIP is absent
                    access[0] = dict(access[0])
                    access[0].setdefault("natIP", f"34.59.10.{len(self.instances) + index + 1}")
                    nic["accessConfigs"] = access
                nics.append(nic)
            info = {"name": data["name"], "labels": data["labels"], "status": "RUNNING",
                    "canIpForward": bool(data.get("canIpForward")),
                    "disks": [{"boot": True, "source": f"disk/{data['name']}"}],
                    "tags": data.get("tags") or {},
                    "networkInterfaces": [
                        {"networkIP": n["networkIP"],
                         "accessConfigs": n.get("accessConfigs") or []}
                        for i, n in enumerate(nics)]}
            self.instances[data["name"]] = info
            return self._op()
        raise AssertionError(f"unfake'd POST {p}")

    def _delete(self, p):
        for table, marker in ((self.networks, "global/networks/"),
                              (self.subnets, "/subnetworks/"),
                              (self.addresses, "/addresses/"),
                              (self.routers, "/routers/"),
                              (self.routes, "global/routes/"),
                              (self.instances, "/instances/"),
                              (self.images, "/images/"),
                              (self.machine_images, "/machineImages/"),
                              (self.firewalls, "global/firewalls/")):
            if marker in p:
                name = p.rsplit("/", 1)[-1]
                if name in table:
                    del table[name]
                    return self._op()
                raise RuntimeError(f"DELETE -> 404: {p}")
        raise AssertionError(f"unfake'd DELETE {p}")

    def _labeled(self, items, params):
        filtr = params.get("filter")
        if not filtr:
            return items
        wanted = dict(pair.split("=", 1) for pair in filtr.split())
        out = []
        for info in items:
            labels = info.get("labels") or {}
            if all(labels.get(k.removeprefix("labels.")) == v for k, v in wanted.items()):
                out.append(info)
        return out

    # --- helper seeds ---
    def add_instance(self, name, comp, run="", role=None, unit=None, status="RUNNING"):
        self.instances[name] = {
            "name": name, "status": status,
            "labels": naming.labels(comp, run, role, unit),
            "networkInterfaces": [{"networkIP": "10.0.0.1", "accessConfigs": []}]}
        return self.instances[name]

    def add_image(self, name, comp, run="", role=None, hash_label=None):
        self.images[name] = {"name": name, "status": "READY",
                             "labels": {**naming.labels(comp, run, role),
                                        **({image_ops.HASH_LABEL: hash_label}
                                           if hash_label else {})}}
        return self.images[name]


class GcpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / ".gcp-sa.json").write_text(json.dumps({
            "project_id": "t-proj", "private_key": "x", "client_email": "sa@t.iam"}))
        self.config = estate_config.active()
        previous = self.config.activate({
            "gcp.credentials_path": str(root / ".gcp-sa.json"),
            "gcp.zone": "us-central1-a",
            "gcp.ingress_sources": "203.0.113.7/32",
            "gcp.image_map": {
                "engine-base": {"project": "ubuntu-os-cloud", "image": "ubuntu-2404-lts-amd64"},
                "red01-base": {"project": "ubuntu-os-cloud", "image": "ubuntu-2404-lts-amd64"},
                "base-ubuntu24.04-fix": {"project": "ubuntu-os-cloud", "image": "ubuntu-2404-lts-amd64"},
            }})
        self.addCleanup(lambda: self.config.deactivate(previous))
        self.fake = FakeGcp()
        self.fake.add_image("ubuntu-2404-noble-amd64-v20261105", "", None)
        self.fake.images["ubuntu-2404-noble-amd64-v20261105"]["family"] = \
            "ubuntu-2404-lts-amd64"
        # every module that binds gapi calls the fake through ITS own binding
        for module in (compute, image_ops, instance_ops, network_ops, preflight):
            patcher = patch.object(module, "gapi", self.fake)
            patcher.start()
            self.addCleanup(patcher.stop)


class NamingTest(unittest.TestCase):
    def test_labels_are_the_ownership_set(self):
        got = naming.labels("Scrim One Light", "run-0e6474eb", "red01", unit=1671)
        self.assertEqual(got[LABEL_COMP], "scrim-one-light")
        self.assertEqual(got[LABEL_RUN], "0e6474eb")
        self.assertEqual(got[LABEL_ROLE], "red01")
        self.assertEqual(got[LABEL_UNIT], "1671")

    def test_owned_matches_the_full_set_only(self):
        ours = {"labels": naming.labels("c1", "run-a", "engine", unit=1)}
        self.assertTrue(naming.owned(ours, "c1", "run-a"))
        self.assertTrue(naming.owned(ours, "c1", "run-a", role="engine"))
        self.assertFalse(naming.owned(ours, "c1", "run-a", role="golden"))
        self.assertFalse(naming.owned(ours, "c1", "run-b"))
        self.assertFalse(naming.owned(ours, "c2", "run-a"))
        self.assertFalse(naming.owned({}, "c1", "run-a"))

    def test_long_names_refuse_rather_than_truncate(self):
        with self.assertRaises(SystemExit):
            naming.instance_name("tz-" + "x" * 60, "team1-web01")


class ResolveBaseTest(GcpBase):
    def test_missing_entry_refuses_by_template_name(self):
        with self.assertRaises(SystemExit) as caught:
            image_ops.resolve_base({}, "base-centos8-fix")
        self.assertIn("base-centos8-fix", str(caught.exception))
        self.assertIn("gcp.image_map", str(caught.exception))

    def test_family_resolves_to_newest_live_image(self):
        self.fake.add_image("ubuntu-2404-lts-amd64-and-more-v20261101", "", None)  # older
        self.fake.images["ubuntu-2404-lts-amd64-and-more-v20261101"] = {
            "name": "ubuntu-2404-lts-amd64-and-more-v20261101", "status": "READY",
            "labels": {}, "family": "ubuntu-2404-lts-amd64"}
        url = image_ops.resolve_base({}, "engine-base")
        self.assertEqual(url, "projects/ubuntu-os-cloud/global/images/"
                              "ubuntu-2404-noble-amd64-v20261105")

    def test_concrete_build_name_is_used_as_is(self):
        self.fake.add_image("ubuntu-pro-2404-v20261105", "", None)
        self.config.activate({"gcp.image_map": {
            "engine-base": {"project": "ubuntu-os-cloud", "image": "ubuntu-pro-2404-v20261105"}}})
        url = image_ops.resolve_base({}, "engine-base")
        self.assertEqual(url, "projects/ubuntu-os-cloud/global/images/ubuntu-pro-2404-v20261105")


class EnsureNetworkTest(GcpBase):
    def test_creates_vpc_subnets_rule_and_records_them(self):
        document = _Document()
        net = network_ops.ensure_network(
            document, comp_name="c1", run_id="run-0e6474eb",
            team_identifiers=["101", "102"], in_path=False, red=True,
            record=lambda *_: None)
        self.assertEqual(net["vpc"], "tz-c1-0e6474eb")
        self.assertEqual(sorted(self.fake.subnets), [
            "tz-c1-0e6474eb-net-mgmt", "tz-c1-0e6474eb-net-red",
            "tz-c1-0e6474eb-net-t101", "tz-c1-0e6474eb-net-t102"])
        self.assertIn("tz-c1-0e6474eb-fw-mgmt", self.fake.firewalls)
        # no static reservation: the engine takes its EPHEMERAL address at create
        self.assertEqual(net["engine_public_ip"], "")
        self.assertEqual([r["name"] for r in document.resources("network")], [net["vpc"]])

    def test_idempotent_second_pass_adds_nothing(self):
        document = _Document()
        network_ops.ensure_network(document, comp_name="c1", run_id="run-0e6474eb",
                                   team_identifiers=["101"], record=lambda *_: None)
        creations = [c for c in self.fake.calls if c[0] == "POST"]
        network_ops.ensure_network(document, comp_name="c1", run_id="run-0e6474eb",
                                   team_identifiers=["101"], record=lambda *_: None)
        after = [c for c in self.fake.calls if c[0] == "POST"]
        self.assertEqual({c[1] for c in creations}, {c[1] for c in after},
                         "the second pass must not create anything new")

    def test_no_ingress_sources_refuses(self):
        previous = self.config.activate({"gcp.ingress_sources": ""})
        try:
            with self.assertRaises(SystemExit) as caught:
                network_ops.ensure_network(_Document(), comp_name="c1", run_id="run-x",
                                           team_identifiers=["101"],
                                           record=lambda *_: None)
            self.assertIn("gcp.ingress_sources", str(caught.exception))
        finally:
            self.config.deactivate(previous)


class _Document:
    """estate.state.EstateState against a tmp comp dir (the real document, no fake)."""

    def __init__(self, comp_dir=None):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = comp_dir or Path(self._tmp.name)
        from estate import state
        self.document = state.EstateState.load(self._root)
        self.document.load = self.document.save

    # estate-state API surface used by the network/teardown ops
    def record(self, *args, **kwargs):
        self.document.record(*args, **kwargs)

    def forget(self, *args, **kwargs):
        self.document.forget(*args, **kwargs)

    def resources(self, kind=None, node=None):
        return self.document.resources(kind, node)

    def save(self):
        self.document.save()

    @property
    def data(self):
        return self.document.data

    def section(self, name):
        return self.document.section(name)


class RestorePointsTest(GcpBase):
    def _ctx(self, comp="c1", run="run-0e6474eb"):
        import types
        return types.SimpleNamespace(comp_name=comp, run_id=run)

    def test_create_stops_marks_and_restarts(self):
        self.fake.add_instance("tz-c1-0e6474eb-team1-web01", "c1", "run-0e6474eb",
                               unit=1010)
        ok = pipeline.restore_point_create(
            self._ctx(), {"vm_name": "team1-web01", "vmid": 1010}, "tz-base")
        self.assertTrue(ok)
        self.assertIn("tz-c1-0e6474eb-team1-web01-tz-base", self.fake.machine_images)
        self.assertEqual(self.fake.instances["tz-c1-0e6474eb-team1-web01"]["status"],
                         "RUNNING")
        # the machine image carries this run's labels
        marks = pipeline.restore_points_list(self._ctx(), {"vm_name": "team1-web01",
                                                           "vmid": 1010})
        self.assertEqual(marks, {"tz-base"})

    def test_missing_instance_warns_not_raises(self):
        ok = pipeline.restore_point_create(
            self._ctx(), {"vm_name": "team1-web01", "vmid": 1010}, "tz-base")
        self.assertFalse(ok)


class DestroyEstateTest(GcpBase):
    def _document_with_range(self, comp, run):
        doc = _Document()
        doc.document.data["identity"] = {"comp_name": comp, "run_id": run}
        for kind, name in (("team_box", "tz-c1-0e6474eb-team1-web01"),
                           ("engine", "tz-c1-0e6474eb-quotient-engine"),
                           ("engine_template", "tz-c1-0e6474eb-engine-img"),
                           ("network", "tz-c1-0e6474eb")):
            doc.record(kind, "", name)
        return doc

    def test_owned_resources_die_and_the_network_lasts_till_last(self):
        comp, run = "c1", "run-0e6474eb"
        self.fake.add_instance("tz-c1-0e6474eb-team1-web01", comp, run, unit=1010)
        self.fake.add_instance("tz-c1-0e6474eb-quotient-engine", comp, run, unit=1000)
        self.fake.add_image("tz-c1-0e6474eb-engine-img", comp, run, "engine-template")
        self.fake.networks["tz-c1-0e6474eb"] = {
            "name": "tz-c1-0e6474eb",
            "selfLink": "https://compute.googleapis.com/compute/v1/projects/"
                        "t-proj/global/networks/tz-c1-0e6474eb"}
        doc = self._document_with_range(comp, run)
        summary = pipeline.destroy_estate(doc.document, full=True,
                                          log=lambda *_: None)
        self.assertEqual(summary["destroyed"] and sorted(r["name"] for r in summary["destroyed"]),
                         ["tz-c1-0e6474eb", "tz-c1-0e6474eb-engine-img",
                          "tz-c1-0e6474eb-quotient-engine",
                          "tz-c1-0e6474eb-team1-web01"])
        self.assertEqual(summary["refused"], [])

    def test_foreign_labeled_resources_are_refused_never_deleted(self):
        comp, run = "c1", "run-0e6474eb"
        self.fake.add_instance("tz-c1-0e6474eb-team1-web01", comp, "run-other", unit=1010)
        doc = _Document()
        doc.document.data["identity"] = {"comp_name": comp, "run_id": run}
        doc.record("team_box", "", "tz-c1-0e6474eb-team1-web01")
        summary = pipeline.destroy_estate(doc.document, full=True, log=lambda *_: None)
        self.assertEqual(len(summary["refused"]), 1)
        self.assertIn("not this run's", summary["refused"][0]["reason"])
        self.assertIn("tz-c1-0e6474eb-team1-web01", self.fake.instances)

    def test_teams_only_keeps_templates(self):
        comp, run = "c1", "run-0e6474eb"
        self.fake.add_image("tz-c1-0e6474eb-engine-img", comp, run, "engine-template")
        self.fake.add_instance("tz-c1-0e6474eb-team1-web01", comp, run, unit=1010)
        doc = self._document_with_range(comp, run)
        summary = pipeline.destroy_estate(doc.document, full=False, log=lambda *_: None)
        self.assertEqual([r["kind"] for r in summary["skipped"]], ["engine_template"])
        self.assertIn("tz-c1-0e6474eb-engine-img", self.fake.images)


class LiveUnitsTest(GcpBase):
    def test_unit_labels_are_the_liveness_proof(self):
        self.fake.add_instance("a", "c1", "run-x", unit=1010)
        self.fake.add_instance("b", "c1", "run-x", unit=1020)
        self.fake.instances["b"]["labels"][LABEL_UNIT] = "not-a-number"
        self.assertEqual(pipeline.live_unit_numbers(), {1010})


class PreflightTest(GcpBase):
    def _ctx(self, boxes, teams, comp="c1"):
        import types
        return types.SimpleNamespace(
            comp_name=comp, comp_dir=Path(self.tmp.name), boxes=boxes, teams=teams,
            placement=None, previous_state={"run_id": ""},
            all_targets=[{"box": b} for b in boxes])

    def test_matrix_refuses_windows_inpath_and_unmanaged(self):
        boxes = [
            {"name": "w1", "template": "Windows-Server", "cpu": 2, "memory_mb": 2048},
            {"name": "f1", "template": "pfsense", "cpu": 1, "memory_mb": 512,
             "in_path": True},
            {"name": "u1", "template": "appliance", "cpu": 1, "memory_mb": 512,
             "unmanaged": True},
            {"name": "ok", "template": "base-ubuntu24.04-fix", "cpu": 1, "memory_mb": 1024},
        ]
        with self.assertRaises(SystemExit) as caught:
            preflight.run_capacity_preflight(self._ctx(boxes, {"team1": {"identifier": 101}}),
                                             Path(self.tmp.name), from_phase=1)
        text = str(caught.exception)
        self.assertIn("w1", text)
        self.assertIn("f1", text)
        self.assertIn("u1", text)
        self.assertNotIn("'ok'", text)

    def test_leftover_range_of_this_comp_refuses(self):
        boxes = [{"name": "web01", "template": "base-ubuntu24.04-fix", "cpu": 1,
                  "memory_mb": 1024}]
        self.fake.add_instance("tz-c1-0e6474eb-team1-web01", "c1", "run-old", unit=1010)
        with self.assertRaises(SystemExit) as caught:
            preflight.run_capacity_preflight(self._ctx(boxes, {"team1": {"identifier": 101}}),
                                             Path(self.tmp.name), from_phase=1)
        self.assertIn("already has a gcp range standing", str(caught.exception))

    def test_a_clean_lineup_passes_the_gates(self):
        boxes = [{"name": "web01", "template": "base-ubuntu24.04-fix", "cpu": 1,
                  "memory_mb": 1024}]
        (Path(self.tmp.name) / "Compfile").write_text("name c1\nremote_access 0\n")
        preflight.run_capacity_preflight(self._ctx(boxes, {"team1": {"identifier": 101}}),
                                         Path(self.tmp.name), from_phase=1)

    def test_nic_ceiling_refuses_oversized_ranges(self):
        boxes = [{"name": "web01", "template": "base-ubuntu24.04-fix", "cpu": 1,
                  "memory_mb": 1024}]
        teams = {f"team{i}": {"identifier": 100 + i} for i in range(1, 9)}
        with self.assertRaises(SystemExit) as caught:
            preflight.run_capacity_preflight(self._ctx(boxes, teams),
                                             Path(self.tmp.name), from_phase=1)
        self.assertIn("gcp.max_engine_nics", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class HashLabelTest(GcpBase):
    def test_a_64_char_digest_stamps_within_the_63_char_label_cap(self):
        import hashlib
        digest = hashlib.sha256(b"engine").hexdigest()
        self.assertEqual(len(digest), 64)
        image_ops.create_from_instance(
            {}, "tz-c1-0e6474eb-engine-img",
            {"disks": [{"boot": True, "source": "disk/x"}]},
            {"tz-comp": "c1", image_ops.HASH_LABEL: digest})
        stored = self.fake.images["tz-c1-0e6474eb-engine-img"]["labels"][image_ops.HASH_LABEL]
        self.assertEqual(len(stored), 63)
        self.assertEqual(stored, image_ops.hash_value(digest))
        # the reuse compare goes through the same clamp, so stamps match expectations
        self.assertEqual(image_ops.image_hash(self.fake.images["tz-c1-0e6474eb-engine-img"]),
                         image_ops.hash_value(digest))


class ReclaimOursRefuseOthersTest(GcpBase):
    def _ctx(self, comp="c1", run="run-x", prior=None):
        import types
        return types.SimpleNamespace(
            comp_name=comp, comp_dir=Path(self.tmp.name), run_id=run,
            previous_state=prior if prior is not None else {"run_id": run},
            boxes=[], teams={"team1": {"identifier": 101}}, placement=None,
            all_targets=[])

    def test_reclaim_sweeps_this_runs_instances_and_leaves_others(self):
        import types
        self.fake.add_instance("tz-c1-abc123-mine", "c1", "run-abc123", unit=1010)
        self.fake.add_instance("tz-c1-other12d-theirs", "c1", "run-other12d", unit=1020)
        ctx = types.SimpleNamespace(comp_name="c1", run_id="run-abc123")
        pipeline.reclaim_interrupted(ctx)
        self.assertNotIn("tz-c1-abc123-mine", self.fake.instances)
        self.assertIn("tz-c1-other12d-theirs", self.fake.instances)

    def test_the_leftover_gate_reads_the_persisted_run_id(self):
        boxes = [{"name": "web01", "template": "base-ubuntu24.04-fix", "cpu": 1,
                  "memory_mb": 1024}]
        (Path(self.tmp.name) / "Compfile").write_text("name c1\nremote_access 0\n")
        # our own crash leftover (state's run id), another comp's machine standing
        self.fake.add_instance("tz-c1-abc123-mine", "c1", "run-abc123", unit=1010)
        self.fake.add_instance("tz-c2-42424-a-live", "c2", "run-00042424", unit=1030)
        # run_capacity_preflight gates BEFORE the mint: ctx.run_id is empty, the
        # persisted previous_state carries it
        self.assertEqual(
            preflight._leftover_gate({}, self._ctx(run="", prior={"run_id": "run-abc123"})),
            ["instance tz-c1-abc123-mine (run abc123)"])

    def test_the_leftover_gate_refuses_a_standing_range_of_another_run(self):
        boxes = [{"name": "web01", "template": "base-ubuntu24.04-fix", "cpu": 1,
                  "memory_mb": 1024}]
        self.fake.add_instance("tz-c1-abcd1234-live", "c1", "run-abcd1234", unit=1030)
        with self.assertRaises(SystemExit) as caught:
            preflight._leftover_gate({}, self._ctx(run="run-x"))
        self.assertIn("another run", str(caught.exception))
        self.assertIn("tz-c1-abcd1234-live", str(caught.exception))


class CreateEngineTest(GcpBase):
    def test_engine_create_takes_nic_order_and_records_the_ephemeral_public_ip(self):
        doc = _Document()
        import types

        def save_state():
            doc.save()

        ctx = types.SimpleNamespace(
            comp_name="c1", run_id="run-0e6474eb", comp_dir=Path(self.tmp.name),
            placement=None, boxes=[], teams={"team1": {"identifier": 101}},
            engine_vmid=1000, engine_mgmt_ip="", state={},
            document=lambda: doc, save_state=save_state)
        pipeline.create_estate(ctx)
        engine = "tz-c1-0e6474eb-quotient-engine"
        info = self.fake.instances[engine]
        nics = info["networkInterfaces"]
        # NIC order is create order: mgmt (ephemeral public), team interface (.2 — the
        # VCP owns .1), red interface (.2)
        self.assertEqual(len(nics), 3)
        self.assertEqual(nics[0]["networkIP"], network_ops.MGMT_ENGINE_IP)
        self.assertTrue(nics[0]["accessConfigs"][0].get("natIP"))
        self.assertEqual(nics[1]["networkIP"], "192.168.101.2")
        self.assertEqual(nics[2]["networkIP"], "10.200.0.2")
        # the range's router: forwarding enabled, and the VPC default route lands here
        self.assertTrue(info["canIpForward"])
        route = self.fake.routes[f"tz-c1-0e6474eb-route-engine"]
        self.assertEqual(route["destRange"], "0.0.0.0/0")
        self.assertEqual(route["priority"], 900)
        self.assertIn("tz-c1-0e6474eb-quotient-engine", route["nextHopInstance"])
        # ... while the ENGINE'S OWN egress rides a higher-priority TAGGED WAN route
        # (next-hop instances are NOT exempt from their own routes)
        wan = self.fake.routes[f"tz-c1-0e6474eb-route-engine-wan"]
        self.assertEqual(wan["priority"], 800)
        self.assertEqual(wan["tags"], ["tz-engine"])
        self.assertIn("default-internet-gateway", wan["nextHopGateway"])
        # the realized ephemeral address is the run's recorded mgmt identity
        self.assertEqual(ctx.engine_mgmt_ip, nics[0]["accessConfigs"][0]["natIP"])
        self.assertEqual(doc.document.data["identity"]["engine_mgmt_ip"],
                         ctx.engine_mgmt_ip)
        self.assertIn(engine, [r["name"] for r in doc.resources("engine")])


class TeamAddressTest(unittest.TestCase):
    def test_the_engine_sits_at_dot2_on_gcp_and_dot1_on_proxmox(self):
        from common.utils import engine_team_gateway
        self.assertEqual(engine_team_gateway("101", "gcp"), "192.168.101.2")
        self.assertEqual(engine_team_gateway("101"), "192.168.101.1")

    def test_netplan_writes_the_platform_anchor(self):
        from estate.engine_setup import netplan_yaml
        gcp = netplan_yaml(["101"], red_segment={"subnet": "10.200.0.0/24",
                                                 "gw": "10.200.0.2"}, platform="gcp")
        self.assertIn('192.168.101.2/24', gcp)
        self.assertIn('10.200.0.2/24', gcp)
        # GCE exposes no MACs: the pin is the interface NAME, ens5+ in create order
        self.assertRegex(gcp, r"(?m)^    ens5:")
        self.assertFalse(gcp.count("macaddress"))
        pve = netplan_yaml(["101"])
        self.assertIn('192.168.101.1/24', pve)
        self.assertIn("ens19:", pve)

    def test_red_dnat_targets_follow_the_platform_anchor(self):
        from red import red01_ops
        self.assertIn("192.168.101.1",
                      red01_ops.dnat_destinations(["101"]))
        self.assertIn("192.168.101.2",
                      red01_ops.dnat_destinations(["101"], platform="gcp"))
