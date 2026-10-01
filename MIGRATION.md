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
