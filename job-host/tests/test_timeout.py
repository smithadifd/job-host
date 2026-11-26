#!/usr/bin/env python3
"""Timeout and telemetry transport: jh_run_bounded, jh_timeout, jh_spool_append."""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _support import HOST_SH, ScratchCase, pid_alive  # noqa: E402

# A child that ignores SIGTERM and forks a grandchild into its own process
# group, recording both pids. Only a group-wide KILL ends both. Readiness is
# published last as an atomic rename, so pids/ready appears whole or not at all.
STUBBORN_CHILD = r'''#!/bin/bash
trap '' TERM
sleep 60 &
echo "$!" > "$PIDS/grandchild"
echo "$$" > "$PIDS/child"
echo ready > "$PIDS/ready.tmp"
mv "$PIDS/ready.tmp" "$PIDS/ready"
while :; do sleep 1; done
'''


class TimeoutTest(ScratchCase):

    def setUp(self):
        super().setUp()
        self.spool = self.scratch / "telemetry" / "events.jsonl"
        self.pids = self.scratch / "pids"
        self.pids.mkdir()
        self.child = self.scratch / "stubborn-child.sh"
        self.child.write_text(STUBBORN_CHILD)
        self.child.chmod(0o755)
        self.log_dir = self.scratch / "logs"

    def run_host(self, body, path=None, **extra):
        env = self.base_env(
            TOOL_NAME="bounded-fixture",
            LOG_DIR=self.log_dir,
            JH_TIMEOUT_SPOOL=self.spool,
            PIDS=self.pids,
            LK_TIMEOUT_KILL_AFTER="1s",
            **extra
        )
        if path:
            env["PATH"] = env["LAUNCHD_KIT_PATH"] = path
        script = 'source "%s"\njh_init\n%s\n' % (HOST_SH, body)
        return self.bash(script, env, timeout=60)

    def spool_records(self):
        if not self.spool.exists():
            return []
        return [json.loads(line) for line in self.spool.read_text().splitlines()]

    def log_text(self):
        return "".join(p.read_text() for p in sorted(self.log_dir.glob("*.log")))

    def run_until_ready(self, body, deadline_secs=90, **kwargs):
        """Run a bounded fixture until it reached readiness before its limit.

        The 1s limit starts when the fixture starts, so on a loaded machine it
        can fire before the fixture has trapped TERM and written its pids. The
        fixture renames pids/ready into place after the TERM trap and both pid
        files, so that marker is atomic proof of readiness: when it is missing
        the limit beat the fixture, and the run is discarded and repeated.
        The bounded command keeps limit 1 on every attempt. Sets
        self.run_secs to the length of the attempt that is returned.
        """
        give_up = time.time() + deadline_secs
        while True:
            started = time.time()
            proc = self.run_host(body, **kwargs)
            self.run_secs = time.time() - started
            if (self.pids / "ready").exists() or time.time() > give_up:
                return proc
            self.reset_run()

    def reset_run(self):
        for path in list(self.pids.iterdir()) + [self.spool] + list(self.log_dir.glob("*")):
            if path.is_file():
                path.unlink()

    def assertKilled(self):
        for name in ("child", "grandchild"):
            path = self.pids / name
            if not path.exists():
                self.fail("pid file %s was never written; the fixture did not reach readiness" % path)
            text = path.read_text()
            try:
                pid = int(text)
            except ValueError:
                self.fail("pid file %s holds %r, not a pid; the fixture did not reach readiness" % (path, text))
            deadline = time.time() + 5
            while pid_alive(pid) and time.time() < deadline:
                time.sleep(0.1)
            self.assertFalse(pid_alive(pid), "%s %d outlived the timeout" % (name, pid))

    def assertTimeoutRecord(self, rc):
        records = self.spool_records()
        self.assertEqual(len(records), 1, records)
        rec = records[0]
        self.assertEqual(rec["event"], "job-host.timeout")
        self.assertEqual(rec["tool"], "bounded-fixture")
        self.assertEqual(rec["limit_secs"], 1)
        self.assertEqual(rec["exit_code"], rc)
        self.assertGreaterEqual(rec["elapsed_secs"], 1)
        self.assertTrue(rec["run_stamp"])

    # --- the timeout kills the child and emits a record -------------------------

    def test_timeout_kills_the_child_group_and_records_it(self):
        proc = self.run_until_ready('jh_run_bounded 1 "%s"; echo "rc=$?"' % self.child)
        self.assertLess(self.run_secs, 20)
        rc = int(proc.stdout.strip().split("rc=")[-1])
        self.assertIn(rc, (124, 137))
        self.assertKilled()
        self.assertTimeoutRecord(rc)
        self.assertIn("exceeded 1s and was killed", self.log_text())

    def test_timeout_on_the_perl_tier(self):
        # No timeout/gtimeout on this PATH, so jh_timeout uses its perl tier.
        perl_bin = self.scratch / "perl-bin"
        perl_bin.mkdir()
        (perl_bin / "perl").symlink_to("/usr/bin/perl")
        path = "%s:%s:/bin" % (self.bin, perl_bin)
        proc = self.run_until_ready(
            'if command -v timeout || command -v gtimeout; then echo TIER-LEAK; exit 99; fi\n'
            'jh_run_bounded 1 "%s"; echo "rc=$?"' % self.child,
            path=path,
        )
        self.assertNotIn("TIER-LEAK", proc.stdout)
        rc = int(proc.stdout.strip().split("rc=")[-1])
        self.assertEqual(rc, 124)
        self.assertKilled()
        self.assertTimeoutRecord(rc)

    def test_caller_payload_replaces_the_default_record(self):
        body = (
            'on_timeout() { printf \'{"caller":"payload","limit":%%s,"rc":%%s}\' "$1" "$2"; }\n'
            'JH_ON_TIMEOUT=on_timeout jh_run_bounded 1 "%s"; echo "rc=$?"' % self.child
        )
        proc = self.run_host(body)
        rc = int(proc.stdout.strip().split("rc=")[-1])
        self.assertEqual(self.spool_records(), [{"caller": "payload", "limit": 1, "rc": rc}])

    def test_fast_command_is_not_a_timeout(self):
        proc = self.run_host('jh_run_bounded 5 /bin/bash -c "echo out; exit 3"; echo "rc=$?"')
        self.assertEqual(proc.stdout, "out\nrc=3\n")
        self.assertFalse(self.spool.exists())

    def test_command_exiting_124_on_its_own_is_not_a_timeout(self):
        proc = self.run_host('jh_run_bounded 5 /bin/bash -c "exit 124"; echo "rc=$?"')
        self.assertIn("rc=124", proc.stdout)
        self.assertFalse(self.spool.exists())
        self.assertNotIn("was killed", self.log_text())

    def test_safe_under_errexit(self):
        proc = self.run_host('set -e\njh_run_bounded 5 /bin/bash -c "exit 4" || echo "rc=$?"\necho after')
        self.assertEqual(proc.stdout, "rc=4\nafter\n")

    def test_usage_errors(self):
        proc = self.run_host('jh_run_bounded 0 true; echo "a=$?"; jh_run_bounded 5; echo "b=$?"; jh_run_bounded x true; echo "c=$?"')
        self.assertEqual(proc.stdout, "a=2\nb=2\nc=2\n")


class SpoolTransportTest(ScratchCase):

    def append(self, *payloads):
        spool = self.scratch / "t" / "spool.jsonl"
        body = "\n".join(
            'jh_spool_append "%s" %s; echo "rc=$?"' % (spool, _sh_quote(p)) for p in payloads
        )
        env = self.base_env(TOOL_NAME="spool-fixture", LOG_DIR=self.scratch / "logs")
        proc = self.bash('source "%s"\njh_init\n%s\n' % (HOST_SH, body), env)
        return proc, spool

    def test_valid_payloads_are_appended_verbatim_in_order(self):
        a = '{"a":1}'
        b = '{"b":"two","c":[1,2]}'
        proc, spool = self.append(a, b)
        self.assertEqual(proc.stdout, "rc=0\nrc=0\n")
        self.assertEqual(spool.read_text(), a + "\n" + b + "\n")

    def test_bad_payloads_are_refused_and_never_fail_the_job(self):
        proc, spool = self.append('{"a":1}', '{"a":\n1}', "not json", "", '{"z":2}')
        self.assertEqual(proc.stdout, "rc=0\n" * 5)
        self.assertEqual(spool.read_text(), '{"a":1}\n{"z":2}\n')
        self.assertIn("more than one line", proc.stderr)
        self.assertIn("not valid JSON", proc.stderr)
        self.assertIn("empty payload", proc.stderr)

    def test_unwritable_spool_never_fails_the_job(self):
        blocker = self.scratch / "file-not-dir"
        blocker.write_text("")
        env = self.base_env(TOOL_NAME="spool-fixture", LOG_DIR=self.scratch / "logs")
        proc = self.bash(
            'source "%s"\njh_init\njh_spool_append "%s/spool.jsonl" \'{"a":1}\'; echo "rc=$?"' % (HOST_SH, blocker),
            env,
        )
        self.assertEqual(proc.stdout, "rc=0\n")
        self.assertIn("payload dropped", proc.stderr)


def _sh_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


if __name__ == "__main__":
    unittest.main(verbosity=2)
