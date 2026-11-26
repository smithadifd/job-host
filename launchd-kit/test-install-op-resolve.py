#!/usr/bin/env python3
"""Unit tests for install-op-resolve.sh (stdlib unittest, no network, no pytest).

Run with:  python3 launchd-kit/test-install-op-resolve.py

Every test runs the installer against a THROWAWAY HOME created with
tempfile.mkdtemp(). The owner's real configuration is never read or written — the
module refuses to run at all if a fixture home ever resolves to the real one
(see _TempHome), and no test constructs a path under the invoking user's home.

Grouped by the behaviour each class proves, at the script's public boundary
(argv in; exit code, stdout/stderr and the bytes on disk out). Nothing here
reaches inside the script.
"""

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
INSTALLER = os.path.join(_HERE, "install-op-resolve.sh")
CANONICAL = os.path.join(_HERE, "op-resolve.py")

# The one path this suite must never touch. Captured once, compared against
# every fixture home before that home is handed to a subprocess.
_REAL_HOME = os.path.realpath(os.path.expanduser("~"))

MIRROR_RELPATH = os.path.join(".claude", "scripts", "op-resolve.py")


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class _TempHome:
    """A disposable HOME. Refuses to be the real one."""

    def __init__(self, case):
        self.path = tempfile.mkdtemp(prefix="op-resolve-mirror-test-")
        real = os.path.realpath(self.path)
        if real == _REAL_HOME or real.startswith(_REAL_HOME + os.sep):
            shutil.rmtree(self.path, ignore_errors=True)
            raise AssertionError(
                "fixture HOME resolved inside the real home (%s) — refusing to run"
                % _REAL_HOME
            )
        case.addCleanup(shutil.rmtree, self.path, ignore_errors=True)

    @property
    def mirror(self):
        return os.path.join(self.path, MIRROR_RELPATH)

    @property
    def scripts_dir(self):
        return os.path.join(self.path, ".claude", "scripts")


class InstallerCase(unittest.TestCase):
    def home(self):
        return _TempHome(self)

    def run_installer(self, *args, home=None, source=None, expect=None):
        env = dict(os.environ)
        if home is not None:
            env["HOME"] = home.path
        argv = ["bash", INSTALLER, *args]
        if source is not None:
            argv += ["--source", source]
        proc = subprocess.run(argv, capture_output=True, text=True, env=env)
        if expect is not None:
            self.assertEqual(
                proc.returncode, expect,
                "expected exit %s, got %s\nstdout:\n%s\nstderr:\n%s"
                % (expect, proc.returncode, proc.stdout, proc.stderr),
            )
        return proc

    def install_clean(self, home):
        return self.run_installer("--install", home=home, expect=0)

    def drift(self, home, suffix=b"\n# a hand edit nobody recorded\n"):
        """Make the mirror differ from the canonical source; return its bytes."""
        with open(home.mirror, "ab") as fh:
            fh.write(suffix)
        with open(home.mirror, "rb") as fh:
            return fh.read()


class CleanInstall(InstallerCase):
    def test_clean_install_mirrors_the_canonical_bytes_exactly(self):
        home = self.home()
        proc = self.install_clean(home)
        self.assertEqual(
            sha256(home.mirror), sha256(CANONICAL),
            "byte-identity: the staged mirror must be byte-identical to %s"
            % CANONICAL,
        )
        with open(home.mirror, "rb") as a, open(CANONICAL, "rb") as b:
            self.assertEqual(
                a.read(), b.read(),
                "byte-identity: staged mirror bytes differ from the canonical source",
            )
        self.assertIn("staged", proc.stdout)

    def test_the_default_target_is_the_documented_machine_path(self):
        home = self.home()
        self.install_clean(home)
        self.assertTrue(
            os.path.isfile(home.mirror),
            "a clean install must place the mirror at $HOME/%s" % MIRROR_RELPATH,
        )

    def test_the_staged_mirror_is_directly_invocable(self):
        home = self.home()
        self.install_clean(home)
        mode = os.stat(home.mirror).st_mode
        self.assertTrue(
            mode & stat.S_IXUSR,
            "the staged mirror must be executable (it is documented as a command)",
        )

    def test_reinstalling_over_an_identical_mirror_is_a_no_op(self):
        home = self.home()
        self.install_clean(home)
        first = sha256(home.mirror)
        proc = self.run_installer("--install", home=home, expect=0)
        self.assertIn("already in sync", proc.stdout)
        self.assertEqual(
            sha256(home.mirror), first,
            "idempotence: a second install must leave the mirror's bytes unchanged",
        )
        self.assertEqual(
            sha256(home.mirror), sha256(CANONICAL),
            "byte-identity: the mirror must still match the canonical source "
            "after a repeat install",
        )

    def test_a_missing_canonical_source_cannot_be_verified(self):
        home = self.home()
        proc = self.run_installer(
            "--install", home=home,
            source=os.path.join(home.path, "no-such-op-resolve.py"), expect=2,
        )
        self.assertIn("cannot verify", proc.stderr)
        self.assertFalse(
            os.path.exists(home.mirror),
            "nothing may be staged when the canonical source is missing",
        )


class Check(InstallerCase):
    def test_check_reports_a_missing_mirror_and_writes_nothing(self):
        home = self.home()
        proc = self.run_installer("--check", home=home, expect=1)
        self.assertIn("missing", proc.stdout)
        self.assertFalse(
            os.path.exists(home.scripts_dir),
            "--check must be read-only: it created %s" % home.scripts_dir,
        )

    def test_check_passes_on_an_in_sync_mirror(self):
        home = self.home()
        self.install_clean(home)
        proc = self.run_installer("--check", home=home, expect=0)
        self.assertIn("in-sync", proc.stdout)

    def test_check_reports_drift_without_repairing_it(self):
        home = self.home()
        self.install_clean(home)
        drifted = self.drift(home)
        proc = self.run_installer("--check", home=home, expect=1)
        self.assertIn(
            "DRIFT", proc.stderr,
            "drift detection: --check must report a mirror that differs from "
            "the canonical source",
        )
        with open(home.mirror, "rb") as fh:
            self.assertEqual(
                fh.read(), drifted,
                "--check must be read-only: it modified the drifted mirror",
            )

    def test_the_drift_report_names_both_digests(self):
        home = self.home()
        self.install_clean(home)
        self.drift(home)
        proc = self.run_installer("--check", home=home, expect=1)
        self.assertIn(sha256(CANONICAL), proc.stderr)
        self.assertIn(sha256(home.mirror), proc.stderr)


class Drift(InstallerCase):
    def test_a_drifted_mirror_is_reported_and_refused_not_overwritten(self):
        home = self.home()
        self.install_clean(home)
        drifted = self.drift(home)
        proc = self.run_installer("--install", home=home, expect=1)
        self.assertIn(
            "DRIFT", proc.stderr,
            "drift detection: an --install over a mirror that differs from the "
            "canonical source must report the drift",
        )
        self.assertIn("refusing to replace", proc.stderr)
        with open(home.mirror, "rb") as fh:
            self.assertEqual(
                fh.read(), drifted,
                "drift detection: the drifted mirror was silently overwritten "
                "instead of being refused",
            )

    def test_forcing_reports_the_drift_before_replacing_and_keeps_the_old_bytes(self):
        home = self.home()
        self.install_clean(home)
        drifted = self.drift(home)
        proc = self.run_installer("--install", "--force", home=home, expect=0)
        self.assertIn(
            "DRIFT", proc.stderr,
            "drift detection: a forced install must still report the drift",
        )
        self.assertEqual(
            sha256(home.mirror), sha256(CANONICAL),
            "byte-identity: the forced install must leave the mirror "
            "byte-identical to the canonical source",
        )
        backups = [
            name for name in os.listdir(home.scripts_dir)
            if name.startswith("op-resolve.py.drift-")
        ]
        self.assertEqual(len(backups), 1, "the drifted bytes must be preserved")
        with open(os.path.join(home.scripts_dir, backups[0]), "rb") as fh:
            self.assertEqual(fh.read(), drifted)

    def test_the_drift_report_precedes_any_write_to_the_mirror(self):
        """Ordering, proven by making the replacement itself impossible.

        The scripts directory is made unwritable, so every write path the
        forced install could take fails. If the report were emitted after the
        replacement, a run that never gets to replace anything would print no
        report at all — so the report's presence here IS the ordering proof.
        """
        home = self.home()
        self.install_clean(home)
        drifted = self.drift(home)
        os.chmod(home.scripts_dir, 0o500)
        self.addCleanup(os.chmod, home.scripts_dir, 0o700)
        proc = self.run_installer("--install", "--force", home=home, expect=2)
        self.assertIn(
            "DRIFT", proc.stderr,
            "report-before-replace: the drift report must be emitted BEFORE "
            "the replacement is attempted — no report was seen on a run whose "
            "replacement could not happen",
        )
        self.assertIn("nothing was replaced", proc.stderr)
        with open(home.mirror, "rb") as fh:
            self.assertEqual(
                fh.read(), drifted,
                "report-before-replace: the mirror changed on a run that could "
                "not complete its replacement",
            )

    def test_a_symlinked_mirror_is_refused_and_never_followed(self):
        home = self.home()
        os.makedirs(home.scripts_dir)
        elsewhere = os.path.join(home.path, "elsewhere.py")
        with open(elsewhere, "w", encoding="utf-8") as fh:
            fh.write("# not the resolver\n")
        os.symlink(elsewhere, home.mirror)
        proc = self.run_installer("--install", "--force", home=home, expect=2)
        self.assertIn("symlink", proc.stderr)
        with open(elsewhere, encoding="utf-8") as fh:
            self.assertEqual(
                fh.read(), "# not the resolver\n",
                "a symlinked mirror must never be written through",
            )


class RealHomeIsUntouched(InstallerCase):
    def test_no_fixture_run_ever_names_the_real_home(self):
        home = self.home()
        self.assertNotEqual(os.path.realpath(home.path), _REAL_HOME)
        proc = self.install_clean(home)
        combined = proc.stdout + proc.stderr
        self.assertNotIn(
            os.path.join(_REAL_HOME, ".claude"), combined,
            "the installer touched a path under the real home",
        )


if __name__ == "__main__":
    unittest.main()
