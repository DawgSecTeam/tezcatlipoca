terraform {
  required_providers {
    proxmox = { source = "bpg/proxmox", version = "~> 0.101" }
  }
  # Optional remote state — replace with local {} if running solo
  backend "local" {}
}

locals {
  # bpg otherwise asks the API for each node's address for SSH ops, which
  # may not be reachable (e.g. it returns a LAN IP but we only have Tailscale)
  proxmox_host = regex("^https?://([^/:]+)", var.proxmox_endpoint)[0]
}

provider "proxmox" {
  endpoint  = var.proxmox_endpoint  # e.g. "https://10.0.0.10:8006/"
  api_token = var.proxmox_api_token # "terraform@pve!automation=<uuid>"
  insecure  = true                  # self-signed cert on most Proxmox installs
  ssh {
    agent    = false
    username = "root"
    # Used by bpg for operations the REST API can't do (e.g. file uploads)
    private_key = file(var.ssh_private_key_path)
    node {
      name    = var.proxmox_node
      address = local.proxmox_host
    }
  }
}

# terraform/main.tf (continued)

resource "proxmox_network_linux_bridge" "team_bridge" {
  for_each  = var.teams
  node_name = var.proxmox_node
  name      = "vmbr${each.value.identifier}"
  comment   = "Quotient team ${each.key} — isolated, no uplink"
  # No 'ports' — empty bridge means no physical uplink, teams can't escape
}

resource "proxmox_virtual_environment_vm" "scoring_engine" {
  node_name = var.proxmox_node
  name      = "quotient-engine"
  vm_id     = 1000 # 100 collides with an existing unrelated VM on this node

  clone {
    vm_id = var.template_vm_id
    full  = true
  }

  agent {
    enabled = true # needed so we can read back the real DHCP-leased IP below
  }

  cpu { cores = 4 }
  memory { dedicated = 4096 }

  disk {
    datastore_id = var.datastore
    interface    = "scsi0"
    size         = 40
    discard      = "on"
  }

  # Management NIC — gets LAN IP via DHCP
  network_device {
    bridge = "vmbr0"
    model  = "virtio"
  }

  # One team-facing NIC per team, dynamically generated
  dynamic "network_device" {
    for_each = var.teams
    content {
      bridge = "vmbr${network_device.value.identifier}"
      model  = "virtio"
    }
  }

  # No cloud-init here — this box has a dynamic IP, and the template's
  # baked-in netplan (dhcp4 on the mgmt NIC) is enough on its own. The
  # template's own cloud-init build already baked in var.ssh_public_key
  # and passwordless sudo for var.vm_username, so no further bootstrap
  # is needed before null_resource.orchestrate connects.

  depends_on = [proxmox_network_linux_bridge.team_bridge]
}

# Look up all VMs tagged "template" — Packer sets this tag on every build
data "proxmox_virtual_environment_vms" "templates" {
  node_name = var.proxmox_node
  tags      = ["template"]
}

locals {
  # Build a name → vm_id map from the data source results
  template_ids = {
    for vm in data.proxmox_virtual_environment_vms.templates.vms :
    vm.name => vm.vm_id
  }

  # Terraform provisions only team1's boxes; create-competition.py clones them to
  # other teams via the Proxmox API after Nakon has run. All bridges are still
  # created here (scoring engine needs its NICs on every team bridge regardless).
  sorted_team_keys = sort(keys(var.teams))
  team1_key        = local.sorted_team_keys[0]

  team_vms = {
    for box in var.boxes_per_team : "${local.team1_key}-${box.name}" => {
      key        = "${local.team1_key}-${box.name}"
      team_key   = local.team1_key
      identifier = var.teams[local.team1_key].identifier
      box        = box
      ip         = "192.168.${var.teams[local.team1_key].identifier}.${box.last_octet}"
      gw         = "192.168.${var.teams[local.team1_key].identifier}.1"
      bridge     = "vmbr${var.teams[local.team1_key].identifier}"
    }
  }
}

resource "proxmox_virtual_environment_vm" "team_box" {
  for_each  = local.team_vms
  node_name = var.proxmox_node
  name      = each.key
  # VM IDs: 200 + team_num*10 + box_index (avoids collisions)
  vm_id = 200 + (tonumber(each.value.identifier) * 10) + index(
    [for b in var.boxes_per_team : b.name], each.value.box.name
  )

  clone {
    vm_id   = local.template_ids[each.value.box.template] # looked up by name from Packer tag
    full    = true
    retries = 15 # Proxmox locks the source VM; concurrent clones from the same template race for the
                 # lock and the loser gets a short timeout. 15 retries gives ~2 min of retry budget —
                 # enough for the winning clone to finish and release the lock before we give up.
  }

  cpu { cores = each.value.box.cpu }
  memory { dedicated = each.value.box.memory_mb }

  # Only include a disk block when disk_gb is explicitly set. Omitting it means the
  # cloned VM keeps the template's own disk as-is — specifying null causes bpg to default
  # to 8 GB which undercuts any real template disk and triggers an unsupported shrink error.
  dynamic "disk" {
    for_each = each.value.box.disk_gb != null ? [each.value.box.disk_gb] : []
    content {
      datastore_id = var.datastore
      interface    = "scsi0"
      size         = disk.value
      discard      = "on"
    }
  }

  network_device {
    bridge = each.value.bridge
    model  = "virtio"
  }

  initialization {
    ip_config {
      ipv4 {
        address = "${each.value.ip}/24"
        gateway = each.value.gw
      }
    }
    user_account {
      username = "ubuntu"
      keys     = [var.ssh_public_key]
      # nakon's paramiko connections use password auth (see quotient/setup.py) — without this
      # the account has no password hash at all and every login attempt is rejected outright
      password = "ubuntu"
    }
    # Always a public resolver, never the team's own dns* box. Pointing boxes at that box
    # deadlocks provisioning: its bind9 is installed by nakon, and nakon installs it with
    # apt-get, which needs a resolver that already works. fix_dns_on_boxes() in
    # create-competition.py forced 8.8.8.8 over the top of this anyway, so the dns* box was
    # never actually serving its team — the two mechanisms just disagreed.
    #
    # To make a dns* box its team's real resolver, repoint the boxes after nakon has run
    # (i.e. from create-competition.py), not here.
    dns {
      servers = ["8.8.8.8"]
    }
  }

  depends_on = [proxmox_network_linux_bridge.team_bridge]
}

locals {
  # ip_config just echoes back "dhcp" (the configured value); the actual
  # leased address has to come from the QEMU guest agent instead.
  #
  # Select the management address by exclusion rather than taking the first one: once
  # null_resource.team_nics runs, the engine also holds 192.168.<identifier>.1 on every team
  # NIC, and the guest agent gives no ordering guarantee. Picking one would break every scp/ssh
  # and print an unreachable scoreboard URL. Team subnets are always 192.168.0.0/16 (see
  # team_vms above), so management must live outside it — .env.example uses 10.0.0.0/8.
  scoring_mgmt_ips = [
    for ip in flatten(proxmox_virtual_environment_vm.scoring_engine.ipv4_addresses) :
    ip if !startswith(ip, "127.") && !startswith(ip, "192.168.")
  ]
  scoring_ip = local.scoring_mgmt_ips[0]
  ssh_cmd    = "ssh -i ${var.ssh_private_key_path} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null ${var.vm_username}@${local.scoring_ip}"
}

# Addressing the engine's team-facing NICs is the one part of provisioning that depends on the
# team set, so it gets its own resource: adding a team has to re-run this (otherwise the new
# team's NIC never gets an address and every one of its services is unreachable), but must NOT
# re-run null_resource.orchestrate, whose Step A does `rm -rf /opt/quotient` and whose Step D
# re-runs nakon — that would flatten a live competition just to add a team to it.
#
# This was Step B of null_resource.orchestrate. The remaining steps keep their original letters
# (A, then C, then D) so the many references to them in README.md/OVERVIEW.md still line up —
# hence the gap where B used to be.
resource "null_resource" "team_nics" {
  triggers = {
    engine_id = proxmox_virtual_environment_vm.scoring_engine.id
    # Identifiers only — putting var.teams here would print team passwords in every plan.
    team_identifiers = join(",", [for k in local.sorted_team_keys : var.teams[k].identifier])
  }

  # Configure team NICs on scoring VM
  provisioner "remote-exec" {
    inline = [
      # Generate netplan config for each team NIC
      # NICs are named predictably: ens18=mgmt, ens19=team1, ens20=team2, ...
      "sudo tee /etc/netplan/60-team-ifaces.yaml << 'EOF'",
      "network:",
      "  version: 2",
      "  ethernets:",
      # One entry per team — identifiers sorted so NIC order is deterministic, and sorted the
      # same way the scoring engine's dynamic network_device blocks are (Terraform iterates
      # maps in lexicographic key order), so stanza N always describes NIC N.
      join("\n", [for idx, team in local.sorted_team_keys :
        "    ens${18 + idx + 1}:\n      addresses: [\"192.168.${var.teams[team].identifier}.1/24\"]"
      ]),
      "EOF",
      # netplan refuses to read (and warns loudly about) world-readable configs
      "sudo chmod 600 /etc/netplan/60-team-ifaces.yaml",
      "sudo netplan apply",
      # A drop-in that gets overwritten, not `tee -a /etc/sysctl.conf`, which appended another
      # copy of this line on every apply. NAT and the rest of the firewall are handled by
      # range-firewall.sh in Step C — they have to be re-applied after Docker starts.
      "echo 'net.ipv4.ip_forward=1' | sudo tee /etc/sysctl.d/99-range-forward.conf",
      "sudo sysctl -p /etc/sysctl.d/99-range-forward.conf",
    ]
    connection {
      type        = "ssh"
      user        = var.vm_username
      private_key = file(var.ssh_private_key_path)
      host        = local.scoring_ip
    }
  }

  depends_on = [proxmox_virtual_environment_vm.scoring_engine]
}

resource "null_resource" "orchestrate" {
  triggers = {
    engine_id = proxmox_virtual_environment_vm.scoring_engine.id
    box_ids   = join(",", [for vm in proxmox_virtual_environment_vm.team_box : vm.id])
  }

  # Step A: install what cloud-init would otherwise have provided (docker,
  # repos). No password-based bootstrap needed — the template's own
  # cloud-init build already baked in the SSH key (authorized_keys) and
  # passwordless sudo (/etc/sudoers.d/90-cloud-init-users) for var.vm_username.
  provisioner "remote-exec" {
    inline = [
      "set -e", # fail fast and loud instead of limping into a confusing error several commands later
      "echo 'scoring VM up'",
      # fresh boots race unattended-upgrades for the dpkg lock — wait it out rather than failing apt-get immediately
      "for i in $(seq 1 60); do sudo fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || break; sleep 2; done",
      "sudo apt-get update",
      "sudo apt-get install -y git python3-pip python3-venv ca-certificates curl",
      # docker-compose-plugin (the v2 'docker compose' subcommand used below) isn't in Ubuntu's
      # own repos — only Docker's official apt repo ships it, hence the convenience script
      "curl -fsSL https://get.docker.com | sudo sh",
      "sudo systemctl enable --now docker",
      # repo ships its own default .env (no .env.example) — Step C overwrites it with real values later
      "sudo rm -rf /opt/quotient && sudo git clone https://github.com/dbaseqp/Quotient /opt/quotient",
      # 'divisor' is a submodule — plain clone leaves it as an empty dir, breaking the compose build
      "cd /opt/quotient && sudo git submodule update --init --recursive",
      # linux.credlist is written by create-competition.py's push_event_conf() instead of
      # copied from upstream's .example — the example's accounts exist on no box, so every
      # login check scored a healthy service as down. windows.credlist is staged only because
      # upstream ships it; no check build_event_conf() emits references it.
      "sudo cp /opt/quotient/config/credlists/windows.credlist.example /opt/quotient/config/credlists/windows.credlist",
      "sudo rm -rf /opt/nakon && sudo git clone https://github.com/CyberDawgsTeam/nakon /opt/nakon",
      "sudo pip3 install --break-system-packages mysql-connector-python paramiko python-dotenv",
    ]
    connection {
      type        = "ssh"
      user        = var.vm_username
      private_key = file(var.ssh_private_key_path)
      host        = local.scoring_ip
    }
  }

  # Step C: drop Quotient .env and start it (paused)
  provisioner "remote-exec" {
    inline = [
      "sudo tee /opt/quotient/.env << 'EOF'",
      "POSTGRES_USER=engineuser",
      "POSTGRES_PASSWORD=changeme_in_prod",
      "POSTGRES_DB=engine",
      "POSTGRES_HOST=quotient_database",
      "REDIS_PASSWORD=changeme_in_prod",
      "REDIS_HOST=quotient_redis",
      "EOF",
      "cd /opt/quotient && sudo docker compose up -d --build",
      "sleep 15", # wait for DB to initialise

      # Docker sets the FORWARD policy to DROP when it starts, which silently blocks all
      # team-subnet → internet forwarding even though NAT and ip_forward are correct. These
      # rules used to be applied inline right here, which left them alive only until the next
      # reboot: a restarted scoring engine lost NAT and every service went down at once, with
      # nothing to point at. A script plus a unit ordered after docker.service survives that,
      # and being idempotent it is also safe to re-run on every apply.
      "sudo tee /usr/local/sbin/range-firewall.sh << 'EOF'",
      "#!/bin/bash",
      "# Applies the range's forwarding rules. Idempotent: safe to re-run at any time.",
      "# Installed by terraform/main.tf Step C; run at boot by range-firewall.service.",
      "set -eu",
      "",
      "TEAM_NET=192.168.0.0/16",
      "UPLINK=$(ip route show default | awk '{print $5; exit}')",
      "[ -n \"$UPLINK\" ] || { echo 'no default route — cannot identify the uplink NIC' >&2; exit 1; }",
      "",
      "# -C tests for an identical rule, so an existing one is never duplicated. Docker appends",
      "# a RETURN to DOCKER-USER, so rules must be inserted (-I) — appending lands after it and",
      "# would never be reached.",
      "ins() {",
      "  local table=$1; shift",
      "  iptables -t \"$table\" -C \"$@\" 2>/dev/null || iptables -t \"$table\" -I \"$@\"",
      "}",
      "",
      "sysctl -qw net.ipv4.ip_forward=1",
      "",
      "# Teams reach the internet through the engine.",
      "ins nat POSTROUTING -s \"$TEAM_NET\" ! -d \"$TEAM_NET\" -j MASQUERADE",
      "",
      "if iptables -t filter -L DOCKER-USER -n >/dev/null 2>&1; then",
      "  # Team traffic is accepted only when it is leaving via the uplink. This was previously",
      "  # '-s $TEAM_NET -j ACCEPT', which also accepted team1 → team2 (the engine holds a NIC on",
      "  # every team bridge and forwards between them, so empty bridges do not isolate anything)",
      "  # and team → Docker's container network, where Quotient's Postgres listens on a default",
      "  # password. Matching the outbound interface keeps the intended egress and drops both.",
      "  ins filter DOCKER-USER -s \"$TEAM_NET\" -o \"$UPLINK\" -j ACCEPT",
      "  ins filter DOCKER-USER -d \"$TEAM_NET\" -i \"$UPLINK\" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
      "else",
      "  echo 'DOCKER-USER chain absent (is Docker running?) — skipping its rules' >&2",
      "fi",
      "",
      "# Explicit, so team ↔ team does not depend on Docker's FORWARD policy being DROP —",
      "# that reverts to ACCEPT the moment Docker is stopped. Only matches traffic the engine",
      "# routes between two team subnets; a team reaching its own gateway is INPUT, not FORWARD.",
      "ins filter FORWARD -s \"$TEAM_NET\" -d \"$TEAM_NET\" -j DROP",
      "EOF",
      "sudo chmod 755 /usr/local/sbin/range-firewall.sh",

      "sudo tee /etc/systemd/system/range-firewall.service << 'EOF'",
      "[Unit]",
      "Description=Range forwarding rules (team subnets to internet, team isolation)",
      "# Docker rebuilds FORWARD and DOCKER-USER on start, so we must apply ours afterwards.",
      "After=docker.service network-online.target",
      "Wants=docker.service network-online.target",
      "",
      "[Service]",
      "Type=oneshot",
      "RemainAfterExit=yes",
      "ExecStart=/usr/local/sbin/range-firewall.sh",
      "",
      "[Install]",
      "WantedBy=multi-user.target",
      "EOF",
      "sudo systemctl daemon-reload",
      "sudo systemctl enable --now range-firewall.service",
    ]
    connection {
      type        = "ssh"
      user        = var.vm_username
      private_key = file(var.ssh_private_key_path)
      host        = local.scoring_ip
    }
  }

  # Step D: prepare team1's boxes, then push nakon's config and run it on them. Cloning to
  # other teams, the post-clone DNS repair, event.conf push, and Quotient seeding all happen
  # in create-competition.py after `terraform apply` returns.
  provisioner "local-exec" {
    command = <<-EOT
      set -e

      # Run nakon on the scoring engine, not locally — team subnets are isolated
      # bridges with no uplink, so only the scoring engine (which has a NIC on
      # each one, addressed by null_resource.team_nics) can actually reach the target machines.
      # /opt/nakon is root-owned from the Step A clone, so stage via /tmp first.
      # config.json must already exist — create-competition.py generates it
      # before calling `terraform apply`.
      test -f ../nakon/config.json || { echo "missing nakon/config.json — run create-competition.py first" >&2; exit 1; }
      scp -i ${var.ssh_private_key_path} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        ../nakon/config.json ../nakon/.env scripts/prepare_boxes.py ../utils.py \
        ${var.vm_username}@${local.scoring_ip}:/tmp/
      # Our two files land in their own directory rather than /opt/nakon, which is a checkout
      # of someone else's repo — dropping a utils.py in there would collide the day nakon adds
      # one of its own.
      ${local.ssh_cmd} "sudo cp /tmp/config.json /tmp/.env /opt/nakon/ && sudo mkdir -p /opt/range-prep && sudo cp /tmp/prepare_boxes.py /tmp/utils.py /opt/range-prep/"
      # prepare_boxes.py runs first and is allowed to fail the whole apply: it repairs the DNS
      # every `apt-get install` in deploy.py depends on, and nakon installs nothing (while
      # still exiting 0) on a box that can't resolve. See prepare_boxes.py's docstring.
      ${local.ssh_cmd} "sudo python3 /opt/range-prep/prepare_boxes.py /opt/nakon/config.json"
      ${local.ssh_cmd} "cd /opt/nakon && sudo python3 deploy.py"

      echo "=== Range infra is up — create-competition.py will push event.conf and start the competition next ==="
    EOT
  }

  depends_on = [
    proxmox_virtual_environment_vm.scoring_engine,
    proxmox_virtual_environment_vm.team_box,
    # Step D reaches team1's boxes over the engine's team-facing NICs, so they must be
    # addressed before nakon runs.
    null_resource.team_nics,
  ]
}