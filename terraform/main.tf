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
  # is needed before create-competition.py connects.

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

  # Step B: configure team NICs and NAT on scoring VM
  # Steps A (package install), C (Quotient Docker), D (Nakon deploy) moved to
  # create-competition.py to keep terraform apply under the tool timeout limit.
  provisioner "remote-exec" {
    inline = [
      "set -e",
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
      # Disable reverse path filtering so NAT/masqueraded return traffic reaches team subnets
      "sudo sysctl -w net.ipv4.conf.all.rp_filter=2",
      "sudo sysctl -w net.ipv4.conf.default.rp_filter=2",
      "echo 'net.ipv4.conf.all.rp_filter=2' | sudo tee -a /etc/sysctl.conf",
      "echo 'net.ipv4.conf.default.rp_filter=2' | sudo tee -a /etc/sysctl.conf",
      "sudo iptables -t nat -A POSTROUTING -s 192.168.0.0/16 ! -d 192.168.0.0/16 -j MASQUERADE",
    ]
    connection {
      type        = "ssh"
      user        = var.vm_username
      private_key = file(var.ssh_private_key_path)
      host        = local.scoring_ip
    }
  }

  depends_on = [
    proxmox_virtual_environment_vm.scoring_engine,
    proxmox_virtual_environment_vm.team_box,
  ]
}
