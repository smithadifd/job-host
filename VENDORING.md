# Vendoring

Source commit: `d74803c9a85a43ee83d4278bb0e04292d952c788`. Vendored members: 62.

After the carve, the member files listed below received leaf-local comment and
documentation edits. Their rows in `scripts/job-host-blobs.tsv` pin the leaf
bytes, not the source commit's blobs. A re-carve must preserve these edits.
The pins file's two header lines are fixed parser syntax retained for compatibility.

- `job-host/README.md`
- `launchd-kit/README.md`
- `launchd-kit/discord-notify.sh`
- `launchd-kit/lib.sh`
- `launchd-kit/serotine_usage.py`
- `launchd-kit/telemetry/serotine-run-v1.schema.json`
- `launchd-kit/telemetry/serotine_spool.py`
- `launchd-kit/telemetry/serotine_trace.py`
- `launchd-kit/test-serotine-trace.py`
- `launchd-kit/test-serotine-usage.py`

`launchd-kit/VERSION` is also pinned to its leaf bytes: from 0.1.1 it carries
the leaf's release number, so a re-carve must keep the leaf's value.

`.job-host-manifest` is UTF-8, LF-terminated, with optional `tree <folder>/`
lines followed by byte-sorted member paths. Every tree folder must contain
only members. There are no tree lines in the initial carve. Seeds and filled
templates are repository-only files, not members. Member modes are always
0644, including files executable in the source. The carve prints their names.

Only member bytes and exact member-path lines in the manifest receive the
leaf scan allowance. Seeds, templates and every other manifest byte receive
none. Source reads use immutable blobs, replacement objects and lazy fetch
disabled. A working-tree edit cannot affect a carve at a recorded commit.

## Comparisons against source blobs

The carve compares every written member with its source blob and prints the
number of identical members. Member paths are recorded only in the manifest:
copying those paths here would give an outsider document member-only words.
