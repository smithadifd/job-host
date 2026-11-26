#!/usr/bin/env python3
"""Secrets: jh_with_secrets over launchd-kit/op-resolve.py, imported by jh_secrets.py.

The resolver is the real one; 1Password Connect is a local stub server on
127.0.0.1 that serves one item and answers 404 (or hangs) for others. The
command under test writes its environment to a file, so "the command never
ran" is checkable as "the file does not exist".
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _support import FIXTURES, HOST_SH, JOB_HOST, ScratchCase, pid_alive  # noqa: E402

VAULT = "v" * 26          # 26-char lowercase ids skip the name lookups
GOOD_ITEM = "g" * 26
MISSING_ITEM = "m" * 26
SLOW_ITEM = "s" * 26
TOKEN = "stub-connect-token"
FIRST_SECRET = "first-secret-value-8c1f"
SECOND_SECRET = "second-secret-value-55aa"


class _ConnectStub(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            self.send_error(401)
            return
        if self.path == "/v1/vaults/%s/items/%s" % (VAULT, GOOD_ITEM):
            body = json.dumps({"fields": [
                {"label": "first", "value": FIRST_SECRET},
                {"label": "second", "value": SECOND_SECRET},
            ]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/v1/vaults/%s/items/%s" % (VAULT, SLOW_ITEM):
            time.sleep(30)
        self.send_error(404)


class _ThreadedServer(ThreadingMixIn, HTTPServer):
    # The slow handler sleeps 30s; never wait for it at shutdown.
    daemon_threads = True
    block_on_close = False


def ref(item, field):
    return "op://%s/%s/%s" % (VAULT, item, field)


class SecretsTest(ScratchCase):

    @classmethod
    def setUpClass(cls):
        cls.server = _ThreadedServer(("127.0.0.1", 0), _ConnectStub)
        cls.host = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        super().setUp()
        self.out = self.scratch / "child-env.txt"
        self.env_file = self.scratch / "job.env"

    def write_env_file(self, *lines):
        self.env_file.write_text("\n".join(lines) + "\n")

    def run_job(self, child=None, host_sh=HOST_SH, **extra):
        """Source host.sh, run jh_with_secrets, then report what the caller's
        own shell can see afterwards."""
        settings = {"OP_CONNECT_HOST": self.host, "OP_CONNECT_TOKEN": TOKEN, "OUT": self.out}
        settings.update(extra)
        env = self.base_env(**settings)
        child = child or 'env > "$OUT"'
        script = (
            'source "%s"\n'
            'jh_with_secrets "%s" /bin/bash -c %s\n'
            'rc=$?\n'
            'echo "rc=$rc"\n'
            'echo "caller-FIRST=${FIRST:-unset}"\n'
            'echo "caller-SECOND=${SECOND:-unset}"\n'
        ) % (host_sh, self.env_file, "'" + child + "'")
        return self.bash(script, env, timeout=90)

    def scratch_tree(self, resolver=None):
        """Copy host.sh and jh_secrets.py into a scratch repo layout and return
        the copied host.sh. `resolver`, when given, is copied in as that tree's
        launchd-kit/op-resolve.py; otherwise the tree has none."""
        tree = self.scratch / "tree"
        (tree / "job-host").mkdir(parents=True)
        for name in ("host.sh", "jh_secrets.py"):
            shutil.copy2(str(JOB_HOST / name), str(tree / "job-host" / name))
        if resolver is not None:
            (tree / "launchd-kit").mkdir()
            shutil.copy2(str(resolver), str(tree / "launchd-kit" / "op-resolve.py"))
        return tree / "job-host" / "host.sh"

    def rc(self, proc):
        lines = [l for l in proc.stdout.splitlines() if l.startswith("rc=")]
        self.assertEqual(len(lines), 1, proc.stdout + proc.stderr)
        return int(lines[0][3:])

    def child_env(self):
        pairs = {}
        for line in self.out.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                pairs[k] = v
        return pairs

    def assertNoSecretLeaked(self, proc):
        for secret in (FIRST_SECRET, SECOND_SECRET):
            self.assertNotIn(secret, proc.stdout)
            self.assertNotIn(secret, proc.stderr)

    # --- success ---------------------------------------------------------------

    def test_all_references_resolve_and_the_command_gets_them(self):
        self.write_env_file(
            "FIRST=" + ref(GOOD_ITEM, "first"),
            "SECOND='%s'" % ref(GOOD_ITEM, "second"),
            "PLAIN=not-a-secret",
        )
        proc = self.run_job()
        self.assertEqual(self.rc(proc), 0, proc.stderr)
        env = self.child_env()
        self.assertEqual(env["FIRST"], FIRST_SECRET)
        self.assertEqual(env["SECOND"], SECOND_SECRET)
        self.assertEqual(env["PLAIN"], "not-a-secret")
        self.assertNotIn("OP_CONNECT_TOKEN", env, "the Connect token must not reach the command")
        self.assertNotIn("OP_CONNECT_HOST", env)
        # Resolution happened in a child process: the caller never held them.
        self.assertIn("caller-FIRST=unset", proc.stdout)
        self.assertNoSecretLeaked(proc)

    def test_command_exit_status_passes_through(self):
        self.write_env_file("FIRST=" + ref(GOOD_ITEM, "first"))
        proc = self.run_job(child="exit 5")
        self.assertEqual(self.rc(proc), 5)

    # --- failure: non-zero, no partial env -------------------------------------

    def test_one_failed_reference_runs_nothing(self):
        # FIRST resolves; SECOND does not. The command must not start with FIRST alone.
        self.write_env_file(
            "FIRST=" + ref(GOOD_ITEM, "first"),
            "SECOND=" + ref(MISSING_ITEM, "second"),
        )
        proc = self.run_job()
        self.assertNotEqual(self.rc(proc), 0)
        self.assertFalse(self.out.exists(), "the command ran with a partial environment")
        self.assertIn("could not resolve %s for SECOND" % ref(MISSING_ITEM, "second"), proc.stderr)
        self.assertIn("caller-FIRST=unset", proc.stdout)
        self.assertIn("caller-SECOND=unset", proc.stdout)
        self.assertNoSecretLeaked(proc)

    def test_missing_field_runs_nothing(self):
        self.write_env_file(
            "FIRST=" + ref(GOOD_ITEM, "first"),
            "SECOND=" + ref(GOOD_ITEM, "no-such-field"),
        )
        proc = self.run_job()
        self.assertNotEqual(self.rc(proc), 0)
        self.assertFalse(self.out.exists())
        self.assertNoSecretLeaked(proc)

    def test_missing_connect_host_runs_nothing(self):
        self.write_env_file("FIRST=" + ref(GOOD_ITEM, "first"))
        proc = self.run_job(OP_CONNECT_HOST=None)
        self.assertEqual(self.rc(proc), 1)
        self.assertIn("OP_CONNECT_HOST is not set", proc.stderr)
        self.assertFalse(self.out.exists())

    def test_unreadable_env_file_runs_nothing(self):
        proc = self.run_job()  # env file never written
        self.assertEqual(self.rc(proc), 1)
        self.assertFalse(self.out.exists())

    def test_hung_resolution_is_bounded_and_runs_nothing(self):
        self.write_env_file(
            "FIRST=" + ref(GOOD_ITEM, "first"),
            "SECOND=" + ref(SLOW_ITEM, "second"),
        )
        started = time.time()
        proc = self.run_job(JH_SECRETS_TIMEOUT_SECS="2")
        self.assertLess(time.time() - started, 15)
        self.assertEqual(self.rc(proc), 124)
        self.assertIn("did not finish within the bound", proc.stderr)
        self.assertFalse(self.out.exists())

    def hanging_security(self):
        """Put a `security` on PATH that hangs, after forking a helper that
        hangs too. Returns a function listing the pids both recorded."""
        pids = self.scratch / "security-pids"
        fake = self.bin / "security"
        fake.write_text(
            "#!/bin/bash\n"
            'echo $$ >> "%s"\n'
            "/bin/sleep 300 &\n"
            'echo $! >> "%s"\n'
            "wait\n" % (pids, pids)
        )
        fake.chmod(0o755)

        def recorded():
            if not pids.exists():
                return []
            return [int(p) for p in pids.read_text().split()]

        def reap():
            for pid in recorded():
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        self.addCleanup(reap)
        return recorded

    def assertNoSurvivors(self, recorded):
        self.assertEqual(len(recorded()), 2, "the fake security never ran")
        deadline = time.time() + 3
        while time.time() < deadline and any(pid_alive(p) for p in recorded()):
            time.sleep(0.05)
        survivors = [p for p in recorded() if pid_alive(p)]
        self.assertEqual(survivors, [], "processes outlived secrets resolution")

    def test_hung_keychain_lookup_is_bounded_and_leaves_no_process(self):
        # With no OP_CONNECT_TOKEN, op-resolve asks `security` for the token.
        # A `security` that hangs must be killed, with everything it started,
        # when the bound fires.
        recorded = self.hanging_security()
        self.write_env_file("FIRST=" + ref(GOOD_ITEM, "first"))
        started = time.time()
        proc = self.run_job(OP_CONNECT_TOKEN=None, JH_SECRETS_TIMEOUT_SECS="2")
        self.assertLess(time.time() - started, 15)
        self.assertEqual(self.rc(proc), 124, proc.stderr)
        self.assertIn("did not finish within the bound", proc.stderr)
        self.assertFalse(self.out.exists())
        self.assertNoSurvivors(recorded)

    def test_terminating_the_wrapper_mid_resolution_leaves_no_process(self):
        recorded = self.hanging_security()
        self.write_env_file("FIRST=" + ref(GOOD_ITEM, "first"))
        env = self.base_env(OP_CONNECT_HOST=self.host, OP_CONNECT_TOKEN=None,
                            JH_SECRETS_TIMEOUT_SECS="120", OUT=self.out)
        proc = subprocess.Popen(
            [sys.executable, str(JOB_HOST / "jh_secrets.py"), "--env-file", str(self.env_file),
             "--", "/bin/bash", "-c", 'env > "$OUT"'],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.time() + 15
            while time.time() < deadline and len(recorded()) < 2:
                time.sleep(0.05)
            self.assertEqual(len(recorded()), 2, "the fake security never ran")
            proc.terminate()
            self.assertEqual(proc.wait(timeout=15), -signal.SIGTERM)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertFalse(self.out.exists())
        self.assertNoSurvivors(recorded)

    def test_resolution_bound_does_not_limit_the_command(self):
        self.write_env_file("FIRST=" + ref(GOOD_ITEM, "first"))
        proc = self.run_job(JH_SECRETS_TIMEOUT_SECS="1", child='sleep 2; env > "$OUT"')
        self.assertEqual(self.rc(proc), 0, proc.stderr)
        self.assertEqual(self.child_env()["FIRST"], FIRST_SECRET)

    def test_invalid_bound_runs_nothing(self):
        self.write_env_file("FIRST=" + ref(GOOD_ITEM, "first"))
        proc = self.run_job(JH_SECRETS_TIMEOUT_SECS="soon")
        self.assertEqual(self.rc(proc), 2)
        self.assertFalse(self.out.exists())

    # --- the import ------------------------------------------------------------

    def test_missing_resolver_module_runs_nothing(self):
        self.write_env_file("FIRST=" + ref(GOOD_ITEM, "first"))
        proc = self.run_job(host_sh=self.scratch_tree())
        self.assertEqual(self.rc(proc), 1)
        self.assertIn("resolver module not found", proc.stderr)
        self.assertFalse(self.out.exists())

    def test_resolver_is_the_imported_module(self):
        sentinel = self.scratch / "stub-called"
        self.write_env_file("FIRST=" + ref(GOOD_ITEM, "first"))
        proc = self.run_job(
            host_sh=self.scratch_tree(resolver=FIXTURES / "stub_resolver.py"),
            STUB_RESOLVER_SENTINEL=sentinel,
        )
        self.assertEqual(self.rc(proc), 0, proc.stderr)
        self.assertTrue(sentinel.exists())
        self.assertTrue(sentinel.read_text().startswith("--env-file %s -- " % self.env_file))
        self.assertEqual(self.child_env()["STUB_RESOLVED"], "from-stub")

    def test_environment_cannot_redirect_the_resolver(self):
        # No variable chooses the module jh_secrets.py imports: a path from the
        # environment would run before the resolution bound is armed.
        sentinel = self.scratch / "stub-called"
        self.write_env_file("FIRST=" + ref(GOOD_ITEM, "first"))
        proc = self.run_job(
            JH_OP_RESOLVE=FIXTURES / "stub_resolver.py",
            STUB_RESOLVER_SENTINEL=sentinel,
        )
        self.assertEqual(self.rc(proc), 0, proc.stderr)
        self.assertFalse(sentinel.exists(), "a module named in the environment was imported")
        env = self.child_env()
        self.assertEqual(env["FIRST"], FIRST_SECRET)
        self.assertNotIn("STUB_RESOLVED", env)

    def test_default_resolver_is_launchd_kit_op_resolve(self):
        sys.path.insert(0, str(JOB_HOST))
        try:
            import jh_secrets
        finally:
            sys.path.pop(0)
        self.assertEqual(
            os.path.realpath(jh_secrets.DEFAULT_RESOLVER),
            os.path.realpath(JOB_HOST.parent / "launchd-kit" / "op-resolve.py"),
        )
        source = (JOB_HOST / "jh_secrets.py").read_text()
        for copied in ("def resolve_ref", "def resolve_env", "def parse_env_file", "def _http_get"):
            self.assertNotIn(copied, source, "the resolver must be imported, not copied")

    def test_usage_error(self):
        env = self.base_env()
        proc = self.bash('source "%s"; jh_with_secrets only-a-file; echo "rc=$?"' % HOST_SH, env)
        self.assertEqual(self.rc(proc), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
