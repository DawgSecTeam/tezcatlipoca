variable "proxmox_endpoint" { type = string }

variable "proxmox_api_token" {
  type      = string
  sensitive = true
}

variable "proxmox_node" {
  type    = string
  default = "pve"
}

variable "ssh_public_key" { type = string }

variable "ssh_private_key_path" { type = string }

variable "vm_username" {
  description = "Built-in OS account already present on every template (not provisioned by us)"
  type        = string
  default     = "sysadmin"
}

variable "box_username" {
  description = "Username for the cloud-init account created on every team box clone. Themeable per competition via competitions/<id>/users.json (see utils.load_users_config()) — create-competition.py writes this into TF_VAR_box_username; defaults to 'ubuntu' when no users.json exists."
  type        = string
  default     = "ubuntu"
}

variable "box_password" {
  description = "Password for var.box_username's cloud-init account created on every team box clone. Generated fresh per competition in deploy() (create-competition.py) — not a fixed literal — because nakon authenticates with password auth (see quotient/setup.py) rather than a key, and a fixed value across every deployment would be guessable from this open-source repo. The username may vary per competition (var.box_username); only this password rotates."
  type        = string
  sensitive   = true
}

variable "event_name" {
  type    = string
  default = "Range 2026"
}

variable "teams" {
  description = "Map of team key → identifier (used as subnet third octet)"
  type        = map(object({ identifier = string, password = string }))
  # Identifiers follow the 192.168.<101-254>.x convention collect_teams() uses — the defaults
  # are placeholders (create-competition.py always overwrites TF_VAR_teams), but keeping them
  # realistic stops anyone hand-running `terraform apply` from building 192.168.1.x boxes the
  # engine's NAT/isolation rules (192.168.0.0/16, with team subnets at 101+) don't expect.
  default = {
    team1 = { identifier = "101", password = "team1pass" }
    team2 = { identifier = "102", password = "team2pass" }
  }
}

variable "boxes_per_team" {
  description = "Boxes cloned identically for every team. 'template' must match a Proxmox VM tagged 'template' exactly (see docs/usage-people.md 'Adding a template VM')."
  type = list(object({
    name       = string
    last_octet = number
    cpu        = number
    memory_mb  = number
    disk_gb    = optional(number) # omit to keep the template's own disk size (no resize). NOTE: omitting it also leaves the disk on the template's storage pool (var.datastore only applies when this block is emitted).
    disk_iface = optional(string) # interface for the disk block; defaults to scsi0. Set "sata0" for Windows templates that boot from SATA (scsi0 needs virtio-scsi drivers the image may lack).
    template   = string           # e.g. "tmpl-ubuntu-22", "tmpl-debian-12", "tmpl-centos-9"
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

variable "template_vm_id" {
  description = "VM ID of the base template the scoring engine is cloned from"
  type        = number
}
