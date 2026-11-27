# launchd-kit

Shared boilerplate for macOS **launchd** scheduled-run wrappers.

Release **0.1.1**, carried by the job-host leaf.

## Excluding-principal seats

Unattended Claude and OMP lanes use a separate macOS principal whose normal
filesystem permissions exclude the operator's account. The inert
[unattended-seat plist template](./templates/)
is a LaunchDaemon template: it supplies `UserName`, a seat-owned working root,
and only `HOME`, `PATH`, and `TMPDIR`. It contains placeholders, is not installed
by this repository, and has `RunAtLoad` disabled. **Do not load** the template.

The owner creates the local user by hand, chooses its password/login posture,
keeps its group membership to the minimum required for its own files, and
creates the seat root plus its seat-owned `home`, `work`, `tmp`, and `logs`
directories. The root itself and its `bin`/policy subtree stay owner-controlled,
readable/executable by the seat, and not writable by the seat. The owner then
renders `__LABEL__`, `__SEAT_USER__`, `__SEAT_ROOT__`, `__EXCLUDE_ROOT__`, and
`__PROGRAM__` in the template during a supervised setup. `__EXCLUDE_ROOT__`
renders only inside the audit comment; it is never passed to the job. The program
placeholder should name that protected launcher; its fixed arguments live in
the script rather than in the plist. Secrets and operator home paths do not
belong in the plist environment.

[`scripts/unattended/seat-launcher.sh`](../scripts/unattended/seat-launcher.sh)
is that protected launcher (CX15): it pins the env vars this LaunchDaemon
shape does not supply (`USER`/`LOGNAME`, alongside the plist's own
`HOME`/`TMPDIR`/`PATH`), refuses fail-closed unless both the layer-1
dead-man evidence and the harness's confinement evidence are fresh
(`refuse_seat_launch: <reason>`, non-zero exit, nothing spawns), then chains
`spawn_ledger.py` and execs the evidence-gated
`codex_builder_launcher.py --unattended`. Like the plist, this repository
neither installs it to `/opt/unattended-seat/bin/` nor loads the job — that
remains a supervised §3 sitting.

The seat principal must have no write permission to `__EXCLUDE_ROOT__`, including
the primary clones, dotfiles, credentials, and sibling worktrees below it. Do
not add the seat to an operator/admin group or grant ACLs that reopen those
paths. Its explicit writable roots are the seat-owned subtrees, the one assigned
worktree and evidence directory, and temporary scratch required by the lane.
The assigned worktree lives below `__SEAT_ROOT__/work`; it is not an ACL
exception for a worktree below `__EXCLUDE_ROOT__`.
Creating the account, directories, ACLs, rendered plist, or LaunchDaemon is a
supervised owner action; this source-only template performs none of them.

Every `$HOME/.claude/scripts/*-run.sh` wrapper on the Host-a re-implemented the same
five concerns by hand: pin the minimal environment launchd hands a job, guard
against overlapping wake-ups, set up a rotating per-tool log, post to Discord,
and shout if the run itself crashed. `lib.sh` extracts those so a new automation
`source`s the library and calls a few functions instead of copy-pasting ~40
lines of shell into every wrapper.

`lib.sh` is a **library**: sourcing it only defines functions and default
variables. It has no side effects until you call `launchd_kit_init` and the
helpers explicitly — it never runs `claude`, moves git state, or posts to
Discord on its own.

Discord delivery is delegated to `discord-notify.sh` — the notifier that owns
the automation layer's notify contract (webhook resolution order, secrets
hygiene, truncation) documented in [`discord-notify.md`](./discord-notify.md).
This kit does **not** re-implement it. The canonical, version-controlled copy now
lives here at [`launchd-kit/discord-notify.sh`](./discord-notify.sh) (see
[The Discord notifier](#the-discord-notifier) below); the machine-local
`$HOME/.claude/scripts/discord-notify.sh` mirrors it. `lk_notify` runs
`bash "$NOTIFY_SCRIPT"` (default: the machine-local copy). `NOTIFY_SCRIPT`
is one readable regular shell-script pathname, not a command string; an
executable bit is not required. Missing, unreadable or non-regular targets skip
in `lk_notify` and fail in `lk_notify_strict`.

For safe review and probes, set `DISCORD_NOTIFY_DRY_RUN=1` when invoking
`discord-notify.sh`, or `NOTIFY_OWNER_DRY_RUN=1` when invoking
`notify-owner.sh`. Either variable set to exactly `1` on `notify-owner.sh`
propagates dry-run mode only to a notifier that advertises the same guard; an
older notifier is never invoked. The payload is rendered to stderr with the
webhook and token redacted, and no timeout library, secrets file, 1Password
reference, or `curl` call is touched. Because dry-run does not read the env file,
a would-be owner mention is shown as `<@mention-redacted>`; `--fyi`/the
notifier's `--no-mentions` mode still disables mention parsing. Review and probe
briefs should always set the applicable variable.

## Scheduled decision wrappers

[`merge-tick-run.sh`](./merge-tick-run.sh) runs the four-hour merge decision
check with a Connect credential and writes `decisions.jsonl` under
`$HOME/.claude/logs/merge-tick/`. Its [plist template](./templates/)
ships in dry-run mode; `MERGE_TICK_APPLY=1` enables applying eligible decisions.
Before loading the template, create its launchd output directory with
`mkdir -p "$HOME/.claude/logs/merge-tick"`; launchd opens the output paths before the
wrapper can create its log directory.

[`hands-on-digest-run.sh`](./hands-on-digest-run.sh) runs once successfully per
day inside `DIGEST_SLOT_START`–`DIGEST_SLOT_END` (default 09:00–09:59 Eastern),
logging under `$HOME/.claude/logs/hands-on-digest/`. Its
[plist template](./templates/) ships in dry-run
mode; `HANDS_ON_DIGEST_SEND=1` enables Discord delivery. Failed runs leave the
slot open for a later tick within the window.
Before loading the template, create its launchd output directory with
`mkdir -p "$HOME/.claude/logs/hands-on-digest"`; launchd opens the output paths before
the wrapper can create its log directory. A successful dry-run also claims the
day's slot, so enabling `HANDS_ON_DIGEST_SEND=1` later that day waits until the
next day's window.

When `HANDS_ON_DIGEST_LEDGER_DB` names the ledger, the wrapper then runs
the needs-you drift check (`needs_you_drift_check.py`) once against that database,
after the digest has succeeded, and logs its exit code in one line. The check
adds `--notify` only with `HANDS_ON_DIGEST_SEND=1`. Its exit code (drift, an
unreadable input, a crash or a 120 second timeout) never changes the wrapper's
exit status or the slot claim. Without the ledger variable the check is skipped.

[`ledger-export-run.sh`](./ledger-export-run.sh) runs hourly and keeps the state
ledger's private export repository current. Each tick runs `scripts/ledger
export --commit`, then `export --push`, then `export --check` (lag limits
`LEDGER_EXPORT_MAX_EVENTS`, default 100, and `LEDGER_EXPORT_MAX_HOURS`, default
24). After those steps succeed, a private weekly slot restores the export into
a `mktemp -d` database, verifies its chain and SQLite integrity, and compares
its event count and stream hash with the live database. A passing drill records
one `item.note` on the `ledger-durability` gate (creating the gate through the
ledger CLI when absent), logs the same count/hash/seconds line, removes the
temporary directory, and claims the week. A failed drill removes the directory,
alerts, and leaves the week open for the next hourly retry. A claimed weekly
slot skips only the drill; the hourly export still runs. The wrapper reads the
database from `LEDGER_DB` and the export clone from `LEDGER_EXPORT_DIR` and
never creates either. It refuses with exit 2 unless the database is a non-empty
SQLite file and the export directory is the top level of
its own git work tree and does not overlap the job's log directory. It also
refuses an export repo whose push could do more than send the current branch to
its configured upstream: a `push.default` other than simple or upstream, a
detached HEAD, a missing upstream, a push remote that differs from the upstream
remote, or, on that remote, a push refspec (a leading `+` forces) or a mirror
setting. Other remotes are not checked. The push is bounded by
`LEDGER_EXPORT_PUSH_TIMEOUT_SECS` (default 600), and git never waits on a
credential prompt; an inherited `GIT_DIR` or `GIT_WORK_TREE` is cleared. A failed step stops the run, exits non-zero and sends one red
Discord alert through this kit's `discord-notify.sh`. The alert carries no text
from CLI or git output: only the step, its exit code, a fixed category
(`refused:<reason>`, `auth-failed`, `non-fast-forward`, `timeout`, `network` or
`other`; only a push failure can be `auth-failed`, `non-fast-forward` or
`network`) and the local log path, where the raw output stays. A
`non-fast-forward` alert names the manual recovery (`git -C '<dir>' pull
--rebase`); the job never rebases, forces or rewrites the export history
itself. Its
[plist template](./templates/) logs under
`$HOME/.claude/logs/ledger-export/`; create that directory with
`mkdir -p "$HOME/.claude/logs/ledger-export"` before loading the template.

[`disk-guard-run.sh`](./disk-guard-run.sh) runs every 15 minutes and keeps
Time Machine local snapshots from holding the data volume's real free space.
Each tick logs `disk free <N>G, snapshots <K>` (whole GiB from `df -k -P` on
the home folder's volume; the count of Time Machine snapshots from `tmutil
listlocalsnapshots /`). Below 30 GiB free it runs `tmutil thinlocalsnapshots
/ 80000000000 4`, logs `thinned: free <before>G -> <after>G, snapshots <k1>
-> <k2>` and sends at most one `notify-owner.sh` ping. `--fyi` goes out only
when free is back at or above 10 GiB and the thin freed at least 1 GiB; a thin
that freed less logs `thin freed <N>G; no ping` and stays silent. A plain ping
goes out when free is still below 10 GiB, at most once per 6 hours: the job
keeps the time of the last one in `.last-alarm-ping` in its log folder, and a
missing, unreadable, malformed or future-dated stamp counts as none. At or
above 30 GiB it only logs. It never uses sudo, never deletes a named snapshot
and never touches the network backup. `DISK_GUARD_ACT_GB`,
`DISK_GUARD_ALARM_GB`, `DISK_GUARD_DF`, `DISK_GUARD_TMUTIL`,
`DISK_GUARD_NOTIFY`, `DISK_GUARD_ALARM_STAMP` and
`DISK_GUARD_ALARM_WINDOW_SECS` override the thresholds, commands, stamp and
window; a threshold or window that is not a positive whole number refuses the
run (exit 2). A df or tmutil failure exits 1 and sends one plain ping (inside
the same 6-hour limit) only when free was already below 30 GiB, so a broken
tool never pings every tick from a healthy disk. Its
[plist template](./templates/) logs under
`$HOME/.claude/logs/disk-guard/`; create that directory with
`mkdir -p "$HOME/.claude/logs/disk-guard"` before loading the template.

## Root-delivery check

[`check-harness-root.sh`](./check-harness-root.sh) reports whether `HARNESS_ROOT`
is the same in four places: a login shell (`zsh -l`), `launchctl getenv`, a
scratch launchd job the script bootstraps without `EnvironmentVariables` (so
the job inherits the manager environment), and the last wrapper-log line
carrying the root. Check (4) needs the named job's wrapper to log a
`HARNESS_ROOT=` line; `lib.sh`'s `lk_log` does not emit one by itself.
Wrapper logs are the files `launchd_kit_init` opens under
`$HOME/.claude/logs/<last-component-of-job-label>/`.

```sh
launchd-kit/check-harness-root.sh /absolute/expected/root com.example.job-label
```

It prints one dated summary line. Exit 0 if all four equal the expected root, 1
if a named check mismatches, 2 if a check could not be verified. When one check
is unverified and another mismatches, the line names both and the exit stays 2.
The scratch job is removed on exit, including when a check fails.

A live run loads and unloads that scratch job; do not invoke it from
unsupervised automation. [`test-check-harness-root.sh`](./test-check-harness-root.sh)
stubs `launchctl` and `zsh` on PATH and never talks to a real launchd.

## Env-var contract

Set these **before** calling `launchd_kit_init`:

| Var | Required | Default | Purpose |
|---|---|---|---|
| `TOOL_NAME` | **yes** | — | Short slug (e.g. `dependabot-sweep`). Drives the default `LOG_DIR`, log lines, notify titles, and the `<TOOL>_FORCE` override. |
| `LOG_DIR` | no | `$HOME/.claude/logs/$TOOL_NAME` | Where run logs, the slot marker, and the lock live. |
| `TZ` | no | `America/New_York` | Timezone to pin so scheduled windows are stable. |
| `LAUNCHD_KIT_PATH` | no | `$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin` | `PATH` to export for the minimal launchd env. `$HOME/.local/bin` leads because the native Claude Code installer puts `claude` there; the Homebrew cask that used to supply `/opt/homebrew/bin/claude` is gone. Homebrew stays on the path for `gh`/`jq`/`git`. (`HOME` is pinned before `PATH` precisely so this default can interpolate it.) |
| `NOTIFY_SCRIPT` | no | `$HOME/.claude/scripts/discord-notify.sh` | The shell notifier `lk_notify` shells out to. |
| `LK_NOTIFY_TIMEOUT_SECS` | no | `300` | Hard bound on the `lk_notify` notifier call (via `_lk_timeout`). Deliberately generous: the stock notifier's worst *legitimate* path — Connect (3 lookups × 2 attempts × 10s = 60s) → `op read` (2 × 90s + 2s = 182s) → `curl` (20s) — is **~262s**, and a bound under that ceiling silently drops real posts instead of preventing a wedge. **Headroom is thin (~28s)**: if any leg grows, raise this first. |
| `DISCORD_WEBHOOK_URL` | no | — | Resolved by the notifier if unset: from the `default` channel (or the one `--channel <name>` or `NOTIFY_CHANNEL` names) of `notify.json`, the one fixed configuration file in the toolkit configuration folder (`$XDG_CONFIG_HOME/claude-toolkit/` when absolute, else `$HOME/.config/claude-toolkit/`; schema in [`scripts/notify-sink.md`](../scripts/notify-sink.md)). **Never logged.** |
| `NOTIFY_CHANNEL` | no | `default` | The `notify.json` channel the notifier posts to when no `--channel` flag is given. `lk_notify` passes no flags, so a wrapper picks its channel by exporting this. Letters, digits, `.`, `_` and `-` only; any other value makes the notifier exit `2`. |
| `WEBHOOK_ENV_FILE` | no | (notifier default) | Legacy, for one release: while `notify.json` is absent, the notifier reads `DISCORD_WEBHOOK_URL` from this env file as data (never sourced) and prints a deprecation line. |
| `LOG_RETENTION_DAYS` | no | `60` | `lk_rotate_logs` deletes `*.log` older than this. |
| `LOG_MAX_FILES` | no | (unset — inert) | Secondary bound for `lk_rotate_logs`, applied **after** the age-based pass: if more than this many `*.log` files remain, delete the oldest until at the cap. Opt-in only — unset changes nothing (byte-identical to the age-only rule). For sub-hourly pollers (e.g. `serotine-tripwire`, 30-min cadence) where a flat day-based bound alone lets file **count** explode even though every file is individually "young enough" (host-a-network-incident FU-4). A daily/weekly job never accumulates enough files within `LOG_RETENTION_DAYS` days to approach a sane cap, so setting this on a low-frequency job is a no-op in practice. |

After `launchd_kit_init` the library exports **`LOG`** (this run's timestamped
log file) and **`LK_STAMP`** (its `YYYY-MM-DD_HHMMSS` stamp).

## Public function API

| Function | What it does |
|---|---|
| `launchd_kit_init` | Pin `PATH`/`HOME`/`TZ`, create `LOG_DIR`, open `LOG`. **Call first.** |
| `lk_log <msg...>` | Append a timestamped line to `LOG` (and stderr). |
| `lk_slot_guard <interval> [HH:MM]` | Idempotency + timing guard. **Skips the run (`exit 0`)** if the current window already fired. See intervals below. Set `LK_SLOT_GRACE=<dur>` to close the window that long after it opened (bounds the backward reach). |
| `lk_single_lock [max_age_min]` | Atomic single-run lock (`mkdir`). **Skips (`exit 0`)** if another run holds a fresh lock; steals a lock older than `max_age_min` (default 120). Auto-releases at exit. |
| `lk_notify <title> [color] < body` | Post a Discord embed (body on stdin). **Never fails the caller** — returns 0 even if delivery fails — and **never blocks it**: the notifier call runs under `_lk_timeout` (`LK_NOTIFY_TIMEOUT_SECS`, default 300s), so a notifier hung on a Keychain prompt, a stalled 1Password lookup or a black-holed socket is killed and logged with a distinct "exceeded Ns — NO post delivered" line instead of parking the wrapper forever. |
| `lk_notify_strict <title> [color] < body` | Same interface as `lk_notify` but **propagates the notifier's exit status** instead of swallowing it — for 2xx-gated marker patterns (write a state marker only after Discord accepts the post). Deliberately allowed to fail the caller; never use it in a crash-alert path. |
| `lk_require_cmd <cmd...>` | Assert every named command resolves to an **executable** on the pinned `PATH`. Silent on success; on failure logs the offending name(s) **and** the `PATH` searched, fires a **red** notify, and **exits 127** — turning "ran, produced nothing, looked healthy" into an attributable failure. A present-but-non-executable file (what a half-finished self-update leaves behind) fails exactly like a missing one; a name lookup alone would wave it through. Opt-in: `launchd_kit_init` only **warns** about `claude`, since most wrappers never invoke it. |
| `lk_enable_crash_alert` | Fire a **red** Discord notify (with the log tail) if the script exits non-zero. Success is silent. |
| `lk_rotate_logs` | Delete logs older than `LOG_RETENTION_DAYS`. Then, if `LOG_MAX_FILES` is set, delete the oldest survivors beyond that count too (opt-in, unset = inert). |
| `lk_on_exit <fn>` | (Low-level) register a function to run at exit via the shared dispatcher. `lk_single_lock` and `lk_enable_crash_alert` use it; call it directly only for custom cleanup. |
| `_lk_timeout <secs> <cmd...>` | (Low-level) portable hard-timeout guard — `timeout` / `gtimeout` / perl-fork-alarm / background-kill fallback chain. Route **every** raw `op` (1Password CLI) call through this; `op` has no built-in timeout and can hang indefinitely under the macOS TCC app-data gate. Every tier follows SIGTERM with an unignorable SIGKILL after a grace (default `5s`, override `LK_TIMEOUT_KILL_AFTER`) — via GNU timeout's `--kill-after` in tiers 1/2, a process-group TERM-then-KILL in tiers 3/4 — so a child that traps/ignores SIGTERM (and any forked helper holding the stdout pipe) can't outlive the bound. `discord-notify.sh`'s `op read` call uses it (sourcing `lib.sh` for the canonical implementation, with an inline fallback copy if `lib.sh` isn't reachable). See the doc comment above its definition in `lib.sh`. |
| `lk_enable_run_telemetry` | (opt-in) Record the run start and arm an EXIT trap for `serotine.run.v1` emission. Call once after `launchd_kit_init`, **before** the guards. See [serotine.run.v1 telemetry](#serotinerunv1-telemetry-opt-in). |
| `lk_emit_run [opts]` | (opt-in) The wrapper's terminal per-run emit. Builds one `serotine.run.v1` JSONL line, spools it, best-effort pushes. **Never fails the caller** — always returns 0. See below. |

### `serotine.run.v1` telemetry (opt-in)

Per-run heartbeat + token/model usage for the Fleet/Serotine observability wedge
(fleet telemetry design contract § Schema, fleet telemetry decision). **Purely additive** — a wrapper
that never calls these two functions is completely unaffected; nothing emits
until a wrapper opts in.

Wire it in with two calls:

```bash
launchd_kit_init
lk_enable_run_telemetry          # record start + arm the EXIT-trap fallback (before guards)
lk_slot_guard slots:8,12,16,20   # a short-circuit here → the trap emits status=skip
lk_single_lock

# ... real work; for an LLM job capture claude -p --output-format json usage ...

lk_emit_run --usage-file "$USAGE_JSON"   # the LAST thing the wrapper does → status=ok
```

- **Clean completion** → call `lk_emit_run` as the last line → `status=ok`.
- **Crash** (non-zero exit before `lk_emit_run`) → the armed EXIT trap emits
  `status=fail` with the **real** exit code.
- **Guard/lock short-circuit** (`exit 0` before `lk_emit_run`) → the trap emits
  `status=skip`, so "skipped by design" is distinguishable from "silently dead".

**Usage capture** is optional and has two paths: pass a `claude -p --output-format
json` result explicitly (`--usage-json` / `--usage-file`), or — with neither —
a ccusage-style fallback parses the local Claude Code session JSONL for usage
records inside the run's time window (disable per-job with
`SEROTINE_USAGE_FALLBACK_DISABLE=1` for known non-LLM jobs). Tokens only; dollars
are derived at ingest, never in the emitter.

**Transport:** one line is appended to `$HOME/.claude/telemetry/spool.jsonl`;
unsent lines (tracked by a byte-offset cursor next to the spool) are best-effort
POSTed to `$SEROTINE_INGEST_URL` with `$SEROTINE_INGEST_TOKEN` **only if both are
set**. A 2xx advances the cursor; otherwise the batch is retried next run.
`serotine.run.id` is the ingest dedupe key, so replay is safe. Unset ingest env =
spool-only, silent.

**Safety (the #1 property):** emission never fails or blocks the wrapper — errors
are swallowed, the push has a short timeout and never retries within a run, and
the wrapper's exit code is untouched by anything the hook does (same posture as
`lk_notify`). `lk_emit_run` always returns 0.

`lk_emit_run` options (all optional): `--status ok|fail|skip`, `--exit-code N`,
`--operation NAME` (`gen_ai.operation.name`), `--usage-json JSON`, `--usage-file
FILE`, `--trigger launchd|manual|kickstart`, `--label LABEL`.

Telemetry env-var contract (all optional; read at emit time):

| Var | Default | Purpose |
|---|---|---|
| `SEROTINE_JOB_LABEL` | `com.$(id -un).$TOOL_NAME` | `serotine.job.label` (the launchd label). |
| `SEROTINE_HOST` | `hostname -s` | `host.name`. |
| `SEROTINE_TRIGGER` | `launchd` | Default `serotine.run.trigger`. |
| `SEROTINE_SPOOL_DIR` | `$HOME/.claude/telemetry` | Spool + cursor directory. |
| `SEROTINE_INGEST_URL` | — | Push target. Push happens only if URL **and** token are set. |
| `SEROTINE_INGEST_TOKEN` | — | Bearer token for the push. **Never logged.** |
| `SEROTINE_INGEST_TIMEOUT` | `5` | `curl --max-time` seconds for the push. |
| `SEROTINE_USAGE_FALLBACK_DISABLE` | `0` | `1` skips the session-JSONL usage fallback. |
| `SEROTINE_USAGE_PROJECTS_DIR` | `$HOME/.claude/projects` | Session-JSONL root for the fallback. |
| `SEROTINE_USAGE_SESSION_ID` | — | Scope the usage scan to one session's transcript (`<projects>/*/<id>.jsonl`) so a concurrent session can't bleed into this job's rollup / model-drift. Unset = whole-tree window walk. Pass the **same** id to `claude -p --session-id`. |
| `SEROTINE_USAGE_PROVIDER` | — | **Opt-in, non-Claude jobs**: `codex`\|`gemini`\|`omp`\|`grok-build` selects the matching provider usage adapter. **Precedence: this path is checked FIRST**, before `--usage-json`/`--usage-file` and the window fallback, and it never falls through to either. Unset = byte-identical to today (purely additive). An unrecognized value is a **hard error** (raises) rather than a silent `[]` or a silent Claude-window scan. |
| `SEROTINE_USAGE_INPUT` | — | File path holding the `SEROTINE_USAGE_PROVIDER` provider's raw output. **Codex: the session ROLLOUT file** (`$HOME/.codex/sessions/**/*.jsonl`) — the only codex-side input that carries a served model id. Raw `codex exec --json` **STDOUT is NOT valid input**: it never carries a model id and is detected + rejected with a loud, actionable `CodexStdoutWireShapeError` (fix the wiring: point this at the rollout file) rather than silently emitting a model-less usage row. Loud at the helper boundary: invoking `serotine_usage.py` directly fails non-zero with the message; under `lk_emit_run` the wrapper is (by design) never failed by telemetry — the observable symptom of a miswired job is that its usage line is simply not emitted, so run the helper directly to see the error. Gemini: a raw API response JSON body. **Omp: the `omp -p --mode json` event stream** (JSONL — the terminal `agent_end` event carries the whole `messages[]`, one usage entry per distinct `(provider, model)`). **Grok-build: the Grok Build CLI's `--output-format json` result object** (ONE JSON object — its `modelUsage` *keys* are the SERVED model ids, e.g. `grok-4.6-build` for a run that requested `grok-4.6`). For those two a capture of the client's **human-readable TUI transcript is NOT valid input** either: it is detected + rejected with a loud, actionable `TuiOutputWireShapeError` naming the flag to add, with the same "no usage line emitted" symptom under `lk_emit_run`. Required alongside `SEROTINE_USAGE_PROVIDER`; missing/unreadable → `[]` (fail closed). |
| `SEROTINE_USAGE_REQUEST_MODEL` | — | The wrapper's own pinned canonical model id (`gen_ai.request.model`) for the `SEROTINE_USAGE_PROVIDER` path — legitimate to supply directly (it **is** the pin). Never used as the response id: `gen_ai.response.model` comes only from what the provider itself reports (omitted, never fabricated, when it doesn't say one). Required; missing/blank → `[]`. |

The line shape is defined by [`telemetry/serotine-run-v1.schema.json`](telemetry/)
and built by the vendored stdlib helper `serotine_usage.py` (next to `lib.sh`). The
product name is **provisional** — see `telemetry/README.md` § Renaming checklist.

**Python producers:** pure-Python jobs (policy-tuner, project-feed's
`audit.py`) don't shell out to the bash hook — they vendor/path-import
[`telemetry/serotine_spool.py`](telemetry/serotine_spool.py), the stdlib-only
sibling of `lk_emit_run`/`_lk_push_spool` (fleet-event design contract
§ Resolutions 5): `emit_run(...)` appends a byte-compatible `serotine.run.v1`
line and best-effort pushes; `emit_event(...)` appends one `serotine.event.v1`
line **append-only** (under a launchd-kit wrapper, the wrapper's terminal
`lk_emit_run` flushes event lines too — no second push path); `push_spool()`
mirrors `_lk_push_spool` exactly (same env contract, cursor advances only on
2xx) and additionally strips `serotine.event.findings` from the POST body
**fail-closed** — findings are spool-only and never leave the machine. Same
safety posture: the public functions never raise and never log the token.

#### Failure traces — opt-in, **OFF fleet-wide**

On a **failing** run the wrapper can upload the tail of its own `$LOG` as that
run's trace through `POST /api/v1/ingest/trace`. The job page retrieves it through
`GET /api/v1/traces/<run-id>`, so `Exit 74` carries its stderr into the dashboard
instead of requiring an SSH connection to the host.

**Nothing changes until a job opts in.** With `SEROTINE_TRACE_CAPTURE` unset —
the default everywhere — the hook returns on its first line: no subprocess, no
file read, no request, no extra log line. Enable one job at a time:

```bash
export SEROTINE_TRACE_CAPTURE=fail    # `always` also uploads ok/skip runs
```

The upload rides the run emit: the run line is pushed first (the server 404s a
trace whose run it has never seen), then the trace is POSTed to
`<SEROTINE_INGEST_URL>/trace?run_id=<the run id just emitted>` with the **same**
Bearer token as ingest.

**Same never-fails-the-job posture as `lk_notify`:** both child processes are
hard-bounded by `_lk_timeout` (a `curl` that ignores `-m`, a hung DNS lookup, or
an unreachable host cannot park the wrapper), errors are swallowed, there is no
retry within a run, and the wrapper's exit code is untouched. A first-tick
failure right after a launchd boot is expected and is not treated as fatal.

**Redaction is the gate to read before enabling a job.** The body is built and
redacted by [`telemetry/serotine_trace.py`](telemetry/serotine_trace.py), whose
header documents the three layers: exact values of the credentials this wrapper
actually holds (the one real guarantee), well-known credential shapes, and an
optional per-job regex. **Fail-closed** — if the redactor cannot run, or its
pattern is unusable, *nothing is uploaded*; the run log never leaves the box
unredacted. Heuristics are heuristics: a secret that is neither in this
process's environment nor in a recognized shape can still pass through, which
is exactly why enabling is per job and starts with reading that job's own log.

| Var | Default | Purpose |
|---|---|---|
| `SEROTINE_TRACE_CAPTURE` | — (**off**) | The arming flag. `fail` (also `1`/`true`/`yes`/`on`) uploads only failing runs; `always` uploads ok/skip too; anything else is off. |
| `SEROTINE_TRACE_INGEST_URL` | `$SEROTINE_INGEST_URL` + `/trace` | Upload endpoint. Needs `SEROTINE_INGEST_TOKEN` too, or nothing is sent. |
| `SEROTINE_TRACE_LOG` | `$LOG` | Log file to capture. |
| `SEROTINE_TRACE_LINES` | `200` | Last-N log lines to send. |
| `SEROTINE_TRACE_UPLOAD_MAX_BYTES` | `262144` | Body cap **after** redaction, trimmed oldest-first. Named apart from serotine's server-side `SEROTINE_TRACE_MAX_BYTES` on purpose. |
| `SEROTINE_TRACE_TIMEOUT` | `10` | `curl --max-time` seconds (the `_lk_timeout` bound is this + 5). Must be a **positive** integer — `0` means "no timeout" to the underlying tools, so it takes the default (tightened in BS8 alongside the forensics lane's identical validation). |
| `SEROTINE_TRACE_REDACT_ENV` | — | Extra env var **names** whose values to redact. |
| `SEROTINE_TRACE_REDACT_PATTERN` | — | Extra job-specific redaction regex. An unusable regex fails closed. |

**Best-effort, never spooled.** One attempt per run, no retry, nothing queued:
a run whose telemetry push failed (host offline) also loses its trace, and that
is the deliberate trade — a spooled trace would mean run logs sitting on disk
waiting to be uploaded later. Worst-case added latency on a **failing** run is
the `_lk_timeout` bound (`SEROTINE_TRACE_TIMEOUT` + 5s, plus the guard's 5s kill
grace); a successful run under the default `fail` mode adds nothing at all.
`always` exists for debugging a specific job — do not leave it on for a poller
that skips dozens of ticks a week.

The payload is raw NDJSON — one provenance header line
(`{"type":"system","subtype":"launchd_kit_trace",…}` carrying the run id, the
capture counts and the redaction count) followed by one `{"type":"log",…}` line
per captured log line. serotine stores it verbatim and parses nothing.

#### Failure forensics on the run line — opt-in, **OFF fleet-wide** (BS8)

The other half of the same finding, and the more visible one. serotine's
`serotine.run.v1` wire has carried an optional `serotine.run.failure` object
(`message` / `last_log_line` / `stderr_tail`) since BC4, `/jobs/<label>` renders
it beneath the run it belongs to, and BH2 quotes an excerpt in the weekly Discord
digest. All of it has rendered **empty on every real run**, because nothing on
the wrapper side populated the field — serotine's own `AGENTS.md` says so:
*"NOT BUILT HERE: the emitter itself (launchd-kit `lib.sh`)"*. `lib.sh` now can.

This is **not** the trace feature above. The trace is a 200-line log tail POSTed
to a second endpoint and stored separately; this is a three-string summary that
rides the run line itself, so "why did this fail?" is answered on the job page
without a second fetch. Separate flags, armed independently, both off.

**Nothing changes until a job opts in.** With `SEROTINE_EMIT_FAILURE` unset —
the default everywhere — the collector returns on its first line (no subprocess,
no file read) and the emitted line is **byte-for-byte the line `lib.sh` emitted
before this feature existed**. That equivalence is the point of `test-lib.sh`
§ (z1)–(z3), which assert the exact nine-key line and pass against the
pre-BS8 library unchanged.

```bash
export SEROTINE_EMIT_FAILURE=fail     # `always` also annotates ok/skip runs
```

A wrapper may pass its own one-liner: `lk_emit_run --status fail --exit-code 74
--failure-message "brief generation failed"`. On the crash path there is no argv
to carry one, so the log tail is the whole evidence — which is exactly the
scenario the finding is about.

⚠ **`stderr_tail` is the wire's name, not a claim about file descriptors.** A
launchd-kit wrapper merges stdout and stderr into one `$LOG`; that is what gets
summarized. Point `SEROTINE_FAILURE_LOG` elsewhere if a job keeps them apart.

**Redaction is the emitter's job, and here it is one-way.** Unlike
`serotine.event.findings` and `serotine.gov.cause` — both free text, both
stripped fail-closed at ingest — this object is **persisted in `runs.raw` and
rendered**, by an explicit, argued exception in serotine's `AGENTS.md`; adding a
strip later would protect future lines only. So the object is built by the same
[`telemetry/serotine_trace.py`](telemetry/serotine_trace.py) redactor the trace
uses (`build-failure`), reusing `SEROTINE_TRACE_REDACT_ENV` /
`SEROTINE_TRACE_REDACT_PATTERN`, and it is **fail-closed**: no redactor on disk,
no python3, or an unusable pattern means *nothing is attached*. Read that
module's REDACTION header before enabling a job. Note also that BH2 puts an
excerpt in a **Discord** embed — off-host and unauthenticated, by election.

| Var | Default | Purpose |
|---|---|---|
| `SEROTINE_EMIT_FAILURE` | — (**off**) | The arming flag. `fail` (also `1`/`true`/`yes`/`on`) annotates only failing runs; `always` annotates ok/skip too; anything else is off. Deliberately **not** `SEROTINE_EMIT_EVENTS` or `SEROTINE_TRACE_CAPTURE` — three lanes, three switches. |
| `SEROTINE_FAILURE_LOG` | `$LOG` | Log file to summarize. |
| `SEROTINE_FAILURE_LINES` | `20` | Last-N **non-blank** lines for `stderr_tail` — serotine's own display bound. Must be a **positive** integer; `0` would disable the cut, so it takes the default. |
| `SEROTINE_FAILURE_MESSAGE` | — | Default `message` when the caller passes no `--failure-message` (the EXIT-trap crash path has no argv). |
| `SEROTINE_FAILURE_TIMEOUT` | `10` | `_lk_timeout` bound on the helper. A hung helper cannot park the job. Must be a **positive** integer — `0` means "no timeout" to the underlying tools, so it takes the default. |
| `SEROTINE_FAILURE_HELPER` | `telemetry/serotine_trace.py` | The redactor+builder. Tests override it; a missing one fails closed. |

Members are **bounded at the emitter** to serotine's display limits (`message`
500 chars and `last_log_line` 1000 keeping their head, `stderr_tail` 20 lines
then 4000 chars keeping its end) and **omitted rather than blanked** — the wire
refuses a whitespace-only member (`pattern: "\S"`) and ingest rejection is
whole-batch atomic, so one blank member would wedge every other line in the
POST.

**Three fail-closed rules, all all-or-nothing:**

1. **A helper that does not exit cleanly is not read at all.** Its stdout is
   captured into a variable and used *only* after a confirmed zero exit inside
   the bound. A helper that prints a well-formed object and then hangs (killed
   at the bound) or exits non-zero has its output discarded — the guarantee must
   not depend on a helper's internal write ordering, because a redactor killed
   mid-pass is exactly the case this hook exists to refuse.
2. **`SEROTINE_FAILURE_TIMEOUT` / `SEROTINE_FAILURE_LINES` must be positive.**
   `timeout 0` means *no timeout* to GNU `timeout` and to the `perl` `alarm()`
   fallback alike, so a `0` would silently remove the hard bound rather than
   tighten it; `0` and non-numeric alike take the documented default.
3. **`resolve_failure()` refuses the WHOLE object, never part of it.** An
   unknown member, a non-string, a blank, a value over the builder's own ceiling
   or one carrying a NUL rejects the entire response. A helper that answered
   malformed is malfunctioning, mis-built or substituted, and the rest of what
   it said is no more trustworthy than the visibly-wrong part — especially for a
   field that is persisted and rendered and whose exposure is one-way. The
   ceilings are asserted equal to the builder's own constants by a test, so they
   cannot drift on one side only.

**Arming a fleet job is a separate, supervised act** and is not done by the code
that introduced the flag.

### Slot-guard intervals

launchd fires jobs on a coarse `StartInterval` (a single `StartCalendarInterval`
timer gets dropped across sleep/DarkWake), so the wrapper fires often and the
guard admits only the **first tick** of each window — self-healing a missed wake.

| Interval arg | Meaning |
|---|---|
| `daily [HH:MM]` | One run per calendar day, on/after `HH:MM` (default `00:00`). |
| `weekly:<dow> [HH:MM]` | One run per week, on/after `<dow> HH:MM`. `<dow>` is a `date -v` day: `sun mon tue wed thu fri sat`. |
| `quarterly [HH:MM]` | One run per **calendar** quarter (Jan/Apr/Jul/Oct cycle, not rolling ~90 days), on/after the 1st of the quarter's first month at `HH:MM`. Pair with a daily-tick plist — the guard admits the quarter's first tick. |
| `slots:8,12,16,20` | One run per listed hour-slot per day (comma list). |

**Force override:** set `LK_FORCE=1`, or `<TOOL>_FORCE=1` where `<TOOL>` is
`TOOL_NAME` uppercased with `-` → `_` (e.g. `TOOL_NAME=dependabot-sweep` →
`DEPENDABOT_SWEEP_FORCE=1`).

**Window grace (`LK_SLOT_GRACE`) — opt-in, unset by default.** Every interval
above resolves its window **backward**: `weekly:sun` asked on a Friday picks
*last* Sunday. That is what makes a missed wake self-heal — but with no upper
bound the window stays "open and unclaimed" for the whole period, so a job
**loaded off-cycle** (or one whose marker is simply absent — fresh install, new
`LOG_DIR`, rotated-away marker) fires on its very first tick rather than waiting
for its real slot. That trap has bitten this fleet: a weekly brief job
(`weekly:sun 16:00`) delivered a full Sunday brief at **00:19 on a Friday**,
claiming `weekly:2026-08-09`.

`LK_SLOT_GRACE=<dur>` bounds the reach: the window **closes** that long after it
opened, and a tick past the close skips (`exit 0`) **without claiming**, so the
next real window is untouched. Duration is `<N>[s|m|h|d]`; a bare number is
seconds.

```bash
LK_SLOT_GRACE=30h lk_slot_guard weekly:sun 16:00
#  Sun 16:00 → Mon 22:00  : first tick runs (a Sunday slept through still
#                           self-heals Monday morning)
#  Wed/Thu/Fri load       : skips — waits for Sunday
```

Unset is exactly the historical unbounded behaviour, so no existing consumer
changes. An unparseable value **fails closed** (`exit 2`) rather than silently
degrading to the unbounded window it was set to prevent.

**Claim semantics (`LK_SLOT_CLAIM`):** by default the guard claims the window
**up front** — it writes the marker *before* the job body runs, so a long run
isn't double-started by the next tick, a crash does **not** retry until the next
window, and an overlapping tick is suppressed at the guard. Set
`LK_SLOT_CLAIM=on-success` to **defer the claim** to an EXIT handler that fires
only on a clean (exit `0`) run: a **failed** run then leaves the window open and
the next launchd tick retries it. This is the claim-on-success poller pattern
(e.g. `serotine-tripwire`, which must retry on tomorrow's tick rather than burn
the whole quarter on a transient `gh`/network error). In `on-success` mode call
`lk_single_lock` **before** `lk_slot_guard` so a lock-skipped overlapping tick
can't reach the guard and consume the slot. Any unrecognized value falls back to
the safe `up-front` claim (with a stderr warning).

### Colors

Shared Discord brand-palette decimals (per [`discord-notify.md`](./discord-notify.md)):

| Var | Decimal | Meaning |
|---|---|---|
| `LK_COLOR_GREEN` | `5763719` | success / all-clear |
| `LK_COLOR_YELLOW` | `16705372` | warning / attention |
| `LK_COLOR_RED` | `15548997` | failure / error |
| `LK_COLOR_BLURPLE` | `5793266` | neutral / info (default) |

## Copy-paste wrapper example

A weekly job that runs a skill headless and posts its summary — the whole
boilerplate is now four function calls:

```bash
#!/usr/bin/env bash
# Example wrapper — invoked by launchd.
set -uo pipefail

export TOOL_NAME="my-sweep"
source "$HOME/claude-toolkit/launchd-kit/lib.sh"

launchd_kit_init                 # pin env, open $LOG
lk_enable_crash_alert            # red Discord ping on non-zero exit
lk_slot_guard weekly:sat 08:00   # skip unless it's the week's first tick after Sat 08:00
lk_single_lock                   # never overlap another run

SUMMARY_FILE="$LOG_DIR/last-summary.txt"; rm -f "$SUMMARY_FILE"

claude -p "/my-skill  (unattended: write the summary to $SUMMARY_FILE)" \
  --dangerously-skip-permissions >>"$LOG" 2>&1
EXIT=$?
lk_log "claude exit: $EXIT"

# The wrapper owns Discord delivery so it survives the headless session ending early.
if [[ -s "$SUMMARY_FILE" ]]; then
  COLOR=$LK_COLOR_GREEN
  grep -qiE 'fail|error|escalat' "$SUMMARY_FILE" && COLOR=$LK_COLOR_YELLOW
  lk_notify "My Sweep — $(date +%Y-%m-%d)" "$COLOR" <"$SUMMARY_FILE"
fi

lk_rotate_logs
exit $EXIT
```

If the script exits non-zero anywhere above, `lk_enable_crash_alert` posts the
red "run failed" embed automatically — you don't wire that per-branch.

## Retrofit guide (migrating an existing wrapper)

The live `$HOME/.claude/scripts/*-run.sh` wrappers run on schedule and are not yet
version-controlled — **do not hot-edit them.** Retrofit is a deliberate,
separate step (pairs with the `new-project` / new-automation skill). When you do
migrate one, this is the mechanical mapping:

| Hand-rolled block in the wrapper | Replace with |
|---|---|
| `export PATH=…`, `export HOME=…`, `export TZ="America/New_York"`, `LOG_DIR=…; mkdir -p`, `STAMP=…; LOG=…` | `export TOOL_NAME="<slug>"` + `source …/launchd-kit/lib.sh` + `launchd_kit_init` |
| The `--- Weekly/slot idempotency + timing guard ---` block (marker + `date -v-sat` + epoch compare + `exit 0`) | `lk_slot_guard weekly:sat 08:00` (or `daily …` / `slots:8,12,16,20`) |
| The `--- Single-run lock ---` block (stale-lock `mkdir` + `trap 'rm -rf' EXIT`) | `lk_single_lock` (default 120-min stale steal) |
| The final `if [[ $EXIT -ne 0 ]]` → tail-the-log → `"$NOTIFY" "… run failed"` branch | `lk_enable_crash_alert` (call it right after `launchd_kit_init`) |
| A success-path `"$NOTIFY" "Title" "$COLOR" <summary` | `lk_notify "Title" "$COLOR" <summary` |
| The trailing `find "$LOG_DIR" -name '*.log' -mtime +60 -delete` | `lk_rotate_logs` |

Behavioral parity notes when retrofitting:

- **`<TOOL>_FORCE` env var:** the existing wrappers each use their own name
  (`DEPENDABOT_SWEEP_FORCE`, `HOMELAB_SWEEP_FORCE`, `AI_TASKS_FORCE`, …). The
  guard derives `<TOOL>_FORCE` from `TOOL_NAME`, so `dependabot-sweep` keeps
  `DEPENDABOT_SWEEP_FORCE` for free — but `ai-coding-tasks` would become
  `AI_CODING_TASKS_FORCE` (it was `AI_TASKS_FORCE`). Either update the operator
  habit or keep a one-line `AI_CODING_TASKS_FORCE=${AI_TASKS_FORCE:-0}` shim.
- **Marker filename:** the kit uses one `.last-run-window` marker regardless of
  interval; the old wrappers used `.last-sweep-week` / `.last-run-slot`. A
  retrofit resets the window once (harmless: at worst one extra run).
- **Slot semantics:** `ai-coding-tasks` claimed its slot marker only *after*
  acquiring the lock (so a lock-blocked tick didn't burn its slot). The kit
  claims the window in `lk_slot_guard`, before `lk_single_lock`. In practice a
  lock-blocked tick is rare and the next window self-heals; if you need the old
  ordering for a high-frequency job, keep that job's bespoke guard.
- **Claim-on-success pollers (`serotine-tripwire` / `macrovoices-fetch` class):**
  by default `lk_slot_guard` claims the window **up front, on the first tick** —
  it assumes that tick either succeeds or is fine to skip until the next window. A
  job that must *retry every tick within a window until it actually succeeds*
  (e.g. `serotine-tripwire`, which must not burn the quarter on a transient `gh`
  error; or `macrovoices-fetch`, which re-polls until the transcript PDF is really
  published — an early tick hits a "check back later" stub) needs
  claim-on-*success* instead of claim-on-*attempt*. **Set
  `LK_SLOT_CLAIM=on-success`** and the guard defers the marker write to an EXIT
  handler that fires only on a clean exit, so a failed tick leaves the window open
  and the next tick retries — no bespoke quarter/week marker needed. Call
  `lk_single_lock` **before** the guard in this mode (a lock-skipped overlapping
  tick would otherwise reach the guard and, on its own clean exit, consume the
  slot). Two caveats that `on-success` alone does **not** solve, so a sub-daily
  within-window poller like `macrovoices-fetch` may still keep a bespoke guard:
  (a) `launchd_kit_init` opens a fresh timestamped `$LOG` on every call, so a
  gate placed *after* init litters the log dir with empty per-tick logs — run the
  no-op check before init if empty logs matter; (b) use `lk_notify_strict` (not
  `lk_notify`) if the success marker must be gated on Discord actually accepting
  the post. `serotine-tripwire` (a once-per-quarter daily-tick job) hits neither
  caveat and migrates cleanly to `lk_slot_guard quarterly … ` + `LK_SLOT_CLAIM=on-success`.

## The Discord notifier

[`discord-notify.sh`](./discord-notify.sh) is the version-controlled copy of the
machine-local `$HOME/.claude/scripts/discord-notify.sh` — the single point every
scheduled job's notify path runs through. The contract is
[`discord-notify.md`](./discord-notify.md); this file must stay faithful to it.

```
bash launchd-kit/discord-notify.sh [--content "<text>"] "Title" [color-decimal] < body-on-stdin
echo "all clear" | bash launchd-kit/discord-notify.sh "Dependabot Sweep" 3066993
```

The Discord embed description hard-limit is 4096 chars; bodies longer than
`MAX=3900` are truncated with a `…(truncated — see local log for full output)`
marker. A non-2xx HTTP response prints `discord-notify: HTTP <code>` and exits
non-zero. The webhook URL and any resolved secret value are **never** logged or
echoed — not on success, not on any error path.

### `--content` — the only way to actually ping someone

**An embed can never mention anyone.** Discord parses `<@…>` mentions only in
the top-level `content` field; a `<@123456789012345678>` placed in an embed
title or description renders as inert text and notifies nobody. Any caller that
needs a human to *get pinged* must therefore use `--content`:

```
bash launchd-kit/discord-notify.sh --content '<@123456789012345678> **NEEDS EXAMPLE** — approve?' \
  "Needs Example" 16705372 < detail-on-stdin
```

- **Additive.** The two positionals keep their meaning and order; every
  pre-existing call shape emits a byte-identical payload (CI asserts this).
  `--content=<text>` works too, and the flag may appear anywhere in the args.
- **Cap 1900 chars.** Discord's hard limit is 2000; overflow is **truncated**
  with a `…(truncated — see local log for full output)` marker (counted inside
  the cap, so the emitted content is always ≤ 1900), never rejected — a
  shortened ping still reaches its human, a rejected one silently does not.
  Truncation is from the tail, so a mention placed at the START of the content
  can never be cut into a broken `<@…` fragment.
- **`allowed_mentions` is pinned to `{"parse":["users"]}`** whenever `--content`
  is used, so interpolated text can ping a *person* but can never fire
  `@everyone` / `@here` or a role ping.
- With `--content` and no stdin body the payload is **content-only** (no empty
  embed). With no `--content` and no body the original `(no content)` embed is
  unchanged.

### Webhook resolution order

1. `$DISCORD_WEBHOOK_URL` if already exported.
2. The channel's `webhook` in `notify.json` (the channel `--channel <name>`
   names, else `$NOTIFY_CHANNEL`, else `default`). A file that breaks the
   schema, or a missing channel, exits `2`. Schema:
   [`scripts/notify-sink.md`](../scripts/notify-sink.md).
3. Only while `notify.json` is absent, for one release: the env file at
   `$WEBHOOK_ENV_FILE`, else the legacy env file in the owner's secrets folder, parsed
   as data (never sourced; only `DISCORD_WEBHOOK_URL` is read), with a
   deprecation line on standard error.

If none yields a value, it prints `discord-notify: no DISCORD_WEBHOOK_URL
configured` and exits non-zero.

### 1Password (`op://`) references

A 1Password-migrated env file sets the webhook to an `op://` reference instead
of a literal URL, e.g.:

```bash
# $WEBHOOK_ENV_FILE (op-migrated)
DISCORD_WEBHOOK_URL="op://YourVault/discord-webhook/url"
```

After resolution, if `DISCORD_WEBHOOK_URL` **literally starts with `op://`** the
notifier resolves it with `op read`. This is strictly backward-compatible: a
plaintext env file (literal URL) never touches the op path and behaves
byte-identically to the pre-op contract.

**`op read` is bounded, not bare.** `op` has no built-in timeout and can hang
indefinitely under the macOS TCC app-data gate (the desktop app's container
probe blocking under display-sleep). The `op read` call is routed through
`_lk_timeout` (90s bound, 2 attempts — see `_lk_timeout`'s own doc comment in
`lib.sh` for the fallback chain), so a hung `op` fails fast with the same
distinct stderr message and non-zero exit as any other op failure, instead of
wedging the notifier — and the launcher parked on it — forever.

The `op` service-account token is sourced in this order:

1. `$OP_SERVICE_ACCOUNT_TOKEN` from the environment if set.
2. Otherwise the macOS Keychain item `op-service-account-token`
   (`security find-generic-password -s op-service-account-token -w`), exported
   as `OP_SERVICE_ACCOUNT_TOKEN` before calling `op`.

**Distinct op-failure mode.** Per the 1Password secrets decision, "op failed" is a new failure mode
that must be distinguishable from a webhook/HTTP failure. When the `op` binary
is missing, no token is available, or `op read` cannot resolve the reference,
the notifier prints a message prefixed `discord-notify: 1Password resolution
failed (op) — …` and exits non-zero **before** any HTTP request — so a
1Password outage never masquerades as a Discord/webhook error.

### Smoke test

[`test-discord-notify.sh`](./test-discord-notify.sh) exercises the notifier with
stubbed `curl`, `op`, and `security` on `PATH` (no network, no real 1Password):
it proves the plaintext literal-URL path calls `curl` unchanged, an `op://`
value triggers `op read` (via the Keychain token fallback) and uses its output,
an `op` failure produces the distinct stderr message and non-zero exit, a hung
`op` (stubbed as a 60s sleep) fails fast via `_lk_timeout` instead of wedging
the notifier, and the webhook URL never appears on stdout. Run it with `bash
launchd-kit/test-discord-notify.sh`. `_lk_timeout` itself (all four fallback
tiers, including the pipe-holding-open edge case) is covered separately in
[`test-lib.sh`](./test-lib.sh).

It additionally covers `--content`: that a no-flag call is byte-identical to the
pre-flag payload, that `--content` lands in the top-level `content` field (never
the embed) with `allowed_mentions` pinned, that a 1901-char input truncates to
≤ 1900 with a marker while 1900 passes through untouched, and that both flag
forms plus a missing value behave.

## `notify-owner.sh` — the blocking-ask escalation ping

[`notify-owner.sh`](./notify-owner.sh) is the session-autonomy-recalibration decision's § Escalation channel:
*"Blocking or time-sensitive items: send the harness push notification, and once
the owner webhook is provisioned, a Discord ping. Never hold a session
open waiting."*

It fires **only** on genuinely blocking / time-sensitive asks. It is **not** a
session-close notifier — everything non-blocking waits for the session-close
NEEDS-YOU banner. A ping that isn't actionable-now is a defect.

```
notify-owner.sh "<the ask>" [--why "<what it unblocks>"] [--effort "<minutes>"] \
                            [--default "<if unanswered>"] [--source "<from>"] \
                            < optional-detail-on-stdin
```

It composes the NEEDS-YOU shape into an embed and hands the mention-bearing line
to `discord-notify.sh --content`. Detail on standard input has a 5-second read
limit; callers should close the pipe after writing. The whole call has a
40-second deadline, including the notifier and its kill grace.

### Destination

1. `$NOTIFY_OWNER_WEBHOOK_URL` (tests / overrides).
2. `channels.owner.webhook` (and the optional `channels.owner.mention`) in
   `notify.json` — normally an `op://` reference resolved downstream by
   `discord-notify.sh`. Schema: [`scripts/notify-sink.md`](../scripts/notify-sink.md).
3. Only while `notify.json` is absent, for one release: `DISCORD_WEBHOOK_URL`
   and `OWNER_DISCORD_ID` (or the legacy owner-id key) from
   `$NOTIFY_OWNER_ENV_FILE` (default: the unchanged legacy owner env file
   in the owner's secrets folder), parsed as data (never sourced), with
   a deprecation line on standard error.

The resolved webhook deliberately **overrides** any ambient
`DISCORD_WEBHOOK_URL`, and with no source configured the script exits `2`
rather than falling back to whatever webhook happens to be exported: a blocking
ping landing in an unrelated job's feed is a delivery *failure*, not a
delivery.

### The mention target (`OWNER_DISCORD_ID`)

`OWNER_DISCORD_ID` must be Discord's **numeric user id** (a "snowflake"), e.g.
`123456789012345678` — **not** the `@username`. Discord's `<@…>` syntax only
resolves numeric ids; `<@example-mail>` renders as literal text and pings nobody.

To get it: **Discord → Settings → Advanced → enable Developer Mode**, then
right-click your own name → **Copy User ID**. Put it in the env file:

```bash
# the legacy owner env file ($NOTIFY_OWNER_ENV_FILE)
DISCORD_WEBHOOK_URL="op://Automation/Claude Code Discord Webhook/webhook"
OWNER_DISCORD_ID=123456789012345678
```

The id is **not** a secret — it is visible to anyone who can read a message you
send — so it lives as a plain value, not a 1Password field.

Degradation is explicit and tested:

| `OWNER_DISCORD_ID` | Behavior |
|---|---|
| valid numeric | `<@id> **NEEDS EXAMPLE** — …` — a real ping |
| unset / empty | ping still sends, **no mention**, no `<@` fragment anywhere in the payload |
| a username, an `op://` ref, any non-numeric | **refused** — loud stderr warning naming the Developer-Mode fix, ping still sends with no mention, and the offending value is never echoed back or placed in the message |
| pasted `<@123…>`, `<@!123…>`, or padded | normalized to one valid mention |

### Renamed from the legacy escalation shim

For one release the legacy escalation script stays as a
shim: it prints one deprecation line on standard error, then runs
`notify-owner.sh` with the same arguments, standard input and exit code. If
the sibling `notify-owner.sh` is missing it exits `3` with `notify_owner_missing`
and sends nothing. The variables follow: `NEEDS_ANDREW_*` is read as
`NOTIFY_OWNER_*` and `ANDREW_DISCORD_ID` as `OWNER_DISCORD_ID`, the new name
winning when both are set. The shim and the old names go together.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | ping delivered |
| `2` | usage error (no ask, or no notifier on the box supports `--content`) |
| `3` | delivery failed (no webhook configured, 1Password failure, non-2xx HTTP) or a deadline expired; the reason is printed on stderr |

Delivery is **fail-fast with no retry loop** — this runs inside live sessions
and hooks, and a retrying escalation that wedges the thing it is escalating from
is worse than a loud failure. `discord-notify.sh` already bounds curl (10s
connect / 20s total) and the `op` read.

Notifier resolution checks `$NOTIFY_SCRIPT`, this script's sibling, then
`$HOME/.claude/scripts/`, then `$HOME/claude-toolkit/launchd-kit/` — and **verifies each
candidate supports `--content`** before using it. A stale machine-local mirror
would otherwise swallow the flag as a positional title and send a
silently-muted escalation; if no candidate supports it, the script refuses
(exit `2`) instead.

### Smoke test

[`test-notify-owner.sh`](./test-notify-owner.sh) stubs `curl` and runs against a
temp env file (never the owner's secrets folder). It proves the exact rendered mention,
the clean unset-id degradation, the username/`op://`-ref refusal, the exit-3
delivery failure, the no-ambient-fallback destination guard, the stale-notifier
refusal, and that the webhook never reaches stdout or stderr. Run it with
`bash launchd-kit/test-notify-owner.sh`.

## `owed-flush.sh` — denial summary

[`owed-flush.sh`](./owed-flush.sh) sends one informational notice when a
scratch directory's `owed.log` contains denied actions, then renames the file
to `owed-<UTC timestamp>.log`. Empty or absent logs do nothing. A delivery
failure leaves `owed.log` in place for the next attempt.

```
owed-flush.sh <scratch> [--source <name>]
```

The source defaults to the scratch directory's basename. The notice includes
the `owed.log` path as detail on standard input to `notify-owner.sh`, uses
`--fyi`, and reports the number of non-empty records.

Each `owed.log` record is one LF-terminated line with exactly three
TAB-separated fields:

```
<UTC ISO-8601 timestamp with Z>	<reason>	<first 200 bytes of the command>
```

Run the offline test with `python3 -m pytest launchd-kit/test_owed_flush.py -q`.
It injects a recording notifier through `OWED_FLUSH_NOTIFIER`; it never calls
the real notifier or the network.

## `continuation.py` + `herald-watch-run.sh` — the continuation transport

The deterministic half of the feedback broker's **park-as-data + resume-on-answer**
contract (feedback-broker design contract § 3). A
session pitches an ask through herald, stamps the parked row, and **closes**.
When Example taps an option, herald writes an answer envelope into
`$HOME/.herald/answers/`; this subsystem decides whether that answer may resume the
row, and is the only thing allowed to say yes.

> ### NOTHING HERE IS ARMED
> `herald-watch-run.sh` ships **inert**. No plist is in this repo, `install.sh`
> does not place it, and no code path calls `launchctl` (both suites assert
> this, statically and with a PATH tripwire). Arming the watcher is the **R7**
> six-condition gate in the brief — Example's hand, one supervised sitting.
> Until then the same `continuation.py` is reachable the safe way: a scheduled
> session runs `scan` + `consume --consumer session` at its own boundary, which
> is the ratified park-and-carry path and needs no daemon.

### The five constraints and where each one lives

| § 3 constraint | Implementation | Refusal codes |
|---|---|---|
| 1. Typed continuation registration | `load_registration` — `{ask id, row key, repo, cwd, session id, expiry, allowed verbs}`, read back with `O_NOFOLLOW` + strict owner/mode. Invalid, stale or unbound → **park**, never a generic spawn. | `registration_unbound`, `registration_malformed`, `registration_symlink`, `registration_mode`, `registration_cwd_missing`, `ask_id_malformed` |
| 2. Validated answer envelope | `validate_envelope` — allowlisted Discord identity + channel, the pending ask id, the interaction-id nonce, an option the ask **actually pitched** (read from `asks/`), and the message id cross-checked against herald's own **pitch receipt** (`pitches/<ask-id>.json`, written at post time — independent of the tap). A hand-forged file fails and **alerts**. | `identity_not_allowlisted`, `channel_not_allowlisted`, `identity_allowlist_unset`, `envelope_symlink`, `envelope_mode`, `answers_dir_mode`, `asks_dir_mode`, `envelope_ask_mismatch`, `option_not_pitched`, `interaction_id_malformed`, `envelope_clock_skew`, `envelope_predates_ask`, `pitches_dir_mode`, `pitch_record_unbound`, `pitch_record_symlink`, `pitch_record_malformed`, `message_id_mismatch` |
| 3. Exactly-once across racing consumers | `CasStore` — `pending → claimed(answer_hash, continuation_id) → acted`, one durable CAS per ask plus a per-row lease, shared by the watcher **and** any scheduled session. The CAS is an `os.link` into `<ask>.v<N+1>.json`: atomic, fails `EEXIST`, so exactly one racer advances the row. The state read itself is `O_NOFOLLOW` + ownership/mode-checked, same as every other read here. | `acted_hash_mismatch`, `lease_lost` |
| 4. Tap vs text | The body-kind check: only a lone `answer` body may drive action. A `note` in `answers/` is refused, not read. | `envelope_body_kind` |
| 5. Least-privilege continuation seat | `ALLOWED_VERBS` (spec drafts, ticket writes, SoT `QUESTIONS:`/`DECISIONS:` lines, re-pitches, plus Stage 1's `stamp-receipt`) and a pinned, never-Fable model. | `verb_not_allowed`, `model_forbidden` |

The **trust boundary is the file, not the writer**: every check that matters is
on the read side, because the threat is a planted file rather than a buggy
caller. Nothing is signed — introducing key material would be new secret
handling this subsystem is not authorised to invent — so integrity rests on
ownership, mode and symlink refusal, the same posture herald's own store takes
(0600 records in 0700 directories). Every directory check hands its still-open
fd to the file open inside it (`dir_fd=`), so a parent directory swapped for a
symlink between the two cannot redirect the read — `O_NOFOLLOW` on the leaf
name alone only refuses a symlinked leaf, not a symlinked parent.

### Exit codes (what the wrapper turns into Discord colors)

| Code | Meaning | Discord |
|---|---|---|
| `0` | acted — this consumer won the CAS and the payload succeeded | green summary |
| `2` | usage/config error (unset allowlist, missing helper) | red |
| `3` | **refused** — registration or envelope failed validation | red |
| `4` | yielded — no answer, another consumer holds the lease, or already acted | silent |
| `5` | parked — TTL lapsed; degrades to next-session pickup, nothing lost | silent |
| `6` | breaker — the chain cap tripped | yellow |
| `7` | payload failed — claimed, ran, came back non-zero | yellow |

### Defaults (from the brief's § 10 Q2, accepted as proposed)

- **Chain cap: 3** continuations per ask **per day** (`LK_CONTINUATION_CHAIN_CAP`).
  A tripped breaker parks the row and recovers only when the day rolls —
  deliberately the sole automatic recovery, since a breaker a consumer can reset
  is not a breaker.
- **Lease: 900s** (`LK_CONTINUATION_LEASE_SECS`). Long enough for a real headless
  continuation, short enough that a crashed holder cannot wedge a row.
- **Clock skew tolerance: 300s** (`LK_CONTINUATION_SKEW_SECS`) on envelope times,
  which come from herald's clock, not ours.
- **TTL** is the registration's own `expiresAt`. Inside it the watcher may
  auto-continue; past it the watcher parks and a scheduled **session** consumer
  picks the row up — through the same CAS, so exactly-once survives the handover.
  The TTL is rechecked on a FRESH clock read immediately before the payload is
  allowed to start (not just at claim time), and a real (`--payload-cmd`)
  payload is capped at the remaining budget via `subprocess.call(timeout=...)`
  — a watcher that wins the CAS a moment before the boundary cannot go on to
  run, or keep running, past it.

### State layout (`$LK_CONTINUATION_DIR`, default `$HOME/.claude/continuations`)

```
registrations/<ask-id>.json   written once by the pitching session, never rewritten
state/<ask-id>.v<N>.json      the CAS revision chain (kept: it is the row's audit trail)
receipts/<ask-id>.json        Stage 1's no-op payload output
alerts/<ns>-<code>-<hex>.json durable refusal records; the wrapper posts from these
armed/<ask-id>.json           a close-time KICKOFF registration, phase ARMED
ready/<ask-id>.json           the same record, published (renamed) by `ready`
claimed/<ask-id>.json         the same record again, claimed (renamed) by `claim-ready`
```

The `armed → ready → claimed` lane is the **kickoff** half, and it is a
different mechanism from the ask half above on purpose: a kickoff is not an
ask. There is no herald envelope, no answer hash, no lease and no CAS
revision — exactly-once is the filesystem's own property:

- `armed → ready` is one `os.rename`, and it is **refused outright if the id
  already sits in `claimed/`** — a spent kickoff id can never re-enter the
  ready lane, which is the only way a same-id record could come to exist in
  both lanes at once.
- `ready → claimed` is `os.link` + `os.unlink`, **not** `rename`: link is
  atomic *and* non-replacing, so "is this id already claimed?" and "take it"
  are one kernel operation and a claim record can never be overwritten by a
  second claimer, whatever the interleaving. The loser gets `ENOENT` (the
  source is gone) or `EEXIST` (the destination is taken) and exits 3.
- The claimer takes an **identity snapshot** (`O_NOFOLLOW`, inode + content
  digest) of the record before it builds an argv from it, and re-checks that
  identity before *and* after the move. A symlinked ready record is refused
  while it is still in `ready/`; a record replaced in the window between the
  read and the claim is refused with nothing moved. The record that gets
  consumed is provably the record the argv describes.

A crash after the claim loses that one run (re-mint recovers it) and can never
double-fire it: the claim is recorded before spawning. `claim-ready` stamps
`claimedAt`, the claimant's own argv and the kickoff argv it handed out under a
top-level `claim` key, leaving the `registration` body byte-identical to what
`ready/` held.

herald's own store additionally supplies `pitches/<ask-id>.json` — the delivery
receipt `ask-store.ts`'s `recordPitch` writes at POST time, read-only here, and
the state constraint 2's message-id cross-check is validated against:

```
```

### Usage

```bash
# the pitching session, before it closes
continuation.py register --ask-id "$ASK" --row-key BQ2 --repo owner/repo \
  --cwd "$PWD" --session-id "$SESSION" --expires-at 2026-08-31T09:00:00Z \
  --verb spec-draft --verb re-pitch --model claude-sonnet-5-5

# any consumer (the watcher wrapper, or a scheduled session at its boundary)
continuation.py scan                      # asks with a registration AND an answer
continuation.py consume --ask-id "$ASK" --consumer session
continuation.py state  --ask-id "$ASK"

# the kickoff lane (close-time continuity across a wave boundary)
continuation.py kickoff --ask-id "$ASK" ... --kickoff-type ROLL --harness omp
continuation.py ready       --ask-id "$ASK"   # armed/ -> ready/
continuation.py claim-ready --newest          # ready/ -> claimed/, prints the argv
```

`claim-ready` takes an ask id (positionally or as `--ask-id`) or `--newest`,
and prints one JSON object — `askId`, `harness`, `cwd`, `claimedPath`, `argv`
— on stdout. It **starts nothing**: the argv is a value, and the consumer that
execs it is `loop-chain-run.sh --exec`, which is itself gated on
`LOOP_CHAIN_EXEC=1` (both gates required; with the variable unset that wrapper
behaves exactly as it did before the exec path existed). A record it refuses
(SWEEP, CHART/MERGE, expired, malformed) is left in `ready/` unspent, because
the refusal happens before the claim.

The scheduled caller of that exec path is the daily queue-generation job:
`triage-wave-run.sh --wave` creates the queue, publishes its ROLL kickoff READY
and, as its step 4, runs `loop-chain-run.sh --exec` — inheriting the same
double gate, so without `LOOP_CHAIN_EXEC=1` the slot stops at READY and its
log says why. `triage-wave-run.sh --triage-only` is the 4h timer's shape
(report the bar, never mint — with one exception: on the literal verdict
`FIRE(priority)`, the urgent path, both it and the no-flag mode mint the
Priority-labelled ticket(s) alone via `triage_wave.py --write --priority-only`
and leave the kickoff READY for the serialized runner; the daily slot is never
involved). The slot's plist is a **template**,
[wave-slot plist template](./templates/)
(StartInterval 3600 + the 07:00–07:59 ET slot guard, shipped without the
`LOOP_CHAIN_EXEC` key) — `install.sh` does not place it and nothing here loads
it; the wrapper's header § MODES has the full contract, and
[`tests/test_triage_wave_run.py`](./tests/test_triage_wave_run.py) (pytest)
is the fixture-harness evidence for all three modes.

### Smoke tests

[`test-continuation.py`](./test-continuation.py) (72 tests, stdlib `unittest`) is
one class per seam — registration, envelope, CAS/lease, chain breaker, TTL,
the pitch-receipt cross-check, the parent-directory `dir_fd` anchoring, the
payload-time TTL recheck, and the CAS store's own read-side integrity — plus
an end-to-end positive control. The race is real rather than simulated: it
launches eight concurrent CLI processes against one answer behind a start
barrier and asserts exactly one acts and exactly one receipt exists.
[`test-herald-watch.sh`](./test-herald-watch.sh) covers the wrapper with a
stubbed notifier, a sandboxed `HOME`, and a `launchctl` tripwire on `PATH`.
Because the watcher is never armed, **these suites are the evidence** for the
brief's stage-1 acceptance — there is no live demo to fall back on.

## `elicitation.py` — the stage-2 elicitation semantics

Stage 1 decides **whether** a continuation may run. `elicitation.py` decides
**what** it is (feedback-broker design contract § 11 stage 2, § 3(D),
§ 10 Q3):

- a binding **tap** → the `claude --resume` argv for the branch that option
  declared, built from the typed stage-1 registration;
- free **text** → a **re-pitch** of the same binding election, never an action
  (§ 3.4, the predecessor rule that survives the new machinery).

> ### NOTHING HERE IS ARMED EITHER
> No plist, no `launchctl`, no install step, and `herald-watch-run.sh` is
> deliberately **not** wired to this module — the wrapper still calls stage 1's
> no-op payload. The CLI's `dispatch` is **plan-only unless `--exec`** is
> passed, so no default path through this file starts a model. Arming remains
> **R7's six-condition gate**: Example's hand, one supervised sitting.

### The three seams

| Seam | Module / interface | Refusal codes |
|---|---|---|
| 1. The branch declaration | `load_interview` → `Interview \| Refusal`. Its own write-once record beside the registration (widening the merged, security-reviewed registration schema for a field only stage 2 reads is not a trade this row takes). Depth is enforced **structurally** — a follow-up that declares children is refused for *being* a level-3 question. | `interview_unbound`, `interview_malformed`, `interview_symlink`, `interview_mode`, `interviews_dir_mode`, `interview_too_deep`, `interview_too_wide`, `interview_depth_mismatch`, `interview_ask_mismatch`, `verb_not_allowed` |
| 2. Tap vs text | `classify_response` → `tap \| text \| none`, then `dispatch_once`. **A tap outranks a note** (herald's own first-answer-wins, restated for the resume path). The text adapter provably cannot reach seam 3. | `note_symlink`, `note_malformed`, `note_body_kind`, `note_ask_mismatch`, `note_version`, `note_clock_skew`, `notes_dir_mode`, `identity_not_allowlisted`, `channel_not_allowlisted`, `interaction_id_malformed`, `message_id_mismatch` |
| 3. The argv builder | `build_resume_argv` → `list[str] \| Refusal` (a pure function: it returns a value, it runs nothing), plus `resume_payload` — the third adapter at the payload seam. The process runner behind it is injected, and the child is bounded by the lease (`lease_budget`). | `branch_not_declared`, `verb_not_allowed`, `model_forbidden`, `session_id_malformed`, `model_malformed`, `claude_bin_malformed`, `prompt_malformed`, `argv_shape`, `argv_forbidden_flag`, `interview_row_mismatch`, `interview_ask_mismatch`, `lease_lost`, `lease_missing`, `lease_unreadable`, `lease_budget_exhausted` |

**The invariant the whole file exists to hold:** no byte of Example's free text
ever reaches an argv. Every word of a resume prompt was declared when the ask
was pitched, *before any answer existed*; the tap only **selects** which
declared branch is used, and the tapped key is a member of a closed set stage 1
already checked against herald's own ask record. Text is preserved verbatim as
**data** on a re-pitch record — a thing a human reads, never an operand.

### Argv construction: allowlisted values, attached form

**Every interpolated value is format-validated before it reaches the argv.**
`session_id` must match `SESSION_ID_RE` (a canonical uuid — what real session ids
actually are) and `model` must match `MODEL_RE` (alphanumeric first character,
then `[A-Za-z0-9._-]` plus an optional bracketed variant suffix). Both anchor on
an alphanumeric first character, so **no interpolated value can begin with `-`**.
This is the fix for the option-injection finding in the PR's second review:
`FORBIDDEN_ARGV_FLAGS` and `--disallowedTools` inspect the positions the builder
writes, never what a field's *content* could masquerade as once emitted, so a
`sessionId` that rendered as `--dangerously-skip-permissions` would have been
handed straight to the CLI's own "value or next flag?" judgement.

**Every option is emitted attached** (`--flag=value`), so no free-standing value
token exists anywhere in the argv: nothing can be split-parsed, and
`--disallowedTools <tools...>`'s variadic appetite cannot reach the prompt.
Verified against the installed CLI rather than assumed —
`--output-format=NOTAFORMAT` and `--output-format NOTAFORMAT` produce commander's
identical `argument ... is invalid` rejection, which only happens if the attached
form is really parsed. The resulting invariant is *enforced in the builder* and
pinned by tests: every element between the binary and the prompt starts with
`-`, and the prompt is always last.

The builder never emits a permission-elevating flag (`FORBIDDEN_ARGV_FLAGS`,
scanned over the built argv, not merely tested), and passes `--disallowedTools`
for the classes § 3.5 keeps on the trust ladder (merge, `gh pr merge`,
`launchctl`, `op`, `security`) — branch pushes are permitted (NY-123, elected
2026-09-11). The deny list is defense in depth; the **gate** is the
registration's verb allowlist, checked before an argv exists.

### Exactly-once, both paths

- **Tap** — stage 1's CAS, unchanged and *not* re-implemented: the resume runs
  inside `run_once`'s payload seam, so the lease, the chain breaker and the TTL
  are the same ones. A second tap on a consumed ask is `already_acted`.
- **The child is bounded by the LEASE, not the TTL** (`lease_budget`). Stage 1
  sized its lease for a payload that finishes in milliseconds and bounded the
  child by the TTL (hours) for a watcher and by *nothing* for a scheduled
  session. Stage 2's payload is a whole headless model session, so a resume
  outliving the lease let `CasStore.claim` hand the row to the next consumer
  while the first child was still alive — **two live `claude --resume` processes
  for one ask**, which the PR's second review found and a threaded test
  reproduced. `finish()` catches it afterwards (`lease_lost`), but detecting a
  double-act is not the guarantee. The child now dies at
  `lease_expires_at - LEASE_SAFETY_MARGIN_SECS` (30s), never later, for every
  consumer. **Named consequence:** a continuation cannot outlive its lease, so
  work needing longer needs a larger `LK_CONTINUATION_LEASE_SECS` — or lease
  renewal, which is real machinery and is not this row's.
- **Text** — the re-pitch record is keyed by `(ask, note hash)` and written
  write-once. It is deliberately **not** the ask's CAS row: a note must not
  consume the ask, or a tap arriving afterwards would find the row already
  acted and text would have destroyed the binding election it was supposed to
  prompt for.

### State layout (added to stage 1's)

```
interviews/<ask-id>.json          the branch declaration, write-once
repitches/<ask-id>.<hash>.json    what text earned: the same election, again
```

### Usage

```bash
# the pitching session, alongside `continuation.py register`
elicitation.py declare --ask-id "$ASK" --row-key BS11 --from-file tree.json

# see exactly what a tap would run, without running it
elicitation.py argv --ask-id "$ASK" --option-key now

# plan-only (the default); add --exec to actually continue
elicitation.py dispatch --ask-id "$ASK" --consumer session
```

### Smoke tests

[`test-elicitation.py`](./test-elicitation.py) (99 tests, stdlib `unittest`) is
one class per seam plus an exactly-once class, an argv-injection class, a
lease-bound class (both from the PR's second review) and a nothing-is-armed
class. It
reuses `test-continuation.py`'s herald fixtures rather than forking them — that
on-disk shape is security-relevant, and a second divergent copy is how a suite
starts proving something the real store no longer does. No test starts a real
`claude`: the runner is injected everywhere, and a `claude` tripwire on `PATH`
asserts the in-process dispatch **and** both CLI subcommands leave it
untouched. As with stage 1, **this suite is the evidence** — § 11 stage 2's
"pitched and answered from the phone" half needs an armed watcher, which is
R7's gate and out of scope.

## `records.py` — the stage-3 record writes

Stage 1 decides **whether** a continuation may run; stage 2 decides **what** it
is. `records.py` is what a completed continuation **leaves behind** — the first
writer behind the write scope `continuation.py:51` has declared since stage 1
("spec drafts, ticket writes, SoT `QUESTIONS:`/`DECISIONS:` lines") and the
`ticket-write` verb (`:160`) has allowlisted with nothing behind it.
Feedback-broker design contract § 11 stage 3.

Given one completed elicitation it writes four destinations in one step:

| # | destination | how | idempotent because |
|---|---|---|---|
| 1 | the **ticket** | the `ai-coding-tasks` skill's own `notion_tasks.api`, `POST /comments` | a journaled `done` marker; an interrupted write is resolved by reading that page's own thread |
| 2 | the **SoT** | one append-only reference line on the pointed row's `QUESTIONS:`/`DECISIONS:` field | the line already carries the record hash |
| 3 | the **ledger** | a write-once stamp under `<root>/ledger/` | create-once, keyed on the record hash |
| 4 | the **receipt** | `$HOME/.herald/receipts/<askId>.json`, `wx` | create-once, keyed on the record hash |

Every write is keyed on **(ask id, record hash)**, so a second run on the same
completed elicitation is a no-op. The receipt is written **last** on purpose: it
is the signal herald reads to render an ask "answered-and-acted" in-channel, and
a receipt that could exist before the SoT line did would put a claim in the
channel the SoT does not carry.

**Nothing is armed and nothing loads it.** No plist, no `launchctl`, no
`subprocess` at all, and `herald-watch-run.sh` is deliberately not wired to it —
a test asserts the wrapper never names this file, and another asserts nothing
else in the kit does either. The CLI is plan-only unless `--exec`.

**Scope.** The SoT file must be a real, non-symlinked, regular file **inside the
registered cwd**, and the row comes from the **registration**, not from the
completion record — neither can be retargeted by whoever wrote the completion.
The herald path is computed from the ask id alone; `answers/` and `notes/` are a
different loop's sink and this module neither reads, writes nor names them. The
ticket client is wrapped so the only requests that can leave the module are an
additive comment on the declared page and a read of that same page's thread —
`PATCH /pages/<id>`, `DELETE` and any database call are unreachable rather than
merely unused.

### State it writes

```
completions/<ask-id>.json               the completed elicitation, write-once
records/<ask-id>.<hash>/ticket.*.json   the Notion write journal (begin/done)
ledger/<ask-id>.<hash>.json             the ledger stamp, create-once
$HOME/.herald/receipts/<ask-id>.json    the herald receipt, create-once (`wx`)
```

### Usage

```bash
# the resumed session, once it has drafted the spec
records.py complete --ask-id "$ASK" --from-file completion.json

# what would be written, writing nothing
records.py plan --ask-id "$ASK"

# plan-only (the default); add --exec to actually write the record
records.py record --ask-id "$ASK" --consumer session --exec
```

### Smoke tests

[`test-records.py`](./test-records.py) (96 tests, stdlib `unittest`) is one class
per seam plus an end-to-end class and a nothing-is-armed class. **No live
Notion**: the ticket client is injected everywhere, and a socket tripwire proves
a full end-to-end record write opens none. It reuses `test-continuation.py`'s
herald fixtures and `test-elicitation.py`'s source-inspection helpers rather than
forking either. As with stages 1 and 2, **this suite is the evidence** — the
in-channel half of § 11 stage 3's sentence is herald's, a different repo.

## `op-resolve.py` — pure-HTTP `op://` resolver

[`op-resolve.py`](./op-resolve.py) is a **stdlib-only** (urllib/json/os/argparse —
no third-party imports) drop-in replacement for `op run --env-file` that resolves
`op://` references against a self-hosted 1Password **Connect** REST server over
plain HTTP. It never invokes the `op` binary.

### Why it exists (launchd + LNP + TCC)

Under launchd the `op` binary is unusable for two structural reasons that
compound:

1. **macOS TCC AppData gate.** `op` probes the 1Password *desktop-app* container
   to satisfy the "access data from other apps" gate. When the display is asleep
   that probe does not fail fast — it **hangs**, releasing only on display-wake.
   A scheduled job parked on it stalls for the whole sleep window.
2. **macOS Local Network Privacy (LNP).** Pointing `op` at the Connect server on
   the LAN doesn't help: a launchd-spawned `op` is blocked by LNP from reaching
   the LAN (`no route to host`), and `op` cannot be granted Local-Network access
   through the GUI.

A **pure-Python** resolver clears both at once: Python already holds the LNP
grant, and talking to Connect's REST API directly never touches the desktop-app
container, so the TCC AppData gate never engages.

### Why stdlib `urllib`, not `connect-sdk-python`

The official `connect-sdk-python` would add a third-party dependency to **every**
wrapper venv on the fleet. Since this resolver is meant to be dropped in front of
arbitrary jobs, it deliberately depends on nothing outside the standard library.

### Client env contract

The host is read from the environment. The token is read from the environment
when present, otherwise the resolver falls back to the macOS Keychain item
`op-connect-token`:

| Var | Purpose |
|---|---|
| `OP_CONNECT_HOST` | Base URL of the Connect server, e.g. `http://nas.local:8090`. |
| `OP_CONNECT_TOKEN` | Connect API token, sent as `Authorization: Bearer …`. **Never logged.** |

### Usage

```bash
op-resolve.py --env-file "$HOME/.claude/secrets/<job>.env" -- <cmd> [args...]
```

It reads a dotenv-style env file (`KEY=value`; `#` comments and blank lines
ignored; `export KEY=value` and one layer of surrounding quotes tolerated),
resolves every value that literally starts with `op://` against Connect, merges
the resolved values over the current environment, and `execvpe`s the child — so
signals and the exit code pass through cleanly and secrets live only in the
child's environment, never on stdout. A command after `--` is **required**;
there is no "print the resolved values" mode (that would leak secrets).

Reference syntax: `op://<vault>/<item>/<field>`, with an optional section
(`op://<vault>/<item>/<section>/<field>`). Vault and item may be given by name
**or** UUID. Fields match on `label` or `id`, plus the well-known
`password`/`username`/`notes` purposes.

Resolution walks the Connect REST API:
`GET /v1/vaults` (name → id) → `GET /v1/vaults/{id}/items` (title → id) →
`GET /v1/vaults/{id}/items/{id}` (extract the field).

### Fail-closed guarantees

- Any unresolved reference, non-2xx HTTP, missing `OP_CONNECT_HOST`, token absent
  from both the environment and Keychain, or timeout ⇒ a clear message on
  **stderr** and a **non-zero exit**. A present-but-unavailable Keychain item is
  reported distinctly from an absent token.
- The child is **never** exec'd with partially-resolved secrets: if any one ref
  in the file fails, the whole run aborts before exec.
- Each HTTP call has a ~10s timeout with **one** retry on timeout / transient
  (5xx) errors; 4xx (e.g. 404) fails fast.
- Secret **values** never appear in logs, stderr, argv, or exception text.
  Failures name the reference (`op://vault/item/field`) and the env `KEY` only.

### Where consumers find `op-resolve.py`

The copy in this directory is **canonical**. Consumers, though, cannot depend on
a checkout: a launchd job's `ProgramArguments` has to name a path that stays
valid when this repo is moved, renamed, or simply not there. That stable path is
the **mirror**:

```
$HOME/.claude/scripts/op-resolve.py
```

One lookup order, used everywhere:

1. `$OP_RESOLVE_PY`, when it is set and names an existing file — the override
   that every test suite uses to point at a stub;
2. the mirror at `$HOME/.claude/scripts/op-resolve.py`;
3. the checkout's own `launchd-kit/op-resolve.py`, for consumers that are
   *already* running from inside a checkout.

`triage-wave-run.sh` and the task client's `notion_tasks.py` implement all
three. `discord-notify.sh` deliberately stops at step 2 — it runs under launchd
from paths that are not a checkout, and its own last resort is the legacy
`op read` fallback rather than a repo copy. `review`'s `jev_client.py` is the
one consumer that does not use this order at all: it *imports* `resolve_ref()`
in-process rather than exec'ing the resolver, from `launchd-kit/op-resolve.py`
two directories above its own file. `review` now comes from the pinned skills
upstream (`skills-upstream.pin`; the file is
`.skills-upstream/skills/review/jev_client.py` in the toolkit checkout), and
that repository ships no `launchd-kit/`, so from there the import finds
nothing and the Jev path reports itself unavailable rather than loading this
checkout's resolver. Restoring it needs a lookup seam in the upstream
`jev_client.py` (for example the `$OP_RESOLVE_PY` / mirror order above).

### Installing the mirror (`install-op-resolve.sh`)

[`install-op-resolve.sh`](./install-op-resolve.sh) is the **only** thing that
writes the mirror. Everything else reads it. One writer is the point: a mirror
refreshed by hand drifts from the canonical source silently, and a
secrets-adjacent helper that has silently drifted is the worst thing to be
running under a job nobody is watching.

```bash
bash launchd-kit/install-op-resolve.sh --check            # read-only state report
bash launchd-kit/install-op-resolve.sh --install          # stage the canonical bytes
bash launchd-kit/install-op-resolve.sh --install --force  # replace a drifted mirror
```

| State | `--check` | `--install` |
|---|---|---|
| mirror absent | reports `missing`, exit 1 | stages the canonical bytes, exit 0 |
| byte-identical | reports `in-sync`, exit 0 | no-op (`already in sync`), exit 0 |
| **drifted** | reports `DRIFT` + both digests, exit 1 | reports `DRIFT`, then **refuses** (exit 1) unless `--force` |
| symlink / not a regular file / unreadable | refuses, exit 2 | refuses, exit 2 |

"In sync" means **byte identity** (`cmp`), not "close enough", and both reports
carry the two SHA-256 digests so the copy being looked at is never ambiguous.
The drift report is always emitted **before** anything is written, so a
replacement can never be discovered after the fact; with `--force` the drifted
copy is preserved as `op-resolve.py.drift-<UTC timestamp>` beside the mirror
before the canonical bytes are staged atomically over it. `--check` writes
nothing under any circumstances — not even the `$HOME/.claude/scripts` directory.

Applying it to a real machine is a **supervised** step, like every other change
in this section: run `--check` first, read the report, and only then
`--install`.

Tested by [`test-install-op-resolve.py`](./test-install-op-resolve.py) (stdlib
`unittest`), which runs every case against a throwaway `HOME` and asserts on
SHA-256 digests and on-disk bytes. Run it with
`python3 launchd-kit/test-install-op-resolve.py`.

### Adoption (supervised, one job per sitting)

This kit only **adds** `op-resolve.py`; it does **not** rewire any wrapper.
Migrating a live wrapper from `op run` (or `op read`) onto `op-resolve.py` is a
supervised, one-job-at-a-time change via the `migrate-launchd-job` pattern:
snapshot the wrapper/plist, swap `op run --env-file …` for
`op-resolve.py --env-file …`, gate on a diff review, then smoke-test in both the
session shell and the real launchd context (kickstart) before trusting it.

### Testing `op-resolve.py`

[`test-op-resolve.py`](./test-op-resolve.py) is a stdlib `unittest` suite — no
network, no pytest. It loads the hyphenated module via `importlib.util` and mocks
the HTTP layer (the module's `_http_get` seam, or `urllib.request.urlopen` for the
retry/timeout tests). It covers ref parsing, the env-file parser (comments,
`export`, quotes, embedded `=`), success resolution (by label, by purpose, with a
section, vault-by-UUID), missing vault/item/field, HTTP 404 (fail-fast) vs
timeout/5xx (one retry), passthrough of non-`op://` values, partial-failure
abort (child not exec'd), and that failure messages never contain the secret
value. Run it with `python3 launchd-kit/test-op-resolve.py`.

## `notion-webhook-receiver.py` — the Notion webhook prototype (INERT)

A stdlib `http.server` receiver that turns an AI Coding Tasks board change
into a launchd-consumable marker. It verifies `X-Notion-Signature`
(HMAC-SHA256 of the raw body, keyed with the subscription's verification
token), filters to the AI Coding Tasks data source
(`data.parent.data_source_id`), filters `page.properties_updated` to the
watched **property ids** (Lane / AI Label / Type — Notion sends ids, not
names), drops all-bot-authored events (the loop's own writes), and on a match
atomically writes `$HOME/.claude/triage-wave/kick/kickstart.json`. A `WatchPaths`
job on that directory (the `herald-watch-run.sh` pattern; the wrapper is
next wave's) is what would run `launchctl kickstart` on
the triage-wave job. The receiver itself never calls `launchctl`, never
spawns a process, never makes an outbound request — its test suite's AST
tripwire proves the first two.

**Nothing is armed.** No plist ships, `install.sh` does not place it, and the
shipped `notion-webhook-receiver.config.json` cannot start it: the three
property ids are `null` until the supervised arming sitting learns them from
the data-source schema. The verification token is an `op://` reference in
that config (`verification_token_ref`), resolved into the environment by
`op-resolve.py` at run time — never a value in this repo.

Design note, research (cited), receiver-host choice, arming steps as a §3
sitting, and the outcome for the next wave:
[`docs/notion-webhook.md`](./docs/notion-webhook.md).

### Usage

```bash
python3 launchd-kit/notion-webhook-receiver.py check --config launchd-kit/notion-webhook-receiver.config.json
#   exit 2 today ("watch_properties ids are null") — by design

# the one-time verification handshake (foreground terminal, arming sitting only):
python3 launchd-kit/notion-webhook-receiver.py serve --config <cfg> --handshake

# normal service, under the resolver (a plist's ProgramArguments, next wave):
python3 launchd-kit/op-resolve.py --env-file "$HOME/.claude/secrets/notion-webhook.env" -- \
  python3 launchd-kit/notion-webhook-receiver.py serve --config <cfg>   # 127.0.0.1:8791
```

### Smoke tests

[`test-notion-webhook-receiver.py`](./test-notion-webhook-receiver.py) — stdlib
`unittest`, 77 tests, no pytest, no Notion, no launchctl. The HTTP tests bind
`127.0.0.1:0` (ephemeral) on a daemon thread and prove the port is closed
afterwards, that a stalled peer is closed by the request timeout, and that
24 concurrent marker writes leave one valid marker. Fixtures under [`testdata/notion-webhook/`](./testdata/notion-webhook/)
follow the shapes at `developers.notion.com/reference/webhooks/page-properties-updated`
and `…/page-created`; their signatures in `SIGNATURES.sha256hmac` were computed
with `openssl dgst -sha256 -hmac <fixture token>`, not by the receiver, so the
signature tests are not tautological. Run with
`python3 launchd-kit/test-notion-webhook-receiver.py`.

## Testing `lib.sh`

[`test-lib.sh`](./test-lib.sh) is the offline stub suite for the library's notify
helpers. It sources `lib.sh` and points `NOTIFY_SCRIPT` at a stub notifier (no
network, no real `discord-notify.sh`) to prove the one contract that separates
the two helpers: `lk_notify` **always returns 0** (a notify is a side effect and
must never fail the caller), while `lk_notify_strict` **propagates** the
notifier's exit status — success returns 0, a non-zero delivery or a
missing/unreadable/non-regular notifier returns non-zero — so a 2xx-gated marker pattern
can gate on delivery. It also checks that both forward the title, color, and body
(stdin) unchanged. Run it with `bash launchd-kit/test-lib.sh` — with **`bash`**,
not `zsh`: section `(t)` turns on a `command -v` behaviour that differs between
the two shells.

Section `(t)` covers `lk_require_cmd` and the `claude` warning in
`launchd_kit_init` — the two PATH guards. Both assert *executability*, not mere
name resolution: under bash, `command -v` returns 0 for a file on `PATH` whose
exec bit is clear, so a present-but-non-executable binary (what a half-finished
self-update leaves behind) used to pass the guard and then die at call time. The
suite fixtures that case with a `chmod 644` file on a temp `PATH`, and checks
that each site keeps its own severity: `lk_require_cmd` **exits 127** with a red
notify, `launchd_kit_init` only **warns**.

The suite also covers the telemetry surface: schema/fixture validation for
`serotine.run.v1` and `serotine.event.v1` (sections f/f2), the bash emit/push
hooks (g–l), and the Python spool helper (section o), which runs
[`test-serotine-spool.py`](./test-serotine-spool.py) — a stdlib `unittest`
suite (stub `http.server`, no network) covering schema validity, byte-compat
with the bash emitter, `_lk_push_spool`-identical cursor semantics, the
fail-closed findings strip, and the never-raises posture — plus bash↔python
shared-spool interop checks. The Python suite also runs standalone:
`python3 launchd-kit/test-serotine-spool.py`.

## Portability (macOS bash 3.2)

`lib.sh` and every wrapper that sources it run under macOS's shipped
`/bin/bash` — bash **3.2** (Apple froze it there after the GPLv3 license
change), not whatever `bash` resolves to on `PATH` once Homebrew is
installed. Two 3.2-specific traps to know about when touching this file:

- **Post-increment arithmetic returns the OLD value.** `((waited++))`
  evaluates to the *pre*-increment value, so on `waited==0` it returns exit
  status 1 — under a caller's `set -e`, invoked outside a conditional, that's
  a silent early-exit. See the comment above `waited=$((waited + 1))` in
  `_lk_timeout`'s wait loop (`lib.sh`, search `NOT (( waited++ ))`) for why
  this file always uses a plain assignment there instead of `((waited++))`.
- **Expanding an empty array under `set -u` throws "unbound variable".**
  `"${arr[@]}"` on a zero-element array is a no-op on bash 4.4+ but errors on
  3.2 — and every wrapper in the [copy-paste example](#copy-paste-wrapper-example)
  runs `set -uo pipefail`. The portable guard is `${arr[@]+"${arr[@]}"}`, or
  avoid the blanket expansion entirely: `_LK_EXIT_HANDLERS` in `lib.sh` never
  does `"${_LK_EXIT_HANDLERS[@]}"` — it only checks `${#_LK_EXIT_HANDLERS[@]}`
  (a count, always safe) and indexes by position. `install.sh`'s OMP export
  path sidesteps this by using newline-joined strings walked with `while read`.
