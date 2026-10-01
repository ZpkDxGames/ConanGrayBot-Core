# v2 implementation ledger

The previous runtime and its unuploaded implementation were lost. The GitHub recovery report preserves historical test evidence only. The implementation is being reconstructed from the authoritative repositories and supplied directive.

## Reconstructed and verified

- Dynamic confidential settings and fail-closed production storage.
- Independent service/media secrets; body-bound actor HMAC, replay rejection, actor rate limits, guild restriction and live Discord staff-role verification.
- Modular management API, bounded request bodies, redacted errors, request IDs and mutation audit.
- Complete schema 4 configuration model; pure migrations and revision compare-and-swap in memory and Firestore transactions.
- Expiring media signatures, safe MIME allowlist and range syntax validation.
- Bounded transient registries and blocking SDK concurrency.
- Updated pinned, hash-locked runtime dependencies.
- 82 tests plus 6 subtests pass on Python 3.12. Critical modules: config 92.31%, signing 92.59%, migrations 100%, models 100%, authentication 95.65%.

## Outstanding source work

Discord decomposition, pooled AI transport/circuit management, storage expiry and pagination, duplicate-safe media pipeline, complete typed response contracts, authenticated Next.js Page, broader tests, Python 3.11 validation, strict CI, deployment documentation and release artifacts still need reconstruction and verification. Current overall backend coverage is 43.5%; the required 80% gate remains unmet.

## External stable-release blockers

Verify deletion of the compromised Google key and provision replacement credentials; remove historical sensitive Git objects; restore authorized Vercel project access, Discord OAuth credentials/callback registration and Discloud runtime access. No stable release or runtime certification is claimed.

2026-10-01 recovered checkpoint: rebuilt deterministic song-judging safeguards, serialized tic-tac-toe moves, bounded bot state, successful-only persisted command manifests with unchanged startup skip and forced manual sync, per-guild session retention deadlines/read expiry and native Firestore log TTL, deployment defaults, and fail-closed replay-registry capacity. Current source passes 105 tests plus six subtests and mypy (26 modules). This supersedes earlier local results for these changes only; overall coverage and release gates remain incomplete. Missing unuploaded work is listed in EXECUTION_RECOVERY.md.
