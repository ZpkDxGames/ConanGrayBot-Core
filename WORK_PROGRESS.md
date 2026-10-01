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

Provider/runtime checkpoint: pooled per-event-loop HTTP clients with shutdown cleanup; bounded AI history/text/context and total request budget; task-local saved models; opt-in discovery; circuit telemetry; confidential provider-error filtering; REST staff authorization before gateway startup. Core: 117 tests plus six subtests, Ruff and mypy pass. Release remains gated.

Coordinated contract checkpoint: all management success/error responses exported; strict memory-clear payload; bounded records pagination/filtering and signed private previews; isolated provider sandbox; corrected Talkin activation enum. Page regenerated types/field constraints, enum/object-array forms, per-game field ownership, pagination/previews, saved-provider test and action refresh. Current source: 122 Core tests plus six subtests, Ruff/mypy pass; Page unit/lint/type/build pass. These are current-source results; browser checks and full coverage/CI/runtime certification still required.

Architecture checkpoint: replaced the 4300-line bot implementation with dedicated gateway/client, presence, AI, weather, game/media event handlers, command registry, utilities, games, admin, responses and media delivery modules. Public import entry points remain available. SDK ports are explicit, settings/store ports typed, all original routing/presentation regressions preserved. Ruff and mypy pass across 47 modules; 122 tests plus six subtests pass after tests patch their owning dependencies.

CI/artifact checkpoint: rebuilt HTTPS integration fixture; Page six E2E scenarios pass against the current real Core API. Strict Python 3.11/3.12 CI, dependency audits, whole-history redacted secret scans, contract drift checks, reproducible source ZIP/manifest/SHA256 checks, and stable certification gate are added. Production dependency audits report no known vulnerabilities. Overall backend coverage remains below 80%; the CI coverage gate intentionally blocks release until meaningful SDK/command/presentation coverage is completed. Historical main-branch credential cleanup also remains an external security gate.
