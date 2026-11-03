"""Throttled HTTP GET with retries, backoff and redacted logging.

Central helper used by the matcher (and later the importer) so that
rate limits (HTTP 429) and transient 5xx errors are retried with
backoff instead of crashing a long migration run.
"""
from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)

# Timestamp (monotonic) of the last request issued via throttled_get.
# Used to enforce a polite delay between calls. Tests may poke this.
_last_request_ts: float = 0.0

# Query-param keys whose values must never appear in logs.
_SENSITIVE_SUBSTRINGS = ("token", "auth", "secret", "cookie", "key", "password", "session")


def _redacted_params(params: dict | None) -> dict:
    safe: dict = {}
    for k, v in (params or {}).items():
        kl = str(k).lower()
        if any(s in kl for s in _SENSITIVE_SUBSTRINGS):
            safe[k] = "***"
        else:
            safe[k] = v
    return safe


def throttled_get(url: str, params: dict | None = None, delay: float = 3.0, retries: int = 3, **kwargs) -> requests.Response:
    """GET *url* with a polite delay, retries and redacted logging.

    - Enforces at least *delay* seconds since the previous request.
    - Retries HTTP 429 and 5xx plus connection-level errors (with
      exponential backoff honouring ``Retry-After`` when present).
    - Other 4xx errors are raised immediately (no retry).
    - Logs URL + redacted params; secret values never hit the log.
    """
    global _last_request_ts

    params = params or {}
    # Polite throttle: ensure `delay` seconds since the previous call.
    now = time.monotonic()
    elapsed = now - _last_request_ts
    if elapsed < delay:
        time.sleep(delay - elapsed)

    attempt = 0
    while True:
        attempt += 1
        logger.info("GET %s params=%s (attempt %d)", url, _redacted_params(params), attempt)
        try:
            resp = requests.get(url, params=params, timeout=kwargs.pop("timeout", 15), **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt >= retries:
                logger.warning("GET %s failed after %d attempts: %s", url, attempt, type(exc).__name__)
                raise
            backoff = min(30.0, 1.0 * (2 ** (attempt - 1)))
            logger.warning("GET %s %s, backing off %.1fs", url, type(exc).__name__, backoff)
            time.sleep(backoff)
            continue

        status = resp.status_code
        if status == 429 or 500 <= status < 600:
            if attempt >= retries:
                # Exhausted: surface as HTTPError with redacted context.
                try:
                    resp.raise_for_status()
                except requests.HTTPError:
                    raise
                raise requests.HTTPError(f"HTTP {status} for {url}")
            retry_after = None
            try:
                retry_after = float(resp.headers.get("Retry-After", ""))
            except (ValueError, TypeError, AttributeError):
                retry_after = None
            backoff = retry_after if retry_after is not None else min(30.0, 1.0 * (2 ** (attempt - 1)))
            logger.warning("GET %s -> HTTP %s, backing off %.1fs", url, status, backoff)
            time.sleep(backoff)
            continue

        if status >= 400:
            # Non-retryable client error: raise without retry.
            try:
                resp.raise_for_status()
            except requests.HTTPError:
                raise
            raise requests.HTTPError(f"HTTP {status} for {url}")

        _last_request_ts = time.monotonic()
        return resp
