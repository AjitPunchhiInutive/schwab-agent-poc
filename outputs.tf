# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Outputs — Runtime Validation Agent
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

output "service_account_email" {
  description = "Agent service account email"
  value       = google_service_account.agent.email
}

output "cloud_sql_instance_name" {
  description = "Cloud SQL instance connection name"
  value       = google_sql_database_instance.postgres.connection_name
}

output "cloud_sql_private_ip" {
  description = "Cloud SQL private IP address"
  value       = google_sql_database_instance.postgres.private_ip_address
}

output "vpc_name" {
  description = "VPC network name"
  value       = google_compute_network.agent_vpc.name
}

output "vpc_connector_name" {
  description = "Serverless VPC Connector name"
  value       = google_vpc_access_connector.agent_connector.name
}

output "artifact_registry_url" {
  description = "Artifact Registry repository path"
  value       = "${local.compute_region}-docker.pkg.dev/${var.gcp_project}/${google_artifact_registry_repository.agent_images.repository_id}"
}

output "database_name" {
  description = "PostgreSQL database name"
  value       = google_sql_database.apm_db.name
}
