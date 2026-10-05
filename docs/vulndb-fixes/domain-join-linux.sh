#!/bin/bash
set -e
if command -v apt-get > /dev/null; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y realmd sssd sssd-tools adcli samba-common-bin krb5-user packagekit libpam-sss libnss-sss
elif command -v dnf > /dev/null; then
    dnf install -y realmd sssd sssd-tools adcli samba-common-tools krb5-workstation oddjob oddjob-mkhomedir
    systemctl enable --now oddjobd
elif command -v yum > /dev/null; then
    yum install -y realmd sssd sssd-tools adcli samba-common-tools krb5-workstation oddjob oddjob-mkhomedir
    systemctl enable --now oddjobd
else
    echo "[domain-join] No supported package manager found" >&2; exit 1
fi

# realmd refuses to join a box whose hostname is still the unconfigured
# localhost/localhost.localdomain placeholder -- it would otherwise try to
# register an AD computer object literally named LOCALHOST, so realm join
# fails outright with "This computer's host name is not set correctly."
# If BOX_HOSTNAME is supplied and the box still has the placeholder name,
# fix it before continuing. Leaves an already-customized hostname alone.
CURRENT_HOSTNAME=$(hostnamectl --static)
if [ -n "$BOX_HOSTNAME" ] && { [ "$CURRENT_HOSTNAME" = "localhost" ] || [ "$CURRENT_HOSTNAME" = "localhost.localdomain" ] || [ -z "$CURRENT_HOSTNAME" ]; }; then
    hostnamectl set-hostname "$BOX_HOSTNAME"
    echo "[domain-join] hostname was unset/default ($CURRENT_HOSTNAME), set to $BOX_HOSTNAME"
fi

# The DC has to be this box's DNS server before discovery/join can work -- getting DNS from
# the firewall/gateway instead (the usual default) makes realm discover fail to find the domain.
IFACE=$(ip route show default | awk '{print $5; exit}')
if command -v resolvectl > /dev/null && systemctl is-active --quiet systemd-resolved; then
    resolvectl dns "$IFACE" "$DC_IP"
    resolvectl domain "$IFACE" "$DOMAIN"
elif command -v nmcli > /dev/null && [ -n "$(nmcli -t -f NAME connection show --active)" ]; then
    CONN=$(nmcli -t -f NAME connection show --active | head -1)
    # nmcli up can fail (or churn the profile) on cloud-init static boxes — never
    # fatal: fall back to the resolv.conf insert (amongus-cde 2026-09-30: airship's
    # join died rc=1 in 3s here under set -e).
    nmcli connection modify "$CONN" ipv4.dns "$DC_IP" ipv4.ignore-auto-dns yes &&         nmcli connection up "$CONN" > /dev/null ||         sed -i "1i nameserver $DC_IP" /etc/resolv.conf
else
    sed -i "1i nameserver $DC_IP" /etc/resolv.conf
fi

# Fedora/RHEL's stock krb5.conf leaves dns_lookup_kdc unset (MIT default: off) and has an empty
# [realms], so realm discover finds the domain (SRV via the DC's DNS) but adcli's kinit then fails
# with: Cannot find KDC for realm "<DOMAIN>". Pin the realm's KDC in a drop-in. (Debian/Ubuntu
# boxes without /etc/krb5.conf.d are unaffected.) Live-found 2026-10-05, pfsense-rvb app01.
if [ -d /etc/krb5.conf.d ]; then
    cat > /etc/krb5.conf.d/10-domain-join.conf <<EOF
[libdefaults]
    dns_lookup_kdc = true

[realms]
    ${DOMAIN^^} = {
        kdc = $DC_IP
    }
EOF
fi

realm discover "$DOMAIN" > /dev/null
if realm list | grep -qx "$DOMAIN"; then
    echo "[domain-join] already joined to $DOMAIN, skipping realm join"
else
    echo "$DOMAIN_ADMIN_PASS" | realm join --user="$DOMAIN_ADMIN_USER" "$DOMAIN"
fi

cat > /etc/sssd/sssd.conf <<EOF
[sssd]
domains = $DOMAIN
config_file_version = 2
services = nss, pam

[domain/$DOMAIN]
default_shell = /bin/bash
krb5_store_password_if_offline = True
cache_credentials = True
krb5_realm = ${DOMAIN^^}
realmd_tags = manages-system joined-with-adcli
id_provider = ad
fallback_homedir = /home/%u
ad_domain = $DOMAIN
use_fully_qualified_names = False
ldap_id_mapping = True
access_provider = ad
EOF
chmod 600 /etc/sssd/sssd.conf
chown root:root /etc/sssd/sssd.conf
systemctl restart sssd

if command -v pam-auth-update > /dev/null; then
    pam-auth-update --enable mkhomedir
elif command -v authselect > /dev/null; then
    authselect enable-feature with-mkhomedir
    authselect apply-changes
fi

echo "[domain-join] Joined $DOMAIN"
