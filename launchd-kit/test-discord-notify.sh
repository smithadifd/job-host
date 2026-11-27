#!/usr/bin/env bash
# Offline smoke test for launchd-kit/discord-notify.sh.
#
# Stubs `curl`, `op`, and `security` on PATH so nothing hits the network or a
# real 1Password. Proves:
#   (a) a plaintext literal URL calls curl unchanged (byte-identical path),
#   (b) an op:// value triggers `op read` (via the Keychain token fallback)
#       and curl uses the op-resolved URL,
#   (c) an `op` failure produces the DISTINCT stderr message and non-zero exit
#       BEFORE any HTTP request,
#   (d) the webhook URL / resolved secret never appears on stdout.
#   (k) both Connect and `op read` fail → resolver stderr is attributed
#       alongside the fallback error,
#   (l) Connect fails and `op read` succeeds → stdout/stderr are byte-identical
#       to the pre-fix contract,
#   (n) Connect succeeds → stdout/stderr unchanged.
#   (p) an unset or empty Connect host refuses before either resolver runs.
#
# Usage:  bash launchd-kit/test-discord-notify.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/discord-notify.sh"

SB="$(mktemp -d)"
trap 'rm -rf "$SB"' EXIT
STUBS="$SB/stubs"; mkdir -p "$STUBS"

cat > "$STUBS/curl" <<EOF
#!/usr/bin/env bash
echo "CURL_CALLED_WITH_ARGS: \$*" >> "$SB/curl.log"
# Capture the JSON body (the value following -d) so payload-shape assertions
# don't have to parse it back out of a flattened, possibly multi-line arg log.
_prev=""
_out=""
for _a in "\$@"; do
  if [[ "\$_prev" == "-d" ]]; then printf '%s' "\$_a" > "$SB/payload.json"; fi
  if [[ "\$_prev" == "-o" ]]; then _out="\$_a"; fi
  _prev="\$_a"
done
if [[ "\$_out" == "/tmp/discord-notify.out" ]]; then
  touch "$SB/legacy-response-targeted"
  _out="$SB/legacy-response.out"
fi
: > "\$_out"
if [[ -n "\${CURL_STUB_BODY:-}" ]]; then
  printf '%s\n' "\$CURL_STUB_BODY" > "\$_out"
fi
if [[ -n "\${CURL_STUB_SYNC_DIR:-}" ]]; then
  touch "\$CURL_STUB_SYNC_DIR/\$CURL_STUB_ID.ready"
  _waited=0
  while [[ ! -f "\$CURL_STUB_SYNC_DIR/\$CURL_STUB_PEER.ready" ]]; do
    if (( _waited >= 100 )); then
      echo "curl stub: peer wait timed out" >&2
      exit 97
    fi
    sleep 0.01
    _waited=\$((_waited + 1))
  done
fi
printf '%s' "\${CURL_STUB_HTTP:-204}"
EOF
cat > "$STUBS/op" <<EOF
#!/usr/bin/env bash
echo "OP_CALLED: \$*" >> "$SB/op.log"
if [[ "\${OP_STUB_FAIL:-0}" == 1 ]]; then echo "op stub: forced failure" >&2; exit 1; fi
if [[ "\${OP_STUB_FAIL_ONCE:-0}" == 1 ]]; then
  n=\$(cat "$SB/op.count" 2>/dev/null || echo 0); n=\$((n + 1)); echo "\$n" > "$SB/op.count"
  if [[ \$n -eq 1 ]]; then echo "op stub: forced first-call failure" >&2; exit 1; fi
fi
if [[ "\${OP_STUB_HANG:-0}" == 1 ]]; then
  echo "op stub: hanging (simulates the TCC app-data gate)" >&2
  sleep 60
  exit 0
fi
echo "https://discord.com/api/webhooks/RESOLVED/FROM_OP"
EOF
cat > "$STUBS/security" <<EOF
#!/usr/bin/env bash
echo "SECURITY_CALLED: \$*" >> "$SB/security.log"
echo "keychain-token-value"
EOF
cat > "$STUBS/op-resolve.py" <<EOF
#!/usr/bin/env python3
import os, sys
from pathlib import Path
Path("$SB/op-resolve.log").write_text("called\\n")
Path("$SB/op-connect-host").write_text(os.environ.get("OP_CONNECT_HOST", ""))
if os.environ.get("OP_RESOLVE_STUB_FAIL") == "1":
    sys.stderr.write("op-resolve: Keychain item op-connect-token absent; Connect token unavailable\\n")
    sys.exit(1)
if os.environ.get("OP_RESOLVE_STUB_LEAK") == "1":
    sys.stderr.write("failed fetching https://discord.com/api/webhooks/SECRET/LEAKED\\n")
    sys.exit(1)
print("https://discord.com/api/webhooks/RESOLVED/FROM_CONNECT")
EOF
chmod +x "$STUBS/curl" "$STUBS/op" "$STUBS/security" "$STUBS/op-resolve.py"
export PATH="$STUBS:$PATH"
# notify.json lives under XDG_CONFIG_HOME; keep the real one out of reach.
export XDG_CONFIG_HOME="$SB/xdg"

FAILURES=0
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; FAILURES=$((FAILURES + 1)); }

LITERAL_URL="https://discord.com/api/webhooks/PLAINTEXT/LITERAL"

echo "== (a) plaintext literal URL — curl called unchanged =="
OUT_A="$(echo "hello body" | env -u OP_CONNECT_HOST DISCORD_WEBHOOK_URL="$LITERAL_URL" bash "$SCRIPT" "Test A" 3066993 2>"$SB/a.err")"; RC_A=$?
[[ $RC_A -eq 0 ]] && pass "exit 0" || fail "expected exit 0, got $RC_A"
grep -q "$LITERAL_URL" "$SB/curl.log" 2>/dev/null && pass "curl received the literal URL" || fail "literal URL not passed to curl"
[[ ! -f "$SB/op.log" ]] && pass "op NOT invoked on plaintext path" || fail "op invoked on plaintext path"
grep -q -- "$LITERAL_URL" <<<"$OUT_A" && fail "URL leaked to stdout" || pass "no URL in stdout"
rm -f "$SB/curl.log" "$SB/op.log" "$SB/security.log"

echo "== (p) op:// without a Connect host — refuse before resolving =="
printf 'discord-notify: OP_CONNECT_HOST is required for op:// webhooks\n' >"$SB/p.expected.err"
for host_state in unset empty; do
  if [[ "$host_state" == unset ]]; then
    HOST_ENV=(-u OP_CONNECT_HOST)
  else
    HOST_ENV=(OP_CONNECT_HOST=)
  fi
  echo "hi" | env "${HOST_ENV[@]}" OP_RESOLVE_PY="$STUBS/op-resolve.py" \
    OP_RESOLVE_STUB_FAIL=1 OP_SERVICE_ACCOUNT_TOKEN=tok \
    DISCORD_WEBHOOK_URL="op://Automation/discord/webhook" \
    bash "$SCRIPT" "Missing host" >"$SB/p-$host_state.out" 2>"$SB/p-$host_state.err"
  RC_P=$?
  [[ $RC_P -eq 1 ]] && pass "$host_state host exits 1" || fail "$host_state host expected exit 1, got $RC_P"
  cmp -s "$SB/p.expected.err" "$SB/p-$host_state.err" \
    && pass "$host_state host emits one named refusal line" \
    || fail "$host_state host missing exact OP_CONNECT_HOST refusal"
  [[ ! -f "$SB/op-resolve.log" ]] \
    && pass "$host_state host: resolver NOT invoked" || fail "$host_state host: resolver invoked"
  [[ ! -f "$SB/op.log" ]] \
    && pass "$host_state host: op NOT invoked" || fail "$host_state host: op invoked"
  [[ ! -f "$SB/security.log" ]] \
    && pass "$host_state host: Keychain NOT invoked" || fail "$host_state host: Keychain invoked"
  [[ ! -f "$SB/curl.log" ]] \
    && pass "$host_state host: curl NOT invoked" || fail "$host_state host: curl invoked"
  [[ ! -s "$SB/p-$host_state.out" ]] \
    && pass "$host_state host: stdout empty" || fail "$host_state host: stdout not empty"
  rm -f "$SB/curl.log" "$SB/op.log" "$SB/security.log" "$SB/op-resolve.log" "$SB/op-connect-host"
done

echo "== (b0) Connect path — shared resolver owns the Keychain fallback =="
CONNECT_HOST="http://connect.invalid:8090/path with space/"
OUT_B0="$(echo "hi" | env -u OP_CONNECT_TOKEN OP_CONNECT_HOST="$CONNECT_HOST" OP_RESOLVE_PY="$STUBS/op-resolve.py" \
  DISCORD_WEBHOOK_URL="op://Automation/discord/webhook" bash "$SCRIPT" "Test B0" \
  2>"$SB/b0.err")"; RC_B0=$?
[[ $RC_B0 -eq 0 ]] && pass "exit 0" || fail "expected exit 0, got $RC_B0"
[[ -f "$SB/op-resolve.log" ]] && pass "shared resolver invoked" || fail "shared resolver not invoked"
[[ "$(cat "$SB/op-connect-host" 2>/dev/null)" == "$CONNECT_HOST" ]] \
  && pass "resolver received OP_CONNECT_HOST unchanged" || fail "resolver received a changed OP_CONNECT_HOST"
[[ ! -f "$SB/security.log" ]] && pass "wrapper did not read Keychain" || fail "wrapper read Keychain"
grep -q "RESOLVED/FROM_CONNECT" "$SB/curl.log" 2>/dev/null && pass "curl received Connect-resolved URL" || fail "Connect-resolved URL not passed to curl"
grep -qE -- "RESOLVED/FROM_CONNECT|op://" <<<"$OUT_B0" && fail "resolved/op value leaked to stdout" || pass "no secret in stdout"
rm -f "$SB/curl.log" "$SB/op.log" "$SB/security.log" "$SB/op-resolve.log"

echo "== (b) op:// value — op read + Keychain token fallback =="
OUT_B="$(echo "hi" | OP_CONNECT_HOST=http://connect.invalid OP_RESOLVE_PY="$SB/missing-resolver.py" DISCORD_WEBHOOK_URL="op://Automation/discord/webhook" bash "$SCRIPT" "Test B" 2>"$SB/b.err")"; RC_B=$?
[[ $RC_B -eq 0 ]] && pass "exit 0" || fail "expected exit 0, got $RC_B"
grep -q "op://Automation/discord/webhook" "$SB/op.log" 2>/dev/null && pass "op read called with the op:// ref" || fail "op not called with ref"
grep -q "op-service-account-token" "$SB/security.log" 2>/dev/null && pass "Keychain token fallback used" || fail "Keychain fallback not used"
grep -q "RESOLVED/FROM_OP" "$SB/curl.log" 2>/dev/null && pass "curl received the op-resolved URL" || fail "op-resolved URL not passed to curl"
grep -qE -- "RESOLVED/FROM_OP|op://" <<<"$OUT_B" && fail "resolved/op value leaked to stdout" || pass "no secret in stdout"
rm -f "$SB/curl.log" "$SB/op.log" "$SB/security.log"

echo "== (c) op failure — distinct message + non-zero exit, no HTTP =="
OUT_C="$(echo "hi" | OP_CONNECT_HOST=http://connect.invalid OP_RESOLVE_STUB_FAIL=1 OP_RESOLVE_PY="$STUBS/op-resolve.py" OP_STUB_FAIL=1 OP_SERVICE_ACCOUNT_TOKEN=tok DISCORD_WEBHOOK_URL="op://Automation/discord/webhook" bash "$SCRIPT" "Test C" 2>"$SB/c.err")"; RC_C=$?
[[ $RC_C -ne 0 ]] && pass "non-zero exit ($RC_C)" || fail "expected non-zero exit"
grep -q "1Password resolution failed (op)" "$SB/c.err" && pass "distinct op-failure message present" || fail "distinct message missing"
grep -qE "discord-notify: HTTP [0-9]" "$SB/c.err" && fail "op failure masqueraded as HTTP failure" || pass "not confused with HTTP failure"
[[ ! -f "$SB/curl.log" ]] && pass "curl NOT called after op failure" || fail "curl called despite op failure"
grep -q -- "op://" <<<"$OUT_C" && fail "op ref leaked to stdout" || pass "no op ref in stdout"
rm -f "$SB/curl.log" "$SB/op.log" "$SB/security.log" "$SB/op.count"

echo "== (d) cold-start retry — first op read fails, retry resolves =="
OUT_D="$(echo "hi" | OP_CONNECT_HOST=http://connect.invalid OP_RESOLVE_PY="$SB/missing-resolver.py" OP_STUB_FAIL_ONCE=1 OP_SERVICE_ACCOUNT_TOKEN=tok DISCORD_WEBHOOK_URL="op://Automation/discord/webhook" bash "$SCRIPT" "Test D" 2>"$SB/d.err")"; RC_D=$?
[[ $RC_D -eq 0 ]] && pass "exit 0 after retry" || fail "expected exit 0, got $RC_D"
[[ "$(grep -c OP_CALLED "$SB/op.log" 2>/dev/null)" == 2 ]] && pass "op read retried exactly once (2 calls)" || fail "expected 2 op calls, got $(grep -c OP_CALLED "$SB/op.log" 2>/dev/null)"
grep -q "RESOLVED/FROM_OP" "$SB/curl.log" 2>/dev/null && pass "curl received the op-resolved URL after retry" || fail "resolved URL not passed to curl after retry"
grep -qE -- "RESOLVED/FROM_OP|op://" <<<"$OUT_D" && fail "resolved/op value leaked to stdout" || pass "no secret in stdout"
rm -f "$SB/curl.log" "$SB/op.log" "$SB/security.log" "$SB/op.count"

echo "== (e) hung op read — _lk_timeout fires, notifier fails fast (not after 60s) =="
# The regression case this whole change exists for: `op` wedges (the macOS
# TCC app-data gate) instead of erroring. OP_READ_TIMEOUT_SECS shortens the
# bound so the test doesn't burn 2x90s; the fix is proven by wall-clock time,
# not just the exit code.
_start_e=$(date +%s)
OUT_E="$(echo "hi" | OP_CONNECT_HOST=http://connect.invalid OP_RESOLVE_PY="$SB/missing-resolver.py" OP_STUB_HANG=1 OP_READ_TIMEOUT_SECS=2 OP_SERVICE_ACCOUNT_TOKEN=tok DISCORD_WEBHOOK_URL="op://Automation/discord/webhook" bash "$SCRIPT" "Test E" 2>"$SB/e.err")"; RC_E=$?
_elapsed_e=$(( $(date +%s) - _start_e ))
[[ $RC_E -ne 0 ]] && pass "non-zero exit ($RC_E)" || fail "expected non-zero exit"
# Retried once (per the cold-start-retry contract): 2 attempts x 2s bound +
# the 2s inter-attempt sleep is ~6s — generously bounded well under the 60s
# `op` stub would otherwise sleep for.
(( _elapsed_e <= 15 )) && pass "failed fast (${_elapsed_e}s, not the stub's 60s sleep)" || fail "took ${_elapsed_e}s — timeout did not fire"
grep -q "1Password resolution failed (op)" "$SB/e.err" && pass "distinct op-failure message present" || fail "distinct message missing"
[[ ! -f "$SB/curl.log" ]] && pass "curl NOT called after a hung op" || fail "curl called despite hung op"
rm -f "$SB/curl.log" "$SB/op.log" "$SB/security.log" "$SB/op.count"

echo "== (f) NO --content — payload shape is byte-identical to the pre-flag contract =="
# The regression guard for every existing caller: same two positionals, same
# piped body, and the emitted JSON must be exactly the old {embeds:[...]} object
# with no content / allowed_mentions keys grafted on.
echo "legacy body" | DISCORD_WEBHOOK_URL="$LITERAL_URL" bash "$SCRIPT" "Legacy Title" 3066993 >/dev/null 2>"$SB/f.err"; RC_F=$?
EXPECT_F="$(jq -n --arg t "Legacy Title" --arg d "legacy body" --argjson c 3066993 \
  '{embeds:[{title:$t, description:$d, color:$c}]}')"
[[ $RC_F -eq 0 ]] && pass "exit 0" || fail "expected exit 0, got $RC_F"
if [[ "$(cat "$SB/payload.json")" == "$EXPECT_F" ]]; then
  pass "payload byte-identical to the pre-flag shape"
else
  fail "payload changed for a legacy caller: $(cat "$SB/payload.json")"
fi
jq -e 'has("content") or has("allowed_mentions")' "$SB/payload.json" >/dev/null 2>&1 \
  && fail "content/allowed_mentions leaked into a legacy payload" \
  || pass "no content/allowed_mentions keys on the legacy path"
# The other pre-existing call shapes: title only, and empty stdin.
echo "b" | DISCORD_WEBHOOK_URL="$LITERAL_URL" bash "$SCRIPT" "Title Only" >/dev/null 2>&1
[[ "$(jq -r '.embeds[0].color' "$SB/payload.json")" == "5793266" ]] && pass "title-only call still defaults to blurple" || fail "default color changed"
: | DISCORD_WEBHOOK_URL="$LITERAL_URL" bash "$SCRIPT" "Empty Body" >/dev/null 2>&1
[[ "$(jq -r '.embeds[0].description' "$SB/payload.json")" == "(no content)" ]] && pass "empty stdin still yields the '(no content)' embed" || fail "empty-stdin fallback changed"
rm -f "$SB/curl.log" "$SB/payload.json"

echo "== (g) --content — plain-text field present, embed still carried, mention preserved =="
OUT_G="$(echo "detail body" | DISCORD_WEBHOOK_URL="$LITERAL_URL" bash "$SCRIPT" \
  --content '<@123456789012345678> **NEEDS EXAMPLE** — approve the cutover?' "Needs Example" 16705372 2>"$SB/g.err")"; RC_G=$?
[[ $RC_G -eq 0 ]] && pass "exit 0" || fail "expected exit 0, got $RC_G"
[[ "$(jq -r '.content' "$SB/payload.json")" == '<@123456789012345678> **NEEDS EXAMPLE** — approve the cutover?' ]] \
  && pass "content is a top-level plain-text field carrying the mention" || fail "content wrong: $(jq -r '.content' "$SB/payload.json")"
[[ "$(jq -r '.embeds[0].description' "$SB/payload.json")" == "detail body" ]] && pass "embed still carries the stdin body alongside content" || fail "embed lost"
[[ "$(jq -rc '.allowed_mentions.parse' "$SB/payload.json")" == '["users"]' ]] \
  && pass "allowed_mentions pinned to users (no @everyone/@here/role blast radius)" || fail "allowed_mentions not pinned"
# The point of the flag: an embed CANNOT ping, so a mention that lives only in
# the embed is inert. Prove the mention is NOT confined to the embed.
jq -e '(.embeds[0].description // "") | test("<@")' "$SB/payload.json" >/dev/null 2>&1 \
  && fail "mention placed in the embed (embeds never ping)" || pass "mention is in content, not the embed"
grep -q -- "$LITERAL_URL" <<<"$OUT_G" && fail "URL leaked to stdout" || pass "no URL in stdout"
rm -f "$SB/curl.log" "$SB/payload.json"

echo "== (h) --content with no stdin body — content-only payload, no empty embed =="
: | DISCORD_WEBHOOK_URL="$LITERAL_URL" bash "$SCRIPT" --content "just a ping" "T" >/dev/null 2>&1; RC_H=$?
[[ $RC_H -eq 0 ]] && pass "exit 0" || fail "expected exit 0, got $RC_H"
[[ "$(jq -r '.content' "$SB/payload.json")" == "just a ping" ]] && pass "content sent" || fail "content missing"
jq -e 'has("embeds")' "$SB/payload.json" >/dev/null 2>&1 && fail "empty embed still attached" || pass "no embed when there is no body"
jq -e '.content | test("no content")' "$SB/payload.json" >/dev/null 2>&1 && fail "'(no content)' fallback bled into content" || pass "'(no content)' fallback correctly suppressed"
rm -f "$SB/curl.log" "$SB/payload.json"

echo "== (i) --content 1900-char cap — 1901 chars truncates with a marker, stays <= 1900 =="
LONG="$(printf 'x%.0s' $(seq 1 1901))"
[[ ${#LONG} -eq 1901 ]] && pass "input is exactly 1901 chars" || fail "input was ${#LONG} chars"
echo "b" | DISCORD_WEBHOOK_URL="$LITERAL_URL" bash "$SCRIPT" --content "$LONG" "T" >/dev/null 2>&1; RC_I=$?
[[ $RC_I -eq 0 ]] && pass "exit 0 (overflow truncates, never rejects)" || fail "expected exit 0, got $RC_I"
SENT_LEN="$(jq -r '.content | length' "$SB/payload.json")"
(( SENT_LEN <= 1900 )) && pass "emitted content is $SENT_LEN chars (<= 1900)" || fail "emitted content is $SENT_LEN chars — over the cap"
(( SENT_LEN < 1901 )) && pass "content was actually shortened" || fail "no truncation happened"
jq -e '.content | test("truncated")' "$SB/payload.json" >/dev/null 2>&1 && pass "truncation marker present" || fail "no truncation indicator"
# A 1900-char input must pass through untouched (boundary, not off-by-one).
EXACT="$(printf 'y%.0s' $(seq 1 1900))"
echo "b" | DISCORD_WEBHOOK_URL="$LITERAL_URL" bash "$SCRIPT" --content "$EXACT" "T" >/dev/null 2>&1
[[ "$(jq -r '.content | length' "$SB/payload.json")" == "1900" ]] && pass "exactly 1900 chars passes through untruncated" || fail "1900-char boundary was truncated"
rm -f "$SB/curl.log" "$SB/payload.json"

echo "== (j) --content= form, flag ordering, and a missing value =="
echo "b" | DISCORD_WEBHOOK_URL="$LITERAL_URL" bash "$SCRIPT" "T" 3066993 --content="trailing form" >/dev/null 2>&1
[[ "$(jq -r '.content' "$SB/payload.json")" == "trailing form" ]] && pass "--content= after the positionals parses" || fail "--content= form or trailing position broken"
[[ "$(jq -r '.embeds[0].title' "$SB/payload.json")" == "T" && "$(jq -r '.embeds[0].color' "$SB/payload.json")" == "3066993" ]] \
  && pass "positionals still bind to TITLE/COLOR with a flag present" || fail "positional binding broken by the flag"
DISCORD_WEBHOOK_URL="$LITERAL_URL" bash "$SCRIPT" "T" --content </dev/null >/dev/null 2>"$SB/j.err"; RC_J=$?
[[ $RC_J -eq 2 ]] && pass "--content with no value exits 2" || fail "expected exit 2, got $RC_J"
grep -q -- "--content requires a value" "$SB/j.err" && pass "clear usage error on stderr" || fail "usage error missing"
rm -f "$SB/curl.log" "$SB/payload.json"

echo "== (k) both paths fail — resolver stderr attributed alongside fallback error =="
echo "hi" | OP_CONNECT_HOST=http://connect.invalid OP_RESOLVE_STUB_FAIL=1 OP_STUB_FAIL=1 OP_RESOLVE_PY="$STUBS/op-resolve.py" \
  OP_SERVICE_ACCOUNT_TOKEN=tok DISCORD_WEBHOOK_URL="op://Automation/discord/webhook" \
  bash "$SCRIPT" "Test K" >"$SB/k.out" 2>"$SB/k.err"; RC_K=$?
[[ $RC_K -ne 0 ]] && pass "non-zero exit ($RC_K)" || fail "expected non-zero exit"
grep -q "1Password resolution failed (op)" "$SB/k.err" && pass "fallback error present" || fail "fallback error missing"
grep -q "discord-notify: op-resolve:" "$SB/k.err" && pass "resolver stderr attributed" || fail "resolver stderr missing"
grep -q "Keychain item op-connect-token absent" "$SB/k.err" && pass "resolver diagnostic text present" || fail "resolver diagnostic text missing"
grep -qE "HTTP [0-9]" "$SB/k.err" && fail "op failure masqueraded as HTTP failure" || pass "not confused with HTTP failure"
[[ ! -s "$SB/k.out" ]] && pass "stdout empty on both-failed" || fail "stdout not empty: $(cat "$SB/k.out")"
[[ ! -f "$SB/curl.log" ]] && pass "curl NOT called after both-path failure" || fail "curl called despite both-path failure"
grep -qE "SECRET/LEAKED|RESOLVED/FROM_|webhooks/" "$SB/k.err" "$SB/k.out" 2>/dev/null \
  && fail "webhook/secret leaked on both-failed" || pass "no webhook/secret in both-failed output"
# Fallback's own message is untouched: first stderr line matches today's text.
EXPECT_K_FALLBACK="discord-notify: 1Password resolution failed (op) — 'op read' could not resolve the webhook reference (or timed out after 2 attempts)"
[[ "$(head -n 1 "$SB/k.err")" == "$EXPECT_K_FALLBACK" ]] && pass "fallback error line byte-identical" || fail "fallback error line drifted: $(head -n 1 "$SB/k.err")"
rm -f "$SB/curl.log" "$SB/op.log" "$SB/security.log" "$SB/op-resolve.log" "$SB/op.count"

echo "== (k2) both paths fail — webhook-shaped URL in resolver stderr is scrubbed =="
echo "hi" | OP_CONNECT_HOST=http://connect.invalid OP_RESOLVE_STUB_LEAK=1 OP_STUB_FAIL=1 OP_RESOLVE_PY="$STUBS/op-resolve.py" \
  OP_SERVICE_ACCOUNT_TOKEN=tok DISCORD_WEBHOOK_URL="op://Automation/discord/webhook" \
  bash "$SCRIPT" "Test K2" >"$SB/k2.out" 2>"$SB/k2.err"; RC_K2=$?
[[ $RC_K2 -ne 0 ]] && pass "non-zero exit ($RC_K2)" || fail "expected non-zero exit"
grep -q "SECRET/LEAKED" "$SB/k2.err" && fail "webhook URL leaked from resolver stderr" || pass "webhook URL not present in stderr"
grep -q "\[redacted-webhook\]" "$SB/k2.err" && pass "webhook URL scrubbed" || fail "redaction marker missing"
grep -q "discord-notify: op-resolve:" "$SB/k2.err" && pass "resolver stderr still attributed after scrub" || fail "resolver stderr missing after scrub"
rm -f "$SB/curl.log" "$SB/op.log" "$SB/security.log" "$SB/op-resolve.log" "$SB/op.count"

echo "== (l) fallback succeeds after op-resolve.py failed — byte-identical to pre-fix =="
# Pre-fix capture (HEAD 409a553, 2026-09-21): stdout = 'discord-notify: ok (204)\n', stderr empty.
echo "hi" | OP_CONNECT_HOST=http://connect.invalid OP_RESOLVE_STUB_FAIL=1 OP_RESOLVE_PY="$STUBS/op-resolve.py" \
  OP_SERVICE_ACCOUNT_TOKEN=tok DISCORD_WEBHOOK_URL="op://Automation/discord/webhook" \
  bash "$SCRIPT" "Today FB" >"$SB/l.out" 2>"$SB/l.err"; RC_L=$?
[[ $RC_L -eq 0 ]] && pass "exit 0" || fail "expected exit 0, got $RC_L"
printf 'discord-notify: ok (204)\n' > "$SB/l.expected.out"
: > "$SB/l.expected.err"
echo "----- fallback-success stdout diff (must be empty) -----"
if diff -u "$SB/l.expected.out" "$SB/l.out"; then
  pass "stdout byte-identical to pre-fix"
else
  fail "stdout drifted from pre-fix"
fi
echo "----- fallback-success stderr diff (must be empty) -----"
if diff -u "$SB/l.expected.err" "$SB/l.err"; then
  pass "stderr byte-identical to pre-fix (empty)"
else
  fail "stderr drifted from pre-fix"
fi
grep -q "discord-notify: op-resolve:" "$SB/l.err" && fail "resolver stderr leaked on fallback-success" || pass "resolver stderr suppressed on fallback-success"
grep -q "RESOLVED/FROM_OP" "$SB/curl.log" 2>/dev/null && pass "curl received the op-resolved URL" || fail "op-resolved URL not passed to curl"
[[ -f "$SB/op-resolve.log" ]] && pass "failing resolver was invoked" || fail "resolver not invoked"
rm -f "$SB/curl.log" "$SB/op.log" "$SB/security.log" "$SB/op-resolve.log" "$SB/op.count"

echo "== (n) op-resolve.py succeeds — stdout/stderr unchanged =="
echo "hi" | OP_CONNECT_HOST=http://connect.invalid OP_RESOLVE_PY="$STUBS/op-resolve.py" \
  DISCORD_WEBHOOK_URL="op://Automation/discord/webhook" \
  bash "$SCRIPT" "Today OK" >"$SB/n.out" 2>"$SB/n.err"; RC_N=$?
[[ $RC_N -eq 0 ]] && pass "exit 0" || fail "expected exit 0, got $RC_N"
printf 'discord-notify: ok (204)\n' > "$SB/n.expected.out"
printf 'discord-notify: webhook resolved via Connect (no op binary)\n' > "$SB/n.expected.err"
if diff -u "$SB/n.expected.out" "$SB/n.out"; then
  pass "stdout byte-identical to pre-fix Connect-success"
else
  fail "Connect-success stdout drifted"
fi
if diff -u "$SB/n.expected.err" "$SB/n.err"; then
  pass "stderr byte-identical to pre-fix Connect-success"
else
  fail "Connect-success stderr drifted"
fi
grep -q "RESOLVED/FROM_CONNECT" "$SB/curl.log" 2>/dev/null && pass "curl received Connect-resolved URL" || fail "Connect-resolved URL not passed to curl"
[[ ! -f "$SB/op.log" ]] && pass "op NOT invoked on Connect-success" || fail "op invoked on Connect-success"
rm -f "$SB/curl.log" "$SB/op.log" "$SB/security.log" "$SB/op-resolve.log"

echo "== (o) per-invocation response files — isolated and removed =="
RESPONSE_TMP="$SB/response-tmp"
SYNC_DIR="$SB/curl-sync"
mkdir -p "$RESPONSE_TMP" "$SYNC_DIR"

response_temp_count() {
  set -- "$RESPONSE_TMP"/discord-notify.*
  if [[ -e "$1" ]]; then printf '%s' "$#"; else printf '0'; fi
}

rm -f "$SB/curl.log"
echo "success" | TMPDIR="$RESPONSE_TMP" CURL_STUB_HTTP=204 \
  DISCORD_WEBHOOK_URL="$LITERAL_URL" bash "$SCRIPT" "Temp success" \
  >"$SB/o-success.out" 2>"$SB/o-success.err"; RC_O_SUCCESS=$?
[[ $RC_O_SUCCESS -eq 0 ]] && pass "success path exits 0" || fail "success path expected 0, got $RC_O_SUCCESS"
[[ "$(response_temp_count)" == 0 ]] \
  && pass "success path removes its response temp file" \
  || fail "success path left response temp files behind"
grep -q -- "-o $RESPONSE_TMP/discord-notify\\." "$SB/curl.log" \
  && pass "curl response path uses TMPDIR" \
  || fail "curl response path did not use TMPDIR"

rm -f "$SB/curl.log"
echo "fallback" | TMPDIR=/nonexistent CURL_STUB_HTTP=204 \
  DISCORD_WEBHOOK_URL="$LITERAL_URL" bash "$SCRIPT" "Temp fallback" \
  >"$SB/o-fallback.out" 2>"$SB/o-fallback.err"; RC_O_FALLBACK=$?
printf 'discord-notify: ok (204)\n' >"$SB/o-fallback.expected"
if [[ $RC_O_FALLBACK -eq 0 ]] \
    && diff -q "$SB/o-fallback.expected" "$SB/o-fallback.out" >/dev/null \
    && [[ ! -s "$SB/o-fallback.err" && -s "$SB/curl.log" ]]; then
  pass "unusable TMPDIR still posts with unchanged output"
else
  fail "unusable TMPDIR did not preserve posting behavior"
fi

rm -f "$SB/curl.log"

echo "failure" | TMPDIR="$RESPONSE_TMP" CURL_STUB_HTTP=500 CURL_STUB_BODY="single failure body" \
  DISCORD_WEBHOOK_URL="$LITERAL_URL" bash "$SCRIPT" "Temp failure" \
  >"$SB/o-failure.out" 2>"$SB/o-failure.err"; RC_O_FAILURE=$?
[[ $RC_O_FAILURE -eq 1 ]] && pass "failure path exits 1" || fail "failure path expected 1, got $RC_O_FAILURE"
grep -q "single failure body" "$SB/o-failure.err" \
  && pass "failure path prints its response body" \
  || fail "failure path lost its response body"
[[ "$(response_temp_count)" == 0 ]] \
  && pass "failure path removes its response temp file" \
  || fail "failure path left response temp files behind"
DEAD_SYNC_DIR="$SYNC_DIR/dead"
mkdir -p "$DEAD_SYNC_DIR"
CURL_STUB_SYNC_DIR="$DEAD_SYNC_DIR" CURL_STUB_ID=only CURL_STUB_PEER=missing \
  "$STUBS/curl" -o "$SB/o-dead-peer.out" \
  >"$SB/o-dead-peer.stdout" 2>"$SB/o-dead-peer.err" &
PID_O_DEAD_PEER=$!
DEAD_PEER_FINISHED=0
for _wait_probe in {1..200}; do
  if ! kill -0 "$PID_O_DEAD_PEER" 2>/dev/null; then
    DEAD_PEER_FINISHED=1
    break
  fi
  sleep 0.01
done
if [[ $DEAD_PEER_FINISHED -eq 1 ]]; then
  wait "$PID_O_DEAD_PEER"; RC_O_DEAD_PEER=$?
  if [[ $RC_O_DEAD_PEER -eq 97 ]] \
      && grep -q "curl stub: peer wait timed out" "$SB/o-dead-peer.err"; then
    pass "curl stub bounds its peer wait"
  else
    fail "curl stub peer timeout returned $RC_O_DEAD_PEER"
  fi
else
  kill "$PID_O_DEAD_PEER" 2>/dev/null || true
  wait "$PID_O_DEAD_PEER" 2>/dev/null
  fail "curl stub peer wait was unbounded"
fi


echo "first" | TMPDIR="$RESPONSE_TMP" CURL_STUB_HTTP=500 CURL_STUB_BODY="first invocation body" \
  CURL_STUB_SYNC_DIR="$SYNC_DIR" CURL_STUB_ID=first CURL_STUB_PEER=second \
  DISCORD_WEBHOOK_URL="$LITERAL_URL" bash "$SCRIPT" "Concurrent first" \
  >"$SB/o-first.out" 2>"$SB/o-first.err" &
PID_O_FIRST=$!
echo "second" | TMPDIR="$RESPONSE_TMP" CURL_STUB_HTTP=500 CURL_STUB_BODY="second invocation body" \
  CURL_STUB_SYNC_DIR="$SYNC_DIR" CURL_STUB_ID=second CURL_STUB_PEER=first \
  DISCORD_WEBHOOK_URL="$LITERAL_URL" bash "$SCRIPT" "Concurrent second" \
  >"$SB/o-second.out" 2>"$SB/o-second.err" &
PID_O_SECOND=$!
wait "$PID_O_FIRST"; RC_O_FIRST=$?
wait "$PID_O_SECOND"; RC_O_SECOND=$?
[[ $RC_O_FIRST -eq 1 && $RC_O_SECOND -eq 1 ]] \
  && pass "concurrent failures both exit 1" \
  || fail "concurrent failure exits were $RC_O_FIRST and $RC_O_SECOND"
grep -q "first invocation body" "$SB/o-first.err" \
  && ! grep -q "second invocation body" "$SB/o-first.err" \
  && pass "first invocation prints only its own response body" \
  || fail "first invocation response body was crossed"
grep -q "second invocation body" "$SB/o-second.err" \
  && ! grep -q "first invocation body" "$SB/o-second.err" \
  && pass "second invocation prints only its own response body" \
  || fail "second invocation response body was crossed"
[[ "$(response_temp_count)" == 0 ]] \
  && pass "concurrent failures remove both response temp files" \
  || fail "concurrent failures left response temp files behind"
if [[ -e "$SB/legacy-response-targeted" ]]; then
  fail "curl targeted /tmp/discord-notify.out"
else
  pass "/tmp/discord-notify.out was not targeted"
fi
echo
if [[ $FAILURES -eq 0 ]]; then
  echo "ALL SMOKE TESTS PASSED"
else
  echo "$FAILURES CHECK(S) FAILED"
fi
exit $FAILURES
