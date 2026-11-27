#!/usr/bin/env bash
# Notification and crash-alert integration shared by launchd-kit and job-host.

if [[ "${_LK_NOTIFY_SH_LOADED:-0}" == "1" ]]; then
  return 0
fi

# Dependencies are supplied by launchd-kit/lib.sh or job-host/host.sh. The
# function-name variables let the host use its native API without defining
# additional compatibility names.
_LK_NOTIFY_TIMEOUT_FN="${_LK_NOTIFY_TIMEOUT_FN:-_lk_timeout}"
_LK_NOTIFY_ON_EXIT_FN="${_LK_NOTIFY_ON_EXIT_FN:-lk_on_exit}"
if ! declare -F "$_LK_NOTIFY_TIMEOUT_FN" >/dev/null \
    || ! declare -F "$_LK_NOTIFY_ON_EXIT_FN" >/dev/null; then
  echo "notify.sh: requires timeout and exit-handler functions" >&2
  return 1
fi
_LK_NOTIFY_SH_LOADED=1

# --- shared color semantics (launchd-kit/discord-notify.md) -------------
LK_COLOR_GREEN=5763719      # success / all-clear
LK_COLOR_YELLOW=16705372    # warning / attention
LK_COLOR_RED=15548997       # failure / error
LK_COLOR_BLURPLE=5793266    # neutral / informational (default)

# --- lk_notify: post a Discord embed (never breaks the job) ------------------
# Body on stdin; delegates to the shell discord-notify.sh (webhook resolution,
# truncation, secrets hygiene all live there). Always returns 0 — a notify is a
# side effect and must never fail the caller (discord-notify.md).
# NOTIFY_SCRIPT names one readable regular shell script, run through bash;
# executable permission is not required.
#
# The notifier call is HARD-BOUNDED through _lk_timeout. "Never fails the
# caller" is only half a contract if the call can never RETURN: a notifier
# wedged on a Keychain prompt, a stalled 1Password lookup or a black-holed
# webhook socket parks the wrapper on it forever — which IS the broken job this
# function exists to promise against, just arriving as a hang instead of a
# non-zero exit. NOTIFY_SCRIPT is caller-overridable, so lk_notify can never
# assume the notifier bounds itself; the guard belongs HERE, at the call.
#
# The bound is deliberately GENEROUS (default 300s), not tight. The stock
# discord-notify.sh is already internally bounded, but its worst LEGITIMATE
# path is long — and longer than it first looks, because the Connect leg is
# three HTTP round trips, not one:
#   op-resolve.py Connect   resolve_ref does 3 lookups (vaults -> items ->
#                           item GET), each retried once at HTTP_TIMEOUT=10s
#                                                            3 x 2 x 10s = 60s
#   legacy `op read`        2 attempts x 90s + a 2s backoff            = 182s
#   curl                    --connect-timeout 10 --max-time 20         =  20s
#                                                                      ~= 262s
# (~272s counting _lk_timeout's two 5s --kill-after graces.) A cold launchd
# context routinely spends 30-60s in its first op call alone. A bound set below
# that ceiling would not prevent a wedge — it would silently DROP real
# notifications, the more expensive failure of the two, and one this fleet has
# already taken: a 15s op bound spuriously failed the weekly sweeps' posts
# (2026-07-04, see discord-notify.sh).
#
# 300s clears that ceiling while staying under the shortest tick in the fleet
# (morning-brief polls every 10 min), so a wedged notifier releases before the
# next tick — and lk_single_lock's 120-min stale window covers any overlap
# regardless. But the HEADROOM IS THIN: ~38s. If any leg above grows (another
# Connect lookup, a longer op retry, a slower webhook), RAISE THIS FIRST — an
# under-set bound does not fail loudly, it quietly eats real notifications.
# Override per-wrapper with LK_NOTIFY_TIMEOUT_SECS.
#
# A timeout means the post is GONE, so it gets its OWN log line and is never
# folded into the generic "delivery failed" message: when reading a run log,
# a notification that was killed unsent must be tellable apart from one the
# webhook actively rejected.
lk_notify() {
  local title="${1:-$TOOL_NAME}"
  local color="${2:-$LK_COLOR_BLURPLE}"
  local notify="${NOTIFY_SCRIPT:-$HOME/.claude/scripts/discord-notify.sh}"
  local secs="${LK_NOTIFY_TIMEOUT_SECS:-300}"
  local body rc=0 started
  body="$(cat)"

  if [[ ! -f "$notify" || ! -r "$notify" ]]; then
    echo "lk_notify: notify script not a readable regular file ($notify) — skipping post" >&2
    return 0
  fi

  # The body stays on STDIN — that is the notifier's interface, and _lk_timeout
  # preserves it on every tier (1/2 exec the child from GNU timeout, 3 forks and
  # execs from perl, 4 backgrounds under `set -m` so job control is active and
  # bash does NOT redirect the async child's stdin from /dev/null). Do not
  # "simplify" this into an argument or a temp file.
  #
  # Timed from AFTER the body is read, so a slow producer upstream of us is not
  # charged against the notifier's bound.
  started=$SECONDS
  printf '%s' "$body" | "$_LK_NOTIFY_TIMEOUT_FN" "$secs" bash "$notify" "$title" "$color" || rc=$?

  if (( (rc == 124 || rc == 137) && SECONDS - started >= secs )); then
    # 124 = the SIGTERM at the bound did it; 137 = 128+SIGKILL, i.e. the child
    # swallowed the TERM and the unignorable follow-up is what ended it.
    #
    # The ELAPSED CHECK is not redundant. Neither code is unique to a timeout:
    # 137 is "died on SIGKILL" for ANY reason — the OOM killer, an operator's
    # `kill -9`, a notifier that kills itself — and 124 is a perfectly legal
    # exit status for a program to choose. Claiming "exceeded ${secs}s" for a
    # notifier that died in under a second would make this line lie in exactly
    # the situation it exists to clarify, sending whoever reads the run log
    # after a dropped post off to hunt a timeout that never happened. Only
    # report a timeout when the clock actually corroborates one.
    echo "lk_notify: notifier exceeded ${secs}s and was killed — NO post delivered (swallowed)" >&2
  elif (( rc != 0 )); then
    echo "lk_notify: delivery failed (swallowed) — see local log" >&2
  fi
  return 0                            # UNCONDITIONAL: a notify never fails the job
}

# --- lk_notify_strict: post a Discord embed, PROPAGATE the delivery status -----
# Same interface as lk_notify (title, color, body on stdin) but returns the
# notifier's exit status instead of swallowing it. Use this for 2xx-gated marker
# patterns — where a state marker must be written ONLY after Discord accepts the
# post (e.g. macrovoices-fetch's done-week / miss-week markers). A missing or
# unreadable or non-regular notifier is treated as a delivery failure (non-zero) here,
# because the caller is relying on delivery to have happened.
#
# Do NOT use this in a crash-alert or other must-never-fail path — that contract
# belongs to lk_notify. This one is deliberately allowed to fail the caller.
lk_notify_strict() {
  local title="${1:-$TOOL_NAME}"
  local color="${2:-$LK_COLOR_BLURPLE}"
  local notify="${NOTIFY_SCRIPT:-$HOME/.claude/scripts/discord-notify.sh}"
  local body
  body="$(cat)"

  if [[ ! -f "$notify" || ! -r "$notify" ]]; then
    echo "lk_notify_strict: notify script not a readable regular file ($notify) — treating as delivery failure" >&2
    return 1
  fi
  printf '%s' "$body" | bash "$notify" "$title" "$color"
}

# --- lk_enable_crash_alert: red Discord ping on non-zero exit ----------------
# Install after launchd_kit_init. On any non-zero exit of the script it fires a
# RED notify with the tail of the run log, so a silent crash is visible in
# Discord instead of dying quietly. Success (exit 0) is silent.
lk_enable_crash_alert() {
  "$_LK_NOTIFY_ON_EXIT_FN" _lk_crash_alert
}

_lk_crash_alert() {
  (( LK_EXIT_CODE == 0 )) && return 0             # success is silent
  printf 'Scheduled %s did NOT complete cleanly (exit %s).\nHost: %s\nLog: %s\n\nLast 25 log lines:\n```\n%s\n```\n' \
    "$TOOL_NAME" "$LK_EXIT_CODE" "$(hostname)" "${LOG:-<no log>}" \
    "$(tail -n 25 "${LOG:-/dev/null}" 2>/dev/null)" \
    | lk_notify "${TOOL_NAME} — run failed" "$LK_COLOR_RED" || true
}
