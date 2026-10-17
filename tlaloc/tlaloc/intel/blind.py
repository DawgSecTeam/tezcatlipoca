"""Blind intel: ROE scope + generic priors. The safe default (and the
fallback mode when no competition artifacts are staged on red01)."""

from .base import Intel

COMMON_USERS = ["root", "ubuntu", "admin", "Administrator", "analyst",
                "svc-backup", "fieldtech", "dev", "deploy", "ops",
                "sysadmin", "user", "test", "demo"]

COMMON_PASSWORDS = ["", "password", "Password1!", "password123", "admin",
                    "letmein", "changeme", "Welcome1!", "P@ssw0rd", "ubuntu"]


class BlindIntel(Intel):
    source_name = "blind"

    def candidate_users(self):
        return list(COMMON_USERS)

    def candidate_passwords(self):
        return list(COMMON_PASSWORDS)

    def services_for(self, ip):
        return []          # recon findings merge in via world targets
