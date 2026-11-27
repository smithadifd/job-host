#!/usr/bin/env python3
"""Stdlib-only spool writer/pusher for Serotine telemetry — the Python sibling
of lib.sh's `lk_emit_run` / `_lk_push_spool` hooks.

Why this exists:
Python producers (policy-tuner, feed-condenser's audit.py) must not shell out
to the bash hook — JSON-through-argv quoting and subprocess failure modes fight
the never-fail-the-producer contract. This helper is homed next to the schema +
validators, is pure stdlib (no package-manager dependencies, same posture as
validate_serotine_run.py / serotine_usage.py), and consumers vendor or
path-import it exactly as they already vendor the schema.

What it does:

  emit_run(...)    build ONE serotine.run.v1 line byte-compatible with the line
                   `lk_emit_run` (via ../serotine_usage.py build-line) produces
                   for the same inputs, append it to the spool, then best-effort
                   push (a standalone Python job has no bash wrapper to flush
                   the spool for it).
  emit_event(...)  append ONE pre-built serotine.event.v1 mapping to the spool.
                   Append-only by default per § Resolutions 5: when the producer
                   runs inside a launchd-kit wrapper, the wrapper's terminal
                   lk_emit_run flushes event lines too — no second push path.
                   Standalone producers pass push=True or call push_spool().
  push_spool(...)  best-effort POST of unsent spool bytes, IDENTICAL semantics
                   to _lk_push_spool in ../lib.sh: no-op unless BOTH
                   SEROTINE_INGEST_URL and SEROTINE_INGEST_TOKEN are set, POST
                   as application/x-ndjson with Bearer auth, short timeout
                   (SEROTINE_INGEST_TIMEOUT, default 5s), byte-offset cursor
                   file (<spool>.cursor) advanced ONLY on a 2xx response, never
                   retries within a call. Ingest dedupes on serotine.run.id /
                   serotine.event.id, so replaying already-sent lines is safe.

                   One line-level transform, from the accepted event design
                   (fleet-event-v1.md § Transport): `serotine.event.findings`
                   is SPOOL-ONLY. The push strips the whole array from the POST
                   body, FAIL-CLOSED — a line carrying the findings key that
                   cannot be parsed/re-serialized is dropped from the body,
                   never sent unstripped. Spool bytes and the cursor are
                   untouched by the strip; lines without the key pass through
                   byte-identical.

SAFETY (the #1 property, same posture as the bash hooks — these fns may front
LIVE production jobs): the public entry points NEVER raise and never block
beyond the short push timeout. Every failure path is swallowed silently; the
ingest token is never logged or printed.

Environment contract (same vars as ../lib.sh):
  SEROTINE_SPOOL_DIR       spool directory (default $HOME/.claude/telemetry)
  SEROTINE_JOB_LABEL       job label (default com.<user>.<TOOL_NAME|unknown>,
                           user = the login name `id -un` prints, `unknown`
                           when that lookup fails or is empty)
  SEROTINE_HOST            host name (default `hostname -s` equivalent)
  SEROTINE_TRIGGER         launchd | manual | kickstart (default launchd)
  SEROTINE_RUN_START       run-start RFC 3339 stamp (lib.sh exports it); when
                           unset, this module's import time is the fallback
  LK_STAMP                 launchd-kit's YYYY-MM-DD_HHMMSS run stamp
  SEROTINE_INGEST_URL      ingest endpoint; push is a silent no-op without it
  SEROTINE_INGEST_TOKEN    Bearer token; push is a silent no-op without it
  SEROTINE_INGEST_TIMEOUT  POST timeout in seconds (default 5)

Concurrency: appends are a single write(2) on an O_APPEND fd, so lines from
this helper and from bash `lk_emit_run` interleave whole-line on the shared
spool. Cursor read/write races with the bash pusher are tolerated exactly as
lib.sh tolerates them — worst case is a double-send, safe under ingest dedupe.

Every namespace occurrence is the literal, grep-able prefix `serotine.` /
`SEROTINE_` on purpose (telemetry/README.md § Renaming checklist).
"""

import errno
import fcntl
import json
import math
import os
import pwd
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

# json.dumps separators used by serotine_usage.py build-line — byte-compat
# with the bash-emitted lines depends on them.
_SEPARATORS = (", ", ": ")

_FINDINGS_KEY = "serotine.event.findings"
_FINDINGS_MARKER = b'"serotine.event.findings"'


def _rfc3339(epoch=None):
    """RFC 3339 with explicit colon offset, seconds precision — the same shape
    lib.sh's _lk_rfc3339 emits (2026-07-09T15:30:00-04:00)."""
    dt = datetime.fromtimestamp(epoch) if epoch is not None else datetime.now()
    return dt.astimezone().isoformat(timespec="seconds")


# SEROTINE_RUN_START fallback for processes that are not under a launchd-kit
# wrapper: import time approximates process start for the jobs this serves.
_IMPORT_START = None
try:
    _IMPORT_START = _rfc3339()
except Exception:
    pass


def _rand4():
    """4 lowercase hex chars for the run/event id suffix."""
    return os.urandom(2).hex()


def _stamp():
    """launchd-kit's YYYY-MM-DD_HHMMSS run stamp (LK_STAMP when exported)."""
    return os.environ.get("LK_STAMP") or time.strftime("%Y-%m-%d_%H%M%S")


def _spool_path(spool=None):
    if spool:
        return spool
    spool_dir = os.environ.get("SEROTINE_SPOOL_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude", "telemetry")
    return os.path.join(spool_dir, "spool.jsonl")


def _login_name():
    """The effective user's login name, the way `id -un` prints it (both read
    getpwuid(geteuid())); `unknown` when the lookup fails or the name is
    empty, never an empty segment and never an exception. Same fallback as
    lib/telemetry.sh `_lk_emit_run_core`."""
    try:
        name = pwd.getpwuid(os.geteuid()).pw_name
    except Exception:
        return "unknown"
    return name if isinstance(name, str) and name else "unknown"


def _default_label():
    """SEROTINE_JOB_LABEL when set and non-empty, else
    com.<login name>.<TOOL_NAME>, each segment falling back to `unknown`
    when empty or unset: byte for byte the default lib/telemetry.sh builds.
    Never raises."""
    return os.environ.get("SEROTINE_JOB_LABEL") \
        or "com." + _login_name() + "." + (os.environ.get("TOOL_NAME") or "unknown")


def _default_host():
    return os.environ.get("SEROTINE_HOST") \
        or socket.gethostname().split(".", 1)[0]


_LOCK_TIMEOUT = 5.0
_LOCK_TIMEOUT_MAX = 300.0
_LOCK_POLL = 0.02


def _spool_lock_path(spool):
    """Sibling lock file shared with launchd-kit/lib/telemetry.sh."""
    return spool + ".lock"


def _lock_timeout(env=None):
    source = os.environ if env is None else env
    raw = source.get("SEROTINE_SPOOL_LOCK_TIMEOUT")
    try:
        value = float(raw) if raw else _LOCK_TIMEOUT
    except (TypeError, ValueError):
        return _LOCK_TIMEOUT
    if not math.isfinite(value) or value <= 0:
        return _LOCK_TIMEOUT
    if value > _LOCK_TIMEOUT_MAX:
        return _LOCK_TIMEOUT_MAX
    return value


def _acquire_spool_lock(spool, timeout=None, env=None):
    """Exclusive flock on <spool>.lock. Returns the lock fd. Raises OSError
    if the lock cannot be taken within timeout — caller must not write.
    An unopenable lock path is a named error on stderr, not a silent raise."""
    if timeout is None:
        timeout = _lock_timeout(env)
    path = _spool_lock_path(spool)
    spool_dir = os.path.dirname(path)
    if spool_dir:
        os.makedirs(spool_dir, exist_ok=True)
    deadline = time.monotonic() + timeout
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        sys.stderr.write("spool lock unopenable (%s): %s\n" % (path, exc))
        raise
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise OSError("spool lock timeout (%s)" % path)
                time.sleep(_LOCK_POLL)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _release_spool_lock(fd):
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _complete_end(path, size=None):
    """Byte offset of the first byte past the last complete JSONL line
    (one that ends in a newline). An incomplete tail with no trailing
    newline is excluded so a later push re-reads it whole."""
    try:
        size = os.path.getsize(path) if size is None else size
    except OSError:
        return 0
    if size <= 0:
        return 0
    with open(path, "rb") as f:
        f.seek(size - 1)
        if f.read(1) == b"\n":
            return size
        pos = size
        while pos > 0:
            start = max(0, pos - 65536)
            f.seek(start)
            buf = f.read(pos - start)
            i = buf.rfind(b"\n")
            if i >= 0:
                return start + i + 1
            pos = start
    return 0


# An unknown spool size never authorizes a truncate.
def _append(spool, obj, env=None):
    """Append one JSONL line under the exclusive <spool>.lock flock shared
    with bash _lk_append_spool_line. A short write truncates back to the
    pre-write size (known via fstat on the write fd) so no torn record
    remains, then raises so emit_run/emit_event surface False. If the lock
    cannot be taken, does not write and does not truncate. `env`, when
    passed, is the mapping _lock_timeout reads instead of os.environ."""
    data = (json.dumps(obj, separators=_SEPARATORS) + "\n").encode("utf-8")
    spool_dir = os.path.dirname(spool)
    if spool_dir:
        os.makedirs(spool_dir, exist_ok=True)
    lock_fd = _acquire_spool_lock(spool, env=env)
    try:
        fd = os.open(spool, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            pre = os.fstat(fd).st_size
            separator = b""
            if pre > 0 and os.pread(fd, 1, pre - 1) != b"\n":
                separator = b"\n"
            payload = separator + data
            n = os.write(fd, payload)
            if n != len(payload):
                os.ftruncate(fd, pre)
                raise OSError("short spool write (%d of %d bytes)" %
                              (n, len(payload)))
        finally:
            os.close(fd)
    finally:
        _release_spool_lock(lock_fd)


def new_run_id(label=None, stamp=None):
    """<label>/<YYYY-MM-DD_HHMMSS>-<4 hex> — the serotine.run.id shape."""
    return "%s/%s-%s" % (label or _default_label(), stamp or _stamp(), _rand4())


def new_event_id(judge_id, stamp=None):
    """<judge id>/<YYYY-MM-DD_HHMMSS>-<4 hex> — the serotine.event.id shape."""
    return "%s/%s-%s" % (judge_id, stamp or _stamp(), _rand4())


def build_run_line(status="ok", exit_code=0, *, run_id=None, label=None,
                   host=None, start=None, end=None, trigger=None,
                   operation=None, usage=None):
    """One serotine.run.v1 dict, key order and content byte-compatible (after
    json.dumps with this module's separators) with serotine_usage.py
    build-line for the same inputs. `usage` is a pre-built serotine.usage
    array (the Python producer knows its own tokens — there is deliberately no
    session-JSONL window fallback here; that stays in serotine_usage.py).
    Mirrors build-line: skip runs never carry usage, and a non-empty usage
    array brings gen_ai.operation.name (default invoke_agent) with it.
    May raise — emit_run wraps it."""
    label = label or _default_label()
    end = end or _rfc3339()
    line = {
        "serotine.schema": "serotine.run.v1",
        "serotine.run.id": run_id or new_run_id(label),
        "serotine.job.label": label,
        "host.name": host or _default_host(),
        "serotine.run.start": start or os.environ.get("SEROTINE_RUN_START")
        or _IMPORT_START or end,
        "serotine.run.end": end,
        "serotine.run.exit_code": int(exit_code),
        "serotine.run.status": status,
        "serotine.run.trigger": trigger
        or os.environ.get("SEROTINE_TRIGGER") or "launchd",
    }
    if usage and status != "skip":
        line["gen_ai.operation.name"] = operation or "invoke_agent"
        line["serotine.usage"] = usage
    return line


def emit_run(status="ok", exit_code=0, *, run_id=None, label=None, host=None,
             start=None, end=None, trigger=None, operation=None, usage=None,
             spool=None, push=True):
    """Append one serotine.run.v1 line, then best-effort push. NEVER raises;
    returns True when the line was appended, False otherwise (best-effort —
    callers must not gate work on it)."""
    try:
        spool = _spool_path(spool)
        _append(spool, build_run_line(
            status, exit_code, run_id=run_id, label=label, host=host,
            start=start, end=end, trigger=trigger, operation=operation,
            usage=usage))
    except Exception:
        return False
    if push:
        push_spool(spool)
    return True


def emit_event(event, *, spool=None, push=False):
    """Append one pre-built serotine.event.v1 mapping (the producer owns the
    envelope; telemetry/serotine-event-v1.schema.json is the contract).
    Append-only by default — under a launchd-kit wrapper the terminal
    lk_emit_run flushes event lines too (§ Resolutions 5); standalone
    producers pass push=True. NEVER raises; returns True when appended."""
    try:
        spool = _spool_path(spool)
        _append(spool, dict(event))
    except Exception:
        return False
    if push:
        push_spool(spool)
    return True


def _strip_findings(raw):
    """POST-body transform mandated by fleet-event-v1.md § Transport:
    serotine.event.findings is spool-only and never leaves the machine. Lines
    without the key pass through byte-identical; a line carrying it is parsed,
    stripped, and re-serialized — and if that fails, the line is DROPPED from
    the body (fail-closed), never sent unstripped. Incomplete or unparseable
    lines (a torn short write that survived on disk) are skipped rather than
    POSTed. Spool bytes and the cursor are not touched. Returns the body bytes
    (no trailing newline, matching the bash pusher's command-substitution
    body)."""
    out = []
    for ln in raw.split(b"\n"):
        if not ln.strip():
            continue
        if _FINDINGS_MARKER not in ln:
            try:
                json.loads(ln.decode("utf-8"))
            except Exception:
                continue  # torn / unparseable: skip, never misparse
            out.append(ln)
            continue
        try:
            obj = json.loads(ln.decode("utf-8"))
            obj.pop(_FINDINGS_KEY, None)
            out.append(json.dumps(obj, separators=_SEPARATORS).encode("utf-8"))
        except Exception:
            continue  # fail-closed: drop rather than send unstripped
    return b"\n".join(out)


def push_spool(spool=None, cursor=None):
    """Best-effort POST of unsent spool bytes — _lk_push_spool semantics.
    NEVER raises, never retries within a call, cursor advances ONLY on 2xx
    (otherwise the batch is retried on a later push; dedupe on run/event id
    makes re-sends safe). Silent no-op unless BOTH SEROTINE_INGEST_URL and
    SEROTINE_INGEST_TOKEN are set. The token is used for the Authorization
    header only — it is never logged, printed, or included in errors
    (failures are swallowed silently)."""
    try:
        url = os.environ.get("SEROTINE_INGEST_URL")
        token = os.environ.get("SEROTINE_INGEST_TOKEN")
        if not url or not token:
            return
        spool = _spool_path(spool)
        cursor = cursor or spool + ".cursor"
        if not os.path.isfile(spool):
            return

        try:
            with open(cursor, "r", encoding="utf-8") as f:
                off = int(f.read().strip())
        except Exception:
            off = 0
        if off < 0:
            off = 0

        size = os.path.getsize(spool)
        if size <= off:
            return
        complete = _complete_end(spool, size)
        if complete <= off:
            return
        with open(spool, "rb") as f:
            f.seek(off)
            raw = f.read(complete - off)
        body = _strip_findings(raw)
        if not body:
            return  # everything dropped fail-closed; no POST → no advance

        try:
            timeout = float(os.environ.get("SEROTINE_INGEST_TIMEOUT") or 5)
        except (TypeError, ValueError):
            timeout = 5.0
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": "application/x-ndjson"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = resp.status
        except urllib.error.HTTPError as e:
            code = e.code
        if 200 <= code < 300:
            with open(cursor, "w", encoding="utf-8") as f:
                f.write(str(complete))
    except Exception:
        pass
