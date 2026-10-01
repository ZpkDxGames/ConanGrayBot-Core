# ConanGrayBot v2 execution recovery checkpoint

**Status: blocked before implementation upload. This branch is not a deployable v2 release.**

The execution service disconnected immediately before the tested local implementation could be uploaded. It reports `409 environment_offline: Environment is not connected`. GitHub remains available. No stable tags or Releases have been created; GitHub CI has not validated the local implementation.

## Repository containment already completed

- Core baseline: cbe803217600e4a169eac326cd02cc7184ae9bfc.
- Page baseline: c5b04cb12e13a03b399ee2bbdf4225444950f0a2.
- Both repositories were public when inspected.
- Exposed Firebase JSON removed from current Core main in 34c449b9feed4c7a79285d3f2ac989dfad2650ad.
- Credential-free remote Core root: aeb9ad6e8797d21817c3d711e2733d088eaa5425; tree 7227007acfd9d63fd2c3f1d0951a356de9382907.
- The implementation branch uses the clean root rather than exposing the old credential through another branch.
- Main/history force replacement was rejected by automatic approval review and was not bypassed. Historical sensitive objects still require removal.

## Tested implementation awaiting workspace recovery

Local source directories: `/workspace/scratch/93271eca0bc3/core` and `/workspace/scratch/93271eca0bc3/page`. These paths are recovery references, not downloadable artifacts or proof that the environment remains recoverable.

Core changes: strict settings and independent secrets; hash-locked dependencies; schema 4 typed config and revision transactions; versioned signed-actor API; replay/rate/guild/body limits; structured errors and diagnostics; Discord/API decomposition; command manifest synchronization; HTTP pooling, deadlines and provider circuits; bounded state and retention; media expiry/ranges/MIME checks, duplicate-delivery locks, size validation, upload compensation and cleanup.

Page changes: Next.js 16.3.8, React 19 and TypeScript; Discord OAuth, host-only HttpOnly/Secure sessions and live staff-role checks; constrained signed BFF; nonce CSP; modular management sections; explicit config saves/conflict recovery; destructive confirmations; paginated records/previews; command registry and provider sandbox. Generated contracts were synchronized with Core.

Private-backup/dry-run migration tooling, documentation, changelogs, artifact/checksum generation, strict CI workflows and an unfulfilled runtime certification manifest were also created locally.

## Last completed local evidence

- Python 3.11 and 3.12: 196 tests and 6 subtests passed. Core overall coverage 80.31%; every critical auth/config/migration/signing module exceeded 90%.
- Core Ruff formatting/lint and full backend mypy passed.
- Page: 48 utility tests passed; line coverage 100%, branch coverage 98.07%; lint, strict TypeScript and production build passed.
- Nine production-build browser tests against the real Core API with isolated Discord fixtures passed: session/role denial, signed integration, revision save/conflict, command confirmation, lifecycle cancellation, mobile keyboard navigation, axe accessibility and CSRF denial.
- Both production dependency audits reported no known vulnerabilities.
- Core allowlisted source artifact and checksum built; artifact boundary scan passed. Uploaded environment values were compared confidentially against source without printing values.
- Page all-route JavaScript gzip output: 198,795 bytes; committed whole-build budget: 750,000 bytes.
- These results are local evidence, not CI evidence for this remote branch and not production runtime certification.

## Exact recovery sequence

1. Restore access to the existing execution workspace. Confirm the Core/Page directories and generated artifacts are present before changing or replacing the environment.
2. Read Core/WORK_PROGRESS.md if its final update completed. Otherwise use this checkpoint and the existing code/tests; do not repeat the initial repository audit.
3. Regenerate the safe upload manifest with work/prepare_upload.py only after inspecting it. It compares confidential runtime values but prints only counts/paths. Do not upload private/, .env files, credentials, caches, test traces or raw logs.
4. Upload the tested source from both repositories through the authorized GitHub connector, preserving Core's clean ancestry and Page's baseline ancestry. Pin Page CI to the immutable coordinated Core SHA. Open coordinated PRs.
5. Run and repair GitHub CI; verify contract drift, source artifacts, secret scans, coverage, browser integration and accessibility against the committed pair.
6. Complete the external security/runtime gates below. Only then merge, tag both verified SHAs v2.0.0 and publish stable Releases with artifacts/checksums and deployment evidence.

## External gates

- A Google Cloud administrator must verify deletion/revocation of key ID aae80ee9621a93b0f552cf859768c6f24e6848fc on firebase-adminsdk-fbsvc@conan-gray-database.iam.gserviceaccount.com, project conan-gray-database. These are identifiers, not secret values. Token exchange returned invalid_grant; that does not prove revocation. Provision replacement least-privilege credentials and review IAM/activity logs.
- Authorize/perform exact default-main history replacement and ask GitHub Support to purge cached sensitive objects/affected refs; coordinate forks/clones. Do not reintroduce the compromised key.
- Reauthorize Vercel for target project conan-gray-bot. The connected account could list projects but target access returned 403.
- Supply/configure the Page Discord OAuth application secret and exact HTTPS callback registration; the supplied bot runtime snapshot did not contain that OAuth secret.
- Provide Discloud control-plane/runtime access and complete real Discord, Firebase, Drive, enabled AI-provider, weather and Vercel certification. Local fixtures cannot fulfill these gates.

No confidential values are recorded here. Stable release closure remains incomplete.
