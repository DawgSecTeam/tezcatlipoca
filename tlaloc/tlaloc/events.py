"""The append-only audit log (events.jsonl): every decision record carries its reason."""

import json
import traceback
from pathlib import Path

from .state.schema import now_iso

SECRET_KEYS = {"password", "new_password", "old_password", "key", "api_key",
               "ssh_key", "command"}  # 'command' masked when nested under a cred
# Masking is key-name based: effects record credential SOURCES, never values in free text.


def mask_secrets(obj):
    if isinstance(obj, dict):
        return {k: ("***" if k in SECRET_KEYS and v else mask_secrets(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_secrets(i) for i in obj]
    return obj


class EventLog:
    def __init__(self, state_dir):
        self.path = Path(state_dir) / "events.jsonl"

    def emit(self, kind, **fields):
        rec = {"ts": now_iso(), "kind": kind}
        rec.update(mask_secrets(fields))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")

    def decision(self, *, cycle, question=None, bundle, answer, probabilities,
                 theta, source, chosen, ok, note=None):
        self.emit("decision", cycle=cycle, question=question, bundle=bundle,
                  answer=answer, probabilities=probabilities, theta=theta,
                  source=source, chosen=chosen, ok=ok, note=note)

    def action(self, *, cycle, effect, params, result, source, duration_s):
        self.emit("action", cycle=cycle, effect=effect, params=params,
                  ok=bool(getattr(result, "ok", False)),
                  detail=str(getattr(result, "detail", ""))[:2048],
                  source=source, duration_s=round(duration_s, 2))

    def error(self, where, exc):
        # Full traceback, not just "type: message" — the consecutive-error breaker needs readable detail.
        self.emit("error", where=where, error=f"{type(exc).__name__}: {exc}",
                  traceback=traceback.format_exc())

    def read(self, last_n=None):
        if not self.path.exists():
            return []
        if last_n is None:
            with open(self.path, encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        chunk, lines, raw = 65536, [], b""
        with open(self.path, "rb") as fh:
            fh.seek(0, 2)
            pos = fh.tell()
            while pos > 0 and len(lines) < last_n + 1:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                raw = fh.read(step) + raw
                lines = raw.splitlines()
        out = []
        for line in lines[-last_n:]:
            try:
                out.append(json.loads(line.decode("utf-8", "replace")))
            except ValueError:
                continue
        return out
