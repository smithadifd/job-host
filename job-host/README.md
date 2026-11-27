# job-host

> **The name is provisional.** "job-host" is a working name. This directory and its `jh_` prefix will be renamed once the boundary drawn in `BOUNDARY.md` has been proven on real jobs. Until then this directory stays in this repository.

`host.sh` is a small shell library for scheduled jobs. It covers five concerns:

| concern | functions |
|---|---|
| run guard | `jh_init`, `jh_slot_guard`, `jh_require_cmd` |
| lock | `jh_single_lock` |
| secrets | `jh_with_secrets` (through `jh_secrets.py`, which imports `launchd-kit/op-resolve.py`) |
| timeout | `jh_timeout`, `jh_run_bounded` |
| telemetry transport | `jh_spool_append` |

The `jh_*` core knows nothing about the delegation loop, Notion, Discord, or
the shape of a telemetry record. The compatibility entry point layers the
sourceable `launchd-kit/lib/{notify,telemetry,chain}.sh` integrations on top.
`BOUNDARY.md` maps every shared function to its source and destination.

## Use

```bash
#!/bin/bash
set -uo pipefail
source "$HOME/claude-toolkit/job-host/host.sh"

TOOL_NAME=my-job
jh_init                      # pin PATH, HOME and TZ, open $LOG
jh_slot_guard daily 07:00    # first tick of the day on/after 07:00
jh_single_lock               # skip if another run holds the lock

# Bound a step to 20 minutes; a timeout kills it and is recorded to
# JH_TIMEOUT_SPOOL.
jh_run_bounded 1200 ./fetch.sh

# Resolve the op:// references in the env-file, then run the step with them.
jh_with_secrets "$HOME/.config/my-job/env" ./publish.sh
```

`jh_timeout` and `jh_run_bounded` run commands, not shell functions.

Leaf programs are tracked as regular files at mode `100644`. Invoke shell
programs with `bash <path>` and Python programs with `python3 <path>`; do not
add executable bits. Sourcing libraries and importing Python modules are unchanged.

A job written against `launchd-kit/lib.sh` can source `host.sh` in its place.
`host.sh` defines the `lib.sh` names for job-host functions and sources the same
notify, run-telemetry and chain modules as `lib.sh`. It reads the same
environment variables and keeps the same state files: the run log,
`.last-run-window` and `.run.lock`. Set `JH_NO_LK_ALIASES=1` before sourcing to
skip only the job-host compatibility wrappers; the three integrations remain
available against the native `jh_timeout` and `jh_on_exit` dependencies.

## Recipes

A recipe is a JSON manifest that records a scheduled job's skill, trigger,
runtime target discovery, model and trust rung. Repository names do not belong
in a recipe; the job discovers its targets when it runs. Each job keeps its
recipe in its own folder as `recipe.json`; the dependency-update job is the first.

Validate a recipe from the repository root, naming the job's folder:

```sh
python3 jobs/_lib/validate_recipe.py <job-folder>/recipe.json
```

The validator requires exactly the five top-level fields, requires runtime
target discovery, and accepts only named trust rungs.

## Secrets

`jh_with_secrets <env-file> <cmd> [args...]` resolves every `op://` value in the env-file against 1Password Connect, using `OP_CONNECT_HOST` and `OP_CONNECT_TOKEN`. It then execs the command with the resolved values.

- **All or nothing.** If any reference fails, the command does not run and the call returns non-zero. The command never starts with a partial environment.
- **Nothing reaches the caller.** Resolution happens in a child process, so the calling shell never holds a secret.
- **The Connect credentials are removed.** They are taken out of the command's environment.
- **Resolution is bounded.** `JH_SECRETS_TIMEOUT_SECS` (default 120) limits how long resolution can take; the command's own runtime is not limited by it.

## Tests

Run `python3 -m pytest job-host/tests -q`, or run each file directly with `python3 job-host/tests/test_<name>.py`. The suite needs macOS, because `lib.sh` and the slot guard use BSD `date`.

- `test_parity.py` runs one fixture job through `launchd-kit/lib.sh` and through `host.sh`, then compares the results.
- `test_lock.py` covers lock contention, including two contenders for one stale lock and a stale holder that exits after its lock was stolen.
- `test_secret_resolution.py` uses the real resolver against a local Connect stub. It covers the success path, a failure that leaves no partial environment, and the resolution bound, including a hung Keychain lookup that must leave no process behind.
- `test_timeout.py` checks that a timeout kills the child's process group and records the event. It also covers the spool transport.
- `test_boundary.py` keeps `BOUNDARY.md` in step with `lib.sh`.
- `jobs/_lib/tests/test_recipes.py` validates recipe manifests, the regular-job layer boundary,
  and the compatibility functions used by the dependency-update job.
- `test_split_integrations.py` compares notify bytes, run-record bytes and a
  two-link chain through `lib.sh` and `host.sh`, and checks every include guard.
