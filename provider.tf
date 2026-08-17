terraform {
  required_version = ">= 1.6.3"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.20.0, <= 7.15.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 5.20.0, <= 7.15.0"
    }
    time = {
      source  = "hashicorp/time"
      version = ">= 0.9.0"
    }
  }
}

provider "google" {
  project        = var.gcp_project
  region         = var.region
  zone           = var.zone
}

provider "google-beta" {
  project        = var.gcp_project
  region         = var.region
  zone           = var.zone
}