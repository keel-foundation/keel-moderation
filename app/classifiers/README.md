# Classifiers

Classifiers emit **signals**, not decisions. Every signal must carry:

- model name and version;
- confidence and threshold;
- input reference (content hash, video id, timestamp);
- a short, human-readable explanation;
- a link to the evidence bundle.

No classifier may terminate a channel, permanently demonetize a video, or close an appeal. Those are human decisions recorded in the audit ledger.

## Adapters

- `DeterministicClassifier` is the default offline fallback. It maps report text to a signal type
  and confidence using a small keyword table.
- `HTTPClassifier` calls `KEELTUBE_CLASSIFIER_URL` with `{"text": "..."}` and expects
  `{"signal_type": "...", "confidence": 0.0, "model_version": "..."}`. It uses
  `KEELTUBE_CLASSIFIER_TOKEN` when configured and falls back to the deterministic classifier on any
  transport, decoding, or validation failure.

Signals created from viewer reports use the classifier result when the reporter chose `other`;
otherwise the reporter's explicit reason remains the signal type and the classifier enriches
confidence and model version.
