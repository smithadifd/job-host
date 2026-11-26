#!/usr/bin/env python3
"""Parity: one fixture job, run through launchd-kit/lib.sh and job-host/host.sh.

Each scenario runs the one job body in tests/fixtures/fixture-job.sh, through
fixture-job-kit.sh or fixture-job-host.sh (each sources its library, then the
body), one or more times in a fresh LOG_DIR per library, then compares what
the two runs did: exit codes, stdout,
stderr, the run log, the slot marker, the lock directory and the spool.
Run-specific values (paths, timestamps, host) are normalized first.

A missing required command is compared as-is, stderr included: both
libraries make the same notification attempt through NOTIFY_SCRIPT.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _support import (  # noqa: E402
    BASH,
    FIXTURES,
    HOST_SH,
    LIB_SH,
    ScratchCase,
    normalize,
)

PAYLOAD = '{"fixture":"payload","n":1,"nested":{"ok":true}}'

# One job body, two thin wrappers: the wrapper picks the library.
FIXTURE_JOB_KIT = FIXTURES / "fixture-job-kit.sh"
FIXTURE_JOB_HOST = FIXTURES / "fixture-job-host.sh"


class Outcome:
    def __init__(self, runs, log, marker, lock, spool, logs_left):
        self.runs = runs            # [(rc, stdout, stderr)] per step
        self.log = log              # every run log, concatenated
        self.marker = marker        # slot marker content, or None
        self.lock = lock            # lock dir still present after the last step
        self.spool = spool          # spool bytes, or None
        self.logs_left = logs_left  # pre-seeded log names that survived

    def comparable(self):
        return (self.runs, self.log, self.marker, self.lock, self.spool, self.logs_left)


class ParityTest(ScratchCase):

    def run_job(self, lib, steps, setup=None):
        """Run the fixture job once per step against `lib` and collect what it did."""
        name = "lib" if lib == str(LIB_SH) else "host"
        job = FIXTURE_JOB_KIT if str(lib) == str(LIB_SH) else FIXTURE_JOB_HOST
        root = self.scratch / name
        log_dir = root / "logs"
        spool_dir = root / "spool"
        log_dir.mkdir(parents=True)
        if setup:
            setup(log_dir)
        seeded = {p.name for p in log_dir.glob("*.log")}

        runs = []
        for step in steps:
            env = self.base_env(
                LOG_DIR=log_dir,
                FIXTURE_SPOOL_DIR=spool_dir,
                SEROTINE_SPOOL_DIR=spool_dir,
                SEROTINE_EMIT_HELPER=FIXTURES / "stub_emit_helper.py",
                SEROTINE_USAGE_FALLBACK_DISABLE="1",
                **step,
            )
            proc = subprocess.run(
                [BASH, str(job)],
                env=env,
                cwd=str(self.scratch),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=60,
            )
            repl = [(str(log_dir), "<LOGDIR>"), (str(root), "<ROOT>"),
                    (str(self.scratch), "<SCRATCH>")]
            stderr = normalize(proc.stderr, repl)
            stdout = normalize(proc.stdout, repl)
            runs.append((proc.returncode, stdout, stderr))

        log = "".join(
            normalize(p.read_text(), [(str(log_dir), "<LOGDIR>"), (str(self.scratch), "<SCRATCH>")])
            for p in sorted(log_dir.glob("*.log")) if p.name not in seeded
        )
        marker_path = log_dir / ".last-run-window"
        marker = marker_path.read_text() if marker_path.exists() else None
        spool_path = spool_dir / "spool.jsonl"
        spool = spool_path.read_bytes() if spool_path.exists() else None
        logs_left = sorted(n for n in seeded if (log_dir / n).exists())
        return Outcome(runs, log, marker, (log_dir / ".run.lock").exists(), spool, logs_left)

    def both(self, steps, setup=None):
        lib = self.run_job(str(LIB_SH), steps, setup)
        host = self.run_job(str(HOST_SH), steps, setup)
        return lib, host

    def assertParity(self, lib, host):
        self.assertEqual(lib.comparable(), host.comparable())

    def test_unset_home_exports_login_fallback(self):
        stub = self.bin / "id"
        stub.write_text("#!/bin/sh\nprintf '%s\\n' fixture-login\n")
        stub.chmod(0o755)
        expected = "/" + "Us" + "ers" + "/fixture-login"
        for lib in (LIB_SH, HOST_SH):
            with self.subTest(lib=lib.name):
                env = self.base_env(
                    HOME=None, TOOL_NAME="fixture",
                    LOG_DIR=self.scratch / lib.stem / "logs",
                )
                proc = self.bash(
                    'set -euo pipefail\nsource "%s"\n' % lib +
                    'launchd_kit_init\n'
                    'python3 -c \'import os; print(os.environ["HOME"])\'\n',
                    env,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, expected + "\n")

    # --- the run guard ---------------------------------------------------------

    def test_daily_window_admits_once_then_skips(self):
        lib, host = self.both([{}, {}])
        self.assertParity(lib, host)
        self.assertEqual([r[0] for r in host.runs], [0, 0])
        self.assertEqual(host.log.count("fixture body start"), 1)
        self.assertTrue(host.marker.startswith("daily:"))
        self.assertIn("WARNING: `claude` does not resolve", host.log)
        self.assertFalse(host.lock)

    def test_force_bypasses_the_window(self):
        lib, host = self.both([{}, {"LK_FORCE": "1"}, {"FIXTURE_JOB_FORCE": "1"}])
        self.assertParity(lib, host)
        self.assertEqual(host.log.count("fixture body start"), 3)

    def test_unknown_interval_fails_closed(self):
        lib, host = self.both([{"FIXTURE_INTERVAL": "hourly"}])
        self.assertParity(lib, host)
        self.assertEqual(host.runs[0][0], 2)
        self.assertIn("unknown interval 'hourly'", host.runs[0][2])
        self.assertNotIn("fixture body start", host.log)

    def test_grace_unparseable_and_zero_fail_closed(self):
        lib, host = self.both([{"LK_SLOT_GRACE": "abc"}, {"LK_SLOT_GRACE": "0s"}])
        self.assertParity(lib, host)
        self.assertEqual([r[0] for r in host.runs], [2, 2])
        self.assertIsNone(host.marker)

    def test_grace_closed_window_skips_without_claiming(self):
        # A one-second grace on a window that opened at midnight is closed.
        lib, host = self.both([{"LK_SLOT_GRACE": "1s"}, {"LK_SLOT_GRACE": "2d"}])
        self.assertParity(lib, host)
        self.assertEqual(host.log.count("fixture body start"), 1)

    def test_future_window_skips(self):
        # A 23:59 window is only "future" before 23:59 New York time, so the
        # test failed when the suite ran inside that minute (one host read
        # the clock after the window opened and ran the body). Pin the epoch
        # both hosts read through `date +%s` to the start of a past day, so
        # the window is future by construction at any wall-clock time. Every
        # other `date` call passes through to the system `date`.
        fake = self.bin / "date"
        fake.write_text(
            "#!/bin/bash\n"
            'if [[ "$*" == "+%s" && -n "${FAKE_EPOCH:-}" ]]; then echo "$FAKE_EPOCH"; exit 0; fi\n'
            'exec /bin/date "$@"\n'
        )
        fake.chmod(0o755)
        past_epoch = "946684800"    # 2000-01-01T00:00:00Z
        lib, host = self.both([{"FIXTURE_AT": "23:59", "FAKE_EPOCH": past_epoch}])
        self.assertParity(lib, host)
        for outcome in (lib, host):
            self.assertNotIn("fixture body start", outcome.log)
            self.assertIsNone(outcome.marker)

    def test_on_success_claim(self):
        steps = [
            {"LK_SLOT_CLAIM": "on-success", "FIXTURE_LOCK_FIRST": "1", "FIXTURE_BODY": "exit 3"},
            {"LK_SLOT_CLAIM": "on-success", "FIXTURE_LOCK_FIRST": "1"},
            {"LK_SLOT_CLAIM": "on-success", "FIXTURE_LOCK_FIRST": "1"},
        ]
        lib, host = self.both(steps)
        self.assertParity(lib, host)
        self.assertEqual([r[0] for r in host.runs], [3, 0, 0])
        self.assertEqual(host.log.count("fixture body start"), 2)
        self.assertTrue(host.marker.startswith("daily:"))

    def test_unknown_claim_mode_falls_back_to_up_front(self):
        lib, host = self.both([{"LK_SLOT_CLAIM": "eventually"}])
        self.assertParity(lib, host)
        self.assertIn("unknown LK_SLOT_CLAIM 'eventually'", host.runs[0][2])
        self.assertIsNotNone(host.marker)

    def test_other_intervals(self):
        steps = [
            {"FIXTURE_INTERVAL": "weekly:mon"},
            {"FIXTURE_INTERVAL": "quarterly"},
            {"FIXTURE_INTERVAL": "slots:0,12"},
            {"FIXTURE_INTERVAL": "slots:0,12"},
        ]
        lib, host = self.both(steps)
        self.assertParity(lib, host)

    def test_slots_ignore_the_time_of_day(self):
        # A slots: interval never reads the time of day, so a junk one is
        # not an error under either library.
        steps = [{"FIXTURE_INTERVAL": "slots:0", "FIXTURE_AT": at} for at in ("noon", "99:99")]
        lib, host = self.both(steps)
        self.assertParity(lib, host)
        self.assertEqual([r[0] for r in host.runs], [0, 0])
        self.assertEqual(host.log.count("fixture body start"), 1)
        self.assertTrue(host.marker.startswith("slot:"))

    def test_unknown_interval_with_a_junk_time_of_day(self):
        lib, host = self.both([{"FIXTURE_INTERVAL": "hourly", "FIXTURE_AT": "noon"}])
        self.assertParity(lib, host)
        self.assertEqual(host.runs[0][0], 2)
        self.assertIn("unknown interval 'hourly'", host.runs[0][2])

    # --- the lock ----------------------------------------------------------------

    def test_held_lock_skips_and_is_left_alone(self):
        lib, host = self.both([{}], setup=lambda d: (d / ".run.lock").mkdir())
        self.assertParity(lib, host)
        self.assertEqual(host.runs[0][0], 0)
        self.assertIn("another run is active", host.runs[0][2])
        self.assertTrue(host.lock)
        self.assertNotIn("fixture body start", host.log)

    def test_stale_lock_is_stolen_and_released(self):
        def stale(d):
            lock = d / ".run.lock"
            lock.mkdir()
            old = time.time() - 3 * 3600
            os.utime(lock, (old, old))

        lib, host = self.both([{}], setup=stale)
        self.assertParity(lib, host)
        self.assertIn("fixture body start", host.log)
        self.assertFalse(host.lock)

    def test_crash_exit_code_passes_through_and_releases_lock(self):
        lib, host = self.both([{"FIXTURE_BODY": "exit 7"}])
        self.assertParity(lib, host)
        self.assertEqual(host.runs[0][0], 7)
        self.assertFalse(host.lock)

    # --- timeout -----------------------------------------------------------------

    def test_hung_body_is_bounded(self):
        started = time.time()
        lib, host = self.both([{"FIXTURE_TIMEOUT": "1", "FIXTURE_BODY": "sleep 30"}])
        self.assertLess(time.time() - started, 40)
        self.assertParity(lib, host)
        self.assertIn(host.runs[0][0], (124, 137))

    # --- require_cmd -------------------------------------------------------------

    def test_require_cmd(self):
        steps = [
            {"FIXTURE_REQUIRE": "bash", "LK_FORCE": "1"},
            {"FIXTURE_REQUIRE": "bash definitely-missing-xyz", "LK_FORCE": "1"},
        ]
        lib, host = self.both(steps)
        self.assertParity(lib, host)
        self.assertEqual([r[0] for r in host.runs], [0, 127])
        self.assertIn("FATAL: required command(s) missing or not executable on PATH: definitely-missing-xyz", host.log)
        self.assertIn("lk_notify: notify script not a readable regular file (<SCRATCH>/no-notifier) — skipping post",
                      host.runs[1][2])

    def test_require_cmd_posts_through_the_notifier(self):
        # An executable notifier that records its arguments and body: once
        # accepting the post, once failing it, once hanging past its bound.
        notifier = self.scratch / "notifier"
        notifier.write_text(
            "#!/bin/bash\n"
            'out="$LOG_DIR/../notified"\n'
            'printf "args:" >>"$out"; printf " [%s]" "$@" >>"$out"; printf "\\nbody:\\n" >>"$out"\n'
            'cat >>"$out"; printf "\\n--\\n" >>"$out"\n'
            '[[ -n "${NOTIFIER_SLEEP:-}" ]] && /bin/sleep "$NOTIFIER_SLEEP"\n'
            'exit "${NOTIFIER_RC:-0}"\n'
        )
        notifier.chmod(0o755)
        missing = {"FIXTURE_REQUIRE": "definitely-missing-xyz", "LK_FORCE": "1",
                   "NOTIFY_SCRIPT": notifier}
        hung = dict(missing, NOTIFIER_SLEEP="30", LK_NOTIFY_TIMEOUT_SECS="1",
                    LK_TIMEOUT_KILL_AFTER="1s")
        lib, host = self.both([missing, dict(missing, NOTIFIER_RC="3"), hung])
        self.assertParity(lib, host)
        self.assertEqual([r[0] for r in host.runs], [127, 127, 127])
        self.assertIn("lk_notify: notifier exceeded 1s and was killed — NO post delivered (swallowed)",
                      host.runs[2][2])
        self.assertNotIn("delivery failed", host.runs[0][2])
        self.assertIn("lk_notify: delivery failed (swallowed) — see local log", host.runs[1][2])

        def posts(name):
            text = (self.scratch / name / "notified").read_text()
            return text.replace(str(self.scratch / name), "<ROOT>")

        self.assertEqual(posts("lib"), posts("host"))
        self.assertEqual(posts("host").count("args: [fixture-job — required command missing] [15548997]"), 3)
        self.assertIn("Missing or not executable on the pinned PATH: `definitely-missing-xyz`", posts("host"))

    # --- telemetry transport -----------------------------------------------------

    def test_spool_write_matches(self):
        lib, host = self.both([{"FIXTURE_PAYLOAD": PAYLOAD}])
        self.assertParity(lib, host)
        self.assertEqual(host.spool, (PAYLOAD + "\n").encode())

    # --- log rotation ------------------------------------------------------------

    def test_rotation(self):
        def seed(d):
            now = time.time()
            for i, age_days in enumerate((90, 5, 4, 3, 2)):
                p = d / ("seed-%d.log" % i)
                p.write_text("old\n")
                t = now - age_days * 86400
                os.utime(p, (t, t))

        lib, host = self.both([{"LOG_MAX_FILES": "3"}], setup=seed)
        self.assertParity(lib, host)
        self.assertEqual(host.logs_left, ["seed-3.log", "seed-4.log"])



class MalformedScheduleTest(ScratchCase):
    """A schedule the guard cannot evaluate must refuse to run (exit 2) and
    leave the window unclaimed. Job host only: these inputs are outside the
    parity contract."""

    def run_host(self, interval, at):
        log_dir = Path(tempfile.mkdtemp(prefix="logs.", dir=str(self.scratch)))
        env = self.base_env(LOG_DIR=log_dir,
                            FIXTURE_INTERVAL=interval, FIXTURE_AT=at)
        proc = subprocess.run(
            [BASH, str(FIXTURE_JOB_HOST)], env=env, cwd=str(self.scratch),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=60,
        )
        log = "".join(p.read_text() for p in log_dir.glob("*.log"))
        return proc, log, (log_dir / ".last-run-window").exists()

    def assertRefused(self, interval, at, needle):
        proc, log, claimed = self.run_host(interval, at)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn(needle, proc.stderr)
        self.assertIn("refusing to run unguarded", proc.stderr)
        self.assertNotIn("fixture body start", log)
        self.assertFalse(claimed, "a malformed schedule claimed the window")

    def test_bad_time_of_day_refuses(self):
        for at in ("25:00", "23:60", "7", "noon", "07:30x"):
            with self.subTest(at=at):
                self.assertRefused("daily", at, "'%s'" % at)

    def test_bad_time_of_day_refuses_on_every_interval(self):
        for interval in ("weekly:mon", "quarterly"):
            with self.subTest(interval=interval):
                self.assertRefused(interval, "99:99", "'99:99'")

    def test_bad_weekday_refuses(self):
        for dow in ("funday", "1", "", "mo"):
            with self.subTest(dow=dow):
                self.assertRefused("weekly:" + dow, "00:00", "weekday '%s'" % dow)

    def test_bad_slot_list_refuses(self):
        for slots in ("abc", "8,x", "24", ""):
            with self.subTest(slots=slots):
                self.assertRefused("slots:" + slots, "00:00", "slot list '%s'" % slots)

    def test_valid_spellings_still_run(self):
        for interval, at in (("daily", "0:00"), ("daily", "00:0"), ("weekly:Sunday", "00:00"),
                             ("weekly:thurs", "00:00"), ("weekly:SAT", "00:00"), ("slots:0,23", "00:00")):
            with self.subTest(interval=interval, at=at):
                proc, log, claimed = self.run_host(interval, at)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("fixture body start", log)
                self.assertTrue(claimed)

NEW_YORK = ZoneInfo("America/New_York")    # the TZ base_env pins


class LeadingZeroSlotTest(ScratchCase):
    """Slots written with a leading zero (`08`, `09`) are hours in base 10.
    A fake `date` on the pinned PATH reports the hour under test; every other
    `date` call passes through."""

    def setUp(self):
        super().setUp()
        fake = self.bin / "date"
        fake.write_text(
            "#!/bin/bash\n"
            'if [[ "$*" == "+%H" && -n "${FAKE_HOUR:-}" ]]; then echo "$FAKE_HOUR"; exit 0; fi\n'
            'if [[ "$*" == "+%s" && -n "${FAKE_EPOCH:-}" ]]; then echo "$FAKE_EPOCH"; exit 0; fi\n'
            'exec /bin/date "$@"\n'
        )
        fake.chmod(0o755)

    def run_job(self, lib, interval, hour, **extra):
        log_dir = Path(tempfile.mkdtemp(prefix="logs.", dir=str(self.scratch)))
        today = datetime.now(NEW_YORK).strftime("%Y-%m-%d")
        dt = datetime.strptime(f"{today} {hour}:30", "%Y-%m-%d %H:%M").replace(tzinfo=NEW_YORK)
        fake_epoch = extra.pop("FAKE_EPOCH", str(int(dt.timestamp())))
        job = FIXTURE_JOB_KIT if str(lib) == str(LIB_SH) else FIXTURE_JOB_HOST
        env = self.base_env(LOG_DIR=log_dir, FIXTURE_INTERVAL=interval,
                            FAKE_HOUR=hour, FAKE_EPOCH=fake_epoch, **extra)
        proc = subprocess.run(
            [BASH, str(job)], env=env, cwd=str(self.scratch),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=60,
        )
        log = "".join(p.read_text() for p in log_dir.glob("*.log"))
        marker_path = log_dir / ".last-run-window"
        marker = marker_path.read_text().strip() if marker_path.exists() else None
        return proc, log, marker

    def test_slots_08_and_09_run_in_their_hour(self):
        today = datetime.now(NEW_YORK).strftime("%Y-%m-%d")
        for interval, hour, slot in (("slots:08", "08", "08"), ("slots:09", "09", "09"),
                                     ("slots:08", "17", "08"), ("slots:07,08,09", "09", "09"),
                                     ("slots:07,08,09", "08", "08"), ("slots:09,12", "11", "09")):
            with self.subTest(interval=interval, hour=hour):
                proc, log, marker = self.run_job(HOST_SH, interval, hour)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertNotIn("value too great for base", proc.stderr)
                self.assertIn("fixture body start", log)
                self.assertEqual(marker, "slot:%s:%s" % (today, slot))

    def test_slot_08_window_starts_at_0800(self):
        # A grace a minute shorter than the time since midnight has closed a
        # window that opened at midnight, but not one that opened at 08:00.
        # (BSD `date -j -f` fills the unparsed seconds from the clock, so a
        # window start can sit up to 59 s past the minute.)
        now = datetime.now(NEW_YORK).replace(hour=12, minute=0, second=0, microsecond=0)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        grace = max(1, int(now.timestamp() - midnight.timestamp()) - 60)
        proc, log, marker = self.run_job(HOST_SH, "slots:08", "08", LK_SLOT_GRACE="%ds" % grace,
                                         FAKE_EPOCH=str(int(now.timestamp())))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("fixture body start", log, "the slot 08 window was computed from midnight")
        self.assertNotIn("invalid number", proc.stderr)

    def test_slots_before_the_first_slot_skip(self):
        proc, log, marker = self.run_job(HOST_SH, "slots:08,09", "07")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("fixture body start", log)
        self.assertIsNone(marker)

    def test_leading_zero_slots_lib_sh_reads_keep_its_marker(self):
        # lib.sh evaluates `07` (valid octal) and records the slot as written;
        # the job host keeps that marker so a switched job keeps its window.
        lib = self.run_job(str(LIB_SH), "slots:07,12", "10")
        host = self.run_job(str(HOST_SH), "slots:07,12", "10")
        self.assertEqual(lib[0].returncode, 0, lib[0].stderr)
        self.assertEqual(host[0].returncode, 0, host[0].stderr)
        self.assertEqual(lib[2], host[2])
        self.assertTrue(host[2].endswith(":07"))


class FixtureLibraryPinTest(ScratchCase):
    """Each parity wrapper loads only its own library, identified by function names.

    lib-only names are those declare -F reports after sourcing launchd-kit/lib.sh
    alone and not after sourcing job-host/host.sh alone; host-only is the reverse.
    Both sets are derived at runtime, never by regex and never hard-coded: lib.sh
    contains commented module copies and eval, and host.sh defines lk_* aliases
    unless JH_NO_LK_ALIASES is set (that variable is kept out of the environment).

    Each wrapper is run from a scratch copy of the fixtures folder whose
    fixture-job.sh is replaced by a stub that writes declare -F of the current
    shell. The snapshot happens when and only when the wrapper sources its body,
    after every library line, function call, or variable the wrapper ran before
    it. Command text is never matched to decide when to stop. A source added
    inside fixture-job.sh itself is out of scope.

    The kit wrapper must define every lib-only name and no host-only name; the
    host wrapper must define every host-only name and no lib-only name. Both
    derived sets must be non-empty. A missing or empty snapshot fails.
    """

    def _function_names(self, text):
        names = set()
        for line in text.splitlines():
            parts = line.split()
            if len(parts) != 3 or parts[0] != "declare" or parts[1] != "-f":
                continue
            names.add(parts[2])
        return names

    def _names_after_sourcing(self, library):
        proc = self.bash(
            'source "%s"; declare -F' % library.resolve(),
            self.base_env(),
        )
        if proc.returncode != 0:
            self.fail(
                "sourcing %s failed (rc=%s stderr=%r)"
                % (library, proc.returncode, proc.stderr)
            )
        return self._function_names(proc.stdout)

    def _derived_sets(self):
        lib_names = self._names_after_sourcing(LIB_SH)
        host_names = self._names_after_sourcing(HOST_SH)
        lib_only = lib_names - host_names
        host_only = host_names - lib_names
        if not lib_only:
            self.fail("lib-only function set is empty")
        if not host_only:
            self.fail("host-only function set is empty")
        return lib_only, host_only

    def _snapshot_wrapper(self, wrapper_name):
        root = self.scratch / ("pin-" + wrapper_name)
        fixtures = root / "job-host" / "tests" / "fixtures"
        fixtures.parent.mkdir(parents=True)
        shutil.copytree(str(FIXTURES), str(fixtures))
        (root / "launchd-kit").symlink_to(LIB_SH.parent.resolve())
        (root / "job-host" / "host.sh").symlink_to(HOST_SH.resolve())
        snapshot = self.scratch / (wrapper_name + ".declare")
        (fixtures / "fixture-job.sh").write_text('declare -F >"%s"\n' % snapshot)
        wrapper = fixtures / wrapper_name
        proc = subprocess.run(
            [BASH, str(wrapper)],
            env=self.base_env(),
            cwd=str(self.scratch),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=60,
        )
        if not snapshot.is_file():
            self.fail(
                "missing snapshot for %s (rc=%s stderr=%r)"
                % (wrapper_name, proc.returncode, proc.stderr)
            )
        names = self._function_names(snapshot.read_text())
        if not names:
            self.fail(
                "empty snapshot for %s (rc=%s stderr=%r)"
                % (wrapper_name, proc.returncode, proc.stderr)
            )
        return names

    def test_parity_fixtures_source_their_own_libraries(self):
        lib_only, host_only = self._derived_sets()
        kit_names = self._snapshot_wrapper("fixture-job-kit.sh")
        host_names = self._snapshot_wrapper("fixture-job-host.sh")
        missing_lib = lib_only - kit_names
        if missing_lib:
            self.fail(
                "kit wrapper missing lib-only names: %s" % sorted(missing_lib)
            )
        kit_host = host_only & kit_names
        if kit_host:
            self.fail(
                "kit wrapper defines host-only names: %s" % sorted(kit_host)
            )
        missing_host = host_only - host_names
        if missing_host:
            self.fail(
                "host wrapper missing host-only names: %s" % sorted(missing_host)
            )
        host_lib = lib_only & host_names
        if host_lib:
            self.fail(
                "host wrapper defines lib-only names: %s" % sorted(host_lib)
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
