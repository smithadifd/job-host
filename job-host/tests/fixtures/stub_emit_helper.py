#!/usr/bin/env python3
"""Stands in for launchd-kit/serotine_usage.py in the parity test: lib.sh's
lk_emit_run calls `<helper> build-line` and appends whatever it prints to the
spool. Printing FIXTURE_PAYLOAD lets the test compare lib.sh's spool write with
the job host's jh_spool_append byte for byte."""
import os
import sys

if sys.argv[1:2] == ["build-line"]:
    sys.stdout.write(os.environ.get("FIXTURE_PAYLOAD", ""))
