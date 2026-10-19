"""One SSH transport for both OSes (paramiko, pooled, imported lazily)."""

import subprocess

from ..errors import TransportError
from . import winexec

# Without keepalive a half-dead link (session gone, no RST, NAT/conntrack drop) stays ESTAB
# forever and wedges the next blocked wait.
_KEEPALIVE_SEC = 15
_READ_TIMEOUT_DEFAULT = 300


class SSH:
    def __init__(self, cfg, state_dir):
        self.action_timeout = cfg["limits"]["action_timeout"]
        self._clients = {}
        # The persistence sshkey family plants this key's public half; when blue closes
        # password auth, the private half is the way back in.
        from pathlib import Path
        key = Path(state_dir) / "red_key"
        self.reentry_key = str(key) if key.exists() else None
        self._key_auth = set()  # (ip, user) pairs whose password path blue closed

    def run_local(self, cmd, timeout=None, dry_run=False):
        """Run a command on red01 (recon tooling). Returns (rc, out, err)."""
        if dry_run:
            return 0, f"[dry-run] {cmd}", ""
        try:
            p = subprocess.run(cmd, shell=isinstance(cmd, str),
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=timeout or self.action_timeout)
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            return 124, "", f"timeout after {timeout or self.action_timeout}s"

    def _connect(self, ip, user, password, key_path=None):
        import paramiko  # lazy: status/report/config work without it installed

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        opts = dict(timeout=15, allow_agent=False, look_for_keys=False)
        try:
            if key_path:
                client.connect(ip, username=user, key_filename=key_path, **opts)
            elif (ip, user) in self._key_auth and self.reentry_key:
                client.connect(ip, username=user, key_filename=self.reentry_key, **opts)
            else:
                try:
                    client.connect(ip, username=user, password=password, **opts)
                except paramiko.AuthenticationException:
                    if not self.reentry_key:
                        raise
                    client.connect(ip, username=user,
                                   key_filename=self.reentry_key, **opts)
                    self._key_auth.add((ip, user))
        except Exception:
            # A client that never connected still owns a socket; close it or
            # it leaks exactly like a dropped cached one.
            try:
                client.close()
            except Exception:
                pass
            raise
        client.get_transport().set_keepalive(_KEEPALIVE_SEC)
        return client

    def _close_client(self, key, client=None):
        if client is None:
            client = self._clients.get(key)
        self._clients.pop(key, None)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _client(self, ip, user, password, key_path=None):
        key = (ip, user)
        client = self._clients.get(key)
        if client is not None:
            try:
                client.transport.is_active()
                return client
            except Exception:
                pass
            self._close_client(key, client)  # dead: a dropped client owns a socket + a thread
        client = self._connect(ip, user, password, key_path=key_path)
        self._clients[key] = client
        return client

    def ssh(self, ip, user, password, cmd, timeout=None, windows=False,
            key_path=None):
        """Run a command. Returns (rc, stdout, stderr).

        exec_command's timeout only gates reads and recv_exit_status waits on the status
        event with no timeout of its own, so status_event.wait(tmo) is mandatory and a breach
        closes the pooled client and raises TransportError instead of hanging forever.
        """
        tmo = timeout or self.action_timeout or _READ_TIMEOUT_DEFAULT
        try:
            client = self._client(ip, user, password, key_path=key_path)
            if windows:
                cmd = winexec.wrap(cmd)
            _, out, err = client.exec_command(cmd, timeout=tmo)
            # Drain both streams before the status wait: paramiko deadlocks once output
            # exceeds the channel window.
            out_bytes = out.read()
            err_bytes = err.read()
            chan = out.channel
            if not chan.status_event.wait(timeout=tmo):
                raise TransportError(
                    f"ssh {user}@{ip}: no exit status after {tmo}s (command hung)")
            rc = chan.recv_exit_status()
            return rc, out_bytes.decode(errors="replace"), err_bytes.decode(errors="replace")
        except TransportError:
            self._close_client((ip, user))
            raise
        except Exception as exc:
            self._close_client((ip, user))
            raise TransportError(f"ssh {user}@{ip}: {exc}") from exc

    def ssh_ps(self, ip, user, password, script, timeout=None, key_path=None):
        """Windows: run a multiline/quote-heavy script via EncodedCommand."""
        return self.ssh(ip, user, password, winexec.ps(script),
                        timeout=timeout, key_path=key_path)

    def ssh_ok(self, ip, user, password, windows=False, key_path=None):
        """Cheap foothold liveness probe."""
        try:
            # PS 5.1 cannot parse `id 2>/dev/null || whoami` (no ||, no
            # /dev/null) — auth would succeed and the probe would report fail.
            probe = "whoami" if windows else "id 2>/dev/null || whoami"
            rc, out, _ = self.ssh(ip, user, password, probe, timeout=20,
                                  windows=windows, key_path=key_path)
            return rc == 0, out.strip()
        except TransportError:
            return False, ""

    def sftp_put(self, ip, user, password, local_path, remote_path,
                 mode=0o755, key_path=None):
        try:
            client = self._client(ip, user, password, key_path=key_path)
            sftp = client.open_sftp()
            try:
                sftp.put(local_path, remote_path)
                try:
                    sftp.chmod(remote_path, mode)
                except Exception:
                    # Windows OpenSSH's sftp maps chmod to the read-only
                    # attribute and can refuse; the upload is what matters.
                    pass
            finally:
                sftp.close()
            return True
        except Exception as exc:
            self._close_client((ip, user))
            raise TransportError(f"sftp put to {user}@{ip}: {exc}") from exc

    def sftp_get(self, ip, user, password, remote_path, local_path, key_path=None):
        try:
            client = self._client(ip, user, password, key_path=key_path)
            sftp = client.open_sftp()
            try:
                sftp.get(remote_path, local_path)
            finally:
                sftp.close()
            return True
        except Exception as exc:
            self._close_client((ip, user))
            raise TransportError(f"sftp get from {user}@{ip}: {exc}") from exc

    def close(self):
        for client in self._clients.values():
            try:
                client.close()
            except Exception:
                pass
        self._clients.clear()
