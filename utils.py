import json
from pathlib import Path

# Boxes boot with an empty /etc/resolv.conf — cloud-init's dns.servers is silently ignored on
# Debian once the interface has a static IP. fix_dns_on_boxes() in create-competition.py
# repairs it: once for team1 in phase 5 (before nakon installs anything, since every install
# is an apt-get that has to resolve a mirror) and again after clone_team_boxes(), because
# `cloud-init clean` + reboot regenerates resolv.conf on the clones and undoes the first fix.
# (The old terraform/scripts/prepare_boxes.py half of this is gone — that script is dead.)
DNS_FIX_CMD = (
    'printf "nameserver 8.8.8.8\\n" | sudo tee /etc/resolv.conf; '
    'printf "nameserver 8.8.8.8\\n" | sudo tee /etc/resolv.conf.head; '
    "sudo mkdir -p /etc/systemd/resolved.conf.d; "
    'printf "[Resolve]\\nDNS=8.8.8.8\\n" | sudo tee /etc/systemd/resolved.conf.d/upstream.conf; '
    "sudo systemctl restart systemd-resolved 2>/dev/null || true"
)


# The account main.tf's initialization.user_account block creates on every box clone (nakon
# authenticates with a password, not a key, hence a password rather than a key). The
# *username* used to be a fixed, non-secret constant everywhere; it's now themeable per
# competition via competitions/<id>/users.json (see load_users_config() below) — these two
# constants are fallback/reference values only, used when no users.json exists. BOX_PASSWORD
# is additionally only ever used by quotient/setup.py's build_credlist(), which is itself dead
# code kept for reference — see its docstring — it is NOT the value any real deployment uses.
BOX_USERNAME_DEFAULT = "ubuntu"
BOX_PASSWORD = "ubuntu"
CREDLIST_USERNAMES_DEFAULT = ["admin", "user1", "user2"]


def load_users_config(comp_dir):
    """Read competitions/<id>/users.json (optional) for the themeable box login username and
    the three credlist account names Quotient's Ssh/Smtp/Imap/Sql/Ftp checks authenticate
    against. Returns (box_username, credlist_usernames), falling back to
    (BOX_USERNAME_DEFAULT, CREDLIST_USERNAMES_DEFAULT) when the file is absent or a key is
    missing — every existing competition directory (no users.json) keeps behaving exactly as
    it did before this config existed.
    """
    path = Path(comp_dir) / "users.json"
    if not path.exists():
        return BOX_USERNAME_DEFAULT, list(CREDLIST_USERNAMES_DEFAULT)

    data = json.loads(path.read_text())
    box_username = data.get("box_username") or BOX_USERNAME_DEFAULT
    credlist_usernames = data.get("credlist_usernames") or list(CREDLIST_USERNAMES_DEFAULT)
    return box_username, credlist_usernames


def load_compfile(path):
    name = ""
    scenario = ""
    difficulty = 0

    with open(path) as f:
        for line in f:
            stripped = line.strip()
            if not stripped or " " not in stripped:
                continue  # skip blank lines and keys with no value
            key, value = stripped.split(" ", 1)
            if key == "name":
                name = value
            elif key == "scenario":
                scenario = value
            elif key == "difficulty":
                try:
                    difficulty = int(value)
                except ValueError:
                    difficulty = 0

    return name, scenario, difficulty


def pick_competition(competitions, label="saved", action="Select a competition"):
    print(f"Found {len(competitions)} {label} competition(s):\n")
    for i, comp in enumerate(competitions, 1):
        name, scenario, difficulty = load_compfile(f"competitions/{comp}/Compfile")
        short_scenario = scenario[:80] + ("..." if len(scenario) > 80 else "")
        print(f"  [{i}] {comp}  (difficulty: {difficulty}/10)")
        print(f"       {name}")
        print(f"       {short_scenario}")
        print()

    while True:
        choice = input(f"{action} [1–{len(competitions)}] or 'exit': ").strip()
        if choice.lower() == "exit":
            return None
        try:
            idx = int(choice)
            if 1 <= idx <= len(competitions):
                return competitions[idx - 1]
        except ValueError:
            pass
        print(f"  Please enter a number between 1 and {len(competitions)}, or 'exit'.")
