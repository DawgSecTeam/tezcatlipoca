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

variable "vm_password" {
  description = "Password for var.vm_username, baked into the templates. Used once to bootstrap key-based access on the scoring engine, which doesn't go through cloud-init."
  type        = string
  sensitive   = true
  default     = "asdf"
}

variable "quotient_admin_password" {
  description = "Password baked into event.conf's admin account (username 'admin'). Orchestration logs in with this after Quotient boots to obtain an API token — you choose this value, no need to know anything from inside Quotient ahead of time."
  type        = string
  sensitive   = true
}

variable "event_name" {
  type    = string
  default = "Range 2025"
}

variable "teams" {
  description = "Map of team key → identifier (used as subnet third octet)"
  type        = map(object({ identifier = string, password = string }))
  default = {
    team1 = { identifier = "1", password = "team1pass" }
    team2 = { identifier = "2", password = "team2pass" }
  }
}

variable "boxes_per_team" {
  description = "Boxes cloned identically for every team. 'template' must match a Packer-built template name in Proxmox."
  type = list(object({
    name       = string
    last_octet = number
    cpu        = number
    memory_mb  = number
    disk_gb    = optional(number) # omit to keep the template's own disk size (no resize)
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
