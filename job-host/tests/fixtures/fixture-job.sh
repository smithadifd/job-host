#!/bin/bash
# The shared body of the fixture scheduled job, written against the
# launchd-kit/lib.sh function names. It sources nothing: fixture-job-kit.sh
# (launchd-kit/lib.sh) and fixture-job-host.sh (job-host/host.sh) each source
# their library by a fixed path and then this file, so the parity test runs the
# same job body, the same way, on both libraries. Everything else about the run
# is driven by FIXTURE_* variables.
set -uo pipefail

# shellcheck disable=SC2034 # read by the library
TOOL_NAME="${FIXTURE_TOOL:-fixture-job}"
launchd_kit_init

if [[ "${FIXTURE_LOCK_FIRST:-0}" == "1" ]]; then
  lk_single_lock "${FIXTURE_LOCK_MAX_AGE:-120}"
fi
lk_slot_guard "${FIXTURE_INTERVAL:-daily}" "${FIXTURE_AT:-00:00}"
if [[ "${FIXTURE_LOCK_FIRST:-0}" != "1" ]]; then
  lk_single_lock "${FIXTURE_LOCK_MAX_AGE:-120}"
fi

if [[ -n "${FIXTURE_REQUIRE:-}" ]]; then
  # shellcheck disable=SC2086 # a space-separated list on purpose
  lk_require_cmd $FIXTURE_REQUIRE
fi

lk_log "fixture body start"
_lk_timeout "${FIXTURE_TIMEOUT:-10}" /bin/bash -c "${FIXTURE_BODY:-echo body-ok}"
rc=$?
lk_log "fixture body rc=$rc"

# Telemetry transport. lib.sh builds the line itself (its helper is pointed at
# a stub that prints FIXTURE_PAYLOAD); the job host takes the payload as given.
if [[ -n "${FIXTURE_PAYLOAD:-}" ]]; then
  if declare -F jh_spool_append >/dev/null; then
    jh_spool_append "$FIXTURE_SPOOL_DIR/spool.jsonl" "$FIXTURE_PAYLOAD"
  else
    lk_emit_run
  fi
fi

lk_rotate_logs
exit "$rc"
