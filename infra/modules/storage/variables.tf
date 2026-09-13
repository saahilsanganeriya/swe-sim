variable "project" {
  type = string
}

variable "region" {
  type = string
}

variable "bucket_name" {
  type = string
}

variable "force_destroy" {
  type    = bool
  default = false
}
