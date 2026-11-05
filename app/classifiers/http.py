from __future__ import annotations

import json
import os
from typing import Protocol
from urllib.error import URLError
from urllib.request import Request, urlopen

from .base import Classification
from .deterministic import DeterministicClassifier

ALLOWED_TYPES = {
    "spam",
    "scam",
    "piracy",
    "impersonation",
    "harassment",
    "csam",
    "copyright",
    "terrorism",
    "other",
}


class _Fallback(Protocol):
    def classify(self, text: str) -> Classification: ...


class HTTPClassifier:
    def __init__(
        self,
        url: str,
        token: str = "",
        timeout: int = 5,
        fallback: _Fallback | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.fallback = fallback or DeterministicClassifier()
        self.model_version = "http-classifier"

    def classify(self, text: str) -> Classification:
        request = Request(
            self.url,
            data=json.dumps({"text": text}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        if self.token:
            request.add_header("Authorization", "Bearer " + self.token)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
            signal_type = str(payload.get("signal_type", "other"))
            if signal_type not in ALLOWED_TYPES:
                signal_type = "other"
            confidence = float(payload.get("confidence", 0.5))
            confidence = max(0.0, min(confidence, 1.0))
            model_version = str(payload.get("model_version", self.model_version))
            return Classification(signal_type, confidence, model_version)
        except (URLError, OSError, ValueError, TypeError):
            return self.fallback.classify(text)


def classifier_from_env() -> object:
    """Select the configured classifier backend.

    ``auto`` (the default) prefers ``http`` when a URL is configured, then
    ``transformers`` when a model is configured, then the bundled linear model.
    ``deterministic`` remains available as an explicit offline fallback.
    """

    backend = os.getenv("KEELTUBE_CLASSIFIER_BACKEND", "auto").strip().lower() or "auto"
    url = os.getenv("KEELTUBE_CLASSIFIER_URL", "").strip()

    if backend == "deterministic":
        return DeterministicClassifier()

    if backend == "transformer" or backend == "transformers":
        from .transformer import transformer_from_env

        model = transformer_from_env()
        if model is not None:
            return model
        return _local_default()

    if backend == "linear":
        return _local_default()

    if backend == "http" or (backend == "auto" and url):
        if not url:
            return _local_default()
        token = os.getenv("KEELTUBE_CLASSIFIER_TOKEN", "").strip()
        try:
            timeout = int(os.getenv("KEELTUBE_CLASSIFIER_TIMEOUT_SECONDS", "5") or "5")
        except ValueError:
            timeout = 5
        return HTTPClassifier(url, token, max(timeout, 1), fallback=_local_default())

    if backend == "auto":
        from .transformer import transformer_from_env

        model = transformer_from_env()
        if model is not None:
            return model
        return _local_default()

    return _local_default()


def _local_default() -> object:
    from .linear import build_default_local

    return build_default_local()
