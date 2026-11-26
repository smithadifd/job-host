#!/usr/bin/env python3
"""Offline coverage for the sourceable notify, telemetry and chain modules."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


import pytest

REPO = Path(os.environ.get("SPLIT_TEST_REPO", Path(__file__).resolve().parents[2]))
HOST = REPO / "job-host" / "host.sh"
LIB = REPO / "launchd-kit" / "lib.sh"
SPLIT = REPO / "launchd-kit" / "lib"
HELPER = REPO / "launchd-kit" / "serotine_usage.py"
BASH = "/bin/bash"


class SplitIntegrationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="split-integrations.")
        self.scratch = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def id_stub_dir(self, name: str, mode: str = "name") -> Path:
        """A bin dir whose `id` answers only `id -un`.

        mode "name" prints `name`; "fail" exits 1 with no output; "empty" exits 0
        with no output. Any other argument list exits 64, so a label built from a
        different `id` flag cannot pass by accident.
        """
        bindir = self.scratch / f"idbin-{name}-{mode}"
        bindir.mkdir(exist_ok=True)
        body = {
            "name": f"echo {name}",
            "fail": "exit 1",
            "empty": "exit 0",
        }[mode]
        stub = bindir / "id"
        stub.write_text(f'#!/bin/sh\n[ "$*" = "-un" ] || exit 64\n{body}\n')
        stub.chmod(0o755)
        return bindir

    def run_fixture(
        self,
        entry: Path,
        name: str,
        *,
        pin_emit_helper: bool = True,
        no_lk_aliases: bool = False,
    ):
        root = self.scratch / name
        root.mkdir()
        notifier = root / "notifier"
        notifier.write_text(
            "#!/bin/bash\n"
            'printf "title=%s\\ncolor=%s\\n" "$1" "$2" >"$CAPTURE_DIR/notify"\n'
            'cat >>"$CAPTURE_DIR/notify"\n'
        )
        notifier.chmod(0o755)

        script = r'''
set -uo pipefail
source "$ENTRY"
if [[ "${EXPECT_NO_LK_ALIASES:-0}" == "1" ]]; then
  declare -F lk_notify lk_run_chain lk_emit_run >/dev/null
  declare -F lk_log >/dev/null && exit 51
  declare -F lk_on_exit >/dev/null && exit 52
  declare -F _lk_timeout >/dev/null && exit 53
fi
# Source the split modules explicitly as well: their guards must make this inert.
source "$SPLIT/notify.sh"
source "$SPLIT/telemetry.sh"
source "$SPLIT/chain.sh"

TOOL_NAME=split-fixture
LK_STAMP=2026-10-07_120000
SEROTINE_RUN_START=2026-10-07T12:00:00-04:00
export TOOL_NAME LK_STAMP SEROTINE_RUN_START
_lk_rfc3339() { printf '%s' '2026-10-07T12:00:00-04:00'; }
_lk_rand4() { printf '%s' 'abcd'; }

printf '%s' 'notify-body' | lk_notify 'Fixture title' "$LK_COLOR_GREEN"
lk_enable_run_telemetry
lk_emit_run --status ok --exit-code 0 --operation fixture.operation --trigger manual

LK_CHAIN_MIN_BYTES=1
if ! lk_run_chain first 'exit 7' second 'printf chain-ok' >"$CAPTURE_DIR/chain.out"; then
  exit 91
fi
printf '%s|%s|%s|%s\n' "$LK_CHAIN_SERVED_NAME" "$LK_CHAIN_SERVED_INDEX" \
  "$LK_CHAIN_FELL_BACK" "$LK_CHAIN_FAILURES" >"$CAPTURE_DIR/chain.state"
'''
        env = {
            "HOME": str(root),
            "PATH": f"{self.id_stub_dir('fixtureuser', 'name')}:/usr/bin:/bin:/usr/sbin:/sbin",
            "TMPDIR": str(root),
            "ENTRY": str(entry),
            "SPLIT": str(SPLIT),
            "CAPTURE_DIR": str(root),
            "NOTIFY_SCRIPT": str(notifier),
            "SEROTINE_SPOOL_DIR": str(root / "spool"),
            "SEROTINE_USAGE_FALLBACK_DISABLE": "1",
            "SEROTINE_HOST": "fixture-host",
            "NO_PROXY": "*",
            "no_proxy": "*",
        }
        if pin_emit_helper:
            env["SEROTINE_EMIT_HELPER"] = str(HELPER)
        if no_lk_aliases:
            env["JH_NO_LK_ALIASES"] = "1"
            env["EXPECT_NO_LK_ALIASES"] = "1"
        proc = subprocess.run(
            [BASH, "-c", script], env=env, cwd=str(root), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        record_path = root / "spool" / "spool.jsonl"
        self.assertTrue(
            record_path.is_file(),
            "telemetry should resolve its default emit helper from the entry point",
        )
        return {
            "notify": (root / "notify").read_bytes(),
            "record": record_path.read_bytes(),
            "chain_out": (root / "chain.out").read_bytes(),
            "chain_state": (root / "chain.state").read_bytes(),
        }

    def test_host_integrations_match_lib(self):
        expected = self.run_fixture(LIB, "lib")
        actual = self.run_fixture(HOST, "host")
        self.assertEqual(actual, expected)
        self.assertEqual(actual["notify"], b"title=Fixture title\ncolor=5763719\nnotify-body")
        self.assertEqual(actual["chain_out"], b"chain-ok")
        self.assertEqual(actual["chain_state"], b"second|2|1|first:exit=7\n")

        record = json.loads(actual["record"])
        self.assertEqual(record["serotine.schema"], "serotine.run.v1")
        self.assertEqual(record["serotine.run.id"],
                         "com.fixtureuser.split-fixture/2026-10-07_120000-abcd")
        self.assertEqual(record["serotine.run.status"], "ok")
        self.assertEqual(record["serotine.run.exit_code"], 0)
        self.assertEqual(record["serotine.run.trigger"], "manual")
        self.assertNotIn("serotine.usage", record)

    def test_include_guards_preserve_existing_definitions(self):
        script = r'''
set -uo pipefail
source "$HOST"
lk_notify() { : notify-sentinel; }
source "$SPLIT/notify.sh"
declare -f lk_notify | grep -q notify-sentinel || exit 41
lk_run_chain() { : chain-sentinel; }
source "$SPLIT/chain.sh"
declare -f lk_run_chain | grep -q chain-sentinel || exit 42
lk_emit_run() { : telemetry-sentinel; }
source "$SPLIT/telemetry.sh"
declare -f lk_emit_run | grep -q telemetry-sentinel || exit 43
'''
        env = {
            "HOME": str(self.scratch),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOST": str(HOST),
            "SPLIT": str(SPLIT),
        }
        proc = subprocess.run(
            [BASH, "-c", script], env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_host_resolves_default_telemetry_helper(self):
        actual = self.run_fixture(
            HOST, "host-default-helper", pin_emit_helper=False
        )
        record = json.loads(actual["record"])
        self.assertEqual(record["serotine.run.status"], "ok")

    def test_host_integrations_work_without_lib_aliases(self):
        actual = self.run_fixture(HOST, "host-no-aliases", no_lk_aliases=True)
        self.assertEqual(actual["notify"], b"title=Fixture title\ncolor=5763719\nnotify-body")
        self.assertEqual(actual["chain_out"], b"chain-ok")
        self.assertEqual(actual["chain_state"], b"second|2|1|first:exit=7\n")

    def test_standalone_lib_copy_runs_integrations(self):
        root = self.scratch / "standalone"
        root.mkdir()
        standalone = root / "lib.sh"
        standalone.write_bytes(LIB.read_bytes())

        notifier = root / "notifier"
        notifier.write_text(
            "#!/bin/bash\n"
            'printf "title=%s\\ncolor=%s\\n" "$1" "$2" >"$CAPTURE_DIR/notify"\n'
            'cat >>"$CAPTURE_DIR/notify"\n'
        )
        notifier.chmod(0o755)

        emit_helper = root / "emit-helper.py"
        emit_helper.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "assert sys.argv[1] == 'build-line'\n"
            "print(json.dumps({\n"
            "    'serotine.schema': 'serotine.run.v1',\n"
            "    'serotine.run.status': os.environ['SEROTINE_LINE_STATUS'],\n"
            "    'serotine.run.operation': os.environ['SEROTINE_LINE_OPERATION'],\n"
            "}, sort_keys=True, separators=(',', ':')))\n"
        )
        emit_helper.chmod(0o755)

        script = r'''
set -euo pipefail
source "$LIB_COPY"
declare -F lk_notify lk_run_chain lk_emit_run >/dev/null

printf '%s' 'standalone-body' | lk_notify 'Standalone title' "$LK_COLOR_GREEN"

LK_CHAIN_MIN_BYTES=1
lk_run_chain first 'exit 9' second 'printf standalone-chain' >"$CAPTURE_DIR/chain.out"
printf '%s|%s|%s|%s\n' "$LK_CHAIN_SERVED_NAME" "$LK_CHAIN_SERVED_INDEX" \
  "$LK_CHAIN_FELL_BACK" "$LK_CHAIN_FAILURES" >"$CAPTURE_DIR/chain.state"

TOOL_NAME=standalone-fixture
LK_STAMP=2026-10-07_120000
SEROTINE_RUN_START=2026-10-07T12:00:00-04:00
export TOOL_NAME LK_STAMP SEROTINE_RUN_START
lk_emit_run --status ok --exit-code 0 --operation standalone.operation --trigger manual
'''
        env = {
            "HOME": str(root),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "TMPDIR": str(root),
            "LIB_COPY": str(standalone),
            "CAPTURE_DIR": str(root),
            "NOTIFY_SCRIPT": str(notifier),
            "SEROTINE_SPOOL_DIR": str(root / "spool"),
            "SEROTINE_EMIT_HELPER": str(emit_helper),
            "SEROTINE_USAGE_FALLBACK_DISABLE": "1",
            "SEROTINE_HOST": "fixture-host",
            "NO_PROXY": "*",
            "no_proxy": "*",
        }
        proc = subprocess.run(
            [BASH, "-c", script], env=env, cwd=str(root), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            (root / "notify").read_bytes(),
            b"title=Standalone title\ncolor=5763719\nstandalone-body",
        )
        self.assertEqual((root / "chain.out").read_bytes(), b"standalone-chain")
        self.assertEqual(
            (root / "chain.state").read_bytes(),
            b"second|2|1|first:exit=9\n",
        )
        record = json.loads((root / "spool" / "spool.jsonl").read_bytes())
        self.assertEqual(record["serotine.run.status"], "ok")
        self.assertEqual(record["serotine.run.operation"], "standalone.operation")

    def _emit_label(self, entry: Path, name: str, id_bin: Path, *emit_args: str,
                    env_extra: dict | None = None) -> dict:
        root = self.scratch / name
        root.mkdir()
        script = r'''
set -uo pipefail
source "$ENTRY"
TOOL_NAME=label-fixture
LK_STAMP=2026-10-07_120000
SEROTINE_RUN_START=2026-10-07T12:00:00-04:00
export TOOL_NAME LK_STAMP SEROTINE_RUN_START
_lk_rfc3339() { printf '%s' '2026-10-07T12:00:00-04:00'; }
_lk_rand4() { printf '%s' 'abcd'; }
lk_emit_run --status ok --exit-code 0 --operation label.operation --trigger manual "$@"
'''
        env = {
            "HOME": str(root),
            "PATH": f"{id_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "TMPDIR": str(root),
            "ENTRY": str(entry),
            "SEROTINE_SPOOL_DIR": str(root / "spool"),
            "SEROTINE_EMIT_HELPER": str(HELPER),
            "SEROTINE_USAGE_FALLBACK_DISABLE": "1",
            "SEROTINE_HOST": "fixture-host",
            "NO_PROXY": "*",
            "no_proxy": "*",
        }
        env.update(env_extra or {})
        proc = subprocess.run(
            [BASH, "-c", script, "_", *emit_args], env=env, cwd=str(root), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads((root / "spool" / "spool.jsonl").read_bytes())

    def _label_entries(self) -> dict:
        standalone = self.scratch / "standalone-label"
        standalone.mkdir(exist_ok=True)
        (standalone / "lib.sh").write_bytes(LIB.read_bytes())
        return {"lib": LIB, "host": HOST, "standalone": standalone / "lib.sh"}

    def test_default_label_is_com_user_tool_from_id_un(self):
        for key, entry in self._label_entries().items():
            with self.subTest(entry=key):
                record = self._emit_label(entry, f"label-{key}", self.id_stub_dir("labeluser"))
                self.assertEqual(record["serotine.job.label"], "com.labeluser.label-fixture")
                self.assertEqual(
                    record["serotine.run.id"],
                    "com.labeluser.label-fixture/2026-10-07_120000-abcd",
                )

    def test_default_label_never_has_an_empty_user_segment(self):
        for mode in ("fail", "empty"):
            for key, entry in self._label_entries().items():
                with self.subTest(mode=mode, entry=key):
                    record = self._emit_label(
                        entry, f"label-{mode}-{key}", self.id_stub_dir("ignored", mode)
                    )
                    self.assertEqual(record["serotine.job.label"], "com.unknown.label-fixture")

    def test_explicit_label_and_env_label_win_over_the_default(self):
        for key, entry in self._label_entries().items():
            with self.subTest(entry=key):
                id_bin = self.id_stub_dir("labeluser")
                from_env = self._emit_label(
                    entry, f"label-env-{key}", id_bin,
                    env_extra={"SEROTINE_JOB_LABEL": "org.example.from-env"},
                )
                self.assertEqual(from_env["serotine.job.label"], "org.example.from-env")
                from_arg = self._emit_label(
                    entry, f"label-arg-{key}", id_bin, "--label", "org.example.from-arg",
                    env_extra={"SEROTINE_JOB_LABEL": "org.example.from-env"},
                )
                self.assertEqual(from_arg["serotine.job.label"], "org.example.from-arg")

    def _run_init(self, entry: Path, init_fn: str, name: str, tool: str = "kit-fixture"):
        root = self.scratch / name
        root.mkdir()
        mark = root / "after-init"
        script = r'''
set -euo pipefail
source "$ENTRY"
"$INIT_FN"
printf done >"$MARK"
'''
        env = {
            "HOME": str(root),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "TMPDIR": str(root),
            "ENTRY": str(entry),
            "INIT_FN": init_fn,
            "MARK": str(mark),
            "TOOL_NAME": tool,
            "LOG_DIR": str(root / "logs"),
            "JH_WARN_CMDS": "",
        }
        proc = subprocess.run(
            [BASH, "-c", script], env=env, cwd=str(root), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        logs = sorted((root / "logs").glob("*.log"))
        self.assertEqual(len(logs), 1, proc.stderr)
        lines = logs[0].read_text().splitlines()
        self.assertGreaterEqual(len(lines), 2, f"no kit: line after the banner: {lines}")
        return proc, lines, mark

    def test_init_logs_release_version_after_the_banner(self):
        kit_root = REPO / "launchd-kit"
        version_path = kit_root / "VERSION"
        self.assertTrue(version_path.is_file(), "VERSION missing: expected launchd-kit/VERSION")
        version_bytes = version_path.read_bytes()
        self.assertRegex(
            version_bytes, rb"\A[0-9]+\.[0-9]+\.[0-9]+\n\Z",
            "VERSION release syntax: expected one newline-terminated MAJOR.MINOR.PATCH line",
        )
        version = version_bytes.split(b"\n", 1)[0].decode("ascii")
        for entry, init_fn in (
            (LIB, "launchd_kit_init"),
            (HOST, "jh_init"),
            (HOST, "launchd_kit_init"),
        ):
            with self.subTest(entry=entry.name, init=init_fn):
                proc, lines, mark = self._run_init(
                    entry, init_fn, f"kit-{entry.parent.name}-{init_fn}"
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertTrue(mark.is_file(), "the run did not continue past init")
                self.assertTrue(lines[0].startswith("=== kit-fixture scheduled run: "), lines)
                self.assertEqual(
                    lines[1], f"kit: {kit_root} {version}",
                    "kit line version mismatch: expected VERSION's first line after the kit root",
                )

    def _kit_copy(self, name: str) -> Path:
        """A scratch copy of the kit with no VERSION file; returns the copy's root."""
        kit = self.scratch / name
        (kit / "launchd-kit").mkdir(parents=True)
        (kit / "job-host").mkdir()
        shutil.copy2(LIB, kit / "launchd-kit" / "lib.sh")
        shutil.copytree(SPLIT, kit / "launchd-kit" / "lib")
        shutil.copy2(HOST, kit / "job-host" / "host.sh")
        return kit

    def test_init_logs_unknown_and_continues_when_version_is_unusable(self):
        cases = {
            "missing": None,
            "empty": b"",
            "directory": "dir",
            "no-trailing-newline": b"9.9.9-nonl",
            "first-line-only": b"1.2.3-first\nsecond line\n",
        }
        expected = {
            "missing": "unknown",
            "empty": "unknown",
            "directory": "unknown",
            "no-trailing-newline": "9.9.9-nonl",
            "first-line-only": "1.2.3-first",
        }
        for case, content in cases.items():
            for rel, init_fn in (
                ("launchd-kit/lib.sh", "launchd_kit_init"),
                ("job-host/host.sh", "jh_init"),
            ):
                with self.subTest(case=case, init=init_fn):
                    kit = self._kit_copy(f"copy-{case}-{init_fn}")
                    version = kit / "launchd-kit" / "VERSION"
                    if content == "dir":
                        version.mkdir()
                    elif content is not None:
                        version.write_bytes(content)
                    proc, lines, mark = self._run_init(
                        kit / rel, init_fn, f"run-{case}-{init_fn}"
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertTrue(mark.is_file(), "a bad VERSION aborted the run")
                    self.assertEqual(
                        lines[1], f"kit: {kit / 'launchd-kit'} {expected[case]}"
                    )

    def test_standalone_module_copies_match_source_files(self):
        bundled = LIB.read_text()
        for name in ("notify.sh", "chain.sh", "telemetry.sh"):
            start = f"# BEGIN GENERATED STANDALONE MODULE: lib/{name}\n"
            end = f"# END GENERATED STANDALONE MODULE: lib/{name}\n"
            self.assertIn(start, bundled)
            self.assertIn(end, bundled)
            embedded = bundled.split(start, 1)[1].split(end, 1)[0]
            encoded_lines = embedded.splitlines(keepends=True)
            self.assertTrue(
                all(line.startswith("#|") for line in encoded_lines),
                f"lib.sh's standalone {name} copy must remain comment-encoded",
            )
            decoded = "".join(line[2:] for line in encoded_lines)
            self.assertEqual(
                decoded,
                (SPLIT / name).read_text(),
                f"regenerate lib.sh's standalone {name} copy from lib/{name}",
            )


# These helpers also let adapter suites exercise the same real notifier bytes
# without inheriting a webhook, config file, credential helper or network tool.
def notifier_sandbox(root: Path, repo: Path = REPO):
    root.mkdir(parents=True)
    home = root / "home"
    home.mkdir()
    config = root / "config"
    config.mkdir()
    tmp = root / "tmp"
    tmp.mkdir()
    bindir = root / "bin"
    bindir.mkdir()
    notifier = root / "notifier ; literal.sh"
    shutil.copyfile(repo / "launchd-kit/discord-notify.sh", notifier)
    notifier.chmod(0o644)
    capture = root / "http.jsonl"
    curl = bindir / "curl"
    curl.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "a = sys.argv[1:]\n"
        "assert a[a.index('-X') + 1] == 'POST'\n"
        "assert a[a.index('-X') + 2] == 'https://capture.invalid/hook'\n"
        "pathlib.Path(a[a.index('-o') + 1]).write_text('local response')\n"
        "with open(os.environ['HTTP_CAPTURE'], 'a') as f:\n"
        "    f.write(json.dumps(json.loads(a[a.index('-d') + 1])) + '\\n')\n"
        "print(os.environ.get('HTTP_STATUS', '204'), end='')\n"
    )
    curl.chmod(0o755)
    for name in ("op", "security"):
        tripwire = bindir / name
        tripwire.write_text("#!/bin/sh\nprintf 'credential tripwire\\n' >&2\nexit 97\n")
        tripwire.chmod(0o755)
    env = {
        "HOME": str(home), "XDG_CONFIG_HOME": str(config), "TMPDIR": str(tmp),
        "PATH": f"{bindir}:{Path(sys.executable).parent}:/opt/homebrew/bin:/usr/bin:/bin",
        "NOTIFY_SCRIPT": str(notifier),
        "DISCORD_WEBHOOK_URL": "https://capture.invalid/hook",
        "HTTP_CAPTURE": str(capture),
    }
    assert notifier.stat().st_mode & 0o777 == 0o644
    return notifier, capture, env


@pytest.mark.parametrize("entry", ["split", "host", "standalone"])
@pytest.mark.parametrize("strict", [False, True], ids=["best_effort", "strict"])
@pytest.mark.parametrize("status", ["204", "500"])
def test_nonexecutable_notifier_delivery(tmp_path, entry, strict, status):
    root = tmp_path / "delivery ; literal"
    notifier, capture, env = notifier_sandbox(root)
    kit = root / "kit"
    (kit / "launchd-kit").mkdir(parents=True)
    shutil.copyfile(LIB, kit / "launchd-kit/lib.sh")
    if entry != "standalone":
        shutil.copytree(SPLIT, kit / "launchd-kit/lib")
    if entry == "host":
        (kit / "job-host").mkdir()
        shutil.copyfile(HOST, kit / "job-host/host.sh")
    env.update(
        ENTRY=str(kit / ("job-host/host.sh" if entry == "host" else "launchd-kit/lib.sh")),
        HTTP_STATUS=status, TITLE="Title ; $(false) ' quoted", COLOR="5763719",
        FN="lk_notify_strict" if strict else "lk_notify",
    )
    body = "Body ; $(false) ' quoted\nsecond line"
    proc = subprocess.run(
        [BASH, "-c",
         'source "$ENTRY"\n"$FN" "$TITLE" "$COLOR"\nrc=$?\n'
         'if (( rc == 0 )); then printf success-marker; fi\nexit "$rc"\n'],
        input=body, env=env, text=True, capture_output=True, timeout=15,
    )
    expected_rc = 1 if strict and status == "500" else 0
    assert proc.returncode == expected_rc, proc.stderr
    assert ("success-marker" in proc.stdout) == (expected_rc == 0), proc.stdout
    assert capture.is_file(), f"notifier delivered nothing: {proc.stderr}"
    payloads = [json.loads(line) for line in capture.read_text().splitlines()]
    assert payloads == [{"embeds": [{
        "title": env["TITLE"], "color": 5763719, "description": body,
    }]}], payloads


@pytest.mark.parametrize("entry", ["split", "standalone"])
@pytest.mark.parametrize("strict", [False, True], ids=["best_effort", "strict"])
@pytest.mark.parametrize("target", ["missing", "directory", "unreadable", "default_mirror"])
def test_notifier_file_gate(tmp_path, entry, strict, target):
    root = tmp_path / "gate ; literal"
    notifier, capture, env = notifier_sandbox(root)
    kit = root / "kit"
    kit.mkdir()
    shutil.copyfile(LIB, kit / "lib.sh")
    if entry == "split":
        shutil.copytree(SPLIT, kit / "lib")
    if target == "missing":
        env["NOTIFY_SCRIPT"] = str(root / "no-such-notifier")
    elif target == "directory":
        env["NOTIFY_SCRIPT"] = str(root)
    elif target == "unreadable":
        notifier.chmod(0o000)
        assert not os.access(notifier, os.R_OK), "test principal must not read mode 000"
    else:
        mirror = Path(env["HOME"]) / ".claude/scripts/discord-notify.sh"
        mirror.parent.mkdir(parents=True)
        shutil.copyfile(notifier, mirror)
        mirror.chmod(0o755)  # The live default mirror remains executable.
        del env["NOTIFY_SCRIPT"]
    env["ENTRY"] = str(kit / "lib.sh")
    env["FN"] = "lk_notify_strict" if strict else "lk_notify"
    try:
        proc = subprocess.run(
            [BASH, "-c", 'source "$ENTRY"\n"$FN" "Mirror title" 5763719\n'],
            input="mirror body", env=env, text=True, capture_output=True, timeout=15,
        )
        accepted = target == "default_mirror"
        assert proc.returncode == (1 if strict and not accepted else 0), proc.stderr
        assert capture.exists() == accepted, proc.stderr
        if accepted:
            assert [json.loads(line) for line in capture.read_text().splitlines()] == [
                {"embeds": [{"title": "Mirror title", "color": 5763719,
                             "description": "mirror body"}]}
            ]
    finally:
        notifier.chmod(0o644)


if __name__ == "__main__":
    unittest.main(verbosity=2)
