# Credential-rejection test fixtures

The `test_reject_static_credential_literals_but_not_identifiers` test constructs a deliberately artificial token at test runtime. Its purpose is to prove that hard-coded credential literals are rejected in both Python-like source and JSON-like source.

No application credential, API integration, or external account uses this synthetic token. Keep the test behavior intact and do not add real secrets or raw scanner findings to public issues or workflow artifacts.

Past commits contained a contiguous artificial test token, which may still be reported by full Git-history secret scanners. This historical test fixture must be narrowly and independently reviewed; do not exclude entire files, rule families, or Git history from scanning.
