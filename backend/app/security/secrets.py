"""
Secret management abstraction layer.

Application code retrieves secrets only through a SecretProvider — never
via direct os.getenv() or settings attribute access outside this module.
This makes the secret source swappable without changing any caller.

Current implementation: EnvironmentSecretProvider (reads from environment
variables loaded by pydantic-settings). No runtime dependencies beyond the
standard library.

Future providers (add without touching callers):
  - AWSSecretsManagerProvider  — fetches from AWS Secrets Manager, supports
                                  automatic rotation via version stages
  - IBMSecretsManagerProvider  — IBM Cloud Secrets Manager API
  - AzureKeyVaultProvider       — Azure Key Vault secrets

Rotation workflow
-----------------
1. Generate new secret value in your secret store.
2. Update the secret version (AWS: add AWSPENDING label; IBM: create new version).
3. During deployment, the provider fetches the latest active version automatically.
4. Revoke the old version after confirming the new one is live.
5. No application restart required when using a cloud provider.
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from functools import lru_cache

logger = logging.getLogger(__name__)


class SecretProvider(ABC):
    """Interface for retrieving named secrets."""

    @abstractmethod
    def get_secret(self, secret_name: str) -> str:
        """
        Return the current value of the named secret.
        Raises KeyError if the secret does not exist.
        """


class EnvironmentSecretProvider(SecretProvider):
    """
    Reads secrets from environment variables.

    secret_name maps directly to an environment variable name.
    Suitable for local development and CI.

    For production: replace with AWSSecretsManagerProvider and set
    CLAUDE_SECRET_NAME=anthropic-api-key (or whatever name you registered
    in your secret store) instead of placing the raw key in the environment.
    """

    def get_secret(self, secret_name: str) -> str:
        # Check os.environ first (set explicitly, e.g. in production/CI).
        value = os.environ.get(secret_name)
        if value:
            logger.debug("Secret '%s' retrieved from environment.", secret_name)
            return value

        # Fall back to pydantic-settings, which loads .env files.
        # Import lazily to avoid a circular import at module load time.
        try:
            from app.config import settings as _settings  # noqa: PLC0415
            if secret_name == _settings.claude_secret_name and _settings.claude_api_key:
                logger.debug("Secret '%s' retrieved from .env via pydantic-settings.", secret_name)
                return _settings.claude_api_key
        except Exception:
            pass

        raise KeyError(
            f"Secret '{secret_name}' not found in environment. "
            "Set the corresponding environment variable or switch to a "
            "cloud secret provider for production."
        )


@lru_cache(maxsize=1)
def get_secret_provider() -> SecretProvider:
    """
    Return the active SecretProvider singleton.

    To swap providers (e.g. in tests or for a different deployment target),
    call get_secret_provider.cache_clear() and override the factory here.
    """
    return EnvironmentSecretProvider()
