"""C2 abstraction: Mythic today, one interface for everything above it.

The engine (tezcatlipoca) owns the C2 infrastructure — the Mythic VM, its
install/update (`Gaia`), the operation per competition, the payload build and the
DNAT that carries the callback path. tlaloc owns two things: the *tasking* call
and the *evidence* that a callback exists.

Contract used by the effects:

* ``callbacks()`` -> {box_id: {"seen_at": epoch, "agent": str, "external_id": str}}
  where box_id comes from the payload's build description (per-box attribution
  without IP guessing).
* ``task(box_id, command)`` -> {"ok", "output", "task_id", "detail"}
* ``plant(box_id, os_name, transport, callback_uri)`` -> the payload artifact for
  one box (used by comp-prep and by the seed's channel stage).

The concrete client is injectable everywhere (tests use c2.FakeMythic), so no
test ever needs a live Mythic.
"""

from ..errors import C2Error
from .mythic import DEFAULT_TRANSPORTS, MythicClient  # noqa: F401

__all__ = ["MythicClient", "C2Error", "DEFAULT_TRANSPORTS", "FakeMythic"]


class FakeMythic:
    """In-memory stand-in with the same surface (tests, dry runs, offline prep).

    Lives in the package (not in tests/) because the seed and the CLIs need a
    no-op C2 for `--dry-run` smoke runs on a host with no Mythic.
    """

    def __init__(self, callbacks=None, task_output="[dry-run c2] ok"):
        self._callbacks = dict(callbacks or {})
        self.task_output = task_output
        self.tasks = []
        self.plants = []

    def callbacks(self):
        return dict(self._callbacks)

    def callback_agents(self):
        return {box: rec.get("agent") for box, rec in self._callbacks.items()}

    def plant(self, box_id, os_name="linux", transport="http", callback_uri=None,
              extra=None):
        self.plants.append({"box": box_id, "os": os_name, "transport": transport,
                            "callback_uri": callback_uri, "extra": extra or {}})
        return {"ok": True, "detail": f"[dry-run] payload for {box_id}",
                "payload": f"<payloads>/{box_id}.bin", "uuid": f"dry-{box_id}"}

    def task(self, box_id, command, timeout=120):
        self.tasks.append({"box": box_id, "command": command})
        return {"ok": True, "output": self.task_output,
                "task_id": f"dry-task-{len(self.tasks)}"}

    def operation(self):
        return {"name": "dry-run", "callbacks": len(self._callbacks)}
