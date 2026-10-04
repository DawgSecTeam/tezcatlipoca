"""Facade: post-nakon hardening and DNS/auth fixes. The code lives in box_settle_ops,
apt_dns_ops, alpine_ops, service_fixup_ops and ubuntu_auth_ops; every name stays importable from here."""

from box_settle_ops import (  # noqa: F401
    _SETTLE_CHECK,
    _box_settled,
    _box_settled_via_ssh,
    wait_boxes_settled,
)
from apt_dns_ops import (  # noqa: F401
    _APT_PREP_BODY,
    _apt_prep_script,
    prep_apt_on_boxes,
    fix_dns_on_boxes,
)
from alpine_ops import (  # noqa: F401
    ALPINE_SERVICES,
    _ALPINE_NGINX_200_VHOST_B64,
    ensure_alpine_services,
)
from service_fixup_ops import (  # noqa: F401
    _ServiceFixup,
    _credlist_account_lines,
    _mysql_fixup,
    _postfix_fixup,
    _dovecot_fixup,
    SERVICE_FIXUPS,
    SERVICE_FIXUP_ORDER,
    service_fixup_script,
    fix_services_on_boxes,
    mysql_credlist_reensure_script,
    reensure_mysql_credlist_users,
)
from ubuntu_auth_ops import (  # noqa: F401
    setup_ubuntu_auth,
)

__all__ = [
    '_SETTLE_CHECK',
    '_box_settled',
    '_box_settled_via_ssh',
    'wait_boxes_settled',
    '_APT_PREP_BODY',
    '_apt_prep_script',
    'prep_apt_on_boxes',
    'fix_dns_on_boxes',
    'ALPINE_SERVICES',
    '_ALPINE_NGINX_200_VHOST_B64',
    'ensure_alpine_services',
    '_ServiceFixup',
    '_credlist_account_lines',
    '_mysql_fixup',
    '_postfix_fixup',
    '_dovecot_fixup',
    'SERVICE_FIXUPS',
    'SERVICE_FIXUP_ORDER',
    'service_fixup_script',
    'fix_services_on_boxes',
    'mysql_credlist_reensure_script',
    'reensure_mysql_credlist_users',
    'setup_ubuntu_auth',
]
