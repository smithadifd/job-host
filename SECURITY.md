# Security

Do not commit credentials, private hostnames or machine configuration. Resolve
secrets at runtime, limit inherited environment and privileges, and keep logs
free of secret values. Report a vulnerability privately to the repository's
maintainers, with reproduction steps and affected revisions. Do not publish a
working credential in a report.

The carve scans all written bytes before and after writing. That scan is a
privacy floor, not a proof that every dependency or job is safe to execute.
