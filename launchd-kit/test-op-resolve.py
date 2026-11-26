#!/usr/bin/env python3
"""Unit tests for op-resolve.py (stdlib unittest, no network, no pytest).

Run with:  python3 launchd-kit/test-op-resolve.py
       or:  cd launchd-kit && python3 test-op-resolve.py

The module under test has a hyphen in its filename (`op-resolve.py`), which is
not a valid Python identifier, so we load it via importlib.util from this
file's own directory and bind it as `opr`.
"""

import importlib.util
import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error

# --- load the hyphenated module -------------------------------------------- #
_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.path.join(_HERE, "op-resolve.py")
_spec = importlib.util.spec_from_file_location("op_resolve", _MOD_PATH)
opr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(opr)


# --- a fake Connect server implemented at the _http_get seam ---------------- #
class FakeConnect:
    """Serves canned JSON keyed by URL suffix; records calls; can inject errors."""

    def __init__(self):
        self.calls = []
        # URL-suffix -> response (dict/list) OR a callable() that returns/raises.
        self.routes = {}

    def route(self, suffix, response):
        self.routes[suffix] = response

    def __call__(self, url, token):
        self.calls.append((url, token))
        for suffix, response in self.routes.items():
            if url.endswith(suffix):
                if callable(response):
                    return response()
                return response
        raise opr.ResolveError("no fake route for %s" % opr._safe_url(url))


VAULTS = [{"id": "vvvvvvvvvvvvvvvvvvvvvvvvvv", "name": "Automation"}]
ITEMS = [{"id": "iiiiiiiiiiiiiiiiiiiiiiiiii", "title": "webhook"}]
ITEM_FULL = {
    "id": "iiiiiiiiiiiiiiiiiiiiiiiiii",
    "title": "webhook",
    "sections": [{"id": "sect1", "label": "extra"}],
    "fields": [
        {"id": "password", "purpose": "PASSWORD", "value": "s3cr3t-value"},
        {"id": "username", "purpose": "USERNAME", "value": "alice"},
        {"id": "urlfld", "label": "url", "value": "https://hook.example/xyz"},
        {"id": "sfld", "label": "token", "section": {"id": "sect1"},
         "value": "sectioned-token"},
    ],
}


def wire_happy(fc):
    fc.route("/v1/vaults", VAULTS)
    fc.route("/items", ITEMS)
    fc.route("/items/iiiiiiiiiiiiiiiiiiiiiiiiii", ITEM_FULL)


class RefParseTests(unittest.TestCase):
    def test_three_part(self):
        self.assertEqual(
            opr.parse_ref("op://Automation/webhook/url"),
            ("Automation", "webhook", None, "url"),
        )

    def test_four_part_section(self):
        self.assertEqual(
            opr.parse_ref("op://Automation/webhook/extra/token"),
            ("Automation", "webhook", "extra", "token"),
        )

    def test_not_op_ref(self):
        with self.assertRaises(opr.ResolveError):
            opr.parse_ref("https://not-op")

    def test_malformed_too_few(self):
        with self.assertRaises(opr.ResolveError):
            opr.parse_ref("op://Automation/webhook")

    def test_malformed_empty_segment(self):
        with self.assertRaises(opr.ResolveError):
            opr.parse_ref("op://Automation//url")


class EnvFileParseTests(unittest.TestCase):
    def test_basic_comments_and_blanks(self):
        text = "# comment\n\nFOO=bar\n  # indented comment\nBAZ=qux\n"
        self.assertEqual(
            opr.parse_env_file(text), [("FOO", "bar"), ("BAZ", "qux")]
        )

    def test_export_prefix(self):
        self.assertEqual(
            opr.parse_env_file("export FOO=bar\n"), [("FOO", "bar")]
        )

    def test_double_quotes_stripped(self):
        self.assertEqual(
            opr.parse_env_file('FOO="a value"\n'), [("FOO", "a value")]
        )

    def test_single_quotes_stripped(self):
        self.assertEqual(
            opr.parse_env_file("FOO='a value'\n"), [("FOO", "a value")]
        )

    def test_op_ref_value_preserved(self):
        self.assertEqual(
            opr.parse_env_file("HOOK=op://Automation/webhook/url\n"),
            [("HOOK", "op://Automation/webhook/url")],
        )

    def test_line_without_equals_ignored(self):
        self.assertEqual(opr.parse_env_file("no_equals_here\nFOO=bar\n"),
                         [("FOO", "bar")])

    def test_value_with_embedded_equals(self):
        self.assertEqual(opr.parse_env_file("URL=a=b=c\n"), [("URL", "a=b=c")])


class ResolveRefTests(unittest.TestCase):
    def setUp(self):
        self.fc = FakeConnect()
        wire_happy(self.fc)
        self._orig = opr._http_get
        opr._http_get = self.fc

    def tearDown(self):
        opr._http_get = self._orig

    def test_success_by_label(self):
        v = opr.resolve_ref("op://Automation/webhook/url", "http://h", "tok")
        self.assertEqual(v, "https://hook.example/xyz")

    def test_success_by_purpose_password(self):
        v = opr.resolve_ref("op://Automation/webhook/password", "http://h", "tok")
        self.assertEqual(v, "s3cr3t-value")

    def test_success_by_purpose_username(self):
        v = opr.resolve_ref("op://Automation/webhook/username", "http://h", "tok")
        self.assertEqual(v, "alice")

    def test_success_with_section(self):
        v = opr.resolve_ref(
            "op://Automation/webhook/extra/token", "http://h", "tok"
        )
        self.assertEqual(v, "sectioned-token")

    def test_vault_by_uuid_skips_vault_list(self):
        # UUID vault should not require the /v1/vaults listing call.
        opr.resolve_ref(
            "op://vvvvvvvvvvvvvvvvvvvvvvvvvv/webhook/url", "http://h", "tok"
        )
        self.assertFalse(
            any(u.endswith("/v1/vaults") for u, _ in self.fc.calls),
            "vault list should be skipped when vault is a UUID",
        )

    def test_missing_field(self):
        with self.assertRaises(opr.ResolveError) as cm:
            opr.resolve_ref("op://Automation/webhook/nope", "http://h", "tok")
        self.assertIn("field not found", str(cm.exception))

    def test_missing_item(self):
        with self.assertRaises(opr.ResolveError) as cm:
            opr.resolve_ref("op://Automation/ghost/url", "http://h", "tok")
        self.assertIn("item not found", str(cm.exception))

    def test_missing_vault(self):
        with self.assertRaises(opr.ResolveError) as cm:
            opr.resolve_ref("op://Ghost/webhook/url", "http://h", "tok")
        self.assertIn("vault not found", str(cm.exception))


class HttpLayerTests(unittest.TestCase):
    """Exercise the real _http_get retry/timeout behavior via a fake urlopen."""

    def setUp(self):
        self._orig_urlopen = opr.urllib.request.urlopen

    def tearDown(self):
        opr.urllib.request.urlopen = self._orig_urlopen

    def test_404_fails_fast_no_retry(self):
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            err = urllib.error.HTTPError(
                req.full_url, 404, "Not Found", {}, io.BytesIO(b"")
            )
            err.close()
            raise err

        opr.urllib.request.urlopen = fake_urlopen
        with self.assertRaises(opr.ResolveError) as cm:
            opr._http_get("http://h/v1/vaults", "tok")
        self.assertIn("404", str(cm.exception))
        self.assertEqual(calls["n"], 1, "404 must not be retried")

    def test_timeout_retries_once_then_fails(self):
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            raise TimeoutError("timed out")

        opr.urllib.request.urlopen = fake_urlopen
        with self.assertRaises(opr.ResolveError):
            opr._http_get("http://h/v1/vaults", "tok")
        self.assertEqual(calls["n"], 2, "timeout should attempt exactly twice (1 retry)")

    def test_5xx_retries_once(self):
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            err = urllib.error.HTTPError(
                req.full_url, 503, "Service Unavailable", {}, io.BytesIO(b"")
            )
            err.close()
            raise err

        opr.urllib.request.urlopen = fake_urlopen
        with self.assertRaises(opr.ResolveError):
            opr._http_get("http://h/v1/vaults", "tok")
        self.assertEqual(calls["n"], 2, "5xx should be retried once")


class ResolveEnvTests(unittest.TestCase):
    def setUp(self):
        self.fc = FakeConnect()
        wire_happy(self.fc)
        self._orig = opr._http_get
        opr._http_get = self.fc

    def tearDown(self):
        opr._http_get = self._orig

    def test_passthrough_non_op_unchanged(self):
        pairs = [("PLAIN", "just-a-value"), ("NUM", "42")]
        out = opr.resolve_env(pairs, "http://h", "tok")
        self.assertEqual(out, {"PLAIN": "just-a-value", "NUM": "42"})

    def test_mixed_resolution(self):
        pairs = [
            ("PLAIN", "keepme"),
            ("HOOK", "op://Automation/webhook/url"),
        ]
        out = opr.resolve_env(pairs, "http://h", "tok")
        self.assertEqual(out["PLAIN"], "keepme")
        self.assertEqual(out["HOOK"], "https://hook.example/xyz")

    def test_partial_failure_raises_and_names_key_not_value(self):
        # One ref resolves, another fails -> whole thing fails closed.
        pairs = [
            ("GOOD", "op://Automation/webhook/url"),
            ("BAD", "op://Automation/webhook/nope"),
        ]
        with self.assertRaises(opr.ResolveError) as cm:
            opr.resolve_env(pairs, "http://h", "tok")
        msg = str(cm.exception)
        self.assertIn("BAD", msg)
        self.assertIn("op://Automation/webhook/nope", msg)
        # The resolved GOOD value must never leak in the failure message.
        self.assertNotIn("https://hook.example/xyz", msg)

    def test_failure_message_never_contains_secret_value(self):
        pairs = [("BAD", "op://Automation/webhook/nope")]
        with self.assertRaises(opr.ResolveError) as cm:
            opr.resolve_env(pairs, "http://h", "tok")
        self.assertNotIn("s3cr3t-value", str(cm.exception))


class MainTests(unittest.TestCase):
    """End-to-end main() wiring without exec'ing a real child unnecessarily."""

    def setUp(self):
        self.fc = FakeConnect()
        wire_happy(self.fc)
        self._orig = opr._http_get
        opr._http_get = self.fc
        self._env = dict(os.environ)
        os.environ["OP_CONNECT_HOST"] = "http://h"
        os.environ["OP_CONNECT_TOKEN"] = "tok"

    def tearDown(self):
        opr._http_get = self._orig
        os.environ.clear()
        os.environ.update(self._env)

    def _write_envfile(self, text):
        import tempfile
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_no_command_exits_nonzero(self):
        path = self._write_envfile("HOOK=op://Automation/webhook/url\n")
        with self.assertRaises(SystemExit) as cm:
            opr.main(["--env-file", path])
        self.assertNotEqual(cm.exception.code, 0)

    def test_missing_host_exits_nonzero(self):
        del os.environ["OP_CONNECT_HOST"]
        path = self._write_envfile("PLAIN=x\n")
        with self.assertRaises(SystemExit) as cm:
            opr.main(["--env-file", path, "--", "true"])
        self.assertNotEqual(cm.exception.code, 0)

    def test_partial_failure_does_not_exec_child(self):
        # If any ref fails, execvpe must never be called.
        exec_called = {"n": 0}

        def boom_exec(*a, **k):
            exec_called["n"] += 1

        orig_exec = opr.os.execvpe
        opr.os.execvpe = boom_exec
        self.addCleanup(setattr, opr.os, "execvpe", orig_exec)

        path = self._write_envfile(
            "GOOD=op://Automation/webhook/url\nBAD=op://Automation/webhook/nope\n"
        )
        with self.assertRaises(SystemExit) as cm:
            opr.main(["--env-file", path, "--", "true"])
        self.assertNotEqual(cm.exception.code, 0)
        self.assertEqual(exec_called["n"], 0, "child must NOT be exec'd on partial failure")

    def test_success_execs_child_with_resolved_env(self):
        captured = {}

        def fake_exec(file, args, env):
            captured["file"] = file
            captured["args"] = list(args)
            captured["env"] = dict(env)
            raise SystemExit(0)  # stand in for process replacement

        orig_exec = opr.os.execvpe
        opr.os.execvpe = fake_exec
        self.addCleanup(setattr, opr.os, "execvpe", orig_exec)

        path = self._write_envfile(
            "PLAIN=keepme\nHOOK=op://Automation/webhook/url\n"
        )
        with self.assertRaises(SystemExit) as cm:
            opr.main(["--env-file", path, "--", "printenv", "HOOK"])
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(captured["file"], "printenv")
        self.assertEqual(captured["args"], ["printenv", "HOOK"])
        self.assertEqual(captured["env"]["HOOK"], "https://hook.example/xyz")
        self.assertEqual(captured["env"]["PLAIN"], "keepme")


class KeychainFallbackCliTests(unittest.TestCase):
    """Exercise the public CLI with a stubbed `security` command only."""

    def test_absent_token_names_every_location_checked(self):
        with tempfile.TemporaryDirectory() as td:
            security = os.path.join(td, "security")
            with open(security, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\nexit 44\n")
            os.chmod(security, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            env_file = os.path.join(td, "job.env")
            with open(env_file, "w", encoding="utf-8") as fh:
                fh.write("PLAIN=x\n")
            env = dict(os.environ)
            env.pop("OP_CONNECT_TOKEN", None)
            env["OP_CONNECT_HOST"] = "http://unused.invalid"
            env["PATH"] = td + os.pathsep + env.get("PATH", "")

            proc = subprocess.run(
                [sys.executable, _MOD_PATH, "--env-file", env_file, "--", "true"],
                capture_output=True,
                text=True,
                env=env,
            )

        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(
            proc.stderr,
            "op-resolve: OP_CONNECT_TOKEN could not be found in the process "
            "environment or Keychain item op-connect-token\n",
        )

    def test_non_item_not_found_probe_failure_is_indeterminate(self):
        with tempfile.TemporaryDirectory() as td:
            security = os.path.join(td, "security")
            with open(security, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\nexit 45\n")
            os.chmod(security, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            env_file = os.path.join(td, "job.env")
            with open(env_file, "w", encoding="utf-8") as fh:
                fh.write("PLAIN=x\n")
            env = dict(os.environ)
            env.pop("OP_CONNECT_TOKEN", None)
            env["OP_CONNECT_HOST"] = "http://unused.invalid"
            env["PATH"] = td + os.pathsep + env.get("PATH", "")

            proc = subprocess.run(
                [sys.executable, _MOD_PATH, "--env-file", env_file, "--", "true"],
                capture_output=True,
                text=True,
                env=env,
            )

        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(
            proc.stderr,
            "op-resolve: could not determine whether OP_CONNECT_TOKEN exists in "
            "Keychain item op-connect-token because the Keychain lookup failed\n",
        )

    def test_present_token_reports_that_its_value_could_not_be_read(self):
        with tempfile.TemporaryDirectory() as td:
            security = os.path.join(td, "security")
            with open(security, "w", encoding="utf-8") as fh:
                fh.write(
                    "#!/bin/sh\n"
                    "case \" $* \" in\n"
                    "  *' -w '*) exit 45 ;;\n"
                    "  *) exit 0 ;;\n"
                    "esac\n"
                )
            os.chmod(security, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            env_file = os.path.join(td, "job.env")
            with open(env_file, "w", encoding="utf-8") as fh:
                fh.write("PLAIN=x\n")
            env = dict(os.environ)
            env.pop("OP_CONNECT_TOKEN", None)
            env["OP_CONNECT_HOST"] = "http://unused.invalid"
            env["PATH"] = td + os.pathsep + env.get("PATH", "")

            proc = subprocess.run(
                [sys.executable, _MOD_PATH, "--env-file", env_file, "--", "true"],
                capture_output=True,
                text=True,
                env=env,
            )

        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(
            proc.stderr,
            "op-resolve: OP_CONNECT_TOKEN exists in Keychain item "
            "op-connect-token but its value could not be read\n",
        )

    def test_token_removed_before_read_reports_absent(self):
        with tempfile.TemporaryDirectory() as td:
            security = os.path.join(td, "security")
            with open(security, "w", encoding="utf-8") as fh:
                fh.write(
                    "#!/bin/sh\n"
                    "case \" $* \" in\n"
                    "  *' -w '*) exit 44 ;;\n"
                    "  *) exit 0 ;;\n"
                    "esac\n"
                )
            os.chmod(security, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            env_file = os.path.join(td, "job.env")
            with open(env_file, "w", encoding="utf-8") as fh:
                fh.write("PLAIN=x\n")
            env = dict(os.environ)
            env.pop("OP_CONNECT_TOKEN", None)
            env["OP_CONNECT_HOST"] = "http://unused.invalid"
            env["PATH"] = td + os.pathsep + env.get("PATH", "")

            proc = subprocess.run(
                [sys.executable, _MOD_PATH, "--env-file", env_file, "--", "true"],
                capture_output=True,
                text=True,
                env=env,
            )

        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(
            proc.stderr,
            "op-resolve: OP_CONNECT_TOKEN could not be found in the process "
            "environment or Keychain item op-connect-token\n",
        )

    def test_keychain_token_trailing_newline_is_stripped(self):
        with tempfile.TemporaryDirectory() as td:
            security = os.path.join(td, "security")
            with open(security, "w", encoding="utf-8") as fh:
                fh.write(
                    "#!/bin/sh\n"
                    "case \" $* \" in\n"
                    "  *' -w '*) printf 'credential-sentinel\\n' ;;\n"
                    "  *) exit 0 ;;\n"
                    "esac\n"
                )
            os.chmod(security, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            old_path = os.environ.get("PATH")
            os.environ["PATH"] = td + os.pathsep + (old_path or "")
            try:
                status, token = opr._keychain_connect_token()
            finally:
                if old_path is None:
                    os.environ.pop("PATH", None)
                else:
                    os.environ["PATH"] = old_path

        self.assertEqual(status, "resolved")
        self.assertEqual(token, "credential-sentinel")

    def test_whitespace_only_keychain_token_is_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            security = os.path.join(td, "security")
            with open(security, "w", encoding="utf-8") as fh:
                fh.write(
                    "#!/bin/sh\n"
                    "case \" $* \" in\n"
                    "  *' -w '*) printf '   \\n' ;;\n"
                    "  *) exit 0 ;;\n"
                    "esac\n"
                )
            os.chmod(security, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            env_file = os.path.join(td, "job.env")
            with open(env_file, "w", encoding="utf-8") as fh:
                fh.write("PLAIN=x\n")
            env = dict(os.environ)
            env.pop("OP_CONNECT_TOKEN", None)
            env["OP_CONNECT_HOST"] = "http://unused.invalid"
            env["PATH"] = td + os.pathsep + env.get("PATH", "")

            proc = subprocess.run(
                [sys.executable, _MOD_PATH, "--env-file", env_file, "--", "true"],
                capture_output=True,
                text=True,
                env=env,
            )

        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertNotIn("credential-sentinel", proc.stderr)
        self.assertEqual(
            proc.stderr,
            "op-resolve: OP_CONNECT_TOKEN exists in Keychain item "
            "op-connect-token but its value could not be read\n",
        )

    def test_keychain_fallback_executes_child_without_emitting_credential(self):
        with tempfile.TemporaryDirectory() as td:
            security = os.path.join(td, "security")
            calls = os.path.join(td, "security.calls")
            with open(security, "w", encoding="utf-8") as fh:
                fh.write(
                    "#!/bin/sh\n"
                    "printf '%%s\\n' \"$*\" >> %s\n"
                    "case \" $* \" in\n"
                    "  *' -w '*) printf 'credential-sentinel' ;;\n"
                    "  *) exit 0 ;;\n"
                    "esac\n" % calls
                )
            os.chmod(security, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            env_file = os.path.join(td, "job.env")
            with open(env_file, "w", encoding="utf-8") as fh:
                fh.write("PLAIN=x\n")
            env = dict(os.environ)
            env.pop("OP_CONNECT_TOKEN", None)
            env["OP_CONNECT_HOST"] = "http://unused.invalid"
            env["PATH"] = td + os.pathsep + env.get("PATH", "")

            proc = subprocess.run(
                [sys.executable, _MOD_PATH, "--env-file", env_file, "--",
                 sys.executable, "-c",
                 "import os; assert 'OP_CONNECT_TOKEN' not in os.environ"],
                capture_output=True,
                text=True,
                env=env,
            )
            with open(calls, encoding="utf-8") as fh:
                call_lines = fh.readlines()

        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("credential-sentinel", proc.stdout)
        self.assertNotIn("credential-sentinel", proc.stderr)
        self.assertEqual(len(call_lines), 2)


if __name__ == "__main__":
    unittest.main()
