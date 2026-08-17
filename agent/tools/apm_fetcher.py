"""
Step 2 — Fetch APM ID from the authenticated endpoint.
Uses the token from Step 1 to retrieve the APM identifier.
"""

import httpx
import logging
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class APMFetchResult:
    status: str        # SUCCESS | APM_FETCH_FAILED
    apm_id: str        # The retrieved APM ID
    application: str   # Application name associated with the APM ID
    environment: str   # Environment tag (prod, staging, dev)
    raw_response: dict # Full API response for audit trail
    error: str         # Empty on success

    def to_dict(self) -> dict:
        return asdict(self)


async def fetch_apm_id(
    endpoint_url: str,
    token: str,
    auth_method: str = "oauth2",
    apm_path: str = "/api/v1/apm/current",
) -> dict:
    """
    Fetch the APM ID from the authenticated service endpoint.

    This is Step 2 of the deterministic validation pipeline.
    Uses the access token from Step 1 to call the APM endpoint
    and retrieve the current APM identifier.

    Args:
        endpoint_url: Base URL of the APM service.
        token: Access token from authentication step.
        auth_method: How to send the token (oauth2/bearer_token = Authorization header, api_key = X-API-Key header).
        apm_path: API path to fetch APM ID.

    Returns:
        APMFetchResult as dictionary with apm_id and metadata.
    """
    logger.info("Step 2: Fetching APM ID from endpoint")

    try:
        full_url = f"{endpoint_url.rstrip('/')}{apm_path}"

        # ── Build headers based on auth method ───────────────────────
        if auth_method in ("oauth2", "bearer_token"):
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
        elif auth_method == "api_key":
            headers = {
                "X-API-Key": token,
                "Accept": "application/json",
            }
        else:
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }

        # ── Make the API call ────────────────────────────────────────
        async with httpx.AsyncClient(timeout=30.0, verify=True) as client:
            response = await client.get(full_url, headers=headers)
            response.raise_for_status()

        data = response.json()

        # ── Extract APM ID from response ─────────────────────────────
        # Supports multiple response formats:
        #   { "apm_id": "..." }
        #   { "data": { "apm_id": "..." } }
        #   { "result": { "id": "..." } }
        apm_id = (
            data.get("apm_id")
            or data.get("data", {}).get("apm_id")
            or data.get("result", {}).get("id")
            or data.get("id")
        )

        if not apm_id:
            logger.error("Step 2 FAILED: APM ID not found in response")
            return APMFetchResult(
                status="APM_FETCH_FAILED",
                apm_id="",
                application="",
                environment="",
                raw_response=data,
                error=f"APM ID not found in response. Available keys: {list(data.keys())}",
            ).to_dict()

        application = (
            data.get("application")
            or data.get("data", {}).get("application")
            or data.get("app_name", "")
        )

        environment = (
            data.get("environment")
            or data.get("data", {}).get("environment")
            or data.get("env", "")
        )

        logger.info(f"Step 2 COMPLETE: APM ID retrieved — {apm_id}")

        return APMFetchResult(
            status="SUCCESS",
            apm_id=str(apm_id).strip(),
            application=application,
            environment=environment,
            raw_response=data,
            error="",
        ).to_dict()

    except httpx.HTTPStatusError as e:
        error_msg = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        logger.error(f"Step 2 FAILED: {error_msg}")
        return APMFetchResult(
            status="APM_FETCH_FAILED",
            apm_id="",
            application="",
            environment="",
            raw_response={},
            error=error_msg,
        ).to_dict()

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        logger.error(f"Step 2 FAILED: {error_msg}")
        return APMFetchResult(
            status="APM_FETCH_FAILED",
            apm_id="",
            application="",
            environment="",
            raw_response={},
            error=error_msg,
        ).to_dict()
