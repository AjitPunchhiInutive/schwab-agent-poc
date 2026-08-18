"""
Runtime Validation Agent — Deterministic Pipeline
Deploy to: Agent Platform → Agents → Deployments

Uses ADK SequentialAgent for strict execution order.
No LLM orchestration. Fixed pipeline:
  Authenticate → Fetch APM ID → Query PostgreSQL → Return Result

Infrastructure:
  Cloud SQL: schwab-agent-poc:us-east4:apm-validation-db
  VPC:       projects/schwab-agent-poc/global/networks/agent-vpc
  SA:        sa-runtime-agent@schwab-agent-poc.iam.gserviceaccount.com
"""

import logging
import asyncpg
import httpx
from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.apps import App
from google.cloud import secretmanager

logger = logging.getLogger(__name__)

PROJECT_ID = "schwab-agent-poc"
CLOUD_SQL_INSTANCE = "schwab-agent-poc:us-east4:apm-validation-db"
MODEL = "gemini-2.5-flash"


# ═══════════════════════════════════════════════════════════════════════
# Secret Manager Helper
# ═══════════════════════════════════════════════════════════════════════

_sm_client = None

def _get_sm_client():
    global _sm_client
    if _sm_client is None:
        _sm_client = secretmanager.SecretManagerServiceClient()
    return _sm_client

def _read_secret(project_id: str, secret_name: str) -> str:
    client = _get_sm_client()
    name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
    response = client.access_secret_version(name=name)
    return response.payload.data.decode("utf-8").strip()


# ═══════════════════════════════════════════════════════════════════════
# Tool 1 — Authenticate against APM endpoint
# ═══════════════════════════════════════════════════════════════════════

def authenticate_endpoint(
    project_id: str = "schwab-agent-poc",
    auth_method: str = "oauth2",
) -> dict:
    """
    Step 1: Authenticate against the APM service endpoint.
    Retrieves credentials from Secret Manager and obtains an access token.

    Args:
        project_id: GCP project ID where secrets are stored.
        auth_method: Authentication method — oauth2, api_key, or bearer_token.

    Returns:
        dict with status, token, endpoint_url, method, and error fields.
    """
    logger.info("STEP 1: Authenticating against APM endpoint")
    try:
        endpoint_url = _read_secret(project_id, "apm-endpoint-url")
        client_id = _read_secret(project_id, "apm-client-id")
        client_secret = _read_secret(project_id, "apm-client-secret")

        if not all([endpoint_url, client_id, client_secret]):
            return {"status": "AUTH_FAILED", "token": "", "endpoint_url": "", "method": auth_method, "error": "Missing secrets in Secret Manager"}

        with httpx.Client(timeout=30.0, verify=True) as http:
            if auth_method == "oauth2":
                resp = http.post(
                    f"{endpoint_url.rstrip('/')}/oauth/token",
                    data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
                )
                resp.raise_for_status()
                token = resp.json()["access_token"]
            elif auth_method == "api_key":
                resp = http.get(
                    f"{endpoint_url.rstrip('/')}/auth/validate",
                    headers={"X-API-Key": client_secret, "X-Client-ID": client_id},
                )
                resp.raise_for_status()
                token = client_secret
            elif auth_method == "bearer_token":
                token = client_secret
            else:
                return {"status": "AUTH_FAILED", "token": "", "endpoint_url": endpoint_url, "method": auth_method, "error": f"Unknown auth method: {auth_method}"}

        logger.info("STEP 1 COMPLETE: Authentication successful")
        return {"status": "SUCCESS", "token": token, "endpoint_url": endpoint_url, "method": auth_method, "error": ""}

    except Exception as e:
        logger.error(f"STEP 1 FAILED: {e}")
        return {"status": "AUTH_FAILED", "token": "", "endpoint_url": "", "method": auth_method, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════
# Tool 2 — Fetch APM ID from endpoint
# ═══════════════════════════════════════════════════════════════════════

def fetch_apm_id(
    endpoint_url: str,
    token: str,
    auth_method: str = "oauth2",
) -> dict:
    """
    Step 2: Fetch the APM ID from the authenticated service endpoint.

    Args:
        endpoint_url: Base URL of the APM service from Step 1.
        token: Access token from Step 1.
        auth_method: How to send the token from Step 1.

    Returns:
        dict with status, apm_id, application, environment, and error fields.
    """
    logger.info("STEP 2: Fetching APM ID from endpoint")
    try:
        url = f"{endpoint_url.rstrip('/')}/api/v1/apm/current"
        if auth_method in ("oauth2", "bearer_token"):
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        else:
            headers = {"X-API-Key": token, "Accept": "application/json"}

        with httpx.Client(timeout=30.0, verify=True) as http:
            resp = http.get(url, headers=headers)
            resp.raise_for_status()

        data = resp.json()
        apm_id = data.get("apm_id") or data.get("data", {}).get("apm_id") or data.get("id", "")

        if not apm_id:
            return {"status": "APM_FETCH_FAILED", "apm_id": "", "application": "", "environment": "", "error": f"APM ID not in response. Keys: {list(data.keys())}"}

        logger.info(f"STEP 2 COMPLETE: APM ID = {apm_id}")
        return {"status": "SUCCESS", "apm_id": str(apm_id).strip(), "application": data.get("application", ""), "environment": data.get("environment", ""), "error": ""}

    except Exception as e:
        logger.error(f"STEP 2 FAILED: {e}")
        return {"status": "APM_FETCH_FAILED", "apm_id": "", "application": "", "environment": "", "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════
# Tool 3 — Validate APM ID against PostgreSQL
# ═══════════════════════════════════════════════════════════════════════

def validate_apm_in_database(
    apm_id: str,
    project_id: str = "schwab-agent-poc",
    expected_application: str = "",
    expected_environment: str = "",
) -> dict:
    """
    Step 3-4: Query PostgreSQL and cross-validate the APM ID.
    Connects to Cloud SQL instance schwab-agent-poc:us-east4:apm-validation-db.

    Args:
        apm_id: The APM ID to validate from Step 2.
        project_id: GCP project ID for Secret Manager.
        expected_application: Application name to cross-check from Step 2.
        expected_environment: Environment to cross-check from Step 2.

    Returns:
        dict with status (VALID/INVALID/NOT_FOUND/DB_QUERY_FAILED),
        apm_id, checks list, db_record dict, and error.
    """
    logger.info(f"STEP 3-4: Validating APM ID '{apm_id}' in PostgreSQL")

    if not apm_id or not apm_id.strip():
        return {"status": "DB_QUERY_FAILED", "apm_id": "", "checks": [], "db_record": None, "error": "Empty APM ID"}

    import asyncio

    async def _query():
        conn = None
        try:
            from google.cloud.sql.connector import Connector
            connector = Connector()
            conn = await connector.connect_async(
                CLOUD_SQL_INSTANCE,
                "asyncpg",
                user="sa-runtime-agent@schwab-agent-poc.iam",
                db="apm_db",
                enable_iam_auth=True,
            )

            row = await conn.fetchrow(
                "SELECT apm_id, application, environment, status, owner "
                "FROM apm_registry WHERE apm_id = $1 LIMIT 1",
                apm_id.strip(),
            )

            if row is None:
                return {"status": "NOT_FOUND", "apm_id": apm_id, "checks": [{"check": "record_exists", "result": "FAIL", "detail": "No matching record"}], "db_record": None, "error": ""}

            rec = dict(row)
            checks = [{"check": "record_exists", "result": "PASS", "detail": "Found in database"}]

            if rec.get("status", "").upper() == "ACTIVE":
                checks.append({"check": "status_active", "result": "PASS", "detail": "ACTIVE"})
            else:
                checks.append({"check": "status_active", "result": "FAIL", "detail": f"Status: {rec.get('status')}"})

            if expected_application:
                ok = rec.get("application", "").lower() == expected_application.lower()
                checks.append({"check": "application_match", "result": "PASS" if ok else "FAIL", "detail": f"DB={rec.get('application')}, Expected={expected_application}"})

            if expected_environment:
                ok = rec.get("environment", "").lower() == expected_environment.lower()
                checks.append({"check": "environment_match", "result": "PASS" if ok else "FAIL", "detail": f"DB={rec.get('environment')}, Expected={expected_environment}"})

            failed = any(c["result"] == "FAIL" for c in checks)
            return {"status": "INVALID" if failed else "VALID", "apm_id": apm_id, "checks": checks, "db_record": rec, "error": ""}

        except Exception as e:
            return {"status": "DB_QUERY_FAILED", "apm_id": apm_id, "checks": [], "db_record": None, "error": str(e)}
        finally:
            if conn:
                await conn.close()

    try:
        result = asyncio.run(_query())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(_query())
        loop.close()

    logger.info(f"STEP 3-4 COMPLETE: {result['status']}")
    return result


# ═══════════════════════════════════════════════════════════════════════
# Sub-Agents — One per pipeline step
# output_key saves results to shared session state
# ═══════════════════════════════════════════════════════════════════════

step1_authenticate = LlmAgent(
    model=MODEL,
    name="step1_authenticate",
    description="Step 1: Authenticate against APM endpoint",
    output_key="auth_result",
    instruction=(
        "You are Step 1 of a fixed validation pipeline.\n"
        "Call authenticate_endpoint with project_id='schwab-agent-poc' and auth_method from the user (default 'oauth2').\n"
        "Return the EXACT tool result as JSON. Do not add commentary."
    ),
    tools=[authenticate_endpoint],
)

step2_fetch_apm = LlmAgent(
    model=MODEL,
    name="step2_fetch_apm",
    description="Step 2: Fetch APM ID from authenticated endpoint",
    output_key="apm_result",
    instruction=(
        "You are Step 2 of a fixed validation pipeline.\n"
        "Read auth_result from state. If status is AUTH_FAILED, return: "
        '{"pipeline": "ABORTED", "failed_at": "Step 1", "error": "<error>"}.\n'
        "Otherwise call fetch_apm_id with endpoint_url, token, method from auth_result.\n"
        "Return the EXACT tool result as JSON."
    ),
    tools=[fetch_apm_id],
)

step3_validate = LlmAgent(
    model=MODEL,
    name="step3_validate_db",
    description="Step 3-4: Validate APM ID against PostgreSQL (schwab-agent-poc:us-east4:apm-validation-db)",
    output_key="validation_result",
    instruction=(
        "You are Step 3-4 of a fixed validation pipeline.\n"
        "Read apm_result from state. If status is APM_FETCH_FAILED, return: "
        '{"pipeline": "ABORTED", "failed_at": "Step 2", "error": "<error>"}.\n'
        "Otherwise call validate_apm_in_database with apm_id from apm_result, "
        "project_id='schwab-agent-poc', expected_application and expected_environment from apm_result.\n"
        "Return the COMPLETE validation result as JSON."
    ),
    tools=[validate_apm_in_database],
)


# ═══════════════════════════════════════════════════════════════════════
# Root Agent — SequentialAgent (DETERMINISTIC)
#
# SequentialAgent is NOT powered by an LLM for orchestration.
# It executes sub-agents in STRICT ORDER: step1 → step2 → step3.
# Data flows via shared session state through output_key.
# ═══════════════════════════════════════════════════════════════════════

root_agent = SequentialAgent(
    name="runtime_validation_agent",
    description=(
        "Deterministic APM validation pipeline deployed on Agent Platform. "
        "Authenticates, fetches APM ID, validates against PostgreSQL. "
        "SequentialAgent — no LLM orchestration, fixed execution order. "
        "Cloud SQL: schwab-agent-poc:us-east4:apm-validation-db"
    ),
    sub_agents=[
        step1_authenticate,
        step2_fetch_apm,
        step3_validate,
    ],
)

app = App(
    root_agent=root_agent,
    name="runtime-validation-agent",
)
