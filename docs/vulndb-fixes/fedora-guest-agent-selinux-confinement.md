# Fedora lab boxes: the QEMU guest-agent channel is SELinux-confined

Found 2026-10-02 while validating the `vulndb-2026-10-02` catalog fixes on **vmid 130
(`tz-vulnlab-f1`, Fedora 44 Cloud Edition)**. Recorded because the earlier assumption
"`pmx exec` gives you root on every lab box" is false on SELinux-enforcing Fedora goldens,
and it silently turns every privileged plant into a no-op or a permission error.

## Symptom

`pmx exec 130 -- <anything privileged>` fails with `Permission denied` /
`Access denied` even though the process runs as `uid=0`:

```
$ pmx exec 130 -- sh -c 'id'
uid=0(root) gid=0(root) groups=0(root) context=system_u:system_r:virt_qemu_ga_t:s0
```

The guest agent runs as real root but inside the SELinux domain `virt_qemu_ga_t`; on a
default-enforcing Fedora image that domain is not permitted to touch package management,
`/etc`, or the service manager.

## Exact denials observed (`pmx exec 130 -- sh /tmp/probe.sh`)

| command | result |
|---|---|
| `/usr/bin/rpm --version` | `Permission denied` |
| `/usr/bin/dnf --version` | `Permission denied` |
| `ls -l /usr/bin/dnf5` | `Permission denied` (stat of the file is denied) |
| `touch /etc/__probe` | `Permission denied` |
| `touch /var/lib/__probe` | `Permission denied` |
| `touch /run/systemd/system/__probe` | `Permission denied` |
| `systemctl daemon-reload` | `Reload daemon failed: Access denied` |
| `systemctl is-active sshd` | `Failed to retrieve unit state: Access denied` |
| `cat /etc/selinux/config` | `Permission denied` |
| `tail /var/log/audit/audit.log` | `Permission denied` |
| `cat /sys/fs/selinux/enforce` | `Permission denied` |
| `ls /sys/fs/selinux/booleans/ \| grep qemu` | shows `virt_qemu_ga_run_unconfined` (value unreadable) |
| `echo 1 > /sys/fs/selinux/booleans/virt_qemu_ga_run_unconfined` | `Permission denied` |
| `/usr/sbin/setenforce 0` | `security_setenforce() failed: Permission denied` |
| `runcon -t unconfined_service_t /usr/bin/id` | `invalid context ... Permission denied` |
| `getenforce` | `security_getenforce() failed: Permission denied` |

Writable: `/tmp`, `/var/tmp`, `/run` (not `/run/systemd/system`), `/var/log`.
Readable: most of `/usr`, `/etc/ssh/sshd_config`, `/etc/systemd/system/`.
Unavailable: any package install, any `/etc` write, any `systemctl` action.
`/usr/bin/python3` and coreutils do execute, but file writes are still checked against
`virt_qemu_ga_t`, so they cannot be used to escalate.

Net effect: on such a box the guest agent can inspect but not plant; a catalog script
run through it either no-ops or fails on a permission error unrelated to the script.

## Working channel

SSH with the deploy key, then passwordless sudo, which lands in
`unconfined_u:unconfined_r:unconfined_t:s0` with SELinux still Enforcing:

```bash
ssh -i /path/to/proxmox -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    fedora@<vm-ip>            # user fedora, key auth
ssh ... fedora@<vm-ip> 'sudo -n id'
# uid=0(root) ... context=unconfined_u:unconfined_r:unconfined_t:s0-s0:c0.c1023
```

Scripts are pushed with `scp` and run as `ssh ... 'sudo -n sh /tmp/script.sh'`. This is also
the representative path for the real pipeline, which plants over SSH.

## Takeaway

The lab VMs are reachable on the LAN (TCP 22 is open from the dev host; 125/129 accept the
guest-agent path as real root because they have no SELinux). Do **not** expect `pmx exec` to
be a privileged channel on Fedora-family goldens; use SSH+sudo there. This is the same
limitation that blocks building Fedora goldens through the guest agent on SELinux-enforcing
nodes.
