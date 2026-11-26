# Boundary: where each part of `launchd-kit/lib.sh` goes

This maps every function in `launchd-kit/lib.sh` and its sourceable integration
modules to one destination:

- **job host**: this directory. These are the run guard, the lock, secrets, timeouts and telemetry transport.
- **notify integration**: posting to Discord, and anything that only exists to post.
- **Serotine payload**: building, pushing and enriching `serotine.run.v1` records. This includes the sink adapter (the push to the ingest API).
- **fallback chain**: fixed engine fallback policy, not a core host concern.

`launchd-kit/lib.sh` sources all three modules. `job-host/host.sh` sources the
same modules after binding their timeout and exit-handler dependencies to
`jh_timeout` and `jh_on_exit`. Each module has an include guard, so sourcing it
again is inert. `tests/test_boundary.py` checks every function and line below.

## Functions

| source | function | line | destination | job-host counterpart | notes |
|---|---|---|---|---|---|
| `launchd-kit/lib.sh` | `_lk_run_exit_handlers` | 257 | job host | `_jh_run_exit_handlers` | The single EXIT dispatcher. |
| `launchd-kit/lib.sh` | `lk_on_exit` | 266 | job host | `jh_on_exit` | Exit-handler registry. |
| `launchd-kit/lib.sh` | `_lk_timeout` | 314 | job host | `jh_timeout` | Four-tier hard bound. |
| `launchd-kit/lib.sh` | `_lk_have_cmd` | 428 | job host | `_jh_have_cmd` | Runnable-file check. |
| `launchd-kit/lib.sh` | `launchd_kit_init` | 437 | job host | `jh_init` | Pins the environment and opens the log. |
| `launchd-kit/lib.sh` | `lk_log` | 489 | job host | `jh_log` | |
| `launchd-kit/lib.sh` | `_lk_parse_duration` | 501 | job host | `_jh_parse_duration` | Parses `LK_SLOT_GRACE`. |
| `launchd-kit/lib.sh` | `lk_slot_guard` | 590 | job host | `jh_slot_guard` | Window guard. |
| `launchd-kit/lib.sh` | `_lk_slot_claim_on_success` | 722 | job host | `_jh_slot_claim_on_success` | |
| `launchd-kit/lib.sh` | `lk_single_lock` | 732 | job host | `jh_single_lock` | Lock. |
| `launchd-kit/lib.sh` | `_lk_release_lock` | 747 | job host | `_jh_release_lock` | |
| `launchd-kit/lib.sh` | `lk_require_cmd` | 765 | job host | `jh_require_cmd` | Missing-command check and red notification. |
| `launchd-kit/lib.sh` | `lk_rotate_logs` | 806 | job host | `jh_rotate_logs` | |
| `launchd-kit/lib/notify.sh` | `lk_notify` | 70 | notify integration | — | Posts through `discord-notify.sh`. |
| `launchd-kit/lib/notify.sh` | `lk_notify_strict` | 123 | notify integration | — | Propagates delivery failure. |
| `launchd-kit/lib/notify.sh` | `lk_enable_crash_alert` | 141 | notify integration | — | Registers the crash handler. |
| `launchd-kit/lib/notify.sh` | `_lk_crash_alert` | 145 | notify integration | — | |
| `launchd-kit/lib/chain.sh` | `lk_run_chain` | 118 | fallback chain | — | Pinned fallback-on-hard-failure chain. |
| `launchd-kit/lib/telemetry.sh` | `_lk_rfc3339` | 44 | Serotine payload | — | Record timestamp. |
| `launchd-kit/lib/telemetry.sh` | `_lk_rand4` | 52 | Serotine payload | — | Run-id suffix. |
| `launchd-kit/lib/telemetry.sh` | `lk_enable_run_telemetry` | 64 | Serotine payload | — | Registers the exit fallback. |
| `launchd-kit/lib/telemetry.sh` | `_lk_emit_run_exit` | 75 | Serotine payload | — | |
| `launchd-kit/lib/telemetry.sh` | `lk_emit_run` | 86 | Serotine payload | — | Explicit terminal emit. |
| `launchd-kit/lib/telemetry.sh` | `_lk_spool_write_line` | 96 | Serotine payload | — | Isolated spool record writer. |
| `launchd-kit/lib/telemetry.sh` | `_lk_truncate_spool` | 103 | Serotine payload | — | In-place spool truncate. |
| `launchd-kit/lib/telemetry.sh` | `_lk_spool_size` | 125 | Serotine payload | — | Spool byte size probe. |
| `launchd-kit/lib/telemetry.sh` | `_lk_append_spool_line` | 149 | Serotine payload | — | Locked spool append with rollback. |
| `launchd-kit/lib/telemetry.sh` | `_lk_emit_run_core` | 359 | Serotine payload | — | Builds, spools and pushes a run line. |
| `launchd-kit/lib/telemetry.sh` | `_lk_resync_cursor` | 504 | Serotine payload | — | Repairs a push cursor. |
| `launchd-kit/lib/telemetry.sh` | `_lk_push_spool` | 538 | Serotine payload | — | Best-effort sink adapter. |
| `launchd-kit/lib/telemetry.sh` | `_lk_collect_failure` | 676 | Serotine payload | — | Failure forensics. |
| `launchd-kit/lib/telemetry.sh` | `_lk_attach_trace` | 807 | Serotine payload | — | Failure trace POST. |

## Top-level definitions that are not functions

| source | definition | destination |
|---|---|---|
| `launchd-kit/lib.sh` | `_LK_DIR`, `LK_EXIT_CODE`, `_LK_EXIT_HANDLERS` | Library location and job-host runtime. |
| `launchd-kit/lib/notify.sh` | `LK_COLOR_*` | Notify integration. |
| `launchd-kit/lib/telemetry.sh` | `_LK_TELEMETRY_ROOT`, `_LK_CURSOR_OFF` | Payload helper location and push cursor. |

## What the host adds that `lib.sh` does not have

- **`jh_with_secrets`** runs `jh_secrets.py`. That script imports `launchd-kit/op-resolve.py` (it does not copy it) and adds a bound on resolution (`JH_SECRETS_TIMEOUT_SECS`). Jobs that call `op-resolve.py --env-file … -- cmd` today get the same fail-closed behaviour.
- **`jh_run_bounded`** runs a command under `jh_timeout`. On a timeout that the clock confirms, it records an event. The payload comes from the caller's `JH_ON_TIMEOUT`, or else it is a small `job-host.timeout` record. The record goes to `JH_TIMEOUT_SPOOL`, which is kept apart from the run-record spool by default.
- **`jh_spool_append`** is the transport on its own. It appends one line of caller-supplied JSON to a named spool.

## Known differences from `lib.sh` for the shared functions

- `host.sh` now loads `notify.sh`, `chain.sh` and `telemetry.sh`; jobs can use
  those functions without sourcing `lib.sh`. The host does not copy or wrap
  their names. Its only bindings are the explicit timeout and exit-handler
  dependency variables, which point at `jh_timeout` and `jh_on_exit`.
- `jh_slot_guard` refuses with exit 2, and claims nothing, when it cannot evaluate the schedule: a time of day outside `H:MM` 00:00–23:59 for `daily`, `weekly:` or `quarterly` (`slots:` ignores the time of day, as `lk_slot_guard` does), a weekday that is not a day name, a malformed `slots:` list, or a window start `date` cannot compute. `lk_slot_guard` claims the window and runs the job in those cases.
- `jh_single_lock` writes an owner token to `.run.lock/owner`. Stealing a stale lock and releasing one both take the directory's claim (`.run.lock/.claim`) and re-check the owner first. Two contenders cannot both steal one stale lock, and a holder whose lock was stolen does not delete the new holder's lock at exit. `lk_single_lock` checks the age and then deletes, so both can happen there. A stale lock written by `lib.sh` (no owner file) is still stolen.
- Like `lib.sh`, `jh_slot_guard` uses BSD `date` (`-j -f`, `-v`), so it runs on macOS only.
