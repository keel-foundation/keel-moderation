# Classifiers

Classifiers emit **signals**, not decisions. Every signal must carry:

- model name and version;
- confidence and threshold;
- input reference (content hash, video id, timestamp);
- a short, human-readable explanation;
- a link to the evidence bundle.

No classifier may terminate a channel, permanently demonetize a video, or close an appeal. Those are human decisions recorded in the audit ledger.
