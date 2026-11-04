from __future__ import annotations

import json
import logging
import os
from threading import Thread
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 3


def notify_channel(
    channel_id: str,
    *,
    kind: str,
    title: str,
    body: str,
    href: str,
    dedupe_key: str,
    payload: dict[str, Any] | None = None,
) -> bool:
    """Best-effort creator notification through core's internal ingest route."""

    base_url = os.getenv("KEELTUBE_CORE_URL", "").strip().rstrip("/")
    token = (
        os.getenv("KEELTUBE_CORE_INTERNAL_TOKEN", "").strip()
        or os.getenv("KEELTUBE_INTERNAL_TOKEN", "").strip()
    )
    if not base_url or not token or not channel_id:
        return False

    request_body = {
        "channel_id": channel_id,
        "kind": kind,
        "title": title,
        "body": body,
        "href": href,
        "dedupe_key": dedupe_key,
    }
    if payload:
        request_body["payload"] = payload
    request = Request(
        base_url + "/internal/v1/notifications",
        data=json.dumps(request_body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
        },
    )
    try:
        with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except (URLError, OSError, ValueError) as error:
        logger.warning("core notification failed: %s", error)
        return False


def notify_channel_async(
    channel_id: str,
    *,
    kind: str,
    title: str,
    body: str,
    href: str,
    dedupe_key: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Dispatch a notification without blocking the moderation response."""

    thread = Thread(
        target=notify_channel,
        kwargs={
            "channel_id": channel_id,
            "kind": kind,
            "title": title,
            "body": body,
            "href": href,
            "dedupe_key": dedupe_key,
            "payload": payload,
        },
        daemon=True,
    )
    thread.start()
