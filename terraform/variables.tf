variable "proxmox_endpoint" { type = string }

variable "proxmox_api_token" {
  type      = string
  sensitive = true
}

variable "proxmox_node" {
  type    = string
  default = "pve"
}

variable "engine_mgmt_ip" {
  description = "Optional static mgmt IPv4 for the scoring engine. Set it on nodes where the guest-agent channel can't report addresses (the realm's PVE answers agent pings but returns null for every data call, so terraform's ipv4_addresses discovery is impossible there). Empty = template's own DHCP + agent discovery (primary-node behavior)."
  type        = string
  default     = ""
}

variable "engine_mgmt_gw" {
  description = "Gateway for engine_mgmt_ip (only meaningful when it is set)."
  type        = string
  default     = ""
}

variable "ssh_public_key" { type = string }

variable "ssh_private_key_path" { type = string }

variable "vm_username" {
  description = "Built-in OS account already present on every template (not provisioned by us)"
  type        = string
  default     = "sysadmin"
}

variable "box_username" {
  description = "Cloud-init account username on every team box clone (themeable via competitions/<id>/users.json; defaults to ubuntu)."
  type        = string
  default     = "ubuntu"
}

variable "box_password" {
  description = "Password for var.box_username's cloud-init account; generated fresh per competition in deploy(), never a fixed literal (nakon authenticates by password)."
  type        = string
  sensitive   = true
}

variable "competition" {
  description = "The competitions/<dir> name — the ownership tag's source (falls back to event_name)."
  type        = string
  default     = ""
}

variable "event_name" {
  type    = string
  default = "Range 2026"
}

variable "run_tag" {
  description = "Per-deploy run identity (run-<hex>, from .deploy_state.json) stamped onto the engine and every team box alongside the comp tag. Destruction guards require the FULL set, so two worktrees deploying the same competition ID can no longer destroy each other's VMs (2026-10-02 near-miss). Set by deploy.py on every apply."
  type        = string
  default     = ""
}

variable "build_team_boxes" {
  description = "M3.3 two-apply gate. Apply #1 (deploy phase 2) leaves this false: only the engine and bridges are built. Apply #2 (deploy phase 4) flips it true once golden_ops has planted and converted the golden set — team boxes then come up as LINKED clones of those templates, and every team enters Terraform state."
  type        = bool
  default     = false
}

variable "golden_template_ids" {
  description = "Positional golden template vmid per box (same order as boxes_per_team), written by deploy() after the golden build. Keyed by POSITION, not template name: two box types may share one base template. Required (and only evaluated) when build_team_boxes is true."
  type        = list(number)
  default     = []
}

variable "teams" {
  description = "Map of team key → identifier (used as subnet third octet). slot picks the hosting node: 0 = engine node (default), 1..4 = satellite index into var.satellites."
  type = map(object({
    identifier = string
    password   = string
    slot       = optional(number, 0)
  }))
  default = {
    team1 = { identifier = "101", password = "team1pass" }
    team2 = { identifier = "102", password = "team2pass" }
  }
}

variable "satellites" {
  description = "Exactly 4 entries (deploy pads unused slots with dummies; providers of resource-less slots are never configured). Index i-1 backs provider alias sat{i} and every slot-i resource group."
  type = list(object({
    endpoint  = string
    api_token = string
    node      = string
    datastore = string
  }))
  default = []
}

variable "golden_template_ids_by_slot" {
  description = "Per-slot positional golden template vmids (same order as boxes_per_team). Slots 1..4 are the satellites; slot 0 (the engine node) uses var.golden_template_ids."
  type        = map(list(number))
  default     = {}
}

variable "satellite_routes" {
  description = "Engine static routes, one per satellite: its anchor team's subnet via the jump's mgmt IP. Written + persisted on the engine by the team_nics provisioner; empty in single-node deploys."
  type = list(object({
    subnet = string
    via    = string
  }))
  default = []
}

variable "boxes_per_team" {
  description = "Boxes cloned identically for every team. 'template' must match a Proxmox VM tagged 'template' exactly (see docs/usage-people.md 'Adding a template VM'). unmanaged = no cloud-init/no nakon (appliance cloned from its own template); in_path = an unmanaged in-path firewall (two NICs: WAN on vmbrW<id>, LAN = the team gateway .1 on vmbr<id>) — at most one per lineup, engine-node teams only."
  type = list(object({
    name       = string
    last_octet = number
    cpu        = number
    memory_mb  = number
    disk_gb    = optional(number)
    disk_iface = optional(string)
    template   = string
    unmanaged  = optional(bool, false)
    in_path    = optional(bool, false)
  }))
  default = [
    { name = "web01", last_octet = 2, cpu = 2, memory_mb = 2048, disk_gb = 20, template = "tmpl-ubuntu-22" },
    { name = "ssh01", last_octet = 3, cpu = 1, memory_mb = 1024, disk_gb = 10, template = "tmpl-ubuntu-22" },
    { name = "dns01", last_octet = 132, cpu = 1, memory_mb = 512, disk_gb = 10, template = "tmpl-ubuntu-22" },
  ]
}

variable "datastore" {
  type    = string
  default = "local-lvm"
}

variable "engine_clone_id" {
  description = "vmid of this competition's engine TEMPLATE — the deployed engine is a linked clone of it (fresh identity/host keys per clone, empty scoring DB every run). deploy() sets it before apply #1; teardown never reads it."
  type        = number
  default     = 0
}

variable "scoring_vm_id" {
  description = "VMID for THIS competition's scoring engine. Per-competition (default 1000) so multiple ranges can be deployed concurrently on one Proxmox node without their engines colliding."
  type        = number
  default     = 1000
}
