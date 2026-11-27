#!/usr/bin/env bash
# Generic Discord webhook notifier for $HOME/.claude automations.
#
# Usage:   bash discord-notify.sh [--content "<text>"] [--no-mentions] [--channel <name>] "Title" [color-decimal] < body-on-stdin
# Dry-run: DISCORD_NOTIFY_DRY_RUN=1 renders the redacted payload to stderr
#          without reading secrets or calling 1Password or curl.
#
# Example: echo "all clear" | bash discord-notify.sh "Scheduled job" 3066993
#
# Title cap: Discord embed titles are capped at 256 chars — a caller (e.g. a
# director brief) that stuffs a long single arg into TITLE gets a 400 from
# the webhook instead of a truncated post; keep TITLE short and put detail
# in the stdin body, which IS truncated rather than rejected (see below).
#
# --content "<text>" (optional, additive — every pre-existing call shape is
# byte-identical without it):
#   Adds a top-level plain-text `content` field to the payload. This is the ONLY
#   field Discord parses mentions in — an embed NEVER pings anyone, so a
#   `<@123456789012345678>` inside an embed title/description renders as inert
#   text. Any caller that needs to actually ping a person must use --content.
#   Capped at 1900 chars (Discord's hard limit is 2000); overflow is TRUNCATED
#   with a marker, never rejected. `allowed_mentions` is pinned to `users`, so
#   --content can ping a person but can never fire @everyone/@here or a role
#   ping out of interpolated text.
#   When --content is given the stdin body is optional: with no body the payload
#   is content-only (no embed).
# --no-mentions disables all mention parsing for content intended only to inform.
# --channel <name> picks the notify.json channel. Without it the channel is
#   $NOTIFY_CHANNEL when set and non-empty, else "default". The environment
#   form lets a wrapper that posts through lk_notify (which takes no flags)
#   choose its channel: export NOTIFY_CHANNEL before calling it.
#
# Webhook resolution order (no configuration file is ever sourced):
#   1. $DISCORD_WEBHOOK_URL if already exported
#   2. channels.<name>.webhook in notify.json, the one fixed configuration file
#      ($XDG_CONFIG_HOME/claude-toolkit/notify.json when XDG_CONFIG_HOME is
#      absolute, else $HOME/.config/claude-toolkit/notify.json; schema in
#      scripts/notify-sink.md). <name> is --channel, else $NOTIFY_CHANNEL,
#      else "default"; the flag wins. An invalid file or a missing channel
#      exits 2.
#   3. Only while notify.json is absent, for one release: DISCORD_WEBHOOK_URL
#      read as data from $WEBHOOK_ENV_FILE, default
#      $HOME/.claude/secrets/dependabot-sweep.env, with a deprecation line on stderr.
#
# 1Password (op://) support:
#   If the resolved DISCORD_WEBHOOK_URL is an `op://vault/item/field` reference
#   (typical of a 1Password-migrated env file) rather than a literal URL, it is
#   resolved through Connect first; OP_CONNECT_HOST must be non-empty. Without
#   it, the notifier refuses before calling either resolver. After a Connect
#   failure, legacy `op read` uses $OP_SERVICE_ACCOUNT_TOKEN if set, otherwise
#   the macOS Keychain item `op-service-account-token`. A plaintext URL is
#   unchanged — the op path engages ONLY when the value starts with `op://`.
#
# This is the canonical, version-controlled copy of the machine-local notifier
# at $HOME/.claude/scripts/discord-notify.sh. The contract is in
# discord-notify.md beside this script.
set -euo pipefail

# --- argument parsing -------------------------------------------------------
# Strictly additive: the two positionals (TITLE, COLOR) keep their meaning and
# their order, so every existing caller is unaffected. --content and
# --no-mentions may appear anywhere in the argument list; `--` ends flag parsing.
CONTENT=""
ALLOW_MENTIONS=1
CHANNEL="${NOTIFY_CHANNEL:-default}"
_dn_positional=()
while (( $# )); do
  case "$1" in
    --channel)
      [[ $# -ge 2 ]] || { echo "discord-notify: channel_value_missing: --channel requires a value" >&2; exit 2; }
      CHANNEL="$2"; shift 2 ;;
    --channel=*)
      CHANNEL="${1#--channel=}"; shift ;;
    --content)
      [[ $# -ge 2 ]] || { echo "discord-notify: --content requires a value" >&2; exit 2; }
      CONTENT="$2"; shift 2 ;;
    --content=*)
      CONTENT="${1#--content=}"; shift ;;
    --no-mentions)
      ALLOW_MENTIONS=0; shift ;;
    --)
      shift; while (( $# )); do _dn_positional+=("$1"); shift; done ;;
    *)
      _dn_positional+=("$1"); shift ;;
  esac
done
set -- ${_dn_positional[@]+"${_dn_positional[@]}"}
unset _dn_positional
if [[ ! "$CHANNEL" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
  echo "discord-notify: channel_name_invalid: --channel or NOTIFY_CHANNEL must be a channel name (letters, digits, '.', '_' or '-')" >&2
  exit 2
fi

TITLE="${1:-Notification}"
COLOR="${2:-5793266}"   # default blurple

_dn_render_payload() {
  # Read the embed body from stdin. Skipping the read when stdin is a TTY keeps a
  # `--content`-only, mention-style ping from hanging on a terminal — a piped or
  # redirected caller (i.e. every pre-existing one) is unaffected.
  BODY=""
  if [[ ! -t 0 ]]; then
    BODY="$(cat)"
  fi
  # Never send an empty payload: with no body AND no --content, preserve the
  # original "(no content)" embed.
  [[ -z "$BODY" && -z "$CONTENT" ]] && BODY="(no content)"

  # Discord embed description hard limit is 4096 chars.
  MAX=3900
  if (( ${#BODY} > MAX )); then
    BODY="${BODY:0:MAX}"$'\n…(truncated — see local log for full output)'
  fi

  # Discord's top-level `content` hard limit is 2000 chars; cap at 1900 to leave
  # headroom and to keep a ping short enough to read on a lock screen. Overflow
  # TRUNCATES rather than rejects — a shortened ping still reaches its human,
  # whereas a rejected one silently does not. Truncation is from the tail and the
  # marker is counted inside the cap, so the emitted content is always <= 1900;
  # callers put mentions at the START of the content, so a mention can never be
  # cut in half into a broken `<@…` fragment.
  #
  # Locale note — deliberate, do NOT "fix" this into a regression. Under launchd
  # and cron, LANG/LC_ALL are unset, so bash's ${var:off:len} counts BYTES, not
  # characters. An emoji straddling the cut therefore yields one U+FFFD, which
  # `jq --arg` absorbs into valid JSON (verified) — the message still sends, and
  # the cap becomes 1900 *bytes*, i.e. strictly under Discord's 2000-char limit.
  # Every failure direction here is the safe one. Wrapping the substring in
  # LC_ALL=C.UTF-8 would count characters correctly where that locale exists, but
  # on any host lacking it bash prints `warning: setlocale: LC_ALL: cannot change
  # locale` to stderr on EVERY invocation — and this notifier's whole design goal
  # is to be silent and safe under unattended launchd. Trading a guaranteed-safe
  # cosmetic artifact on the overflow path for possible log noise on every run is
  # a bad trade.
  MAX_CONTENT=1900
  if (( ${#CONTENT} > MAX_CONTENT )); then
    _dn_mark=$'\n…(truncated — see local log for full output)'
    CONTENT="${CONTENT:0:$(( MAX_CONTENT - ${#_dn_mark} ))}${_dn_mark}"
    unset _dn_mark
  fi

  if [[ -n "$BODY" ]]; then
    PAYLOAD="$(jq -n --arg t "$TITLE" --arg d "$BODY" --argjson c "$COLOR" \
      '{embeds:[{title:$t, description:$d, color:$c}]}')"
  else
    PAYLOAD='{}'
  fi
  if [[ -n "$CONTENT" ]]; then
    if (( ALLOW_MENTIONS )); then
      PAYLOAD="$(jq -n --argjson p "$PAYLOAD" --arg c "$CONTENT" \
        '$p + {content:$c, allowed_mentions:{parse:["users"]}}')"
    else
      PAYLOAD="$(jq -n --argjson p "$PAYLOAD" --arg c "$CONTENT" \
        '$p + {content:$c, allowed_mentions:{parse:[]}}')"
    fi
  fi
}

if [[ "${DISCORD_NOTIFY_DRY_RUN:-}" == 1 ]]; then
  _dn_render_payload
  printf 'discord-notify: dry-run webhook=<redacted> token=<redacted> payload=%s\n' "$PAYLOAD" >&2
  exit 0
fi

# >>> notify-config. Keep this block byte-identical in notify-owner.sh,
# discord-notify.sh and hands-on-digest-run.sh; a test compares the copies.
# Configuration is read as data and never sourced or executed. The one fixed
# file is notify.json in the toolkit configuration folder:
# $XDG_CONFIG_HOME/claude-toolkit/ when XDG_CONFIG_HOME is an absolute path,
# else $HOME/.config/claude-toolkit/ (the folder skills/_lib/overrides.py
# resolves). The schema is in scripts/notify-sink.md.
_nc_config_path() {
  local base="${XDG_CONFIG_HOME:-}"
  if [[ "$base" != /* ]]; then
    base="$HOME/.config"
  fi
  printf '%s/claude-toolkit/notify.json' "$base"
}

# jq validates the whole file, every channel included, then prints one record
# for the asked channel: OK, webhook, mention and desktop_fallback joined by the
# unit separator (0x1f), or ERR and a reason. A reason names keys, never a
# value. A malformed channel refuses the file even when another channel is
# asked for, so a broken entry never waits silently for its first caller.
_NC_JQ='
def chan($n): "channel " + ($n | @json);
def check_channel($n):
  if type != "object" then error(chan($n) + " must be an object") else . end
  | ((keys - ["webhook", "mention"]) | .[0]) as $cextra
  | if $cextra != null
    then error(chan($n) + " has an unknown key " + ($cextra | @json)) else . end
  | if has("webhook") | not then error(chan($n) + " has no webhook") else . end
  | if (.webhook | type) != "string"
    then error(chan($n) + " webhook must be an https:// URL or an op:// reference") else . end
  | if (.webhook | (test("\\Ahttps://[!-~]+\\z") or test("\\Aop://[ -~]+\\z")) | not)
    then error(chan($n) + " webhook must be an https:// URL or an op:// reference") else . end
  | if has("mention") and ((.mention | type) != "string")
    then error(chan($n) + " mention must be a numeric Discord user id in quotes") else . end
  | if has("mention") and ((.mention | test("\\A[0-9]{15,25}\\z")) | not)
    then error(chan($n) + " mention must be a numeric Discord user id in quotes") else . end;
try (
  if length != 1 then error("must hold exactly one JSON value") else .[0] end
  | if type != "object" then error("is not a JSON object") else . end
  | ((keys - ["channels", "desktop_fallback"]) | .[0]) as $extra
  | if $extra != null then error("has an unknown key " + ($extra | @json)) else . end
  | if has("desktop_fallback") and ((.desktop_fallback | type) != "boolean")
    then error("desktop_fallback must be true or false") else . end
  | if has("channels") and ((.channels | type) != "object")
    then error("channels must be an object") else . end
  | (if .desktop_fallback == true then "true" else "false" end) as $fallback
  | (.channels // {}) as $channels
  | ($channels | to_entries | map(.key as $n | .value | check_channel($n)) | length) as $checked
  | if ($channels | has($channel)) | not then
      "OK\u001f\u001f\u001f" + $fallback
    else
      $channels[$channel] as $c
      | "OK\u001f" + ($c | .webhook) + "\u001f" + ($c | .mention // "") + "\u001f" + $fallback
    end
) catch ("ERR\u001f" + (. | tostring))'

# _nc_read_channel <file> <channel>: validate the file and read one channel.
# Returns 0 with _NC_WEBHOOK and _NC_MENTION set (both empty when the channel
# is absent) and _NC_DESKTOP_FALLBACK set to true or false. Returns 2 with
# _NC_ERROR set to the reason when the file breaks the schema.
_nc_read_channel() {
  local record
  _NC_WEBHOOK="" _NC_MENTION="" _NC_DESKTOP_FALLBACK="" _NC_ERROR=""
  if [[ ! -f "$1" ]]; then
    _NC_ERROR="is not a regular file"
    return 2
  fi
  if [[ ! -r "$1" ]]; then
    _NC_ERROR="is not readable"
    return 2
  fi
  if ! command -v jq >/dev/null 2>&1; then
    _NC_ERROR="cannot be read: jq is not on PATH"
    return 2
  fi
  if ! record="$(jq -r -s --arg channel "$2" "$_NC_JQ" "$1" 2>/dev/null)"; then
    _NC_ERROR="is not valid JSON"
    return 2
  fi
  case "$record" in
    "OK"$'\x1f'*)
      IFS=$'\x1f' read -r _ _NC_WEBHOOK _NC_MENTION _NC_DESKTOP_FALLBACK <<<"$record"
      return 0
      ;;
    "ERR"$'\x1f'*)
      _NC_ERROR="${record#ERR$'\x1f'}"
      return 2
      ;;
  esac
  _NC_ERROR="could not be read"
  return 2
}

# _nc_read_legacy <file>: read a legacy env file as data, for one release
# while callers move to notify.json. Only two lines count:
#   [export ]DISCORD_WEBHOOK_URL=VALUE   and   [export ]OWNER_DISCORD_ID=VALUE
# (the old key ANDREW_DISCORD_ID still counts; OWNER_DISCORD_ID wins over it)
# VALUE is bare (no quotes, whitespace, $, backtick, backslash, ~ or shell
# operator), or wholly inside one pair of single quotes, or wholly inside one
# pair of double quotes holding no $, backtick or backslash; spaces and a
# # comment may follow. Every other line is ignored, nothing is executed or
# expanded, and the last matching line wins. Sets _NC_LEGACY_WEBHOOK,
# _NC_LEGACY_ID and _NC_LEGACY_ID_SET (1 when an owner id line counted).
_nc_read_legacy() {
  local line key value owner_seen=0
  local re_line='^[[:space:]]*(export[[:space:]]+)?(DISCORD_WEBHOOK_URL|OWNER_DISCORD_ID|ANDREW_DISCORD_ID)=(.*)$'
  local re_tail='([[:space:]]+#.*)?[[:space:]]*$'
  local re_double="^\"([^\"\$\`\\\\]*)\"$re_tail"
  local re_single="^'([^']*)'$re_tail"
  local re_bare="^([^\"'\$\`\\\\[:space:];&|<>()~]*)$re_tail"
  _NC_LEGACY_WEBHOOK="" _NC_LEGACY_ID="" _NC_LEGACY_ID_SET=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    if [[ ! "$line" =~ $re_line ]]; then
      continue
    fi
    key="${BASH_REMATCH[2]}"
    value="${BASH_REMATCH[3]}"
    if [[ "$value" =~ $re_double || "$value" =~ $re_single || "$value" =~ $re_bare ]]; then
      value="${BASH_REMATCH[1]}"
    else
      continue
    fi
    if [[ "$key" == DISCORD_WEBHOOK_URL ]]; then
      _NC_LEGACY_WEBHOOK="$value"
    elif [[ "$key" == OWNER_DISCORD_ID ]]; then
      _NC_LEGACY_ID="$value"
      _NC_LEGACY_ID_SET=1
      owner_seen=1
    elif (( ! owner_seen )); then
      _NC_LEGACY_ID="$value"
      _NC_LEGACY_ID_SET=1
    fi
  done <"$1"
  return 0
}

# _nc_legacy_notice <program> <legacy file>: the one deprecation line.
_nc_legacy_notice() {
  printf '%s: deprecated: read %s as data; move this configuration to %s (schema: scripts/notify-sink.md)\n' \
    "$1" "$2" "$(_nc_config_path)" >&2
}
# <<< notify-config

if [[ -z "${DISCORD_WEBHOOK_URL:-}" ]]; then
  _dn_config="$(_nc_config_path)"
  if [[ -e "$_dn_config" || -L "$_dn_config" ]]; then
    if ! _nc_read_channel "$_dn_config" "$CHANNEL"; then
      echo "discord-notify: notify_config_invalid: invalid notify configuration $_dn_config: $_NC_ERROR" >&2
      exit 2
    fi
    if [[ -z "$_NC_WEBHOOK" ]]; then
      echo "discord-notify: notify_channel_missing: notify configuration $_dn_config has no channel \"$CHANNEL\"" >&2
      exit 2
    fi
    DISCORD_WEBHOOK_URL="$_NC_WEBHOOK"
  else
    ENV_FILE="${WEBHOOK_ENV_FILE:-$HOME/.claude/secrets/dependabot-sweep.env}"
    if [[ -f "$ENV_FILE" ]]; then
      _nc_read_legacy "$ENV_FILE"
      _nc_legacy_notice discord-notify "$ENV_FILE"
      DISCORD_WEBHOOK_URL="$_NC_LEGACY_WEBHOOK"
    fi
  fi
  unset _dn_config
fi

# --- _lk_timeout: portable hard-timeout guard (shared with launchd-kit/lib.sh)
# The op:// resolution block below needs a bound on the `op` CLI call (it has
# no built-in timeout and can hang indefinitely under the macOS TCC app-data
# gate — see the comment ahead of the op read call). Rather than reimplement
# the timeout/gtimeout/perl-alarm fallback chain here, source it from the
# canonical implementation in lib.sh. Sourcing lib.sh is safe here: it defines
# functions + a handful of module-level globals (color constants, _LK_DIR from
# a cd/pwd, the empty exit-handler registry) but runs NOTHING external — no
# claude, git, curl, or Discord post — until its init/guard/notify helpers are
# called explicitly, none of which this notifier invokes. Only the fixed
# sibling path is read (this file's directory, launchd-kit/, the path wrappers
# source it from per launchd-kit/README.md). A standalone copy, such as the
# machine-local mirror, has no sibling lib.sh and uses the inline copy of the
# identical guard below, so an `op` call is NEVER left unbounded — keep this in
# sync with lib.sh's _lk_timeout if that implementation ever changes.
__LK_NOTIFY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$__LK_NOTIFY_DIR/lib.sh" ]]; then
  # shellcheck source=lib.sh
  source "$__LK_NOTIFY_DIR/lib.sh"
fi
unset __LK_NOTIFY_DIR

if ! declare -F _lk_timeout >/dev/null 2>&1; then
  # Mirror of lib.sh's _lk_timeout — keep in sync if that implementation
  # changes. Every tier follows SIGTERM with an unignorable SIGKILL after a
  # grace period so a TERM-ignoring child can't survive the bound (tiers 1/2
  # via GNU timeout's --kill-after, which also group-kills by default; tiers
  # 3/4 via a process-group TERM-then-KILL). A single-process, TERM-only kill
  # could leave `$(_lk_timeout ...)` blocked on pipe EOF if the timed-out
  # command forked a helper that inherited the stdout pipe — see lib.sh's
  # _lk_timeout doc comment for how this was found.
  _lk_timeout() {
    local secs="$1"; shift
    [[ $# -eq 0 ]] && { echo "_lk_timeout: no command given" >&2; return 2; }
    local kill_after="${LK_TIMEOUT_KILL_AFTER:-5s}"
    if command -v timeout >/dev/null 2>&1; then
      timeout --kill-after="$kill_after" "$secs" "$@"; return $?
    fi
    if command -v gtimeout >/dev/null 2>&1; then
      gtimeout --kill-after="$kill_after" "$secs" "$@"; return $?
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
    local _lk_had_monitor=1
    case $- in *m*) _lk_had_monitor=1 ;; *) _lk_had_monitor=0 ;; esac
    set -m
    "$@" &
    local cmd_pid=$! waited=0
    (( _lk_had_monitor )) || set +m
    while kill -0 "$cmd_pid" 2>/dev/null; do
      if (( waited >= secs )); then
        kill -TERM -- "-$cmd_pid" 2>/dev/null || kill -TERM "$cmd_pid" 2>/dev/null
        sleep 1
        kill -KILL -- "-$cmd_pid" 2>/dev/null || kill -KILL "$cmd_pid" 2>/dev/null
        wait "$cmd_pid" 2>/dev/null; return 124
      fi
      sleep 1; waited=$((waited + 1))
    done
    wait "$cmd_pid"
  }
fi

if [[ -z "${DISCORD_WEBHOOK_URL:-}" ]]; then
  echo "discord-notify: no DISCORD_WEBHOOK_URL configured" >&2
  exit 1
fi

# --- op:// resolution (1Password) -------------------------------------------
# Only ever engages when the value literally starts with op://, so a plaintext
# literal URL is byte-identical to the original contract. On any op failure we
# emit a DISTINCT stderr message (unmistakably different from a webhook/HTTP
# failure) and exit non-zero — a 1Password outage is its own failure mode.
if [[ "$DISCORD_WEBHOOK_URL" == op://* ]]; then
  __OCH="${OP_CONNECT_HOST:-}"
  if [[ -z "$__OCH" ]]; then
    echo "discord-notify: OP_CONNECT_HOST is required for op:// webhooks" >&2
    exit 1
  fi

  RESOLVED_URL=""
  __OPR_STDERR=""

  # ---- Preferred: pure-HTTP 1Password Connect resolver (op-resolve.py) -------
  # No `op` binary is spawned, so nothing probes the 1Password desktop-app
  # container and the macOS "access data from other apps" TCC gate never fires
  # (that gate — attributed to the calling bash — is what hung/killed this
  # notifier and silently dropped posts).
  # The caller supplies the Connect host; this notifier has no default address.
  # The shared resolver lookup order (launchd-kit/README.md § Where consumers
  # find op-resolve.py) stops at the machine-local mirror here, on purpose:
  # this notifier runs under launchd from paths that are not a checkout, and
  # its own last resort is the legacy `op read` fallback below, not a repo
  # copy. install-op-resolve.sh is what keeps the mirror's bytes canonical.
  __OPR="${OP_RESOLVE_PY:-$HOME/.claude/scripts/op-resolve.py}"
  if [[ -f "$__OPR" ]] && command -v python3 >/dev/null 2>&1; then
    # Capture Connect-resolver stderr instead of discarding it. Emit it only
    # when the legacy `op read` fallback ALSO fails — a successful fallback
    # must stay byte-identical to the pre-capture contract (2>/dev/null).
    __OPR_ERRFILE="${TMPDIR:-/tmp}/discord-notify.op-resolve.err.$$"
    # shellcheck disable=SC2016  # $U expands in the deliberate child shell.
    RESOLVED_URL="$(OP_CONNECT_HOST="$__OCH" python3 "$__OPR" \
      --env-file <(printf 'U=%s\n' "$DISCORD_WEBHOOK_URL") -- sh -c 'printf %s "$U"' 2>"$__OPR_ERRFILE")" \
      || RESOLVED_URL=""
    __OPR_STDERR="$(cat "$__OPR_ERRFILE" 2>/dev/null || true)"
    rm -f "$__OPR_ERRFILE"
    [[ -n "$RESOLVED_URL" ]] && echo "discord-notify: webhook resolved via Connect (no op binary)" >&2
  fi

  # ---- Fallback: legacy `op read` (can trip the TCC gate; kept as a safety net)
  if [[ -z "$RESOLVED_URL" ]]; then
  if ! command -v op >/dev/null 2>&1; then
    echo "discord-notify: 1Password resolution failed (op) — 'op' binary not found on PATH" >&2
    exit 1
  fi

  if [[ -z "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]]; then
    if ! OP_SERVICE_ACCOUNT_TOKEN="$(security find-generic-password -s op-service-account-token -w 2>/dev/null)"; then
      echo "discord-notify: 1Password resolution failed (op) — no OP_SERVICE_ACCOUNT_TOKEN in env or Keychain item 'op-service-account-token'" >&2
      exit 1
    fi
    export OP_SERVICE_ACCOUNT_TOKEN
  fi

  # Force op into pure service-account mode for this headless read. Two separate
  # macOS TCC triggers otherwise fire the "<proc> would like to access data from
  # other apps" prompt (op touching the 1Password desktop-app group container),
  # which an UNATTENDED launchd run can't consent to → auto-denied → op fails:
  #   OP_CACHE=false                    → no local caching daemon (one probe source)
  #   OP_BIOMETRIC_UNLOCK_ENABLED=false → no desktop-app integration probe (the other)
  # Both are per-process env only; the 1Password app's global "Integrate with
  # 1Password CLI" toggle stays ON, so interactive biometric unlock is unaffected.
  # A service account authenticates online and needs neither. (2026-07-05 — refs:
  # 1Password docs on service accounts, about-biometric-unlock, CLI OP_CACHE.)
  export OP_CACHE=false
  export OP_BIOMETRIC_UNLOCK_ENABLED=false

  # Bound the op read: a service-account token authenticates online, so a
  # stalled network call to 1Password would otherwise hang the notifier — and
  # the launcher parked on it — indefinitely (op read has no built-in timeout;
  # observed as a wedged post 2026-07-03). Routed through _lk_timeout (see
  # above), whose fallback chain (timeout -> gtimeout -> perl alarm ->
  # background-kill) guarantees a bound even on a stock Mac with neither GNU
  # timeout nor homebrew coreutils on PATH — this call is NEVER unbounded.
  #
  # The bound is 90s, NOT a few seconds: the FIRST op call from a COLD launchd
  # context (op-daemon / Keychain / network cold start) routinely takes 30-60s,
  # so a tight 15s bound spuriously failed jobs whose ONLY op call is this
  # notifier read — jobs that never run `op run` do not pre-warm
  # the daemon. Their posts timed out at 15s while jobs that had already
  # called `op run` posted successfully.
  # We also retry once: the first attempt warms the daemon, so a retry
  # after a cold-start timeout resolves in <1s. Worst case on a real outage is
  # ~2x90s before failing — the launcher's EXIT-trap frees the lock either way.
  # OP_READ_TIMEOUT_SECS overrides the 90s bound — for tests only (proving the
  # guard fires without a real 90s wait); never set it in production.
  RESOLVED_URL=""
  for _op_attempt in 1 2; do
    if RESOLVED_URL="$(_lk_timeout "${OP_READ_TIMEOUT_SECS:-90}" op read "$DISCORD_WEBHOOK_URL" 2>/dev/null)" && [[ -n "$RESOLVED_URL" ]]; then
      break
    fi
    RESOLVED_URL=""
    if [[ "$_op_attempt" == 1 ]]; then sleep 2; fi
  done
    if [[ -z "$RESOLVED_URL" ]]; then
      echo "discord-notify: 1Password resolution failed (op) — 'op read' could not resolve the webhook reference (or timed out after 2 attempts)" >&2
      if [[ -n "$__OPR_STDERR" ]]; then
        # Attribute Connect diagnostics only on this both-paths-failed branch.
        # Truncate and scrub webhook-shaped URLs so a resolver bug cannot leak
        # the secret this notifier exists to hide.
        printf '%s\n' "${__OPR_STDERR:0:4096}" | sed -E \
          's#https://discord.com/api/webhooks/[^[:space:]]*#[redacted-webhook]#g' \
          | sed 's/^/discord-notify: op-resolve: /' >&2
      fi
      exit 1
    fi
  fi

  if [[ -z "$RESOLVED_URL" ]]; then
    echo "discord-notify: 1Password resolution failed — Connect (op-resolve.py) and legacy 'op read' both failed" >&2
    exit 1
  fi
  DISCORD_WEBHOOK_URL="$RESOLVED_URL"
fi

_dn_render_payload

if ! __DN_RESPONSE_FILE="$(mktemp "${TMPDIR:-/tmp}/discord-notify.XXXXXX" 2>/dev/null)"; then
  __DN_RESPONSE_FILE="$(mktemp "/tmp/discord-notify.XXXXXX")"
fi
trap 'rm -f "$__DN_RESPONSE_FILE"' EXIT

HTTP_CODE="$(curl -sS --connect-timeout 10 --max-time 20 \
  -o "$__DN_RESPONSE_FILE" -w '%{http_code}' \
  -H 'Content-Type: application/json' \
  -X POST "$DISCORD_WEBHOOK_URL" \
  -d "$PAYLOAD")"

if [[ $HTTP_CODE != 2* ]]; then
  echo "discord-notify: HTTP $HTTP_CODE" >&2
  cat "$__DN_RESPONSE_FILE" >&2
  exit 1
fi
echo "discord-notify: ok ($HTTP_CODE)"
