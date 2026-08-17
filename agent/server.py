"""
HTTP server entry point for Cloud Run.
Exposes /healthz for readiness probes and /validate for agent invocation.
"""

import os
import json
import asyncio
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tools.authenticator import authenticate_endpoint
from tools.apm_fetcher import fetch_apm_id
from tools.db_validator import validate_apm_in_database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Runtime Validation Agent")

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")


@app.get("/healthz")
async def health():
    """Health check endpoint for Cloud Run readiness probes."""
    return {"status": "healthy", "agent": "runtime-validation-agent"}


@app.post("/validate")
async def validate(request: Request):
    """
    Execute the deterministic validation pipeline.

    Request body (JSON):
        {
            "project_id": "melodic-furnace-403022",    (optional — defaults to GCP_PROJECT_ID env)
            "auth_method": "oauth2",                    (optional — oauth2 | api_key | bearer_token)
            "cloud_sql_instance": "project:region:instance"  (optional — for Cloud SQL Connector)
        }
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    project_id = body.get("project_id", PROJECT_ID)
    auth_method = body.get("auth_method", "oauth2")
    cloud_sql_instance = body.get("cloud_sql_instance", "")

    if not project_id:
        return JSONResponse(
            status_code=400,
            content={"status": "ERROR", "error": "project_id is required (set GCP_PROJECT_ID env or pass in body)"},
        )

    logger.info(f"Validation request received — project: {project_id}, auth: {auth_method}")

    # ── Step 1: Authenticate ─────────────────────────────────────────
    logger.info("Step 1/4: Authenticating...")
    auth_result = await authenticate_endpoint(
        project_id=project_id,
        auth_method=auth_method,
    )

    if auth_result["status"] != "SUCCESS":
        logger.error(f"Pipeline ABORTED at Step 1: {auth_result['error']}")
        return JSONResponse(
            status_code=401,
            content={
                "pipeline_status": "ABORTED",
                "failed_at": "Step 1 — Authentication",
                "auth_result": auth_result,
            },
        )

    # ── Step 2: Fetch APM ID ─────────────────────────────────────────
    logger.info("Step 2/4: Fetching APM ID...")
    apm_result = await fetch_apm_id(
        endpoint_url=auth_result["endpoint_url"],
        token=auth_result["token"],
        auth_method=auth_result["method"],
    )

    if apm_result["status"] != "SUCCESS":
        logger.error(f"Pipeline ABORTED at Step 2: {apm_result['error']}")
        return JSONResponse(
            status_code=502,
            content={
                "pipeline_status": "ABORTED",
                "failed_at": "Step 2 — Fetch APM ID",
                "auth_result": auth_result,
                "apm_result": apm_result,
            },
        )

    # ── Step 3 & 4: Validate against PostgreSQL ──────────────────────
    logger.info(f"Step 3-4/4: Validating APM ID '{apm_result['apm_id']}' against database...")
    validation_result = await validate_apm_in_database(
        apm_id=apm_result["apm_id"],
        project_id=project_id,
        expected_application=apm_result.get("application", ""),
        expected_environment=apm_result.get("environment", ""),
        cloud_sql_instance=cloud_sql_instance,
    )

    # ── Return complete result ───────────────────────────────────────
    pipeline_status = "SUCCESS" if validation_result["status"] == "VALID" else "COMPLETED_WITH_ISSUES"
    status_code = 200 if validation_result["status"] in ("VALID", "INVALID", "NOT_FOUND") else 500

    logger.info(f"Pipeline COMPLETE: {validation_result['status']}")

    return JSONResponse(
        status_code=status_code,
        content={
            "pipeline_status": pipeline_status,
            "validation_result": validation_result,
            "apm_result": {
                "apm_id": apm_result["apm_id"],
                "application": apm_result["application"],
                "environment": apm_result["environment"],
            },
        },
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
