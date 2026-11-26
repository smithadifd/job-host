#!/usr/bin/env python3
"""op-resolve.py — a stdlib-only, pure-HTTP replacement for `op run --env-file`.

Why this exists
---------------
On the host the `op` CLI binary is unusable under launchd for two structural
reasons that compound each other:

  1. **macOS TCC AppData gate.** `op` probes the 1Password *desktop app*
     container to satisfy the "allow access to data from other apps" gate.
     When the display is asleep that probe does not fail fast — it HANGS,
     releasing only when the display wakes. A launchd job parked on it stalls
     for the entire sleep window.

  2. **macOS Local Network Privacy (LNP).** Pointing `op` at a self-hosted
     1Password **Connect** server on the LAN (the NAS) does not help either:
     a launchd-spawned `op` is blocked by LNP from reaching the LAN
     (`no route to host`), and `op` cannot be granted Local-Network access
     through the GUI.

A **pure-Python** resolver structurally clears both: Python already holds the
LNP grant, and by talking to the Connect REST API directly over HTTP it never
touches the desktop-app container, so the TCC AppData gate never engages.

Why stdlib `urllib`, not `connect-sdk-python`
---------------------------------------------
Adopting the official SDK would add a third-party dependency to *every* wrapper
venv on the fleet. This resolver is meant to be dropped in front of arbitrary
jobs, so it deliberately depends on nothing outside the Python standard library.

CLI shape (drop-in for `op run`)
--------------------------------
    op-resolve.py --env-file FILE -- cmd args...

It reads a dotenv-style env-file, resolves every `op://…` value against the
Connect REST API, merges the resolved values over the current environment, and
`execvpe`s the child command (so signals and the exit code pass through
cleanly). It is **fail-closed**: any unresolved reference, HTTP error, missing
env var, or timeout ⇒ a clear message to stderr and a non-zero exit, and the
child is NEVER exec'd with partially-resolved secrets.

Environment contract
---------------------
    OP_CONNECT_HOST   base URL of the Connect server, e.g. http://nas.local:8090
    OP_CONNECT_TOKEN  Connect API token (Bearer). Falls back to the macOS
                      Keychain item op-connect-token when unset.

Secret hygiene: resolved secret VALUES never appear in logs, stderr, argv, or
exception text. Failures name the reference (`op://vault/item/field`) and the
env KEY only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


# Security.framework's errSecItemNotFound is -25300. Process exit statuses are
# eight bits on POSIX, so the `security` CLI's status is observed as 44.
_SECURITY_ITEM_NOT_FOUND_EXIT = (-25300) % 256

# Per-HTTP-call timeout (seconds) and retry count (ONE retry on timeout/transient).
HTTP_TIMEOUT = 10.0
HTTP_RETRIES = 1

# 1Password well-known field purposes (used when a field is referenced by the
# purpose name rather than an explicit label/id).
_PURPOSE_ALIASES = {
    "password": "PASSWORD",
    "username": "USERNAME",
    "notes": "NOTES",
    "notesplain": "NOTES",
}


class ResolveError(Exception):
    """A resolution failure that is safe to show the user.

    The message must NEVER contain a secret value — only refs, keys, and
    transport-level detail.
    """


# --------------------------------------------------------------------------- #
# HTTP layer (thin, mockable seam)                                            #
# --------------------------------------------------------------------------- #
def _http_get(url, token):
    """GET `url` with a Bearer token, returning parsed JSON.

    Retries once on timeout / transient transport error. Raises ResolveError
    (secret-free) on any non-2xx, timeout-after-retry, or malformed JSON.

    This function is the single network seam; tests monkeypatch it (or the
    `urllib.request.urlopen` it calls) so no real network is touched.
    """
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")

    last_exc = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                body = resp.read()
                if not (200 <= int(status) < 300):
                    raise ResolveError(
                        "Connect returned HTTP %s for %s" % (status, _safe_url(url))
                    )
                try:
                    return json.loads(body.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ResolveError(
                        "Connect returned malformed JSON for %s (%s)"
                        % (_safe_url(url), type(exc).__name__)
                    )
        except urllib.error.HTTPError as exc:
            # HTTP errors (404, 401, 5xx). 5xx is worth one retry; others fail fast.
            if 500 <= exc.code < 600 and attempt < HTTP_RETRIES:
                last_exc = exc
                continue
            raise ResolveError(
                "Connect returned HTTP %s for %s" % (exc.code, _safe_url(url))
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Timeout / connection error: retry once, then fail closed.
            last_exc = exc
            if attempt < HTTP_RETRIES:
                continue
            reason = getattr(exc, "reason", exc)
            raise ResolveError(
                "Connect request to %s failed: %s"
                % (_safe_url(url), _exc_label(reason))
            )
    # Defensive: loop exhausted without returning/raising.
    raise ResolveError(
        "Connect request to %s failed: %s" % (_safe_url(url), _exc_label(last_exc))
    )


def _safe_url(url):
    """A URL is not a secret, but strip any query string just in case."""
    return url.split("?", 1)[0]


def _exc_label(exc):
    """A short, secret-free label for an exception/reason."""
    if exc is None:
        return "unknown error"
    return "%s: %s" % (type(exc).__name__, exc) if not isinstance(exc, str) else exc


# --------------------------------------------------------------------------- #
# op:// reference parsing                                                      #
# --------------------------------------------------------------------------- #
def parse_ref(ref):
    """Parse `op://<vault>/<item>/<field>` or
    `op://<vault>/<item>/<section>/<field>`.

    Returns (vault, item, section, field) where section may be None.
    Raises ResolveError on a malformed reference (no secret involved).
    """
    if not ref.startswith("op://"):
        raise ResolveError("not an op:// reference: %r" % ref)
    rest = ref[len("op://"):]
    parts = [p for p in rest.split("/")]
    if len(parts) < 3 or any(p == "" for p in parts):
        raise ResolveError(
            "malformed op:// reference (expected op://vault/item/[section/]field): %s"
            % ref
        )
    if len(parts) == 3:
        vault, item, field = parts
        return vault, item, None, field
    if len(parts) == 4:
        vault, item, section, field = parts
        return vault, item, section, field
    raise ResolveError(
        "malformed op:// reference (too many path segments): %s" % ref
    )


# --------------------------------------------------------------------------- #
# Connect resolution                                                          #
# --------------------------------------------------------------------------- #
def _looks_like_uuid(s):
    """1Password ids are 26-char lowercase base32 (Crockford-ish) strings."""
    return len(s) == 26 and s.isalnum() and s.lower() == s


def _lookup_vault_id(host, token, vault):
    """Map a vault name-or-UUID to a vault id via GET /v1/vaults."""
    if _looks_like_uuid(vault):
        return vault
    vaults = _http_get(host.rstrip("/") + "/v1/vaults", token)
    for v in vaults or []:
        if v.get("id") == vault or v.get("name") == vault:
            return v["id"]
    raise ResolveError("vault not found: %r" % vault)


def _lookup_item_id(host, token, vault_id, item):
    """Map an item title-or-UUID to an item id via GET .../items."""
    if _looks_like_uuid(item):
        return item
    # Connect supports a filter query, but list-and-match keeps us robust to
    # servers that ignore the filter param.
    items = _http_get(
        host.rstrip("/") + "/v1/vaults/" + urllib.parse.quote(vault_id) + "/items",
        token,
    )
    for it in items or []:
        if it.get("id") == item or it.get("title") == item:
            return it["id"]
    raise ResolveError("item not found: %r" % item)


def _extract_field(item_obj, section, field):
    """Pull the requested field value out of a full item object.

    Match order:
      1. If a section was given, restrict to fields whose section label/id matches.
      2. Match field by `label`, then by `id`.
      3. Match by well-known `purpose` (password/username/notes).
    Raises ResolveError (secret-free) if not found.
    """
    fields = item_obj.get("fields") or []

    # Build a section-id set if a section name was requested.
    section_ids = None
    if section is not None:
        section_ids = set()
        for sec in item_obj.get("sections") or []:
            if sec.get("id") == section or sec.get("label") == section:
                section_ids.add(sec.get("id"))
        if not section_ids:
            raise ResolveError("section not found in item: %r" % section)

    def in_section(f):
        if section_ids is None:
            return True
        sec = f.get("section") or {}
        return sec.get("id") in section_ids

    # 1 & 2: label / id match.
    for f in fields:
        if not in_section(f):
            continue
        if f.get("label") == field or f.get("id") == field:
            return _field_value(f, field)

    # 3: well-known purpose (only meaningful when no section was requested,
    # since purpose fields are section-less on the item root).
    if section is None:
        want_purpose = _PURPOSE_ALIASES.get(field.lower())
        if want_purpose:
            for f in fields:
                if f.get("purpose") == want_purpose:
                    return _field_value(f, field)

    raise ResolveError("field not found in item: %r" % field)


def _field_value(f, field):
    if "value" not in f or f.get("value") is None:
        raise ResolveError("field %r has no value" % field)
    return f["value"]


def resolve_ref(ref, host, token):
    """Resolve a single op:// reference to its secret value.

    Raises ResolveError (never containing the value) on any failure.
    """
    vault, item, section, field = parse_ref(ref)
    vault_id = _lookup_vault_id(host, token, vault)
    item_id = _lookup_item_id(host, token, vault_id, item)
    item_obj = _http_get(
        host.rstrip("/")
        + "/v1/vaults/"
        + urllib.parse.quote(vault_id)
        + "/items/"
        + urllib.parse.quote(item_id),
        token,
    )
    return _extract_field(item_obj, section, field)


# --------------------------------------------------------------------------- #
# env-file parsing                                                            #
# --------------------------------------------------------------------------- #
def parse_env_file(text):
    """Parse dotenv-style text into an ordered list of (key, value) pairs.

    Rules:
      - Blank lines and `#` comment lines are ignored.
      - `export KEY=value` is tolerated (the `export ` prefix is stripped).
      - A single layer of surrounding single or double quotes on the value is
        stripped.
      - Lines without `=` are ignored (tolerant).
    Values are returned verbatim otherwise (op:// detection happens later).
    """
    pairs = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        # Strip one matching pair of surrounding quotes.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        pairs.append((key, value))
    return pairs


# --------------------------------------------------------------------------- #
# Orchestration                                                               #
# --------------------------------------------------------------------------- #
def resolve_env(pairs, host, token):
    """Resolve all op:// values in `pairs`, returning a dict of resolved env.

    Fail-closed: if ANY op:// ref fails, raises ResolveError and returns
    nothing — the caller must not exec the child. Non-op values pass through
    untouched.
    """
    resolved = {}
    for key, value in pairs:
        if value.startswith("op://"):
            try:
                resolved[key] = resolve_ref(value, host, token)
            except ResolveError as exc:
                # Name the ref + KEY, never the value.
                raise ResolveError(
                    "could not resolve %s for %s: %s" % (value, key, exc)
                )
        else:
            resolved[key] = value
    return resolved


def _die(msg, code=1):
    sys.stderr.write("op-resolve: " + msg + "\n")
    sys.exit(code)


def _keychain_connect_token():
    """Return (status, token), probing existence before any value read."""
    try:
        probe = subprocess.run(
            ["security", "find-generic-password", "-s", "op-connect-token"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return "indeterminate", None
    # `security` returns errSecItemNotFound (-25300), which a POSIX shell
    # observes modulo 256 as 44. Other failures (for example a locked or
    # unauthorized Keychain) say nothing about whether the item exists.
    if probe.returncode == _SECURITY_ITEM_NOT_FOUND_EXIT:
        return "absent", None
    if probe.returncode != 0:
        return "indeterminate", None

    try:
        read = subprocess.run(
            ["security", "find-generic-password", "-s", "op-connect-token", "-w"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return "present-unavailable", None
    if read.returncode == _SECURITY_ITEM_NOT_FOUND_EXIT:
        return "absent", None
    if read.returncode != 0 or not read.stdout:
        return "present-unavailable", None
    try:
        token = read.stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        return "present-unavailable", None
    if not token:
        return "present-unavailable", None
    return "resolved", token


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    parser = argparse.ArgumentParser(
        prog="op-resolve.py",
        description="Pure-HTTP op:// resolver (drop-in for `op run --env-file`).",
        add_help=True,
    )
    parser.add_argument(
        "--env-file",
        required=True,
        help="dotenv-style file whose op:// values are resolved via 1Password Connect",
    )
    # Everything after `--` is the child command.
    if "--" in argv:
        idx = argv.index("--")
        head, cmd = argv[:idx], argv[idx + 1:]
    else:
        head, cmd = argv, []

    try:
        args = parser.parse_args(head)
    except SystemExit as exc:
        # argparse already wrote its message; preserve its exit code.
        raise

    if not cmd:
        _die("no child command given; usage: op-resolve.py --env-file FILE -- cmd args...", 2)

    host = os.environ.get("OP_CONNECT_HOST")
    token = os.environ.get("OP_CONNECT_TOKEN")
    if not host:
        _die("OP_CONNECT_HOST is not set", 1)
    if not token:
        token_status, token = _keychain_connect_token()
        if token_status == "absent":
            _die(
                "OP_CONNECT_TOKEN could not be found in the process environment "
                "or Keychain item op-connect-token",
                1,
            )
        if token_status == "indeterminate":
            _die(
                "could not determine whether OP_CONNECT_TOKEN exists in "
                "Keychain item op-connect-token because the Keychain lookup failed",
                1,
            )
        if token_status == "present-unavailable":
            _die(
                "OP_CONNECT_TOKEN exists in Keychain item op-connect-token but "
                "its value could not be read",
                1,
            )

    try:
        with open(args.env_file, "r", encoding="utf-8") as fh:
            pairs = parse_env_file(fh.read())
    except OSError as exc:
        _die("cannot read env-file %r: %s" % (args.env_file, exc.strerror or exc), 1)

    try:
        resolved = resolve_env(pairs, host, token)
    except ResolveError as exc:
        _die(str(exc), 1)

    child_env = dict(os.environ)
    # The resolver's own credential never crosses into the child: a job needs
    # its resolved refs, not Automation-vault-wide reach. Popped BEFORE the
    # resolved merge, so an env-file that explicitly resolves either name for
    # the child still wins. (Found at the 2026-08-12 herald arming sitting —
    # herald's wrapper claims "nothing op-/Connect-shaped in the child's
    # environment", and this pop is where that claim becomes true for every
    # consumer of this resolver.)
    for _key in ("OP_CONNECT_TOKEN", "OP_CONNECT_HOST"):
        child_env.pop(_key, None)
    child_env.update(resolved)

    # Prefer exec so signals/exit codes pass through cleanly. execvpe replaces
    # this process; secrets live only in the child's environment, never printed.
    try:
        os.execvpe(cmd[0], cmd, child_env)
    except OSError as exc:
        _die("cannot exec %r: %s" % (cmd[0], exc.strerror or exc), 127)


if __name__ == "__main__":
    main()
