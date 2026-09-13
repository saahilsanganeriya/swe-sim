# Programmatic requests still need an IAP-authorized identity. This configures which OAuth clients may supply it.
# https://docs.cloud.google.com/iap/docs/sharing-oauth-clients#programmatic_access
#
# IAP only accepts clients owned by a project in the same organization as the console; the API answers "Programmatic
# Oauth Client ... is not in the same organization as resource" (measured 2026-09-13 with the gcloud SDK client
# 32555940559.apps.googleusercontent.com) and the apply fails half-way. The precondition below turns that into a plan-time
# refusal: a client is accepted only when the project number it starts with is this project's or one the operator listed
# in iap_client_projects.
locals {
  accepted_client_projects = concat([var.project_number], var.iap_client_projects)
  # "<project_number>-<id>.apps.googleusercontent.com" -> "<project_number>"; anything else -> "" (never accepted).
  client_owner     = { for c in var.iap_programmatic_clients : c => can(regex("^[0-9]+-[a-z0-9]+\\.apps\\.googleusercontent\\.com$", c)) ? split("-", c)[0] : "" }
  rejected_clients = [for c, owner in local.client_owner : c if !contains(local.accepted_client_projects, owner)]
}

resource "google_iap_settings" "console" {
  count = length(var.iap_programmatic_clients) > 0 ? 1 : 0
  name  = var.iap_resource_name
  access_settings {
    oauth_settings {
      programmatic_clients = var.iap_programmatic_clients
    }
  }
  depends_on = [google_cloud_run_v2_service.console]

  lifecycle {
    precondition {
      condition     = length(local.rejected_clients) == 0
      error_message = "iap_programmatic_clients must be organization OAuth clients: ${join(", ", local.rejected_clients)} is not owned by project ${var.project_number} or by a project listed in iap_programmatic_client_projects. Google's public clients (32555940559, 764086051850) can never pass IAP here. Clear the list (SWESIM_IAP_PROGRAMMATIC_CLIENTS in the env file, then `make tfvars`) or create an OAuth client in this project."
    }
  }
}
