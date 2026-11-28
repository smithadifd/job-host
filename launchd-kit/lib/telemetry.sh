#!/usr/bin/env bash
# serotine.run.v1 payload and sink integration shared by launchd-kit and job-host.

if [[ "${_LK_TELEMETRY_SH_LOADED:-0}" == "1" ]]; then
  return 0
fi

# Dependencies are supplied by launchd-kit/lib.sh or job-host/host.sh.
_LK_TELEMETRY_TIMEOUT_FN="${_LK_TELEMETRY_TIMEOUT_FN:-_lk_timeout}"
_LK_TELEMETRY_ON_EXIT_FN="${_LK_TELEMETRY_ON_EXIT_FN:-lk_on_exit}"
if ! declare -F "$_LK_TELEMETRY_TIMEOUT_FN" >/dev/null \
    || ! declare -F "$_LK_TELEMETRY_ON_EXIT_FN" >/dev/null; then
  echo "telemetry.sh: requires timeout and exit-handler functions" >&2
  return 1
fi
_LK_TELEMETRY_SH_LOADED=1

# Resolve from lib.sh for its generated copy and one directory up for this
# sourceable module, without relying on the caller's working directory.
_LK_TELEMETRY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && { [[ "${BASH_SOURCE[0]##*/}" != "telemetry.sh" ]] || cd ..; } && pwd)"

# ==============================================================================
# serotine.run.v1 telemetry (opt-in)
# ------------------------------------------------------------------------------
# One JSONL line per run appended to a local spool, then best-effort POSTed to a
# Fleet/Serotine ingest API.
# The wire namespace is serotine.*.
# The line shape is validated by telemetry/validate_serotine_run.py against
# telemetry/serotine-run-v1.schema.json.
#
# SAFETY (the #1 property — lib.sh fronts LIVE production jobs): nothing in this
# section may ever fail or block the wrapper. Every path swallows errors, the
# push has a short timeout and never retries within a run, and the wrapper's exit
# code is completely unaffected by anything the hook does — same posture as
# lk_notify. The public entry points ALWAYS return 0.
#
# The product name is provisional (telemetry/README.md § Renaming checklist);
# every namespace occurrence is kept as the literal, grep-able prefix `serotine.`
# / `SEROTINE_` on purpose so a rename is a single mechanical sweep.
# ==============================================================================

# RFC 3339 timestamp with an explicit colon offset. Arg: epoch
# seconds (default: now). Empty string on any failure (callers tolerate it).
_lk_rfc3339() {
  local epoch="${1:-$(date +%s 2>/dev/null)}"
  { date -r "$epoch" +%Y-%m-%dT%H:%M:%S%z 2>/dev/null ||
    date -d "@$epoch" +%Y-%m-%dT%H:%M:%S%z 2>/dev/null; } \
    | sed -E 's/([0-9]{2})([0-9]{2})$/\1:\2/'
}

# 4 lowercase hex chars for the serotine.run.id suffix. openssl → urandom → RANDOM.
_lk_rand4() {
  local h
  h="$(openssl rand -hex 2 2>/dev/null)" || h=""
  if [[ ! "$h" =~ ^[0-9a-f]{4}$ ]]; then
    h="$(od -An -N2 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')"
  fi
  [[ "$h" =~ ^[0-9a-f]{4}$ ]] || h="$(printf '%04x' $((RANDOM & 0xffff)))"
  printf '%s' "$h"
}

# lk_enable_run_telemetry: record the run start + arm the EXIT trap fallback.
# Call once after launchd_kit_init and BEFORE the guards. Purely additive.
lk_enable_run_telemetry() {
  LK_TELEMETRY_ON=1
  LK_EMITTED=0
  SEROTINE_RUN_START="$(_lk_rfc3339 2>/dev/null || true)"
  export LK_TELEMETRY_ON LK_EMITTED SEROTINE_RUN_START
  "$_LK_TELEMETRY_ON_EXIT_FN" _lk_emit_run_exit
}

# _lk_emit_run_exit: EXIT-trap fallback. Only fires when the wrapper never
# reached its explicit lk_emit_run — i.e. it crashed (non-zero exit → fail with
# the real exit code) or a slot-guard/lock short-circuited (exit 0 → skip).
_lk_emit_run_exit() {
  [[ "${LK_TELEMETRY_ON:-0}" == "1" ]] || return 0
  [[ "${LK_EMITTED:-0}" == "1" ]] && return 0
  local status
  if (( ${LK_EXIT_CODE:-0} == 0 )); then status="skip"; else status="fail"; fi
  _lk_emit_run_core --status "$status" --exit-code "${LK_EXIT_CODE:-0}" || true
  return 0
}

# lk_emit_run: the wrapper's explicit terminal emit (the ok path). Call ONCE as
# the last thing the wrapper does. Always returns 0; never affects the exit code.
lk_emit_run() {
  _lk_emit_run_core "$@" || true
  LK_EMITTED=1
  return 0
}

# _lk_spool_write_line: write one JSONL record (`$1\n`) onto `$2`, optionally
# prefixed by the separator in `$3`. Isolated so tests can stub a short write
# without filling a disk. A complete or empty spool passes no separator, so its
# healthy-disk bytes remain exactly `printf '%s\n'`.
_lk_spool_write_line() {
  printf '%s%s\n' "${3:-}" "$1" >>"$2"
}

# _lk_truncate_spool: shrink `$1` to `$2` bytes (0 = empty) IN PLACE, so a
# full disk cannot block recovery. Only returns 0 when the resulting size
# matches; the caller logs "truncated" only then.
_lk_truncate_spool() {
  local path="$1" bytes="$2" got
  [[ -f "$path" ]] || return 0
  if (( bytes <= 0 )); then
    : >"$path" || return 1
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import os,sys; os.truncate(sys.argv[1], int(sys.argv[2]))' \
      "$path" "$bytes" || return 1
  elif command -v perl >/dev/null 2>&1; then
    perl -e 'truncate($ARGV[0], 0+$ARGV[1]) or die $!' "$path" "$bytes" \
      || return 1
  else
    return 1
  fi
  got="$(wc -c <"$path" 2>/dev/null | tr -d ' ')"
  [[ "$got" =~ ^[0-9]+$ ]] && (( got == bytes )) || return 1
  return 0
}

# _lk_spool_size <path>: print the byte size of a spool. Non-zero when the
# probe exits non-zero or the output is not an integer. An unreadable size
# is not zero — callers must not coerce a failure into an empty spool.
_lk_spool_size() {
  local path="$1" raw
  raw="$(wc -c <"$path" 2>/dev/null)" || return 1
  raw="${raw#"${raw%%[![:space:]]*}"}"
  raw="${raw%"${raw##*[![:space:]]}"}"
  [[ "$raw" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "$raw"
}


# An unknown spool size never authorizes a truncate.
# _lk_append_spool_line <line> <spool>: append one fully formed JSONL record.
# Append + verify + truncate-back run under an exclusive flock on
# <spool>.lock, shared with telemetry/serotine_spool.py. On a short write
# or writer failure, truncate back to the pre-write size so no torn record
# remains — only when that size was read. A pre-append size probe that
# exits non-zero or prints a non-integer refuses before writing. A
# post-append probe that fails the same way is a named error and does not
# roll back. If the lock cannot be taken within SEROTINE_SPOOL_LOCK_TIMEOUT
# seconds (default 5; finite > 0, capped at 300; anything else uses 5),
# do not write and do not truncate. An unopenable <spool>.lock is a named
# error, not a silent refusal. Reports via lk_log
# (else stderr) and returns non-zero; callers in live jobs must swallow
# that (lk_emit_run always returns 0).
_lk_append_spool_line() {
  local line="$1" spool="$2"
  local pre=0 size=0 expected rc=0 size_known=0
  local last_byte separator=""
  local lock="${spool}.lock"
  local timeout timeout_i max_i
  local hold status release pid i s locked=0 crit_rc=0

  if ! command -v python3 >/dev/null 2>&1; then
    if type lk_log >/dev/null 2>&1; then
      lk_log "spool append failed ($spool); lock not acquired"
    else
      echo "[launchd-kit] spool append failed ($spool); lock not acquired" >&2
    fi
    return 1
  fi

  timeout="$(python3 -c '
import math, os
raw = os.environ.get("SEROTINE_SPOOL_LOCK_TIMEOUT")
default, cap = 5.0, 300.0
try:
    v = float(raw) if raw else default
except (TypeError, ValueError):
    v = default
if not math.isfinite(v) or v <= 0:
    v = default
elif v > cap:
    v = cap
print("%.6f" % v)
' 2>/dev/null)" || true
  [[ "$timeout" =~ ^[0-9]+\.[0-9]+$ ]] || timeout="5.000000"
  timeout_i="${timeout%.*}"
  [[ "$timeout_i" =~ ^[0-9]+$ ]] || timeout_i=5
  max_i=$((timeout_i * 50 + 100))
  (( max_i >= 1 )) || max_i=350

  hold="$(mktemp -d "${TMPDIR:-/tmp}/lk-spool-lock.XXXXXX")" || {
    if type lk_log >/dev/null 2>&1; then
      lk_log "spool append failed ($spool); lock not acquired"
    else
      echo "[launchd-kit] spool append failed ($spool); lock not acquired" >&2
    fi
    return 1
  }
  status="$hold/st"
  release="$hold/rel"
  : > "$status"
  python3 -c '
import errno, fcntl, os, sys, time
lock, status, release, timeout = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
try:
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
except OSError:
    try:
        with open(status, "w") as f:
            f.write("unopenable\n")
    except OSError:
        pass
    sys.exit(2)
deadline = time.monotonic() + timeout
held = False
try:
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = True
            break
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                with open(status, "w") as f:
                    f.write("err\n")
                sys.exit(2)
            if time.monotonic() >= deadline:
                with open(status, "w") as f:
                    f.write("timeout\n")
                sys.exit(1)
            time.sleep(0.02)
    with open(status, "w") as f:
        f.write("ok\n")
    while not os.path.exists(release):
        if os.getppid() == 1:
            break
        time.sleep(0.02)
finally:
    if held:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
    os.close(fd)
' "$lock" "$status" "$release" "$timeout" &
  pid=$!
  i=0
  while (( i < max_i )); do
    s="$(cat "$status" 2>/dev/null || true)"
    case "$s" in
      ok*) locked=1; break ;;
      unopenable*) break ;;
      timeout*|err*) break ;;
    esac
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 0.02
    i=$((i + 1))
  done
  if (( locked != 1 )); then
    kill "$pid" 2>/dev/null || true
    : > "$release"
    wait "$pid" 2>/dev/null || true
    s="$(cat "$status" 2>/dev/null || true)"
    rm -f "$status" "$release"
    rmdir "$hold" 2>/dev/null || true
    if [[ "$s" == unopenable* ]]; then
      if type lk_log >/dev/null 2>&1; then
        lk_log "spool lock unopenable ($lock)"
      else
        echo "[launchd-kit] spool lock unopenable ($lock)" >&2
      fi
    elif type lk_log >/dev/null 2>&1; then
      lk_log "spool append failed ($spool); lock not acquired"
    else
      echo "[launchd-kit] spool append failed ($spool); lock not acquired" >&2
    fi
    return 1
  fi

  if [[ -f "$spool" ]]; then
    if ! pre="$(_lk_spool_size "$spool")"; then
      : > "$release"
      wait "$pid" 2>/dev/null || true
      rm -f "$status" "$release"
      rmdir "$hold" 2>/dev/null || true
      if type lk_log >/dev/null 2>&1; then
        lk_log "spool size unreadable; append skipped"
      else
        echo "[launchd-kit] spool size unreadable; append skipped" >&2
      fi
      return 1
    fi
  fi
  if (( pre > 0 )); then
    last_byte="$(tail -c 1 "$spool" 2>/dev/null \
      | od -An -tu1 2>/dev/null | tr -d '[:space:]')"
    if [[ ! "$last_byte" =~ ^[0-9]+$ ]]; then
      : > "$release"
      wait "$pid" 2>/dev/null || true
      rm -f "$status" "$release"
      rmdir "$hold" 2>/dev/null || true
      if type lk_log >/dev/null 2>&1; then
        lk_log "spool final byte unreadable; append skipped"
      else
        echo "[launchd-kit] spool final byte unreadable; append skipped" >&2
      fi
      return 1
    fi
    [[ "$last_byte" == 10 ]] || separator=$'\n'
  fi
  expected="$(printf '%s%s\n' "$separator" "$line" | wc -c | tr -d ' ')"
  if [[ ! "$expected" =~ ^[0-9]+$ ]]; then
    : > "$release"
    wait "$pid" 2>/dev/null || true
    rm -f "$status" "$release"
    rmdir "$hold" 2>/dev/null || true
    return 1
  fi
  _lk_spool_write_line "$line" "$spool" "$separator" || rc=$?
  if [[ -f "$spool" ]]; then
    if size="$(_lk_spool_size "$spool")"; then
      size_known=1
    fi
  else
    size=0
    size_known=1
  fi
  if (( size_known != 1 )); then
    if type lk_log >/dev/null 2>&1; then
      lk_log "spool size unreadable; append not truncated"
    else
      echo "[launchd-kit] spool size unreadable; append not truncated" >&2
    fi
    crit_rc=1
  elif (( rc != 0 )) || (( size != pre + expected )); then
    if _lk_truncate_spool "$spool" "$pre"; then
      if type lk_log >/dev/null 2>&1; then
        lk_log "spool append failed ($spool); truncated to ${pre} bytes"
      else
        echo "[launchd-kit] spool append failed ($spool); truncated to ${pre} bytes" >&2
      fi
    else
      if type lk_log >/dev/null 2>&1; then
        lk_log "spool append failed ($spool); truncate to ${pre} bytes failed"
      else
        echo "[launchd-kit] spool append failed ($spool); truncate to ${pre} bytes failed" >&2
      fi
    fi
    crit_rc=1
  fi

  : > "$release"
  wait "$pid" 2>/dev/null || true
  rm -f "$status" "$release"
  rmdir "$hold" 2>/dev/null || true
  return "$crit_rc"
}


# _lk_emit_run_core: build one serotine.run.v1 line, append to the spool, push.
# All work is best-effort; returns 0 unconditionally.
_lk_emit_run_core() {
  local status="ok" exit_code=0 operation="" usage_json="" usage_file=""
  local trigger="${SEROTINE_TRIGGER:-launchd}" label="" failure_message=""
  while (( $# )); do
    case "$1" in
      --status)     status="${2:-ok}"; shift 2 || shift;;
      --exit-code)  exit_code="${2:-0}"; shift 2 || shift;;
      --operation)  operation="${2:-}"; shift 2 || shift;;
      --usage-json) usage_json="${2:-}"; shift 2 || shift;;
      --usage-file) usage_file="${2:-}"; shift 2 || shift;;
      --trigger)    trigger="${2:-launchd}"; shift 2 || shift;;
      --label)      label="${2:-}"; shift 2 || shift;;
      --failure-message) failure_message="${2:-}"; shift 2 || shift;;
      *) shift;;
    esac
  done

  local helper spool_dir spool cursor host end stamp label_val runid line lk_user
  local failure_json
  helper="${SEROTINE_EMIT_HELPER:-$_LK_TELEMETRY_ROOT/serotine_usage.py}"
  command -v python3 >/dev/null 2>&1 || return 0
  [[ -f "$helper" ]] || return 0

  spool_dir="${SEROTINE_SPOOL_DIR:-$HOME/.claude/telemetry}"
  spool="$spool_dir/spool.jsonl"
  cursor="$spool.cursor"
  host="${SEROTINE_HOST:-$(hostname -s 2>/dev/null || hostname 2>/dev/null)}"
  end="$(_lk_rfc3339)"
  stamp="${LK_STAMP:-$(date +%Y-%m-%d_%H%M%S 2>/dev/null)}"
  # An explicit --label or SEROTINE_JOB_LABEL wins. The default is
  # com.<launchd user>.<tool>: the user part comes from `id -un` (so it is the
  # same label as before for the same launchd user) and falls back to
  # `unknown`, never an empty segment, if `id` fails.
  label_val="${label:-${SEROTINE_JOB_LABEL:-}}"
  if [[ -z "$label_val" ]]; then
    lk_user="$(id -un 2>/dev/null)" || lk_user=""
    label_val="com.${lk_user:-unknown}.${TOOL_NAME:-unknown}"
  fi
  runid="$label_val/$stamp-$(_lk_rand4)"

  # Opt-in failure forensics, BEFORE the line is built (unlike the trace, which
  # is a separate POST and must wait for the run line to land).
  #
  # THE GATE IS REPEATED HERE, at the call site, and that is not redundancy for
  # its own sake: a command substitution forks a subshell unconditionally, even
  # around a function that returns on its first line. `_lk_attach_trace` below
  # is a plain call and forks nothing when disarmed, and this path should cost
  # a disarmed job exactly as little. With the flag unset the line below is one
  # parameter expansion and an empty assignment — no fork, no subshell, no
  # helper. The gate inside _lk_collect_failure stays too: it is the one that
  # makes the FUNCTION safe to call directly, which its own tests do.
  failure_json=""
  if [[ -n "${SEROTINE_EMIT_FAILURE:-}" ]]; then
    failure_json="$(_lk_collect_failure "$status" "$failure_message" 2>/dev/null || true)"
  fi

  # Build the whole line in python3 so JSON quoting is correct by construction
  # and the optional serotine.usage array is resolved (provider adapter, when
  # SEROTINE_USAGE_PROVIDER is set → explicit → window fallback). The three
  # SEROTINE_USAGE_PROVIDER/_INPUT/_REQUEST_MODEL vars are passed through
  # exactly like SEROTINE_USAGE_SESSION_ID below (an explicit "${VAR:-}"
  # passthrough of whatever the wrapper already exported, defaulting to
  # unset/empty) — a wrapper that never sets them sees byte-identical
  # behavior; this is PURELY ADDITIVE, same posture as every other opt-in
  # var in this hook.
  #
  # SEROTINE_LINE_FAILURE_JSON is assigned UNCONDITIONALLY, empty when the
  # forensics gate is off. That is deliberate and load-bearing: this prefix
  # does not clear the rest of the environment, so an assignment that were
  # conditional would let a wrapper's own inherited value reach the helper with
  # the flag unset. An explicit empty assignment shadows it.
  line="$(
    SEROTINE_LINE_RUN_ID="$runid" \
    SEROTINE_LINE_JOB_LABEL="$label_val" \
    SEROTINE_LINE_HOST="$host" \
    SEROTINE_LINE_START="${SEROTINE_RUN_START:-$end}" \
    SEROTINE_LINE_END="$end" \
    SEROTINE_LINE_EXIT_CODE="$exit_code" \
    SEROTINE_LINE_STATUS="$status" \
    SEROTINE_LINE_TRIGGER="$trigger" \
    SEROTINE_LINE_OPERATION="$operation" \
    SEROTINE_USAGE_JSON="$usage_json" \
    SEROTINE_USAGE_FILE="$usage_file" \
    SEROTINE_USAGE_WINDOW_START="${SEROTINE_RUN_START:-}" \
    SEROTINE_USAGE_WINDOW_END="$end" \
    SEROTINE_USAGE_SESSION_ID="${SEROTINE_USAGE_SESSION_ID:-}" \
    SEROTINE_USAGE_DISABLE="${SEROTINE_USAGE_FALLBACK_DISABLE:-0}" \
    SEROTINE_USAGE_PROVIDER="${SEROTINE_USAGE_PROVIDER:-}" \
    SEROTINE_USAGE_INPUT="${SEROTINE_USAGE_INPUT:-}" \
    SEROTINE_USAGE_REQUEST_MODEL="${SEROTINE_USAGE_REQUEST_MODEL:-}" \
    SEROTINE_LINE_FAILURE_JSON="$failure_json" \
    python3 "$helper" build-line 2>/dev/null
  )" || return 0
  [[ -n "$line" ]] || return 0

  mkdir -p "$spool_dir" 2>/dev/null || true
  _lk_append_spool_line "$line" "$spool" || true
  _lk_push_spool "$spool" "$cursor" || true
  # Opt-in failure trace, AFTER the push: the run line must be ingested before
  # its trace or the server answers 404 (store.ts § Contract). Returns
  # immediately unless SEROTINE_TRACE_CAPTURE is set — see its own section.
  _lk_attach_trace "$runid" "$label_val" "$host" "$status" "$exit_code" || true
  return 0
}

# _lk_push_spool: best-effort POST of unsent spool bytes (byte-offset cursor).
# No-op unless BOTH SEROTINE_INGEST_URL and SEROTINE_INGEST_TOKEN are set. On a 2xx
# response the cursor advances to the byte size that was read; otherwise the
# cursor is untouched so the batch is retried on a later run. run.id is the
# ingest dedupe key, so re-POSTing the same lines is safe by design. Never
# retries within a run; short timeout; always returns 0.
#
# serotine.event.findings is SPOOL-ONLY:
# the whole array is stripped from the POST body FAIL-CLOSED before it leaves the
# machine — a body carrying the key is re-serialized without it, and any line
# that cannot be stripped safely is dropped from the body rather than sent
# unstripped. This mirrors serotine_spool.py's push layer (its _strip_findings is
# reused verbatim, so bash and Python emit byte-identical stripped bytes). A body
# with NO findings key skips the strip entirely and is POSTed byte-for-byte as
# before — the run.v1 push path is unchanged and needs no python3.
# _lk_resync_cursor: repair a byte-offset cursor that no longer names a record
# boundary, BEFORE it is used to slice a POST body. Sets $_LK_CURSOR_OFF to the
# offset to use; returns non-zero only when it cannot establish a safe one.
#
# Why this exists: a cleanup removed two synthetic records
# from the MIDDLE of the live spool and left the cursor alone. The cursor was
# then 780 bytes past the end of the file. Every later push sliced from that
# offset, so each body began mid-record (`rotine.schema": ...`), the collector
# rejected the malformed batch, and the cursor only advances on a 2xx — so it
# never moved and the same broken batch was re-sent every tick, forever. The
# whole host went dark to serotine in one edit, with NO local signal: this
# path logged nothing, and the only reason it surfaced inside 15 minutes is that
# the heartbeat job has a dead-man's-switch. Every other job would have been
# silently undelivered for days.
#
# STRICTLY CONSERVATIVE: the offset only ever moves DOWN, so a resync can cause
# a re-send but can never skip a record. Re-sending is safe by design — run.id
# is the ingest dedupe key (see _lk_push_spool's header).
#
# The clamp deliberately trusts the CURRENT file: a spool that shrank is assumed
# to have been edited in place (the real case), not rotated and refilled with
# different content. Resetting to 0 instead would re-POST the entire history —
# megabytes against a 5s timeout, which would simply stall in a new way. The
# log line below is what makes the choice reviewable rather than silent.
_LK_CURSOR_OFF=""
_lk_resync_cursor() {
  local spool="$1" cursor="$2" off="$3" size="$4"
  local orig="$off" partial byte

  # (1) Spool shrank below the cursor — edited, truncated, or restored.
  (( off > size )) && off="$size"

  # (2) The cursor must sit immediately after a newline (or at 0). Byte $off is
  # the last byte of the already-pushed region; seek to it rather than reading
  # the file, so the healthy path stays O(1).
  if (( off > 0 )); then
    byte="$(tail -c +"$off" "$spool" 2>/dev/null | head -c 1 | od -An -tu1 2>/dev/null | tr -cd '0-9')"
    if [[ "$byte" != 10 ]]; then
      # Back up over the trailing partial record. LC_ALL=C so awk's length()
      # counts BYTES, not characters — a UTF-8 payload would otherwise
      # under-count and leave the cursor mid-record again. POSIX keeps the last
      # record in $0 at END.
      partial="$(head -c "$off" "$spool" 2>/dev/null | LC_ALL=C awk 'END{ print length($0) }' 2>/dev/null)"
      if [[ ! "$partial" =~ ^[0-9]+$ ]] || (( partial > off )); then
        type lk_log >/dev/null 2>&1 && lk_log "spool cursor $orig is not on a record boundary and the boundary could not be located — HELD, nothing pushed this run"
        return 1
      fi
      off=$(( off - partial ))
    fi
  fi

  if (( off != orig )); then
    printf '%s' "$off" >"$cursor" 2>/dev/null || true
    type lk_log >/dev/null 2>&1 && lk_log "spool cursor resynced ${orig} -> ${off} (spool size ${size}) — cursor was off a record boundary; every push would have been rejected until corrected"
  fi
  _LK_CURSOR_OFF="$off"
  return 0
}

_lk_push_spool() {
  local spool="$1" cursor="$2"
  [[ -n "${SEROTINE_INGEST_URL:-}" && -n "${SEROTINE_INGEST_TOKEN:-}" ]] || return 0
  command -v curl >/dev/null 2>&1 || return 0
  [[ -f "$spool" ]] || return 0

  local off size body code strip_helper complete
  off="$(cat "$cursor" 2>/dev/null || echo 0)"
  [[ "$off" =~ ^[0-9]+$ ]] || off=0
  size="$(wc -c <"$spool" 2>/dev/null | tr -d ' ')"
  [[ "$size" =~ ^[0-9]+$ ]] || return 0

  _lk_resync_cursor "$spool" "$cursor" "$off" "$size" || return 0
  off="$_LK_CURSOR_OFF"

  complete="$size"
  strip_helper="${SEROTINE_STRIP_HELPER:-$_LK_TELEMETRY_ROOT/telemetry/serotine_spool.py}"
  if command -v python3 >/dev/null 2>&1 && [[ -f "$strip_helper" ]]; then
    if ! complete="$(SEROTINE_STRIP_HELPER="$strip_helper" python3 -c '
import os, sys
sys.path.insert(0, os.path.dirname(os.environ["SEROTINE_STRIP_HELPER"]))
import serotine_spool
print(serotine_spool._complete_end(sys.argv[1], int(sys.argv[2])))
' "$spool" "$size" 2>/dev/null)"; then
      echo "[launchd-kit] spool complete boundary unavailable; push skipped" >&2
      return 0
    elif [[ ! "$complete" =~ ^[0-9]+$ ]]; then
      echo "[launchd-kit] spool complete boundary unavailable; push skipped" >&2
      return 0
    fi
  fi

  (( complete > off )) || return 0

  if (( complete < size )); then
    body="$(tail -c +"$((off + 1))" "$spool" 2>/dev/null | head -c "$((complete - off))")" || return 0
  else
    body="$(tail -c +"$((off + 1))" "$spool" 2>/dev/null)" || return 0
  fi
  [[ -n "$body" ]] || return 0

  # Drop incomplete/unparseable JSONL records (and strip spool-only findings)
  # before POST so a torn last line cannot poison the batch. Complete records
  # keep original bytes. A findings-free body still needs no python3: if the
  # helper cannot run, the batch is POSTed as-is (same as before). A body that
  # carries findings still HOLDs when the helper cannot run.
  if command -v python3 >/dev/null 2>&1 && [[ -f "$strip_helper" ]]; then
    body="$(printf '%s' "$body" | SEROTINE_STRIP_HELPER="$strip_helper" python3 -c '
import os, sys
sys.path.insert(0, os.path.dirname(os.environ["SEROTINE_STRIP_HELPER"]))
import serotine_spool
sys.stdout.buffer.write(serotine_spool._strip_findings(sys.stdin.buffer.read()))
' 2>/dev/null)" || return 0
    [[ -n "$body" ]] || return 0
  elif [[ "$body" == *'"serotine.event.findings"'* ]]; then
    return 0
  fi

  code="$(printf '%s' "$body" | curl -sS -m "${SEROTINE_INGEST_TIMEOUT:-5}" \
      -o /dev/null -w '%{http_code}' -X POST \
      -H "Authorization: Bearer $SEROTINE_INGEST_TOKEN" \
      -H 'Content-Type: application/x-ndjson' \
      --data-binary @- "$SEROTINE_INGEST_URL" 2>/dev/null)" || return 0
  if [[ "$code" =~ ^2[0-9][0-9]$ ]]; then
    printf '%s' "$complete" >"$cursor" 2>/dev/null || true
  fi
  return 0
}

# ==============================================================================
# Wrapper-side FAILURE FORENSICS on the run line (opt-in per job; OFF fleet-wide)
# ------------------------------------------------------------------------------
# Failure forensics for the run record's optional diagnostic object. The wire
# accepts an optional `serotine.run.failure` object, `/jobs/<label>` renders it
# beneath the run it belongs to, and the weekly digest quotes an excerpt. It has
# rendered EMPTY on the real fleet the whole time, because nothing on the
# wrapper side has ever populated it — serotine's own AGENTS.md says so:
# "NOT BUILT HERE: the emitter itself (launchd-kit lib.sh)". This section is
# that emitter.
#
# NOT the same thing as the trace capture below, and not a replacement for it.
# The trace is a 200-line log tail POSTed to a SECOND endpoint
# (/api/v1/ingest/trace) and stored separately; this is a three-string summary
# riding the run LINE, so the answer to "why did this fail?" is on the job page
# without a second fetch. Independent flags, independently armed.
#
# DEFAULT OFF, FLEET-WIDE. `_lk_collect_failure` returns on its first line
# unless SEROTINE_EMIT_FAILURE is set: no subprocess, no file read, no
# measurable delay, and `_lk_emit_run_core` then passes an EMPTY
# SEROTINE_LINE_FAILURE_JSON, so the emitted line is byte-for-byte the line
# this library emitted before the feature existed. NOTHING IS ARMED BY THIS
# CODE. Enabling a job is a separate, supervised act.
#
# The flag is deliberately its OWN name, not SEROTINE_EMIT_EVENTS and not
# SEROTINE_TRACE_CAPTURE: the three lanes ship different data to different
# surfaces and must be armable one at a time.
#
# NEVER FAILS OR DELAYS THE JOB IT INSTRUMENTS — the same posture as the rest
# of this file. The helper subprocess is hard-bounded by _lk_timeout, every
# path swallows its errors, and the wrapper's exit code is untouched.
#
# FAIL-CLOSED ON REDACTION, and this one matters more than the trace's does:
# the object is PERSISTED in serotine's `runs.raw` and RENDERED on the job page,
# and per serotine AGENTS.md the consequence is ONE-WAY — "adding a strip later
# would protect future lines only". Redaction is the EMITTER's job. The body is
# therefore built by telemetry/serotine_trace.py (`build-failure`), which is
# where launchd-kit's only redactor lives; if that helper or python3 is
# unavailable, or it returns nothing, NOTHING IS ATTACHED. Read that module's
# REDACTION header before enabling a job.
#
# Env-var contract (all optional; read at emit time):
#   SEROTINE_EMIT_FAILURE       THE ARMING FLAG. Unset (default) = fully inert.
#                               `fail` (also 1|true|yes|on) = attach only when
#                               the emitted run status is `fail`. `always` =
#                               attach on ok/skip too. Anything else = off.
#   SEROTINE_FAILURE_LOG        Log file to summarize. Default: $LOG (this run's).
#   SEROTINE_FAILURE_LINES      Last-N non-blank log lines for stderr_tail.
#                               Default: 20 — serotine's own display bound.
#                               Must be a POSITIVE integer; 0 and anything
#                               non-numeric take the default (0 is falsy in the
#                               helper and would disable the cut entirely).
#   SEROTINE_FAILURE_MESSAGE    Default `message` member when the caller passes
#                               no --failure-message (the EXIT-trap crash path
#                               has no argv to carry one).
#   SEROTINE_FAILURE_TIMEOUT    Hard bound in seconds on the helper. Default: 10.
#                               Must be a POSITIVE integer — `timeout 0` means
#                               "no timeout" to GNU timeout AND to the perl
#                               alarm() leg, so 0 takes the default rather than
#                               silently removing the bound.
#   SEROTINE_FAILURE_HELPER     Path to serotine_trace.py (tests override it).
#   SEROTINE_TRACE_REDACT_ENV / SEROTINE_TRACE_REDACT_PATTERN are reused as-is:
#                               a job that has tuned redaction for its own log
#                               should not restate it for the same log's summary.
# ==============================================================================

# _lk_collect_failure: PRINT the serotine.run.failure object for this run, or
# nothing. Args: <status> <message>. Always returns 0-or-empty to the caller;
# never writes a file, never touches the network.
_lk_collect_failure() {
  # THE INERTNESS GATE. Nothing below this line runs for a job that has not
  # opted in — one parameter expansion, no subprocess, no syscall.
  [[ -n "${SEROTINE_EMIT_FAILURE:-}" ]] || return 0

  local mode
  # Explicit alternatives rather than ${VAR,,}: this library still supports the
  # bash 3.2 that ships with macOS (see lk_require_cmd's note).
  case "${SEROTINE_EMIT_FAILURE}" in
    fail|FAIL|Fail|1|true|TRUE|True|yes|YES|Yes|on|ON|On) mode="fail" ;;
    always|ALWAYS|Always)                                 mode="always" ;;
    *) return 0 ;;
  esac

  local status="${1:-}" message="${2:-}"
  [[ "$mode" == "always" || "$status" == "fail" ]] || return 0
  [[ -n "$message" ]] || message="${SEROTINE_FAILURE_MESSAGE:-}"

  command -v python3 >/dev/null 2>&1 || return 0

  local helper log lines timeout
  helper="${SEROTINE_FAILURE_HELPER:-$_LK_TELEMETRY_ROOT/telemetry/serotine_trace.py}"
  # FAIL CLOSED: no redactor on disk means no forensics, never unredacted ones.
  [[ -f "$helper" ]] || return 0

  log="${SEROTINE_FAILURE_LOG:-${LOG:-}}"
  [[ -n "$log" && -f "$log" ]] || log=""
  # An explicit message is worth attaching even with no readable log; a run with
  # neither has nothing to say and skips the subprocess entirely.
  [[ -n "$message" || -n "$log" ]] || return 0

  # STRICTLY POSITIVE, both of them. `^[0-9]+$` would accept 0, and a 0 here is
  # not "no limit by request" — it is the limit silently switching itself off:
  # `timeout 0` (GNU) and `alarm(0)` (the perl leg) BOTH mean "no timeout", so
  # SEROTINE_FAILURE_TIMEOUT=0 would defeat the hard bound this whole hook's
  # fail-safety rests on, and SEROTINE_FAILURE_LINES=0 is falsy in the helper
  # and disables the last-N cut. Verified, not assumed: `gtimeout
  # --kill-after=5s 0 sleep 4` runs the full 4s and exits 0. An out-of-range
  # value takes the documented default, exactly like a non-numeric one.
  lines="${SEROTINE_FAILURE_LINES:-20}"
  [[ "$lines" =~ ^[1-9][0-9]*$ ]] || lines=20
  timeout="${SEROTINE_FAILURE_TIMEOUT:-10}"
  [[ "$timeout" =~ ^[1-9][0-9]*$ ]] || timeout=10

  # Only NON-SECRET identifiers ride the `env` prefix — argv is world-readable
  # through `ps`. The credentials the redactor needs are the ones this wrapper
  # already holds in its own environment, INHERITED here rather than re-passed,
  # which is how it can redact their literal values.
  #
  # CAPTURED, THEN GATED ON THE EXIT STATUS — never streamed straight out. A
  # bare `… || return 0` would discard only the STATUS: whatever the helper had
  # already written to the pipe is captured by the caller's command
  # substitution regardless, so a helper that prints a well-formed object and
  # THEN hangs (killed at the bound, exit 124/137) or exits non-zero would ship
  # its output anyway. That silently converts the fail-closed guarantee into
  # "fail-closed unless the helper wrote something first", and the guarantee
  # must not depend on a helper's internal write ordering — a redactor killed
  # mid-pass is exactly the case this hook exists to refuse.
  local out rc
  out="$("$_LK_TELEMETRY_TIMEOUT_FN" "$timeout" env \
    SEROTINE_FAILURE_LOG="$log" \
    SEROTINE_FAILURE_MESSAGE="$message" \
    SEROTINE_FAILURE_LINES="$lines" \
    python3 "$helper" build-failure 2>/dev/null)"
  rc=$?
  (( rc == 0 )) || return 0
  printf '%s' "$out"
  return 0
}

# ==============================================================================
# Wrapper-side FAILURE TRACE capture (opt-in per job; OFF fleet-wide)
# ------------------------------------------------------------------------------
# Wrapper-side failure trace capture. Serotine's job pages show `Exit 74` next to an empty Trace
# column on every row because nothing on the wrapper side has ever attached a
# trace; the SERVER half (POST /api/v1/ingest/trace, GET /api/v1/traces/…,
# retention) has shipped for months. This section closes that: on a failing run
# the wrapper uploads the tail of its OWN run log as the run's trace, so a
# failure carries its stderr into the dashboard instead of requiring an SSH.
#
# DEFAULT OFF, FLEET-WIDE. `_lk_attach_trace` returns on its first line unless
# SEROTINE_TRACE_CAPTURE is set: no subprocess, no file read, no network, no
# log line, no measurable delay. Enabling is a separate, per-job supervised act.
#
# NEVER FAILS OR DELAYS THE JOB IT INSTRUMENTS — the same posture as lk_notify
# and the run.v1 emit hooks, and the reason telemetry is allowed near live
# production jobs at all. Every path swallows its errors, both subprocesses are
# hard-bounded by _lk_timeout (so a curl that ignores `-m`, or a hung DNS
# lookup, still cannot park the wrapper), there is no retry within a run, and
# the wrapper's exit code is untouched no matter what happens here. A first-tick
# failure at launchd boot is expected and is not treated as fatal (the LAN-fetch
# transient during certificate setup).
#
# BEST-EFFORT, NEVER SPOOLED. One attempt per run, no retry, nothing queued —
# a run whose telemetry push failed (host offline) also loses its trace. That is
# the deliberate trade: a spooled trace would leave run logs on disk waiting to
# be uploaded later. `always` is a debugging mode for one job, not a fleet
# setting: a poller that skips dozens of ticks a week would upload every one.
#
# FAIL-CLOSED ON REDACTION. The body is built (and redacted) by
# telemetry/serotine_trace.py. If that helper or python3 is unavailable, or it
# returns nothing, NOTHING IS UPLOADED — the run log never goes out unredacted.
# Read that module's header before enabling a job: it documents what the
# redactor does and does not cover.
#
# Env-var contract (all optional; read at emit time):
#   SEROTINE_TRACE_CAPTURE      THE ARMING FLAG. Unset (default) = fully inert.
#                               `fail` (also 1|true|yes|on) = upload only when
#                               the emitted run status is `fail`. `always` =
#                               upload on ok/skip too. Anything else = off.
#   SEROTINE_TRACE_INGEST_URL   Upload endpoint. Default: SEROTINE_INGEST_URL
#                               with `/trace` appended (serotine's own layout,
#                               /api/v1/ingest → /api/v1/ingest/trace). Upload
#                               happens only if the URL AND SEROTINE_INGEST_TOKEN
#                               resolve — the trace endpoint takes the SAME
#                               Bearer token as ingest.
#   SEROTINE_TRACE_LOG          Log file to capture. Default: $LOG (this run's).
#   SEROTINE_TRACE_LINES        Last-N log lines to send. Default: 200.
#   SEROTINE_TRACE_UPLOAD_MAX_BYTES  Body cap in bytes, applied AFTER redaction,
#                               trimming oldest-first. Default: 262144. (Named
#                               apart from serotine's server-side
#                               SEROTINE_TRACE_MAX_BYTES on purpose.)
#   SEROTINE_TRACE_TIMEOUT      curl --max-time seconds. Default: 10.
#   SEROTINE_TRACE_REDACT_ENV   Extra env var NAMES whose values to redact.
#   SEROTINE_TRACE_REDACT_PATTERN  Extra job-specific redaction regex. An
#                               unusable regex fails closed (nothing uploaded).
#   SEROTINE_TRACE_HELPER       Path to serotine_trace.py (tests override it).
# ==============================================================================

# _lk_attach_trace: upload this run's log tail as its serotine trace.
# Args: <run id> <job label> <host> <status> <exit code>. Always returns 0.
_lk_attach_trace() {
  # THE INERTNESS GATE. Nothing below this line runs for a job that has not
  # opted in — one parameter expansion, no subprocess, no syscall.
  [[ -n "${SEROTINE_TRACE_CAPTURE:-}" ]] || return 0

  local mode
  # Explicit alternatives rather than ${VAR,,}: this library still supports the
  # bash 3.2 that ships with macOS (see lk_require_cmd's note).
  case "${SEROTINE_TRACE_CAPTURE}" in
    fail|FAIL|Fail|1|true|TRUE|True|yes|YES|Yes|on|ON|On) mode="fail" ;;
    always|ALWAYS|Always)                                 mode="always" ;;
    *) return 0 ;;
  esac

  local runid="${1:-}" label="${2:-}" host="${3:-}" status="${4:-}" exit_code="${5:-0}"
  [[ -n "$runid" ]] || return 0
  [[ "$mode" == "always" || "$status" == "fail" ]] || return 0

  local url token log helper timeout body code
  token="${SEROTINE_INGEST_TOKEN:-}"
  url="${SEROTINE_TRACE_INGEST_URL:-}"
  if [[ -z "$url" && -n "${SEROTINE_INGEST_URL:-}" ]]; then
    url="${SEROTINE_INGEST_URL%/}/trace"
  fi
  [[ -n "$url" && -n "$token" ]] || return 0

  command -v curl >/dev/null 2>&1 || return 0
  command -v python3 >/dev/null 2>&1 || return 0

  log="${SEROTINE_TRACE_LOG:-${LOG:-}}"
  [[ -n "$log" && -f "$log" ]] || return 0

  helper="${SEROTINE_TRACE_HELPER:-$_LK_TELEMETRY_ROOT/telemetry/serotine_trace.py}"
  # FAIL CLOSED: no redactor on disk means no upload, never an unredacted one.
  [[ -f "$helper" ]] || return 0

  # STRICTLY POSITIVE — see the identical note in _lk_collect_failure. `timeout
  # 0` means "no timeout" to GNU timeout and to the perl alarm() leg alike, so
  # accepting 0 here would let one env var silently remove the hard bound this
  # section's whole fail-safe claim rests on. Tightened alongside the forensics
  # failure-forensics copy because it is literally the same latent defect in the same
  # file; no job sets this var, and 0 never had a legitimate meaning.
  timeout="${SEROTINE_TRACE_TIMEOUT:-10}"
  [[ "$timeout" =~ ^[1-9][0-9]*$ ]] || timeout=10

  # Build the body in python3 (JSON quoting correct by construction, redaction
  # applied there). Only NON-SECRET identifiers ride the `env` prefix — argv is
  # world-readable through `ps`. The credentials the redactor needs are the ones
  # this wrapper already holds in its own environment, INHERITED here rather
  # than re-passed, which is how it can redact their literal values.
  body="$("$_LK_TELEMETRY_TIMEOUT_FN" "$((timeout + 5))" env \
    SEROTINE_TRACE_LOG="$log" \
    SEROTINE_LINE_RUN_ID="$runid" \
    SEROTINE_LINE_JOB_LABEL="$label" \
    SEROTINE_LINE_HOST="$host" \
    SEROTINE_LINE_STATUS="$status" \
    SEROTINE_LINE_EXIT_CODE="$exit_code" \
    python3 "$helper" build-body 2>/dev/null)" || return 0
  [[ -n "$body" ]] || return 0

  # The run id's single '/' is legal unencoded in a query value (RFC 3986) and
  # is exactly the form serotine's README curl recipe uses.
  code="$(printf '%s' "$body" | "$_LK_TELEMETRY_TIMEOUT_FN" "$((timeout + 5))" curl -sS \
      -m "$timeout" --connect-timeout 5 \
      -o /dev/null -w '%{http_code}' -X POST \
      -H "Authorization: Bearer $token" \
      -H 'Content-Type: application/x-ndjson' \
      --data-binary @- "$url?run_id=$runid" 2>/dev/null)" || code="000"

  # One line in the run log so an operator enabling a cohort can see what
  # happened without turning on anything else. Only ever reached when armed.
  if [[ -n "${LOG:-}" ]]; then
    printf '[launchd-kit] trace: %s bytes -> %s (http %s)\n' \
      "${#body}" "$url" "${code:-000}" >>"$LOG" 2>/dev/null || true
  fi
  return 0
}
