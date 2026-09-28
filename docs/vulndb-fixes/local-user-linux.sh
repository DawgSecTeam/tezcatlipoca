#!/bin/sh
set -eu

: "${USERNAME:?USERNAME is required}"

if ! id "$USERNAME" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "$USERNAME"
fi

if [ -n "${PASSWORD-}" ]; then
    printf '%s:%s\n' "$USERNAME" "$PASSWORD" | chpasswd
else
    # An empty password is intentional: it is the finding for the exercise.
    usermod -p '' "$USERNAME"
fi

if [ -n "${GROUPS_ADD-}" ]; then
    # The admin group differs by distro: Debian/Ubuntu use `sudo`, RHEL/Fedora use `wheel`.
    # Map each requested group to whichever the box actually has (so an "unauthorized sudo
    # membership" finding still grants admin on Fedora), and create any other genuinely-missing
    # group, so `usermod -a -G` never fails rc=6 ("group does not exist") on a non-Debian box.
    _grps=""
    _oifs=$IFS
    IFS=,
    for _g in $GROUPS_ADD; do
        if [ "$_g" = sudo ] && ! getent group sudo >/dev/null 2>&1 && getent group wheel >/dev/null 2>&1; then
            _g=wheel
        elif [ "$_g" = wheel ] && ! getent group wheel >/dev/null 2>&1 && getent group sudo >/dev/null 2>&1; then
            _g=sudo
        fi
        getent group "$_g" >/dev/null 2>&1 || groupadd "$_g"
        _grps="${_grps:+$_grps,}$_g"
    done
    IFS=$_oifs
    usermod -a -G "$_grps" "$USERNAME"
fi
