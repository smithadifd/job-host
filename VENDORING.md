# Vendoring

Source commit: `d74803c9a85a43ee83d4278bb0e04292d952c788`. Vendored members: 62.

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
