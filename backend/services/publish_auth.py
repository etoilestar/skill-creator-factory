"""Publish authentication and rate limiting service.

Provides token verification, simple in-memory rate limiting,
and request logging for the published OpenAI-compatible endpoints.
"""

import logging
import time
from collections import defaultdict

from ..config import settings
from .publish_config import get_config_by_api_key, load_publish_configs

logger = logging.getLogger(__name__)

# In-memory rate limit tracking: {endpoint_id: [(timestamp, ...)] }
_request_log: dict[str, list[float]] = defaultdict(list)


def verify_publish_token(token: str) -> dict | None:
    """Verify a ****** against publish configs.

    Returns the matching PublishConfig dict if valid and active, else None.
    """
    if not token:
        return None

    config = get_config_by_api_key(token)
    if not config:
        return None

    if not config.get("is_active", False):
        return None

    return config


def check_rate_limit(endpoint_id: str) -> bool:
    """Check if the endpoint has exceeded rate limits.

    Returns True if the request is allowed, False if rate-limited.
    Uses a simple sliding window of 60 seconds.
    """
    now = time.time()
    window = 60.0  # 1 minute window
    max_requests = settings.publish_rate_limit

    # Clean old entries
    _request_log[endpoint_id] = [
        ts for ts in _request_log[endpoint_id]
        if now - ts < window
    ]

    if len(_request_log[endpoint_id]) >= max_requests:
        return False

    _request_log[endpoint_id].append(now)
    return True


def log_request(endpoint_id: str, model: str, *, success: bool = True) -> None:
    """Log a request for auditing purposes."""
    logger.info(
        "[Publish] request endpoint_id=%s model=%s success=%s",
        endpoint_id,
        model,
        success,
    )


def get_active_published_models() -> list[dict]:
    """Return all currently active published model configs."""
    configs = load_publish_configs()
    return [c for c in configs if c.get("is_active", False)]
