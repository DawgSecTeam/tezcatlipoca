from pathlib import Path

# Boxes boot with an empty /etc/resolv.conf — cloud-init's dns.servers is silently ignored on
# Debian once the interface has a static IP. Two separate places have to repair it and must not
# drift apart: terraform/scripts/prepare_boxes.py (before nakon installs anything, since every
# install is an apt-get that has to resolve a mirror) and fix_dns_on_boxes() in
# create-competition.py (again after clone_team_boxes(), because `cloud-init clean` + reboot
# regenerates resolv.conf on the clones and undoes the first fix).
DNS_FIX_CMD = (
    'printf "nameserver 8.8.8.8\\n" | sudo tee /etc/resolv.conf; '
    'printf "nameserver 8.8.8.8\\n" | sudo tee /etc/resolv.conf.head; '
    "sudo mkdir -p /etc/systemd/resolved.conf.d; "
    'printf "[Resolve]\\nDNS=8.8.8.8\\n" | sudo tee /etc/systemd/resolved.conf.d/upstream.conf; '
    "sudo systemctl restart systemd-resolved 2>/dev/null || true"
)


# The one account main.tf's initialization.user_account block creates on every box clone
# (nakon authenticates with a password, not a key, hence the fixed password). Quotient's
# credlist has to carry exactly this pair or its login checks score a box that is actually
# healthy as down — keep in sync with main.tf's user_account block if it ever changes.
BOX_USERNAME = "ubuntu"
BOX_PASSWORD = "ubuntu"


def load_compfile(path):
    name = None
    scenario = None
    difficulty = None

    with open(path) as f:
        for line in f:
            key, value = line.strip().split(" ", 1)
            if key == "name":
                name = value
            elif key == "scenario":
                scenario = value
            elif key == "difficulty":
                difficulty = int(value)

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
