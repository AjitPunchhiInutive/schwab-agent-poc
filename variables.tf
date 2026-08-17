variable "bucket_name" {
  description = "GCS bucket name for Terraform remote state"
  type        = string
}

variable "gcp_project" {
  description = "GCP project ID to deploy resources into"
  type        = string
}

variable "region" {
  description = "GCP region for resource deployment"
  type        = string
}

variable "zone" {
  description = "GCP zone for resource deployment"
  type        = string
}

variable "organization_id" {
  description = "GCP organization ID"
  type        = string
}