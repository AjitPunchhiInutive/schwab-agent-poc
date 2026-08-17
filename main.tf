# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Runtime Validation Agent — Infrastructure
#  Project: melodic-furnace-403022 (schwab-agent-poc)
#  State:   gs://itp-terraform-test/schwab-agent-poc/state
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

locals {
  compute_region = "us-east4"
  agent_sa_name  = "sa-runtime-agent"

  labels = {
    business_unit = "cloud-engineering"
    application   = "schwab-agent-poc"
    cost_center   = "c852700"
    environment   = "poc"
    agent_type    = "deterministic"
  }
}

# ══════════════════════════════════════════════════════════════════════
# Enable Required APIs
# ══════════════════════════════════════════════════════════════════════

resource "google_project_service" "apis" {
  for_each = toset([
    "aiplatform.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "sqladmin.googleapis.com",
    "cloudtrace.googleapis.com",
    "logging.googleapis.com",
    "artifactregistry.googleapis.com",
    "vpcaccess.googleapis.com",
    "iam.googleapis.com",
    "servicenetworking.googleapis.com",
    "compute.googleapis.com",
  ])

  project            = var.gcp_project
  service            = each.value
  disable_on_destroy = false
}

# ══════════════════════════════════════════════════════════════════════
# Service Account (Least Privilege)
# ══════════════════════════════════════════════════════════════════════

resource "google_service_account" "agent" {
  account_id   = local.agent_sa_name
  display_name = "Runtime Validation Agent"
  description  = "Deterministic agent — APM ID validation against PostgreSQL"
  project      = var.gcp_project

  depends_on = [google_project_service.apis]
}

locals {
  agent_roles = [
    "roles/secretmanager.secretAccessor",
    "roles/cloudsql.client",
    "roles/cloudsql.instanceUser",
    "roles/logging.logWriter",
    "roles/cloudtrace.agent",
    "roles/run.invoker",
  ]
}

resource "google_project_iam_member" "agent_roles" {
  for_each = toset(local.agent_roles)

  project = var.gcp_project
  role    = each.value
  member  = "serviceAccount:${google_service_account.agent.email}"
}

# ══════════════════════════════════════════════════════════════════════
# Secret Manager
# ══════════════════════════════════════════════════════════════════════

resource "google_secret_manager_secret" "secrets" {
  for_each = toset([
    "apm-endpoint-url",
    "apm-client-id",
    "apm-client-secret",
  ])

  secret_id = each.value
  project   = var.gcp_project
  labels    = local.labels

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "agent_secret_access" {
  for_each = toset([
    "apm-endpoint-url",
    "apm-client-id",
    "apm-client-secret",
  ])

  project   = var.gcp_project
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent.email}"

  depends_on = [
    google_secret_manager_secret.secrets,
    google_service_account.agent,
  ]
}

# ══════════════════════════════════════════════════════════════════════
# VPC Network
# ══════════════════════════════════════════════════════════════════════

resource "google_compute_network" "agent_vpc" {
  name                    = "agent-vpc"
  auto_create_subnetworks = false
  project                 = var.gcp_project

  depends_on = [google_project_service.apis]
}

resource "google_compute_subnetwork" "agent_subnet" {
  name                     = "agent-subnet"
  ip_cidr_range            = "10.0.0.0/24"
  region                   = local.compute_region
  network                  = google_compute_network.agent_vpc.id
  project                  = var.gcp_project
  private_ip_google_access = true
}

# Serverless VPC Connector (Cloud Run → Cloud SQL)
resource "google_vpc_access_connector" "agent_connector" {
  name          = "agent-vpc-connector"
  region        = local.compute_region
  project       = var.gcp_project
  ip_cidr_range = "10.8.0.0/28"
  network       = google_compute_network.agent_vpc.name

  min_instances = 2
  max_instances = 10

  depends_on = [google_project_service.apis]
}

# Private Service Networking (Cloud SQL Private IP)
resource "google_compute_global_address" "private_ip_range" {
  name          = "cloud-sql-private-ip"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.agent_vpc.id
  project       = var.gcp_project
}

resource "google_service_networking_connection" "private_vpc" {
  network                 = google_compute_network.agent_vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_ip_range.name]

  depends_on = [google_project_service.apis]
}

# ══════════════════════════════════════════════════════════════════════
# Firewall Rules
# ══════════════════════════════════════════════════════════════════════

resource "google_compute_firewall" "deny_all_ingress" {
  name    = "deny-all-ingress"
  network = google_compute_network.agent_vpc.name
  project = var.gcp_project

  direction = "INGRESS"
  priority  = 65534

  deny {
    protocol = "all"
  }

  source_ranges = ["0.0.0.0/0"]
}

resource "google_compute_firewall" "allow_internal_postgres" {
  name    = "allow-internal-postgres"
  network = google_compute_network.agent_vpc.name
  project = var.gcp_project

  direction = "INGRESS"
  priority  = 1000

  allow {
    protocol = "tcp"
    ports    = ["5432"]
  }

  source_ranges = ["10.0.0.0/8"]
  description   = "Allow PostgreSQL from internal VPC (agent to Cloud SQL)"
}

# ══════════════════════════════════════════════════════════════════════
# Cloud SQL (PostgreSQL)
# ══════════════════════════════════════════════════════════════════════

resource "google_sql_database_instance" "postgres" {
  name                = "apm-validation-db"
  database_version    = "POSTGRES_15"
  region              = local.compute_region
  project             = var.gcp_project
  deletion_protection = true

  settings {
    tier              = "db-custom-2-4096"
    availability_type = "REGIONAL"
    disk_autoresize   = true
    disk_size         = 20
    disk_type         = "PD_SSD"

    ip_configuration {
      ipv4_enabled    = false
      private_network = google_compute_network.agent_vpc.id
      require_ssl     = true
    }

    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = "03:00"
      backup_retention_settings {
        retained_backups = 14
      }
    }

    maintenance_window {
      day          = 7
      hour         = 4
      update_track = "stable"
    }

    user_labels = local.labels
  }

  depends_on = [google_service_networking_connection.private_vpc]
}

resource "google_sql_database" "apm_db" {
  name     = "apm_db"
  instance = google_sql_database_instance.postgres.name
  project  = var.gcp_project
}

# IAM database user — agent authenticates via service account, no password
resource "google_sql_user" "agent_iam_user" {
  name     = google_service_account.agent.email
  instance = google_sql_database_instance.postgres.name
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"
  project  = var.gcp_project
}

# ══════════════════════════════════════════════════════════════════════
# Artifact Registry
# ══════════════════════════════════════════════════════════════════════

resource "google_artifact_registry_repository" "agent_images" {
  location      = local.compute_region
  repository_id = "agent-images"
  format        = "DOCKER"
  project       = var.gcp_project
  description   = "Container images for runtime validation agent"
  labels        = local.labels

  depends_on = [google_project_service.apis]
}
