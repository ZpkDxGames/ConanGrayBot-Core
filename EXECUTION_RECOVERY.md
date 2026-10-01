# Execution recovery checkpoint — 2026-10-01

The local execution service disconnected while preparing the next source checkpoint. New process creation fails with: "exec-server transport disconnected; failed to resume exec-server session: recovery timed out after 25s". Simple command and apply-patch recovery probes remain unavailable. GitHub remains accessible.

## Durable source checkpoints

- Core: ac9624bd04e4a98fdf4394fe69c2259af8ea4803
- Page: 19ee338bd78b752221cb309d842b488f0503388d
- Core PR: https://github.com/ZpkDxGames/ConanGrayBot-Core/pull/1
- Page PR: https://github.com/ZpkDxGames/ConanGrayBot-Page/pull/1

This document preserves status only. The later local source and its validation results are NOT present in those source commits and MUST NOT certify them for release.

## Local work awaiting recovery/upload

Workspace: /workspace/scratch/93271eca0bc3, repositories core/ and page/.

Core: Discord responsibilities split into backend/discord_bot modules; typed management response contracts; REST staff verification before gateway startup; pooled HTTP sessions, provider circuits, bounded prompt shaping and explicit model selection; native retention timestamps and read expiry; Firestore keyset pagination; archive idempotence/size limits/compensating cleanup; deterministic partial-guess rejection; source-artifact manifest/checksum scanners; strict CI/release-certification workflows; SDK/storage regression doubles and tests.

Page: generated complete response contract and field constraints; enum/array form controls; per-game field ownership; paginated/filterable records and signed private previews; provider sandbox; refresh after command/lifecycle actions; HTTPS production-mode integration fixture, Playwright accessibility/security/mutation tests; strict CI with immutable Core pin still required.

Latest local observations: 106 Core tests plus six subtests passed on Python 3.11; overall Core coverage 55.28%, FAILING the required 80% gate. Five critical modules exceed 90%. Fresh non-incremental mypy passed 48 files. Page: 32 unit tests, ESLint, type check and production build passed; six HTTPS browser integration/accessibility scenarios passed. Utility coverage 100% statements/lines/functions, 99.4% branches; compressed JavaScript 184501 bytes. Production dependency audits reported no known vulnerabilities. These results apply only to the unuploaded local source.

## Immediate correctness work

An attempted edit at disconnection is unconfirmed: inspect work/correctness.py and actual files before reapplying.

1. AI game verdict must never override deterministic wrong-answer truth; fix judge_guess_reply and escaped word-boundary regex, add adversarial verdict tests.
2. Honor guild ai.memoryRetentionDays in session writes and reference/channel expiry. Fresh configuration should apply nonsecret AI_CHANNEL_ID / ALLOWED_CATEGORY_ID deployment defaults. Add native expiresAt to Firestore log writes.
3. Persist scope-specific command manifest digests; skip unchanged startup sync and keep explicit manual sync forceful. Test successful-only persistence and changed configuration.
4. Enforce configured per-channel active game limits; test concurrency, timeout and completion release.
5. Add Page history secret-scan job. Pin Page CI Core checkout to the actual immutable newly uploaded Core commit.
6. Expand meaningful Drive SDK/provider transport/Discord command/presentation/weather tests to meet whole-backend coverage >=80%; retain critical >=90% gates without exclusions or waivers.
7. Regenerate and copy Core contracts; regenerate Page types/field rules; update migration/index/TTL docs and progress ledgers. Inspect range bounds and mutation audit failure behavior.
8. Upload all pending source using work/checkpoint.py; independent confidential-content scan required. Transfer JSON in bounded chunks rather than dumping source. Create trees/commits and fast-forward revamp/v2 using the GitHub connector. Preserve local edits when updating checkout baseline.
9. Run applicable checks, repair GitHub CI, build/scanner-check source artifacts, and verify full coordinated integration before release decisions.

## External stable-release gates

- Public Firebase credential exists in historical commits: verify deletion/revocation of key aae80ee9621a93b0f552cf859768c6f24e6848fc for firebase-adminsdk-fbsvc@conan-gray-database.iam.gserviceaccount.com, project conan-gray-database; provision private least-privilege replacement and inspect IAM audit logs. Failed token exchange is not revocation proof.
- Default-branch history replacement was rejected by automatic approval review as destructive and insufficiently explicitly authorized. Do not bypass with force pushes. Sanitized root exists separately.
- Vercel project access returns 403 under current account/team scope; account authorization must be corrected.
- Private Discord OAuth client secret and exact HTTPS callback registration are not available.
- Discloud runtime control, deployment/provider/live Discord/Firebase/Drive/AI/weather production validation remain unavailable.
- No stable tags or Releases were created. Certification flags remain false. Do not publish v2.0.0 while quality or external gates remain incomplete.
