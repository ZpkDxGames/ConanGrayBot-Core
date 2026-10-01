# v2 migration preparation

Do not migrate production until current source, CI, security and runtime gates are complete.

| Previous variable or behavior | v2 replacement/action |
| --- | --- |
| DASHBOARD_SESSION_KEY | Remove from Core and browser storage. Provision CORE_SERVICE_TOKEN privately on Core and Page. |
| Browser API origin/key inputs | Remove. APP_ORIGIN and CORE_ORIGIN are server environment configuration. |
| Shared/fallback media key | Provision independent MEDIA_STREAM_SIGNING_KEY on Core. |
| Permanent signed media URLs | Expiring signatures bind file, filename, expiry and disposition; regenerate links. |
| Hardcoded staff role | Configure DISCORD_STAFF_ROLE_ID; no default privileged role. |
| Unversioned management API | `/api/v1` with signed actor metadata. |
| Config without revision | Pure migration to schema 4, initial revision 0; future writes use revision compare-and-swap. |
| Silent in-memory production fallback | Production requires Firestore credentials or managed ADC. |
| Page sign-in | Discord identify-only OAuth, SESSION_SECRET and exact APP_ORIGIN/auth/callback registration. |

Back up configuration in a private location outside the repository before applying a migration. Preserve customized persona, song catalog and message templates. Unknown fields fail validation and require review; they are not silently discarded. Roll back deployment and configuration together, retaining the previous private backup.

The compromised Firebase key must never be used as a rollback credential. Review SECURITY.md before provisioning replacement credentials.

## Retention, records and command manifests

New AI session writes use the saved guild `ai.memoryRetentionDays` setting (1–365 days). Deploy Firestore TTL policies for `expiresAt` in collection groups `ai_sessions`, `ai_branch_refs`, `ai_channel_state` and `logs`; log writes expire after 30 days. Reads suppress expired content even before asynchronous TTL deletion. Existing records without deadlines remain readable during migration; back up privately and assign deadlines before enabling cleanup. Reducing retention affects newly written sessions; use memory clear for immediate removal of older content.

Media and logs return `{guildId, items, nextCursor}` with limits 1–100. Preserve the opaque cursor with the same filters. Firestore sorts by `createdAt` then document name, both descending. Provision collection-group indexes on `media_archive`: `mediaType ASC, createdAt DESC, __name__ DESC`; `channelId ASC, createdAt DESC, __name__ DESC`; and their combined mediaType/channelId form. Search evaluates a bounded Firestore page and may return an empty page with a continuation; continue until the cursor is null. Use an external search index if complete large-scale text search is required.

Command manifest hashes live separately in `command_manifests/{scope}`. Only successful Discord synchronization persists a hash. Startup skips an unchanged manifest; explicit dashboard publication forces synchronization. Configuration saves do not silently publish command changes.

The isolated provider sandbox uses saved configuration and neither reads nor writes conversation memory. Media preview URLs expire and bypass public image optimization caches; refreshing the record page renews them.
