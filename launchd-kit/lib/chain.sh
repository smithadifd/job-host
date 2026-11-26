#!/usr/bin/env bash
# Static fallback-chain integration shared by launchd-kit and job-host.

if [[ "${_LK_CHAIN_SH_LOADED:-0}" == "1" ]]; then
  return 0
fi

# Dependency supplied by launchd-kit/lib.sh or job-host/host.sh.
_LK_CHAIN_TIMEOUT_FN="${_LK_CHAIN_TIMEOUT_FN:-_lk_timeout}"
if ! declare -F "$_LK_CHAIN_TIMEOUT_FN" >/dev/null; then
  echo "chain.sh: requires a timeout function" >&2
  return 1
fi
_LK_CHAIN_SH_LOADED=1

# --- lk_run_chain: static fallback-on-hard-failure chain ---------------------
# A caller-defined ORDERED list of pinned engine
# invocations, tried in sequence, advancing to the next ONLY on a HARD
# failure of the current one — never on output quality or content (this
# function has no opinion on what a link said, only on whether it said
# enough to exist). Per foundry standards/model-routing.md:296's carve-out,
# this is a fixed, committed policy artifact (the chain and its order ARE
# the policy, edited by commit), not a router or a quality-based selector.
#
# Usage:  lk_run_chain <name1> <cmd1> [<name2> <cmd2> ...]
#
#   <nameN>   a short id for the link (e.g. "claude", "codex") — used only in
#             the LK_CHAIN_* result vars below and in log/notify text; never
#             interpreted.
#   <cmdN>    ONE shell command line, run as `bash -c "<cmdN>"` — exactly the
#             shape of an engine.sh case-arm. Bash 3.2 has no nested arrays
#             and no namerefs, so a single self-contained command string per
#             link (not an argv array) is this file's existing convention for
#             passing "the actual command to run" across a function boundary.
#             THE CALLER'S CONTRACT: bake any variable a link needs (a prompt
#             file path, a model id) into the string at BUILD time — a plain
#             double-quoted shell string interpolates them before lk_run_chain
#             ever sees the result, so no export-for-a-subshell dance is
#             needed. A link's own stdin redirection (e.g. `<"$PROMPT"`)
#             belongs INSIDE the string for the same reason. A link's stderr
#             is NOT captured — it passes straight through to lk_run_chain's
#             own stderr (the caller's usual `2>>"$LOG"` redirection at the
#             call site works exactly as it does for a bare command).
#
# HARD failure (and only these three — see the file header's carve-out
# language above) advances to the next link:
#   * non-zero exit
#   * exit 0 but stdout is under LK_CHAIN_MIN_BYTES (default 200) bytes
#   * a timeout, bounded by LK_CHAIN_TIMEOUT_SECS (default 240s) via the
#     shared _lk_timeout guard (so a wedged link cannot hang the wrapper any
#     more than any other _lk_timeout-guarded call in this file can). A
#     124/137 exit is corroborated against ELAPSED TIME (the same pattern
#     lk_notify already uses) before it is labeled "timeout" — an engine
#     that happens to exit 124/137 on its OWN, well under the bound, is
#     reported as an ordinary exit=124/137, never mislabeled.
#
# ERREXIT-SAFE regardless of the caller's own `set -e`/`errexit` state or
# how lk_run_chain itself is invoked (wrapped in `if` or not): every
# command whose non-zero status this function needs to inspect is captured
# via `if cmd; then rc=0; else rc=$?; fi`, never a bare command left to
# propagate through `set -e` on its own. A prior version of this function
# called `_lk_timeout` bare and relied on the CALLER already wrapping the
# whole `lk_run_chain` invocation in an `if` — bash's errexit-suspension for
# an `if`-tested function does not reliably reach every nested command in
# every bash version/context, so link 1's hard failure could abort the
# whole calling shell before link 2 ever ran. Fixed.
#
# PROCESS-GROUP CLEANUP. Each link runs in ITS OWN process group, and
# lk_run_chain sweeps (TERM, then KILL after a short grace) any remaining
# member of that group immediately after the link resolves — REGARDLESS of
# whether it hard-failed, timed out, or served the run. This matters
# because only a genuine _lk_timeout-triggered kill cleans up a link's
# process group on its own; an ORDINARY non-zero exit does not — a link
# that backgrounds something and then exits 1 without waiting for it would
# otherwise leave that something running into the NEXT link's attempt and
# beyond lk_run_chain's own return. The group boundary is established at
# the INNERMOST point (immediately around the link's own command, inside
# whichever `_lk_timeout` tier ends up running it) and recorded to a
# scratch pid file lk_run_chain reads back — necessary because the four
# `_lk_timeout` tiers place the monitored command in DIFFERENT groups
# relative to themselves (GNU timeout/gtimeout leave it in their OWN group;
# the perl and bash-native tiers each `setpgid`/job-control it into a FRESH
# one) — an outer-only wrapper would only be correct for some tiers. A
# failed pid-file mktemp degrades to no sweep for that call (best-effort;
# the chain's own link-selection logic is unaffected either way).
#
# CALLER CONTRACT — redirect, never command-substitute. Capture the winning
# link's output with plain redirection at the call site (`lk_run_chain ... \
# >"$RAW"`), exactly like a bare command. Do NOT wrap the call in `$(...)`:
# command substitution runs lk_run_chain in a SUBSHELL, so every LK_CHAIN_*
# assignment below would vanish the instant the call returns — the same
# family of pitfall LK_EXIT_CODE's exit-registry design (top of this file)
# exists to avoid for trap handlers. Redirection creates no subshell, so the
# result vars land in the caller's own shell as documented.
#
# On success (some link produced a real answer): prints that link's stdout
# on lk_run_chain's own stdout (byte-for-byte — captured via a temp FILE and
# `cat`, deliberately not `$(...)` command substitution internally either,
# which would strip trailing newlines and cannot carry a NUL byte) and sets,
# for the caller to read immediately after the call (these are NOT local —
# they are the function's return channel, same pattern as LK_EXIT_CODE above):
#   LK_CHAIN_SERVED_NAME    the <nameN> of the link that served
#   LK_CHAIN_SERVED_INDEX   its 1-based position in the list
#   LK_CHAIN_FELL_BACK      "1" if a later link served (index > 1), else "0"
#   LK_CHAIN_FAILURES       "name:reason" per link tried before the served
#                           one, semicolon-joined (empty when link 1 served);
#                           reason is one of exit=<N> / empty-output(<n>b) /
#                           timeout
# Returns 0.
#
# On TOTAL failure (every link hard-failed): prints nothing on stdout,
# LK_CHAIN_SERVED_NAME is empty, LK_CHAIN_FAILURES names every link's
# reason, and this function returns 1 — an ordinary non-zero return. It
# does NOT itself notify or log a crash: the caller's existing failure path
# (lk_on_exit's crash alert, or its own `if ! lk_run_chain ...; then ...`)
# handles a chain's total failure exactly like any other hard failure of a
# single-engine call, unchanged.
lk_run_chain() {
  LK_CHAIN_SERVED_NAME=""
  LK_CHAIN_SERVED_INDEX=""
  LK_CHAIN_FELL_BACK="0"
  LK_CHAIN_FAILURES=""

  if [[ $# -lt 2 ]]; then
    echo "lk_run_chain: need at least one <name> <cmd> pair" >&2
    return 2
  fi
  if (( $# % 2 != 0 )); then
    echo "lk_run_chain: expected pairs of <name> <cmd> — got an odd argument count ($#)" >&2
    return 2
  fi

  local min_bytes="${LK_CHAIN_MIN_BYTES:-200}"
  local secs="${LK_CHAIN_TIMEOUT_SECS:-240}"
  local out_tmp pid_file
  out_tmp="$(mktemp "${TMPDIR:-/tmp}/lk_run_chain.out.XXXXXX" 2>/dev/null)" || {
    echo "lk_run_chain: mktemp failed — cannot capture link output" >&2
    return 2
  }
  # Best-effort: a failure here degrades to "no process-group sweep for this
  # call" (pid_file left empty), never a hard failure of the whole function —
  # see the doc comment above.
  pid_file="$(mktemp "${TMPDIR:-/tmp}/lk_run_chain.pid.XXXXXX" 2>/dev/null)" || pid_file=""

  local idx=0 name cmd rc bytes reason link_pgid started elapsed
  while [[ $# -gt 0 ]]; do
    idx=$((idx + 1))
    name="$1"; cmd="$2"; shift 2

    : >"$out_tmp"
    link_pgid=""
    started=$SECONDS

    if [[ -n "$pid_file" ]]; then
      : >"$pid_file"
      # The wrapper script establishes a FRESH process group at the
      # innermost point, immediately around the link's own command: `set -m`
      # (job control) + backgrounding makes the `eval`'d command's own PID
      # its process-group id, and that PID is recorded to $2 (pid_file)
      # before `wait` blocks on it — so lk_run_chain can find and sweep that
      # exact group afterward no matter which _lk_timeout tier ran it. $1 is
      # the caller's original command STRING, passed as a real argv element
      # (never string-concatenated) so it needs no re-escaping here; `eval`
      # re-parses and runs it exactly as `bash -c "$cmd"` would.
      if "$_LK_CHAIN_TIMEOUT_FN" "$secs" bash -c '
          set -m
          eval "$1" &
          printf "%s" "$!" >"$2"
          wait "$!"
        ' _ "$cmd" "$pid_file" >"$out_tmp"; then
        rc=0
      else
        rc=$?
      fi
      link_pgid="$(cat "$pid_file" 2>/dev/null)" || link_pgid=""
      [[ "$link_pgid" =~ ^[0-9]+$ ]] || link_pgid=""
    else
      if "$_LK_CHAIN_TIMEOUT_FN" "$secs" bash -c "$cmd" >"$out_tmp"; then
        rc=0
      else
        rc=$?
      fi
    fi
    elapsed=$((SECONDS - started))

    # Sweep the link's own process group UNCONDITIONALLY (success, hard
    # failure, or timeout alike) — see PROCESS-GROUP CLEANUP above. TERM
    # first, then KILL after a short grace for anything that ignored it;
    # both are no-ops (silently) if the group is already empty.
    if [[ -n "$link_pgid" ]]; then
      # `|| true` on BOTH: `kill` returns non-zero (ESRCH) the instant the
      # group is already empty — the COMMON case, not the exception — and a
      # bare non-zero here would abort the whole function under a CALLER's
      # `set -e` on every ordinary link, not just the orphan-cleanup case
      # this sweep exists for.
      kill -TERM -- "-$link_pgid" 2>/dev/null || true
      sleep 0.3
      kill -KILL -- "-$link_pgid" 2>/dev/null || true
    fi

    bytes="$(wc -c <"$out_tmp" 2>/dev/null | tr -d ' ')" || true
    [[ "$bytes" =~ ^[0-9]+$ ]] || bytes=0

    if (( (rc == 124 || rc == 137) && elapsed >= secs )); then
      reason="timeout"
    elif (( rc != 0 )); then
      reason="exit=$rc"
    elif (( bytes < min_bytes )); then
      reason="empty-output(${bytes}b)"
    else
      reason=""
    fi

    if [[ -z "$reason" ]]; then
      LK_CHAIN_SERVED_NAME="$name"
      LK_CHAIN_SERVED_INDEX="$idx"
      (( idx > 1 )) && LK_CHAIN_FELL_BACK="1"
      cat "$out_tmp"
      rm -f "$out_tmp" "$pid_file"
      return 0
    fi

    LK_CHAIN_FAILURES="${LK_CHAIN_FAILURES:+$LK_CHAIN_FAILURES;}${name}:${reason}"
  done

  rm -f "$out_tmp" "$pid_file"
  return 1
}
