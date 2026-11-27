#!/usr/bin/env python3
"""Lock contention for job-host/host.sh's jh_single_lock."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _support import BASH, FIXTURES, ScratchCase  # noqa: E402

FIXTURE_JOB_HOST = FIXTURES / "fixture-job-host.sh"

CONTENDERS = 6


class LockContentionTest(ScratchCase):

    def setUp(self):
        super().setUp()
        self.log_dir = self.scratch / "logs"
        self.lock = self.log_dir / ".run.lock"

    def start(self, start_gate=None, **extra):
        env = self.base_env(LOG_DIR=self.log_dir, LK_FORCE="1", **extra)
        command = [BASH, str(FIXTURE_JOB_HOST)]
        if start_gate is not None:
            command = [
                BASH, "-c",
                'gate="$1"; shift; '
                'while [[ ! -f "$gate" ]]; do sleep 0.05; done; exec "$@"',
                "lock-contender", str(start_gate), *command,
            ]
        return subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )

    def logs(self):
        return "".join(p.read_text() for p in sorted(self.log_dir.glob("*.log")))

    def wait_for(self, predicate, secs=15):
        deadline = time.time() + secs
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_simultaneous_runs_admit_exactly_one(self):
        start_gate = self.scratch / "start"
        release = self.scratch / "release"
        # A duration is not a contention boundary: a descheduled contender
        # could arrive after the holder's sleep and legitimately acquire.
        # Start together, then keep the winner alive until all losers exit.
        body = 'while [[ ! -f "%s" ]]; do sleep 0.05; done' % release
        procs = []
        try:
            for _ in range(CONTENDERS):
                procs.append(self.start(start_gate=start_gate, FIXTURE_BODY=body,
                                        FIXTURE_TIMEOUT="60"))
            start_gate.touch()
            self.assertTrue(self.wait_for(
                lambda: self.logs().count("fixture body start") > 1
                or sum(p.poll() is not None for p in procs) >= CONTENDERS - 1),
                "contenders must take the contended exit while the holder runs")
            self.assertEqual(self.logs().count("fixture body start"), 1)
            self.assertEqual(sum(p.poll() is None for p in procs), 1,
                             "the winner must still hold the lock")
        finally:
            start_gate.touch()
            release.touch()
            results = []
            for p in procs:
                _, err = p.communicate(timeout=60)
                results.append((p.returncode, err))

        self.assertEqual([rc for rc, _ in results], [0] * CONTENDERS)
        self.assertEqual(self.logs().count("fixture body start"), 1)
        skipped = [err for _, err in results if "another run is active" in err]
        self.assertEqual(len(skipped), CONTENDERS - 1)
        self.assertFalse(self.lock.exists(), "the winner must release the lock at exit")

    def test_losing_tick_leaves_the_holders_lock_in_place(self):
        holder = self.start(FIXTURE_BODY="sleep 3")
        try:
            self.assertTrue(self.wait_for(lambda: "fixture body start" in self.logs()))
            self.assertTrue(self.lock.is_dir())

            contender = self.start()
            rc = contender.wait(timeout=30)
            err = contender.stderr.read()
            contender.stdout.close()
            contender.stderr.close()

            self.assertEqual(rc, 0)
            self.assertIn("another run is active", err)
            self.assertTrue(self.lock.is_dir(), "a skipped tick must not release the holder's lock")
            self.assertEqual(holder.poll(), None, "the holder is still running")
        finally:
            self.assertEqual(holder.wait(timeout=30), 0)
            holder.stdout.close()
            holder.stderr.close()
        self.assertFalse(self.lock.exists())
        self.assertEqual(self.logs().count("fixture body start"), 1)

    def test_fresh_lock_blocks_and_stale_lock_is_stolen(self):
        self.log_dir.mkdir()
        self.lock.mkdir()
        old = time.time() - 30 * 60
        os.utime(self.lock, (old, old))

        # 30 minutes old against a 60-minute limit: still held.
        blocked = self.start(FIXTURE_LOCK_MAX_AGE="60")
        self.assertEqual(blocked.wait(timeout=30), 0)
        self.assertIn("another run is active", blocked.stderr.read())
        blocked.stdout.close()
        blocked.stderr.close()
        self.assertTrue(self.lock.is_dir())

        # The same lock against a 20-minute limit: stale, stolen, released.
        stealer = self.start(FIXTURE_LOCK_MAX_AGE="20")
        self.assertEqual(stealer.wait(timeout=30), 0)
        stealer.stdout.close()
        stealer.stderr.close()
        self.assertEqual(self.logs().count("fixture body start"), 1)
        self.assertFalse(self.lock.exists())

    def stale_lock(self):
        self.log_dir.mkdir(exist_ok=True)
        self.lock.mkdir()
        old = time.time() - 3 * 3600
        os.utime(self.lock, (old, old))

    def slow_staleness_check(self):
        """Put a `find` on the pinned PATH that answers the lock's staleness
        check (`-maxdepth 0`) truthfully, then pauses, so every contender has
        decided the lock is stale before any of them acts on it."""
        fake = self.bin / "find"
        fake.write_text(
            "#!/bin/bash\n"
            'out="$(/usr/bin/find "$@")"; rc=$?\n'
            '[[ -n "$out" ]] && printf "%s\\n" "$out"\n'
            'case " $* " in *" -maxdepth 0 "*) [[ -n "$out" ]] && /bin/sleep 1 ;; esac\n'
            "exit $rc\n"
        )
        fake.chmod(0o755)

    def test_contenders_for_a_stale_lock_admit_exactly_one(self):
        self.stale_lock()
        self.slow_staleness_check()
        procs = [self.start(FIXTURE_BODY="sleep 3") for _ in range(2)]
        results = [(p.wait(timeout=60), p.stderr.read()) for p in procs]
        for p in procs:
            p.stdout.close()
            p.stderr.close()

        self.assertEqual([rc for rc, _ in results], [0, 0], results)
        self.assertEqual(self.logs().count("fixture body start"), 1,
                         "both contenders stole the stale lock and ran")
        self.assertEqual(sum("another run is active" in err for _, err in results), 1)
        self.assertFalse(self.lock.exists())
        self.assertEqual(sorted(p.name for p in self.log_dir.iterdir() if p.name.startswith(".run.lock")), [])

    def staggered_staleness_check(self):
        """Put a `find` on the pinned PATH that answers the lock's staleness
        check truthfully, then pauses 1 s for the first caller and 3 s for
        every later one. The first contender steals the stale lock and starts
        its body while the second is still paused after deciding the OLD lock
        was stale."""
        fake = self.bin / "find"
        fake.write_text(
            "#!/bin/bash\n"
            'out="$(/usr/bin/find "$@")"; rc=$?\n'
            '[[ -n "$out" ]] && printf "%s\\n" "$out"\n'
            'case " $* " in *" -maxdepth 0 "*)\n'
            '  if [[ -n "$out" ]]; then\n'
            '    if /bin/mkdir "FIRST_FIND" 2>/dev/null; then /bin/sleep 1; else /bin/sleep 3; fi\n'
            "  fi ;;\n"
            "esac\n"
            "exit $rc\n"
            .replace("FIRST_FIND", str(self.scratch / "first-find"))
        )
        fake.chmod(0o755)

    def test_late_contender_does_not_steal_the_replacement_lock(self):
        # Both contenders see the same stale lock. The first replaces it with
        # its own fresh lock and starts running; the second wakes up later and
        # must leave that live lock alone.
        self.stale_lock()
        self.staggered_staleness_check()
        procs = [self.start(FIXTURE_BODY="sleep 5") for _ in range(2)]
        results = [(p.wait(timeout=60), p.stderr.read()) for p in procs]
        for p in procs:
            p.stdout.close()
            p.stderr.close()

        self.assertEqual([rc for rc, _ in results], [0, 0], results)
        self.assertEqual(self.logs().count("fixture body start"), 1,
                         "the late contender stole the fresh lock and ran too")
        self.assertEqual(sum("another run is active" in err for _, err in results), 1)
        self.assertFalse(self.lock.exists())
        self.assertEqual(sorted(p.name for p in self.log_dir.iterdir() if p.name.startswith(".run.lock")), [])

    def test_stale_holder_exit_leaves_the_new_holders_lock(self):
        # A holder that outlives max_age has its lock stolen. When it finally
        # exits, it must not delete the lock that now belongs to the stealer.
        holder = self.start(FIXTURE_BODY="sleep 3")
        stealer = None
        try:
            self.assertTrue(self.wait_for(lambda: "fixture body start" in self.logs()))
            old = time.time() - 3 * 3600
            os.utime(self.lock, (old, old))

            stealer = self.start(FIXTURE_BODY="sleep 8")
            self.assertTrue(self.wait_for(lambda: self.logs().count("fixture body start") == 2),
                            "the stale lock was not stolen")
            self.assertEqual(holder.wait(timeout=30), 0)
            self.assertIsNone(stealer.poll(), "the stealer is still running")
            self.assertTrue(self.lock.is_dir(),
                            "the earlier holder's exit removed the stealer's lock")

            contender = self.start()
            self.assertEqual(contender.wait(timeout=30), 0)
            err = contender.stderr.read()
            contender.stdout.close()
            contender.stderr.close()
            self.assertIn("another run is active", err)
        finally:
            for proc in (holder, stealer):
                if proc is None:
                    continue
                if proc.poll() is None:
                    proc.wait(timeout=30)
                proc.stdout.close()
                proc.stderr.close()
        self.assertEqual(stealer.returncode, 0)
        self.assertFalse(self.lock.exists(), "the stealer releases its own lock")
        self.assertEqual(self.logs().count("fixture body start"), 2)

    def test_lock_is_released_when_the_job_fails(self):
        proc = self.start(FIXTURE_BODY="exit 9")
        self.assertEqual(proc.wait(timeout=30), 9)
        proc.stdout.close()
        proc.stderr.close()
        self.assertFalse(self.lock.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
