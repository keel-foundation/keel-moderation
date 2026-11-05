from __future__ import annotations

import json
import os
from typing import Any

from .base import Classification
from .linear import build_default_local

DEFAULT_LABEL_MAP = {
    "toxic": "harassment",
    "severe_toxic": "harassment",
    "obscene": "harassment",
    "threat": "harassment",
    "insult": "harassment",
    "identity_hate": "harassment",
    "hate": "harassment",
    "abusive": "harassment",
    "harassment": "harassment",
    "spam": "spam",
    "promotional": "spam",
    "scam": "scam",
    "fraud": "scam",
    "impersonation": "impersonation",
    "copyright": "copyright",
    "piracy": "piracy",
    "terrorism": "terrorism",
    "extremist": "terrorism",
    "csam": "csam",
    "child_abuse": "csam",
    "neutral": "other",
    "non-toxic": "other",
    "non_toxic": "other",
}


class TransformersClassifier:
    """Optional Hugging Face text-classification adapter.

    Install the ``model`` extra and set ``KEELTUBE_CLASSIFIER_BACKEND=transformers``
    plus ``KEELTUBE_CLASSIFIER_MODEL`` (for example ``unitary/toxic-bert``). Label
    names are mapped onto the moderation signal vocabulary with
    ``KEELTUBE_CLASSIFIER_LABEL_MAP`` (a JSON object). Model load or inference
    failures fall back to the bundled linear model so ingestion never fails open
    into a decision.
    """

    def __init__(
        self,
        model_name: str,
        *,
        label_map: dict[str, str] | None = None,
        device: int = -1,
        threshold: float = 0.5,
    ) -> None:
        from transformers import pipeline

        self.model_name = model_name
        self.label_map = {**DEFAULT_LABEL_MAP, **(label_map or {})}
        self.threshold = max(0.0, min(threshold, 1.0))
        self.model_version = f"transformers:{model_name}"
        self.fallback = build_default_local()
        self._pipeline: Any = pipeline(
            "text-classification",
            model=model_name,
            tokenizer=model_name,
            device=device,
            top_k=None,
            truncation=True,
        )

    def _normalize(self, output: Any) -> list[dict[str, Any]]:
        if isinstance(output, dict):
            return [output]
        if isinstance(output, list):
            if output and isinstance(output[0], list):
                return list(output[0])
            return [item for item in output if isinstance(item, dict)]
        return []

    def classify(self, text: str) -> Classification:
        try:
            raw = self._pipeline(text)
        except Exception:
            return self.fallback.classify(text)

        scores: dict[str, float] = {}
        for item in self._normalize(raw):
            label = str(item.get("label", "")).strip().lower()
            signal_type = self.label_map.get(label)
            if not signal_type:
                continue
            score = float(item.get("score", 0.0))
            scores[signal_type] = scores.get(signal_type, 0.0) + max(0.0, min(score, 1.0))

        if not scores:
            return self.fallback.classify(text)
        signal_type, confidence = max(scores.items(), key=lambda item: item[1])
        confidence = max(0.0, min(confidence, 1.0))
        if signal_type == "other" or confidence < self.threshold:
            return Classification("other", confidence, self.model_version)
        return Classification(signal_type, confidence, self.model_version)


def transformer_from_env() -> TransformersClassifier | None:
    model_name = os.getenv("KEELTUBE_CLASSIFIER_MODEL", "").strip()
    if not model_name:
        return None
    label_map: dict[str, str] = {}
    raw_map = os.getenv("KEELTUBE_CLASSIFIER_LABEL_MAP", "").strip()
    if raw_map:
        try:
            parsed = json.loads(raw_map)
            if isinstance(parsed, dict):
                label_map = {str(key).lower(): str(value) for key, value in parsed.items()}
        except json.JSONDecodeError:
            label_map = {}
    try:
        threshold = float(os.getenv("KEELTUBE_CLASSIFIER_MIN_CONFIDENCE", "0.5"))
    except ValueError:
        threshold = 0.5
    try:
        device = int(os.getenv("KEELTUBE_CLASSIFIER_DEVICE", "-1"))
    except ValueError:
        device = -1
    try:
        return TransformersClassifier(
            model_name,
            label_map=label_map,
            device=device,
            threshold=threshold,
        )
    except Exception:
        return None
