"""
auth_registry.py — Canonical Auth Header Registry for Nexus

Single source of truth for all service-to-service authentication headers.
Every service calls validate_service_auth_config(SERVICE_NAME) at startup.
"""
import os
from typing import Dict, Any

class AuthConfigError(RuntimeError):
    """Raised when a service's auth configuration is invalid at startup."""
    pass

# Registry: service_name → list of required auth configs
# Each entry: {header, env_var, min_len, role, direction}
AUTH_REGISTRY: Dict[str, list] = {
    "alpha-buffer": [
        {"header": "X-Nexus-Secret", "env_var": "NEXUS_SECRET", "min_len": 32,
         "role": "inbound_from_agents", "direction": "inbound"},
    ],
    "prime-buffer": [
        {"header": "X-Nexus-Prime-Secret", "env_var": "NEXUS_PRIME_SECRET", "min_len": 32,
         "role": "inbound_from_agents", "direction": "inbound"},
    ],
    "omni": [
        {"header": "X-Nexus-Secret", "env_var": "NEXUS_SECRET", "min_len": 32,
         "role": "inbound_from_alpha_buffer", "direction": "inbound"},
        {"header": "X-Nexus-Prime-Secret", "env_var": "NEXUS_PRIME_SECRET", "min_len": 32,
         "role": "inbound_from_prime_buffer", "direction": "inbound"},
        {"header": "X-Nexus-Secret", "env_var": "NEXUS_SECRET", "min_len": 32,
         "role": "outbound_to_alpha_execution", "direction": "outbound"},
        {"header": "X-Nexus-Prime-Secret", "env_var": "NEXUS_PRIME_SECRET", "min_len": 32,
         "role": "outbound_to_prime_execution", "direction": "outbound"},
    ],
    "alpha-execution": [
        {"header": "X-Nexus-Secret", "env_var": "NEXUS_SECRET", "min_len": 32,
         "role": "inbound_from_omni", "direction": "inbound"},
    ],
    "prime-execution": [
        {"header": "X-Nexus-Prime-Secret", "env_var": "NEXUS_PRIME_SECRET", "min_len": 32,
         "role": "inbound_from_omni", "direction": "inbound"},
    ],
    "atlas": [
        {"header": "X-Nexus-Secret", "env_var": "NEXUS_SECRET", "min_len": 32,
         "role": "outbound_to_alpha_buffer", "direction": "outbound"},
        {"header": "X-Nexus-Prime-Secret", "env_var": "NEXUS_PRIME_SECRET", "min_len": 32,
         "role": "outbound_to_prime_buffer", "direction": "outbound"},
    ],
    "sage": [
        {"header": "X-Nexus-Secret", "env_var": "NEXUS_SECRET", "min_len": 32,
         "role": "outbound_to_alpha_buffer", "direction": "outbound"},
        {"header": "X-Nexus-Prime-Secret", "env_var": "NEXUS_PRIME_SECRET", "min_len": 32,
         "role": "outbound_to_prime_buffer", "direction": "outbound"},
    ],
    "cipher": [
        {"header": "X-Nexus-Secret", "env_var": "NEXUS_SECRET", "min_len": 32,
         "role": "outbound_to_alpha_buffer", "direction": "outbound"},
        {"header": "X-Nexus-Prime-Secret", "env_var": "NEXUS_PRIME_SECRET", "min_len": 32,
         "role": "outbound_to_prime_buffer", "direction": "outbound"},
    ],
    "axiom": [
        {"header": "X-Axiom-Secret", "env_var": "AXIOM_SECRET", "min_len": 16,
         "role": "outbound_to_agents", "direction": "outbound"},
    ],
    "oracle": [
        {"header": "X-Oracle-Secret", "env_var": "ORACLE_SECRET", "min_len": 32,
         "role": "inbound_from_any", "direction": "inbound"},
    ],
    "ails": [
        {"header": "X-AILS-Secret", "env_var": "AILS_SECRET", "min_len": 32,
         "role": "inbound_from_any", "direction": "inbound"},
    ],
    "guardian-angel": [
        # Guardian Angel calls /health endpoints — no auth required (public endpoints)
        # Documented here for completeness
    ],
}


def validate_service_auth_config(service_name: str) -> None:
    """
    Validate that all required auth env vars for a service are present and meet minimum length.

    Call this in each service's lifespan startup block before accepting traffic.

    Args:
        service_name: Service identifier matching AUTH_REGISTRY key.

    Raises:
        AuthConfigError: If any required auth env var is missing or too short.
        KeyError: If service_name is not in AUTH_REGISTRY.
    """
    if service_name not in AUTH_REGISTRY:
        raise KeyError(
            f"Service '{service_name}' not in AUTH_REGISTRY. "
            f"Add it to shared/auth_registry.py before startup."
        )

    entries = AUTH_REGISTRY[service_name]
    errors = []

    for entry in entries:
        env_var = entry["env_var"]
        min_len = entry["min_len"]
        role = entry["role"]
        value = os.getenv(env_var, "")

        if not value:
            errors.append(
                f"Missing env var '{env_var}' (required for {role}). "
                f"Header: {entry['header']}"
            )
        elif len(value) < min_len:
            errors.append(
                f"Env var '{env_var}' is too short (len={len(value)}, min={min_len}). "
                f"Required for {role}."
            )

    if errors:
        raise AuthConfigError(
            f"Auth config validation failed for service '{service_name}':\n" +
            "\n".join(f"  • {e}" for e in errors)
        )


def get_registry_summary() -> Dict[str, Any]:
    """Return a summary of all registered services and their auth requirements."""
    return {
        service: [
            {"env_var": e["env_var"], "role": e["role"], "direction": e["direction"]}
            for e in entries
        ]
        for service, entries in AUTH_REGISTRY.items()
    }
