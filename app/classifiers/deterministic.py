from __future__ import annotations

from .base import Classification

KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("csam", ("csam", "child sexual", "minor sexual")),
    ("terrorism", ("terrorism", "terrorist", "extremist", "bomb")),
    ("copyright", ("copyright", "piracy", "unlicensed", "stolen footage", "dmca")),
    ("harassment", ("harassment", "threat", "doxx", "abuse", "targeted")),
    ("impersonation", ("impersonation", "impersonating", "fake account")),
    ("scam", ("scam", "fraud", "phishing", "stolen money")),
    ("spam", ("spam", "mass produced", "reposted", "bot network")),
)


class DeterministicClassifier:
    model_version = "deterministic-keywords-v1"

    def classify(self, text: str) -> Classification:
        lowered = text.lower()
        for signal_type, terms in KEYWORDS:
            matches = sum(1 for term in terms if term in lowered)
            if matches:
                confidence = min(0.55 + 0.15 * matches, 0.95)
                return Classification(signal_type, confidence, self.model_version)
        return Classification("other", 0.3, self.model_version)
