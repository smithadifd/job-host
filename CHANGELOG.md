# Changelog

## 0.1.1 — 2026-11-27

A handle-free telemetry schema id and documentation that describes the code
directly. Runtime behaviour, run records and test expectations are unchanged.

- **Telemetry schema id.** `serotine-run-v1.schema.json` now carries the
  `$id` `urn:serotine:run:v1` instead of a repository URL. The validator
  treats `$id` as an annotation, so every record that validated before still
  validates.
- **Continuation contract citation.** The launchd-kit README cites the
  continuation contract without a private path.
- **Documentation and comments.** Reword documentation and comments to describe
  scheduled jobs, telemetry and input contracts directly. Refresh member blob
  pins to record these leaf-local comment and documentation edits after the
  carve; future carves must preserve them.
- **Kit version.** `launchd-kit/VERSION` reads `0.1.1`, so the kit line a run
  logs names this release.

Upgrade with `v0.1.1`.

## Initial carve

Carved 62 vendored files from commit `d74803c9a85a43ee83d4278bb0e04292d952c788`, preserving member bytes
and recording member paths in `.job-host-manifest`. All members are mode 0644.
The initial manifest is file-level; no folder is claimed in full.
