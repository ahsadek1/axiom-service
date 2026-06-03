"""
api_key_validator.py — External API Key Boot Validator

Validates that external API keys are functional (not just present) at service startup.
Critical APIs block startup on failure. Non-critical APIs allow degraded-start with WARN.
"""
import logging
import time
from dataclasses import dataclass
from typing import Literal, Optional

import requests

logger = logging.getLogger("nexus.api_key_validator")

PROBE_TIMEOUT_SEC = 8
MAX_PROBE_RETRIES = 3
RETRY_DELAY_SEC = 2.0


@dataclass
class ValidationResult:
    """Result of a single API key validation probe."""
    api_name: str
    status: Literal["ok", "degraded", "failed"]
    message: str
    latency_ms: int


class ApiKeyValidator:
    """Validates external API keys by making lightweight probe calls at startup."""

    def validate_alpaca(self, key: str, secret: str, base_url: str) -> ValidationResult:
        """
        Validate Alpaca API key by probing the /v2/account endpoint.

        Args:
            key:      Alpaca API key ID.
            secret:   Alpaca API secret key.
            base_url: Alpaca base URL (paper or live).

        Returns:
            ValidationResult with status 'ok', 'degraded', or 'failed'.
        """
        url = f"{base_url.rstrip('/')}/v2/account"
        headers = {
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
        }
        return self._probe("alpaca", url, headers=headers)

    def validate_anthropic(self, key: str) -> ValidationResult:
        """
        Validate Anthropic API key by probing the /v1/models endpoint.

        Args:
            key: Anthropic API key.

        Returns:
            ValidationResult with status 'ok', 'degraded', or 'failed'.
        """
        url = "https://api.anthropic.com/v1/models"
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        }
        return self._probe("anthropic", url, headers=headers)

    def validate_openai(self, key: str) -> ValidationResult:
        """
        Validate OpenAI API key by probing the /v1/models endpoint.

        Args:
            key: OpenAI API key.

        Returns:
            ValidationResult with status 'ok', 'degraded', or 'failed'.
        """
        url = "https://api.openai.com/v1/models"
        headers = {"Authorization": f"Bearer {key}"}
        return self._probe("openai", url, headers=headers)

    def validate_gemini(self, key: str) -> ValidationResult:
        """
        Validate Gemini API key by probing the /v1beta/models endpoint.

        Args:
            key: Gemini API key.

        Returns:
            ValidationResult with status 'ok', 'degraded', or 'failed'.
        """
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
        return self._probe("gemini", url)

    def validate_deepseek(self, key: str) -> ValidationResult:
        """
        Validate DeepSeek API key by probing the /v1/models endpoint.

        Args:
            key: DeepSeek API key.

        Returns:
            ValidationResult with status 'ok', 'degraded', or 'failed'.
        """
        url = "https://api.deepseek.com/v1/models"
        headers = {"Authorization": f"Bearer {key}"}
        return self._probe("deepseek", url, headers=headers)

    def validate_polygon(self, key: str) -> ValidationResult:
        """
        Validate Polygon.io API key by probing the market status endpoint.

        Args:
            key: Polygon.io API key.

        Returns:
            ValidationResult with status 'ok', 'degraded', or 'failed'.
        """
        url = f"https://api.polygon.io/v1/marketstatus/now?apiKey={key}"
        return self._probe("polygon", url)

    def _probe(
        self,
        api_name: str,
        url: str,
        headers: Optional[dict] = None,
    ) -> ValidationResult:
        """
        Make a lightweight probe GET request with retry and timeout handling.

        - Retry loop: MAX_PROBE_RETRIES=3, RETRY_DELAY_SEC=2s between retries
        - 8s timeout per probe attempt
        - On 429: mark as 'degraded' (rate limit = key is valid)
        - On 401/403: mark as 'failed'
        - On success (2xx): mark as 'ok' with latency

        Args:
            api_name: Human-readable API name for logging.
            url:      URL to probe.
            headers:  Optional request headers.

        Returns:
            ValidationResult with status and latency.
        """
        last_message = "No attempts made"

        for attempt in range(1, MAX_PROBE_RETRIES + 1):
            t0 = time.time()
            try:
                resp = requests.get(
                    url,
                    headers=headers or {},
                    timeout=PROBE_TIMEOUT_SEC,
                )
                latency_ms = int((time.time() - t0) * 1000)

                if resp.status_code == 429:
                    logger.warning(
                        "%s probe: 429 rate-limited (attempt %d) — key is valid, degraded start",
                        api_name, attempt,
                    )
                    return ValidationResult(
                        api_name=api_name,
                        status="degraded",
                        message=f"Rate limited (429) — key is valid but quota exceeded",
                        latency_ms=latency_ms,
                    )

                if resp.status_code in (401, 403):
                    logger.error(
                        "%s probe: HTTP %d — key is invalid or expired",
                        api_name, resp.status_code,
                    )
                    return ValidationResult(
                        api_name=api_name,
                        status="failed",
                        message=f"HTTP {resp.status_code} — API key invalid or expired",
                        latency_ms=latency_ms,
                    )

                if 200 <= resp.status_code < 300:
                    logger.info(
                        "%s probe OK: HTTP %d in %dms",
                        api_name, resp.status_code, latency_ms,
                    )
                    return ValidationResult(
                        api_name=api_name,
                        status="ok",
                        message=f"HTTP {resp.status_code} in {latency_ms}ms",
                        latency_ms=latency_ms,
                    )

                last_message = f"HTTP {resp.status_code}"
                logger.warning(
                    "%s probe: HTTP %d (attempt %d/%d)",
                    api_name, resp.status_code, attempt, MAX_PROBE_RETRIES,
                )

            except requests.exceptions.Timeout:
                latency_ms = int((time.time() - t0) * 1000)
                last_message = f"Timeout after {PROBE_TIMEOUT_SEC}s"
                logger.warning(
                    "%s probe: timeout (attempt %d/%d)",
                    api_name, attempt, MAX_PROBE_RETRIES,
                )
            except Exception as e:
                latency_ms = int((time.time() - t0) * 1000)
                last_message = str(e)[:120]
                logger.warning(
                    "%s probe: error (attempt %d/%d): %s",
                    api_name, attempt, MAX_PROBE_RETRIES, e,
                )

            if attempt < MAX_PROBE_RETRIES:
                time.sleep(RETRY_DELAY_SEC)

        logger.error("%s probe FAILED after %d attempts: %s", api_name, MAX_PROBE_RETRIES, last_message)
        return ValidationResult(
            api_name=api_name,
            status="failed",
            message=f"Failed after {MAX_PROBE_RETRIES} attempts: {last_message}",
            latency_ms=0,
        )

    def raise_on_critical_failures(
        self,
        results: list,
        critical_apis: list,
    ) -> None:
        """
        Raise RuntimeError if any critical API failed validation.

        Args:
            results:       List of ValidationResult from validation calls.
            critical_apis: List of api_name strings that are critical (must be ok/degraded).

        Raises:
            RuntimeError: If any critical API has status='failed'.
        """
        failures = [
            r for r in results
            if r.api_name in critical_apis and r.status == "failed"
        ]
        if failures:
            details = ", ".join(f"{r.api_name}: {r.message}" for r in failures)
            raise RuntimeError(
                f"Critical API key validation failed at startup: {details}. "
                "Fix the failing API keys before starting the service."
            )
