from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Classification:
    signal_type: str
    confidence: float
    model_version: str


class Classifier(Protocol):
    def classify(self, text: str) -> Classification: ...
