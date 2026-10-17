"""Inventory: the ONE classified view of the estate a teardown plans against."""

from infra.ownership import ownership_verdict, parse_vm_tags


# `error` set = fail-closed.
class NodeView:
    def __init__(self, node):
        self.node = node
        self.vms = []
        self.error = None


class Inventory:
    def __init__(self, nodes, api=None):
        self._nodes = list(nodes)
        self._api = api
        self._generation = 0
        self._views = {}

    def _api_fn(self):
        if self._api is None:
            from infra.client import proxmox_api
            self._api = proxmox_api
        return self._api

    def nodes(self):
        return list(self._nodes)

    def invalidate(self):
        self._generation += 1
        self._views = {}

    def view(self, node):
        gen = self._generation
        if gen not in self._views:
            self._views[gen] = {}
        if node not in self._views[gen]:
            nv = NodeView(node)
            try:
                nv.vms = self._api_fn()("GET", f"/nodes/{node}/qemu")["data"]
            except Exception as e:                                   # noqa: BLE001
                nv.error = f"{type(e).__name__}: {e}"
            self._views[gen][node] = nv
        return self._views[gen][node]

    def views(self):
        return [self.view(n) for n in self._nodes]

    # nodes that would not list are skipped here; clean checks use listing_failed to refuse a clean verdict
    def owned(self, ownership):
        out = []
        expect = set(ownership.tags)
        for nv in self.views():
            if nv.error:
                continue
            for v in nv.vms:
                if expect <= parse_vm_tags(v.get("tags")):
                    out.append((nv.node, v))
        return out

    # regardless of run tag — the accounting view: ours / different-run / no run tag
    def comp_tagged(self, comp_name):
        tag = f"comp-{comp_name}"
        out = []
        for nv in self.views():
            if nv.error:
                continue
            for v in nv.vms:
                if tag in parse_vm_tags(v.get("tags")):
                    out.append((nv.node, v))
        return out

    # the clone-marker probe is a config read and only happens for a tagless VM (same as ownership_verdict)
    def classify(self, node, vm, ownership):
        return ownership_verdict(node, vm["vmid"], vm.get("tags"), set(ownership.tags))

    def bridges(self, node):
        try:
            nets = self._api_fn()("GET", f"/nodes/{node}/network")["data"]
        except Exception:                                            # noqa: BLE001
            return None
        return [n["iface"] for n in nets if n.get("type") == "bridge"]

    # a clean verdict is impossible while this is non-empty
    def listing_failed(self):
        return [(nv.node, nv.error) for nv in self.views() if nv.error]
