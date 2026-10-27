import contextlib
import os
import signal
import subprocess
import threading
from urllib.parse import urlparse

from scrim import core
from scrim.core import log


# reverse SSH tunnel so red01 (node LAN, no tailscale) can reach an operator-side LLM
# endpoint; keepalives + a restart supervisor because the dress rehearsal ran an event
# red-LLM-less after a plain `ssh -R` died unnoticed
class RedTunnel:

    def __init__(self, llm_base_url, red_ip, key, user):
        url = urlparse(llm_base_url if "//" in llm_base_url else f"http://{llm_base_url}")
        host = url.hostname or "127.0.0.1"
        port = url.port or (443 if url.scheme == "https" else 80)
        if host in ("localhost", "127.0.0.1", "::1"):
            # Base URL already names an operator-side relay — mirror that port.
            self.remote_port, self.target = port, f"127.0.0.1:{port}"
        else:
            # Endpoint reachable only from the operator: bind 8180 on red01 and
            # forward straight at it (no socat relay needed).
            self.remote_port, self.target = 8180, f"{host}:{port}"
        self.red_ip, self.key, self.user = red_ip, key, user
        self.proc = None
        self.stop = threading.Event()

    def red_base_url(self):
        return f"http://localhost:{self.remote_port}/v1"

    def _spawn(self):
        self.proc = subprocess.Popen(
            ["ssh", "-N", "-T",
             "-o", "ExitOnForwardFailure=yes",
             "-o", "ServerAliveInterval=15",
             "-o", "ServerAliveCountMax=3",
             *core.SSH_NOCHECK,
             "-o", "ConnectTimeout=15",
             "-i", self.key,
             "-R", f"{self.remote_port}:{self.target}",
             f"{self.user}@{self.red_ip}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True)

    def start(self):
        self._spawn()
        threading.Thread(target=self._supervise, daemon=True).start()

    def _supervise(self):
        while not self.stop.wait(15):
            if self.proc is None or self.proc.poll() is not None:
                log("red LLM tunnel died — restarting")
                self._spawn()

    # _supervise only notices a proc that has exited; an ssh still running but wedged
    # (no longer forwarding) needs an explicit kill+respawn — SIGKILL after a short grace
    # so a hung ssh cannot hold the forwarded port and fail the respawn with
    # ExitOnForwardFailure
    def restart(self):
        if self.proc is not None and self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.proc.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.proc.wait(timeout=10)
        self._spawn()

    def shutdown(self):
        self.stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def maybe_start_red_tunnel(args):
    mode = getattr(args, "red_tunnel", "auto") or "auto"
    local = core.is_local_endpoint(args.llm_base_url)
    if mode == "off" or (mode == "auto" and not local):
        return None
    if mode == "on" and not local:
        log("--red-tunnel ignored for an openrouter endpoint (red01 reaches it directly)")
        return None
    # A central run's engine lives in a per-run worktree with no key in it: the configured
    # path is the only reliable source.
    tunnel = RedTunnel(args.llm_base_url, args.red_ip, str(core.ssh_key_path()),
                       core.vm_username())
    tunnel.start()
    log(f"red LLM tunnel up: red01 localhost:{tunnel.remote_port} -> {tunnel.target}")
    return tunnel
