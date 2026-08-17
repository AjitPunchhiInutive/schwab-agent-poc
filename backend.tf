terraform {
  backend "gcs" {
    bucket = "itp-terraform-test"
    prefix = "schwab-agent-poc/state"
  }
}
