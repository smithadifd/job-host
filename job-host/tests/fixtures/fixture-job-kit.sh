#!/bin/bash
# The fixture scheduled job on launchd-kit/lib.sh. Sources the library by a fixed path
# relative to this file, then the shared body in fixture-job.sh, so the same
# job body runs on both libraries (see test_parity.py and test_lock.py).
set -uo pipefail

# shellcheck source=launchd-kit/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../../launchd-kit/lib.sh"
# shellcheck source=job-host/tests/fixtures/fixture-job.sh
source "$(dirname "${BASH_SOURCE[0]}")/fixture-job.sh"
