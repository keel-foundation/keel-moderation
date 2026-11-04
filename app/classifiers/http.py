from __future__ import annotations

import json
import os
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


class HTTPClassifier:
    def __init__(self, url: str, token: str = "", timeout: int = 5) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.fallback = DeterministicClassifier()
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


def classifier_from_env() -> DeterministicClassifier | HTTPClassifier:
    url = os.getenv("KEELTUBE_CLASSIFIER_URL", "").strip()
    if not url:
        return DeterministicClassifier()
    token = os.getenv("KEELTUBE_CLASSIFIER_TOKEN", "").strip()
    try:
        timeout = int(os.getenv("KEELTUBE_CLASSIFIER_TIMEOUT_SECONDS", "5") or "5")
    except ValueError:
        timeout = 5
    return HTTPClassifier(url, token, max(timeout, 1))
