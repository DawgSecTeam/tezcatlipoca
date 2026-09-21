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
  description = "Cloud-init account username on every team box clone (themeable via competitions/<id>/users.json; defaults to ubuntu)."
  type        = string
  default     = "ubuntu"
}

variable "box_password" {
  description = "Password for var.box_username's cloud-init account; generated fresh per competition in deploy(), never a fixed literal (nakon authenticates by password)."
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
    disk_gb    = optional(number)
    disk_iface = optional(string)
    template   = string
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
