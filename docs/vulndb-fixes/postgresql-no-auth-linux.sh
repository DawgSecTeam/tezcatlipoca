#!/bin/bash
# postgresql-no-auth (linux) -- fixed body
#
# Finding (deliberately preserved): pg_hba.conf is rewritten so that local socket
# connections and loopback TCP connections are accepted with `trust`, i.e. any
# local account authenticates as any role -- including `postgres` -- with no
# password. postgresql-remote-access builds the network half on top of this.
#
# Defects fixed (all reproduced live on noble / debian13 / fedora44):
#  1. Debian-only cluster plumbing. The old RPM path ran
#     `pg_ctlcluster $(pg_lsclusters -h | head -1 ...) init`, both of which are
#     Debian-only: on Fedora 44 the line died `pg_lsclusters: command not found`
#     and the cluster was never initialized, so `postgresql.service` was `failed`
#     and no socket/port existed at all. RPM boxes now use
#     `postgresql-setup --initdb` (with an `initdb` fallback).
#  2. `sed 's/^local.*all.*all.*peer/.../'` never matched the shipped
#     `local all postgres peer` line, which is the first match for the postgres
#     user -- so `psql -U postgres` over the socket still failed with
#     "Peer authentication failed" and the local-trust finding did NOT land.
#     Every `local`/loopback line for database `all` is now rewritten to trust
#     whatever its shipped method was (peer/ident/md5/scram-sha-256/...).
#  3. `systemctl enable/restart postgresql || systemctl ... postgresql-14 || true`
#     guessed unit names and swallowed every failure. The unit is now detected
#     (postgresql.service, an instantiated postgresql@<ver>-<cluster>.service, or
#     pg_ctlcluster) and the restart rc is propagated.
#  4. Nothing verified the server actually came up; `pg_isready` is now polled and
#     a failure aborts the step.
#
# Idempotent: the rewrite is a pure function of the file and the service restart
# is repeatable.
set -euo pipefail

# ---- 1. install -------------------------------------------------------------
if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y postgresql
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y postgresql-server
elif command -v yum >/dev/null 2>&1; then
    yum install -y postgresql-server
elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache postgresql postgresql-client
else
    echo "postgresql-no-auth: no supported package manager (apt-get/dnf/yum/apk)" >&2
    exit 1
fi

# ---- 2. make sure an RPM cluster exists (Debian packages create theirs) ------
rpm_data=""
for d in /var/lib/pgsql/data /var/lib/postgresql/data; do
    [ -d "$d" ] && rpm_data="$d" && break
done
if [ -n "$rpm_data" ] && [ ! -s "$rpm_data/PG_VERSION" ]; then
    if command -v postgresql-setup >/dev/null 2>&1; then
        postgresql-setup --initdb
    else
        if command -v runuser >/dev/null 2>&1; then
            runuser -u postgres -- initdb -D "$rpm_data"
        else
            su -s /bin/sh postgres -c "initdb -D '$rpm_data'"
        fi
    fi
fi

# ---- 3. locate pg_hba.conf --------------------------------------------------
hbas=""
for f in /etc/postgresql/*/main/pg_hba.conf "$rpm_data/pg_hba.conf" /var/lib/postgresql/data/pg_hba.conf; do
    [ -n "$f" ] && [ -f "$f" ] && hbas="$hbas $f"
done
if [ -z "$hbas" ]; then
    echo "postgresql-no-auth: no pg_hba.conf found after install (no cluster?)" >&2
    exit 1
fi

# ---- 4. the finding: trust for local + loopback, any shipped auth method ----
for hba in $hbas; do
    tmp="${hba}.tezcatlipoca-fix"
    awk '
        /^[[:space:]]*#/ { print; next }
        NF == 0          { print; next }
        $2 != "all"      { print; next }
        $1 == "local" {
            if (NF < 4) { print; next }
            out = $1 " " $2 " " $3 " trust"
            for (i = 5; i <= NF; i++) out = out " " $i
            print out; next
        }
        $1 ~ /^host/ {
            if (NF < 5) { print; next }
            if ($4 == "127.0.0.1/32" || $4 == "::1/128") {
                out = $1 " " $2 " " $3 " " $4 " trust"
                for (i = 6; i <= NF; i++) out = out " " $i
                print out; next
            }
        }
        { print }
    ' "$hba" >"$tmp" && cat "$tmp" >"$hba" && rm -f "$tmp"

    # guarantee the finding even on a shipped file that has no such line at all
    grep -Eq '^[[:space:]]*local[[:space:]]+all[[:space:]]+all[[:space:]]+trust([[:space:]]|$)' "$hba" ||
        echo "local   all             all                                     trust" >>"$hba"
    grep -Eq '^[[:space:]]*host[[:space:]]+all[[:space:]]+all[[:space:]]+127\.0\.0\.1/32[[:space:]]+trust([[:space:]]|$)' "$hba" ||
        echo "host    all             all             127.0.0.1/32            trust" >>"$hba"
    grep -Eq '^[[:space:]]*host[[:space:]]+all[[:space:]]+all[[:space:]]+::1/128[[:space:]]+trust([[:space:]]|$)' "$hba" ||
        echo "host    all             all             ::1/128                 trust" >>"$hba"
done

# ---- 5. (re)start the real unit and prove it is up --------------------------
pg_restart() {
    local unit=""
    if command -v systemctl >/dev/null 2>&1; then
        if [ "$(systemctl show -p LoadState --value postgresql.service 2>/dev/null)" = loaded ]; then
            unit=postgresql.service
        elif command -v pg_lsclusters >/dev/null 2>&1; then
            local v c
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
        local v c
        v=$(pg_lsclusters -h 2>/dev/null | awk 'NR==1{print $1}')
        c=$(pg_lsclusters -h 2>/dev/null | awk 'NR==1{print $2}')
        pg_ctlcluster "$v" "$c" restart
        return 0
    fi
    echo "postgresql-no-auth: could not find a PostgreSQL service unit to restart" >&2
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
    echo "postgresql-no-auth: server did not become ready within 60s" >&2
    pg_isready >&2 2>&1 || true
    if command -v systemctl >/dev/null 2>&1; then
        systemctl status postgresql --no-pager -l >&2 2>&1 || true
    fi
    exit 1
fi

# ---- 6. assert the finding: a non-postgres OS user gets in with no password --
if command -v psql >/dev/null 2>&1; then
    if ! env -u PGPASSWORD psql -w -U postgres -tAc 'select 1' >/dev/null 2>&1; then
        echo "postgresql-no-auth: local 'trust' auth is NOT live (psql as $(id -un) -> postgres failed)" >&2
        env -u PGPASSWORD psql -w -U postgres -tAc 'select 1' >&2 2>&1 || true
        exit 1
    fi
else
    echo "postgresql-no-auth: psql not installed; asserting pg_hba structurally" >&2
    grep -Eq '^[[:space:]]*local[[:space:]]+all[[:space:]]+all[[:space:]]+trust([[:space:]]|$)' $hbas ||
        { echo "postgresql-no-auth: no local trust line in pg_hba.conf" >&2; exit 1; }
fi
