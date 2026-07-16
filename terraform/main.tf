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

# Reboot scoring engine so the guest kernel detects the additional virtio NICs
# that Terraform added at the hypervisor level. Without this, only the first
# NIC (eth0) is visible and the team networks (ens19/ens20) don't exist.
#
# CRITICAL: Must use hard shutdown + start via Proxmox API — guest-level
# "sudo reboot" does NOT trigger a PCI bus scan for newly added VirtIO NICs,
# so ens19/ens20 never appear. A cold-boot from the hypervisor is required.
resource "null_resource" "reboot_scoring_engine" {
  triggers = {
    engine_id = proxmox_virtual_environment_vm.scoring_engine.id
  }

  provisioner "local-exec" {
    command = <<-EOT
      echo "Hard-stopping scoring engine VM ${proxmox_virtual_environment_vm.scoring_engine.id} via Proxmox API..."
      curl -sk -X POST \
        -H "Authorization: PVEAPIToken $${TF_VAR_proxmox_api_token}" \
        "${var.proxmox_endpoint}api2/json/nodes/${var.proxmox_node}/qemu/${proxmox_virtual_environment_vm.scoring_engine.id}/status/stop" \
        -d "shutdown=0"
      echo ""
      echo "Waiting for VM to fully stop..."
      sleep 15
      echo "Starting scoring engine VM ${proxmox_virtual_environment_vm.scoring_engine.id} via Proxmox API..."
      curl -sk -X POST \
        -H "Authorization: PVEAPIToken $${TF_VAR_proxmox_api_token}" \
        "${var.proxmox_endpoint}api2/json/nodes/${var.proxmox_node}/qemu/${proxmox_virtual_environment_vm.scoring_engine.id}/status/start"
      echo ""
      echo "Cold-boot complete — PCI bus scan will detect new VirtIO NICs."
    EOT
  }

  depends_on = [
    proxmox_virtual_environment_vm.scoring_engine,
    proxmox_virtual_environment_vm.team_box,
  ]
}

resource "null_resource" "orchestrate" {
  triggers = {
    engine_id = proxmox_virtual_environment_vm.scoring_engine.id
    box_ids   = join(",", [for vm in proxmox_virtual_environment_vm.team_box : vm.id])
  }

  # Wait for the scoring engine to come back up after reboot.
  # Loop runs SSH with actual command execution (not just TCP connect) to
  # confirm auth is working, then sleeps 30s to let the system stabilize
  # before the remote-exec provisioner fires.
  provisioner "local-exec" {
    command = "for i in $(seq 1 30); do if ssh -i ${var.ssh_private_key_path} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o BatchMode=yes ${var.vm_username}@${local.scoring_ip} echo ok 2>/dev/null | grep -q ok; then echo 'Scoring engine online'; break; fi; echo \"Waiting for scoring engine ($i/30)\"; sleep 10; done; sleep 30"
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
      timeout     = "5m"
    }
  }
  depends_on = [
    proxmox_virtual_environment_vm.scoring_engine,
    proxmox_virtual_environment_vm.team_box,
    # Step D reaches team1's boxes over the engine's team-facing NICs, so they must be
    # addressed before nakon runs.
    null_resource.team_nics,
  ]
}
