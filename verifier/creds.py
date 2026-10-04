"""Credential gates: admin password lookup and the default-credential regression guard."""

from verifier.loaders import read_credentials_lines

DEFAULT_ADMIN_PASSWORD = "changeme123"


_DEFAULT_CRED_LITERALS = {"changeme123", "password1", "password2", "ubuntu"}


def load_admin_password(comp_dir, override):
    if override:
        return override
    for line in read_credentials_lines(comp_dir) or []:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "admin":
            return parts[1]
    return DEFAULT_ADMIN_PASSWORD


def check_no_default_creds(comp_dir):
    """Confirm credentials.txt doesn't carry default literal passwords (rotation guard)."""
    lines = read_credentials_lines(comp_dir)
    if lines is None:
        print("  (no credentials.txt to check for default creds)")
        return True
    box_lines = [l for l in lines if l.startswith("box-")]
    if not box_lines:
        print("  (credentials.txt has no box-login/box-credlist lines — pre-rotation "
              "competition, or credential rotation isn't wired up)")
        return True
    bad = [l for l in box_lines if l.split()[-1] in _DEFAULT_CRED_LITERALS]
    if bad:
        print("  FAIL  credentials.txt still carries a default credential literal:")
        for l in bad:
            print(f"      {l}")
        return False
    print(f"  PASS  {len(box_lines)} box credential line(s), no default literals found")
    return True
