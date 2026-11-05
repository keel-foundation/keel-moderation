# Classifiers

Classifiers emit **signals**, not decisions. Every signal must carry:

- model name and version;
- confidence and threshold;
- input reference (content hash, video id, timestamp);
- a short, human-readable explanation;
- a link to the evidence bundle.

No classifier may terminate a channel, permanently demonetize a video, or close an appeal. Those are human decisions recorded in the audit ledger.

## Adapters

- `LinearClassifier` is the default in-process model. It is a multinomial Naive Bayes
  classifier trained from the auditable corpus in `data/training.jsonl`, with unigram and
  bigram tokens and Laplace smoothing. The model version includes a digest of the corpus
  (`linear-nb-v1+<12 hex>`), so a changed corpus cannot silently reuse an old version.
- `TransformersClassifier` is the production path for a Hugging Face text-classification
  model. Install the `model` extra (`pip install -e '.[model]'`), set
  `KEELTUBE_CLASSIFIER_BACKEND=transformers`, `KEELTUBE_CLASSIFIER_MODEL`, and optionally
  `KEELTUBE_CLASSIFIER_LABEL_MAP` (a JSON object). Model labels are mapped onto the
  moderation signal vocabulary; inference failures fall back to the bundled linear model.
- `HTTPClassifier` calls `KEELTUBE_CLASSIFIER_URL` with `{"text": "..."}` and expects
  `{"signal_type": "...", "confidence": 0.0, "model_version": "..."}`. It uses
  `KEELTUBE_CLASSIFIER_TOKEN` when configured.
- `DeterministicClassifier` is the explicit offline fallback. It maps report text to a
  signal type using a small keyword table.

`KEELTUBE_CLASSIFIER_BACKEND` selects the backend: `auto` (default), `linear`,
`transformers`, `http`, or `deterministic`. In `auto` mode an HTTP URL wins, then a
configured transformer model, then the bundled linear model. `KEELTUBE_CLASSIFIER_MIN_CONFIDENCE`
sets the minimum probability for a non-`other` signal (default `0.35` for linear and `0.5`
for transformers).

Signals created from viewer reports use the classifier result when the reporter chose `other`;
otherwise the reporter's explicit reason remains the signal type and the classifier enriches
confidence and model version.

The bundled model is a small, public, auditable baseline. It is not a substitute for a
production safety model. Deploy a reviewed transformer model (for example a toxic-content
classifier) behind the same adapter and keep human review mandatory for enforcement.
