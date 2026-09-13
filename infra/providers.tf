provider "google" {
  project        = var.project
  region         = var.region
  default_labels = { app = "swesim" }
}

# google-beta is used only for `google_project_service_identity` (the IAP service agent); modules inherit it.
provider "google-beta" {
  project        = var.project
  region         = var.region
  default_labels = { app = "swesim" }
}
