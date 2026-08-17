"""
Step 1 — Authenticate against the configured APM endpoint.
Retrieves OAuth2 token or validates API key, returns authenticated session.
"""

import httpx
import logging
from enum import Enum
from dataclasses import dataclass, asdict
from tools.secret_manager import get_secret

logger = logging.getLogger(__name__)


class AuthMethod(str, Enum):
    OAUTH2 = "oauth2"
    API_KEY = "api_key"
    BEARER_TOKEN = "bearer_token"


@dataclass
class AuthResult:
    status: str           # SUCCESS | AUTH_FAILED
    method: str           # oauth2 | api_key | bearer_token
    token: str            # Access token (masked in logs)
    endpoint_url: str     # Authenticated endpoint base URL
    expires_in: int       # Token TTL in seconds (0 if non-expiring)
    error: str            # Empty on success, error message on failure

    def to_dict(self) -> dict:
        d = asdict(self)
        # Mask token in output — never expose full token
        if self.token:
            d["token"] = f"{self.token[:8]}...{self.token[-4:]}" if len(self.token) > 12 else "***"
        return d


async def authenticate_endpoint(
    project_id: str,
    auth_method: str = "oauth2",
    endpoint_secret_name: str = "apm-endpoint-url",
    client_id_secret_name: str = "apm-client-id",
    client_secret_name: str = "apm-client-secret",
) -> dict:
    """
    Authenticate against the APM service endpoint.

    This is Step 1 of the deterministic validation pipeline.
    Retrieves credentials from Secret Manager, authenticates
    against the endpoint, and returns an access token.

    Args:
        project_id: GCP project ID for Secret Manager.
        auth_method: Authentication method (oauth2, api_key, bearer_token).
        endpoint_secret_name: Secret Manager name for endpoint URL.
        client_id_secret_name: Secret Manager name for client ID.
        client_secret_name: Secret Manager name for client secret.

    Returns:
        AuthResult as dictionary with status, token, and endpoint URL.
    """
    logger.info("Step 1: Authenticating against APM endpoint")

    try:
        # ── Retrieve secrets ─────────────────────────────────────────
        endpoint_url = await get_secret(project_id, endpoint_secret_name)
        client_id = await get_secret(project_id, client_id_secret_name)
        client_secret = await get_secret(project_id, client_secret_name)

        if not all([endpoint_url, client_id, client_secret]):
            return AuthResult(
                status="AUTH_FAILED",
                method=auth_method,
                token="",
                endpoint_url=endpoint_url or "",
                expires_in=0,
                error="Missing secrets: one or more credentials not found in Secret Manager",
            ).to_dict()

        logger.info(f"Secrets retrieved. Endpoint: {endpoint_url}")

        # ── Authenticate based on method ─────────────────────────────
        method = AuthMethod(auth_method)

        async with httpx.AsyncClient(timeout=30.0, verify=True) as client:

            if method == AuthMethod.OAUTH2:
                token, expires_in = await _oauth2_authenticate(
                    client, endpoint_url, client_id, client_secret
                )

            elif method == AuthMethod.API_KEY:
                token, expires_in = await _api_key_authenticate(
                    client, endpoint_url, client_id, client_secret
                )

            elif method == AuthMethod.BEARER_TOKEN:
                # Bearer token is the client_secret itself
                token = client_secret
                expires_in = 0
                # Validate token with a health check
                await _validate_bearer(client, endpoint_url, token)

            else:
                return AuthResult(
                    status="AUTH_FAILED",
                    method=auth_method,
                    token="",
                    endpoint_url=endpoint_url,
                    expires_in=0,
                    error=f"Unsupported auth method: {auth_method}",
                ).to_dict()

        logger.info("Step 1 COMPLETE: Authentication successful")

        return AuthResult(
            status="SUCCESS",
            method=auth_method,
            token=token,
            endpoint_url=endpoint_url,
            expires_in=expires_in,
            error="",
        ).to_dict()

    except httpx.HTTPStatusError as e:
        error_msg = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        logger.error(f"Step 1 FAILED: {error_msg}")
        return AuthResult(
            status="AUTH_FAILED",
            method=auth_method,
            token="",
            endpoint_url=endpoint_url if 'endpoint_url' in dir() else "",
            expires_in=0,
            error=error_msg,
        ).to_dict()

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        logger.error(f"Step 1 FAILED: {error_msg}")
        return AuthResult(
            status="AUTH_FAILED",
            method=auth_method,
            token="",
            endpoint_url="",
            expires_in=0,
            error=error_msg,
        ).to_dict()


async def _oauth2_authenticate(
    client: httpx.AsyncClient, endpoint_url: str, client_id: str, client_secret: str
) -> tuple[str, int]:
    """Perform OAuth2 client_credentials flow."""
    token_url = f"{endpoint_url.rstrip('/')}/oauth/token"

    response = await client.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    response.raise_for_status()

    data = response.json()
    return data["access_token"], data.get("expires_in", 3600)


async def _api_key_authenticate(
    client: httpx.AsyncClient, endpoint_url: str, client_id: str, api_key: str
) -> tuple[str, int]:
    """Validate API key by calling a health/auth endpoint."""
    auth_url = f"{endpoint_url.rstrip('/')}/auth/validate"

    response = await client.get(
        auth_url,
        headers={
            "X-API-Key": api_key,
            "X-Client-ID": client_id,
        },
    )
    response.raise_for_status()
    return api_key, 0  # API keys don't expire


async def _validate_bearer(
    client: httpx.AsyncClient, endpoint_url: str, token: str
) -> None:
    """Validate bearer token with a lightweight health check."""
    health_url = f"{endpoint_url.rstrip('/')}/healthz"

    response = await client.get(
        health_url,
        headers={"Authorization": f"Bearer {token}"},
    )
    response.raise_for_status()
