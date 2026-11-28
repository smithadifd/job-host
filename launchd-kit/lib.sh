#!/usr/bin/env bash
# launchd-kit/lib.sh — shared boilerplate for launchd scheduled-run wrappers.
#
# Scheduled-run wrappers all re-implement the same five
# concerns: pin the environment launchd doesn't give them, guard against
# overlapping wake-ups, set up a rotating per-tool log, post to Discord, and
# alert loudly if the run itself crashes. This library extracts those so a new
# automation `source`s it instead of copy-pasting ~40 lines of shell.
#
# This file is a LIBRARY: sourcing it only DEFINES functions and default vars —
# it has NO side effects until you call launchd_kit_init (env + logging) and the
# guards/notify helpers explicitly. Nothing here runs claude, moves git state,
# or posts to Discord on its own.
#
# ------------------------------------------------------------------------------
# Env-var contract (set these BEFORE calling launchd_kit_init)
# ------------------------------------------------------------------------------
#   TOOL_NAME              (required) short slug, e.g. "dependabot-sweep". Used
#                          in the default LOG_DIR, log lines, and notify titles.
#   LOG_DIR                (optional) log directory.
#                          See launchd_kit_init for the default.
#   TZ                     (optional) timezone to pin. Default: America/New_York
#   LAUNCHD_KIT_PATH       (optional) PATH to export for the minimal launchd env.
#                          Default: $HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
#                          ($HOME/.local/bin leads because the native Claude Code
#                          installer puts `claude` there; see launchd_kit_init.)
#   DISCORD_WEBHOOK_URL    (optional) resolved by the notify helper if unset —
#                          see notify.json and discord-notify.md. NEVER logged.
#   WEBHOOK_ENV_FILE       (optional, legacy) env file read as data, never sourced,
#                          for DISCORD_WEBHOOK_URL. See discord-notify.sh
#                          for its default.
#   NOTIFY_SCRIPT          (optional) path to the shell discord-notify.sh.
#                          See lk_notify for the default.
#   LOG_RETENTION_DAYS     (optional) delete *.log older than this. Default: 60
#   LOG_MAX_FILES          (optional) secondary count cap for lk_rotate_logs,
#                          applied after the age-based pass. Default: unset
#                          (inert) — see lk_rotate_logs below.
#
# After launchd_kit_init the library exports: LOG (the timestamped run log for
# this invocation) and LK_STAMP (its YYYY-MM-DD_HHMMSS stamp).
#
# ------------------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------------------
#   launchd_kit_init            Pin PATH, HOME and TZ, create LOG_DIR, open LOG. Call first.
#                               Logs `kit: <root> <version>` right after the run
#                               banner (version = first line of the sibling VERSION
#                               file, `unknown` if it is missing or unreadable).
#   lk_log <msg...>             Append a timestamped line to the run log (and stderr).
#   lk_slot_guard <interval> [start_hhmm]
#                               Daily/weekly/quarterly/slot idempotency guard. Skips
#                               (exit 0) if this window already ran. See below for
#                               INTERVAL. Claims the window up front by default; set
#                               LK_SLOT_CLAIM=on-success to defer the claim until a
#                               clean exit (a failed run then retries next tick).
#                               Set LK_SLOT_GRACE=<dur> to CLOSE the window that
#                               far after it opened, so a load/tick long past the
#                               slot cannot satisfy it backward (opt-in; unset =
#                               today's unbounded behaviour).
#   lk_single_lock [max_age_min]
#                               Atomic single-run lock (mkdir). Skips (exit 0) if
#                               another run holds it; steals a stale lock. Installs
#                               an EXIT trap to release it.
#   lk_notify <title> [color] < body-on-stdin
#                               Post a Discord embed via NOTIFY_SCRIPT. Never fails
#                               the caller (returns 0 even if delivery fails) AND
#                               never blocks it: the notifier call is hard-bounded
#                               by _lk_timeout (LK_NOTIFY_TIMEOUT_SECS, default
#                               300s), so a hung notifier is killed and logged
#                               rather than parking the wrapper forever.
#   lk_notify_strict <title> [color] < body-on-stdin
#                               Same as lk_notify but PROPAGATES the notifier's
#                               exit status — for 2xx-gated marker patterns (write
#                               a marker only after Discord accepts). May fail the
#                               caller; never use in a crash-alert path.
#   lk_require_cmd <cmd...>     Assert each command resolves to an EXECUTABLE on
#                               the pinned PATH (a present-but-non-executable
#                               file fails, same as a missing one). Silent on
#                               success; on failure logs the offending name(s) +
#                               the PATH searched, fires a RED notify, and exits
#                               127. Use for anything the wrapper cannot work
#                               without (e.g. `claude`), so a PATH regression
#                               fails loudly instead of producing an empty run
#                               that reads as healthy. Opt-in; launchd_kit_init
#                               only WARNS, since most wrappers never invoke
#                               claude.
#   lk_enable_crash_alert       Install an EXIT trap that fires a RED notify on
#                               a non-zero exit — makes a silent failure visible.
#   lk_run_chain <name1> <cmd1> [<name2> <cmd2> ...]
#                               A static, pinned fallback-on-HARD-failure chain
#                               (a fixed policy artifact, not a router;
#                               its order changes only through a commit).
#                               Tries each <cmdN> in order via `bash -c`,
#                               advancing to the next ONLY when the current one
#                               hard-fails: non-zero exit, exit 0 with fewer
#                               than LK_CHAIN_MIN_BYTES (default 200) bytes on
#                               stdout, or a timeout (LK_CHAIN_TIMEOUT_SECS,
#                               default 240s, via _lk_timeout). NEVER on output
#                               quality or content. On success prints the
#                               winning link's stdout and sets LK_CHAIN_SERVED_
#                               NAME/_INDEX/_FELL_BACK/_FAILURES for the caller
#                               to read; returns 0. Every link hard-failing
#                               prints nothing and returns 1 — the caller's own
#                               existing crash-alert path handles that exactly
#                               like any other hard failure. See its own doc
#                               comment below for the full contract.
#   lk_rotate_logs              Delete logs older than LOG_RETENTION_DAYS, then
#                               (if LOG_MAX_FILES is set) delete the oldest
#                               survivors beyond that count too. Opt-in;
#                               unset LOG_MAX_FILES = today's behaviour.
#   _lk_timeout <secs> <cmd...> Portable hard-timeout guard (timeout/gtimeout/
#                               perl-alarm/background-kill fallback chain).
#                               Route every raw `op` (1Password CLI) call
#                               through this — it has no built-in timeout and
#                               can hang indefinitely under the macOS TCC
#                               app-data gate. See its own doc comment below.
#
#   --- serotine.run.v1 telemetry (opt-in; schema in telemetry/) ---
#   lk_enable_run_telemetry     Record the run start and arm an EXIT trap. Call
#                               once after launchd_kit_init, BEFORE the guards, so
#                               a slot-guard/lock short-circuit still emits skip and
#                               a crash still emits fail. PURELY ADDITIVE — a wrapper
#                               that never calls it is completely unaffected.
#   lk_emit_run [opts]          The wrapper's terminal emit (the ok path). Call once
#                               as the LAST thing the wrapper does. Builds one
#                               serotine.run.v1 JSONL line, appends it to the spool
#                               (spool.jsonl in SEROTINE_SPOOL_DIR), and best-effort
#                               POSTs unsent spool lines. Opts (all optional):
#                                 --status ok|fail|skip     (default ok)
#                                 --exit-code N             (default 0)
#                                 --operation NAME          gen_ai.operation.name
#                                 --usage-json JSON         explicit usage (path a)
#                                 --usage-file FILE         explicit usage from a file
#                                 --trigger launchd|manual|kickstart
#                                 --label LABEL             serotine.job.label
#                                 --failure-message MSG     serotine.run.failure's
#                                     `message` member. INERT unless
#                                     SEROTINE_EMIT_FAILURE arms the path (off
#                                     fleet-wide) — see § failure forensics.
#                               If neither --usage-json/--usage-file is given, a
#                               ccusage-style fallback parses local Claude Code
#                               session JSONL in the run's time window. NEVER fails
#                               or blocks the wrapper — same posture as lk_notify:
#                               all errors swallowed, short push timeout, ALWAYS
#                               returns 0, and the wrapper's exit code is untouched.
#                               If the wrapper crashes or a guard short-circuits
#                               before this call, the armed EXIT trap emits instead
#                               (fail with the real exit code / skip on exit 0).
#
#   Telemetry env-var contract (all optional; read at emit time):
#     SEROTINE_JOB_LABEL         serotine.job.label. Default: com.$(id -un).$TOOL_NAME
#     SEROTINE_HOST              host.name. Default: `hostname -s`
#     SEROTINE_TRIGGER           default serotine.run.trigger. Default: launchd
#     SEROTINE_SPOOL_DIR         spool directory. See lk_emit_run for the default.
#     SEROTINE_INGEST_URL        push target. Push happens only if URL AND TOKEN set.
#     SEROTINE_INGEST_TOKEN      Bearer token for the push (never logged).
#     SEROTINE_INGEST_TIMEOUT    curl --max-time seconds for the push. Default: 5
#     SEROTINE_EMIT_FAILURE      OFF FLEET-WIDE (unset). Arms the optional
#                                serotine.run.failure forensics object on the run
#                                line — see § failure forensics for the full
#                                contract, the redaction posture, and the sibling
#                                SEROTINE_FAILURE_* knobs. Unset is byte-identical
#                                to the line this library emitted before failure forensics.
#     SEROTINE_USAGE_FALLBACK_DISABLE=1   skip the session-JSONL usage fallback
#                                       (set for known non-LLM heartbeat jobs).
#     SEROTINE_USAGE_PROJECTS_DIR         session-JSONL root; see serotine_usage.py.
#     SEROTINE_USAGE_SESSION_ID           scope the usage scan to ONE session's transcript
#                                       (<projects>/*/<session-id>.jsonl) so a concurrent
#                                       session can't bleed its models/tokens into this job's
#                                       usage rollup and cause false model-drift
#                                       reports. Unset = whole-tree
#                                       window walk. The wrapper must pass the SAME id to
#                                       `claude -p --session-id`.
#     SEROTINE_USAGE_PROVIDER           opt-in, non-Claude jobs only:
#                                       explicit provider selection;
#                                       codex|gemini|omp|grok-build selects the matching
#                                       provider usage adapter in
#                                       serotine_usage.py. PRECEDENCE: when set, this path
#                                       OWNS usage resolution for the run — it is checked
#                                       BEFORE --usage-json/--usage-file and the window
#                                       fallback, and never falls through to either. Unset
#                                       (the default for every existing wrapper) is
#                                       byte-identical to today: PURELY ADDITIVE. An
#                                       unrecognized value is a hard error (raises inside
#                                       the helper) rather than a silent empty array or a
#                                       silent Claude-window scan — see serotine_usage.py's
#                                       resolve_usage() docstring. Still can't fail the
#                                       wrapper's own exit code (this whole hook is
#                                       swallowed on error, same as every other emit path);
#                                       it just means that one run's line goes unemitted.
#     SEROTINE_USAGE_INPUT               file path holding the SEROTINE_USAGE_PROVIDER
#                                       provider's raw output. Codex: the session ROLLOUT
#                                       file ($HOME/.codex/sessions/**/*.jsonl) -- the only
#                                       codex-side input carrying a served model id. Raw
#                                       `codex exec --json` STDOUT is NOT valid input here:
#                                       it never carries a model id and is detected +
#                                       rejected with a loud CodexStdoutWireShapeError
#                                       (naming this wiring rule) instead of a silent
#                                       model-less usage row -- when miswired this way, the
#                                       observable symptom in a real wrapper run is simply
#                                       "no usage lines emitted" (this whole helper call is
#                                       swallowed like any other emit-hook error, same as
#                                       the unrecognized-provider case above); run the
#                                       helper directly (`python3 serotine_usage.py usage`)
#                                       to see the actionable error. Gemini: a raw API
#                                       response JSON body. Omp: the `omp -p --mode json`
#                                       EVENT STREAM (JSONL; the terminal agent_end event
#                                       carries the whole messages[]). Grok-build: the Grok
#                                       Build CLI's `--output-format json` RESULT OBJECT
#                                       (one JSON object; its modelUsage KEYS are the
#                                       served ids). For those two, a capture of the
#                                       client's human-readable TUI transcript is NOT valid
#                                       input either: it is detected + rejected with a loud
#                                       TuiOutputWireShapeError naming the flag to add,
#                                       with the same "no usage lines emitted" observable
#                                       symptom described above. Required alongside
#                                       SEROTINE_USAGE_PROVIDER; missing or unreadable ->
#                                       [] (fail closed, never a guess).
#     SEROTINE_USAGE_REQUEST_MODEL       the wrapper's OWN pinned canonical model id
#                                       (gen_ai.request.model) for the SEROTINE_USAGE_PROVIDER
#                                       path — legitimate to supply directly (it IS the pin).
#                                       NEVER used as the response id: gen_ai.response.model
#                                       comes only from what the provider itself reports in
#                                       SEROTINE_USAGE_INPUT (omitted, never fabricated, when
#                                       it doesn't say one). Required; missing/blank -> [].
#
# Color helpers (Discord brand-palette decimals, per discord-notify.md):
#   LK_COLOR_GREEN=5763719  LK_COLOR_YELLOW=16705372
#   LK_COLOR_RED=15548997   LK_COLOR_BLURPLE=5793266
# ------------------------------------------------------------------------------

# Directory this library lives in — used to locate the vendored serotine_usage.py
# helper next to it. Resolved at source time; defining it has no side effects.
_LK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"


# --- EXIT-trap registry -------------------------------------------------------
# Several helpers want to run something at exit (release the lock, fire a crash
# alert). Chaining trap strings is fragile — the first handler's exit status
# clobbers $? for the rest. Instead we install ONE dispatcher that captures the
# script's real exit code up front, then runs each registered handler in LIFO
# order with LK_EXIT_CODE available to all of them.
#
# Array-handling convention (this array and any other array this file grows):
# never blanket-expand "${arr[@]}" — only count it (${#arr[@]}, always safe)
# or index it by position. That's deliberate, not style: macOS ships bash 3.2
# (not whatever Homebrew bash resolves to on PATH), and every wrapper that
# sources this kit runs under `set -uo pipefail` (see the copy-paste wrapper
# example below). Under `set -u`, "${arr[@]}" on a ZERO-element array throws
# "unbound variable" on 3.2 — bash 4.4+ silently expands to nothing. If a
# future helper here needs a blanket expansion that might be empty, guard it
# with ${arr[@]+"${arr[@]}"} instead. (Different 3.2 trap, same family: the
# post-increment-arithmetic gotcha in _lk_timeout below, around waited++.)
LK_EXIT_CODE=0
_LK_EXIT_HANDLERS=()

_lk_run_exit_handlers() {
  LK_EXIT_CODE=$?
  local i
  for (( i=${#_LK_EXIT_HANDLERS[@]}-1 ; i>=0 ; i-- )); do
    "${_LK_EXIT_HANDLERS[$i]}" || true
  done
}

# Register a function name to run at exit. Installs the dispatcher on first use.
lk_on_exit() {
  if (( ${#_LK_EXIT_HANDLERS[@]} == 0 )); then
    trap _lk_run_exit_handlers EXIT
  fi
  _LK_EXIT_HANDLERS+=("$1")
}

# --- _lk_timeout: portable hard-timeout guard --------------------------------
# Runs "$@" bounded to <secs> seconds so nothing this kit shells out to can
# hang the job forever. This exists because of a real, repeated failure mode:
# the `op` (1Password CLI) binary probes the desktop app's container, which
# hits a macOS TCC "access data from other apps" gate that BLOCKS under
# display-sleep — an unattended launchd job then hangs indefinitely with no
# error, silently missing its whole window.
# `op` has no built-in timeout, so every raw `op` call in this kit must be
# routed through this guard instead of invoked bare.
#
# Fallback chain (first available wins — macOS ships neither GNU `timeout`
# nor `gtimeout` by default, so the guard must not assume either exists):
#   1. `timeout`  (GNU coreutils, or a Linux box)
#   2. `gtimeout` (homebrew coreutils on macOS)
#   3. `perl fork+alarm+setpgid` — perl ships on every stock macOS, so this is
#      the guaranteed bound on a Mac with neither of the above.
#   4. background + poll + kill — last-resort guard needing only bash
#      builtins, for the (currently theoretical) case where even perl is gone.
#
# EVERY tier must survive a child that IGNORES/traps SIGTERM (op's helper
# subprocesses, or any misbehaving binary): a lone SIGTERM the child swallows
# would leave it — and a forked helper holding the stdout pipe — alive, and
# command substitution ($(...)) BLOCKED on pipe EOF indefinitely, silently
# defeating the whole "hard bound". So SIGTERM is always followed, after a
# short grace, by an unignorable SIGKILL:
#   - Tiers 1/2 pass `--kill-after=<grace>` to GNU timeout/gtimeout (which by
#     default — i.e. NOT in --foreground mode — already signals the child's
#     whole process group, so the KILL reaches a forked helper too).
#   - Tiers 3/4 put the child in its OWN process group and signal the whole
#     group (negative PID) with TERM then, after the grace, KILL.
# The pipe-holding-open failure this defends against was found empirically
# while building this guard: a single-process, TERM-only kill left
# `$(_lk_timeout ...)` hung for the wrapped command's full runtime even though
# the immediate child had already died on schedule.
#
# On a TERM-respecting timeout this returns 124 (GNU timeout's convention);
# if the KILL follow-up is what finally terminated the child, the exit is 137
# (128+SIGKILL) — both are non-zero, so a caller's failure path fires either
# way. Other non-zero exits pass the child's real status through. A fast,
# successful command behaves exactly as if run unwrapped — same stdout, same
# exit code.
_lk_timeout() {
  local secs="$1"; shift
  if [[ $# -eq 0 ]]; then
    echo "_lk_timeout: no command given" >&2
    return 2
  fi

  # SIGKILL grace after the SIGTERM (read at call time so it can be overridden
  # per-invocation via the environment). GNU timeout duration syntax, e.g. 5s.
  local kill_after="${LK_TIMEOUT_KILL_AFTER:-5s}"

  if command -v timeout >/dev/null 2>&1; then
    timeout --kill-after="$kill_after" "$secs" "$@"
    return $?
  fi

  if command -v gtimeout >/dev/null 2>&1; then
    gtimeout --kill-after="$kill_after" "$secs" "$@"
    return $?
  fi

  if command -v perl >/dev/null 2>&1; then
    perl -e '
      use POSIX ();
      my $secs = shift(@ARGV);
      my $timed_out = 0;
      my $pid = fork();
      if (!defined $pid) { exit 126; }
      if ($pid == 0) {
        eval { POSIX::setpgid(0, 0); };
        exec { $ARGV[0] } @ARGV or exit 127;
      }
      eval { POSIX::setpgid($pid, $pid); };
      eval {
        local $SIG{ALRM} = sub { $timed_out = 1; die "lk_timeout\n"; };
        alarm($secs);
        waitpid($pid, 0);
        alarm(0);
      };
      if ($timed_out) {
        # Group-kill (reaches a forked helper holding the pipe) ONLY if the
        # child actually got its own process group. If setpgid failed the
        # child still shares our group, so a negative-PID signal would hit US
        # (or no-op) and the final waitpid could hang — fall back to signalling
        # the child PID directly. getpgrp($pid) reports the childs real pgid.
        my $pgid;
        eval { $pgid = getpgrp($pid); };
        my $target = (defined $pgid && $pgid == $pid) ? -$pid : $pid;
        kill("TERM", $target);
        select(undef, undef, undef, 0.3);
        kill("KILL", $target);
        waitpid($pid, 0);
        exit(124);
      }
      my $rc = $?;
      exit(($rc & 127) ? 128 + ($rc & 127) : $rc >> 8);
    ' "$secs" "$@"
    return $?
  fi

  # No timeout/gtimeout/perl on PATH at all: run the command in its own
  # process group (via job control) and hard-kill the whole group if it
  # outlives the deadline. Every stock bash has this available, so this is
  # the guaranteed floor.
  local _lk_had_monitor=1
  case $- in *m*) _lk_had_monitor=1 ;; *) _lk_had_monitor=0 ;; esac
  set -m
  # `<&0` is LOAD-BEARING and is not redundant with `set -m`. POSIX says an
  # asynchronous list gets its stdin from /dev/null "before any explicit
  # redirections" when job control is inactive — and bash 3.2 (the stock macOS
  # shell, and what CI runs) applies that redirection even with `set -m` on in
  # a non-interactive shell. Without this, the notifier's body — which arrives
  # on stdin — is silently replaced by EOF and every post on this tier goes out
  # with an empty embed. An EXPLICIT redirection is exempted by that same POSIX
  # clause, so it wins on every shell regardless of job-control state.
  "$@" <&0 &
  local cmd_pid=$! waited=0
  (( _lk_had_monitor )) || set +m
  while kill -0 "$cmd_pid" 2>/dev/null; do
    if (( waited >= secs )); then
      kill -TERM -- "-$cmd_pid" 2>/dev/null || kill -TERM "$cmd_pid" 2>/dev/null
      sleep 1
      kill -KILL -- "-$cmd_pid" 2>/dev/null || kill -KILL "$cmd_pid" 2>/dev/null
      wait "$cmd_pid" 2>/dev/null
      return 124
    fi
    sleep 1
    # NOT (( waited++ )): the post-increment arithmetic command evaluates to
    # the OLD value, so it returns status 1 when waited==0 — which under bash
    # 3.2 `set -e` would early-exit a caller that invoked _lk_timeout outside
    # a conditional, leaving this background group alive. Plain assignment
    # always returns 0.
    waited=$((waited + 1))
  done
  wait "$cmd_pid"
}

# --- _lk_have_cmd: does NAME resolve to something actually RUNNABLE? ---------
# `command -v` answers a NAME question, not a RUNNABLE one: for a file found on
# PATH it prints the path and returns 0 based on the lookup, so a present-but-
# NON-EXECUTABLE file passes a bare `command -v` guard and then dies at call
# time with "permission denied". That is not a hypothetical shape — it is
# exactly what a half-finished self-update (the native Claude Code installer
# replacing $HOME/.local/bin/claude) leaves on disk, and it is the one case a
# PATH guard exists to catch. So resolve the name first, then test the resolved
# path with -x.
#
# Two arms are load-bearing:
#   * the empty-result guard — `[[ -x "" ]]` is false, but relying on that makes
#     an empty resolution pass or fail by coincidence rather than by intent;
#   * the no-slash arm — a shell builtin/function/keyword resolves to a bare
#     name (`command -v cd` prints `cd`), which is always callable and has no
#     path to -x. Testing those would false-FAIL them. Anything containing a
#     slash is a real path (absolute or relative) and gets the -x test.
_lk_have_cmd() {
  local resolved
  resolved="$(command -v "$1" 2>/dev/null)" || return 1
  [[ -n "$resolved" ]] || return 1
  [[ "$resolved" == */* ]] || return 0
  [[ -x "$resolved" ]]
}

# --- launchd_kit_init: pin env + open the run log ----------------------------
launchd_kit_init() {
  : "${TOOL_NAME:?launchd_kit_init: TOOL_NAME must be set before init}"

  # launchd hands a job a near-empty environment. Pin PATH, HOME and TZ so claude,
  # gh, jq, git, curl resolve and so any date/time guard is stable in Eastern.
  # HOME is pinned BEFORE PATH because the PATH default interpolates it: the native
  # Claude Code installer puts `claude` in $HOME/.local/bin as of 2026-07-25, and the
  # Homebrew cask that used to supply /opt/homebrew/bin/claude is gone. Homebrew stays
  # on the path for gh/jq/git.
  export HOME="${HOME:-/Us""ers/$(id -un)}"
  export PATH="${LAUNCHD_KIT_PATH:-$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}"
  export TZ="${TZ:-America/New_York}"

  LOG_DIR="${LOG_DIR:-$HOME/.claude/logs/$TOOL_NAME}"
  export LOG_DIR
  mkdir -p "$LOG_DIR"

  LK_STAMP="$(date +%Y-%m-%d_%H%M%S)"
  LOG="$LOG_DIR/$LK_STAMP.log"
  export LK_STAMP LOG

  echo "=== $TOOL_NAME scheduled run: $LK_STAMP (host $(hostname)) ===" >>"$LOG"

  # Name the kit checkout this run came from and its version: the first line of
  # the sibling VERSION file. A missing, empty or unreadable VERSION logs
  # `unknown` and the run goes on; this line must never abort a live job.
  local _lk_kit_ver=""
  { IFS= read -r _lk_kit_ver <"$_LK_DIR/VERSION"; } 2>/dev/null || true
  echo "kit: $_LK_DIR ${_lk_kit_ver:-unknown}" >>"$LOG"

  # Fleet-wide regression guard. The failure this catches is silent by nature:
  # launchd hands a job a near-empty environment, so a PATH regression makes a
  # binary vanish and the run "succeeds" having done no work — which, for a
  # quiet-when-healthy job, is indistinguishable from a healthy week. Say it at
  # the TOP of the log instead of letting the run fail downstream unattributed.
  # Warning only, and the measured fleet says that is the right severity: of the
  # 13 wrappers that source this library (12 launchd-wired + one unwired), only
  # 4 invoke `claude` directly — 9 do not. (Two of those 9 reach it indirectly,
  # via a child that fans out `claude -p` itself.) A hard failure here would
  # therefore be a false alarm for most of the fleet. Wrappers that genuinely
  # cannot proceed without a binary call lk_require_cmd (below), which does exit.
  #
  # _lk_have_cmd, not `command -v`: a present-but-non-executable `claude` is
  # precisely the half-finished-self-update case this warning is for, and a bare
  # name lookup would wave it through. See _lk_have_cmd's doc comment above.
  if ! _lk_have_cmd claude; then
    echo "[launchd-kit] WARNING: \`claude\` does not resolve to an executable on this PATH." >>"$LOG"
    echo "[launchd-kit]          PATH=$PATH" >>"$LOG"
  fi
}

# --- lk_log: timestamped line to the run log (and stderr) --------------------
lk_log() {
  local line
  line="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
  if [[ -n "${LOG:-}" ]]; then
    echo "$line" >>"$LOG"
  fi
  echo "$line" >&2
}

# _lk_parse_duration: "<N>[s|m|h|d]" -> seconds on stdout. A bare number is
# seconds. Prints nothing and returns 1 on anything else, so callers can decide
# whether an unparseable value is fatal (lk_slot_guard treats it as fatal).
_lk_parse_duration() {
  local raw="${1:-}" n unit secs
  [[ "$raw" =~ ^([0-9]+)([smhd]?)$ ]] || return 1
  # Reject absurd magnitudes BEFORE any arithmetic. Past ~19 digits an int64
  # wraps, and it can wrap either sign: a positive wrap silently restores the
  # unbounded window, a negative one closes every future window forever. Both
  # are invisible. 10 digits is ~317 years in seconds — nothing legitimate is
  # excluded, and no multiply below can overflow.
  (( ${#BASH_REMATCH[1]} <= 10 )) || return 1
  # Force base 10. Bash reads a leading-zero literal as OCTAL, so without this
  # a bare "010" silently means 8 seconds, and "09"/"08" are a hard arithmetic
  # error that the CALLER's (( )) swallows into a false condition — leaving the
  # window UNBOUNDED, i.e. exactly the failure this variable exists to prevent.
  n=$(( 10#${BASH_REMATCH[1]} )) || return 1
  unit="${BASH_REMATCH[2]:-s}"
  case "$unit" in
    s) secs=$(( n ))         ;;
    m) secs=$(( n * 60 ))    ;;
    h) secs=$(( n * 3600 ))  ;;
    d) secs=$(( n * 86400 )) ;;
  esac
  # A value large enough to wrap past INT64 is a typo, not a window. Reject it
  # so the caller fails CLOSED (exit 2) rather than computing a negative bound
  # that silently closes every future slot forever.
  (( secs >= 0 )) || return 1
  printf '%s' "$secs"
}

# --- lk_slot_guard: idempotency + timing guard -------------------------------
# launchd fires jobs on a coarse StartInterval (a single StartCalendarInterval
# timer gets dropped across sleep/DarkWake). So the wrapper fires often and this
# guard admits only the FIRST tick of each window, self-healing a missed wake.
#
#   lk_slot_guard daily  [HH:MM]   one run per calendar day on/after HH:MM (default 00:00)
#   lk_slot_guard weekly:<dow> [HH:MM]
#                                  one run per week on/after <dow> HH:MM.
#                                  <dow> is a day name understood by `date -v`:
#                                  sun mon tue wed thu fri sat. Default 00:00.
#   lk_slot_guard quarterly [HH:MM]
#                                  one run per CALENDAR quarter (Jan/Apr/Jul/Oct
#                                  cycle, not rolling ~90 days), on/after the 1st
#                                  of the quarter's first month at HH:MM.
#                                  Default 00:00. Pair with a daily-tick plist —
#                                  the guard admits the first tick of each quarter.
#   lk_slot_guard slots:8,12,16,20 one run per listed hour-slot per day (comma list)
#
# Skips (exit 0) when the current window has already run. Set
# ${TOOL_NAME_upper}_FORCE=1 OR LK_FORCE=1 to bypass.
#
# WINDOW GRACE (LK_SLOT_GRACE) — opt-in, unset by default:
#   Every interval above resolves its window BACKWARD: `weekly:sun` on a Friday
#   picks LAST Sunday, `daily` picks this morning, `quarterly` picks the quarter
#   start. That is what makes a missed wake self-heal — but with no upper bound
#   it also means a window stays "open, unclaimed" for the whole period, so a
#   job LOADED off-cycle (or one whose marker is absent for any reason: fresh
#   install, new LOG_DIR, rotated-away marker) fires IMMEDIATELY on its first
#   tick instead of waiting for its real slot. That trap has bitten this fleet.
#
#   LK_SLOT_GRACE bounds the backward reach: the window CLOSES that long after
#   it opened, and a tick past the close skips (exit 0) WITHOUT claiming, so the
#   next real window is untouched. Duration is <N>[s|m|h|d] (bare N = seconds):
#
#     LK_SLOT_GRACE=30h lk_slot_guard weekly:sun 16:00
#       → runs on the first tick from Sun 16:00 through Mon 22:00 (a Sunday
#         missed across sleep still self-heals Monday morning); a Wednesday load
#         does NOT fire, it waits for Sunday.
#
#   Unset (the default) is exactly the historical behaviour — unbounded — so no
#   existing consumer changes. LK_SLOT_GRACE="" counts as unset, matching this
#   file's ${X:-} convention everywhere else: empty means "not configured".
#   An unparseable value fails CLOSED (exit 2), same posture as an unknown
#   interval: a typo must be loud, never silently unguarded. A ZERO grace is
#   likewise refused (exit 2) — it parses, but it defines an EMPTY window and
#   would silently disable the job forever.
#
# CLAIM SEMANTICS (LK_SLOT_CLAIM):
#   up-front  (default) The marker is written BEFORE the job body runs, so a long
#                       run isn't double-started by the next tick and a crash does
#                       NOT retry until the next window. Also suppresses an
#                       overlapping tick at the guard. This is the safe default and
#                       existing consumers are unaffected.
#   on-success          DEFER the claim: the marker is written from an EXIT handler
#                       ONLY when the run exits 0, so a FAILED run leaves the window
#                       open and the next launchd tick retries it (the claim-on-
#                       success poller class — e.g. serotine-tripwire, which must
#                       retry on tomorrow's tick rather than burn the quarter on a
#                       transient gh/network error). In this mode acquire
#                       lk_single_lock BEFORE this guard, so a lock-skipped
#                       overlapping tick can't reach the guard and consume the slot.
lk_slot_guard() {
  local interval="${1:?lk_slot_guard: interval required}"
  local at="${2:-00:00}"

  # FORCE override: LK_FORCE=1 or <TOOL>_FORCE=1 (TOOL uppercased, - -> _).
  local force_var force_val
  force_var="$(printf '%s' "${TOOL_NAME:-}" | tr '[:lower:]-' '[:upper:]_')_FORCE"
  force_val="${!force_var:-0}"
  if [[ "${LK_FORCE:-0}" == "1" || "$force_val" == "1" ]]; then
    return 0
  fi

  local marker="$LOG_DIR/.last-run-window"
  local key now open_epoch
  now="$(date +%s)"
  open_epoch=""                                  # when THIS window opened (for LK_SLOT_GRACE)

  case "$interval" in
    daily)
      local day_date day_epoch
      day_date="$(date +%Y-%m-%d)"
      day_epoch="$(date -j -f '%Y-%m-%d %H:%M' "$day_date $at" +%s)"
      (( now < day_epoch )) && exit 0            # before today's window
      key="daily:$day_date"
      open_epoch="$day_epoch"
      ;;
    weekly:*)
      local dow win_date win_epoch
      dow="${interval#weekly:}"
      win_date="$(date -v-"$dow" +%Y-%m-%d)"     # today if it's <dow>, else prior <dow>
      win_epoch="$(date -j -f '%Y-%m-%d %H:%M' "$win_date $at" +%s)"
      (( now < win_epoch )) && exit 0            # before this week's window
      key="weekly:$win_date"
      open_epoch="$win_epoch"
      ;;
    quarterly)
      # Calendar quarters (Jan 1 / Apr 1 / Jul 1 / Oct 1), NOT rolling ~90 days —
      # consistent with daily/weekly being calendar windows, and matching the
      # YYYY-Qn semantics of the bespoke quarter marker this interval replaces
      # (serotine-tripwire's wrapper). The window opens on the 1st of the
      # quarter's first month at HH:MM; a daily launchd tick self-heals a
      # missed quarter-start the same way daily heals a missed wake.
      local q_month q_date q_epoch
      q_month=$(( ( (10#$(date +%m) - 1) / 3 ) * 3 + 1 ))
      q_date="$(printf '%s-%02d-01' "$(date +%Y)" "$q_month")"
      q_epoch="$(date -j -f '%Y-%m-%d %H:%M' "$q_date $at" +%s)"
      (( now < q_epoch )) && exit 0              # before this quarter's window
      key="quarterly:$q_date"
      open_epoch="$q_epoch"
      ;;
    slots:*)
      local list h slot
      list="${interval#slots:}"
      h=$((10#$(date +%H)))
      slot=""
      # pick the highest listed slot <= current hour. Split the comma list onto
      # separate lines BEFORE sort so `sort -rn` sees one number per line and
      # actually orders them descending (a quoted single arg would be a no-op,
      # leaving the list ascending and picking the lowest slot).
      for s in $(tr ',' '\n' <<<"$list" | sort -rn); do
        (( h >= s )) && { slot=$s; break; }
      done
      [[ -z "$slot" ]] && exit 0                 # before the first slot today
      key="slot:$(date +%Y-%m-%d):$slot"
      open_epoch="$(date -j -f '%Y-%m-%d %H:%M' "$(date +%Y-%m-%d) $(printf '%02d' "$slot"):00" +%s)"
      ;;
    *)
      # Fail CLOSED: wrappers run without -e, so a returned error would let a
      # typo'd interval fall through and run the job unguarded every tick.
      echo "lk_slot_guard: unknown interval '$interval' — refusing to run unguarded" >&2
      exit 2
      ;;
  esac

  # Opt-in upper bound on how far back a window reaches (see WINDOW GRACE above).
  # Checked BEFORE the marker so a long-closed window can never be claimed —
  # a stale-but-open window is exactly the shape that fires an off-cycle load.
  if [[ -n "${LK_SLOT_GRACE:-}" ]]; then
    local grace_s
    if ! grace_s="$(_lk_parse_duration "${LK_SLOT_GRACE}")"; then
      # Fail CLOSED, same as an unknown interval: a typo must not silently
      # degrade into the unbounded window it was set to prevent.
      echo "lk_slot_guard: unparseable LK_SLOT_GRACE '${LK_SLOT_GRACE}' — refusing to run unguarded" >&2
      exit 2
    fi
    # A ZERO grace parses cleanly but defines an EMPTY window: the guard has
    # already returned for `now < open_epoch`, so `now >= open_epoch + 0` is
    # true for every remaining instant and the job can never run again — a
    # silent permanent disable, from a one-character typo, in a fleet-shared
    # library. Refuse it LOUDLY rather than reinterpreting it as "no grace":
    # silently reading an operator's tightest-possible bound as its opposite
    # (unbounded) is the guess the unparseable case above already rules out.
    if (( grace_s == 0 )); then
      echo "lk_slot_guard: LK_SLOT_GRACE '${LK_SLOT_GRACE}' is zero — that closes every window forever; refusing to run unguarded" >&2
      exit 2
    fi
    if [[ -n "$open_epoch" ]] && (( now >= open_epoch + grace_s )); then
      exit 0                                      # window closed; wait for the next one
    fi
  fi

  if [[ -f "$marker" && "$(cat "$marker")" == "$key" ]]; then
    exit 0                                        # this window already ran
  fi

  # Claim the window. Default (up-front) writes the marker NOW; the opt-in
  # (LK_SLOT_CLAIM=on-success) defers it to an EXIT handler that fires only on a
  # clean exit, so a failed run stays retryable on the next tick.
  case "${LK_SLOT_CLAIM:-up-front}" in
    on-success)
      # NOT declared local: the EXIT handler runs after this function returns and
      # reads these from the script scope.
      LK_SLOT_MARKER="$marker"
      LK_SLOT_KEY="$key"
      lk_on_exit _lk_slot_claim_on_success
      ;;
    up-front)
      echo "$key" >"$marker"                      # claim the window up front
      ;;
    *)
      # Fail SAFE: an unrecognized claim mode falls back to the safe up-front
      # claim (never leaves a window unguarded), but warn so a typo is visible.
      echo "lk_slot_guard: unknown LK_SLOT_CLAIM '${LK_SLOT_CLAIM}' — using up-front" >&2
      echo "$key" >"$marker"
      ;;
  esac
}

# _lk_slot_claim_on_success: EXIT handler for LK_SLOT_CLAIM=on-success. Writes the
# slot marker ONLY when the run exited 0, so a non-zero (failed) run leaves the
# window unclaimed and the next launchd tick retries it. Same exit-code posture as
# _lk_crash_alert: reads LK_EXIT_CODE captured by the exit dispatcher.
_lk_slot_claim_on_success() {
  (( LK_EXIT_CODE == 0 )) || return 0             # failed run → leave the window open to retry
  echo "$LK_SLOT_KEY" >"$LK_SLOT_MARKER" 2>/dev/null || true
}

# --- lk_single_lock: atomic single-run lock ----------------------------------
# macOS ships no flock; mkdir is atomic. Prevents two runs overlapping (which
# would race on git worktrees/branches). A stale lock (older than max_age_min,
# default 120) is assumed dead and stolen. Installs an EXIT trap to release it.
# Skips this tick (exit 0) if another run holds a fresh lock.
lk_single_lock() {
  local max_age_min="${1:-120}"
  LK_LOCK_DIR="$LOG_DIR/.run.lock"

  if [[ -d "$LK_LOCK_DIR" && -n "$(find "$LK_LOCK_DIR" -maxdepth 0 -mmin +"$max_age_min" 2>/dev/null)" ]]; then
    rm -rf "$LK_LOCK_DIR"                         # steal a stale (dead-run) lock
  fi
  if ! mkdir "$LK_LOCK_DIR" 2>/dev/null; then
    echo "$TOOL_NAME: another run is active ($LK_LOCK_DIR) — skipping this tick" >&2
    exit 0
  fi
  date >"$LK_LOCK_DIR/started-at" 2>/dev/null || true
  lk_on_exit _lk_release_lock                     # release via the exit registry
}

_lk_release_lock() {
  [[ -n "${LK_LOCK_DIR:-}" ]] && rm -rf "$LK_LOCK_DIR"
}

# Notification helpers and crash alerts live in a sourceable integration.
# shellcheck source=launchd-kit/lib/notify.sh
if [[ -f "$_LK_DIR/lib/notify.sh" ]]; then source "$_LK_DIR/lib/notify.sh"; else eval "$(sed -n '/^# BEGIN GENERATED STANDALONE MODULE: lib\/notify.sh$/,/^# END GENERATED STANDALONE MODULE: lib\/notify.sh$/p' "${BASH_SOURCE[0]}" | sed -e '1d' -e '$d' -e 's/^#|//')"; fi
# --- lk_require_cmd: hard-fail loudly when a needed binary is missing --------
# Call AFTER launchd_kit_init for anything the wrapper cannot work without:
#   lk_require_cmd claude gh
# On success, silent. On failure it logs the missing name(s) AND the PATH that
# was searched, fires a RED notify, and exits 127 — converting "ran, produced
# nothing, looked healthy" into an explicit, attributable failure.
#
# Deliberately opt-in: launchd_kit_init only warns (see above) because most
# wrappers here don't need claude, and a kit-wide hard failure would break jobs
# that never used it. Uses a string accumulator, not an array — see the bash 3.2
# array-handling convention near the top of this file.
lk_require_cmd() {
  local missing="" c
  for c in "$@"; do
    # _lk_have_cmd, not `command -v`: the name resolving is not the question —
    # whether it will RUN is. A present-but-non-executable file passes a name
    # lookup and then fails at call time, which is the silent-empty-run this
    # guard exists to convert into a loud failure. See _lk_have_cmd above.
    _lk_have_cmd "$c" || missing="${missing:+$missing }$c"
  done
  [[ -z "$missing" ]] && return 0

  lk_log "FATAL: required command(s) missing or not executable on PATH: $missing"
  lk_log "FATAL: PATH=$PATH"
  printf '%s\n%s\n%s\n' \
    "Missing or not executable on the pinned PATH: \`$missing\`" \
    "PATH=$PATH" \
    "The job exited without doing any work — this is NOT a healthy run." \
    | lk_notify "$TOOL_NAME — required command missing" "$LK_COLOR_RED"
  exit 127
}

# The fallback-chain helper lives in a sourceable integration.
# shellcheck source=launchd-kit/lib/chain.sh
if [[ -f "$_LK_DIR/lib/chain.sh" ]]; then source "$_LK_DIR/lib/chain.sh"; else eval "$(sed -n '/^# BEGIN GENERATED STANDALONE MODULE: lib\/chain.sh$/,/^# END GENERATED STANDALONE MODULE: lib\/chain.sh$/p' "${BASH_SOURCE[0]}" | sed -e '1d' -e '$d' -e 's/^#|//')"; fi



# --- lk_rotate_logs: prune old run logs --------------------------------------
# Two independent bounds, applied in order:
#   1. AGE  — the original rule: delete *.log older than LOG_RETENTION_DAYS
#      (default 60). Unconditional, exactly as before.
#   2. COUNT — opt-in secondary cap via LOG_MAX_FILES (unset = inert). Exists
#      for sub-hourly pollers (e.g. serotine-tripwire, 30-min cadence) where
#      the age bound alone lets file COUNT explode even though every file is
#      individually "young enough" — 60d retention x 48 runs/day ~= 2,880
#      files at steady state (network-incident FU-4, the same
#      unbounded-growth pressure class as the host incident). A daily/weekly
#      job never accumulates enough files within LOG_RETENTION_DAYS days to
#      approach a sane cap, so LOG_MAX_FILES is unset-by-default and, like
#      every other opt-in lib.sh knob (LK_SLOT_GRACE, SEROTINE_TRACE_CAPTURE),
#      changes nothing for a job that never sets it.
lk_rotate_logs() {
  local days="${LOG_RETENTION_DAYS:-60}"
  find "$LOG_DIR" -name '*.log' -mtime +"$days" -delete 2>/dev/null || true

  local max_files="${LOG_MAX_FILES:-0}"
  [[ "$max_files" =~ ^[0-9]+$ ]] || return 0
  (( max_files > 0 )) || return 0

  local count
  count="$(find "$LOG_DIR" -name '*.log' -type f 2>/dev/null | wc -l | tr -d ' ')"
  [[ "$count" =~ ^[0-9]+$ ]] || return 0
  (( count > max_files )) || return 0

  # Oldest-first by mtime; drop everything past the cap. BSD `stat -f` (every
  # launchd-kit host is macOS) avoids `find -printf` (GNU-only, unavailable on
  # macOS's BSD find) and avoids piping through `ls -t`/xargs, whose
  # ARG_MAX-driven chunking would silently re-sort each chunk independently
  # once file counts get large — `sort -rn` sorts the whole set at once instead.
  find "$LOG_DIR" -name '*.log' -type f -exec stat -f '%m %N' {} \; 2>/dev/null \
    | sort -rn \
    | tail -n "+$((max_files + 1))" \
    | cut -d' ' -f2- \
    | while IFS= read -r f; do rm -f -- "$f"; done
}

# The run-record payload and sink live in a sourceable integration.
# shellcheck source=launchd-kit/lib/telemetry.sh
if [[ -f "$_LK_DIR/lib/telemetry.sh" ]]; then source "$_LK_DIR/lib/telemetry.sh"; else eval "$(sed -n '/^# BEGIN GENERATED STANDALONE MODULE: lib\/telemetry.sh$/,/^# END GENERATED STANDALONE MODULE: lib\/telemetry.sh$/p' "${BASH_SOURCE[0]}" | sed -e '1d' -e '$d' -e 's/^#|//')"; fi

# Standalone fallbacks are generated from the sourceable modules. Each line
# is comment-encoded so static function inventories see only the active
# definitions. test_standalone_module_copies_match_source_files prevents drift.
# BEGIN GENERATED STANDALONE MODULE: lib/notify.sh
#|#!/usr/bin/env bash
#|# Notification and crash-alert integration shared by launchd-kit and job-host.
#|
#|if [[ "${_LK_NOTIFY_SH_LOADED:-0}" == "1" ]]; then
#|  return 0
#|fi
#|
#|# Dependencies are supplied by launchd-kit/lib.sh or job-host/host.sh. The
#|# function-name variables let the host use its native API without defining
#|# additional compatibility names.
#|_LK_NOTIFY_TIMEOUT_FN="${_LK_NOTIFY_TIMEOUT_FN:-_lk_timeout}"
#|_LK_NOTIFY_ON_EXIT_FN="${_LK_NOTIFY_ON_EXIT_FN:-lk_on_exit}"
#|if ! declare -F "$_LK_NOTIFY_TIMEOUT_FN" >/dev/null \
#|    || ! declare -F "$_LK_NOTIFY_ON_EXIT_FN" >/dev/null; then
#|  echo "notify.sh: requires timeout and exit-handler functions" >&2
#|  return 1
#|fi
#|_LK_NOTIFY_SH_LOADED=1
#|
#|# --- shared color semantics (launchd-kit/discord-notify.md) -------------
#|LK_COLOR_GREEN=5763719      # success / all-clear
#|LK_COLOR_YELLOW=16705372    # warning / attention
#|LK_COLOR_RED=15548997       # failure / error
#|LK_COLOR_BLURPLE=5793266    # neutral / informational (default)
#|
#|# --- lk_notify: post a Discord embed (never breaks the job) ------------------
#|# Body on stdin; delegates to the shell discord-notify.sh (webhook resolution,
#|# truncation, secrets hygiene all live there). Always returns 0 — a notify is a
#|# side effect and must never fail the caller (discord-notify.md).
#|# NOTIFY_SCRIPT names one readable regular shell script, run through bash;
#|# executable permission is not required.
#|#
#|# The notifier call is HARD-BOUNDED through _lk_timeout. "Never fails the
#|# caller" is only half a contract if the call can never RETURN: a notifier
#|# wedged on a Keychain prompt, a stalled 1Password lookup or a black-holed
#|# webhook socket parks the wrapper on it forever — which IS the broken job this
#|# function exists to promise against, just arriving as a hang instead of a
#|# non-zero exit. NOTIFY_SCRIPT is caller-overridable, so lk_notify can never
#|# assume the notifier bounds itself; the guard belongs HERE, at the call.
#|#
#|# The bound is deliberately GENEROUS (default 300s), not tight. The stock
#|# discord-notify.sh is already internally bounded, but its worst LEGITIMATE
#|# path is long — and longer than it first looks, because the Connect leg is
#|# three HTTP round trips, not one:
#|#   op-resolve.py Connect   resolve_ref does 3 lookups (vaults -> items ->
#|#                           item GET), each retried once at HTTP_TIMEOUT=10s
#|#                                                            3 x 2 x 10s = 60s
#|#   legacy `op read`        2 attempts x 90s + a 2s backoff            = 182s
#|#   curl                    --connect-timeout 10 --max-time 20         =  20s
#|#                                                                      ~= 262s
#|# (~272s counting _lk_timeout's two 5s --kill-after graces.) A cold launchd
#|# context routinely spends 30-60s in its first op call alone. A bound set below
#|# that ceiling would not prevent a wedge — it would silently DROP real
#|# notifications, the more expensive failure of the two, and one this fleet has
#|# already taken: a 15s op bound spuriously failed the weekly sweeps' posts
#|# (2026-07-04, see discord-notify.sh).
#|#
#|# 300s clears that ceiling while staying under the shortest tick in the fleet
#|# (morning-brief polls every 10 min), so a wedged notifier releases before the
#|# next tick — and lk_single_lock's 120-min stale window covers any overlap
#|# regardless. But the HEADROOM IS THIN: ~28s. If any leg above grows (another
#|# Connect lookup, a longer op retry, a slower webhook), RAISE THIS FIRST — an
#|# under-set bound does not fail loudly, it quietly eats real notifications.
#|# Override per-wrapper with LK_NOTIFY_TIMEOUT_SECS.
#|#
#|# A timeout means the post is GONE, so it gets its OWN log line and is never
#|# folded into the generic "delivery failed" message: when reading a run log,
#|# a notification that was killed unsent must be tellable apart from one the
#|# webhook actively rejected.
#|lk_notify() {
#|  local title="${1:-$TOOL_NAME}"
#|  local color="${2:-$LK_COLOR_BLURPLE}"
#|  local notify="${NOTIFY_SCRIPT:-$HOME/.claude/scripts/discord-notify.sh}"
#|  local secs="${LK_NOTIFY_TIMEOUT_SECS:-300}"
#|  local body rc=0 started
#|  body="$(cat)"
#|
#|  if [[ ! -f "$notify" || ! -r "$notify" ]]; then
#|    echo "lk_notify: notify script not a readable regular file ($notify) — skipping post" >&2
#|    return 0
#|  fi
#|
#|  # The body stays on STDIN — that is the notifier's interface, and _lk_timeout
#|  # preserves it on every tier (1/2 exec the child from GNU timeout, 3 forks and
#|  # execs from perl, 4 backgrounds under `set -m` so job control is active and
#|  # bash does NOT redirect the async child's stdin from /dev/null). Do not
#|  # "simplify" this into an argument or a temp file.
#|  #
#|  # Timed from AFTER the body is read, so a slow producer upstream of us is not
#|  # charged against the notifier's bound.
#|  started=$SECONDS
#|  printf '%s' "$body" | "$_LK_NOTIFY_TIMEOUT_FN" "$secs" bash "$notify" "$title" "$color" || rc=$?
#|
#|  if (( (rc == 124 || rc == 137) && SECONDS - started >= secs )); then
#|    # 124 = the SIGTERM at the bound did it; 137 = 128+SIGKILL, i.e. the child
#|    # swallowed the TERM and the unignorable follow-up is what ended it.
#|    #
#|    # The ELAPSED CHECK is not redundant. Neither code is unique to a timeout:
#|    # 137 is "died on SIGKILL" for ANY reason — the OOM killer, an operator's
#|    # `kill -9`, a notifier that kills itself — and 124 is a perfectly legal
#|    # exit status for a program to choose. Claiming "exceeded ${secs}s" for a
#|    # notifier that died in under a second would make this line lie in exactly
#|    # the situation it exists to clarify, sending whoever reads the run log
#|    # after a dropped post off to hunt a timeout that never happened. Only
#|    # report a timeout when the clock actually corroborates one.
#|    echo "lk_notify: notifier exceeded ${secs}s and was killed — NO post delivered (swallowed)" >&2
#|  elif (( rc != 0 )); then
#|    echo "lk_notify: delivery failed (swallowed) — see local log" >&2
#|  fi
#|  return 0                            # UNCONDITIONAL: a notify never fails the job
#|}
#|
#|# --- lk_notify_strict: post a Discord embed, PROPAGATE the delivery status -----
#|# Same interface as lk_notify (title, color, body on stdin) but returns the
#|# notifier's exit status instead of swallowing it. Use this for 2xx-gated marker
#|# patterns — where a state marker must be written ONLY after Discord accepts the
#|# post (e.g. macrovoices-fetch's done-week / miss-week markers). A missing or
#|# unreadable or non-regular notifier is treated as a delivery failure (non-zero) here,
#|# because the caller is relying on delivery to have happened.
#|#
#|# Do NOT use this in a crash-alert or other must-never-fail path — that contract
#|# belongs to lk_notify. This one is deliberately allowed to fail the caller.
#|lk_notify_strict() {
#|  local title="${1:-$TOOL_NAME}"
#|  local color="${2:-$LK_COLOR_BLURPLE}"
#|  local notify="${NOTIFY_SCRIPT:-$HOME/.claude/scripts/discord-notify.sh}"
#|  local body
#|  body="$(cat)"
#|
#|  if [[ ! -f "$notify" || ! -r "$notify" ]]; then
#|    echo "lk_notify_strict: notify script not a readable regular file ($notify) — treating as delivery failure" >&2
#|    return 1
#|  fi
#|  printf '%s' "$body" | bash "$notify" "$title" "$color"
#|}
#|
#|# --- lk_enable_crash_alert: red Discord ping on non-zero exit ----------------
#|# Install after launchd_kit_init. On any non-zero exit of the script it fires a
#|# RED notify with the tail of the run log, so a silent crash is visible in
#|# Discord instead of dying quietly. Success (exit 0) is silent.
#|lk_enable_crash_alert() {
#|  "$_LK_NOTIFY_ON_EXIT_FN" _lk_crash_alert
#|}
#|
#|_lk_crash_alert() {
#|  (( LK_EXIT_CODE == 0 )) && return 0             # success is silent
#|  printf 'Scheduled %s did NOT complete cleanly (exit %s).\nHost: %s\nLog: %s\n\nLast 25 log lines:\n```\n%s\n```\n' \
#|    "$TOOL_NAME" "$LK_EXIT_CODE" "$(hostname)" "${LOG:-<no log>}" \
#|    "$(tail -n 25 "${LOG:-/dev/null}" 2>/dev/null)" \
#|    | lk_notify "${TOOL_NAME} — run failed" "$LK_COLOR_RED" || true
#|}
# END GENERATED STANDALONE MODULE: lib/notify.sh
# BEGIN GENERATED STANDALONE MODULE: lib/chain.sh
#|#!/usr/bin/env bash
#|# Static fallback-chain integration shared by launchd-kit and job-host.
#|
#|if [[ "${_LK_CHAIN_SH_LOADED:-0}" == "1" ]]; then
#|  return 0
#|fi
#|
#|# Dependency supplied by launchd-kit/lib.sh or job-host/host.sh.
#|_LK_CHAIN_TIMEOUT_FN="${_LK_CHAIN_TIMEOUT_FN:-_lk_timeout}"
#|if ! declare -F "$_LK_CHAIN_TIMEOUT_FN" >/dev/null; then
#|  echo "chain.sh: requires a timeout function" >&2
#|  return 1
#|fi
#|_LK_CHAIN_SH_LOADED=1
#|
#|# --- lk_run_chain: static fallback-on-hard-failure chain ---------------------
#|# A caller-defined ORDERED list of pinned engine
#|# invocations, tried in sequence, advancing to the next ONLY on a HARD
#|# failure of the current one — never on output quality or content (this
#|# function has no opinion on what a link said, only on whether it said
#|# enough to exist). To keep fallback deterministic,
#|# this is a fixed, committed policy artifact (the chain and its order ARE
#|# the policy, edited by commit), not a router or a quality-based selector.
#|#
#|# Usage:  lk_run_chain <name1> <cmd1> [<name2> <cmd2> ...]
#|#
#|#   <nameN>   a short id for the link (e.g. "claude", "codex") — used only in
#|#             the LK_CHAIN_* result vars below and in log/notify text; never
#|#             interpreted.
#|#   <cmdN>    ONE shell command line, run as `bash -c "<cmdN>"` — exactly the
#|#             shape of an engine.sh case-arm. Bash 3.2 has no nested arrays
#|#             and no namerefs, so a single self-contained command string per
#|#             link (not an argv array) is this file's existing convention for
#|#             passing "the actual command to run" across a function boundary.
#|#             THE CALLER'S CONTRACT: bake any variable a link needs (a prompt
#|#             file path, a model id) into the string at BUILD time — a plain
#|#             double-quoted shell string interpolates them before lk_run_chain
#|#             ever sees the result, so no export-for-a-subshell dance is
#|#             needed. A link's own stdin redirection (e.g. `<"$PROMPT"`)
#|#             belongs INSIDE the string for the same reason. A link's stderr
#|#             is NOT captured — it passes straight through to lk_run_chain's
#|#             own stderr (the caller's usual `2>>"$LOG"` redirection at the
#|#             call site works exactly as it does for a bare command).
#|#
#|# HARD failure (and only these three — see the file header's carve-out
#|# language above) advances to the next link:
#|#   * non-zero exit
#|#   * exit 0 but stdout is under LK_CHAIN_MIN_BYTES (default 200) bytes
#|#   * a timeout, bounded by LK_CHAIN_TIMEOUT_SECS (default 240s) via the
#|#     shared _lk_timeout guard (so a wedged link cannot hang the wrapper any
#|#     more than any other _lk_timeout-guarded call in this file can). A
#|#     124/137 exit is corroborated against ELAPSED TIME (the same pattern
#|#     lk_notify already uses) before it is labeled "timeout" — an engine
#|#     that happens to exit 124/137 on its OWN, well under the bound, is
#|#     reported as an ordinary exit=124/137, never mislabeled.
#|#
#|# ERREXIT-SAFE regardless of the caller's own `set -e`/`errexit` state or
#|# how lk_run_chain itself is invoked (wrapped in `if` or not): every
#|# command whose non-zero status this function needs to inspect is captured
#|# via `if cmd; then rc=0; else rc=$?; fi`, never a bare command left to
#|# propagate through `set -e` on its own. A prior version of this function
#|# called `_lk_timeout` bare and relied on the CALLER already wrapping the
#|# whole `lk_run_chain` invocation in an `if` — bash's errexit-suspension for
#|# an `if`-tested function does not reliably reach every nested command in
#|# every bash version/context, so link 1's hard failure could abort the
#|# whole calling shell before link 2 ever ran. Fixed.
#|#
#|# PROCESS-GROUP CLEANUP. Each link runs in ITS OWN process group, and
#|# lk_run_chain sweeps (TERM, then KILL after a short grace) any remaining
#|# member of that group immediately after the link resolves — REGARDLESS of
#|# whether it hard-failed, timed out, or served the run. This matters
#|# because only a genuine _lk_timeout-triggered kill cleans up a link's
#|# process group on its own; an ORDINARY non-zero exit does not — a link
#|# that backgrounds something and then exits 1 without waiting for it would
#|# otherwise leave that something running into the NEXT link's attempt and
#|# beyond lk_run_chain's own return. The group boundary is established at
#|# the INNERMOST point (immediately around the link's own command, inside
#|# whichever `_lk_timeout` tier ends up running it) and recorded to a
#|# scratch pid file lk_run_chain reads back — necessary because the four
#|# `_lk_timeout` tiers place the monitored command in DIFFERENT groups
#|# relative to themselves (GNU timeout/gtimeout leave it in their OWN group;
#|# the perl and bash-native tiers each `setpgid`/job-control it into a FRESH
#|# one) — an outer-only wrapper would only be correct for some tiers. A
#|# failed pid-file mktemp degrades to no sweep for that call (best-effort;
#|# the chain's own link-selection logic is unaffected either way).
#|#
#|# CALLER CONTRACT — redirect, never command-substitute. Capture the winning
#|# link's output with plain redirection at the call site (`lk_run_chain ... \
#|# >"$RAW"`), exactly like a bare command. Do NOT wrap the call in `$(...)`:
#|# command substitution runs lk_run_chain in a SUBSHELL, so every LK_CHAIN_*
#|# assignment below would vanish the instant the call returns — the same
#|# family of pitfall LK_EXIT_CODE's exit-registry design (top of this file)
#|# exists to avoid for trap handlers. Redirection creates no subshell, so the
#|# result vars land in the caller's own shell as documented.
#|#
#|# On success (some link produced a real answer): prints that link's stdout
#|# on lk_run_chain's own stdout (byte-for-byte — captured via a temp FILE and
#|# `cat`, deliberately not `$(...)` command substitution internally either,
#|# which would strip trailing newlines and cannot carry a NUL byte) and sets,
#|# for the caller to read immediately after the call (these are NOT local —
#|# they are the function's return channel, same pattern as LK_EXIT_CODE above):
#|#   LK_CHAIN_SERVED_NAME    the <nameN> of the link that served
#|#   LK_CHAIN_SERVED_INDEX   its 1-based position in the list
#|#   LK_CHAIN_FELL_BACK      "1" if a later link served (index > 1), else "0"
#|#   LK_CHAIN_FAILURES       "name:reason" per link tried before the served
#|#                           one, semicolon-joined (empty when link 1 served);
#|#                           reason is one of exit=<N> / empty-output(<n>b) /
#|#                           timeout
#|# Returns 0.
#|#
#|# On TOTAL failure (every link hard-failed): prints nothing on stdout,
#|# LK_CHAIN_SERVED_NAME is empty, LK_CHAIN_FAILURES names every link's
#|# reason, and this function returns 1 — an ordinary non-zero return. It
#|# does NOT itself notify or log a crash: the caller's existing failure path
#|# (lk_on_exit's crash alert, or its own `if ! lk_run_chain ...; then ...`)
#|# handles a chain's total failure exactly like any other hard failure of a
#|# single-engine call, unchanged.
#|lk_run_chain() {
#|  LK_CHAIN_SERVED_NAME=""
#|  LK_CHAIN_SERVED_INDEX=""
#|  LK_CHAIN_FELL_BACK="0"
#|  LK_CHAIN_FAILURES=""
#|
#|  if [[ $# -lt 2 ]]; then
#|    echo "lk_run_chain: need at least one <name> <cmd> pair" >&2
#|    return 2
#|  fi
#|  if (( $# % 2 != 0 )); then
#|    echo "lk_run_chain: expected pairs of <name> <cmd> — got an odd argument count ($#)" >&2
#|    return 2
#|  fi
#|
#|  local min_bytes="${LK_CHAIN_MIN_BYTES:-200}"
#|  local secs="${LK_CHAIN_TIMEOUT_SECS:-240}"
#|  local out_tmp pid_file
#|  out_tmp="$(mktemp "${TMPDIR:-/tmp}/lk_run_chain.out.XXXXXX" 2>/dev/null)" || {
#|    echo "lk_run_chain: mktemp failed — cannot capture link output" >&2
#|    return 2
#|  }
#|  # Best-effort: a failure here degrades to "no process-group sweep for this
#|  # call" (pid_file left empty), never a hard failure of the whole function —
#|  # see the doc comment above.
#|  pid_file="$(mktemp "${TMPDIR:-/tmp}/lk_run_chain.pid.XXXXXX" 2>/dev/null)" || pid_file=""
#|
#|  local idx=0 name cmd rc bytes reason link_pgid started elapsed
#|  while [[ $# -gt 0 ]]; do
#|    idx=$((idx + 1))
#|    name="$1"; cmd="$2"; shift 2
#|
#|    : >"$out_tmp"
#|    link_pgid=""
#|    started=$SECONDS
#|
#|    if [[ -n "$pid_file" ]]; then
#|      : >"$pid_file"
#|      # The wrapper script establishes a FRESH process group at the
#|      # innermost point, immediately around the link's own command: `set -m`
#|      # (job control) + backgrounding makes the `eval`'d command's own PID
#|      # its process-group id, and that PID is recorded to $2 (pid_file)
#|      # before `wait` blocks on it — so lk_run_chain can find and sweep that
#|      # exact group afterward no matter which _lk_timeout tier ran it. $1 is
#|      # the caller's original command STRING, passed as a real argv element
#|      # (never string-concatenated) so it needs no re-escaping here; `eval`
#|      # re-parses and runs it exactly as `bash -c "$cmd"` would.
#|      if "$_LK_CHAIN_TIMEOUT_FN" "$secs" bash -c '
#|          set -m
#|          eval "$1" &
#|          printf "%s" "$!" >"$2"
#|          wait "$!"
#|        ' _ "$cmd" "$pid_file" >"$out_tmp"; then
#|        rc=0
#|      else
#|        rc=$?
#|      fi
#|      link_pgid="$(cat "$pid_file" 2>/dev/null)" || link_pgid=""
#|      [[ "$link_pgid" =~ ^[0-9]+$ ]] || link_pgid=""
#|    else
#|      if "$_LK_CHAIN_TIMEOUT_FN" "$secs" bash -c "$cmd" >"$out_tmp"; then
#|        rc=0
#|      else
#|        rc=$?
#|      fi
#|    fi
#|    elapsed=$((SECONDS - started))
#|
#|    # Sweep the link's own process group UNCONDITIONALLY (success, hard
#|    # failure, or timeout alike) — see PROCESS-GROUP CLEANUP above. TERM
#|    # first, then KILL after a short grace for anything that ignored it;
#|    # both are no-ops (silently) if the group is already empty.
#|    if [[ -n "$link_pgid" ]]; then
#|      # `|| true` on BOTH: `kill` returns non-zero (ESRCH) the instant the
#|      # group is already empty — the COMMON case, not the exception — and a
#|      # bare non-zero here would abort the whole function under a CALLER's
#|      # `set -e` on every ordinary link, not just the orphan-cleanup case
#|      # this sweep exists for.
#|      kill -TERM -- "-$link_pgid" 2>/dev/null || true
#|      sleep 0.3
#|      kill -KILL -- "-$link_pgid" 2>/dev/null || true
#|    fi
#|
#|    bytes="$(wc -c <"$out_tmp" 2>/dev/null | tr -d ' ')" || true
#|    [[ "$bytes" =~ ^[0-9]+$ ]] || bytes=0
#|
#|    if (( (rc == 124 || rc == 137) && elapsed >= secs )); then
#|      reason="timeout"
#|    elif (( rc != 0 )); then
#|      reason="exit=$rc"
#|    elif (( bytes < min_bytes )); then
#|      reason="empty-output(${bytes}b)"
#|    else
#|      reason=""
#|    fi
#|
#|    if [[ -z "$reason" ]]; then
#|      LK_CHAIN_SERVED_NAME="$name"
#|      LK_CHAIN_SERVED_INDEX="$idx"
#|      (( idx > 1 )) && LK_CHAIN_FELL_BACK="1"
#|      cat "$out_tmp"
#|      rm -f "$out_tmp" "$pid_file"
#|      return 0
#|    fi
#|
#|    LK_CHAIN_FAILURES="${LK_CHAIN_FAILURES:+$LK_CHAIN_FAILURES;}${name}:${reason}"
#|  done
#|
#|  rm -f "$out_tmp" "$pid_file"
#|  return 1
#|}
# END GENERATED STANDALONE MODULE: lib/chain.sh
# BEGIN GENERATED STANDALONE MODULE: lib/telemetry.sh
#|#!/usr/bin/env bash
#|# serotine.run.v1 payload and sink integration shared by launchd-kit and job-host.
#|
#|if [[ "${_LK_TELEMETRY_SH_LOADED:-0}" == "1" ]]; then
#|  return 0
#|fi
#|
#|# Dependencies are supplied by launchd-kit/lib.sh or job-host/host.sh.
#|_LK_TELEMETRY_TIMEOUT_FN="${_LK_TELEMETRY_TIMEOUT_FN:-_lk_timeout}"
#|_LK_TELEMETRY_ON_EXIT_FN="${_LK_TELEMETRY_ON_EXIT_FN:-lk_on_exit}"
#|if ! declare -F "$_LK_TELEMETRY_TIMEOUT_FN" >/dev/null \
#|    || ! declare -F "$_LK_TELEMETRY_ON_EXIT_FN" >/dev/null; then
#|  echo "telemetry.sh: requires timeout and exit-handler functions" >&2
#|  return 1
#|fi
#|_LK_TELEMETRY_SH_LOADED=1
#|
#|# Resolve from lib.sh for its generated copy and one directory up for this
#|# sourceable module, without relying on the caller's working directory.
#|_LK_TELEMETRY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && { [[ "${BASH_SOURCE[0]##*/}" != "telemetry.sh" ]] || cd ..; } && pwd)"
#|
#|# ==============================================================================
#|# serotine.run.v1 telemetry (opt-in)
#|# ------------------------------------------------------------------------------
#|# One JSONL line per run appended to a local spool, then best-effort POSTed to a
#|# Fleet/Serotine ingest API.
#|# The wire namespace is serotine.*.
#|# The line shape is validated by telemetry/validate_serotine_run.py against
#|# telemetry/serotine-run-v1.schema.json.
#|#
#|# SAFETY (the #1 property — lib.sh fronts LIVE production jobs): nothing in this
#|# section may ever fail or block the wrapper. Every path swallows errors, the
#|# push has a short timeout and never retries within a run, and the wrapper's exit
#|# code is completely unaffected by anything the hook does — same posture as
#|# lk_notify. The public entry points ALWAYS return 0.
#|#
#|# The product name is provisional (telemetry/README.md § Renaming checklist);
#|# every namespace occurrence is kept as the literal, grep-able prefix `serotine.`
#|# / `SEROTINE_` on purpose so a rename is a single mechanical sweep.
#|# ==============================================================================
#|
#|# RFC 3339 timestamp with an explicit colon offset. Arg: epoch
#|# seconds (default: now). Empty string on any failure (callers tolerate it).
#|_lk_rfc3339() {
#|  local epoch="${1:-$(date +%s 2>/dev/null)}"
#|  { date -r "$epoch" +%Y-%m-%dT%H:%M:%S%z 2>/dev/null ||
#|    date -d "@$epoch" +%Y-%m-%dT%H:%M:%S%z 2>/dev/null; } \
#|    | sed -E 's/([0-9]{2})([0-9]{2})$/\1:\2/'
#|}
#|
#|# 4 lowercase hex chars for the serotine.run.id suffix. openssl → urandom → RANDOM.
#|_lk_rand4() {
#|  local h
#|  h="$(openssl rand -hex 2 2>/dev/null)" || h=""
#|  if [[ ! "$h" =~ ^[0-9a-f]{4}$ ]]; then
#|    h="$(od -An -N2 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')"
#|  fi
#|  [[ "$h" =~ ^[0-9a-f]{4}$ ]] || h="$(printf '%04x' $((RANDOM & 0xffff)))"
#|  printf '%s' "$h"
#|}
#|
#|# lk_enable_run_telemetry: record the run start + arm the EXIT trap fallback.
#|# Call once after launchd_kit_init and BEFORE the guards. Purely additive.
#|lk_enable_run_telemetry() {
#|  LK_TELEMETRY_ON=1
#|  LK_EMITTED=0
#|  SEROTINE_RUN_START="$(_lk_rfc3339 2>/dev/null || true)"
#|  export LK_TELEMETRY_ON LK_EMITTED SEROTINE_RUN_START
#|  "$_LK_TELEMETRY_ON_EXIT_FN" _lk_emit_run_exit
#|}
#|
#|# _lk_emit_run_exit: EXIT-trap fallback. Only fires when the wrapper never
#|# reached its explicit lk_emit_run — i.e. it crashed (non-zero exit → fail with
#|# the real exit code) or a slot-guard/lock short-circuited (exit 0 → skip).
#|_lk_emit_run_exit() {
#|  [[ "${LK_TELEMETRY_ON:-0}" == "1" ]] || return 0
#|  [[ "${LK_EMITTED:-0}" == "1" ]] && return 0
#|  local status
#|  if (( ${LK_EXIT_CODE:-0} == 0 )); then status="skip"; else status="fail"; fi
#|  _lk_emit_run_core --status "$status" --exit-code "${LK_EXIT_CODE:-0}" || true
#|  return 0
#|}
#|
#|# lk_emit_run: the wrapper's explicit terminal emit (the ok path). Call ONCE as
#|# the last thing the wrapper does. Always returns 0; never affects the exit code.
#|lk_emit_run() {
#|  _lk_emit_run_core "$@" || true
#|  LK_EMITTED=1
#|  return 0
#|}
#|
#|# _lk_spool_write_line: write one JSONL record (`$1\n`) onto `$2`, optionally
#|# prefixed by the separator in `$3`. Isolated so tests can stub a short write
#|# without filling a disk. A complete or empty spool passes no separator, so its
#|# healthy-disk bytes remain exactly `printf '%s\n'`.
#|_lk_spool_write_line() {
#|  printf '%s%s\n' "${3:-}" "$1" >>"$2"
#|}
#|
#|# _lk_truncate_spool: shrink `$1` to `$2` bytes (0 = empty) IN PLACE, so a
#|# full disk cannot block recovery. Only returns 0 when the resulting size
#|# matches; the caller logs "truncated" only then.
#|_lk_truncate_spool() {
#|  local path="$1" bytes="$2" got
#|  [[ -f "$path" ]] || return 0
#|  if (( bytes <= 0 )); then
#|    : >"$path" || return 1
#|  elif command -v python3 >/dev/null 2>&1; then
#|    python3 -c 'import os,sys; os.truncate(sys.argv[1], int(sys.argv[2]))' \
#|      "$path" "$bytes" || return 1
#|  elif command -v perl >/dev/null 2>&1; then
#|    perl -e 'truncate($ARGV[0], 0+$ARGV[1]) or die $!' "$path" "$bytes" \
#|      || return 1
#|  else
#|    return 1
#|  fi
#|  got="$(wc -c <"$path" 2>/dev/null | tr -d ' ')"
#|  [[ "$got" =~ ^[0-9]+$ ]] && (( got == bytes )) || return 1
#|  return 0
#|}
#|
#|# _lk_spool_size <path>: print the byte size of a spool. Non-zero when the
#|# probe exits non-zero or the output is not an integer. An unreadable size
#|# is not zero — callers must not coerce a failure into an empty spool.
#|_lk_spool_size() {
#|  local path="$1" raw
#|  raw="$(wc -c <"$path" 2>/dev/null)" || return 1
#|  raw="${raw#"${raw%%[![:space:]]*}"}"
#|  raw="${raw%"${raw##*[![:space:]]}"}"
#|  [[ "$raw" =~ ^[0-9]+$ ]] || return 1
#|  printf '%s\n' "$raw"
#|}
#|
#|
#|# An unknown spool size never authorizes a truncate.
#|# _lk_append_spool_line <line> <spool>: append one fully formed JSONL record.
#|# Append + verify + truncate-back run under an exclusive flock on
#|# <spool>.lock, shared with telemetry/serotine_spool.py. On a short write
#|# or writer failure, truncate back to the pre-write size so no torn record
#|# remains — only when that size was read. A pre-append size probe that
#|# exits non-zero or prints a non-integer refuses before writing. A
#|# post-append probe that fails the same way is a named error and does not
#|# roll back. If the lock cannot be taken within SEROTINE_SPOOL_LOCK_TIMEOUT
#|# seconds (default 5; finite > 0, capped at 300; anything else uses 5),
#|# do not write and do not truncate. An unopenable <spool>.lock is a named
#|# error, not a silent refusal. Reports via lk_log
#|# (else stderr) and returns non-zero; callers in live jobs must swallow
#|# that (lk_emit_run always returns 0).
#|_lk_append_spool_line() {
#|  local line="$1" spool="$2"
#|  local pre=0 size=0 expected rc=0 size_known=0
#|  local last_byte separator=""
#|  local lock="${spool}.lock"
#|  local timeout timeout_i max_i
#|  local hold status release pid i s locked=0 crit_rc=0
#|
#|  if ! command -v python3 >/dev/null 2>&1; then
#|    if type lk_log >/dev/null 2>&1; then
#|      lk_log "spool append failed ($spool); lock not acquired"
#|    else
#|      echo "[launchd-kit] spool append failed ($spool); lock not acquired" >&2
#|    fi
#|    return 1
#|  fi
#|
#|  timeout="$(python3 -c '
#|import math, os
#|raw = os.environ.get("SEROTINE_SPOOL_LOCK_TIMEOUT")
#|default, cap = 5.0, 300.0
#|try:
#|    v = float(raw) if raw else default
#|except (TypeError, ValueError):
#|    v = default
#|if not math.isfinite(v) or v <= 0:
#|    v = default
#|elif v > cap:
#|    v = cap
#|print("%.6f" % v)
#|' 2>/dev/null)" || true
#|  [[ "$timeout" =~ ^[0-9]+\.[0-9]+$ ]] || timeout="5.000000"
#|  timeout_i="${timeout%.*}"
#|  [[ "$timeout_i" =~ ^[0-9]+$ ]] || timeout_i=5
#|  max_i=$((timeout_i * 50 + 100))
#|  (( max_i >= 1 )) || max_i=350
#|
#|  hold="$(mktemp -d "${TMPDIR:-/tmp}/lk-spool-lock.XXXXXX")" || {
#|    if type lk_log >/dev/null 2>&1; then
#|      lk_log "spool append failed ($spool); lock not acquired"
#|    else
#|      echo "[launchd-kit] spool append failed ($spool); lock not acquired" >&2
#|    fi
#|    return 1
#|  }
#|  status="$hold/st"
#|  release="$hold/rel"
#|  : > "$status"
#|  python3 -c '
#|import errno, fcntl, os, sys, time
#|lock, status, release, timeout = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
#|try:
#|    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
#|except OSError:
#|    try:
#|        with open(status, "w") as f:
#|            f.write("unopenable\n")
#|    except OSError:
#|        pass
#|    sys.exit(2)
#|deadline = time.monotonic() + timeout
#|held = False
#|try:
#|    while True:
#|        try:
#|            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
#|            held = True
#|            break
#|        except OSError as e:
#|            if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
#|                with open(status, "w") as f:
#|                    f.write("err\n")
#|                sys.exit(2)
#|            if time.monotonic() >= deadline:
#|                with open(status, "w") as f:
#|                    f.write("timeout\n")
#|                sys.exit(1)
#|            time.sleep(0.02)
#|    with open(status, "w") as f:
#|        f.write("ok\n")
#|    while not os.path.exists(release):
#|        if os.getppid() == 1:
#|            break
#|        time.sleep(0.02)
#|finally:
#|    if held:
#|        try:
#|            fcntl.flock(fd, fcntl.LOCK_UN)
#|        except OSError:
#|            pass
#|    os.close(fd)
#|' "$lock" "$status" "$release" "$timeout" &
#|  pid=$!
#|  i=0
#|  while (( i < max_i )); do
#|    s="$(cat "$status" 2>/dev/null || true)"
#|    case "$s" in
#|      ok*) locked=1; break ;;
#|      unopenable*) break ;;
#|      timeout*|err*) break ;;
#|    esac
#|    if ! kill -0 "$pid" 2>/dev/null; then
#|      break
#|    fi
#|    sleep 0.02
#|    i=$((i + 1))
#|  done
#|  if (( locked != 1 )); then
#|    kill "$pid" 2>/dev/null || true
#|    : > "$release"
#|    wait "$pid" 2>/dev/null || true
#|    s="$(cat "$status" 2>/dev/null || true)"
#|    rm -f "$status" "$release"
#|    rmdir "$hold" 2>/dev/null || true
#|    if [[ "$s" == unopenable* ]]; then
#|      if type lk_log >/dev/null 2>&1; then
#|        lk_log "spool lock unopenable ($lock)"
#|      else
#|        echo "[launchd-kit] spool lock unopenable ($lock)" >&2
#|      fi
#|    elif type lk_log >/dev/null 2>&1; then
#|      lk_log "spool append failed ($spool); lock not acquired"
#|    else
#|      echo "[launchd-kit] spool append failed ($spool); lock not acquired" >&2
#|    fi
#|    return 1
#|  fi
#|
#|  if [[ -f "$spool" ]]; then
#|    if ! pre="$(_lk_spool_size "$spool")"; then
#|      : > "$release"
#|      wait "$pid" 2>/dev/null || true
#|      rm -f "$status" "$release"
#|      rmdir "$hold" 2>/dev/null || true
#|      if type lk_log >/dev/null 2>&1; then
#|        lk_log "spool size unreadable; append skipped"
#|      else
#|        echo "[launchd-kit] spool size unreadable; append skipped" >&2
#|      fi
#|      return 1
#|    fi
#|  fi
#|  if (( pre > 0 )); then
#|    last_byte="$(tail -c 1 "$spool" 2>/dev/null \
#|      | od -An -tu1 2>/dev/null | tr -d '[:space:]')"
#|    if [[ ! "$last_byte" =~ ^[0-9]+$ ]]; then
#|      : > "$release"
#|      wait "$pid" 2>/dev/null || true
#|      rm -f "$status" "$release"
#|      rmdir "$hold" 2>/dev/null || true
#|      if type lk_log >/dev/null 2>&1; then
#|        lk_log "spool final byte unreadable; append skipped"
#|      else
#|        echo "[launchd-kit] spool final byte unreadable; append skipped" >&2
#|      fi
#|      return 1
#|    fi
#|    [[ "$last_byte" == 10 ]] || separator=$'\n'
#|  fi
#|  expected="$(printf '%s%s\n' "$separator" "$line" | wc -c | tr -d ' ')"
#|  if [[ ! "$expected" =~ ^[0-9]+$ ]]; then
#|    : > "$release"
#|    wait "$pid" 2>/dev/null || true
#|    rm -f "$status" "$release"
#|    rmdir "$hold" 2>/dev/null || true
#|    return 1
#|  fi
#|  _lk_spool_write_line "$line" "$spool" "$separator" || rc=$?
#|  if [[ -f "$spool" ]]; then
#|    if size="$(_lk_spool_size "$spool")"; then
#|      size_known=1
#|    fi
#|  else
#|    size=0
#|    size_known=1
#|  fi
#|  if (( size_known != 1 )); then
#|    if type lk_log >/dev/null 2>&1; then
#|      lk_log "spool size unreadable; append not truncated"
#|    else
#|      echo "[launchd-kit] spool size unreadable; append not truncated" >&2
#|    fi
#|    crit_rc=1
#|  elif (( rc != 0 )) || (( size != pre + expected )); then
#|    if _lk_truncate_spool "$spool" "$pre"; then
#|      if type lk_log >/dev/null 2>&1; then
#|        lk_log "spool append failed ($spool); truncated to ${pre} bytes"
#|      else
#|        echo "[launchd-kit] spool append failed ($spool); truncated to ${pre} bytes" >&2
#|      fi
#|    else
#|      if type lk_log >/dev/null 2>&1; then
#|        lk_log "spool append failed ($spool); truncate to ${pre} bytes failed"
#|      else
#|        echo "[launchd-kit] spool append failed ($spool); truncate to ${pre} bytes failed" >&2
#|      fi
#|    fi
#|    crit_rc=1
#|  fi
#|
#|  : > "$release"
#|  wait "$pid" 2>/dev/null || true
#|  rm -f "$status" "$release"
#|  rmdir "$hold" 2>/dev/null || true
#|  return "$crit_rc"
#|}
#|
#|
#|# _lk_emit_run_core: build one serotine.run.v1 line, append to the spool, push.
#|# All work is best-effort; returns 0 unconditionally.
#|_lk_emit_run_core() {
#|  local status="ok" exit_code=0 operation="" usage_json="" usage_file=""
#|  local trigger="${SEROTINE_TRIGGER:-launchd}" label="" failure_message=""
#|  while (( $# )); do
#|    case "$1" in
#|      --status)     status="${2:-ok}"; shift 2 || shift;;
#|      --exit-code)  exit_code="${2:-0}"; shift 2 || shift;;
#|      --operation)  operation="${2:-}"; shift 2 || shift;;
#|      --usage-json) usage_json="${2:-}"; shift 2 || shift;;
#|      --usage-file) usage_file="${2:-}"; shift 2 || shift;;
#|      --trigger)    trigger="${2:-launchd}"; shift 2 || shift;;
#|      --label)      label="${2:-}"; shift 2 || shift;;
#|      --failure-message) failure_message="${2:-}"; shift 2 || shift;;
#|      *) shift;;
#|    esac
#|  done
#|
#|  local helper spool_dir spool cursor host end stamp label_val runid line lk_user
#|  local failure_json
#|  helper="${SEROTINE_EMIT_HELPER:-$_LK_TELEMETRY_ROOT/serotine_usage.py}"
#|  command -v python3 >/dev/null 2>&1 || return 0
#|  [[ -f "$helper" ]] || return 0
#|
#|  spool_dir="${SEROTINE_SPOOL_DIR:-$HOME/.claude/telemetry}"
#|  spool="$spool_dir/spool.jsonl"
#|  cursor="$spool.cursor"
#|  host="${SEROTINE_HOST:-$(hostname -s 2>/dev/null || hostname 2>/dev/null)}"
#|  end="$(_lk_rfc3339)"
#|  stamp="${LK_STAMP:-$(date +%Y-%m-%d_%H%M%S 2>/dev/null)}"
#|  # An explicit --label or SEROTINE_JOB_LABEL wins. The default is
#|  # com.<launchd user>.<tool>: the user part comes from `id -un` (so it is the
#|  # same label as before for the same launchd user) and falls back to
#|  # `unknown`, never an empty segment, if `id` fails.
#|  label_val="${label:-${SEROTINE_JOB_LABEL:-}}"
#|  if [[ -z "$label_val" ]]; then
#|    lk_user="$(id -un 2>/dev/null)" || lk_user=""
#|    label_val="com.${lk_user:-unknown}.${TOOL_NAME:-unknown}"
#|  fi
#|  runid="$label_val/$stamp-$(_lk_rand4)"
#|
#|  # Opt-in failure forensics, BEFORE the line is built (unlike the trace, which
#|  # is a separate POST and must wait for the run line to land).
#|  #
#|  # THE GATE IS REPEATED HERE, at the call site, and that is not redundancy for
#|  # its own sake: a command substitution forks a subshell unconditionally, even
#|  # around a function that returns on its first line. `_lk_attach_trace` below
#|  # is a plain call and forks nothing when disarmed, and this path should cost
#|  # a disarmed job exactly as little. With the flag unset the line below is one
#|  # parameter expansion and an empty assignment — no fork, no subshell, no
#|  # helper. The gate inside _lk_collect_failure stays too: it is the one that
#|  # makes the FUNCTION safe to call directly, which its own tests do.
#|  failure_json=""
#|  if [[ -n "${SEROTINE_EMIT_FAILURE:-}" ]]; then
#|    failure_json="$(_lk_collect_failure "$status" "$failure_message" 2>/dev/null || true)"
#|  fi
#|
#|  # Build the whole line in python3 so JSON quoting is correct by construction
#|  # and the optional serotine.usage array is resolved (provider adapter, when
#|  # SEROTINE_USAGE_PROVIDER is set → explicit → window fallback). The three
#|  # SEROTINE_USAGE_PROVIDER/_INPUT/_REQUEST_MODEL vars are passed through
#|  # exactly like SEROTINE_USAGE_SESSION_ID below (an explicit "${VAR:-}"
#|  # passthrough of whatever the wrapper already exported, defaulting to
#|  # unset/empty) — a wrapper that never sets them sees byte-identical
#|  # behavior; this is PURELY ADDITIVE, same posture as every other opt-in
#|  # var in this hook.
#|  #
#|  # SEROTINE_LINE_FAILURE_JSON is assigned UNCONDITIONALLY, empty when the
#|  # forensics gate is off. That is deliberate and load-bearing: this prefix
#|  # does not clear the rest of the environment, so an assignment that were
#|  # conditional would let a wrapper's own inherited value reach the helper with
#|  # the flag unset. An explicit empty assignment shadows it.
#|  line="$(
#|    SEROTINE_LINE_RUN_ID="$runid" \
#|    SEROTINE_LINE_JOB_LABEL="$label_val" \
#|    SEROTINE_LINE_HOST="$host" \
#|    SEROTINE_LINE_START="${SEROTINE_RUN_START:-$end}" \
#|    SEROTINE_LINE_END="$end" \
#|    SEROTINE_LINE_EXIT_CODE="$exit_code" \
#|    SEROTINE_LINE_STATUS="$status" \
#|    SEROTINE_LINE_TRIGGER="$trigger" \
#|    SEROTINE_LINE_OPERATION="$operation" \
#|    SEROTINE_USAGE_JSON="$usage_json" \
#|    SEROTINE_USAGE_FILE="$usage_file" \
#|    SEROTINE_USAGE_WINDOW_START="${SEROTINE_RUN_START:-}" \
#|    SEROTINE_USAGE_WINDOW_END="$end" \
#|    SEROTINE_USAGE_SESSION_ID="${SEROTINE_USAGE_SESSION_ID:-}" \
#|    SEROTINE_USAGE_DISABLE="${SEROTINE_USAGE_FALLBACK_DISABLE:-0}" \
#|    SEROTINE_USAGE_PROVIDER="${SEROTINE_USAGE_PROVIDER:-}" \
#|    SEROTINE_USAGE_INPUT="${SEROTINE_USAGE_INPUT:-}" \
#|    SEROTINE_USAGE_REQUEST_MODEL="${SEROTINE_USAGE_REQUEST_MODEL:-}" \
#|    SEROTINE_LINE_FAILURE_JSON="$failure_json" \
#|    python3 "$helper" build-line 2>/dev/null
#|  )" || return 0
#|  [[ -n "$line" ]] || return 0
#|
#|  mkdir -p "$spool_dir" 2>/dev/null || true
#|  _lk_append_spool_line "$line" "$spool" || true
#|  _lk_push_spool "$spool" "$cursor" || true
#|  # Opt-in failure trace, AFTER the push: the run line must be ingested before
#|  # its trace or the server answers 404 (store.ts § Contract). Returns
#|  # immediately unless SEROTINE_TRACE_CAPTURE is set — see its own section.
#|  _lk_attach_trace "$runid" "$label_val" "$host" "$status" "$exit_code" || true
#|  return 0
#|}
#|
#|# _lk_push_spool: best-effort POST of unsent spool bytes (byte-offset cursor).
#|# No-op unless BOTH SEROTINE_INGEST_URL and SEROTINE_INGEST_TOKEN are set. On a 2xx
#|# response the cursor advances to the byte size that was read; otherwise the
#|# cursor is untouched so the batch is retried on a later run. run.id is the
#|# ingest dedupe key, so re-POSTing the same lines is safe by design. Never
#|# retries within a run; short timeout; always returns 0.
#|#
#|# serotine.event.findings is SPOOL-ONLY:
#|# the whole array is stripped from the POST body FAIL-CLOSED before it leaves the
#|# machine — a body carrying the key is re-serialized without it, and any line
#|# that cannot be stripped safely is dropped from the body rather than sent
#|# unstripped. This mirrors serotine_spool.py's push layer (its _strip_findings is
#|# reused verbatim, so bash and Python emit byte-identical stripped bytes). A body
#|# with NO findings key skips the strip entirely and is POSTed byte-for-byte as
#|# before — the run.v1 push path is unchanged and needs no python3.
#|# _lk_resync_cursor: repair a byte-offset cursor that no longer names a record
#|# boundary, BEFORE it is used to slice a POST body. Sets $_LK_CURSOR_OFF to the
#|# offset to use; returns non-zero only when it cannot establish a safe one.
#|#
#|# Why this exists: a cleanup removed two synthetic records
#|# from the MIDDLE of the live spool and left the cursor alone. The cursor was
#|# then 780 bytes past the end of the file. Every later push sliced from that
#|# offset, so each body began mid-record (`rotine.schema": ...`), the collector
#|# rejected the malformed batch, and the cursor only advances on a 2xx — so it
#|# never moved and the same broken batch was re-sent every tick, forever. The
#|# whole host went dark to serotine in one edit, with NO local signal: this
#|# path logged nothing, and the only reason it surfaced inside 15 minutes is that
#|# the heartbeat job has a dead-man's-switch. Every other job would have been
#|# silently undelivered for days.
#|#
#|# STRICTLY CONSERVATIVE: the offset only ever moves DOWN, so a resync can cause
#|# a re-send but can never skip a record. Re-sending is safe by design — run.id
#|# is the ingest dedupe key (see _lk_push_spool's header).
#|#
#|# The clamp deliberately trusts the CURRENT file: a spool that shrank is assumed
#|# to have been edited in place (the real case), not rotated and refilled with
#|# different content. Resetting to 0 instead would re-POST the entire history —
#|# megabytes against a 5s timeout, which would simply stall in a new way. The
#|# log line below is what makes the choice reviewable rather than silent.
#|_LK_CURSOR_OFF=""
#|_lk_resync_cursor() {
#|  local spool="$1" cursor="$2" off="$3" size="$4"
#|  local orig="$off" partial byte
#|
#|  # (1) Spool shrank below the cursor — edited, truncated, or restored.
#|  (( off > size )) && off="$size"
#|
#|  # (2) The cursor must sit immediately after a newline (or at 0). Byte $off is
#|  # the last byte of the already-pushed region; seek to it rather than reading
#|  # the file, so the healthy path stays O(1).
#|  if (( off > 0 )); then
#|    byte="$(tail -c +"$off" "$spool" 2>/dev/null | head -c 1 | od -An -tu1 2>/dev/null | tr -cd '0-9')"
#|    if [[ "$byte" != 10 ]]; then
#|      # Back up over the trailing partial record. LC_ALL=C so awk's length()
#|      # counts BYTES, not characters — a UTF-8 payload would otherwise
#|      # under-count and leave the cursor mid-record again. POSIX keeps the last
#|      # record in $0 at END.
#|      partial="$(head -c "$off" "$spool" 2>/dev/null | LC_ALL=C awk 'END{ print length($0) }' 2>/dev/null)"
#|      if [[ ! "$partial" =~ ^[0-9]+$ ]] || (( partial > off )); then
#|        type lk_log >/dev/null 2>&1 && lk_log "spool cursor $orig is not on a record boundary and the boundary could not be located — HELD, nothing pushed this run"
#|        return 1
#|      fi
#|      off=$(( off - partial ))
#|    fi
#|  fi
#|
#|  if (( off != orig )); then
#|    printf '%s' "$off" >"$cursor" 2>/dev/null || true
#|    type lk_log >/dev/null 2>&1 && lk_log "spool cursor resynced ${orig} -> ${off} (spool size ${size}) — cursor was off a record boundary; every push would have been rejected until corrected"
#|  fi
#|  _LK_CURSOR_OFF="$off"
#|  return 0
#|}
#|
#|_lk_push_spool() {
#|  local spool="$1" cursor="$2"
#|  [[ -n "${SEROTINE_INGEST_URL:-}" && -n "${SEROTINE_INGEST_TOKEN:-}" ]] || return 0
#|  command -v curl >/dev/null 2>&1 || return 0
#|  [[ -f "$spool" ]] || return 0
#|
#|  local off size body code strip_helper complete
#|  off="$(cat "$cursor" 2>/dev/null || echo 0)"
#|  [[ "$off" =~ ^[0-9]+$ ]] || off=0
#|  size="$(wc -c <"$spool" 2>/dev/null | tr -d ' ')"
#|  [[ "$size" =~ ^[0-9]+$ ]] || return 0
#|
#|  _lk_resync_cursor "$spool" "$cursor" "$off" "$size" || return 0
#|  off="$_LK_CURSOR_OFF"
#|
#|  complete="$size"
#|  strip_helper="${SEROTINE_STRIP_HELPER:-$_LK_TELEMETRY_ROOT/telemetry/serotine_spool.py}"
#|  if command -v python3 >/dev/null 2>&1 && [[ -f "$strip_helper" ]]; then
#|    if ! complete="$(SEROTINE_STRIP_HELPER="$strip_helper" python3 -c '
#|import os, sys
#|sys.path.insert(0, os.path.dirname(os.environ["SEROTINE_STRIP_HELPER"]))
#|import serotine_spool
#|print(serotine_spool._complete_end(sys.argv[1], int(sys.argv[2])))
#|' "$spool" "$size" 2>/dev/null)"; then
#|      echo "[launchd-kit] spool complete boundary unavailable; push skipped" >&2
#|      return 0
#|    elif [[ ! "$complete" =~ ^[0-9]+$ ]]; then
#|      echo "[launchd-kit] spool complete boundary unavailable; push skipped" >&2
#|      return 0
#|    fi
#|  fi
#|
#|  (( complete > off )) || return 0
#|
#|  if (( complete < size )); then
#|    body="$(tail -c +"$((off + 1))" "$spool" 2>/dev/null | head -c "$((complete - off))")" || return 0
#|  else
#|    body="$(tail -c +"$((off + 1))" "$spool" 2>/dev/null)" || return 0
#|  fi
#|  [[ -n "$body" ]] || return 0
#|
#|  # Drop incomplete/unparseable JSONL records (and strip spool-only findings)
#|  # before POST so a torn last line cannot poison the batch. Complete records
#|  # keep original bytes. A findings-free body still needs no python3: if the
#|  # helper cannot run, the batch is POSTed as-is (same as before). A body that
#|  # carries findings still HOLDs when the helper cannot run.
#|  if command -v python3 >/dev/null 2>&1 && [[ -f "$strip_helper" ]]; then
#|    body="$(printf '%s' "$body" | SEROTINE_STRIP_HELPER="$strip_helper" python3 -c '
#|import os, sys
#|sys.path.insert(0, os.path.dirname(os.environ["SEROTINE_STRIP_HELPER"]))
#|import serotine_spool
#|sys.stdout.buffer.write(serotine_spool._strip_findings(sys.stdin.buffer.read()))
#|' 2>/dev/null)" || return 0
#|    [[ -n "$body" ]] || return 0
#|  elif [[ "$body" == *'"serotine.event.findings"'* ]]; then
#|    return 0
#|  fi
#|
#|  code="$(printf '%s' "$body" | curl -sS -m "${SEROTINE_INGEST_TIMEOUT:-5}" \
#|      -o /dev/null -w '%{http_code}' -X POST \
#|      -H "Authorization: Bearer $SEROTINE_INGEST_TOKEN" \
#|      -H 'Content-Type: application/x-ndjson' \
#|      --data-binary @- "$SEROTINE_INGEST_URL" 2>/dev/null)" || return 0
#|  if [[ "$code" =~ ^2[0-9][0-9]$ ]]; then
#|    printf '%s' "$complete" >"$cursor" 2>/dev/null || true
#|  fi
#|  return 0
#|}
#|
#|# ==============================================================================
#|# Wrapper-side FAILURE FORENSICS on the run line (opt-in per job; OFF fleet-wide)
#|# ------------------------------------------------------------------------------
#|# Failure forensics for the run record's optional diagnostic object. The wire
#|# accepts an optional `serotine.run.failure` object, `/jobs/<label>` renders it
#|# beneath the run it belongs to, and the weekly digest quotes an excerpt. It has
#|# rendered EMPTY on the real fleet the whole time, because nothing on the
#|# wrapper side has ever populated it — serotine's own AGENTS.md says so:
#|# "NOT BUILT HERE: the emitter itself (launchd-kit lib.sh)". This section is
#|# that emitter.
#|#
#|# NOT the same thing as the trace capture below, and not a replacement for it.
#|# The trace is a 200-line log tail POSTed to a SECOND endpoint
#|# (/api/v1/ingest/trace) and stored separately; this is a three-string summary
#|# riding the run LINE, so the answer to "why did this fail?" is on the job page
#|# without a second fetch. Independent flags, independently armed.
#|#
#|# DEFAULT OFF, FLEET-WIDE. `_lk_collect_failure` returns on its first line
#|# unless SEROTINE_EMIT_FAILURE is set: no subprocess, no file read, no
#|# measurable delay, and `_lk_emit_run_core` then passes an EMPTY
#|# SEROTINE_LINE_FAILURE_JSON, so the emitted line is byte-for-byte the line
#|# this library emitted before the feature existed. NOTHING IS ARMED BY THIS
#|# CODE. Enabling a job is a separate, supervised act.
#|#
#|# The flag is deliberately its OWN name, not SEROTINE_EMIT_EVENTS and not
#|# SEROTINE_TRACE_CAPTURE: the three lanes ship different data to different
#|# surfaces and must be armable one at a time.
#|#
#|# NEVER FAILS OR DELAYS THE JOB IT INSTRUMENTS — the same posture as the rest
#|# of this file. The helper subprocess is hard-bounded by _lk_timeout, every
#|# path swallows its errors, and the wrapper's exit code is untouched.
#|#
#|# FAIL-CLOSED ON REDACTION, and this one matters more than the trace's does:
#|# the object is PERSISTED in serotine's `runs.raw` and RENDERED on the job page,
#|# and per serotine AGENTS.md the consequence is ONE-WAY — "adding a strip later
#|# would protect future lines only". Redaction is the EMITTER's job. The body is
#|# therefore built by telemetry/serotine_trace.py (`build-failure`), which is
#|# where launchd-kit's only redactor lives; if that helper or python3 is
#|# unavailable, or it returns nothing, NOTHING IS ATTACHED. Read that module's
#|# REDACTION header before enabling a job.
#|#
#|# Env-var contract (all optional; read at emit time):
#|#   SEROTINE_EMIT_FAILURE       THE ARMING FLAG. Unset (default) = fully inert.
#|#                               `fail` (also 1|true|yes|on) = attach only when
#|#                               the emitted run status is `fail`. `always` =
#|#                               attach on ok/skip too. Anything else = off.
#|#   SEROTINE_FAILURE_LOG        Log file to summarize. Default: $LOG (this run's).
#|#   SEROTINE_FAILURE_LINES      Last-N non-blank log lines for stderr_tail.
#|#                               Default: 20 — serotine's own display bound.
#|#                               Must be a POSITIVE integer; 0 and anything
#|#                               non-numeric take the default (0 is falsy in the
#|#                               helper and would disable the cut entirely).
#|#   SEROTINE_FAILURE_MESSAGE    Default `message` member when the caller passes
#|#                               no --failure-message (the EXIT-trap crash path
#|#                               has no argv to carry one).
#|#   SEROTINE_FAILURE_TIMEOUT    Hard bound in seconds on the helper. Default: 10.
#|#                               Must be a POSITIVE integer — `timeout 0` means
#|#                               "no timeout" to GNU timeout AND to the perl
#|#                               alarm() leg, so 0 takes the default rather than
#|#                               silently removing the bound.
#|#   SEROTINE_FAILURE_HELPER     Path to serotine_trace.py (tests override it).
#|#   SEROTINE_TRACE_REDACT_ENV / SEROTINE_TRACE_REDACT_PATTERN are reused as-is:
#|#                               a job that has tuned redaction for its own log
#|#                               should not restate it for the same log's summary.
#|# ==============================================================================
#|
#|# _lk_collect_failure: PRINT the serotine.run.failure object for this run, or
#|# nothing. Args: <status> <message>. Always returns 0-or-empty to the caller;
#|# never writes a file, never touches the network.
#|_lk_collect_failure() {
#|  # THE INERTNESS GATE. Nothing below this line runs for a job that has not
#|  # opted in — one parameter expansion, no subprocess, no syscall.
#|  [[ -n "${SEROTINE_EMIT_FAILURE:-}" ]] || return 0
#|
#|  local mode
#|  # Explicit alternatives rather than ${VAR,,}: this library still supports the
#|  # bash 3.2 that ships with macOS (see lk_require_cmd's note).
#|  case "${SEROTINE_EMIT_FAILURE}" in
#|    fail|FAIL|Fail|1|true|TRUE|True|yes|YES|Yes|on|ON|On) mode="fail" ;;
#|    always|ALWAYS|Always)                                 mode="always" ;;
#|    *) return 0 ;;
#|  esac
#|
#|  local status="${1:-}" message="${2:-}"
#|  [[ "$mode" == "always" || "$status" == "fail" ]] || return 0
#|  [[ -n "$message" ]] || message="${SEROTINE_FAILURE_MESSAGE:-}"
#|
#|  command -v python3 >/dev/null 2>&1 || return 0
#|
#|  local helper log lines timeout
#|  helper="${SEROTINE_FAILURE_HELPER:-$_LK_TELEMETRY_ROOT/telemetry/serotine_trace.py}"
#|  # FAIL CLOSED: no redactor on disk means no forensics, never unredacted ones.
#|  [[ -f "$helper" ]] || return 0
#|
#|  log="${SEROTINE_FAILURE_LOG:-${LOG:-}}"
#|  [[ -n "$log" && -f "$log" ]] || log=""
#|  # An explicit message is worth attaching even with no readable log; a run with
#|  # neither has nothing to say and skips the subprocess entirely.
#|  [[ -n "$message" || -n "$log" ]] || return 0
#|
#|  # STRICTLY POSITIVE, both of them. `^[0-9]+$` would accept 0, and a 0 here is
#|  # not "no limit by request" — it is the limit silently switching itself off:
#|  # `timeout 0` (GNU) and `alarm(0)` (the perl leg) BOTH mean "no timeout", so
#|  # SEROTINE_FAILURE_TIMEOUT=0 would defeat the hard bound this whole hook's
#|  # fail-safety rests on, and SEROTINE_FAILURE_LINES=0 is falsy in the helper
#|  # and disables the last-N cut. Verified, not assumed: `gtimeout
#|  # --kill-after=5s 0 sleep 4` runs the full 4s and exits 0. An out-of-range
#|  # value takes the documented default, exactly like a non-numeric one.
#|  lines="${SEROTINE_FAILURE_LINES:-20}"
#|  [[ "$lines" =~ ^[1-9][0-9]*$ ]] || lines=20
#|  timeout="${SEROTINE_FAILURE_TIMEOUT:-10}"
#|  [[ "$timeout" =~ ^[1-9][0-9]*$ ]] || timeout=10
#|
#|  # Only NON-SECRET identifiers ride the `env` prefix — argv is world-readable
#|  # through `ps`. The credentials the redactor needs are the ones this wrapper
#|  # already holds in its own environment, INHERITED here rather than re-passed,
#|  # which is how it can redact their literal values.
#|  #
#|  # CAPTURED, THEN GATED ON THE EXIT STATUS — never streamed straight out. A
#|  # bare `… || return 0` would discard only the STATUS: whatever the helper had
#|  # already written to the pipe is captured by the caller's command
#|  # substitution regardless, so a helper that prints a well-formed object and
#|  # THEN hangs (killed at the bound, exit 124/137) or exits non-zero would ship
#|  # its output anyway. That silently converts the fail-closed guarantee into
#|  # "fail-closed unless the helper wrote something first", and the guarantee
#|  # must not depend on a helper's internal write ordering — a redactor killed
#|  # mid-pass is exactly the case this hook exists to refuse.
#|  local out rc
#|  out="$("$_LK_TELEMETRY_TIMEOUT_FN" "$timeout" env \
#|    SEROTINE_FAILURE_LOG="$log" \
#|    SEROTINE_FAILURE_MESSAGE="$message" \
#|    SEROTINE_FAILURE_LINES="$lines" \
#|    python3 "$helper" build-failure 2>/dev/null)"
#|  rc=$?
#|  (( rc == 0 )) || return 0
#|  printf '%s' "$out"
#|  return 0
#|}
#|
#|# ==============================================================================
#|# Wrapper-side FAILURE TRACE capture (opt-in per job; OFF fleet-wide)
#|# ------------------------------------------------------------------------------
#|# Wrapper-side failure trace capture. Serotine's job pages show `Exit 74` next to an empty Trace
#|# column on every row because nothing on the wrapper side has ever attached a
#|# trace; the SERVER half (POST /api/v1/ingest/trace, GET /api/v1/traces/…,
#|# retention) has shipped for months. This section closes that: on a failing run
#|# the wrapper uploads the tail of its OWN run log as the run's trace, so a
#|# failure carries its stderr into the dashboard instead of requiring an SSH.
#|#
#|# DEFAULT OFF, FLEET-WIDE. `_lk_attach_trace` returns on its first line unless
#|# SEROTINE_TRACE_CAPTURE is set: no subprocess, no file read, no network, no
#|# log line, no measurable delay. Enabling is a separate, per-job supervised act.
#|#
#|# NEVER FAILS OR DELAYS THE JOB IT INSTRUMENTS — the same posture as lk_notify
#|# and the run.v1 emit hooks, and the reason telemetry is allowed near live
#|# production jobs at all. Every path swallows its errors, both subprocesses are
#|# hard-bounded by _lk_timeout (so a curl that ignores `-m`, or a hung DNS
#|# lookup, still cannot park the wrapper), there is no retry within a run, and
#|# the wrapper's exit code is untouched no matter what happens here. A first-tick
#|# failure at launchd boot is expected and is not treated as fatal (the LAN-fetch
#|# transient during certificate setup).
#|#
#|# BEST-EFFORT, NEVER SPOOLED. One attempt per run, no retry, nothing queued —
#|# a run whose telemetry push failed (host offline) also loses its trace. That is
#|# the deliberate trade: a spooled trace would leave run logs on disk waiting to
#|# be uploaded later. `always` is a debugging mode for one job, not a fleet
#|# setting: a poller that skips dozens of ticks a week would upload every one.
#|#
#|# FAIL-CLOSED ON REDACTION. The body is built (and redacted) by
#|# telemetry/serotine_trace.py. If that helper or python3 is unavailable, or it
#|# returns nothing, NOTHING IS UPLOADED — the run log never goes out unredacted.
#|# Read that module's header before enabling a job: it documents what the
#|# redactor does and does not cover.
#|#
#|# Env-var contract (all optional; read at emit time):
#|#   SEROTINE_TRACE_CAPTURE      THE ARMING FLAG. Unset (default) = fully inert.
#|#                               `fail` (also 1|true|yes|on) = upload only when
#|#                               the emitted run status is `fail`. `always` =
#|#                               upload on ok/skip too. Anything else = off.
#|#   SEROTINE_TRACE_INGEST_URL   Upload endpoint. Default: SEROTINE_INGEST_URL
#|#                               with `/trace` appended (serotine's own layout,
#|#                               /api/v1/ingest → /api/v1/ingest/trace). Upload
#|#                               happens only if the URL AND SEROTINE_INGEST_TOKEN
#|#                               resolve — the trace endpoint takes the SAME
#|#                               Bearer token as ingest.
#|#   SEROTINE_TRACE_LOG          Log file to capture. Default: $LOG (this run's).
#|#   SEROTINE_TRACE_LINES        Last-N log lines to send. Default: 200.
#|#   SEROTINE_TRACE_UPLOAD_MAX_BYTES  Body cap in bytes, applied AFTER redaction,
#|#                               trimming oldest-first. Default: 262144. (Named
#|#                               apart from serotine's server-side
#|#                               SEROTINE_TRACE_MAX_BYTES on purpose.)
#|#   SEROTINE_TRACE_TIMEOUT      curl --max-time seconds. Default: 10.
#|#   SEROTINE_TRACE_REDACT_ENV   Extra env var NAMES whose values to redact.
#|#   SEROTINE_TRACE_REDACT_PATTERN  Extra job-specific redaction regex. An
#|#                               unusable regex fails closed (nothing uploaded).
#|#   SEROTINE_TRACE_HELPER       Path to serotine_trace.py (tests override it).
#|# ==============================================================================
#|
#|# _lk_attach_trace: upload this run's log tail as its serotine trace.
#|# Args: <run id> <job label> <host> <status> <exit code>. Always returns 0.
#|_lk_attach_trace() {
#|  # THE INERTNESS GATE. Nothing below this line runs for a job that has not
#|  # opted in — one parameter expansion, no subprocess, no syscall.
#|  [[ -n "${SEROTINE_TRACE_CAPTURE:-}" ]] || return 0
#|
#|  local mode
#|  # Explicit alternatives rather than ${VAR,,}: this library still supports the
#|  # bash 3.2 that ships with macOS (see lk_require_cmd's note).
#|  case "${SEROTINE_TRACE_CAPTURE}" in
#|    fail|FAIL|Fail|1|true|TRUE|True|yes|YES|Yes|on|ON|On) mode="fail" ;;
#|    always|ALWAYS|Always)                                 mode="always" ;;
#|    *) return 0 ;;
#|  esac
#|
#|  local runid="${1:-}" label="${2:-}" host="${3:-}" status="${4:-}" exit_code="${5:-0}"
#|  [[ -n "$runid" ]] || return 0
#|  [[ "$mode" == "always" || "$status" == "fail" ]] || return 0
#|
#|  local url token log helper timeout body code
#|  token="${SEROTINE_INGEST_TOKEN:-}"
#|  url="${SEROTINE_TRACE_INGEST_URL:-}"
#|  if [[ -z "$url" && -n "${SEROTINE_INGEST_URL:-}" ]]; then
#|    url="${SEROTINE_INGEST_URL%/}/trace"
#|  fi
#|  [[ -n "$url" && -n "$token" ]] || return 0
#|
#|  command -v curl >/dev/null 2>&1 || return 0
#|  command -v python3 >/dev/null 2>&1 || return 0
#|
#|  log="${SEROTINE_TRACE_LOG:-${LOG:-}}"
#|  [[ -n "$log" && -f "$log" ]] || return 0
#|
#|  helper="${SEROTINE_TRACE_HELPER:-$_LK_TELEMETRY_ROOT/telemetry/serotine_trace.py}"
#|  # FAIL CLOSED: no redactor on disk means no upload, never an unredacted one.
#|  [[ -f "$helper" ]] || return 0
#|
#|  # STRICTLY POSITIVE — see the identical note in _lk_collect_failure. `timeout
#|  # 0` means "no timeout" to GNU timeout and to the perl alarm() leg alike, so
#|  # accepting 0 here would let one env var silently remove the hard bound this
#|  # section's whole fail-safe claim rests on. Tightened alongside the forensics
#|  # failure-forensics copy because it is literally the same latent defect in the same
#|  # file; no job sets this var, and 0 never had a legitimate meaning.
#|  timeout="${SEROTINE_TRACE_TIMEOUT:-10}"
#|  [[ "$timeout" =~ ^[1-9][0-9]*$ ]] || timeout=10
#|
#|  # Build the body in python3 (JSON quoting correct by construction, redaction
#|  # applied there). Only NON-SECRET identifiers ride the `env` prefix — argv is
#|  # world-readable through `ps`. The credentials the redactor needs are the ones
#|  # this wrapper already holds in its own environment, INHERITED here rather
#|  # than re-passed, which is how it can redact their literal values.
#|  body="$("$_LK_TELEMETRY_TIMEOUT_FN" "$((timeout + 5))" env \
#|    SEROTINE_TRACE_LOG="$log" \
#|    SEROTINE_LINE_RUN_ID="$runid" \
#|    SEROTINE_LINE_JOB_LABEL="$label" \
#|    SEROTINE_LINE_HOST="$host" \
#|    SEROTINE_LINE_STATUS="$status" \
#|    SEROTINE_LINE_EXIT_CODE="$exit_code" \
#|    python3 "$helper" build-body 2>/dev/null)" || return 0
#|  [[ -n "$body" ]] || return 0
#|
#|  # The run id's single '/' is legal unencoded in a query value (RFC 3986) and
#|  # is exactly the form serotine's README curl recipe uses.
#|  code="$(printf '%s' "$body" | "$_LK_TELEMETRY_TIMEOUT_FN" "$((timeout + 5))" curl -sS \
#|      -m "$timeout" --connect-timeout 5 \
#|      -o /dev/null -w '%{http_code}' -X POST \
#|      -H "Authorization: Bearer $token" \
#|      -H 'Content-Type: application/x-ndjson' \
#|      --data-binary @- "$url?run_id=$runid" 2>/dev/null)" || code="000"
#|
#|  # One line in the run log so an operator enabling a cohort can see what
#|  # happened without turning on anything else. Only ever reached when armed.
#|  if [[ -n "${LOG:-}" ]]; then
#|    printf '[launchd-kit] trace: %s bytes -> %s (http %s)\n' \
#|      "${#body}" "$url" "${code:-000}" >>"$LOG" 2>/dev/null || true
#|  fi
#|  return 0
#|}
# END GENERATED STANDALONE MODULE: lib/telemetry.sh
