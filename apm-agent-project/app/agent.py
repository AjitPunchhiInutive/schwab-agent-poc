# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Deterministic runtime validation agent.

Fixed pipeline (SequentialAgent, not LLM-ordered):
  1. authenticate_endpoint  -> auth_result
  2. fetch_apm_id           -> apm_result
  3. validate_apm_in_database -> validation_result
"""

import functools
import os
from functools import cached_property

import httpx
from google.adk.agents import Agent, SequentialAgent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import Client, types

MODEL = "gemini-3.6-flash"


class GlobalGemini(Gemini):
    """Gemini model served via the "global" Vertex AI endpoint.

    us-east4 (this project's region for Cloud SQL/gateway/deployment) doesn't
    serve Gemini publisher models; the model client needs "global" regardless
    of GOOGLE_CLOUD_LOCATION.
    """

    @cached_property
    def api_client(self) -> Client:
        return Client(vertexai=True, project=PROJECT_ID, location="global")

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "schwab-agent-poc")
SQL_REGION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-east4")
SQL_INSTANCE = os.environ.get("SQL_INSTANCE", "apm-validation-db")
DB_NAME = os.environ.get("DB_NAME", "apm_db")
DB_IAM_USER = os.environ.get(
    "DB_IAM_USER", "sa-runtime-agent@schwab-agent-poc.iam"
)
APM_TABLE = os.environ.get("APM_TABLE", "apm_registry")

SECRET_APM_ENDPOINT_URL = "apm-endpoint-url"
SECRET_APM_CLIENT_ID = "apm-client-id"
SECRET_APM_CLIENT_SECRET = "apm-client-secret"


@functools.cache
def _get_secret(secret_id: str, version: str = "latest") -> str:
    """Reads a secret payload from Secret Manager at call time (cached per process)."""
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/{version}"
    response = client.access_secret_version(name=name)
    return response.payload.data.decode("utf-8")


def authenticate_endpoint() -> dict:
    """Authenticates against the APM endpoint using credentials from Secret Manager.

    Reads the endpoint URL and OAuth2 client credentials from Secret Manager
    (apm-endpoint-url, apm-client-id, apm-client-secret) and exchanges them for
    a bearer token via the standard client_credentials grant.

    Returns:
        A dict with keys: status ("AUTHENTICATED" or "AUTH_FAILED"), endpoint_url,
        and access_token (omitted on failure), plus an "error" message on failure.
    """
    try:
        endpoint_url = _get_secret(SECRET_APM_ENDPOINT_URL)
        client_id = _get_secret(SECRET_APM_CLIENT_ID)
        client_secret = _get_secret(SECRET_APM_CLIENT_SECRET)
    except Exception as e:
        return {"status": "AUTH_FAILED", "error": f"Secret Manager error: {e}"}

    try:
        response = httpx.post(
            f"{endpoint_url.rstrip('/')}/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        token = response.json().get("access_token")
        if not token:
            return {
                "status": "AUTH_FAILED",
                "endpoint_url": endpoint_url,
                "error": "Token endpoint returned no access_token",
            }
        return {
            "status": "AUTHENTICATED",
            "endpoint_url": endpoint_url,
            "access_token": token,
        }
    except httpx.HTTPError as e:
        return {
            "status": "AUTH_FAILED",
            "endpoint_url": endpoint_url,
            "error": str(e),
        }


def fetch_apm_id(auth_result: dict) -> dict:
    """Retrieves the APM ID (and associated application/environment) from the
    authenticated APM endpoint.

    Args:
        auth_result: The dict returned by authenticate_endpoint (state key
            "auth_result"). Must have status == "AUTHENTICATED".

    Returns:
        A dict with keys: status ("FETCHED" or "FETCH_FAILED"), apm_id,
        application, environment, plus an "error" message on failure.
    """
    if auth_result.get("status") != "AUTHENTICATED":
        return {"status": "FETCH_FAILED", "error": "Not authenticated"}

    endpoint_url = auth_result["endpoint_url"]
    token = auth_result["access_token"]

    try:
        response = httpx.get(
            f"{endpoint_url.rstrip('/')}/apm",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
        apm_id = payload.get("apm_id")
        if not apm_id:
            return {
                "status": "FETCH_FAILED",
                "error": "Response missing apm_id",
            }
        return {
            "status": "FETCHED",
            "apm_id": apm_id,
            "application": payload.get("application"),
            "environment": payload.get("environment"),
        }
    except httpx.HTTPError as e:
        return {"status": "FETCH_FAILED", "error": str(e)}


async def validate_apm_in_database(apm_result: dict) -> dict:
    """Validates the fetched APM ID against the apm_registry table in Cloud SQL.

    Connects via the Cloud SQL Python Connector using IAM authentication (no
    passwords) and runs the four checks: record_exists, status_active,
    application_match, environment_match.

    Args:
        apm_result: The dict returned by fetch_apm_id (state key "apm_result").
            Must have status == "FETCHED".

    Returns:
        A dict with keys: overall_status (VALID / INVALID / NOT_FOUND /
        DB_QUERY_FAILED), apm_id, and checks (per-check PASS/FAIL details).
    """
    if apm_result.get("status") != "FETCHED":
        return {"overall_status": "DB_QUERY_FAILED", "error": "No APM ID to validate"}

    apm_id = apm_result["apm_id"]

    from google.cloud.sql.connector import Connector

    connector = Connector()
    try:
        conn = await connector.connect_async(
            f"{PROJECT_ID}:{SQL_REGION}:{SQL_INSTANCE}",
            "asyncpg",
            user=DB_IAM_USER,
            db=DB_NAME,
            enable_iam_auth=True,
        )
        try:
            row = await conn.fetchrow(
                f"SELECT apm_id, application, environment, status "
                f"FROM {APM_TABLE} WHERE apm_id = $1",
                apm_id,
            )
        finally:
            await conn.close()
    except Exception as e:
        return {
            "overall_status": "DB_QUERY_FAILED",
            "apm_id": apm_id,
            "error": str(e),
        }
    finally:
        await connector.close_async()

    if row is None:
        return {"overall_status": "NOT_FOUND", "apm_id": apm_id}

    checks = {
        "record_exists": "PASS",
        "status_active": "PASS" if row["status"] == "ACTIVE" else "FAIL",
        "application_match": (
            "PASS" if row["application"] == apm_result.get("application") else "FAIL"
        ),
        "environment_match": (
            "PASS" if row["environment"] == apm_result.get("environment") else "FAIL"
        ),
    }
    overall_status = "VALID" if all(v == "PASS" for v in checks.values()) else "INVALID"

    return {
        "overall_status": overall_status,
        "apm_id": apm_id,
        "checks": checks,
    }


def _llm_agent(name: str, instruction: str, tools: list, output_key: str) -> Agent:
    return Agent(
        name=name,
        model=GlobalGemini(
            model=MODEL, retry_options=types.HttpRetryOptions(attempts=3)
        ),
        instruction=instruction,
        tools=tools,
        output_key=output_key,
    )


step1_authenticate = _llm_agent(
    name="step1_authenticate",
    instruction=(
        "Call the authenticate_endpoint tool (no arguments) to authenticate "
        "against the APM endpoint. Report the tool's result verbatim."
    ),
    tools=[authenticate_endpoint],
    output_key="auth_result",
)

step2_fetch_apm = _llm_agent(
    name="step2_fetch_apm",
    instruction=(
        "Read auth_result from state: {auth_result}. Call the fetch_apm_id tool "
        "with auth_result as its argument to retrieve the APM ID. Report the "
        "tool's result verbatim."
    ),
    tools=[fetch_apm_id],
    output_key="apm_result",
)

step3_validate_db = _llm_agent(
    name="step3_validate_db",
    instruction=(
        "Read apm_result from state: {apm_result}. Call the "
        "validate_apm_in_database tool with apm_result as its argument to "
        "validate the APM ID against the database. Report the tool's result "
        "verbatim, including overall_status and each check."
    ),
    tools=[validate_apm_in_database],
    output_key="validation_result",
)

root_agent = SequentialAgent(
    name="root_agent",
    sub_agents=[step1_authenticate, step2_fetch_apm, step3_validate_db],
)

app = App(
    root_agent=root_agent,
    name="runtime-validation-agent",
)
