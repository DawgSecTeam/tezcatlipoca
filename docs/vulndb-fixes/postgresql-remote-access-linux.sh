#!/bin/bash
# postgresql-remote-access (linux) -- fixed body
#
# Finding (deliberately preserved): PostgreSQL listens on every interface
# (`listen_addresses = '*'`) and pg_hba.conf carries `trust` lines for
# 0.0.0.0/0 and ::/0, so any host that can reach 5432 gets unauthenticated
# access as any role. Its dependency postgresql-no-auth supplies the local and
# loopback half.
#
# Defects fixed (reproduced live on noble / debian13 / fedora44):
#  1. `sed -i "s/^#*listen_addresses=.*/listen_addresses = '*'/"` requires the
#     key and `=` to be adjacent, but every shipped postgresql.conf writes
#     `#listen_addresses = 'localhost'` (spaces) -- so the replacement silently
#     no-opped, the server stayed bound to loopback, and the whole network-trust
#     finding was absent (connection refused from the box's LAN address).
#     The file is now edited by pattern with optional whitespace.
#  2. `systemctl restart postgresql || systemctl restart postgresql-14 || true`
#     guessed a version-pinned unit and swallowed the failure. The unit is now
#     detected and the restart rc propagated.
#  3. The appended IPv6 line was written as `::0/0`; `::/0` is used (both parse,
#     but the canonical form is what a blue-teamer would expect to find).
#  4. Nothing proved the server was reachable, so `pg_isready` is polled and the
#     script performs a real unauthenticated psql connect to the box's own LAN
#     address (which can only succeed if the 0.0.0.0/0 trust line matched).
#
# Idempotent: the listen_addresses rewrite is a pure function of the file and the
# host lines are appended only when absent.
set -euo pipefail

# ---- 0. find the cluster config (postgresql-no-auth should have installed it) --
confs=""
hbas=""
for f in /etc/postgresql/*/main/postgresql.conf /var/lib/pgsql/data/postgresql.conf /var/lib/postgresql/data/postgresql.conf; do
    [ -f "$f" ] && confs="$confs $f"
done
for f in /etc/postgresql/*/main/pg_hba.conf /var/lib/pgsql/data/pg_hba.conf /var/lib/postgresql/data/pg_hba.conf; do
    [ -f "$f" ] && hbas="$hbas $f"
done
if [ -z "$hbas" ]; then
    echo "postgresql-remote-access: no pg_hba.conf found -- is postgresql-no-auth planted?" >&2
    exit 1
fi

# ---- 1. listen on every interface -------------------------------------------
for conf in $confs; do
    if grep -Eq '^[[:space:]]*listen_addresses[[:space:]]*=' "$conf"; then
        sed -i "s|^[[:space:]]*listen_addresses[[:space:]]*=.*|listen_addresses = '*'|" "$conf"
    else
        printf "\nlisten_addresses = '*'\n" >>"$conf"
    fi
done

# ---- 2. the finding: trust from anywhere ------------------------------------
for hba in $hbas; do
    grep -Eq '^[[:space:]]*host[[:space:]]+all[[:space:]]+all[[:space:]]+0\.0\.0\.0/0[[:space:]]+trust([[:space:]]|$)' "$hba" ||
        echo "host    all             all             0.0.0.0/0               trust" >>"$hba"
    grep -Eq '^[[:space:]]*host[[:space:]]+all[[:space:]]+all[[:space:]]+::0?/0[[:space:]]+trust([[:space:]]|$)' "$hba" ||
        echo "host    all             all             ::/0                    trust" >>"$hba"
done

# ---- 3. (re)start the real unit and prove it is up --------------------------
pg_restart() {
    local unit="" v c
    if command -v systemctl >/dev/null 2>&1; then
        if [ "$(systemctl show -p LoadState --value postgresql.service 2>/dev/null)" = loaded ]; then
            unit=postgresql.service
        elif command -v pg_lsclusters >/dev/null 2>&1; then
            v=$(pg_lsclusters -h 2>/dev/null | awk 'NR==1{print $1}')
            c=$(pg_lsclusters -h 2>/dev/null | awk 'NR==1{print $2}')
            [ -n "$v" ] && [ -n "$c" ] && unit="postgresql@${v}-${c}.service"
        fi
        if [ -n "$unit" ]; then
            systemctl enable "$unit" >/dev/null 2>&1 || true
            systemctl restart "$unit"
            return 0
        fi
    fi
    if command -v pg_ctlcluster >/dev/null 2>&1; then
        v=$(pg_lsclusters -h 2>/dev/null | awk 'NR==1{print $1}')
        c=$(pg_lsclusters -h 2>/dev/null | awk 'NR==1{print $2}')
        pg_ctlcluster "$v" "$c" restart
        return 0
    fi
    echo "postgresql-remote-access: could not find a PostgreSQL service unit to restart" >&2
    return 1
}
pg_restart

ready=1
i=0
while [ "$i" -lt 60 ]; do
    if pg_isready -q 2>/dev/null; then ready=0; break; fi
    i=$((i + 1))
    sleep 1
done
if [ "$ready" -ne 0 ]; then
    echo "postgresql-remote-access: server did not become ready within 60s" >&2
    pg_isready >&2 2>&1 || true
    exit 1
fi

# ---- 4. assert the finding: it is really listening on a routable address, and
#         a non-postgres OS user gets in over the network with no password ------
lanip=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -z "$lanip" ]; then
    lanip=$(ip -4 -o addr show scope global 2>/dev/null | awk '{split($4,a,"/"); print a[1]; exit}')
fi
if [ -z "$lanip" ]; then
    echo "postgresql-remote-access: could not determine a LAN address to test against" >&2
    exit 1
fi

if ! ss -lnt 2>/dev/null | grep -Eq '(^|[[:space:]])(0\.0\.0\.0|\*|\[::\]):5432'; then
    echo "postgresql-remote-access: postgres is not listening on a wildcard address (listen_addresses did not apply)" >&2
    ss -lnt >&2 2>&1 || true
    exit 1
fi

if command -v psql >/dev/null 2>&1; then
    if ! env -u PGPASSWORD psql -w -U postgres -h "$lanip" -tAc 'select 1' >/dev/null 2>&1; then
        echo "postgresql-remote-access: network 'trust' auth is NOT live (psql to $lanip:5432 as $(id -un) -> postgres failed)" >&2
        env -u PGPASSWORD psql -w -U postgres -h "$lanip" -tAc 'select 1' >&2 2>&1 || true
        exit 1
    fi
else
    echo "postgresql-remote-access: psql not installed; asserting pg_hba structurally" >&2
    grep -Eq '^[[:space:]]*host[[:space:]]+all[[:space:]]+all[[:space:]]+0\.0\.0\.0/0[[:space:]]+trust([[:space:]]|$)' $hbas ||
        { echo "postgresql-remote-access: no 0.0.0.0/0 trust line in pg_hba.conf" >&2; exit 1; }
fi
