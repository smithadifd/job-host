#!/usr/bin/env python3
"""Unit tests for telemetry/serotine_trace.py — the wrapper-side failure-trace
body builder and its server-compatible payload contract.


Run with:  python3 launchd-kit/test-serotine-trace.py
       or:  cd launchd-kit && python3 test-serotine-trace.py

Stdlib unittest, no pytest (matches test-serotine-gov.py / test-serotine-spool.py
— launchd-kit takes no package-manager dependencies).

THE SOURCE OF TRUTH FOR "VALID PAYLOAD" IS SEROTINE'S OWN SERVER, NOT THIS
EMITTER. Two independent artifacts stand in for it here, so a test can actually
disagree with the builder:

  1. `_server_accepts()` transcribes the acceptance rules of
     serotine `src/lib/traces/store.ts` + `src/app/api/v1/ingest/trace/route.ts`
     with file:line citations (see the function). It is exercised against a
     VENDORED VERBATIM COPY of serotine's own upload fixture
     (telemetry/fixtures/trace/serotine-trace-sample.jsonl = serotine
     test/fixtures/trace-sample.jsonl) as a known-good control, so the checker
     cannot drift into "whatever we emit".
  2. The run id in the header is validated against the VENDORED
     telemetry/serotine-run-v1.schema.json — serotine's own frozen schema,
     already in this repo — not against a pattern retyped here.

Secret material in this file is fake, generated for the redaction tests, and
matches no real credential.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TELEMETRY = os.path.join(_HERE, "telemetry")
sys.path.insert(0, _TELEMETRY)

import serotine_trace  # noqa: E402

_CONTROL_FIXTURE = os.path.join(
    _TELEMETRY, "fixtures", "trace", "serotine-trace-sample.jsonl")
_RUN_SCHEMA = os.path.join(_TELEMETRY, "serotine-run-v1.schema.json")

# serotine src/config/env.ts:256 — SEROTINE_TRACE_MAX_BYTES default (25 MiB).
SERVER_MAX_BYTES = 25 * 1024 * 1024


def _server_accepts(body, run_id, max_bytes=SERVER_MAX_BYTES):
    """Every acceptance rule serotine applies to a trace upload, transcribed
    from the server with citations. Returns [] when the upload would be stored,
    otherwise the list of reasons it would be rejected.

    ingest/trace/route.ts:60  — a missing run_id query param is a 422.
    traces/store.ts:107       — an empty body is a 422.
    traces/store.ts:110       — a body over maxBytes is a 413.
    traces/store.ts:45,59-62  — the run id must flatten its single '/' to one
                                path component matching [A-Za-z0-9._-]+ and not
                                be '.'/'..', else 422.
    traces/store.ts:181-184   — lineCount counts non-blank lines; a body whose
                                lines are not JSON still stores, but the archive
                                is documented as raw NDJSON
                                (route.ts:9-12, README.md § Trace archive), so
                                this checker holds us to real NDJSON.
    """
    reasons = []
    if not run_id:
        reasons.append("missing run_id (route.ts:60 → 422)")
    if len(body) == 0:
        reasons.append("empty body (store.ts:107 → 422)")
    if len(body) > max_bytes:
        reasons.append("over cap (store.ts:110 → 413)")
    flat = (run_id or "").replace("/", "_")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", flat) or flat in (".", ".."):
        reasons.append("run id does not flatten to a safe component "
                       "(store.ts:45,59-62 → 422)")
    lines = [ln for ln in body.decode("utf-8", "replace").split("\n") if ln.strip()]
    if not lines:
        reasons.append("no non-blank lines (store.ts:181-184 → lineCount 0)")
    for i, ln in enumerate(lines, 1):
        try:
            json.loads(ln)
        except Exception as exc:
            reasons.append("line %d is not JSON (README § Trace archive: raw "
                           "NDJSON): %s" % (i, exc))
            break
    return reasons


def _run_id_pattern():
    """serotine.run.id pattern read out of the VENDORED serotine-run-v1 schema
    (telemetry/serotine-run-v1.schema.json) — not retyped here."""
    with open(_RUN_SCHEMA, "r", encoding="utf-8") as fh:
        schema = json.load(fh)
    return schema["properties"]["serotine.run.id"]["pattern"]


def _lines(body):
    return [json.loads(ln) for ln in body.decode("utf-8").split("\n") if ln.strip()]


def _write_log(text):
    fd, path = tempfile.mkstemp(suffix=".log")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


BASE = dict(
    run_id="com.example.ay7-probe/2026-08-18_120000-abcd",
    job_label="com.example.ay7-probe",
    host="host-a",
    status="fail",
    exit_code=74,
)


class TestControlFixture(unittest.TestCase):
    """The checker must accept serotine's OWN fixture. If this fails the
    checker is wrong, not the emitter."""

    def test_serotine_own_fixture_is_accepted(self):
        with open(_CONTROL_FIXTURE, "rb") as fh:
            body = fh.read()
        self.assertEqual(
            _server_accepts(body, "example-job/2026-01-15_030001-7f3a"), [])

    def test_checker_rejects_what_the_server_rejects(self):
        rid = "example-job/2026-01-15_030001-7f3a"
        self.assertTrue(_server_accepts(b"", rid))                  # 422 empty
        self.assertTrue(_server_accepts(b"{}\n", rid, max_bytes=1))  # 413
        self.assertTrue(_server_accepts(b"{}\n", ""))               # 422 no run id
        self.assertTrue(_server_accepts(b"{}\n", "bad label/x y"))  # 422 unsafe
        self.assertTrue(_server_accepts(b"not json\n", rid))        # not NDJSON


class TestPayloadShape(unittest.TestCase):
    def test_body_would_be_stored_by_the_server(self):
        path = _write_log("line one\nline two\nboom: it died\n")
        body = serotine_trace.build_body(path, **BASE)
        self.assertEqual(_server_accepts(body, BASE["run_id"]), [], body)

    def test_run_id_matches_the_vendored_run_v1_schema_pattern(self):
        path = _write_log("boom\n")
        body = serotine_trace.build_body(path, **BASE)
        header = _lines(body)[0]
        self.assertRegex(header["serotine.run.id"], _run_id_pattern())

    def test_header_carries_the_triage_facts(self):
        path = _write_log("a\nb\nboom: it died\n")
        body = serotine_trace.build_body(path, **BASE)
        header = _lines(body)[0]
        self.assertEqual(header["type"], "system")
        self.assertEqual(header["subtype"], "launchd_kit_trace")
        self.assertEqual(header["serotine.job.label"], BASE["job_label"])
        self.assertEqual(header["serotine.run.status"], "fail")
        self.assertEqual(header["serotine.run.exit_code"], 74)
        self.assertEqual(header["trace.source"], path)

    def test_log_lines_are_carried_verbatim_and_in_order(self):
        path = _write_log("alpha\nbravo\ncharlie\n")
        body = serotine_trace.build_body(path, **BASE)
        texts = [o["text"] for o in _lines(body) if o["type"] == "log"]
        self.assertEqual(texts, ["alpha", "bravo", "charlie"])

    def test_control_characters_and_undecodable_bytes_stay_valid_ndjson(self):
        fd, path = tempfile.mkstemp(suffix=".log")
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"tab\there\n\x1b[31mred\x1b[0m\n\xff\xfe binary\n\x00nul\n")
        body = serotine_trace.build_body(path, **BASE)
        self.assertEqual(_server_accepts(body, BASE["run_id"]), [], body)
        self.assertNotIn(b"\x00", body)


class TestLastN(unittest.TestCase):
    def test_captures_only_the_last_n_lines(self):
        path = _write_log("".join("line %d\n" % i for i in range(1, 501)))
        body = serotine_trace.build_body(path, lines=10, **BASE)
        texts = [o["text"] for o in _lines(body) if o["type"] == "log"]
        self.assertEqual(len(texts), 10)
        self.assertEqual(texts[0], "line 491")
        self.assertEqual(texts[-1], "line 500")

    def test_header_flags_truncation_and_counts(self):
        path = _write_log("".join("line %d\n" % i for i in range(1, 501)))
        header = _lines(serotine_trace.build_body(path, lines=10, **BASE))[0]
        self.assertTrue(header["trace.truncated"])
        self.assertEqual(header["trace.lines_captured"], 10)
        short = _write_log("only\n")
        self.assertFalse(_lines(serotine_trace.build_body(short, **BASE))[0]["trace.truncated"])


class TestByteCap(unittest.TestCase):
    def test_cap_is_enforced_and_the_body_stays_valid(self):
        path = _write_log("".join("x" * 200 + "\n" for _ in range(400)))
        body = serotine_trace.build_body(path, max_bytes=4096, **BASE)
        self.assertLessEqual(len(body), 4096)
        self.assertEqual(_server_accepts(body, BASE["run_id"]), [], body)
        self.assertEqual(_lines(body)[0]["subtype"], "launchd_kit_trace")

    def test_cap_keeps_the_MOST_RECENT_lines(self):
        path = _write_log("".join("line %d\n" % i for i in range(1, 2001)))
        body = serotine_trace.build_body(path, max_bytes=2048, **BASE)
        texts = [o["text"] for o in _lines(body) if o["type"] == "log"]
        self.assertEqual(texts[-1], "line 2000")


class TestRedaction(unittest.TestCase):
    """The privacy gate. Every secret below is fake."""

    def test_exact_env_values_never_survive(self):
        token = "sk-fake-INGEST-TOKEN-9c1f4b7a2e"
        hook = "https://discord.com/api/webhooks/123456789/FAKEwebhookSECRETvalue0001"
        path = _write_log(
            "posting with %s\ncurl -H 'Authorization: Bearer %s' %s\n" % (token, token, hook))
        env = {"SEROTINE_INGEST_TOKEN": token, "DISCORD_WEBHOOK_URL": hook}
        body = serotine_trace.build_body(path, env=env, **BASE)
        self.assertNotIn(token.encode(), body)
        self.assertNotIn(b"FAKEwebhookSECRETvalue0001", body)
        self.assertIn(b"[redacted:SEROTINE_INGEST_TOKEN]", body)

    def test_explicitly_named_env_var_is_redacted_even_with_an_innocent_name(self):
        val = "correct-horse-battery-staple"
        path = _write_log("value was %s\n" % val)
        body = serotine_trace.build_body(
            path, env={"MY_THING": val}, redact_env=["MY_THING"], **BASE)
        self.assertNotIn(val.encode(), body)

    def test_short_env_values_are_not_used_as_redaction_needles(self):
        # A 1-char value must never blank out every occurrence of that char.
        path = _write_log("alpha bravo charlie\n")
        body = serotine_trace.build_body(path, env={"API_KEY": "a"}, **BASE)
        self.assertIn(b"alpha bravo charlie", body)

    def test_credential_shapes_are_redacted_without_any_env_help(self):
        secrets = {
            "github": "gh" "p_FAKE0000000000000000000000000000abcd",
            "github_pat": "github_pat_FAKE00000000000000000_aaaaaaaaaaaaaaaaaaaaaaaa",
            "anthropic": "sk-ant-api03-FAKE000000000000000000000000",
            "slack": "xo" "xb-0000000000-FAKE0000000000-aaaaaaaaaaaaaaaa",
            "aws": "AK" "IAFAKE000000000000",
            "google": "AIzaFAKE0000000000000000000000000000000",
            "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.FAKEsignature00000",
        }
        path = _write_log("\n".join("%s=%s" % kv for kv in secrets.items()) + "\n")
        body = serotine_trace.build_body(path, **BASE)
        for name, val in secrets.items():
            self.assertNotIn(val.encode(), body, name)

    def test_authorization_header_credential_is_redacted_scheme_kept(self):
        path = _write_log("Authorization: Bearer aVeryLongOpaqueCredential123456\n")
        body = serotine_trace.build_body(path, **BASE)
        self.assertNotIn(b"aVeryLongOpaqueCredential123456", body)
        self.assertIn(b"Bearer", body)

    def test_assignment_shapes_are_redacted(self):
        path = _write_log(
            'PASSWORD=hunter2hunter2\n'
            'export MY_API_KEY="k-FAKE-000111222333"\n'
            '{"webhook_url": "https://example.invalid/FAKE-0001112223"}\n'
            '--api-key s3cr3tFAKEvalue999\n')
        body = serotine_trace.build_body(path, **BASE)
        for needle in (b"hunter2hunter2", b"k-FAKE-000111222333",
                       b"FAKE-0001112223", b"s3cr3tFAKEvalue999"):
            self.assertNotIn(needle, body)

    def test_auth_named_assignments_are_redacted_layer_2c(self):
        # `auth` is excluded from LAYER 1's needle list (SSH_AUTH_SOCK is a
        # path), but layer 2c redacts one occurrence in place, so it carries
        # the broader list. These two shapes are the ones the narrower list
        # let through.
        path = _write_log(
            "AUTH=s3cr3tFAKEauthvalue\n"
            "SEROTINE_BASIC_AUTH=dXNlcjpGQUtFcGFzcw==\n")
        body = serotine_trace.build_body(path, **BASE)
        self.assertNotIn(b"s3cr3tFAKEauthvalue", body)
        self.assertNotIn(b"dXNlcjpGQUtFcGFzcw==", body)

    def test_auth_stays_out_of_layer_1s_global_needle_list(self):
        # The OTHER half of the same decision: an `auth`-named env var must
        # NOT become a global search-and-replace needle, or one socket path
        # blanks out every line that mentions it. Pinning both halves keeps a
        # later reader from collapsing the two predicates back together.
        sock = "/private/tmp/com.apple.launchd.7Kd9/Listeners"
        path = _write_log("cwd is %s and the job wrote %s/out\n" % (sock, sock))
        body = serotine_trace.build_body(path, env={"SSH_AUTH_SOCK": sock}, **BASE)
        self.assertIn(sock.encode(), body)

    def test_layer_2c_widening_does_not_eat_the_authorization_scheme(self):
        # Layer 2b keeps the scheme (`Bearer`) on purpose. Widening 2c must not
        # then match the header's own name and swallow it.
        path = _write_log("Authorization: Bearer aVeryLongOpaqueCredential123456\n")
        body = serotine_trace.build_body(path, **BASE)
        self.assertNotIn(b"aVeryLongOpaqueCredential123456", body)
        self.assertIn(b"Bearer", body)

    def test_pem_private_key_block_is_redacted(self):
        path = _write_log(
            "-----BEGIN OPENSSH PRIVATE " "KEY-----\n"
            "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2g=\n"
            "-----END OPENSSH PRIVATE KEY-----\n")
        body = serotine_trace.build_body(path, **BASE)
        self.assertNotIn(b"b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ", body)

    def test_pem_begin_line_becomes_the_exact_redaction_marker(self):
        # Pins the exact text the redactor writes in place of a PEM
        # private-key BEGIN line. The source spells that marker as two
        # adjacent literals; a wrong split (a lost space) must go red here.
        # The expected value is built from fragments split elsewhere, so
        # this file carries no whole key header of its own.
        path = _write_log(
            "-----BEGIN OPENSSH PRIVATE " "KEY-----\n"
            "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2g=\n"
            "-----END OPENSSH PRIVATE KEY-----\n")
        body = serotine_trace.build_body(path, **BASE)
        texts = [o["text"] for o in _lines(body) if o["type"] == "log"]
        expected = "-----BEGIN PRIVATE" + " KEY-----" + " " + "[redacted]"
        self.assertEqual(texts[0], expected)

    def test_op_references_are_kept_they_are_diagnostics_not_secrets(self):
        path = _write_log("resolving op://Automation/dependabot-sweep/webhook\n")
        body = serotine_trace.build_body(path, **BASE)
        self.assertIn(b"op://Automation/dependabot-sweep/webhook", body)

    def test_ordinary_log_text_survives_redaction(self):
        path = _write_log("gh pr list --state open\nexit status 74: fetch failed\n")
        body = serotine_trace.build_body(path, **BASE)
        self.assertIn(b"exit status 74: fetch failed", body)

    def test_redaction_count_is_reported_in_the_header(self):
        token = "sk-fake-INGEST-TOKEN-9c1f4b7a2e"
        path = _write_log("%s\n%s\n" % (token, token))
        header = _lines(serotine_trace.build_body(
            path, env={"SEROTINE_INGEST_TOKEN": token}, **BASE))[0]
        self.assertGreaterEqual(header["trace.redactions"], 2)
        self.assertEqual(header["trace.redactor"], serotine_trace.REDACTOR_VERSION)

    def test_extra_operator_pattern_is_applied(self):
        path = _write_log("internal-id ACME-9931 leaked\n")
        body = serotine_trace.build_body(
            path, redact_pattern=r"ACME-\d+", **BASE)
        self.assertNotIn(b"ACME-9931", body)

    def test_a_broken_operator_pattern_fails_closed(self):
        path = _write_log("boom\n")
        body = serotine_trace.build_body(path, redact_pattern="([unclosed", **BASE)
        self.assertEqual(body, b"")


class TestNeverRaisesFailsClosed(unittest.TestCase):
    def test_missing_log_returns_empty_not_an_exception(self):
        self.assertEqual(serotine_trace.build_body("/nope/missing.log", **BASE), b"")

    def test_empty_log_returns_empty_so_the_caller_never_posts_a_422(self):
        self.assertEqual(serotine_trace.build_body(_write_log(""), **BASE), b"")
        self.assertEqual(serotine_trace.build_body(_write_log("\n\n  \n"), **BASE), b"")

    def test_unreadable_log_returns_empty(self):
        path = _write_log("boom\n")
        os.chmod(path, 0o000)
        try:
            self.assertEqual(serotine_trace.build_body(path, **BASE), b"")
        finally:
            os.chmod(path, 0o600)

    def test_a_directory_in_place_of_the_log_returns_empty(self):
        self.assertEqual(serotine_trace.build_body(tempfile.mkdtemp(), **BASE), b"")


class TestCli(unittest.TestCase):
    """The form lib.sh actually invokes: env in, NDJSON on stdout, and the
    secret NEVER on argv (argv is world-readable via ps)."""

    def _run(self, env, args=("build-body",)):
        e = dict(os.environ)
        e.pop("SEROTINE_TRACE_REDACT_ENV", None)
        e.pop("SEROTINE_TRACE_REDACT_PATTERN", None)
        e.update(env)
        return subprocess.run(
            [sys.executable, os.path.join(_TELEMETRY, "serotine_trace.py")] + list(args),
            env=e, capture_output=True)

    def _base_env(self, path):
        return {
            "SEROTINE_TRACE_LOG": path,
            "SEROTINE_LINE_RUN_ID": BASE["run_id"],
            "SEROTINE_LINE_JOB_LABEL": BASE["job_label"],
            "SEROTINE_LINE_HOST": BASE["host"],
            "SEROTINE_LINE_STATUS": "fail",
            "SEROTINE_LINE_EXIT_CODE": "74",
        }

    def test_cli_emits_a_storable_body(self):
        path = _write_log("a\nb\nboom\n")
        r = self._run(self._base_env(path))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_server_accepts(r.stdout, BASE["run_id"]), [], r.stdout)

    def test_cli_redacts_from_its_own_environment(self):
        token = "sk-fake-CLI-TOKEN-77aa11bb22"
        path = _write_log("used %s here\n" % token)
        env = self._base_env(path)
        env["SEROTINE_INGEST_TOKEN"] = token
        r = self._run(env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(token.encode(), r.stdout)

    def test_cli_honors_lines_and_max_bytes(self):
        path = _write_log("".join("line %d\n" % i for i in range(1, 101)))
        env = self._base_env(path)
        env["SEROTINE_TRACE_LINES"] = "3"
        r = self._run(env)
        texts = [o["text"] for o in _lines(r.stdout) if o["type"] == "log"]
        self.assertEqual(texts, ["line 98", "line 99", "line 100"])

    def test_cli_writes_nothing_on_a_missing_log(self):
        env = self._base_env("/nope/missing.log")
        r = self._run(env)
        self.assertEqual(r.stdout, b"")


class TestBuildFailure(unittest.TestCase):
    """`build_failure` — the serotine.run.failure object (wave row BS8).

    Same module, same redactor, a different destination: three strings that
    ride the run.v1 LINE instead of a separate trace upload. The bounds it
    applies are serotine's own display bounds; the members it omits are the
    ones the wire's `pattern: "\\S"` would refuse.
    """

    def test_all_three_members_from_a_log_plus_a_message(self):
        path = _write_log("first\nsecond\n\nboom: it broke\n")
        out = serotine_trace.build_failure(path, message="job died")
        self.assertEqual(out["message"], "job died")
        self.assertEqual(out["last_log_line"], "boom: it broke")
        self.assertEqual(out["stderr_tail"], "first\nsecond\nboom: it broke")

    def test_blank_lines_are_dropped_and_do_not_become_last_log_line(self):
        path = _write_log("real content\n\n   \n\n")
        out = serotine_trace.build_failure(path)
        self.assertEqual(out["last_log_line"], "real content")

    def test_a_message_alone_survives_an_unreadable_log(self):
        out = serotine_trace.build_failure("/nope/missing.log", message="only this")
        self.assertEqual(out, {"message": "only this"})

    def test_nothing_to_say_is_an_empty_object_not_a_blank_member(self):
        self.assertEqual(serotine_trace.build_failure("", message=""), {})
        self.assertEqual(serotine_trace.build_failure(_write_log("\n  \n"), message="   "), {})

    def test_lines_bounds_the_tail_to_the_last_n(self):
        path = _write_log("".join("line %d\n" % i for i in range(1, 101)))
        out = serotine_trace.build_failure(path, lines=3)
        self.assertEqual(out["stderr_tail"], "line 98\nline 99\nline 100")
        self.assertEqual(out["last_log_line"], "line 100")

    def test_members_are_bounded_at_serotines_own_display_limits(self):
        long_line = "x" * 5000
        path = _write_log(long_line + "\n")
        out = serotine_trace.build_failure(path, message="m" * 2000)
        self.assertEqual(len(out["message"]), serotine_trace.FAILURE_MESSAGE_CHARS)
        self.assertEqual(len(out["last_log_line"]),
                         serotine_trace.FAILURE_LAST_LOG_LINE_CHARS)
        self.assertEqual(len(out["stderr_tail"]),
                         serotine_trace.FAILURE_STDERR_TAIL_CHARS)

    def test_message_keeps_its_head_and_stderr_tail_keeps_its_end(self):
        path = _write_log("".join("line %d\n" % i for i in range(1, 2001)))
        out = serotine_trace.build_failure("HEAD" + "." * 1000, message="HEAD" + "." * 1000)
        self.assertTrue(out["message"].startswith("HEAD"))
        out = serotine_trace.build_failure(path, lines=1000)
        self.assertTrue(out["stderr_tail"].endswith("line 2000"))

    def test_the_redactor_runs_on_the_message_as_well_as_the_log(self):
        token = "sk-fake-FORENSICS-9c1f4b7a2e"
        path = _write_log("posting with %s now\n" % token)
        env = {"SEROTINE_INGEST_TOKEN": token}
        out = serotine_trace.build_failure(path, message="failed using %s" % token, env=env)
        self.assertNotIn(token, json.dumps(out))
        self.assertIn("[redacted:SEROTINE_INGEST_TOKEN]", out["message"])
        self.assertIn("[redacted:SEROTINE_INGEST_TOKEN]", out["stderr_tail"])

    def test_a_nul_in_the_log_neither_splits_the_object_nor_reaches_the_wire(self):
        """The message/tail split uses a NUL sentinel; a NUL in the log (a torn
        write, a binary blob) must not be mistaken for it, and must not ride out
        to a store that re-reads these strings verbatim."""
        path = _write_log("before\x00after\nboom\n")
        out = serotine_trace.build_failure(path, message="msg")
        self.assertEqual(out["message"], "msg")
        self.assertEqual(out["last_log_line"], "boom")
        self.assertIn("beforeafter", out["stderr_tail"])
        self.assertNotIn("\x00", json.dumps(out))

    def test_a_nul_in_the_message_cannot_forge_the_sentinel(self):
        path = _write_log("boom\n")
        out = serotine_trace.build_failure(path, message="a\x00b")
        self.assertEqual(out["message"], "ab")
        self.assertEqual(out["last_log_line"], "boom")

    def test_an_unusable_operator_pattern_fails_closed_to_nothing(self):
        path = _write_log("boom\n")
        self.assertEqual(
            serotine_trace.build_failure(path, message="m", redact_pattern="(unclosed"),
            {})

    def test_a_directory_in_place_of_the_log_still_yields_the_message(self):
        out = serotine_trace.build_failure(tempfile.mkdtemp(), message="still here")
        self.assertEqual(out, {"message": "still here"})

    def test_cli_build_failure_prints_one_json_object(self):
        path = _write_log("a\nboom\n")
        e = dict(os.environ)
        e.pop("SEROTINE_TRACE_REDACT_ENV", None)
        e.pop("SEROTINE_TRACE_REDACT_PATTERN", None)
        e.update({"SEROTINE_FAILURE_LOG": path, "SEROTINE_FAILURE_MESSAGE": "cli msg"})
        r = subprocess.run(
            [sys.executable, os.path.join(_TELEMETRY, "serotine_trace.py"), "build-failure"],
            env=e, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout.decode())["message"], "cli msg")

    def test_cli_build_failure_prints_nothing_when_there_is_nothing_to_say(self):
        e = dict(os.environ)
        e.update({"SEROTINE_FAILURE_LOG": "/nope/missing.log",
                  "SEROTINE_FAILURE_MESSAGE": ""})
        r = subprocess.run(
            [sys.executable, os.path.join(_TELEMETRY, "serotine_trace.py"), "build-failure"],
            env=e, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, b"")

    def test_build_body_is_untouched_by_this_row(self):
        """The trace lane and the forensics lane share a module, not a state."""
        path = _write_log("a\nb\nboom\n")
        self.assertEqual(_server_accepts(serotine_trace.build_body(path, **BASE),
                                         BASE["run_id"]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
