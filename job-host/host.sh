#!/usr/bin/env bash
# job-host/host.sh — the job-host contract for scheduled (launchd) jobs.
#
# "job-host" is a WORKING NAME. The directory and the jh_ prefix will be
# renamed once the boundary is proven (see README.md).
#
# This library carries the five concerns every unattended job needs and
# nothing else:
#
#   1. run guard   jh_init (pins env, opens the run log), jh_slot_guard (the
#                  window guard that sits behind a coarse StartInterval tick),
#                  jh_require_cmd
#   2. lock        jh_single_lock (atomic mkdir lock, owner token, claimed
#                  stale-lock steal, released on exit only by its owner)
#   3. secrets     jh_with_secrets (resolves an env-file's op:// references
#                  through launchd-kit/op-resolve.py, imported by jh_secrets.py,
#                  then execs the command; fail-closed, never a partial env)
#   4. timeout     jh_timeout (portable hard bound), jh_run_bounded (bound a
#                  command and record a timeout)
#   5. telemetry   jh_spool_append (append a caller-supplied one-line JSON
#     transport    payload to a spool file). The host never builds a run
#                  record's schema; it moves bytes.
#
# It knows nothing about the delegation loop, Notion, Discord or any telemetry
# payload schema. Notification, run telemetry and the fallback chain are
# sourceable launchd-kit integrations that this compatibility entry point loads
# after defining the shared runtime aliases (see BOUNDARY.md).
#
# Like launchd-kit/lib.sh, sourcing this file only DEFINES functions: nothing
# runs until the caller calls jh_init and the guards.
#
# ------------------------------------------------------------------------------
# Compatibility with launchd-kit/lib.sh
# ------------------------------------------------------------------------------
# For the functions the two libraries share, behaviour matches lib.sh exactly
# (tests/test_parity.py runs one fixture job against both). That includes the
# state a job leaves on disk — the run log name, the slot marker
# ($LOG_DIR/.last-run-window) and the lock directory ($LOG_DIR/.run.lock) — so
# a job switched from lib.sh to this file keeps its window and lock state.
#
# Two deliberate differences, both on inputs lib.sh mishandles: jh_slot_guard
# refuses (exit 2) a schedule BSD `date` cannot evaluate, where lib.sh claims
# the window and runs; and jh_single_lock steals a stale lock and releases its
# own only through a per-directory claim and an owner token, so two stealers
# cannot both run and a holder whose lock was stolen leaves the new one alone.
#
# The env-var contract is lib.sh's, read under the same names:
#   TOOL_NAME, LOG_DIR, TZ, LAUNCHD_KIT_PATH, LOG_RETENTION_DAYS, LOG_MAX_FILES,
#   LK_FORCE / <TOOL>_FORCE, LK_SLOT_GRACE, LK_SLOT_CLAIM, LK_TIMEOUT_KILL_AFTER,
#   NOTIFY_SCRIPT, LK_NOTIFY_TIMEOUT_SECS
# and exported/readable under the same names: LOG, LK_STAMP, LK_EXIT_CODE,
# LK_LOCK_DIR.
#
# Unless JH_NO_LK_ALIASES=1 is set before sourcing, the lib.sh names of the
# shared functions are defined as thin wrappers (launchd_kit_init, lk_log,
# lk_on_exit, lk_slot_guard, lk_single_lock, lk_require_cmd, lk_rotate_logs,
# _lk_timeout, _lk_have_cmd, _lk_parse_duration). The notify, chain and run
# telemetry integrations are then sourced against those aliases, so their
# lib.sh names are available without copying their implementations here.
#
# ------------------------------------------------------------------------------
# Env vars this file adds (all optional)
# ------------------------------------------------------------------------------
#   JH_WARN_CMDS            space-separated commands jh_init warns about when
#                           they do not resolve to an executable on the pinned
#                           PATH. Default: claude (lib.sh's behaviour).
#   JH_SECRETS_TIMEOUT_SECS bound on op:// resolution in jh_with_secrets.
#                           Default: 120. The command itself is NOT bounded by
#                           it.
#   JH_ON_TIMEOUT           a function jh_run_bounded calls on a timeout, as
#                           `<fn> <limit_secs> <exit_code> <elapsed_secs>`; its
#                           stdout is the payload recorded. Default: a small
#                           job-host event record (see jh_run_bounded).
#   JH_TIMEOUT_SPOOL        spool file jh_run_bounded appends the timeout
#                           payload to. See jh_run_bounded for the default
#                           spool path —
#                           deliberately NOT the run-record spool, so a
#                           job-host event never reaches a run-record ingest.
#   JH_NO_LK_ALIASES        set to 1 to skip the lib.sh-name wrappers.
# ------------------------------------------------------------------------------

_JH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- exit-handler registry ----------------------------------------------------
# One EXIT trap dispatches every registered handler in LIFO order, with the
# script's real exit code captured first (LK_EXIT_CODE, and JH_EXIT_CODE).
# Arrays are only counted or indexed, never blanket-expanded: bash 3.2 under
# `set -u` treats "${arr[@]}" on an empty array as unbound.
LK_EXIT_CODE=0
JH_EXIT_CODE=0
_JH_EXIT_HANDLERS=()

_jh_run_exit_handlers() {
  LK_EXIT_CODE=$?
  # shellcheck disable=SC2034 # read by the caller's exit handlers
  JH_EXIT_CODE=$LK_EXIT_CODE
  local i
  for (( i=${#_JH_EXIT_HANDLERS[@]}-1 ; i>=0 ; i-- )); do
    "${_JH_EXIT_HANDLERS[$i]}" || true
  done
}

# jh_on_exit <function-name>: run it at exit. Installs the dispatcher once.
jh_on_exit() {
  if (( ${#_JH_EXIT_HANDLERS[@]} == 0 )); then
    trap _jh_run_exit_handlers EXIT
  fi
  _JH_EXIT_HANDLERS+=("$1")
}

# --- jh_timeout: portable hard-timeout guard ----------------------------------
# Runs "$@" bounded to <secs> seconds. First available tier wins:
#   1. timeout   2. gtimeout   (both with --kill-after, signalling the group)
#   3. perl fork + alarm + setpgid, TERM then KILL to the child's group
#   4. bash job control: background in its own group, poll, TERM then KILL
# Every tier follows the TERM with an unignorable KILL after a short grace
# (LK_TIMEOUT_KILL_AFTER, default 5s, for tiers 1 and 2), so a child that
# traps TERM, or a forked helper holding the stdout pipe, cannot outlive the
# bound. Returns 124 on a timeout (137 when the KILL is what ended it);
# otherwise the command's own exit status. Same implementation as lib.sh's
# _lk_timeout.
jh_timeout() {
  local secs="$1"; shift
  if [[ $# -eq 0 ]]; then
    echo "_lk_timeout: no command given" >&2
    return 2
  fi

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

  local _jh_had_monitor=1
  case $- in *m*) _jh_had_monitor=1 ;; *) _jh_had_monitor=0 ;; esac
  set -m
  # `<&0` keeps the caller's stdin: without an explicit redirection bash 3.2
  # gives an async list /dev/null even with job control on.
  "$@" <&0 &
  local cmd_pid=$! waited=0
  (( _jh_had_monitor )) || set +m
  while kill -0 "$cmd_pid" 2>/dev/null; do
    if (( waited >= secs )); then
      kill -TERM -- "-$cmd_pid" 2>/dev/null || kill -TERM "$cmd_pid" 2>/dev/null
      sleep 1
      kill -KILL -- "-$cmd_pid" 2>/dev/null || kill -KILL "$cmd_pid" 2>/dev/null
      wait "$cmd_pid" 2>/dev/null
      return 124
    fi
    sleep 1
    # Plain assignment, not (( waited++ )): the post-increment returns 1 when
    # waited is 0, which `set -e` would treat as a failure.
    waited=$((waited + 1))
  done
  wait "$cmd_pid"
}

# --- _jh_have_cmd: does NAME resolve to something actually runnable? ---------
# A present-but-non-executable file passes `command -v`; test the resolved path
# with -x. Builtins, functions and keywords resolve to a bare name and pass.
_jh_have_cmd() {
  local resolved
  resolved="$(command -v "$1" 2>/dev/null)" || return 1
  [[ -n "$resolved" ]] || return 1
  [[ "$resolved" == */* ]] || return 0
  [[ -x "$resolved" ]]
}

# --- jh_init: pin env + open the run log (run guard, part 1) ------------------
jh_init() {
  : "${TOOL_NAME:?launchd_kit_init: TOOL_NAME must be set before init}"

  # launchd hands a job a near-empty environment. HOME first, because the
  # PATH default interpolates it.
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

  # Name the kit this run came from and its version. host.sh sources the
  # integrations from ../launchd-kit, so it reads that same kit's VERSION file
  # (one version line for one checkout; a second file could only drift). A
  # missing, empty or unreadable VERSION logs `unknown` and the run goes on.
  local _jh_kit_dir _jh_kit_ver=""
  _jh_kit_dir="$(cd "$_JH_DIR/../launchd-kit" 2>/dev/null && pwd)" \
    || _jh_kit_dir="$_JH_DIR/../launchd-kit"
  { IFS= read -r _jh_kit_ver <"$_jh_kit_dir/VERSION"; } 2>/dev/null || true
  echo "kit: $_jh_kit_dir ${_jh_kit_ver:-unknown}" >>"$LOG"

  # Warn (never fail) at the top of the log when an expected binary does not
  # resolve, so a PATH regression is attributable instead of a silent empty
  # run. Jobs that cannot proceed without a binary call jh_require_cmd.
  local c
  for c in ${JH_WARN_CMDS-claude}; do
    if ! _jh_have_cmd "$c"; then
      echo "[launchd-kit] WARNING: \`$c\` does not resolve to an executable on this PATH." >>"$LOG"
      echo "[launchd-kit]          PATH=$PATH" >>"$LOG"
    fi
  done
}

# --- jh_log: timestamped line to the run log (and stderr) ---------------------
jh_log() {
  local line
  line="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
  if [[ -n "${LOG:-}" ]]; then
    echo "$line" >>"$LOG"
  fi
  echo "$line" >&2
}

# _jh_parse_duration: "<N>[s|m|h|d]" -> seconds on stdout; returns 1 (and
# prints nothing) on anything else. At most 10 digits, forced base 10, never
# negative.
_jh_parse_duration() {
  local raw="${1:-}" n unit secs
  [[ "$raw" =~ ^([0-9]+)([smhd]?)$ ]] || return 1
  (( ${#BASH_REMATCH[1]} <= 10 )) || return 1
  n=$(( 10#${BASH_REMATCH[1]} )) || return 1
  unit="${BASH_REMATCH[2]:-s}"
  case "$unit" in
    s) secs=$(( n ))         ;;
    m) secs=$(( n * 60 ))    ;;
    h) secs=$(( n * 3600 ))  ;;
    d) secs=$(( n * 86400 )) ;;
  esac
  (( secs >= 0 )) || return 1
  printf '%s' "$secs"
}

# --- jh_slot_guard: the window guard (run guard, part 2) ----------------------
# launchd fires the job on a coarse StartInterval; this admits only the FIRST
# tick of each window, so a missed wake heals on the next tick.
#
#   jh_slot_guard daily [HH:MM]          one run per day on/after HH:MM
#   jh_slot_guard weekly:<dow> [HH:MM]   one run per week on/after <dow> HH:MM
#   jh_slot_guard quarterly [HH:MM]      one run per calendar quarter
#   jh_slot_guard slots:8,12,16,20       one run per listed hour-slot per day
#
# Exits 0 (skip) when the window already ran, has not opened yet, or (with
# LK_SLOT_GRACE) has closed. LK_FORCE=1 or <TOOL>_FORCE=1 bypasses it. An
# unknown interval, an unparseable LK_SLOT_GRACE or a zero LK_SLOT_GRACE exits
# 2: a typo must never leave the job unguarded. So does a schedule `date`
# cannot evaluate: a time of day that is not H:MM within 00:00-23:59 (daily,
# weekly and quarterly only; slots: ignores it), a weekday that is not a day
# name (three letters or more, any case), a slot list that is not
# comma-separated hours 0-23, or any window start that does not compute to an
# epoch. None of those claims the window.
#
# LK_SLOT_CLAIM=up-front (default) writes the marker now; on-success defers it
# to an exit handler that writes it only on exit 0 (take jh_single_lock BEFORE
# the guard in that mode). An unknown value falls back to up-front, with a
# warning.
#
# Uses BSD `date -j -f`/`date -v` (macOS), as lib.sh does.
jh_slot_guard() {
  local interval="${1:?lk_slot_guard: interval required}"
  local at="${2:-00:00}"

  local force_var force_val
  force_var="$(printf '%s' "${TOOL_NAME:-}" | tr '[:lower:]-' '[:upper:]_')_FORCE"
  force_val="${!force_var:-0}"
  if [[ "${LK_FORCE:-0}" == "1" || "$force_val" == "1" ]]; then
    return 0
  fi

  local marker="$LOG_DIR/.last-run-window"
  local key now open_epoch
  now="$(date +%s)"
  open_epoch=""

  case "$interval" in
    daily)
      _jh_require_time_of_day "$at"
      local day_date day_epoch
      day_date="$(date +%Y-%m-%d)"
      day_epoch="$(date -j -f '%Y-%m-%d %H:%M' "$day_date $at" +%s)"
      _jh_require_epoch "$day_epoch" "$day_date $at"
      (( now < day_epoch )) && exit 0
      key="daily:$day_date"
      open_epoch="$day_epoch"
      ;;
    weekly:*)
      _jh_require_time_of_day "$at"
      local dow win_date win_epoch
      dow="${interval#weekly:}"
      _jh_valid_weekday "$dow" \
        || _jh_slot_refuse "unknown weekday '$dow' in '$interval'"
      win_date="$(date -v-"$dow" +%Y-%m-%d)"
      win_epoch="$(date -j -f '%Y-%m-%d %H:%M' "$win_date $at" +%s)"
      _jh_require_epoch "$win_epoch" "$win_date $at"
      (( now < win_epoch )) && exit 0
      key="weekly:$win_date"
      open_epoch="$win_epoch"
      ;;
    quarterly)
      _jh_require_time_of_day "$at"
      local q_month q_date q_epoch
      q_month=$(( ( (10#$(date +%m) - 1) / 3 ) * 3 + 1 ))
      q_date="$(printf '%s-%02d-01' "$(date +%Y)" "$q_month")"
      q_epoch="$(date -j -f '%Y-%m-%d %H:%M' "$q_date $at" +%s)"
      _jh_require_epoch "$q_epoch" "$q_date $at"
      (( now < q_epoch )) && exit 0
      key="quarterly:$q_date"
      open_epoch="$q_epoch"
      ;;
    slots:*)
      local list h slot s
      list="${interval#slots:}"
      _jh_valid_slot_list "$list" \
        || _jh_slot_refuse "unparseable slot list '$list' (want comma-separated hours 0-23)"
      h=$((10#$(date +%H)))
      slot=""
      # Highest listed slot <= the current hour; one number per line so
      # `sort -rn` really orders them. Every slot is read in base 10: bash
      # reads a leading zero as octal, which makes `08` and `09` an error.
      # The marker keeps the slot as written, as lib.sh's does.
      for s in $(tr ',' '\n' <<<"$list" | sort -rn); do
        (( h >= 10#$s )) && { slot=$s; break; }
      done
      [[ -z "$slot" ]] && exit 0
      key="slot:$(date +%Y-%m-%d):$slot"
      open_epoch="$(date -j -f '%Y-%m-%d %H:%M' "$(date +%Y-%m-%d) $(printf '%02d' "$((10#$slot))"):00" +%s)"
      _jh_require_epoch "$open_epoch" "slot $slot"
      ;;
    *)
      echo "lk_slot_guard: unknown interval '$interval' — refusing to run unguarded" >&2
      exit 2
      ;;
  esac

  if [[ -n "${LK_SLOT_GRACE:-}" ]]; then
    local grace_s
    if ! grace_s="$(_jh_parse_duration "${LK_SLOT_GRACE}")"; then
      echo "lk_slot_guard: unparseable LK_SLOT_GRACE '${LK_SLOT_GRACE}' — refusing to run unguarded" >&2
      exit 2
    fi
    if (( grace_s == 0 )); then
      echo "lk_slot_guard: LK_SLOT_GRACE '${LK_SLOT_GRACE}' is zero — that closes every window forever; refusing to run unguarded" >&2
      exit 2
    fi
    if [[ -n "$open_epoch" ]] && (( now >= open_epoch + grace_s )); then
      exit 0
    fi
  fi

  if [[ -f "$marker" && "$(cat "$marker")" == "$key" ]]; then
    exit 0
  fi

  case "${LK_SLOT_CLAIM:-up-front}" in
    on-success)
      # Script scope on purpose: the exit handler reads these after return.
      LK_SLOT_MARKER="$marker"
      LK_SLOT_KEY="$key"
      jh_on_exit _jh_slot_claim_on_success
      ;;
    up-front)
      echo "$key" >"$marker"
      ;;
    *)
      echo "lk_slot_guard: unknown LK_SLOT_CLAIM '${LK_SLOT_CLAIM}' — using up-front" >&2
      echo "$key" >"$marker"
      ;;
  esac
}

# _jh_slot_refuse <reason>: the guard cannot evaluate the schedule. Exit 2
# without claiming the window.
_jh_slot_refuse() {
  echo "lk_slot_guard: $1 — refusing to run unguarded" >&2
  exit 2
}

# _jh_require_time_of_day <H:MM>: refuse unless it is a time of day. Only the
# intervals that read it (daily, weekly, quarterly) check it; slots: ignores
# it, as lib.sh does.
_jh_require_time_of_day() {
  _jh_valid_time_of_day "$1" \
    || _jh_slot_refuse "unparseable time of day '$1' (want H:MM, 00:00-23:59)"
}

# _jh_valid_time_of_day <H:MM>: one or two digits each side (what BSD
# `date -j -f %H:%M` reads), hour 0-23, minute 0-59.
_jh_valid_time_of_day() {
  [[ "$1" =~ ^([0-9]{1,2}):([0-9]{1,2})$ ]] || return 1
  (( 10#${BASH_REMATCH[1]} <= 23 && 10#${BASH_REMATCH[2]} <= 59 ))
}

# _jh_valid_weekday <name>: three or more letters of an English day name, any
# case (the spellings BSD `date -v` accepts).
_jh_valid_weekday() {
  local d full
  d="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  [[ "$d" =~ ^[a-z]{3,}$ ]] || return 1
  for full in sunday monday tuesday wednesday thursday friday saturday; do
    [[ "$full" == "$d"* ]] && return 0
  done
  return 1
}

# _jh_valid_slot_list <list>: one or more comma-separated hours, 0-23.
_jh_valid_slot_list() {
  local list="$1" s
  [[ "$list" =~ ^[0-9]{1,2}(,[0-9]{1,2})*$ ]] || return 1
  for s in $(tr ',' ' ' <<<"$list"); do
    (( 10#$s <= 23 )) || return 1
  done
}

# _jh_require_epoch <value> <what>: refuse unless `date` produced an epoch.
_jh_require_epoch() {
  [[ "$1" =~ ^[0-9]+$ ]] \
    || _jh_slot_refuse "could not compute the window start for '$2'"
}

# Exit handler for LK_SLOT_CLAIM=on-success: claim the window only on exit 0.
_jh_slot_claim_on_success() {
  (( LK_EXIT_CODE == 0 )) || return 0
  echo "$LK_SLOT_KEY" >"$LK_SLOT_MARKER" 2>/dev/null || true
}

# --- jh_single_lock: atomic single-run lock -----------------------------------
# mkdir is atomic (macOS has no flock). The holder writes a unique token to
# $LK_LOCK_DIR/owner. A lock whose directory is older than max_age_min
# (default 120) is assumed dead and stolen. A tick that loses the race exits 0
# and does NOT register a release, so it can never remove the holder's lock.
#
# Removing a lock directory, whether to steal it or to release it, first takes
# that directory's claim: `mkdir $LK_LOCK_DIR/.claim`, which only one process
# can win per lock directory. A contender records the lock's identity (inode
# and owner token) BEFORE it judges the lock stale; after winning the claim it
# re-checks that the directory still has that identity, and withdraws if not.
# So a contender only ever removes the very directory it judged stale: two
# contenders cannot both steal one stale lock, a contender that was delayed
# after its staleness check leaves the replacement lock alone, and a holder
# whose lock was stolen leaves the new holder's lock alone at exit (its token
# no longer matches). A claim older than max_age_min (its claimant was killed
# mid-removal) is cleared before the next steal.
jh_single_lock() {
  local max_age_min="${1:-120}"
  LK_LOCK_DIR="$LOG_DIR/.run.lock"
  # shellcheck disable=SC2034 # read by the caller
  JH_LOCK_DIR="$LK_LOCK_DIR"
  _JH_LOCK_TOKEN="$$.${RANDOM}${RANDOM}.$(date +%s)"

  if ! mkdir "$LK_LOCK_DIR" 2>/dev/null; then
    # Identity first, staleness second: the steal must act on the directory
    # that was judged stale, never on one that replaced it in between.
    local ino owner
    ino="$(_jh_lock_inode "$LK_LOCK_DIR")"
    owner="$(_jh_lock_owner "$LK_LOCK_DIR")"
    if [[ -z "$ino" ]] \
      || ! _jh_lock_is_stale "$LK_LOCK_DIR" "$max_age_min" \
      || ! _jh_steal_lock "$max_age_min" "$ino" "$owner" \
      || ! mkdir "$LK_LOCK_DIR" 2>/dev/null; then
      echo "$TOOL_NAME: another run is active ($LK_LOCK_DIR) — skipping this tick" >&2
      exit 0
    fi
  fi
  printf '%s\n' "$_JH_LOCK_TOKEN" >"$LK_LOCK_DIR/owner" 2>/dev/null || true
  date >"$LK_LOCK_DIR/started-at" 2>/dev/null || true
  jh_on_exit _jh_release_lock
}

# _jh_lock_is_stale <path> <max_age_min>: is <path> older than the limit?
_jh_lock_is_stale() {
  [[ -d "$1" && -n "$(find "$1" -maxdepth 0 -mmin +"$2" 2>/dev/null)" ]]
}

# _jh_lock_inode <dir>: the directory's inode number, or nothing.
_jh_lock_inode() {
  local ino rest
  read -r ino rest < <(ls -di "$1" 2>/dev/null) || return 0
  printf '%s' "$ino"
}

# _jh_lock_owner <dir>: the owner token in <dir>, or nothing (a lock written
# by launchd-kit/lib.sh has none).
_jh_lock_owner() {
  local line=""
  [[ -f "$1/owner" ]] && IFS= read -r line <"$1/owner" 2>/dev/null
  printf '%s' "$line"
}

# _jh_remove_claimed_lock: move the claimed lock directory aside and delete
# it. Only called by the process holding that directory's claim.
_jh_remove_claimed_lock() {
  local gone="$LK_LOCK_DIR.gone.$_JH_LOCK_TOKEN"
  mv "$LK_LOCK_DIR" "$gone" 2>/dev/null || return 1
  rm -rf "$gone"
}

# _jh_steal_lock <max_age_min> <inode> <owner>: remove the stale lock this
# process inspected, identified by the inode and owner token recorded before
# it was judged stale, or return 1 (someone else is removing it, or it is no
# longer that lock). The age is not re-checked here: taking the claim
# (`mkdir .claim`) updates the directory's mtime.
_jh_steal_lock() {
  local max_age_min="$1" ino="$2" owner="$3" claim="$LK_LOCK_DIR/.claim"
  [[ -n "$ino" ]] || return 1

  if ! mkdir "$claim" 2>/dev/null; then
    # A claim outlives its claimant only when that process was killed
    # between claiming and removing; one older than the limit is cleared.
    _jh_lock_is_stale "$claim" "$max_age_min" || return 1
    rmdir "$claim" 2>/dev/null || return 1
    mkdir "$claim" 2>/dev/null || return 1
  fi
  if [[ "$(_jh_lock_inode "$LK_LOCK_DIR")" != "$ino" \
     || "$(_jh_lock_owner "$LK_LOCK_DIR")" != "$owner" ]]; then
    # The lock was replaced after it was judged stale: this claim is on a
    # live lock. Withdraw it and leave that lock alone.
    rmdir "$claim" 2>/dev/null || true
    return 1
  fi
  _jh_remove_claimed_lock
}

# Release only the lock this process holds: its token must still be the
# owner, and the directory's claim must be free (a stealer holding it will
# remove the directory itself).
_jh_release_lock() {
  [[ -n "${LK_LOCK_DIR:-}" && -n "${_JH_LOCK_TOKEN:-}" ]] || return 0
  [[ "$(_jh_lock_owner "$LK_LOCK_DIR")" == "$_JH_LOCK_TOKEN" ]] || return 0
  mkdir "$LK_LOCK_DIR/.claim" 2>/dev/null || return 0
  if [[ "$(_jh_lock_owner "$LK_LOCK_DIR")" != "$_JH_LOCK_TOKEN" ]]; then
    rmdir "$LK_LOCK_DIR/.claim" 2>/dev/null || true
    return 0
  fi
  _jh_remove_claimed_lock
}

# --- jh_require_cmd: hard-fail loudly when a needed binary is missing ---------
# Silent on success. On failure: logs the missing name(s) and the PATH, posts
# a red alert exactly as lib.sh's lk_require_cmd does (see _jh_notify_fatal),
# and exits 127.
jh_require_cmd() {
  local missing="" c
  for c in "$@"; do
    _jh_have_cmd "$c" || missing="${missing:+$missing }$c"
  done
  [[ -z "$missing" ]] && return 0

  jh_log "FATAL: required command(s) missing or not executable on PATH: $missing"
  jh_log "FATAL: PATH=$PATH"
  printf '%s\n%s\n%s\n' \
    "Missing or not executable on the pinned PATH: \`$missing\`" \
    "PATH=$PATH" \
    "The job exited without doing any work — this is NOT a healthy run." \
    | lk_notify "$TOOL_NAME — required command missing" "$LK_COLOR_RED"
  exit 127
}

# --- jh_rotate_logs: prune old run logs ---------------------------------------
# Deletes *.log older than LOG_RETENTION_DAYS (default 60), then, if
# LOG_MAX_FILES is a positive integer, the oldest survivors beyond that count.
jh_rotate_logs() {
  local days="${LOG_RETENTION_DAYS:-60}"
  find "$LOG_DIR" -name '*.log' -mtime +"$days" -delete 2>/dev/null || true

  local max_files="${LOG_MAX_FILES:-0}"
  [[ "$max_files" =~ ^[0-9]+$ ]] || return 0
  (( max_files > 0 )) || return 0

  local count
  count="$(find "$LOG_DIR" -name '*.log' -type f 2>/dev/null | wc -l | tr -d ' ')"
  [[ "$count" =~ ^[0-9]+$ ]] || return 0
  (( count > max_files )) || return 0

  find "$LOG_DIR" -name '*.log' -type f -exec stat -f '%m %N' {} \; 2>/dev/null \
    | sort -rn \
    | tail -n "+$((max_files + 1))" \
    | cut -d' ' -f2- \
    | while IFS= read -r f; do rm -f -- "$f"; done
}

# --- jh_with_secrets: resolve an env-file, then exec the command --------------
# Usage: jh_with_secrets <env-file> <cmd> [args...]
#
# Runs jh_secrets.py, which imports launchd-kit/op-resolve.py from beside this
# directory (no variable overrides that path) and resolves every op:// value
# in <env-file> against 1Password Connect (OP_CONNECT_HOST, OP_CONNECT_TOKEN). Only when EVERY reference resolves is
# <cmd> exec'd, with the resolved values in its environment and the Connect
# credentials removed. Any failure exits non-zero before <cmd> starts, so the
# command never sees a partial environment; and because resolution happens in
# a child process, the calling shell's environment never holds a secret at all.
#
# Resolution is bounded by JH_SECRETS_TIMEOUT_SECS (default 120): it runs in
# its own process group, and past the bound that whole group is killed (a hung
# Keychain lookup included), jh_secrets.py exits 124 and <cmd> never starts.
# <cmd>'s own runtime is not bounded by it — wrap the call in jh_run_bounded
# for that.
#
# Returns the command's exit status, or the resolver's non-zero status on a
# resolution failure (1 for a resolve error, 124 for the bound, 2 for usage).
jh_with_secrets() {
  local env_file="${1:-}"
  if [[ -z "$env_file" || $# -lt 2 ]]; then
    echo "jh_with_secrets: usage: jh_with_secrets <env-file> <cmd> [args...]" >&2
    return 2
  fi
  shift
  if ! command -v python3 >/dev/null 2>&1; then
    echo "jh_with_secrets: python3 not on PATH — cannot resolve secrets; command NOT run" >&2
    return 127
  fi
  python3 "$_JH_DIR/jh_secrets.py" --env-file "$env_file" -- "$@"
}

# --- jh_spool_append: telemetry transport -------------------------------------
# Usage: jh_spool_append <spool-file> <payload>
#
# Appends <payload> as ONE line to <spool-file>, creating the directory. The
# payload is the caller's: the host checks only that it is a single line of
# valid JSON (a broken line would corrupt every reader of a JSONL spool), and
# never inspects its fields. Never fails or blocks the job: always returns 0;
# a refused or failed write is reported on stderr (and in the run log).
jh_spool_append() {
  local spool="${1:-}" payload="${2:-}"
  if [[ -z "$spool" ]]; then
    jh_log "jh_spool_append: no spool file given — payload dropped"
    return 0
  fi
  if [[ -z "$payload" ]]; then
    jh_log "jh_spool_append: empty payload — nothing appended to $spool"
    return 0
  fi
  case "$payload" in
    *$'\n'*|*$'\r'*)
      jh_log "jh_spool_append: payload spans more than one line — refused, nothing appended to $spool"
      return 0
      ;;
  esac
  if ! command -v python3 >/dev/null 2>&1; then
    jh_log "jh_spool_append: python3 not on PATH — cannot check the payload, nothing appended to $spool"
    return 0
  fi
  if ! printf '%s' "$payload" | python3 -c 'import json,sys; json.loads(sys.stdin.read())' 2>/dev/null; then
    jh_log "jh_spool_append: payload is not valid JSON — refused, nothing appended to $spool"
    return 0
  fi
  # Parameter expansion, not dirname: a launchd PATH may not include /usr/bin.
  case "$spool" in
    */*) mkdir -p "${spool%/*}" 2>/dev/null || true ;;
  esac
  if ! printf '%s\n' "$payload" >>"$spool" 2>/dev/null; then
    jh_log "jh_spool_append: could not write to $spool — payload dropped"
  fi
  return 0
}

# _jh_default_timeout_record <limit_secs> <exit_code> <elapsed_secs>: a small
# job-host event, not a run record: {"event":"job-host.timeout", ...}.
_jh_default_timeout_record() {
  command -v python3 >/dev/null 2>&1 || return 1
  JH_R_TOOL="${TOOL_NAME:-}" JH_R_LIMIT="$1" JH_R_RC="$2" JH_R_ELAPSED="$3" \
  JH_R_STAMP="${LK_STAMP:-}" JH_R_AT="$(date +%Y-%m-%dT%H:%M:%S%z)" \
  python3 -c '
import json, os
e = os.environ
print(json.dumps({
    "event": "job-host.timeout",
    "tool": e["JH_R_TOOL"],
    "run_stamp": e["JH_R_STAMP"],
    "limit_secs": int(e["JH_R_LIMIT"]),
    "elapsed_secs": int(e["JH_R_ELAPSED"]),
    "exit_code": int(e["JH_R_RC"]),
    "at": e["JH_R_AT"],
}, sort_keys=True))
'
}

# --- jh_run_bounded: bound a command, record a timeout ------------------------
# Usage: jh_run_bounded <secs> <cmd> [args...]
#
# Runs <cmd> under jh_timeout and returns its exit status. When the bound is
# what ended it (124 or 137 AND at least <secs> elapsed — a command that exits
# 124 on its own is not a timeout), the child and its process group have been
# killed, a log line says so, and a payload is appended to JH_TIMEOUT_SPOOL:
# the stdout of the caller's JH_ON_TIMEOUT function if one is set, otherwise
# the default job-host event record. Safe under the caller's `set -e`.
jh_run_bounded() {
  local secs="${1:-}"
  if [[ ! "$secs" =~ ^[1-9][0-9]*$ || $# -lt 2 ]]; then
    echo "jh_run_bounded: usage: jh_run_bounded <secs> <cmd> [args...]" >&2
    return 2
  fi
  shift
  local started rc elapsed payload spool
  started=$SECONDS
  if jh_timeout "$secs" "$@"; then
    rc=0
  else
    rc=$?
  fi
  elapsed=$((SECONDS - started))

  if (( (rc == 124 || rc == 137) && elapsed >= secs )); then
    jh_log "jh_run_bounded: '$1' exceeded ${secs}s and was killed (exit $rc)"
    spool="${JH_TIMEOUT_SPOOL:-$HOME/.claude/telemetry/job-host-events.jsonl}"
    payload=""
    if [[ -n "${JH_ON_TIMEOUT:-}" ]]; then
      payload="$("$JH_ON_TIMEOUT" "$secs" "$rc" "$elapsed" 2>/dev/null)" || payload=""
    else
      payload="$(_jh_default_timeout_record "$secs" "$rc" "$elapsed" 2>/dev/null)" || payload=""
    fi
    jh_spool_append "$spool" "$payload"
  fi
  return "$rc"
}

# --- lib.sh-name wrappers -----------------------------------------------------
if [[ "${JH_NO_LK_ALIASES:-0}" != "1" ]]; then
  launchd_kit_init()   { jh_init "$@"; }
  lk_log()             { jh_log "$@"; }
  lk_on_exit()         { jh_on_exit "$@"; }
  lk_slot_guard()      { jh_slot_guard "$@"; }
  lk_single_lock()     { jh_single_lock "$@"; }
  lk_require_cmd()     { jh_require_cmd "$@"; }
  lk_rotate_logs()     { jh_rotate_logs "$@"; }
  _lk_timeout()        { jh_timeout "$@"; }
  _lk_have_cmd()       { _jh_have_cmd "$@"; }
  _lk_parse_duration() { _jh_parse_duration "$@"; }

fi

# These integrations own their names; host.sh supplies their explicit runtime
# dependencies through the native job-host API. Their include guards make
# repeat sourcing safe, including when JH_NO_LK_ALIASES=1.
_LK_NOTIFY_TIMEOUT_FN=jh_timeout
_LK_NOTIFY_ON_EXIT_FN=jh_on_exit
_LK_CHAIN_TIMEOUT_FN=jh_timeout
_LK_TELEMETRY_TIMEOUT_FN=jh_timeout
_LK_TELEMETRY_ON_EXIT_FN=jh_on_exit
# shellcheck source=launchd-kit/lib/notify.sh
source "$_JH_DIR/../launchd-kit/lib/notify.sh"
# shellcheck source=launchd-kit/lib/chain.sh
source "$_JH_DIR/../launchd-kit/lib/chain.sh"
# shellcheck source=launchd-kit/lib/telemetry.sh
source "$_JH_DIR/../launchd-kit/lib/telemetry.sh"
