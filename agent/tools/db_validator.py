"""
Step 3 & 4 — Query PostgreSQL and validate the APM ID.
Connects via Cloud SQL Auth Proxy or direct connection,
queries the apm_registry table, and returns validation result.
"""

import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from tools.secret_manager import get_secret

logger = logging.getLogger(__name__)


@dataclass
class DBRecord:
    apm_id: str
    application: str
    environment: str
    status: str
    owner: str
    created_at: str
    updated_at: str
    metadata: dict

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ValidationResult:
    status: str                # VALID | INVALID | NOT_FOUND | DB_QUERY_FAILED
    apm_id: str                # The APM ID that was validated
    validation_details: str    # Human-readable explanation
    db_record: Optional[dict]  # Matching DB record (None if not found)
    checks_performed: list     # List of individual checks
    query_time_ms: float       # Database query execution time
    error: str                 # Empty on success

    def to_dict(self) -> dict:
        return asdict(self)


async def validate_apm_in_database(
    apm_id: str,
    project_id: str,
    expected_application: str = "",
    expected_environment: str = "",
    db_secret_name: str = "db-connection-string",
    use_cloud_sql_connector: bool = True,
    cloud_sql_instance: str = "",
    db_name: str = "apm_db",
    db_user: str = "agent_validator",
) -> dict:
    """
    Query PostgreSQL to validate the APM ID.

    This is Steps 3 and 4 of the deterministic validation pipeline.
    Connects to PostgreSQL, queries the apm_registry table for the
    given APM ID, and performs cross-validation checks.

    Args:
        apm_id: The APM ID retrieved from Step 2.
        project_id: GCP project ID for Secret Manager and Cloud SQL.
        expected_application: Expected application name (from Step 2).
        expected_environment: Expected environment (from Step 2).
        db_secret_name: Secret Manager name for DB connection string.
        use_cloud_sql_connector: Use Cloud SQL Python Connector (recommended).
        cloud_sql_instance: Cloud SQL instance connection name (project:region:instance).
        db_name: Database name.
        db_user: Database user.

    Returns:
        ValidationResult as dictionary.
    """
    logger.info(f"Step 3: Querying PostgreSQL for APM ID: {apm_id}")

    if not apm_id or not apm_id.strip():
        return ValidationResult(
            status="DB_QUERY_FAILED",
            apm_id="",
            validation_details="APM ID is empty — cannot query database",
            db_record=None,
            checks_performed=[],
            query_time_ms=0,
            error="Empty APM ID provided",
        ).to_dict()

    conn = None
    start_time = datetime.now(timezone.utc)

    try:
        # ── Establish connection ─────────────────────────────────────
        if use_cloud_sql_connector and cloud_sql_instance:
            conn = await _connect_via_connector(
                cloud_sql_instance, db_name, db_user, project_id
            )
        else:
            connection_string = await get_secret(project_id, db_secret_name)
            conn = await asyncpg.connect(connection_string, ssl="require")

        logger.info("Database connection established")

        # ── Query the database ───────────────────────────────────────
        query = """
            SELECT
                apm_id,
                application,
                environment,
                status,
                owner,
                created_at::TEXT AS created_at,
                updated_at::TEXT AS updated_at,
                metadata::TEXT AS metadata
            FROM apm_registry
            WHERE apm_id = $1
            LIMIT 1
        """

        row = await conn.fetchrow(query, apm_id.strip())
        query_time = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000

        logger.info(f"Step 3 COMPLETE: Query executed in {query_time:.1f}ms")

        # ── Step 4: Validate ─────────────────────────────────────────
        if row is None:
            logger.info(f"Step 4 COMPLETE: APM ID {apm_id} NOT_FOUND in database")
            return ValidationResult(
                status="NOT_FOUND",
                apm_id=apm_id,
                validation_details=f"APM ID '{apm_id}' does not exist in the apm_registry table",
                db_record=None,
                checks_performed=[
                    {"check": "record_exists", "result": "FAIL", "detail": "No matching record found"}
                ],
                query_time_ms=round(query_time, 2),
                error="",
            ).to_dict()

        # ── Build DB record ──────────────────────────────────────────
        import json
        metadata_raw = row["metadata"]
        try:
            metadata_parsed = json.loads(metadata_raw) if metadata_raw else {}
        except (json.JSONDecodeError, TypeError):
            metadata_parsed = {}

        db_record = DBRecord(
            apm_id=row["apm_id"],
            application=row["application"],
            environment=row["environment"],
            status=row["status"],
            owner=row["owner"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            metadata=metadata_parsed,
        )

        # ── Run validation checks ────────────────────────────────────
        checks = []

        # Check 1: Record exists
        checks.append({
            "check": "record_exists",
            "result": "PASS",
            "detail": f"APM ID '{apm_id}' found in database",
        })

        # Check 2: Status is ACTIVE
        if db_record.status.upper() == "ACTIVE":
            checks.append({
                "check": "status_active",
                "result": "PASS",
                "detail": f"Status is ACTIVE",
            })
        else:
            checks.append({
                "check": "status_active",
                "result": "FAIL",
                "detail": f"Status is '{db_record.status}' — expected ACTIVE",
            })

        # Check 3: Application matches (if provided)
        if expected_application:
            if db_record.application.lower() == expected_application.lower():
                checks.append({
                    "check": "application_match",
                    "result": "PASS",
                    "detail": f"Application matches: {db_record.application}",
                })
            else:
                checks.append({
                    "check": "application_match",
                    "result": "FAIL",
                    "detail": f"Application mismatch: endpoint says '{expected_application}', DB says '{db_record.application}'",
                })

        # Check 4: Environment matches (if provided)
        if expected_environment:
            if db_record.environment.lower() == expected_environment.lower():
                checks.append({
                    "check": "environment_match",
                    "result": "PASS",
                    "detail": f"Environment matches: {db_record.environment}",
                })
            else:
                checks.append({
                    "check": "environment_match",
                    "result": "FAIL",
                    "detail": f"Environment mismatch: endpoint says '{expected_environment}', DB says '{db_record.environment}'",
                })

        # Check 5: Owner is assigned
        if db_record.owner:
            checks.append({
                "check": "owner_assigned",
                "result": "PASS",
                "detail": f"Owner: {db_record.owner}",
            })
        else:
            checks.append({
                "check": "owner_assigned",
                "result": "WARN",
                "detail": "No owner assigned to this APM entry",
            })

        # ── Determine overall status ────────────────────────────────
        has_failures = any(c["result"] == "FAIL" for c in checks)
        overall_status = "INVALID" if has_failures else "VALID"

        # Build human-readable details
        passed = sum(1 for c in checks if c["result"] == "PASS")
        failed = sum(1 for c in checks if c["result"] == "FAIL")
        warned = sum(1 for c in checks if c["result"] == "WARN")

        if overall_status == "VALID":
            details = f"APM ID '{apm_id}' is VALID. All {passed} checks passed"
            if warned > 0:
                details += f" ({warned} warning(s))"
        else:
            details = f"APM ID '{apm_id}' is INVALID. {failed} check(s) failed out of {len(checks)}"

        logger.info(f"Step 4 COMPLETE: {details}")

        return ValidationResult(
            status=overall_status,
            apm_id=apm_id,
            validation_details=details,
            db_record=db_record.to_dict(),
            checks_performed=checks,
            query_time_ms=round(query_time, 2),
            error="",
        ).to_dict()

    except asyncpg.PostgresError as e:
        error_msg = f"PostgreSQL error: {type(e).__name__}: {str(e)}"
        logger.error(f"Step 3 FAILED: {error_msg}")
        return ValidationResult(
            status="DB_QUERY_FAILED",
            apm_id=apm_id,
            validation_details="Database query failed",
            db_record=None,
            checks_performed=[],
            query_time_ms=0,
            error=error_msg,
        ).to_dict()

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        logger.error(f"Step 3 FAILED: {error_msg}")
        return ValidationResult(
            status="DB_QUERY_FAILED",
            apm_id=apm_id,
            validation_details="Database connection or query failed",
            db_record=None,
            checks_performed=[],
            query_time_ms=0,
            error=error_msg,
        ).to_dict()

    finally:
        if conn:
            await conn.close()
            logger.info("Database connection closed")


async def _connect_via_connector(
    instance_connection_name: str,
    db_name: str,
    db_user: str,
    project_id: str,
) -> asyncpg.Connection:
    """Connect to Cloud SQL using the Python Connector with IAM auth."""
    from google.cloud.sql.connector import Connector

    connector = Connector()

    conn = await connector.connect_async(
        instance_connection_name,
        "asyncpg",
        user=db_user,
        db=db_name,
        enable_iam_auth=True,
    )
    return conn
