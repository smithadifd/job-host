#!/usr/bin/env python3
"""Stdlib-only builder for a wrapper-side FAILURE TRACE body — the payload
`lib.sh`'s `_lk_attach_trace` POSTs to serotine's existing
`POST /api/v1/ingest/trace?run_id=<serotine.run.id>`.

Without a wrapper-side upload, job pages show `Exit 74` with a `—` in the
Trace column because no trace has been attached. This module
supplies that missing upload path and
turns the tail of a launchd-kit run log into the raw NDJSON body that endpoint
already accepts. The SERVER side is unchanged — ingest, `/api/v1/traces/…`
read, and retention all shipped already.

OFF BY DEFAULT, PER JOB. Nothing here runs unless a wrapper's environment sets
`SEROTINE_TRACE_CAPTURE` (see lib.sh § trace capture). A wrapper that does not
set it never invokes this module at all.

------------------------------------------------------------------------------
Payload
------------------------------------------------------------------------------
Raw NDJSON, one JSON object per line — the shape `src/lib/traces/store.ts`
stores verbatim and `GET /api/v1/traces/<run-id>` hands back:

  line 1   {"type":"system","subtype":"launchd_kit_trace", …}   provenance header
  line 2+  {"type":"log","stream":"run-log","n":1,"text":"…"}   captured log lines

There is deliberately no `{"type":"result"}` line: serotine does not parse
traces (route.ts § "no parsing, no analysis"), and a synthetic result line
would read like a Claude Code turn result to any future consumer that does.

------------------------------------------------------------------------------
REDACTION — the privacy gate (this is the part to read before enabling a job)
------------------------------------------------------------------------------
A wrapper's run log can contain credentials, and this POSTs it off-box into a
store whose read surface is open by default on the LAN
(`SEROTINE_TRACE_DOWNLOAD_TOKEN` unset = no gate). Three layers, strongest
first:

  1. EXACT KNOWN VALUES — the wrapper's own environment is in hand at capture
     time, so every env var whose NAME looks secret (and every name the
     operator lists in SEROTINE_TRACE_REDACT_ENV) has its literal VALUE
     replaced wherever it appears. This layer is exact, not heuristic: it is
     the one real guarantee here, and it covers the credentials a launchd-kit
     job actually holds (ingest token, Discord webhook, anything `op run`
     injected).
  2. CREDENTIAL SHAPES — well-known token formats and `name=value` /
     `"name": "value"` / `--name value` assignments whose NAME contains a
     secret word, plus `Authorization:` header credentials and PEM private-key
     blocks. Catches secrets that are NOT in our environment (a child process
     echoing its own config).
  3. OPERATOR PATTERN — SEROTINE_TRACE_REDACT_PATTERN, a job-specific regex.
     An unusable regex FAILS CLOSED: the body comes back empty and nothing is
     POSTed.

`op://` references are deliberately NOT redacted — they are references, not
secrets, and they are exactly the diagnostic you want in a failure trace.

RESIDUAL RISK, stated plainly: layers 2 and 3 are heuristics. A secret that is
neither in this process's environment nor in a recognized shape (a third-party
tool printing a bare password on its own) will pass through. That is why the
capture flag is per job and off fleet-wide: enabling a job is a supervised act
whose first step is reading that job's own log.

SAFETY: `build_body()` NEVER raises and never blocks — any failure returns
`b""`, which the bash caller treats as "hold, POST nothing" (an empty body
would be a 422 at the server anyway, store.ts:107). Nothing here writes a file,
opens a socket, or logs a secret.

Every namespace occurrence is the literal, grep-able `serotine.` / `SEROTINE_`
prefix on purpose (telemetry/README.md § Renaming checklist).

------------------------------------------------------------------------------
SECOND CONSUMER — `serotine.run.failure` forensics (wave row BS8)
------------------------------------------------------------------------------
`build_failure()` is the same log tail, the same redactor, a different
destination: the three-member `serotine.run.failure` object that rides the
run.v1 LINE itself (message / last_log_line / stderr_tail), rendered by
serotine's `/jobs/<label>` page. It lives HERE, not in serotine_usage.py, for
one reason — REDACTION IS THE EMITTER'S JOB (serotine AGENTS.md § "Failure
forensics on a run line": "Anyone wiring an emitter owns the redaction"), and
this module is where the redactor is. There is exactly one redaction
implementation in launchd-kit and both wrapper-side forensics paths go through
it.

The two paths are INDEPENDENT and separately flagged — `SEROTINE_TRACE_CAPTURE`
arms the 200-line trace upload to `/api/v1/ingest/trace`;
`SEROTINE_EMIT_FAILURE` arms this 20-line summary on the run line. Both are
unset (off) fleet-wide. Arming either is a separate supervised act.

⚠ `stderr_tail` is the wire's name, not a claim about file descriptors. A
launchd-kit wrapper merges stdout and stderr into ONE run log ($LOG), which is
what this reads; a wrapper that keeps a separate stderr file points
SEROTINE_FAILURE_LOG at it.

CLI (the form lib.sh invokes — secrets travel in the ENVIRONMENT, never on
argv, which is world-readable via `ps`):

    SEROTINE_TRACE_LOG=/path/to/run.log \
    SEROTINE_LINE_RUN_ID=com.example.job/2026-08-18_120000-abcd \
    SEROTINE_LINE_JOB_LABEL=com.example.job \
    SEROTINE_LINE_HOST=worker \
    SEROTINE_LINE_STATUS=fail SEROTINE_LINE_EXIT_CODE=74 \
      python3 serotine_trace.py build-body > body.ndjson

    SEROTINE_FAILURE_LOG=/path/to/run.log \
    SEROTINE_FAILURE_MESSAGE='claude -p exited 74' \
      python3 serotine_trace.py build-failure > failure.json

Empty stdout means "nothing to send" — never an error the caller must handle.
"""

import json
import os
import re
import sys

SUBTYPE = "launchd_kit_trace"
REDACTOR_VERSION = "v1"
TRACE_VERSION = 1

# json.dumps separators — match serotine_spool.py / serotine_usage.py build-line
# so a trace line reads byte-for-byte like its run/event siblings.
_SEPARATORS = (", ", ": ")

# Defaults (every one overridable through the env contract in lib.sh).
DEFAULT_LINES = 200
DEFAULT_MAX_BYTES = 256 * 1024      # after redaction; server cap is 25 MiB
DEFAULT_TAIL_BYTES = 1024 * 1024    # never read more than this from the log
MAX_TEXT_CHARS = 8192               # per-line hard cap; one monster line can't
                                    # crowd out every other line

# serotine.run.failure defaults. The three caps deliberately MATCH the bounds
# serotine's own `lib/dashboard/run-forensics.ts` applies at DISPLAY time
# (message 500 chars / last_log_line 1000, both keeping their head;
# stderr_tail 20 lines then 4000 chars, keeping its end). Bounding here as
# well is not redundant: the wire schema carries no length keyword and the
# ingest body ceiling is 32 MiB, so without an emitter-side bound one
# pathological run ships megabytes that the dashboard then throws away. Sending
# what will actually render also means serotine's "Truncated for display"
# notice reflects a real cut rather than firing on every armed job.
DEFAULT_FAILURE_LINES = 20
FAILURE_MESSAGE_CHARS = 500
FAILURE_LAST_LOG_LINE_CHARS = 1000
FAILURE_STDERR_TAIL_CHARS = 4000

_REDACTED = "[redacted]"

# --- name words: two lists, because the two layers have different blast radii -
# A matched name means something different in each layer, so they deliberately
# do NOT share one list:
#
#   Layer 1 — the name selects an env var whose VALUE becomes a GLOBAL
#     search-and-replace needle across the whole capture. Over-matching here
#     costs the entire trace: one `SSH_AUTH_SOCK=/private/tmp/.../Listeners`
#     would blank that path out of every line that mentions it.
#   Layer 2c — the name matches a `NAME=value` occurrence and redacts THAT
#     VALUE IN PLACE. Over-matching costs one token on one line.
#
# So layer 2c can afford the broader list, and `auth` lives only there: it
# catches `AUTH=…` and `SEROTINE_BASIC_AUTH=…` printed as plain log text
# without ever letting a socket path become a global needle. `authorization`
# is deliberately NOT added — layer 2b already owns the HTTP header and keeps
# its scheme (`Bearer`), which a 2c match on the header's own name would eat.
_SECRET_WORD = (r"token|secret|password|passwd|passphrase|apikey|key|keys|"
                r"webhook|credential|credentials|bearer")
_ASSIGN_ONLY_WORD = r"auth"
# The word must sit on a component boundary: API_KEY / PASSWORD / webhook_url
# match; "keyword" does not.
_SECRET_NAME_RE = re.compile(
    r"(?i)(?:^|[_\-.])(?:%s)(?:$|[_\-.])" % _SECRET_WORD)
_ASSIGN_NAME_RE = re.compile(
    r"(?i)(?:^|[_\-.])(?:%s|%s)(?:$|[_\-.])" % (_SECRET_WORD, _ASSIGN_ONLY_WORD))
# Below this length a value is too generic to use as a global search-and-replace
# needle (a 1-char "secret" would blank out the whole log).
_MIN_AUTO_VALUE = 6
_MIN_NAMED_VALUE = 4

# --- layer 2: credential shapes, checked without any environment help ---------
_SHAPE_RES = [
    # Authorization / Proxy-Authorization header: keep the scheme, drop the credential.
    (re.compile(r"(?i)\b((?:proxy-)?authorization\s*:\s*(?:bearer|basic|token)\s+)\S+"),
     r"\1" + _REDACTED),
    # Discord webhook: keep the id, drop the token half.
    (re.compile(r"(?i)(https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/)\S+"),
     r"\1" + _REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), _REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), _REDACTED),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"), _REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), _REDACTED),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), _REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), _REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), _REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
     _REDACTED),
]

# NAME=value · NAME: value · "name": "value" · --name value / --name=value.
# The NAME is matched loosely and then judged by _is_secret_name, so the
# boundary rule lives in exactly one place ("api_key" yes, "keyword" no)
# instead of being re-encoded into each shape.
_ASSIGN_RE = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_.\-]*)(?P<sep>\"?\s*[=:]\s*\"?)"
    r"(?P<val>[^\s\"',]{4,})")
_FLAG_RE = re.compile(
    r"(?P<name>--?[A-Za-z0-9][A-Za-z0-9_.\-]*)(?P<sep>[=\s]+)(?P<val>[^\s\"']{4,})")

_PEM_BEGIN_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_PEM_END_RE = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")


def _is_secret_name(name):
    """True when an env var NAME looks like it holds a credential, judged for
    LAYER 1 — i.e. strictly enough that its value is safe to use as a global
    search-and-replace needle. See the two-list note above."""
    return bool(_SECRET_NAME_RE.search(name))


def _is_assignment_secret_name(name):
    """True when a `NAME=value` name reads as a credential, judged for LAYER 2c
    — the broader list, because a match here redacts one value in place rather
    than seeding a global needle."""
    return bool(_ASSIGN_NAME_RE.search(name))


def secret_values(env, extra_names=()):
    """Literal values from `env` to redact wherever they appear, longest first
    (so a longer secret is never left half-standing by a shorter one).

    Returns [(value, name), …]. Never includes the value of a var whose value
    is too short to be a safe global needle.
    """
    named = set()
    for raw in extra_names or ():
        for part in re.split(r"[,\s]+", str(raw)):
            if part:
                named.add(part)
    out = []
    for name, value in (env or {}).items():
        if not isinstance(value, str) or not value:
            continue
        if name in named:
            if len(value) >= _MIN_NAMED_VALUE:
                out.append((value, name))
        elif _is_secret_name(name) and len(value) >= _MIN_AUTO_VALUE:
            out.append((value, name))
    out.sort(key=lambda pair: len(pair[0]), reverse=True)
    return out


def redact(text, env=None, redact_env=(), redact_pattern=None):
    """Apply all three redaction layers. Returns (text, count).

    Raises `re.error` only for an unusable operator pattern — the one failure
    the caller must turn into "POST nothing" rather than "POST less".
    """
    count = 0

    # Layer 1 — exact known values.
    for value, name in secret_values(env if env is not None else os.environ,
                                     redact_env):
        if value in text:
            count += text.count(value)
            text = text.replace(value, "[redacted:%s]" % name)

    # Layer 2a — PEM private-key blocks (line-scoped, needs block state).
    out_lines = []
    in_pem = False
    for line in text.split("\n"):
        if _PEM_BEGIN_RE.search(line):
            in_pem = True
            out_lines.append(_PEM_BEGIN_RE.sub("-----BEGIN PRIVATE " "KEY----- "
                                               + _REDACTED, line))
            count += 1
            continue
        if in_pem:
            if _PEM_END_RE.search(line):
                in_pem = False
                out_lines.append(line)
            else:
                out_lines.append(_REDACTED)
                count += 1
            continue
        out_lines.append(line)
    text = "\n".join(out_lines)

    # Layer 2b — credential shapes.
    for pattern, replacement in _SHAPE_RES:
        text, n = pattern.subn(replacement, text)
        count += n

    # Layer 2c — assignments whose NAME reads as a credential. subn() counts
    # every match, including the ones left alone, so the closure counts instead.
    hits = [0]

    def _assign_sub(match):
        name = match.group("name").lstrip("-")
        if (not _is_assignment_secret_name(name)
                or match.group("val").startswith("[redacted")):
            return match.group(0)
        hits[0] += 1
        return match.group("name") + match.group("sep") + _REDACTED

    for pattern in (_ASSIGN_RE, _FLAG_RE):
        text = pattern.sub(_assign_sub, text)
    count += hits[0]

    # Layer 3 — the operator's own pattern (a bad regex propagates: fail closed).
    if redact_pattern:
        text, n = re.subn(redact_pattern, _REDACTED, text)
        count += n

    return text, count


def _read_tail(path, tail_bytes):
    """The last `tail_bytes` of `path` as text. Returns (text, file_bytes) or
    (None, 0) when the file cannot be read as a regular file."""
    st = os.stat(path)
    if not os.path.isfile(path):
        return None, 0
    size = st.st_size
    with open(path, "rb") as fh:
        if size > tail_bytes:
            fh.seek(size - tail_bytes)
            fh.read(1)  # discard the (probably partial) first line
            raw = fh.read()
            partial = True
        else:
            raw = fh.read()
            partial = False
    text = raw.decode("utf-8", "replace")
    if partial:
        nl = text.find("\n")
        text = text[nl + 1:] if nl >= 0 else text
    return text, size


def build_body(log_path, run_id="", job_label="", host="", status="fail",
               exit_code=0, lines=DEFAULT_LINES, max_bytes=DEFAULT_MAX_BYTES,
               tail_bytes=DEFAULT_TAIL_BYTES, env=None, redact_env=(),
               redact_pattern=None):
    """The module's whole interface: a log path in, the NDJSON upload body out.

    Returns b"" for every "do not POST" outcome — missing/empty/unreadable log,
    an unusable operator redaction pattern, or any unexpected error. NEVER
    raises.
    """
    try:
        text, file_bytes = _read_tail(log_path, tail_bytes)
        if not text:
            return b""

        raw_lines = [ln for ln in text.split("\n")]
        if raw_lines and raw_lines[-1] == "":
            raw_lines.pop()
        if not any(ln.strip() for ln in raw_lines):
            return b""

        truncated = file_bytes > tail_bytes
        if lines and len(raw_lines) > lines:
            raw_lines = raw_lines[-lines:]
            truncated = True

        redacted, redactions = redact("\n".join(raw_lines), env=env,
                                      redact_env=redact_env,
                                      redact_pattern=redact_pattern)
        captured = redacted.split("\n")

        # Serialize each captured line once, then drop from the FRONT until the
        # whole body fits: the tail of a failing run is where the error is.
        payload = []
        for text_line in captured:
            if len(text_line) > MAX_TEXT_CHARS:
                text_line = text_line[-MAX_TEXT_CHARS:]
            payload.append(text_line)

        def header_for(count, was_truncated):
            return {
                "type": "system",
                "subtype": SUBTYPE,
                "trace.producer": "launchd-kit/telemetry/serotine_trace.py",
                "trace.version": TRACE_VERSION,
                "trace.redactor": REDACTOR_VERSION,
                "trace.redactions": redactions,
                "trace.source": log_path,
                "trace.log_bytes": file_bytes,
                "trace.lines_captured": count,
                "trace.truncated": bool(was_truncated),
                "serotine.run.id": run_id,
                "serotine.job.label": job_label,
                "host.name": host,
                "serotine.run.status": status,
                "serotine.run.exit_code": exit_code,
            }

        def encode(obj):
            return (json.dumps(obj, separators=_SEPARATORS, sort_keys=False)
                    + "\n").encode("utf-8")

        body_lines = [encode({"type": "log", "stream": "run-log", "n": i + 1,
                              "text": t}) for i, t in enumerate(payload)]
        start = 0
        while True:
            head = encode(header_for(len(body_lines) - start,
                                     truncated or start > 0))
            total = len(head) + sum(len(b) for b in body_lines[start:])
            if total <= max_bytes or start >= len(body_lines):
                break
            start += 1
        if start >= len(body_lines):
            return b""

        # Renumber so `n` reads 1..K over what actually shipped.
        kept = []
        for i, t in enumerate(payload[start:], 1):
            kept.append(encode({"type": "log", "stream": "run-log", "n": i,
                                "text": t}))
        head = encode(header_for(len(kept), truncated or start > 0))
        return head + b"".join(kept)
    except Exception:
        return b""


def build_failure(log_path, message="", lines=DEFAULT_FAILURE_LINES,
                  tail_bytes=DEFAULT_TAIL_BYTES, env=None, redact_env=(),
                  redact_pattern=None):
    """A log path (+ an optional operator message) in, the `serotine.run.failure`
    object out — `{}` when there is nothing safe or non-empty to say.

    Same NEVER-RAISES posture as `build_body`: every failure (unreadable log,
    unusable operator pattern, anything unexpected) answers `{}`, which the
    bash caller and `serotine_usage.py` both read as "emit no failure key".
    FAIL-CLOSED on redaction — a redactor that cannot run yields nothing, never
    an unredacted forensics object.

    Members are omitted, never blanked: the wire refuses a whitespace-only
    string (`pattern: "\\S"`) and ingest rejection is WHOLE-BATCH atomic, so a
    blank member here would wedge the spool for every line in the batch.

    Every cut is a Python str slice, i.e. by CODE POINT — the surrogate-pair
    hazard serotine's TypeScript reader has to work around does not exist on
    this side, and a cut can never produce the lone half that U+FFFD replaces.
    """
    try:
        # An unreadable log is not fatal to the whole object: a wrapper that
        # passed an explicit message still has something worth saying. Only a
        # REDACTION failure takes the object down to {} (the outer except).
        text = ""
        if log_path:
            try:
                text, _file_bytes = _read_tail(log_path, tail_bytes)
                text = text or ""
            except OSError:
                text = ""

        raw_lines = text.split("\n") if text else []
        # Blank lines carry no forensic content and cost the tail budget.
        kept = [ln for ln in raw_lines if ln.strip()]
        if lines and len(kept) > lines:
            kept = kept[-lines:]

        # ONE redaction pass over message + log tail together, so layer 1's
        # global search-and-replace needles apply identically to both, split
        # again afterwards on a NUL sentinel.
        #
        # NUL is stripped from BOTH sides first, for two reasons. It makes the
        # sentinel unambiguous (partition takes the FIRST occurrence, and a
        # message NUL would otherwise land ahead of it). And it keeps a NUL
        # off the wire: json.dumps escapes it legally, but these strings are
        # stored verbatim and re-read downstream by consumers where an
        # embedded NUL truncates rather than errors. A NUL in a run log is a
        # torn write or a binary blob, never forensics.
        sep = "\x00"
        joined = ((message or "").replace(sep, "") + sep
                  + "\n".join(kept).replace(sep, ""))
        redacted, _count = redact(joined, env=env, redact_env=redact_env,
                                  redact_pattern=redact_pattern)
        head, _, tail = redacted.partition(sep)

        out = {}
        msg = head.strip()
        if msg:
            out["message"] = msg[:FAILURE_MESSAGE_CHARS]

        tail_lines = [ln for ln in tail.split("\n") if ln.strip()]
        if tail_lines:
            # last_log_line keeps its HEAD (a line is identified by how it
            # starts); stderr_tail keeps its END (the last thing a run said is
            # the answer to "why"). Same rule serotine renders under.
            last = tail_lines[-1].strip()
            if last:
                out["last_log_line"] = last[:FAILURE_LAST_LOG_LINE_CHARS]
            stderr_tail = "\n".join(tail_lines)[-FAILURE_STDERR_TAIL_CHARS:]
            if stderr_tail.strip():
                out["stderr_tail"] = stderr_tail
        return out
    except Exception:
        return {}


def _env_int(name, default):
    raw = os.environ.get(name, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _main_build_failure():
    """`build-failure`: print the serotine.run.failure object as one JSON line.

    Empty stdout means "attach nothing" — the same contract build-body uses,
    and the reason `{}` is never printed: lib.sh passes stdout straight through
    to serotine_usage.py, and an empty string there is unambiguously absence.
    The failure REDACTION knobs are the trace ones on purpose: a job that has
    already tuned SEROTINE_TRACE_REDACT_ENV/_PATTERN for its own log should not
    have to restate them for the same log's summary.
    """
    obj = build_failure(
        os.environ.get("SEROTINE_FAILURE_LOG", ""),
        message=os.environ.get("SEROTINE_FAILURE_MESSAGE", ""),
        lines=_env_int("SEROTINE_FAILURE_LINES", DEFAULT_FAILURE_LINES),
        tail_bytes=_env_int("SEROTINE_TRACE_TAIL_BYTES", DEFAULT_TAIL_BYTES),
        redact_env=os.environ.get("SEROTINE_TRACE_REDACT_ENV", ""),
        redact_pattern=os.environ.get("SEROTINE_TRACE_REDACT_PATTERN") or None,
    )
    if obj:
        sys.stdout.write(json.dumps(obj, separators=_SEPARATORS,
                                    sort_keys=False))
        sys.stdout.flush()
    return 0


def main(argv):
    if len(argv) > 1 and argv[1] == "build-failure":
        return _main_build_failure()
    if len(argv) < 2 or argv[1] != "build-body":
        sys.stderr.write("usage: serotine_trace.py build-body|build-failure\n")
        return 2
    body = build_body(
        os.environ.get("SEROTINE_TRACE_LOG", ""),
        run_id=os.environ.get("SEROTINE_LINE_RUN_ID", ""),
        job_label=os.environ.get("SEROTINE_LINE_JOB_LABEL", ""),
        host=os.environ.get("SEROTINE_LINE_HOST", ""),
        status=os.environ.get("SEROTINE_LINE_STATUS", "fail"),
        exit_code=_env_int("SEROTINE_LINE_EXIT_CODE", 0),
        lines=_env_int("SEROTINE_TRACE_LINES", DEFAULT_LINES),
        max_bytes=_env_int("SEROTINE_TRACE_UPLOAD_MAX_BYTES", DEFAULT_MAX_BYTES),
        tail_bytes=_env_int("SEROTINE_TRACE_TAIL_BYTES", DEFAULT_TAIL_BYTES),
        redact_env=os.environ.get("SEROTINE_TRACE_REDACT_ENV", ""),
        redact_pattern=os.environ.get("SEROTINE_TRACE_REDACT_PATTERN") or None,
    )
    if body:
        sys.stdout.buffer.write(body)
        sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
