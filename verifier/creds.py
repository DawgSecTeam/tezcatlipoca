"""Admin password lookup."""

from verifier.loaders import read_credentials_lines

DEFAULT_ADMIN_PASSWORD = "changeme123"


def load_admin_password(comp_dir, override):
    if override:
        return override
    for line in read_credentials_lines(comp_dir) or []:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "admin":
            return parts[1]
    return DEFAULT_ADMIN_PASSWORD

