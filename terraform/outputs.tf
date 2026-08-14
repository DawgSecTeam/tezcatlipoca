output "scoring_engine_ip" {
  value = local.scoring_ip
}

output "teams" {
  value = { for k, v in var.teams : k => v.identifier }
}

output "boxes_per_team" {
  value = var.boxes_per_team
}

output "ssh_private_key_path" {
  value = var.ssh_private_key_path
}

output "team_vms" {
  value = {
    for k, v in local.team_vms : k => {
      ip         = v.ip
      identifier = v.identifier
      box_name   = v.box.name
    }
  }
}

output "team_passwords" {
  value     = { for k, v in var.teams : k => v.password }
  sensitive = true
}

# Written to disk for the agent
output "agent_context" {
  value = jsonencode({
    scoring_engine_ip       = local.scoring_ip
    teams                   = { for k, v in var.teams : k => v.identifier }
    boxes_per_team          = var.boxes_per_team
    team_passwords          = { for k, v in var.teams : k => v.password }
    ssh_key_path            = var.ssh_private_key_path
    vm_username             = var.vm_username
    box_username            = var.box_username
    event_name              = var.event_name
    quotient_admin_password = var.quotient_admin_password
  })
  sensitive = true
}
