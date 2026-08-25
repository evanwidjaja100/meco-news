# Security policy

This service processes hostile publisher metadata and sends links to a Telegram chat. Security-sensitive boundaries are URL validation, redirect/DNS policy, bounded response/parser work, HTML rendering, SQLite state transitions, secret handling, and container/scheduler permissions.

Report suspected vulnerabilities privately to the repository owner. Do not include live Telegram tokens, private URLs, raw response bodies, or production databases in an issue. Rotate any credential that may have been exposed before investigating.

The application rejects URL userinfo, control characters, invalid ports, non-HTTPS production URLs, private/link-local address literals, unsafe redirects, DTD/entity declarations, oversized bodies/fields, and malformed items. Rejected item URLs are not persisted or logged. Telegram acceptance-unknown failures become `ambiguous` and require audited reconciliation.

Supported deployment assumptions are local NTFS/POSIX SQLite storage, runtime-only secrets, a non-root container identity, and deployment-level egress controls. Application DNS checks do not replace host/container egress policy.

