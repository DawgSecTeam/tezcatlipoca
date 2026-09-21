terraform {
  required_providers {
    proxmox = { source = "bpg/proxmox", version = "~> 0.101" }
  }
  backend "local" {}
}

locals {
  proxmox_host = regex("^https?://([^/:]+)", var.proxmox_endpoint)[0]
}

provider "proxmox" {
  endpoint  = var.proxmox_endpoint
  api_token = var.proxmox_api_token
  insecure  = true
  ssh {
    agent       = false
    username    = "root"
    private_key = file(var.ssh_private_key_path)
    node {
      name    = var.proxmox_node
      address = local.proxmox_host
    }
  }
}


resource "proxmox_network_linux_bridge" "team_bridge" {
  for_each  = var.teams
  node_name = var.proxmox_node
  name      = "vmbr${each.value.identifier}"
  comment   = "Quotient team ${each.key} — isolated, no uplink"
}

resource "proxmox_virtual_environment_vm" "scoring_engine" {
  node_name = var.proxmox_node
  name      = "quotient-engine"
  vm_id     = 1000

  clone {
    vm_id = var.template_vm_id
    full  = true
  }

  agent {
    enabled = true
  }

  cpu { cores = 4 }
  memory { dedicated = 4096 }

  disk {
    datastore_id = var.datastore
    interface    = "scsi0"
    size         = 40
    discard      = "on"
  }

  network_device {
    bridge = "vmbr0"
    model  = "virtio"
  }

  dynamic "network_device" {
    for_each = var.teams
    content {
      bridge = "vmbr${network_device.value.identifier}"
      model  = "virtio"
    }
  }


  depends_on = [proxmox_network_linux_bridge.team_bridge]
}

data "proxmox_virtual_environment_vms" "templates" {
  node_name = var.proxmox_node
  tags      = ["template"]
}

locals {
  template_ids = {
    for vm in data.proxmox_virtual_environment_vms.templates.vms :
    vm.name => vm.vm_id
  }

  sorted_team_keys = sort(keys(var.teams))
  team1_key        = "team1"

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
  vm_id = 200 + (tonumber(each.value.identifier) * 10) + index(
    [for b in var.boxes_per_team : b.name], each.value.box.name
  )

  clone {
    vm_id   = local.template_ids[each.value.box.template]
    full    = true
    retries = 15
  }

  lifecycle {
    precondition {
      condition = (200 + (tonumber(each.value.identifier) * 10) + index(
        [for b in var.boxes_per_team : b.name], each.value.box.name
      )) != proxmox_virtual_environment_vm.scoring_engine.vm_id
      error_message = "Computed team_box vm_id collides with the scoring engine's fixed vm_id (1000). Adjust team identifiers or box count."
    }
  }

  cpu { cores = each.value.box.cpu }
  memory { dedicated = each.value.box.memory_mb }

  dynamic "disk" {
    for_each = each.value.box.disk_gb != null ? [each.value.box.disk_gb] : []
    content {
      datastore_id = var.datastore
      interface    = coalesce(each.value.box.disk_iface, "scsi0")
      size         = disk.value
      discard      = "on"
    }
  }

  network_device {
    bridge = each.value.bridge
    model  = "virtio"
  }

  dynamic "initialization" {
    for_each = strcontains(lower(each.value.box.template), "win") ? [] : [1]
    content {
      ip_config {
        ipv4 {
          address = "${each.value.ip}/24"
          gateway = each.value.gw
        }
      }
      user_account {
        username = var.box_username
        keys     = [var.ssh_public_key]
        password = var.box_password
      }
      dns {
        servers = ["8.8.8.8"]
      }
    }
  }

  depends_on = [proxmox_network_linux_bridge.team_bridge]
}

locals {
  scoring_mgmt_ips = [
    for ip in flatten(proxmox_virtual_environment_vm.scoring_engine.ipv4_addresses) :
    ip if(
      !startswith(ip, "127.") &&
      !startswith(ip, "192.168.") &&
      !(startswith(ip, "172.") && tonumber(split(".", ip)[1]) >= 16 && tonumber(split(".", ip)[1]) <= 31) &&
      !(startswith(ip, "100.") && tonumber(split(".", ip)[1]) >= 64 && tonumber(split(".", ip)[1]) <= 127)
    )
  ]
  scoring_ip = local.scoring_mgmt_ips[0]
  ssh_cmd    = "ssh -i ${var.ssh_private_key_path} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null ${var.vm_username}@${local.scoring_ip}"
}

resource "null_resource" "team_nics" {
  triggers = {
    engine_id        = proxmox_virtual_environment_vm.scoring_engine.id
    team_identifiers = join(",", [for k in local.sorted_team_keys : var.teams[k].identifier])
  }

  provisioner "remote-exec" {
    inline = [
      "sudo tee /etc/netplan/60-team-ifaces.yaml << 'EOF'",
      "network:",
      "  version: 2",
      "  ethernets:",
      join("\n", [for idx, team in local.sorted_team_keys :
        "    ens${18 + idx + 1}:\n      addresses: [\"192.168.${var.teams[team].identifier}.1/24\"]"
      ]),
      "EOF",
      "sudo chmod 600 /etc/netplan/60-team-ifaces.yaml",
      "sudo netplan apply",
      "printf 'net.ipv4.ip_forward=1\\nnet.ipv4.conf.all.rp_filter=2\\nnet.ipv4.conf.default.rp_filter=2\\n' | sudo tee /etc/sysctl.d/99-range-forward.conf",
      "sudo sysctl -p /etc/sysctl.d/99-range-forward.conf",
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
    null_resource.reboot_scoring_engine,
  ]
}

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

  provisioner "local-exec" {
    command = "for i in $(seq 1 30); do if ssh -i ${var.ssh_private_key_path} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o BatchMode=yes ${var.vm_username}@${local.scoring_ip} echo ok 2>/dev/null | grep -q ok; then echo 'Scoring engine online'; break; fi; echo \"Waiting for scoring engine ($i/30)\"; sleep 10; done; sleep 30"
  }

  depends_on = [
    proxmox_virtual_environment_vm.scoring_engine,
    proxmox_virtual_environment_vm.team_box,
    null_resource.reboot_scoring_engine,
    null_resource.team_nics,
  ]
}
