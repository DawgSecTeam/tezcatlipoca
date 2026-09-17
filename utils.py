import json
from pathlib import Path

# cloud-init ignores dns.servers with static IP; fix via resolv.conf + systemd-resolved.
DNS_FIX_CMD = (
    'printf "nameserver 8.8.8.8\\n" | sudo tee /etc/resolv.conf; '
    'printf "nameserver 8.8.8.8\\n" | sudo tee /etc/resolv.conf.head; '
    "sudo mkdir -p /etc/systemd/resolved.conf.d; "
    'printf "[Resolve]\\nDNS=8.8.8.8\\n" | sudo tee /etc/systemd/resolved.conf.d/upstream.conf; '
    "sudo systemctl restart systemd-resolved 2>/dev/null || true"
)


# Fallback when no users.json exists; themeable per competition.
BOX_USERNAME_DEFAULT = "ubuntu"
CREDLIST_USERNAMES_DEFAULT = ["admin", "user1", "user2"]


def load_users_config(comp_dir):
    """Load themeable box login + credlist usernames from users.json, or fall back to defaults."""

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


def compfile_flag(path, key, default=0):
    """Read an integer Compfile knob (e.g. `team_beacons 1`); default when the
    key or the file is absent, so old Compfiles keep their behavior."""
    try:
        with open(path) as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(key + " "):
                    try:
                        return int(stripped.split(" ", 1)[1].strip())
                    except ValueError:
                        return default
    except FileNotFoundError:
        pass
    return default


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
