from __future__ import annotations

import json
import math
import os
import re
from hashlib import sha256
from pathlib import Path

from .base import Classification
from .deterministic import DeterministicClassifier

DATA_PATH = Path(__file__).resolve().parent / "data" / "training.jsonl"
TOKEN_PATTERN = re.compile(r"[a-z0-9']+")
DEFAULT_THRESHOLD = 0.35
DEFAULT_ALPHA = 1.0


class ClassifierUnavailable(RuntimeError):
    """Raised when the bundled model cannot be loaded or trained."""


def tokenize(text: str) -> list[str]:
    """Tokenize text into unigrams and bigrams used by the classifier."""

    words = TOKEN_PATTERN.findall(text.lower())
    tokens = list(words)
    tokens.extend(f"{left}_{right}" for left, right in zip(words, words[1:]))
    return tokens


class LinearClassifier:
    """Multinomial Naive Bayes classifier trained from the bundled corpus.

    This is a real trained model rather than a keyword table: it learns token
    weights and class priors from labelled examples. The corpus is deliberately
    small and visible so maintainers can audit every training example. It still
    emits signals, never decisions.
    """

    def __init__(
        self,
        data_path: str | Path = DATA_PATH,
        *,
        alpha: float = DEFAULT_ALPHA,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> None:
        path = Path(data_path)
        self.data_path = path
        self.alpha = max(alpha, 1e-9)
        self.threshold = max(0.0, min(threshold, 1.0))
        self.class_counts: dict[str, int] = {}
        self.token_counts: dict[str, dict[str, int]] = {}
        self.class_tokens: dict[str, int] = {}
        self.vocabulary: set[str] = set()
        self.model_version = "linear-nb-v1"
        self._train(path)

    def _train(self, path: Path) -> None:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ClassifierUnavailable(f"cannot read classifier corpus: {error}") from error

        examples: list[tuple[str, str]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as error:
                raise ClassifierUnavailable(f"invalid classifier corpus line: {error}") from error
            text = str(item.get("text", ""))
            label = str(item.get("label", "")).strip()
            if not text or not label:
                continue
            examples.append((text, label))

        if not examples:
            raise ClassifierUnavailable("classifier corpus is empty")

        for text, label in examples:
            self.class_counts[label] = self.class_counts.get(label, 0) + 1
            counts = self.token_counts.setdefault(label, {})
            for token in tokenize(text):
                counts[token] = counts.get(token, 0) + 1
                self.class_tokens[label] = self.class_tokens.get(label, 0) + 1
                self.vocabulary.add(token)

        digest = sha256(raw.encode("utf-8")).hexdigest()[:12]
        self.model_version = f"linear-nb-v1+{digest}"

    @property
    def labels(self) -> list[str]:
        return sorted(self.class_counts)

    def _log_prior(self, label: str) -> float:
        total = sum(self.class_counts.values())
        return math.log(self.class_counts[label] / total)

    def _log_likelihood(self, label: str, token: str) -> float:
        alpha = self.alpha
        vocabulary_size = max(len(self.vocabulary), 1)
        numerator = self.token_counts.get(label, {}).get(token, 0) + alpha
        denominator = self.class_tokens.get(label, 0) + alpha * vocabulary_size
        return math.log(numerator / denominator)

    def scores(self, text: str) -> dict[str, float]:
        tokens = tokenize(text)
        log_scores = {
            label: self._log_prior(label)
            + sum(self._log_likelihood(label, token) for token in tokens)
            for label in self.class_counts
        }
        highest = max(log_scores.values())
        exponentials = {label: math.exp(value - highest) for label, value in log_scores.items()}
        total = sum(exponentials.values()) or 1.0
        return {label: value / total for label, value in exponentials.items()}

    def classify(self, text: str) -> Classification:
        if not text.strip():
            return Classification("other", 0.3, self.model_version)
        probabilities = self.scores(text)
        label, confidence = max(probabilities.items(), key=lambda item: item[1])
        if label == "other" or confidence < self.threshold:
            return Classification("other", max(0.0, min(confidence, 1.0)), self.model_version)
        return Classification(label, max(0.0, min(confidence, 1.0)), self.model_version)


_DEFAULT: LinearClassifier | None = None


def build_default_local() -> LinearClassifier | DeterministicClassifier:
    """Load the bundled linear model, falling back to the keyword classifier."""

    global _DEFAULT
    if _DEFAULT is None:
        try:
            threshold = float(
                os.getenv("KEELTUBE_CLASSIFIER_MIN_CONFIDENCE", str(DEFAULT_THRESHOLD))
            )
        except ValueError:
            threshold = DEFAULT_THRESHOLD
        try:
            _DEFAULT = LinearClassifier(threshold=threshold)
        except ClassifierUnavailable:
            return DeterministicClassifier()
    return _DEFAULT
