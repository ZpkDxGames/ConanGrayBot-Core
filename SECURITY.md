# Security and credential incident

The publicly committed Firebase service-account key is compromised. Removing its file from current source does not revoke it or remove historical objects.

A Google Cloud administrator must verify deletion of key `aae80ee9621a93b0f552cf859768c6f24e6848fc` on `firebase-adminsdk-fbsvc@conan-gray-database.iam.gserviceaccount.com` in project `conan-gray-database`, provision least-privilege replacement credentials and review IAM activity. An earlier `invalid_grant` response is not proof of revocation.

Default-main history still requires an explicitly authorized rewrite and GitHub Support removal of cached sensitive objects. Automatic approval review rejected the previous forced main replacement; it was not bypassed.

Keep all production credentials outside Git. Use independent random service and media keys of at least 32 characters. Core management requests require server credentials, signed actor metadata and a live staff-role check. Historical permanent media links are deliberately invalid; regenerate expiring links through Core.

Run one Core worker. Multi-worker deployment requires shared replay/rate state and a distributed lifecycle owner before use.
