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

# Satellite providers (multi-node). Exactly MAX_SATELLITES=4 aliases; unused slots
# carry dummy settings and are never configured because no resource references them.
# Tokens arrive via tfvars.json (0600, written per competition by deploy()).
provider "proxmox" {
  alias     = "sat1"
  endpoint  = try(var.satellites[0].endpoint, "https://sat1.invalid")
  api_token = try(var.satellites[0].api_token, "root@pam!dummy=00000000-0000-0000-0000-000000000000")
  insecure  = true
  ssh {
    agent       = false
    username    = "root"
    private_key = file(var.ssh_private_key_path)
    node {
      name    = try(var.satellites[0].node, "unused")
      address = try(regex("^https?://([^/:]+)", var.satellites[0].endpoint)[0], "sat1.invalid")
    }
  }
}

provider "proxmox" {
  alias     = "sat2"
  endpoint  = try(var.satellites[1].endpoint, "https://sat2.invalid")
  api_token = try(var.satellites[1].api_token, "root@pam!dummy=00000000-0000-0000-0000-000000000000")
  insecure  = true
  ssh {
    agent       = false
    username    = "root"
    private_key = file(var.ssh_private_key_path)
    node {
      name    = try(var.satellites[1].node, "unused")
      address = try(regex("^https?://([^/:]+)", var.satellites[1].endpoint)[0], "sat2.invalid")
    }
  }
}

provider "proxmox" {
  alias     = "sat3"
  endpoint  = try(var.satellites[2].endpoint, "https://sat3.invalid")
  api_token = try(var.satellites[2].api_token, "root@pam!dummy=00000000-0000-0000-0000-000000000000")
  insecure  = true
  ssh {
    agent       = false
    username    = "root"
    private_key = file(var.ssh_private_key_path)
    node {
      name    = try(var.satellites[2].node, "unused")
      address = try(regex("^https?://([^/:]+)", var.satellites[2].endpoint)[0], "sat3.invalid")
    }
  }
}

provider "proxmox" {
  alias     = "sat4"
  endpoint  = try(var.satellites[3].endpoint, "https://sat4.invalid")
  api_token = try(var.satellites[3].api_token, "root@pam!dummy=00000000-0000-0000-0000-000000000000")
  insecure  = true
  ssh {
    agent       = false
    username    = "root"
    private_key = file(var.ssh_private_key_path)
    node {
      name    = try(var.satellites[3].node, "unused")
      address = try(regex("^https?://([^/:]+)", var.satellites[3].endpoint)[0], "sat4.invalid")
    }
  }
}

locals {
  # Teams split by hosting slot. t.slot defaults to 0 (variables.tf optional field),
  # so legacy tfvars without the key place every team on the engine node — the
  # single-node behavior, unchanged.
  slot_teams = [for s in range(5) : { for k, t in var.teams : k => t if try(t.slot, 0) == s }]

  # In-path firewall presence: one optional box (unmanaged + in_path) switches on the
  # transit bridges, the engine's transit NICs and the firewall's two-NIC wiring below.
  # Without it none of these resources exist and every plan is byte-identical to the
  # pre-firewall pipeline.
  has_in_path_fw = anytrue([for b in var.boxes_per_team : b.in_path])
}


resource "proxmox_network_linux_bridge" "team_bridge" {
  for_each  = local.slot_teams[0]
  node_name = var.proxmox_node
  name      = "vmbr${each.value.identifier}"
  comment   = "Quotient team ${each.key} — isolated, no uplink"
}

resource "proxmox_network_linux_bridge" "transit_bridge" {
  # Engine-side WAN bridge for the in-path firewall: engine (172.31.<id>.1/30) ─
  # pfSense WAN (172.31.<id>.2/30). Engine-node teams only — a satellite's jump VM
  # owns 192.168.<id>.1 there, so firewall+transit wiring is refused at preflight.
  for_each  = local.has_in_path_fw ? local.slot_teams[0] : {}
  node_name = var.proxmox_node
  name      = "vmbrW${each.value.identifier}"
  comment   = "Quotient team ${each.key} — transit to in-path firewall"
}

resource "proxmox_network_linux_bridge" "team_bridge_sat1" {
  provider  = proxmox.sat1
  for_each  = local.slot_teams[1]
  node_name = try(var.satellites[0].node, "unused")
  name      = "vmbr${each.value.identifier}"
  comment   = "Quotient team ${each.key} — isolated, no uplink (satellite 1)"
}

resource "proxmox_network_linux_bridge" "team_bridge_sat2" {
  provider  = proxmox.sat2
  for_each  = local.slot_teams[2]
  node_name = try(var.satellites[1].node, "unused")
  name      = "vmbr${each.value.identifier}"
  comment   = "Quotient team ${each.key} — isolated, no uplink (satellite 2)"
}

resource "proxmox_network_linux_bridge" "team_bridge_sat3" {
  provider  = proxmox.sat3
  for_each  = local.slot_teams[3]
  node_name = try(var.satellites[2].node, "unused")
  name      = "vmbr${each.value.identifier}"
  comment   = "Quotient team ${each.key} — isolated, no uplink (satellite 3)"
}

resource "proxmox_network_linux_bridge" "team_bridge_sat4" {
  provider  = proxmox.sat4
  for_each  = local.slot_teams[4]
  node_name = try(var.satellites[3].node, "unused")
  name      = "vmbr${each.value.identifier}"
  comment   = "Quotient team ${each.key} — isolated, no uplink (satellite 4)"
}

locals {
  # Ownership tags for the parallel cleanup sweep's defense-in-depth check (M1.4):
  # phase 1 refuses to destroy a tagged VM whose tags lack these. event_name is the
  # competition name (validated [a-z0-9._-] at creation); the replace() only matters
  # for hand-written tfvars.
  comp_tag = "comp-${replace(lower(var.event_name), " ", "-")}"
}

resource "proxmox_virtual_environment_vm" "scoring_engine" {
  node_name = var.proxmox_node
  name      = "quotient-engine"
  vm_id     = var.scoring_vm_id
  tags      = compact(["tezcatlipoca", local.comp_tag, var.run_tag])

  clone {
    # M4: the deployed engine is a linked clone of the competition's engine template
    # (fresh identity + host keys via the template's clean step, empty scoring DB per
    # run). 0 = pre-M4 fallback: full clone straight from the base image.
    vm_id = var.engine_clone_id != 0 ? var.engine_clone_id : var.template_vm_id
    full  = var.engine_clone_id == 0
  }

  agent {
    enabled = true
  }

  # Cloud-init mgmt setup, used only when engine_mgmt_ip is set (portable-node mode):
  # writes the sysadmin key and a static mgmt address, since agent-based IP discovery is
  # unavailable there. On the primary (empty var) nothing is emitted and the dedicated
  # scoring-engine image behaves exactly as before.
  dynamic "initialization" {
    for_each = var.engine_mgmt_ip != "" ? [1] : []
    content {
      user_account {
        username = var.vm_username
        keys     = [var.ssh_public_key]
      }
      ip_config {
        ipv4 {
          address = "${var.engine_mgmt_ip}/24"
          gateway = var.engine_mgmt_gw
        }
      }
      dns {
        servers = ["8.8.8.8"]
      }
    }
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
    # Only slot-0 teams get a NIC on the engine — satellite bridges live on other
    # hosts; the engine reaches their subnets via the jump routes (satellite_routes).
    for_each = local.slot_teams[0]
    content {
      bridge = "vmbr${network_device.value.identifier}"
      model  = "virtio"
    }
  }

  dynamic "network_device" {
    # Transit NICs for in-path firewalls, appended AFTER every team NIC so the team
    # NICs keep their positional netplan names (ens19+); team_nics addresses the
    # transit NICs at 172.31.<id>.1/30 by continuing the same positional scheme.
    for_each = local.has_in_path_fw ? local.slot_teams[0] : {}
    content {
      bridge = "vmbrW${network_device.value.identifier}"
      model  = "virtio"
    }
  }


  depends_on = [
    proxmox_network_linux_bridge.team_bridge,
    proxmox_network_linux_bridge.transit_bridge,
  ]
}

data "proxmox_virtual_environment_vms" "templates" {
  node_name = var.proxmox_node
  tags      = ["template"]
}

locals {
  template_ids = {
    for vm in data.proxmox_virtual_environment_vms.templates.vms :
    vm.name => vm.vm_id
    # Templates are stopped by definition (qm template requires it). A running box with
    # a stray `template` tag (live-confirmed 2026-09-24: three running competition boxes
    # carried it, duping names in this map and failing every new apply) must not resolve
    # as a clone source.
    if vm.status == "stopped"
  }

  sorted_team_keys = sort(keys(local.slot_teams[0]))

  # Satellite routes for the engine: runtime `ip route replace` (apply #1 lands them
  # immediately, after the cold boot) plus a oneshot systemd unit so an operator
  # engine reboot mid-event re-asserts them instead of silently stranding satellite
  # scoring. Empty in single-node deploys — no extra guest state.
  satellite_route_cmds = length(var.satellite_routes) == 0 ? [] : concat(
    [for r in var.satellite_routes : ["sudo ip route replace ${r.subnet} via ${r.via} || true"]],
    [[
      "printf '%s\\n' '#!/bin/sh' ${join(" ", [for r in var.satellite_routes : "'ip route replace ${r.subnet} via ${r.via} || true'"])} | sudo tee /usr/local/sbin/satellite-routes.sh > /dev/null",
      "sudo chmod +x /usr/local/sbin/satellite-routes.sh",
      "printf '%s\\n' '[Unit]' 'Description=Tezcatlipoca satellite team routes' 'After=network-online.target' '' '[Service]' 'Type=oneshot' 'ExecStart=/usr/local/sbin/satellite-routes.sh' 'RemainAfterExit=yes' '' '[Install]' 'WantedBy=multi-user.target' | sudo tee /etc/systemd/system/satellite-routes.service > /dev/null",
      "sudo systemctl daemon-reload",
      "sudo systemctl enable --now satellite-routes.service",
    ]]
  )
  team1_key = "team1"

  # M3.3: every team is Terraform-managed now. Keys keep the historical naming
  # (team1-<box>, <identifier>-<box>) so enumerate_targets' vm_name matches.
  # Split by hosting slot: slot 0's map keeps the legacy `all_team_vms` name/address;
  # satellite slots clone from their own node's golden copies (golden_ids_for_slot).
  all_team_vms_by_slot = [for s in range(5) : merge([
    for team_key, team in local.slot_teams[s] : {
      for box in var.boxes_per_team :
      (team_key == local.team1_key ? "team1-${box.name}" : "${team.identifier}-${box.name}") => ({
        key        = (team_key == local.team1_key ? "team1-${box.name}" : "${team.identifier}-${box.name}")
        team_key   = team_key
        identifier = tostring(team.identifier)
        box        = box
        ip         = "192.168.${team.identifier}.${box.last_octet}"
        gw         = "192.168.${team.identifier}.1"
        bridge     = "vmbr${team.identifier}"
      })
    }
  ]...)]
  all_team_vms = local.all_team_vms_by_slot[0]

}

resource "proxmox_virtual_environment_vm" "team_box" {
  for_each  = var.build_team_boxes ? local.all_team_vms : {}
  node_name = var.proxmox_node
  name      = each.key
  vm_id = 200 + (tonumber(each.value.identifier) * 10) + index(
    [for b in var.boxes_per_team : b.name], each.value.box.name
  )
  tags = compact(["tezcatlipoca", local.comp_tag, var.run_tag])

  # The golden templates carry agent=1, so bpg would otherwise wait its default 15 min
  # per box for an agent IP — serially under -parallelism=1. An unbooted DC golden's
  # clone is in sysprep specialize for ~10 min (winad-testrun 2026-09-25: two DCs = 20+
  # min of pure waiting). A timeout here is only a warning; deploy's own readiness waits
  # (SSH / the Windows setup-complete gate) are what gate the next steps.
  agent {
    enabled = true
    timeout = "30s"
  }

  # Linked clone of this box type's golden template (planted by golden_ops in phase 4,
  # strictly green before conversion). No retries: templates aren't locked the way a
  # clone-source VM is, and linked clones are seconds of metadata work, not bulk writes.
  clone {
    vm_id = var.golden_template_ids[index(
      [for b in var.boxes_per_team : b.name], each.value.box.name
    )]
    full = false
  }

  lifecycle {
    precondition {
      condition     = !var.build_team_boxes || length(var.golden_template_ids) == length(var.boxes_per_team)
      error_message = "build_team_boxes is true but golden_template_ids doesn't have one entry per box — the golden build (deploy phase 4) must run before apply #2."
    }
    precondition {
      condition     = length([for b in var.boxes_per_team : b if b.in_path]) <= 1
      error_message = "At most one in_path firewall per lineup — the transit design routes every team through a single gateway (engine ─ vmbrW<id> ─ fw ─ vmbr<id>)."
    }
    precondition {
      condition = (200 + (tonumber(each.value.identifier) * 10) + index(
        [for b in var.boxes_per_team : b.name], each.value.box.name
      )) != proxmox_virtual_environment_vm.scoring_engine.vm_id
      error_message = "Computed team_box vm_id collides with the scoring engine's vm_id (var.scoring_vm_id). Adjust team identifiers, box count, or --scoring-vmid."
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

  dynamic "network_device" {
    # In-path firewall: net0 = WAN on the transit bridge (172.31.<id>.2/30 per the
    # generated pfSense config), net1 = LAN on the team bridge (the boxes' gateway .1).
    # Order is load-bearing — pfSense's config keys the interfaces on vtnet0/vtnet1,
    # and net1 must EXIST for the bootstrap's ifconfig/fetch and for the boxes' gateway
    # path (live-found 2026-10-04: emitting only the WAN device left the firewall
    # unrouted and the phase-5 fetch never fired).
    for_each = each.value.box.in_path ? ["vmbrW${each.value.identifier}", "vmbr${each.value.identifier}"] : []
    content {
      bridge = network_device.value
      model  = "virtio"
    }
  }

  dynamic "network_device" {
    for_each = each.value.box.in_path ? [] : [each.value.bridge]
    content {
      bridge = network_device.value
      model  = "virtio"
    }
  }

  dynamic "initialization" {
    # No cloud-init identity on Windows (bootstrap_windows_box owns it) or on any
    # unmanaged box (pfSense ignores cloud-init; the appliance self-configures —
    # firewall_ops bootstraps the in-path config over its console).
    for_each = (!each.value.box.unmanaged && !strcontains(lower(each.value.box.template), "win")) ? [1] : []
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

  depends_on = [
    proxmox_network_linux_bridge.team_bridge,
    proxmox_network_linux_bridge.transit_bridge,
  ]
}

# Satellite team boxes: same shape, hosted on the satellite's node and linked from
# that node's own golden copies (jump routes carry the engine's plant/scoring there).
# Addresses team_box_sat1..4 exist only in multi-node deploys.
resource "proxmox_virtual_environment_vm" "team_box_sat1" {
  provider  = proxmox.sat1
  for_each  = var.build_team_boxes ? local.all_team_vms_by_slot[1] : {}
  node_name = try(var.satellites[0].node, "unused")
  name      = each.key
  vm_id = 200 + (tonumber(each.value.identifier) * 10) + index(
    [for b in var.boxes_per_team : b.name], each.value.box.name
  )
  tags = compact(["tezcatlipoca", local.comp_tag, var.run_tag])

  agent {
    enabled = true
    timeout = "30s"
  }

  clone {
    vm_id = try(
      var.golden_template_ids_by_slot[tostring(1)][index(
        [for b in var.boxes_per_team : b.name], each.value.box.name
      )],
      var.golden_template_ids[index(
        [for b in var.boxes_per_team : b.name], each.value.box.name
      )]
    )
    full = false
  }

  cpu { cores = each.value.box.cpu }
  memory { dedicated = each.value.box.memory_mb }

  dynamic "disk" {
    for_each = each.value.box.disk_gb != null ? [each.value.box.disk_gb] : []
    content {
      datastore_id = try(var.satellites[0].datastore, "unused")
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
    for_each = (!each.value.box.unmanaged && !strcontains(lower(each.value.box.template), "win")) ? [1] : []
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

  depends_on = [proxmox_network_linux_bridge.team_bridge_sat1]
}

resource "proxmox_virtual_environment_vm" "team_box_sat2" {
  provider  = proxmox.sat2
  for_each  = var.build_team_boxes ? local.all_team_vms_by_slot[2] : {}
  node_name = try(var.satellites[1].node, "unused")
  name      = each.key
  vm_id = 200 + (tonumber(each.value.identifier) * 10) + index(
    [for b in var.boxes_per_team : b.name], each.value.box.name
  )
  tags = compact(["tezcatlipoca", local.comp_tag, var.run_tag])

  agent {
    enabled = true
    timeout = "30s"
  }

  clone {
    vm_id = try(
      var.golden_template_ids_by_slot[tostring(2)][index(
        [for b in var.boxes_per_team : b.name], each.value.box.name
      )],
      var.golden_template_ids[index(
        [for b in var.boxes_per_team : b.name], each.value.box.name
      )]
    )
    full = false
  }

  cpu { cores = each.value.box.cpu }
  memory { dedicated = each.value.box.memory_mb }

  dynamic "disk" {
    for_each = each.value.box.disk_gb != null ? [each.value.box.disk_gb] : []
    content {
      datastore_id = try(var.satellites[1].datastore, "unused")
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
    for_each = (!each.value.box.unmanaged && !strcontains(lower(each.value.box.template), "win")) ? [1] : []
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

  depends_on = [proxmox_network_linux_bridge.team_bridge_sat2]
}

resource "proxmox_virtual_environment_vm" "team_box_sat3" {
  provider  = proxmox.sat3
  for_each  = var.build_team_boxes ? local.all_team_vms_by_slot[3] : {}
  node_name = try(var.satellites[2].node, "unused")
  name      = each.key
  vm_id = 200 + (tonumber(each.value.identifier) * 10) + index(
    [for b in var.boxes_per_team : b.name], each.value.box.name
  )
  tags = compact(["tezcatlipoca", local.comp_tag, var.run_tag])

  agent {
    enabled = true
    timeout = "30s"
  }

  clone {
    vm_id = try(
      var.golden_template_ids_by_slot[tostring(3)][index(
        [for b in var.boxes_per_team : b.name], each.value.box.name
      )],
      var.golden_template_ids[index(
        [for b in var.boxes_per_team : b.name], each.value.box.name
      )]
    )
    full = false
  }

  cpu { cores = each.value.box.cpu }
  memory { dedicated = each.value.box.memory_mb }

  dynamic "disk" {
    for_each = each.value.box.disk_gb != null ? [each.value.box.disk_gb] : []
    content {
      datastore_id = try(var.satellites[2].datastore, "unused")
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
    for_each = (!each.value.box.unmanaged && !strcontains(lower(each.value.box.template), "win")) ? [1] : []
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

  depends_on = [proxmox_network_linux_bridge.team_bridge_sat3]
}

resource "proxmox_virtual_environment_vm" "team_box_sat4" {
  provider  = proxmox.sat4
  for_each  = var.build_team_boxes ? local.all_team_vms_by_slot[4] : {}
  node_name = try(var.satellites[3].node, "unused")
  name      = each.key
  vm_id = 200 + (tonumber(each.value.identifier) * 10) + index(
    [for b in var.boxes_per_team : b.name], each.value.box.name
  )
  tags = compact(["tezcatlipoca", local.comp_tag, var.run_tag])

  agent {
    enabled = true
    timeout = "30s"
  }

  clone {
    vm_id = try(
      var.golden_template_ids_by_slot[tostring(4)][index(
        [for b in var.boxes_per_team : b.name], each.value.box.name
      )],
      var.golden_template_ids[index(
        [for b in var.boxes_per_team : b.name], each.value.box.name
      )]
    )
    full = false
  }

  cpu { cores = each.value.box.cpu }
  memory { dedicated = each.value.box.memory_mb }

  dynamic "disk" {
    for_each = each.value.box.disk_gb != null ? [each.value.box.disk_gb] : []
    content {
      datastore_id = try(var.satellites[3].datastore, "unused")
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
    for_each = (!each.value.box.unmanaged && !strcontains(lower(each.value.box.template), "win")) ? [1] : []
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

  depends_on = [proxmox_network_linux_bridge.team_bridge_sat4]
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
  # Portable-node override first (agent channel broken on the realm → discovery is
  # impossible); the guard keeps evaluate-time totals so a destroy against a dead/agentless
  # engine can't die on an empty tuple.
  scoring_ip = var.engine_mgmt_ip != "" ? var.engine_mgmt_ip : (
    length(local.scoring_mgmt_ips) > 0 ? local.scoring_mgmt_ips[0] : "127.0.0.1"
  )
  ssh_cmd = "ssh -i ${var.ssh_private_key_path} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null ${var.vm_username}@${local.scoring_ip}"
}

resource "null_resource" "team_nics" {
  triggers = {
    engine_id        = proxmox_virtual_environment_vm.scoring_engine.id
    team_identifiers = join(",", [for k in local.sorted_team_keys : var.teams[k].identifier])
    satellite_routes = join(",", [for r in var.satellite_routes : "${r.subnet}@${r.via}"])
    in_path_fw       = local.has_in_path_fw ? "1" : "0"
  }

  provisioner "remote-exec" {
    # Netplan block only when the engine has local (slot-0) teams — an all-satellite
    # spread has none, and an empty string in a remote-exec script list is fatal.
    # With an in-path firewall, the transit NICs are addressed 172.31.<id>.1/30 AFTER
    # the team NICs (the engine keeps 192.168.<id>.1 on the team bridges for now —
    # deploy phase 5's cutover moves the gateway address to the firewall and adds the
    # 192.168.<id>.0/24 via 172.31.<id>.2 routes once every firewall is configured;
    # routes here would blackhole engine→box traffic before that).
    inline = concat(
      length(local.sorted_team_keys) == 0 ? [] : [
        "sudo tee /etc/netplan/60-team-ifaces.yaml << 'EOF'",
        "network:",
        "  version: 2",
        "  ethernets:",
        join("\n", concat(
          [for idx, team in local.sorted_team_keys :
          "    ens${18 + idx + 1}:\n      addresses: [\"192.168.${var.teams[team].identifier}.1/24\"]"],
          local.has_in_path_fw ? [for idx, team in local.sorted_team_keys :
          "    ens${18 + length(local.sorted_team_keys) + idx + 1}:\n      addresses: [\"172.31.${var.teams[team].identifier}.1/30\"]"] : [],
        )),
        "EOF",
        "sudo chmod 600 /etc/netplan/60-team-ifaces.yaml",
        "sudo netplan apply",
      ],
      [
        "printf 'net.ipv4.ip_forward=1\\nnet.ipv4.conf.all.rp_filter=2\\nnet.ipv4.conf.default.rp_filter=2\\n' | sudo tee /etc/sysctl.d/99-range-forward.conf",
        "sudo sysctl -p /etc/sysctl.d/99-range-forward.conf",
      ],
    flatten(local.satellite_route_cmds))
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
