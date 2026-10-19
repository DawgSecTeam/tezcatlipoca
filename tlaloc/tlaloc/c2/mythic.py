"""Mythic 4 client: GraphQL tasking and callback evidence — callback rows survive the agent's death, so liveness is row freshness (c2.callback_max_age_sec), never a process; attribution is the payload description (= box id)."""

import base64
import json
import time
from dataclasses import dataclass

from ..errors import C2Error

DEFAULT_TRANSPORTS = ("http", "webhook")
AGENT_BY_OS = {"linux": "poseidon", "windows": "apollo"}
# selected_os must match the payload type's capitalised supported_os; "linux" is rejected.
OS_NAME = {"linux": "Linux", "windows": "Windows", "macos": "macOS"}

# Mythic reports `submitted` while a task is only queued — treating it as
# terminal reads an empty output as success.
TERMINAL_OK = ("completed", "processed", "success")
TERMINAL_BAD = ("error", "failed", "cancelled", "canceled", "expired")


@dataclass
class Callback:
    external_id: str
    box_id: str
    agent: str
    host: str = ""
    user: str = ""
    seen_at: float = 0.0
    raw: dict = None

    def age(self, now=None):
        return (now or time.time()) - self.seen_at


class MythicClient:
    def __init__(self, cfg, http=None, graphql=None, now=None, log=None, password=None):
        c2 = cfg.get("c2") or {}
        self.base_url = (c2.get("base_url") or "").rstrip("/")
        self.username = c2.get("username") or "tlaloc"
        self.password = password
        self.password_env = c2.get("password_env") or "TLALOC_MYTHIC_PASSWORD"
        self.operation_name = c2.get("operation") or ""
        self.verify_tls = bool(c2.get("verify_tls", False))
        self.callback_max_age = float(c2.get("callback_max_age_sec", 120))
        # The engine's DNAT gateway as the team boxes see it; empty = profile default.
        self.callback_host = str(c2.get("callback_host") or "")
        self.callback_port = int(c2.get("callback_port") or 0)
        self.timeout = float(c2.get("timeout_sec", 30))
        self.enabled = bool(c2.get("enabled")) and bool(self.base_url)
        self._http = http
        self._graphql_fn = graphql
        self._now = now or time.time
        self._token = None
        self.log = log

    def _password(self):
        if self.password:
            return self.password
        import os
        return os.environ.get(self.password_env) or ""

    def _request(self, method, path, body=None, *, raw=False, timeout=None):
        if self._http is not None:
            return self._http(method, path, body, timeout or self.timeout)
        import requests
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json"}
        if self._token and path != "/auth":
            headers["Authorization"] = f"Bearer {self._token}"
        response = requests.request(method, url, json=body, headers=headers,
                                    timeout=timeout or self.timeout,
                                    verify=self.verify_tls)
        if response.status_code >= 400:
            raise C2Error(f"{method} {path} -> HTTP {response.status_code}: "
                          f"{response.text[:200]}")
        if raw:
            return response.content
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}

    def login(self):
        payload = self._request("POST", "/auth",
                                {"username": self.username,
                                 "password": self._password()})
        token = (payload or {}).get("access_token")
        if not token:
            raise C2Error("mythic login returned no access_token")
        self._token = token
        return token

    def _graphql_call(self, query, variables=None):
        if self._graphql_fn is not None:
            return self._graphql_fn(query, variables or {})
        if not self._token:
            self.login()
        import requests
        # The trailing slash is load-bearing: nginx 301s /graphql and a followed POST loses its body.
        response = requests.post(
            f"{self.base_url}/graphql/",
            json={"query": query, "variables": variables or {}},
            headers={"Authorization": f"Bearer {self._token}",
                     "Content-Type": "application/json"},
            timeout=self.timeout, verify=self.verify_tls)
        if response.status_code >= 400:
            raise C2Error(f"POST /graphql/ -> HTTP {response.status_code}: "
                          f"{response.text[:200]}")
        body = response.json()
        if body.get("errors"):
            message = (body["errors"][0] or {}).get("message") or body["errors"][0]
            raise C2Error(f"graphql error: {message}")
        return body.get("data") or {}

    CALLBACK_QUERY = """
query Callbacks($operation: String!) {
  callback(where: {operation: {name: {_eq: $operation}}, active: {_eq: true}}) {
    id
    display_id
    description
    host
    user
    process_name
    last_checkin
    payload { payloadtype { name } }
  }
}
"""

    CREATE_TASK = """
mutation CreateTask($callback_id: Int!, $command: String!) {
  createTask(callback_id: $callback_id, command: $command, params: "") {
    status
    error
    id
    display_id
  }
}
"""

    TASK_QUERY = """
query Task($id: Int!) {
  task(where: {id: {_eq: $id}}) {
    id
    status
    completed
    stdout
    stderr
  }
  response(where: {task_id: {_eq: $id}}, order_by: {timestamp: asc}) {
    response_text
  }
}
"""

    CREATE_PAYLOAD = """
mutation CreatePayload($payload: String!) {
  createPayload(payloadDefinition: $payload) {
    status
    error
    uuid
  }
}
"""

    PAYLOAD_QUERY = """
query Payload($uuid: String!) {
  payload(where: {uuid: {_eq: $uuid}}) {
    uuid
    build_phase
    build_message
    filemetum { agent_file_id filename_utf8 }
  }
}
"""

    def callback_rows(self):
        data = self._graphql_call(self.CALLBACK_QUERY,
                                  {"operation": self.operation_name})
        rows = []
        for row in data.get("callback") or []:
            payload = row.get("payload") or {}
            ptype = (payload.get("payloadtype") or {}) if isinstance(payload, dict) else {}
            rows.append({"id": row.get("id"), "display_id": row.get("display_id"),
                         "description": row.get("description"), "host": row.get("host"),
                         "user": row.get("user"), "process_name": row.get("process_name"),
                         "last_checkin": row.get("last_checkin"),
                         "agent": ptype.get("name")})
        return rows

    def callbacks(self):
        now = self._now()
        out = {}
        for row in self.callback_rows():
            box_id = (row.get("description") or row.get("host") or "").strip()
            if not box_id:
                continue
            seen = _epoch(row.get("last_checkin") or row.get("timestamp"))
            out[box_id] = {"seen_at": seen,
                           "agent": (row.get("agent") or (row.get("payload") or {})
                                     .get("payload_type") or "?"),
                           "external_id": str(row.get("id") or row.get("display_id") or ""),
                           "host": row.get("host"), "user": row.get("user"),
                           "process": row.get("process_name")}
            if seen is None:
                out[box_id]["seen_at"] = now
                out[box_id]["stale"] = True
        return out

    def fresh_callbacks(self):
        now = self._now()
        return {box: rec for box, rec in self.callbacks().items()
                if rec.get("seen_at") is not None
                and not rec.get("stale")
                and (now - rec["seen_at"]) <= self.callback_max_age}

    def callback_for(self, box_id):
        rec = self.callbacks().get(box_id)
        if not rec:
            return None
        return rec

    def callback_id_for(self, box_id):
        for row in self.callback_rows():
            if (row.get("description") or row.get("host") or "").strip() == box_id:
                return row.get("id") or row.get("display_id")
        return None

    def task(self, box_id, command, *, timeout=120, poll_interval=2.0):
        if not self.enabled:
            raise C2Error("c2 disabled (c2.enabled / c2.base_url)")
        callback_id = self.callback_id_for(box_id)
        if callback_id is None:
            return {"ok": False, "detail": f"no callback for {box_id}",
                    "output": "", "task_id": None}
        created = (self._graphql_call(self.CREATE_TASK,
                                      {"callback_id": int(callback_id),
                                       "command": command}).get("createTask") or {})
        if created.get("error"):
            return {"ok": False, "detail": f"task creation: {created['error']}",
                    "output": "", "task_id": None}
        task_id = created.get("id")
        if task_id is None:
            return {"ok": False, "detail": f"task creation returned {str(created)[:120]}",
                    "output": "", "task_id": None}
        deadline = self._now() + timeout
        last, output = {}, ""
        while self._now() < deadline:
            data = self._graphql_call(self.TASK_QUERY, {"id": int(task_id)})
            rows = data.get("task") or []
            last = rows[0] if rows and isinstance(rows[0], dict) else {}
            output = _responses_text(data.get("response") or []) or _task_output(last)
            status = str(last.get("status") or "").lower()
            if status in TERMINAL_BAD:
                return {"ok": False, "detail": f"task {status}",
                        "output": output, "task_id": task_id}
            if status in TERMINAL_OK or last.get("completed"):
                return {"ok": True, "output": output,
                        "task_id": task_id, "status": status}
            time.sleep(poll_interval)
        return {"ok": False, "detail": f"task {task_id} not finished within {timeout}s",
                "output": output, "task_id": task_id}

    # comp-prep only — never mid-event.
    def build_payload(self, box_id, os_name="linux", transport="http",
                      c2_profile="http", extra=None):
        """The profile's callback params must ride the build: left on the
        default loopback callback_host, the payload can never reach the range."""
        agent = AGENT_BY_OS.get(os_name, "poseidon")
        profile: dict = {"c2_profile": c2_profile}
        params = self.profile_parameters(extra)
        if params:
            profile["c2_profile_parameters"] = params
        definition = {"selected_os": OS_NAME.get(os_name.lower(), os_name),
                      "filename": f"{box_id}-{transport}.bin",
                      "description": box_id, "payload_type": agent,
                      "commands": [], "c2_profiles": [profile],
                      "build_parameters": []}
        created = (self._graphql_call(
            self.CREATE_PAYLOAD, {"payload": json.dumps(definition)}).get("createPayload") or {})
        uuid = created.get("uuid")
        if not uuid:
            raise C2Error(f"payload build for {box_id} returned {str(created)[:120]}")
        return {"uuid": str(uuid), "box": box_id, "agent": agent,
                "transport": transport, "description": box_id,
                "callback": self.callback_summary()}

    def profile_parameters(self, extra=None):
        params = {}
        if self.callback_host:
            params["callback_host"] = self.callback_host
        if self.callback_port:
            params["callback_port"] = int(self.callback_port)
        for key, value in (extra or {}).items():
            if value not in (None, ""):
                params[key] = value
        return params

    def callback_summary(self):
        return {"host": self.callback_host, "port": self.callback_port}

    def wait_for_payload(self, uuid, timeout=600, poll_interval=3.0):
        deadline = self._now() + timeout
        last = {}
        while self._now() < deadline:
            last = self.payload_status(uuid)
            phase = str(last.get("build_phase") or "").lower()
            # "success" is the Mythic 4 finished phase; "built" is the older REST shape.
            if phase in ("built", "success", "completed"):
                return last
            if phase in ("error", "failed"):
                raise C2Error(f"payload {uuid} build {phase}: "
                              f"{(last.get('build_message') or '')[:300]}")
            time.sleep(poll_interval)
        raise C2Error(f"payload {uuid} still {last.get('build_phase')!r} after {timeout}s")

    def payload_status(self, uuid):
        data = self._graphql_call(self.PAYLOAD_QUERY, {"uuid": uuid})
        rows = data.get("payload") or []
        return rows[0] if rows and isinstance(rows[0], dict) else {}

    def download_payload(self, uuid, path, timeout=600):
        status = self.wait_for_payload(uuid, timeout=timeout)
        file_id = (status.get("filemetum") or {}).get("agent_file_id")
        if not file_id:
            raise C2Error(f"payload {uuid} built without a file id")
        blob = self._request("GET", f"/direct/download/{file_id}", raw=True)
        if not blob:
            raise C2Error(f"payload {uuid} downloaded 0 bytes from "
                          f"/direct/download/{file_id}")
        with open(path, "wb") as fh:
            fh.write(blob)
        return path

    def operation(self):
        return {"name": self.operation_name,
                "callbacks": len(self.callbacks())}


def _epoch(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        import datetime
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _responses_text(rows):
    chunks = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            chunk = base64.b64decode(str(row.get("response_text") or "")).decode(
                "utf-8", "replace")
        except (ValueError, TypeError):
            continue
        if chunk:
            chunks.append(chunk)
    return "".join(chunks)


def _task_output(row):
    if not row:
        return ""
    for key in ("response", "output", "stdout"):
        value = row.get(key)
        if not value:
            continue
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError:
                return value
            value = parsed
        if isinstance(value, dict):
            return str(value.get("output") or value.get("stdout") or value)[:4096]
        return str(value)[:4096]
    return ""
