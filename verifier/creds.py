"""Admin password lookup."""

from verifier.loaders import read_credentials_lines

DEFAULT_ADMIN_PASSWORD = "changeme123"


def load_admin_password(comp_dir, override):
    if override:
        return override
    # The state document is the authority (credentials.txt is a generated handoff that a
    # redeploy/resync may not have rewritten).
    from estate import state as estate_state
    recorded = estate_state.load_flat(comp_dir).get("admin_password")
    if recorded:
        return recorded
    for line in read_credentials_lines(comp_dir) or []:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "admin":
            return parts[1]
    return DEFAULT_ADMIN_PASSWORD

