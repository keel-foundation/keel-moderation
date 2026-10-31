# Policy bundles

Policy is code. Each bundle is versioned, signed, and testable.

A bundle should define:

- `policy_version` — semantic version and hash.
- `rules` — reason codes, scope, severity, and penalty ranges.
- `human_review_gates` — the actions that require a human decision.
- `appeal_windows` — deadlines and service levels.
- `content_protection` — piracy, spam, scam, impersonation, and fraud rules.

Policy changes require an RFC and a public decision-log entry. Emergency changes are allowed only under the Constitution's emergency provisions.
