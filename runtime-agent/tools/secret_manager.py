"""
Secret Manager helper — retrieves secrets at runtime.
Caches secrets in memory for the duration of the agent session.
"""

import logging
from functools import lru_cache
from google.cloud import secretmanager

logger = logging.getLogger(__name__)

_client = None


def _get_client() -> secretmanager.SecretManagerServiceClient:
    global _client
    if _client is None:
        _client = secretmanager.SecretManagerServiceClient()
    return _client


@lru_cache(maxsize=32)
def _fetch_secret_sync(project_id: str, secret_name: str, version: str = "latest") -> str:
    """Synchronous secret fetch with in-memory caching."""
    client = _get_client()
    name = f"projects/{project_id}/secrets/{secret_name}/versions/{version}"

    try:
        response = client.access_secret_version(name=name)
        payload = response.payload.data.decode("utf-8").strip()
        logger.info(f"Secret retrieved: {secret_name} (version: {version})")
        return payload
    except Exception as e:
        logger.error(f"Failed to retrieve secret '{secret_name}': {e}")
        raise


async def get_secret(project_id: str, secret_name: str, version: str = "latest") -> str:
    """
    Retrieve a secret from Google Cloud Secret Manager.

    Args:
        project_id: GCP project ID.
        secret_name: Name of the secret.
        version: Secret version (default: latest).

    Returns:
        Secret value as string.
    """
    return _fetch_secret_sync(project_id, secret_name, version)


def clear_cache():
    """Clear the secret cache — call on secret rotation."""
    _fetch_secret_sync.cache_clear()
    logger.info("Secret cache cleared")
