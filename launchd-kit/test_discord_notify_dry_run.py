from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
DISCORD_SCRIPT = Path(os.environ.get("DISCORD_NOTIFY_SCRIPT_UNDER_TEST", HERE / "discord-notify.sh"))
SECRET_WEBHOOK = "https://discord.invalid/api/webhooks/SECRET_WEBHOOK_VALUE"
SECRET_TOKEN = "SECRET_SERVICE_TOKEN_VALUE"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def safe_env(tmp_path: Path) -> tuple[dict[str, str], dict[str, Path]]:
    stubs = tmp_path / "bin"
    stubs.mkdir()
    markers = {
        "curl": tmp_path / "curl-called",
        "op": tmp_path / "op-called",
        "secrets": tmp_path / "secrets-sourced",
        "timeout": tmp_path / "timeout-lib-sourced",
    }
    _write_executable(
        stubs / "curl",
        "#!/usr/bin/env bash\nprintf called > \"$CURL_MARKER\"\nexit 1\n",
    )
    _write_executable(
        stubs / "op",
        "#!/usr/bin/env bash\nprintf called > \"$OP_MARKER\"\nexit 1\n",
    )
    secrets = tmp_path / "notifier.env"
    secrets.write_text(
        f"printf sourced > {shlex.quote(str(markers['secrets']))}\n"
        f"DISCORD_WEBHOOK_URL={shlex.quote(SECRET_WEBHOOK)}\n"
        f"OP_SERVICE_ACCOUNT_TOKEN={shlex.quote(SECRET_TOKEN)}\n"
        "OWNER_DISCORD_ID=123456789012345678\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "CURL_MARKER": str(markers["curl"]),
        "OP_MARKER": str(markers["op"]),
        "WEBHOOK_ENV_FILE": str(secrets),
        "NOTIFY_OWNER_ENV_FILE": str(secrets),
        "NOTIFY_SCRIPT": str(DISCORD_SCRIPT),
    }
    for name in (
        "DISCORD_NOTIFY_DRY_RUN",
        "NOTIFY_OWNER_DRY_RUN",
        "NEEDS_ANDREW_DRY_RUN",
        "DISCORD_WEBHOOK_URL",
        "NOTIFY_OWNER_WEBHOOK_URL",
        "NEEDS_ANDREW_WEBHOOK_URL",
        "OP_SERVICE_ACCOUNT_TOKEN",
    ):
        env.pop(name, None)
    return env, markers


def _run(script: Path, arguments: list[str], env: dict[str, str], body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *arguments],
        input=body,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def _assert_dry_run(result: subprocess.CompletedProcess[str], markers: dict[str, Path], *payload_text: str) -> None:
    assert not markers["curl"].exists(), "dry-run called curl"
    assert not markers["op"].exists(), "dry-run called op"
    assert not markers["secrets"].exists(), "dry-run sourced the secrets file"
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert (
        "discord-notify: dry-run webhook=<redacted> token=<redacted> payload="
        in result.stderr
    ), "dry-run output did not redact webhook and token"
    for text in payload_text:
        assert text in result.stderr
    assert SECRET_WEBHOOK not in result.stderr, "dry-run printed the webhook secret"
    assert SECRET_TOKEN not in result.stderr, "dry-run printed the token secret"


def test_discord_notify_dry_run_renders_direct_payload(
    safe_env: tuple[dict[str, str], dict[str, Path]],
) -> None:
    env, markers = safe_env
    env["DISCORD_NOTIFY_DRY_RUN"] = "1"
    env["OP_SERVICE_ACCOUNT_TOKEN"] = SECRET_TOKEN

    result = _run(
        DISCORD_SCRIPT,
        ["--content", "probe content", "Probe title", "3066993"],
        env,
        "probe body",
    )

    _assert_dry_run(result, markers, "probe content", "Probe title", "probe body")


def test_discord_notify_dry_run_precedes_timeout_library_source(
    safe_env: tuple[dict[str, str], dict[str, Path]],
    tmp_path: Path,
) -> None:
    env, markers = safe_env
    script_dir = tmp_path / "script"
    script_dir.mkdir()
    copied_script = script_dir / DISCORD_SCRIPT.name
    shutil.copy2(DISCORD_SCRIPT, copied_script)
    (script_dir / "lib.sh").write_text(
        f"printf sourced > {shlex.quote(str(markers['timeout']))}\n"
        '_lk_timeout() { shift; "$@"; }\n',
        encoding="utf-8",
    )
    env["DISCORD_NOTIFY_DRY_RUN"] = "1"

    dry_result = _run(copied_script, ["Probe title"], env, "probe body")

    _assert_dry_run(dry_result, markers, "probe body")
    assert not markers["timeout"].exists(), "dry-run sourced the timeout library"

    env.pop("DISCORD_NOTIFY_DRY_RUN")
    normal_result = _run(copied_script, ["Probe title"], env, "probe body")

    assert markers["timeout"].exists(), "normal run did not source the timeout library"
    assert markers["curl"].exists(), "normal run did not reach curl"
    assert normal_result.returncode != 0


@pytest.mark.parametrize(
    "dry_value",
    [None, "", "0", "true", "yes", "11"],
    ids=["unset", "empty", "zero", "true", "yes", "eleven"],
)
def test_discord_notify_non_dry_values_reach_curl(
    safe_env: tuple[dict[str, str], dict[str, Path]],
    dry_value: str | None,
) -> None:
    env, markers = safe_env
    if dry_value is not None:
        env["DISCORD_NOTIFY_DRY_RUN"] = dry_value

    result = _run(DISCORD_SCRIPT, ["Probe title"], env, "probe body")

    assert markers["curl"].exists(), "non-1 dry-run value did not reach curl"
    # The legacy env file is read as data: its webhook reaches curl, and its
    # shell line never runs.
    assert not markers["secrets"].exists(), "non-dry run executed its legacy env file"
    assert not markers["op"].exists(), "literal test webhook unexpectedly called op"
    assert result.returncode != 0
