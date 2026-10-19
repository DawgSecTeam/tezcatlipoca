"""C2 abstraction: Mythic today, one interface for everything above it."""

from ..errors import C2Error
from .mythic import DEFAULT_TRANSPORTS, MythicClient  # noqa: F401

__all__ = ["MythicClient", "C2Error", "DEFAULT_TRANSPORTS", "FakeMythic"]


class FakeMythic:
    """In-memory stand-in; in the package, not tests/, because the seed and CLIs need a no-op C2."""

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
