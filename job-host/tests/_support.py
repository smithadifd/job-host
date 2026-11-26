"""Shared helpers for the job-host suites (stdlib only, Python 3.9+)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
JOB_HOST = TESTS.parent
REPO = JOB_HOST.parent
HOST_SH = JOB_HOST / "host.sh"
LIB_SH = REPO / "launchd-kit" / "lib.sh"
FIXTURES = TESTS / "fixtures"
FIXTURE_JOB = FIXTURES / "fixture-job.sh"

# The shell launchd runs these jobs under on macOS.
BASH = "/bin/bash"

# What the fixture job's pinned PATH holds: a scratch bin with python3 (the
# interpreter running this suite), then the system directories. No `claude`, so
# the init warning fires identically under both libraries.
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


class ScratchCase(unittest.TestCase):
    """A test case with a private scratch directory and HOME."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="job-host-test.")
        self.scratch = Path(self._tmp.name)
        self.home = self.scratch / "home"
        self.home.mkdir()
        self.bin = self.scratch / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)

    def tearDown(self):
        self._tmp.cleanup()

    def base_env(self, **extra):
        """A minimal, explicit environment: nothing leaks in from the caller."""
        path = "%s:%s" % (self.bin, SYSTEM_PATH)
        env = {
            "HOME": str(self.home),
            "PATH": path,
            "LAUNCHD_KIT_PATH": path,
            "TZ": "America/New_York",
            "TMPDIR": str(self.scratch),
            "LANG": "C",
            "LC_ALL": "C",
            "NOTIFY_SCRIPT": str(self.scratch / "no-notifier"),
            "no_proxy": "*",
            "NO_PROXY": "*",
        }
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)     # None means "unset this variable"
            else:
                env[key] = str(value)
        return env

    def bash(self, script, env, timeout=60, cwd=None):
        return subprocess.run(
            [BASH, "-c", script],
            env=env,
            cwd=cwd or str(self.scratch),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
        )


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


_TS = re.compile(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]")
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}_\d{6}")
_HOST = re.compile(r"\(host [^)]*\)")


def normalize(text, replacements):
    """Replace run-specific values (paths, timestamps, host) with tokens."""
    for old, new in replacements:
        text = text.replace(old, new)
    text = _TS.sub("[TS]", text)
    text = _STAMP.sub("<STAMP>", text)
    text = _HOST.sub("(host <HOST>)", text)
    return text
