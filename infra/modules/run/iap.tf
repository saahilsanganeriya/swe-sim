# Programmatic requests still need an IAP-authorized identity. This configures which OAuth clients may supply it.
# https://docs.cloud.google.com/iap/docs/sharing-oauth-clients#programmatic_access
resource "google_iap_settings" "console" {
  count = length(var.iap_programmatic_clients) > 0 ? 1 : 0
  name  = var.iap_resource_name
  access_settings {
    oauth_settings {
      programmatic_clients = var.iap_programmatic_clients
    }
  }
  depends_on = [google_cloud_run_v2_service.console]
}
