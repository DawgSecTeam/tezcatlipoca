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

  # Whichever box is named "dns*" is the resolver for its team; if a team
  # has none, fall back to the gateway (the scoring engine's team-facing NIC).
  dns_box_last_octet = [for b in var.boxes_per_team : b.last_octet if can(regex("^dns", b.name))]
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
    dns {
      servers = [
        length(local.dns_box_last_octet) > 0
        ? "192.168.${each.value.identifier}.${local.dns_box_last_octet[0]}"
        : "8.8.8.8"
      ]
    }
  }

  depends_on = [proxmox_network_linux_bridge.team_bridge]
}

locals {
  # ip_config just echoes back "dhcp" (the configured value); the actual
  # leased address has to come from the QEMU guest agent instead.
  scoring_ip = [
    for ip in flatten(proxmox_virtual_environment_vm.scoring_engine.ipv4_addresses) :
    ip if ip != "127.0.0.1"
  ][0]
  ssh_cmd = "ssh -i ${var.ssh_private_key_path} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null ${var.vm_username}@${local.scoring_ip}"
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
      # repo ships only .example credlists — event.conf references real .credlist filenames
      "sudo cp /opt/quotient/config/credlists/linux.credlist.example /opt/quotient/config/credlists/linux.credlist",
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

  # Step B: configure team NICs on scoring VM
  provisioner "remote-exec" {
    inline = [
      # Generate netplan config for each team NIC
      # NICs are named predictably: ens18=mgmt, ens19=team1, ens20=team2, ...
      "sudo tee /etc/netplan/60-team-ifaces.yaml << 'EOF'",
      "network:",
      "  version: 2",
      "  ethernets:",
      # One entry per team — identifiers sorted so NIC order is deterministic
      join("\n", [for idx, team in sort(keys(var.teams)) :
        "    ens${18 + idx + 1}:\n      addresses: [\"192.168.${var.teams[team].identifier}.1/24\"]"
      ]),
      "EOF",
      "sudo netplan apply",
      "sudo sysctl -w net.ipv4.ip_forward=1",
      "echo 'net.ipv4.ip_forward=1' | sudo tee -a /etc/sysctl.conf",
      "sudo iptables -t nat -A POSTROUTING -s 192.168.0.0/16 ! -d 192.168.0.0/16 -j MASQUERADE",
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
      # Docker sets FORWARD policy to DROP when it starts, which silently blocks all
      # team-subnet → internet forwarding (even though NAT and ip_forward are correct).
      # DOCKER-USER is processed first in FORWARD and Docker never flushes it on restart,
      # so this is the correct place to permanently allow team traffic through.
      "sudo iptables -I DOCKER-USER -s 192.168.0.0/16 -j ACCEPT",
      "sudo iptables -I DOCKER-USER -d 192.168.0.0/16 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
    ]
    connection {
      type        = "ssh"
      user        = var.vm_username
      private_key = file(var.ssh_private_key_path)
      host        = local.scoring_ip
    }
  }

  # Step D: push nakon's config and run it on team1 boxes only. DNS fix,
  # cloning to other teams, event.conf push, and Quotient seeding all happen
  # in create-competition.py after `terraform apply` returns.
  provisioner "local-exec" {
    command = <<-EOT
      set -e

      # Run nakon on the scoring engine, not locally — team subnets are isolated
      # bridges with no uplink, so only the scoring engine (which has a NIC on
      # each one, set up in Step B) can actually reach the target machines.
      # /opt/nakon is root-owned from the Step A clone, so stage via /tmp first.
      # config.json must already exist — create-competition.py generates it
      # before calling `terraform apply`.
      test -f ../nakon/config.json || { echo "missing nakon/config.json — run create-competition.py first" >&2; exit 1; }
      scp -i ${var.ssh_private_key_path} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        ../nakon/config.json ../nakon/.env ${var.vm_username}@${local.scoring_ip}:/tmp/
      ${local.ssh_cmd} "sudo cp /tmp/config.json /opt/nakon/config.json && sudo cp /tmp/.env /opt/nakon/.env"
      ${local.ssh_cmd} "cd /opt/nakon && sudo python3 -c \"
import json, paramiko
from dotenv import load_dotenv
load_dotenv()
with open('config.json') as f:
    cfg = json.load(f)
for m in cfg['machines']:
    try:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(m['ip'], username=m['user'], password=m['password'])
        c.exec_command('find /tmp -maxdepth 1 -type f -delete')
        c.close()
        print('[pre-clean] cleared /tmp on ' + m['ip'])
    except Exception as e:
        print('[pre-clean] ' + m['ip'] + ': ' + str(e))
\" && sudo python3 deploy.py"

      echo "=== Range infra is up — create-competition.py will push event.conf and start the competition next ==="
    EOT
  }

  depends_on = [
    proxmox_virtual_environment_vm.scoring_engine,
    proxmox_virtual_environment_vm.team_box,
  ]
}