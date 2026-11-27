# job-host

A roost is where scheduled jobs settle between runs.

Shell helpers for scheduled jobs: locking, bounded execution, secret resolution
and telemetry transport. The carved tree contains 62 vendored files.
See `job-host/README.md` for the library API and `job-host/BOUNDARY.md` for its
integration boundary.

## Use

Run `bash install.sh DEST` to copy the two library folders to an absent
installation directory. Source `DEST/job-host/host.sh` from a Bash job.
All vendored files deliberately retain mode 0644; invoke scripts through their
interpreter. The example plist is data: edit its command and schedule before
loading it yourself. Installation never loads a job or changes shell startup.

## Provenance

This tree was carved from commit `d74803c9a85a43ee83d4278bb0e04292d952c788`. `.job-host-manifest` lists only
vendored members. `VENDORING.md` records the initial byte comparisons and the
leaf-local comment and documentation edits that later member pins preserve.
Repository docs, installer, workflow and example plist are not vendored members.
