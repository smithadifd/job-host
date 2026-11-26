#!/usr/bin/env python3
"""serotine.run.v1 line assembler + usage extractor for the lk_emit_run hook.

Stdlib only — launchd-kit takes no package-manager dependencies (same posture as
telemetry/validate_serotine_run.py and scripts/lanes/usage_guard.py). The
bash `lk_emit_run` hook in lib.sh calls this to (a) build the whole JSONL line so
JSON quoting/escaping is correct by construction, and (b) resolve the optional
`serotine.usage` array by one of three paths (checked in this precedence
order — see resolve_usage):

  provider   opt-in, non-Claude jobs.
             SEROTINE_USAGE_PROVIDER=codex|gemini|omp|
             grok-build selects a provider adapter (usage_from_codex_exec_json
             / usage_from_gemini_json / usage_from_omp_json /
             usage_from_grok_build_json, below); SEROTINE_USAGE_INPUT names the
             file holding that provider's raw output; SEROTINE_USAGE_REQUEST_MODEL
             is the wrapper's own pinned canonical id (gen_ai.request.model —
             legitimate to supply directly, since it IS the pin). This path
             OWNS the run once selected: it never falls through to either
             path below, and an unrecognized selector is a hard error (raises)
             rather than a silent empty array — see resolve_usage's docstring.
             INPUT CONTRACT: SEROTINE_USAGE_INPUT
             for codex MUST be the session ROLLOUT file
             ($HOME/.codex/sessions/**/*.jsonl) — the only codex-side input that
             ever carries a served model id (via its turn_context events).
             Raw `codex exec --json` STDOUT is a DIFFERENT wire shape
             (turn.completed events carrying a usage object, with NO model id
             anywhere in the stream — see testdata/codex-exec-stdout-turn-
             completed.jsonl for a real captured specimen) that this adapter
             cannot attribute to a model; pointing SEROTINE_USAGE_INPUT at it
             is a wiring bug, so it is detected and REJECTED LOUDLY
             (CodexStdoutWireShapeError) rather than silently emitting a
             model-less array or fabricating a model id — see
             _parse_codex_exec_json's PROVENANCE NOTE, below.
             KNOWN LIMITATION (codex): a session whose turn_context events
             report multiple DISTINCT served models fails closed to [] with a
             warning — the cumulative token counter can't be attributed per
             model, and wrong attribution is worse than missing usage for
             drift detection. Single-model sessions (the launchd `codex exec`
             case) are unaffected — see _parse_codex_exec_json.
             ADDITIONAL PROVIDERS (measured 2026-09-02):
               omp         SEROTINE_USAGE_INPUT is the `omp -p --mode json`
                           EVENT STREAM (JSONL). Read from the terminal
                           `agent_end` event's messages[]; one entry per
                           distinct (provider, model) with summed tokens.
                           Unlike codex, a multi-model run is attributable
                           rather than fail-closed — every OMP message carries
                           its own model and usage.
               grok-build  SEROTINE_USAGE_INPUT is the Grok Build CLI's
                           `--output-format json` RESULT OBJECT (one JSON
                           object). Its `modelUsage` KEYS are the SERVED ids
                           (`grok-4.6-build` for a run that requested
                           `grok-4.6`) — the first genuine request≠response
                           echo this module has had.
             Both REFUSE a capture of the client's human-readable TUI
             transcript (TuiOutputWireShapeError), the same wiring-bug posture
             codex-stdout gets. Both may carry the client's own notional
             `serotine.usage.cost_usd`, which the schema marks ADVISORY —
             ingest still recomputes canonically. Both apply ONE rule to
             every reported quantity: a total is emitted only when every
             contribution to it was readable. A message/served-id whose token
             counts cannot be read in full is dropped (warned, by name) rather
             than contributing a fabricated 0 or a laundered bool; a group's
             cost is omitted entirely unless every contributor reported one,
             since summing the readable ones is arithmetically the same as
             calling the rest free. Neither carries reasoning
             tokens: both streams report them, and the wire has no member for
             them (additionalProperties:false), so they are dropped rather
             than folded into another count.
  explicit   the wrapper ran `claude -p --output-format json` and passes that
             result's usage object (SEROTINE_USAGE_JSON / SEROTINE_USAGE_FILE).
  fallback   ccusage-style — parse the local Claude Code session JSONL under
             SEROTINE_USAGE_PROJECTS_DIR (default $HOME/.claude/projects) for usage
             records whose timestamp falls in the run's [start, end] window,
             aggregated per (provider, model). Mirrors usage_guard.py's dedup
             semantics: records repeat per (message.id, requestId) as streaming
             chunks land carrying the same cumulative usage — keep the last.
             When SEROTINE_USAGE_SESSION_ID is set, the scan is restricted to
             that one session's transcript (<projects>/*/<session-id>.jsonl) so
             a concurrent session (an interactive window, or another job) can't
             bleed its models/tokens into a monitored job's rollup — the cause
             of model-drift false positives.
             Unset means the whole-tree window walk (backward compatible).

             Run scoping (fan-out jobs). A single --session-id can't attribute a
             job that fans out MULTIPLE `claude -p` calls per run, each with its
             own session id (the 3 feed-condenser weekly audit jobs) — those
             sessions leak into the whole-tree rollup and over-count the fleet.
             Two opt-in modes attribute every session that belongs to ONE run,
             and nothing outside it:
               SEROTINE_USAGE_RUN_PREFIX      shared run-id prefix; scans every
                                              <projects>/*/<prefix>*.jsonl. The
                                              fan-out wrapper stamps each `claude
                                              -p --session-id` with a common run
                                              marker prefix so one glob captures
                                              exactly that run's N sessions. The
                                              marker MUST end in a delimiter
                                              (e.g. "run1-") — a bare "run1"
                                              also matches "run10-…" (warns).
               SEROTINE_USAGE_SESSION_IDS     explicit list (comma/space
                                              separated) of the run's session
                                              ids — a sidecar-marker equivalent
                                              for when the wrapper records the
                                              ids it generated rather than a
                                              shared prefix.
             Precedence: RUN_PREFIX > SESSION_IDS > SESSION_ID > whole-tree. A
             set-but-unmatched run scope yields [] (like SESSION_ID) — it never
             falls back to the whole-tree walk, which would re-introduce the
             bleed. All three are unset by default (backward compatible).
             lib.sh and fan-out wrappers must pass these inputs explicitly
             to enable run-scoped attribution.

Tokens only. There is deliberately NO price table here — dollars are derived at
ingest. The one cost member on
the wire, serotine.usage.cost_usd, is PROVIDER-REPORTED and advisory: it is
copied verbatim from a client that volunteers one (OMP, Grok Build CLI, a
Claude usage file's own costUSD or total_cost_usd) and never computed, never defaulted to 0, and never treated as authoritative.

Subcommands:
  build-line   read all fields from SEROTINE_LINE_* env, resolve usage, print one
               serotine.run.v1 JSONL line on stdout. Nothing else touches stdout.
  usage        resolve + print just the serotine.usage array (for tests/debug).

SEROTINE_LINE_FAILURE_JSON (optional, default empty) carries an already-built,
already-REDACTED serotine.run.failure object — telemetry/serotine_trace.py
`build-failure` is what builds it, because that is where launchd-kit's only
redactor lives. This module never reads a log file and never redacts; it
validates the object and refuses it WHOLE if anything about it is wrong —
unknown member, non-string, blank, over the builder's own ceiling, or carrying
a NUL (`resolve_failure`). No partial pass-through: a helper that answered
malformed is not a helper whose remaining members are worth trusting. lib.sh
passes the variable explicitly on every emit, empty unless SEROTINE_EMIT_FAILURE
arms the path, so the default is off and an inherited value cannot arm it.

Every namespace occurrence is the literal, grep-able prefix `serotine.` on purpose
(telemetry/README.md § Renaming checklist) — the product name is provisional.
"""

import glob
import json
import math
import os
import sys
import warnings
from datetime import datetime

# Map an OTel gen_ai model name to its semconv provider name. The window
# fallback only ever sees Claude Code session records (anthropic); the table
# stays small and defaults to "unknown" rather than guessing wrongly.
_PROVIDER_PREFIXES = (
    ("claude", "anthropic"),
    ("gpt", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("gemini", "google"),
    # xAI's registered OTel semconv value is `x_ai` (semconv registry,
    # gen_ai.provider.name well-known values). Covers every served id the OMP
    # and Grok Build CLI adapters below have been observed to report
    # (`grok-build`, `grok-4.6-build`).
    ("grok", "x_ai"),
)


def provider_for(model):
    m = (model or "").lower()
    for prefix, prov in _PROVIDER_PREFIXES:
        if m.startswith(prefix):
            return prov
    return "unknown"


def _parse_ts(s):
    # RFC 3339 / ISO 8601 with offset (or Z). Returns an aware datetime or None.
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _usage_entry(provider, req_model, resp_model, inp, out, cache_read, cache_write,
                 cost=None):
    """One serotine.usage[*] object with verbatim gen_ai.* keys. Cache keys are
    serotine.* extensions, included only when non-zero to keep lines lean.
    `cost` is a provider-reported amount already vetted by the caller; None is
    absence and adds no member."""
    entry = {
        "gen_ai.provider.name": provider or "unknown",
        "gen_ai.request.model": req_model,
        "gen_ai.usage.input_tokens": _int(inp),
        "gen_ai.usage.output_tokens": _int(out),
    }
    if resp_model:
        entry["gen_ai.response.model"] = resp_model
    if _int(cache_read) > 0:
        entry["serotine.usage.cache_read_tokens"] = _int(cache_read)
    if _int(cache_write) > 0:
        entry["serotine.usage.cache_write_tokens"] = _int(cache_write)
    if cost is not None:
        entry["serotine.usage.cost_usd"] = cost
    return entry


# --- explicit path: parse a `claude -p --output-format json` result ----------

def _cache_write_from(u):
    """Sum cache-creation tokens across the shapes Claude Code emits."""
    cc = u.get("cache_creation") or u.get("cacheCreation") or {}
    if isinstance(cc, dict) and cc:
        return (_int(cc.get("ephemeral_5m_input_tokens"))
                + _int(cc.get("ephemeral_1h_input_tokens")))
    return _int(u.get("cache_creation_input_tokens")
                or u.get("cacheCreationInputTokens"))


def usage_from_claude_json(obj):
    """serotine.usage array from a claude -p result object (or a bare usage obj).

    Prefers the per-model `modelUsage` map (one entry per model, the reliable
    source of the model name); falls back to a single-model `usage` + `model`.
    Entries without a determinable model are dropped — request.model is required
    by the schema, so a modelless entry could never validate.

    A cost is carried only where the file states one for exactly that entry: a
    `modelUsage` entry's own `costUSD` (unless the model is listed in the
    top-level `uncosted_models`), or, on the single-model path, the top-level
    `total_cost_usd` (unless `uncosted_models` lists any model, which makes the
    run total incomplete). A run-level total is never split across models, and
    an unusable cost is absence, never zero.
    """
    entries = []
    model_usage = obj.get("modelUsage") if isinstance(obj, dict) else None
    if isinstance(model_usage, dict) and model_usage:
        uncosted = obj.get("uncosted_models")
        if not isinstance(uncosted, list):
            uncosted = []
        for model, u in model_usage.items():
            if not model or not isinstance(u, dict):
                continue
            cost = u.get("costUSD")
            if model in uncosted or not _is_reported_amount(cost):
                cost = None
            inp = u.get("input_tokens", u.get("inputTokens"))
            out = u.get("output_tokens", u.get("outputTokens"))
            cread = u.get("cache_read_input_tokens", u.get("cacheReadInputTokens"))
            entries.append(_usage_entry(
                provider_for(model), model, model, inp, out, cread, _cache_write_from(u),
                cost))
        return entries

    # Bare usage object (or a result carrying a top-level `usage` + `model`).
    u = obj.get("usage") if isinstance(obj, dict) and "usage" in obj else obj
    if not isinstance(u, dict):
        return entries
    model = obj.get("model") if isinstance(obj, dict) else None
    if not model:
        return entries  # cannot satisfy required gen_ai.request.model
    total = obj.get("total_cost_usd") if isinstance(obj, dict) else None
    uncosted = obj.get("uncosted_models") if isinstance(obj, dict) else None
    if isinstance(uncosted, list) and uncosted:
        total = None
    entries.append(_usage_entry(
        provider_for(model), model, model,
        u.get("input_tokens", u.get("inputTokens")),
        u.get("output_tokens", u.get("outputTokens")),
        u.get("cache_read_input_tokens", u.get("cacheReadInputTokens")),
        _cache_write_from(u),
        total if _is_reported_amount(total) else None))
    return entries


# --- provider adapters: Codex / Gemini usage -> serotine.usage[] -------------
#
# The provider adapters emit verbatim OTel gen_ai.* keys:
# gen_ai.request.model = the wrapper's OWN pinned canonical id (legitimate —
# it IS the pin, by definition), gen_ai.response.model = ONLY what the
# provider itself reports, absent (never fabricated, never copied from the
# request) when the provider doesn't say. usage_from_claude_json can't
# express request != response (it forces both to the one id it found); these
# provider-specific adapters carry a differing response id when the provider
# actually reports one.
#
# Two-layer interface:
# per-provider PARSE function turns raw provider output into ONE candidate
# entry (best-effort, untrusted), then every candidate — from every provider
# — passes through the single shared _validate_usage_entry() gate before it
# can join a returned serotine.usage[] array. A parse function never hands
# its candidate straight to a caller. This is spoof-resistant: even a future
# parser bug (or a malformed/adversarial provider payload) that smuggles in
# an unexpected key or a wrong-typed value gets that key dropped (with a
# warning), not passed through blindly to the JSONL line that later gets
# schema-validated downstream.
#
# Fail-closed, uniformly, for both adapters: a structural parse failure (no
# usable usage record found anywhere in the raw provider output) returns []
# — never zeros. A missing/blank request_model (the caller's own pin) also
# returns [] — gen_ai.request.model is schema-required, so an entry without
# one could never validate anyway. Neither adapter ever falls through to the
# Claude session-window scan (usage_from_window) — that scan only ever finds
# Claude Code session JSONL, so it would silently find nothing for another
# provider, and that silence would be indistinguishable from "no usage" when
# it is really "wrong adapter wired up".


class CodexStdoutWireShapeError(ValueError):
    """Raised by _parse_codex_exec_json when SEROTINE_USAGE_INPUT is
    unambiguously the raw `codex exec --json` STDOUT stream — turn.completed
    events carrying a usage object, with no provider-model-bearing event
    anywhere in the stream — rather than the session ROLLOUT file this
    adapter actually understands. Stdout never carries a served model id, so
    silently emitting from it would mean either a model-less heartbeat (a
    silent, mystifying drift-monitoring gap) or, worse, an inducement to
    fabricate one. Missing model identity must never be invented. A
    subclass of ValueError (this module's existing hard-error idiom — see
    _resolve_provider_usage's unrecognized-provider raise) so it is loud by
    construction: uncaught, it exits serotine_usage.py non-zero with this
    message on stderr; lib.sh's _lk_emit_run_core still swallows it like any
    other emit-hook error (the WRAPPER's own exit code is never touched) —
    that run's line is simply not emitted. Fix the wiring, not the adapter:
    point SEROTINE_USAGE_INPUT at the session rollout file."""


def _codex_stdout_wire_shape_error():
    return CodexStdoutWireShapeError(
        "SEROTINE_USAGE_INPUT looks like raw `codex exec --json` STDOUT "
        "(a turn.completed event carrying a usage object, with no served "
        "model id anywhere in the stream) rather than the codex session "
        "ROLLOUT file. Stdout never carries a provider model id, so this "
        "adapter refuses to emit a model-less or fabricated usage row from "
        "it. Fix: point SEROTINE_USAGE_INPUT at the session rollout file "
        "(~/" ".codex/sessions/**/*.jsonl) instead of the exec --json stdout "
        "capture -- see foundry standards/model-routing.md § Fail-behavior "
        "when the id is missing.")


class TuiOutputWireShapeError(ValueError):
    """Raised by the OMP and Grok Build CLI parsers when SEROTINE_USAGE_INPUT
    holds the client's HUMAN-READABLE TUI transcript instead of the
    machine-readable JSON its headless flag emits — detected as "non-blank
    text carrying no parseable JSON at all".

    Same posture, and same reason, as CodexStdoutWireShapeError above: both
    of these clients will happily print a run's narrative to stdout, and a
    wrapper that captured THAT has a wiring bug, not a usage-free run.
    Returning [] would be indistinguishable from a correctly-wired
    heartbeat-only job and would hide the misconfiguration indefinitely;
    inventing a model id or a token count from prose would fabricate usage.
    ONE class for both clients on purpose: the
    failure, the detection rule and the remedy are identical and differ only
    in which flag the message names.

    A subclass of ValueError, this module's hard-error idiom, so a generic
    `except ValueError` still catches it and so it exits serotine_usage.py
    non-zero with an actionable message; lib.sh's _lk_emit_run_core swallows
    it like any other emit-hook error, so the WRAPPER's own exit code is
    never touched — that run's line is simply not emitted.
    """


def _tui_wire_shape_error(client, json_flag, expected_shape):
    return TuiOutputWireShapeError(
        "SEROTINE_USAGE_INPUT does not hold %s machine-readable output: it "
        "carries no parseable JSON at all, which is what a capture of the "
        "human-readable TUI transcript looks like. This adapter refuses to "
        "emit a usage row it would have to invent from prose. Fix the "
        "wiring, not the adapter: run the seat with `%s` and capture ITS "
        "stdout (%s) -- see foundry standards/model-routing.md § "
        "Fail-behavior when the id is missing." %
        (client, json_flag, expected_shape))


def _as_text(raw):
    """`raw` as str when it is text this module can inspect, else None (a
    dict/list from a programmatic caller, or bytes that are not UTF-8)."""
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return raw if isinstance(raw, str) else None


def _is_nonblank_text(raw):
    text = _as_text(raw)
    return bool(text and text.strip())


_USAGE_ENTRY_TYPES = {
    "gen_ai.provider.name": str,
    "gen_ai.request.model": str,
    "gen_ai.response.model": str,
    "gen_ai.usage.input_tokens": int,
    "gen_ai.usage.output_tokens": int,
    "serotine.usage.cache_read_tokens": int,
    "serotine.usage.cache_write_tokens": int,
    # The schema's one NUMBER-typed member ({"type": "number", "minimum": 0}),
    # explicitly "Advisory only. Ingest recomputes canonically from Serotine's
    # pinned price table and stores both." Only a provider that REPORTS a cost
    # may fill it; there is still no price table in this module.
    "serotine.usage.cost_usd": float,
}
_USAGE_ENTRY_REQUIRED = (
    "gen_ai.provider.name", "gen_ai.request.model",
    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
)


def _validate_usage_entry(candidate):
    """Shared strict VALIDATE/COPY step (layer 2) every provider-adapter
    candidate passes through before it can join a serotine.usage[] array.

    Whitelists exactly the serotine-run-v1 schema's serotine.usage[*] keys
    (the schema itself is additionalProperties:false) — any other key is
    DROPPED with a warning, never passed through blindly. int-typed keys are
    coerced via the existing _int() posture (a malformed individual value ->
    0, matching usage_from_claude_json's tolerance for one bad field — see
    the module's "malformed token values" contract) — with two hard bounds
    the schema imposes:

      * a BOOL is a type violation, checked BEFORE the int coercion —
        bool is an int subclass in Python, so _int(True) would silently
        pass 1 as a token count. The field is omitted (and the required-key
        check below then rejects the entry when the field was required),
        same posture as a wrong-typed string.
      * a NEGATIVE coerced value violates the schema's `minimum: 0` and
        rejects the WHOLE entry (returns None) — an entry carrying it could
        never validate downstream, and a partially-scrubbed count would be
        a fabrication.

    A string-typed key that isn't a NON-BLANK string is simply omitted, not
    coerced — a fabricated placeholder string would be worse than a missing
    field. Blank includes WHITESPACE-ONLY: the schema's `minLength: 1`
    accepts "   ", so truthiness alone would let an id that names nothing
    satisfy every check downstream. If, after whitelisting, any
    schema-required key is still missing, the WHOLE candidate is rejected
    (returns None) — never a partial/malformed entry that could not validate
    anyway.

    The one NUMBER-typed key, serotine.usage.cost_usd, is NOT _int()-style
    coerced: a malformed token count of 0 is a harmless under-count, but a
    malformed cost of 0.0 is an affirmative "this run was free" claim. A
    bool, a non-number, or a NaN/±Inf is therefore OMITTED (the entry
    survives without a cost), while a NEGATIVE cost — like a negative token
    count — rejects the WHOLE entry, tokens included, because a candidate
    carrying it could never validate against `minimum: 0` downstream. The
    adapters that can emit a cost (OMP, Grok Build CLI, and the Claude usage
    file parser) never form a negative one in the first place (they omit it
    at the parse layer, keeping the tokens), so this branch is the backstop
    for a direct caller or a future adapter, not their normal path.
    """
    if not isinstance(candidate, dict):
        return None
    out = {}
    for key, value in candidate.items():
        expected = _USAGE_ENTRY_TYPES.get(key)
        if expected is None:
            warnings.warn(
                "usage entry: dropping unrecognized key %r (not part of the "
                "serotine.usage[*] schema)" % (key,), stacklevel=3)
            continue
        if expected is int:
            if isinstance(value, bool):
                continue  # bool ⊂ int: a type violation, never a token count
            coerced = _int(value)
            if coerced < 0:
                return None  # schema minimum: 0 — the entry could never validate
            out[key] = coerced
        elif expected is float:
            # NUMBER-typed (cost). Deliberately NOT _int()-style coerced: a
            # malformed token count of 0 is a harmless under-count, but a
            # malformed cost of 0.0 is an affirmative "this run was free"
            # claim, so a non-number is OMITTED (like a wrong-typed string)
            # rather than turned into one. bool is excluded first (bool ⊂
            # int ⊂ number in Python), and NaN/±Inf are excluded because
            # json.dumps emits them as bare NaN/Infinity — not valid JSON,
            # so they would wedge the whole spooled batch downstream.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if not math.isfinite(value):
                continue
            if value < 0:
                return None  # schema minimum: 0 — same bound as the counts
            out[key] = value
        elif isinstance(value, str) and value.strip():
            out[key] = value
        # else: wrong-typed/blank string field — omitted, not coerced. BLANK
        # means whitespace-only too: the schema's minLength:1 accepts "   ",
        # so an all-space model id would satisfy every downstream check while
        # carrying no id at all. The value itself is never stripped — a
        # provider-reported id is emitted verbatim or not at all.
    for req in _USAGE_ENTRY_REQUIRED:
        if req not in out:
            return None
    return out


def _iter_provider_json_lines(raw):
    """Yield parsed JSON objects from `raw`, tolerant of the shapes a caller
    might reasonably hand a provider adapter: an already-parsed list of dicts
    (programmatic callers, tests), or str/bytes JSONL text (one JSON object
    per line — what `codex exec --json` streams to stdout). A line that
    fails to parse is skipped, mirroring usage_from_window's per-line
    tolerance: one corrupt line must not sink a whole run's usage."""
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict):
                yield item
        return
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return
    if not isinstance(raw, str):
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            yield d


def _codex_event(d):
    """Resolve one codex JSONL line to (event_type, body) — tolerant of ALL
    the shapes observed in real `codex exec` output on this machine (rollout
    files stamped "originator": "codex_exec"), which are NOT uniform across
    event kinds:

      wrapped      {"type": "event_msg", "payload": {"type": "token_count",
                   ...}} (or {"id": ..., "msg": {"type": ..., ...}}) — the
                   INNER dict carries the event's own type and IS the body.
                   This is how token_count arrives in real rollout files.
      double-      {"payload": {"id": ..., "msg": {"type": "token_count",
      wrapped      ...}}} — the documented protocol shape where "payload"
                   wraps an id'd "msg" envelope; the msg dict carries the
                   event's own type and is the body.
      outer-typed  {"type": "turn_context", "payload": {...turn body, NO
                   nested "type" key...}} — the OUTER type identifies the
                   event and "payload" is the body. This is how turn_context
                   (the event that carries the served "model") arrives in
                   real rollout files. An unwrap that always descended into a
                   dict-valued payload looking for an inner "type" silently
                   skipped this whole event — the empirically-found review
                   bug that made gen_ai.response.model ALWAYS absent for
                   Codex while token counts still captured.
      bare         {"type": "token_count", ...} — the dict is its own body.

    Returns (None, None) when no event type is discernible (e.g. an
    "event_msg" wrapper whose payload carries no inner type — the wrapper
    discriminator itself is never an event type)."""
    payload = d.get("payload") if isinstance(d.get("payload"), dict) else None
    msg = d.get("msg") if isinstance(d.get("msg"), dict) else None
    nested = None                                   # payload WRAPPING msg
    if payload is not None and isinstance(payload.get("msg"), dict):
        nested = payload["msg"]
    for cand in (payload, nested, msg):
        if cand is not None and isinstance(cand.get("type"), str):
            return cand["type"], cand               # wrapped / double-wrapped
    outer = d.get("type")
    if isinstance(outer, str) and outer and outer != "event_msg":
        body = payload if payload is not None else (msg if msg is not None else d)
        return outer, body                          # outer-typed / bare
    return None, None


def _parse_codex_exec_json(raw):
    """PARSE layer (layer 1) for Codex: raw codex session ROLLOUT-file JSONL
    -> one untrusted candidate dict, or None on a structural parse failure
    (no usable token-usage record found anywhere in the stream). Raises
    CodexStdoutWireShapeError instead of returning None when `raw` is
    unambiguously `codex exec --json` STDOUT rather than the rollout file —
    see DETECTED WIRE-SHAPE MISMATCH, below.

    PROVENANCE NOTE: the rollout shape this function actually
    parses is tested against the real rollout
    format of captured `codex exec` output
    ($HOME/.codex/sessions/**/*.jsonl, "originator": "codex_exec"): token_count
    arrives WRAPPED ({"type": "event_msg", "payload": {"type": "token_count",
    ...}}) while turn_context is OUTER-TYPED ({"type": "turn_context",
    "payload": {...body, no nested type...}}) — see _codex_event.

    A separate, REAL capture of `codex exec --json`'s STDOUT stream
    (codex-cli 0.144.1, `codex exec --json 'reply
    with the single word ok' < /dev/null`, committed verbatim at
    testdata/codex-exec-stdout-turn-completed.jsonl) confirms it is a
    DIFFERENT, INCOMPATIBLE wire shape from the rollout file: a
    thread.started/item.completed/turn.started/turn.completed event stream
    where the token usage rides on a top-level "turn.completed" event's
    "usage" object ({"type": "turn.completed", "usage": {"input_tokens": ...,
    "output_tokens": ..., "cached_input_tokens": ..., ...}}) — NOT wrapped
    the way the rollout file's token_count is — and NO event in that stream
    ever carries a served model id (no turn_context event at all). This
    settles the DECIDED CONTRACT (see the module docstring's `provider`
    section): SEROTINE_USAGE_INPUT for codex is REQUIRED to be the session
    rollout file; stdout is model-less by construction and is rejected
    loudly rather than silently emitting a heartbeat-only [] the wiring bug
    would otherwise hide.

    DETECTED WIRE-SHAPE MISMATCH: classified as codex-stdout ONLY when the
    signature is unambiguous — at least one parsed event is a top-level
    "turn.completed" carrying a dict "usage" object, AND no recognized
    provider-model-bearing event (today: "turn_context" with a non-empty
    string "model", the only model source this parser recognizes) exists
    anywhere in the same stream. The second half keeps this conservative: a
    stream that happens to carry a turn.completed event ALONGSIDE a real
    model-bearing turn_context (e.g. a hypothetical mixed/future encoding)
    is never misclassified — it falls through to the ordinary parse below
    exactly as before.

    Looks for the LAST "token_count" event (its info.total_token_usage is
    cumulative for the run, so the last one is the final tally — the same
    "keep last" idiom usage_from_window already uses for Claude's streaming
    chunks) and the served model from the stream's "turn_context" events.
    gen_ai.request.model is deliberately NEVER read from here — it is always
    the caller's own pin, applied by usage_from_codex_exec_json after this
    function returns.

    KNOWN LIMITATION: a session
    whose turn_context events report MULTIPLE DISTINCT served models cannot
    be attributed — total_token_usage is one cumulative counter for the whole
    session, so last-model-wins would charge every earlier model's tokens to
    the final model. In a drift-detection system a WRONG attribution is
    strictly worse than a missing one, so a multi-model session fails closed:
    NO usage entry (heartbeat-only, []) plus a stderr warning naming the
    models seen. Single-model sessions — the launchd `codex exec` case,
    where the wrapper pins one model per run — are unaffected, including
    multi-TURN sessions on that one model (normal last-wins totals).
    """
    token_info = None
    models_seen = []  # distinct served models, in observation order
    saw_any_event = False
    saw_turn_completed_usage = False  # the codex-stdout signature (no wrapping)
    for d in _iter_provider_json_lines(raw):
        ev_type, body = _codex_event(d)
        if ev_type is None:
            continue
        saw_any_event = True
        if ev_type == "token_count":
            info = body.get("info")
            totals = info.get("total_token_usage") if isinstance(info, dict) else None
            if isinstance(totals, dict):
                token_info = totals
        elif ev_type == "turn_context":
            model = body.get("model")
            if isinstance(model, str) and model and model not in models_seen:
                models_seen.append(model)
        elif ev_type == "turn.completed":
            if isinstance(body, dict) and isinstance(body.get("usage"), dict):
                saw_turn_completed_usage = True
    if saw_turn_completed_usage and not models_seen:
        # Unambiguous codex-stdout signature and no model-bearing event
        # anywhere in the stream: loud fail per the DECIDED CONTRACT, before
        # the generic structural-failure path below would otherwise turn
        # this into a silent [] (exactly the wiring bug this guards against).
        raise _codex_stdout_wire_shape_error()
    if not saw_any_event or token_info is None:
        return None  # structural failure: nothing usable in the stream
    if len(models_seen) > 1:
        warnings.warn(
            "codex exec stream reports MULTIPLE distinct served models (%s); "
            "the cumulative total_token_usage cannot be attributed to one "
            "model, so no usage entry is emitted (heartbeat-only, fail "
            "closed — wrong attribution is worse than missing usage for "
            "drift detection)." % ", ".join(models_seen), stacklevel=3)
        return None
    response_model = models_seen[0] if models_seen else None

    candidate = {
        "gen_ai.provider.name": "openai",
        "gen_ai.usage.input_tokens": token_info.get("input_tokens"),
        "gen_ai.usage.output_tokens": token_info.get("output_tokens"),
    }
    cache_read = token_info.get("cached_input_tokens")
    if cache_read:
        candidate["serotine.usage.cache_read_tokens"] = cache_read
    if response_model:
        candidate["gen_ai.response.model"] = response_model
    return candidate


def usage_from_codex_exec_json(raw, request_model=None):
    """serotine.usage array from a raw codex session ROLLOUT-file JSONL (the
    only codex-side input that carries a served model id — see the DECIDED
    CONTRACT in the module docstring's `provider` section).

    `request_model` is the wrapper's OWN pinned canonical id (e.g. the job's
    `expected_model`) — legitimate per the standard's provenance rule (it IS
    the pin, by definition). It is NEVER read from the provider output and
    NEVER copied onto gen_ai.response.model, which comes only from what Codex
    itself reports in the stream (omitted, not fabricated, when the stream
    never carries a model id). Alias normalization (a reported dated/suffixed
    variant vs. the canonical pin) is deliberately NOT done here — per the
    standard, that stays an auditable WRAPPER-layer concern; this adapter
    always emits the raw response id verbatim.

    Fail-closed: a structural parse failure, or a missing/blank
    request_model (schema-required — an entry without one could never
    validate), returns [] — heartbeat-only, never a guess, and this NEVER
    falls through to the Claude session-window scan.

    LOUD EXCEPTION to fail-closed: raises CodexStdoutWireShapeError (does NOT
    return []) when `raw` is unambiguously raw `codex exec --json` STDOUT
    (turn.completed + a usage object, no model id anywhere in the stream)
    rather than the rollout file — see _parse_codex_exec_json. Stdout is a
    wiring bug, not an absent-usage run: silently returning [] here would
    look identical to a correctly-wired heartbeat-only job and hide the
    misconfiguration indefinitely.
    """
    candidate = _parse_codex_exec_json(raw)
    if candidate is None or not request_model:
        return []
    candidate["gen_ai.request.model"] = request_model
    entry = _validate_usage_entry(candidate)
    return [entry] if entry else []


def _coerce_json_obj(raw):
    """Best-effort JSON decode for a single-object provider payload: a dict
    passes through unchanged (matching usage_from_claude_json's convention —
    the caller already parsed it), str/bytes gets decoded. None on anything
    else, or on a decode failure."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None
    return None


def _parse_gemini_response_json(raw):
    """PARSE layer (layer 1) for Gemini: a raw Gemini API response body (the
    public generateContent shape — usageMetadata.{promptTokenCount,
    candidatesTokenCount[, cachedContentTokenCount]} + top-level
    modelVersion) -> one untrusted candidate dict, or None on a structural
    parse failure (not a JSON object, or no usable usageMetadata)."""
    obj = _coerce_json_obj(raw)
    if obj is None:
        return None
    usage = obj.get("usageMetadata")
    if not isinstance(usage, dict):
        return None  # structural failure: not a usage-bearing Gemini response

    candidate = {
        "gen_ai.provider.name": "google",
        "gen_ai.usage.input_tokens": usage.get("promptTokenCount"),
        "gen_ai.usage.output_tokens": usage.get("candidatesTokenCount"),
    }
    cache_read = usage.get("cachedContentTokenCount")
    if cache_read:
        candidate["serotine.usage.cache_read_tokens"] = cache_read
    model = obj.get("modelVersion") or obj.get("model")
    if isinstance(model, str) and model:
        candidate["gen_ai.response.model"] = model
    return candidate


def usage_from_gemini_json(raw, request_model=None):
    """serotine.usage array from a raw Gemini API response body (the public
    generateContent shape: usageMetadata + modelVersion).

    Same provenance contract as the Codex adapter: `request_model` is the
    wrapper's OWN pinned canonical id (never read from the response body);
    gen_ai.response.model comes ONLY from the response's modelVersion
    (omitted, never fabricated, when the response doesn't carry one). No
    alias normalization here either — that stays a wrapper-layer concern.

    Fail-closed: a structural parse failure (not a dict, or no usageMetadata
    object), or a missing/blank request_model, returns [] — never a guess,
    never a fall-through to the Claude session-window scan.
    """
    candidate = _parse_gemini_response_json(raw)
    if candidate is None or not request_model:
        return []
    candidate["gen_ai.request.model"] = request_model
    entry = _validate_usage_entry(candidate)
    return [entry] if entry else []


# --- OMP `--mode json` (multi-message, multi-model) --------------------------


def _is_reported_amount(value):
    """True when `value` is a JSON number this module may treat as a quantity
    the provider actually REPORTED: a real number (bool excluded — bool ⊂ int
    ⊂ number in Python), finite (json.dumps would emit NaN/Infinity as bare
    invalid-JSON tokens and wedge a whole spooled batch), and >= 0 (the
    schema's `minimum: 0`).

    Everything else is "the provider did not report a usable amount". Callers
    must treat that as ABSENCE — omit the member — never as zero.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and value >= 0


def _is_named_id(value):
    """True when `value` is a usable model id: a string with at least one
    non-whitespace character. The schema's `minLength: 1` accepts "   ", so
    truthiness alone lets a blank-looking id satisfy both the required-pin
    gate and downstream validation while naming nothing."""
    return isinstance(value, str) and bool(value.strip())


def _reported_count(value):
    """A provider-reported TOKEN COUNT as a non-negative int, or None when the
    provider did not report a usable one.

    Strict on purpose, and deliberately NOT _int(): _int() answers 0 for
    None/"" /"abc", and 1 for True (bool ⊂ int). Those answers are fine as a
    per-field tolerance on a candidate the shared validator still gets to
    inspect, but they are NOT fine as a term in a SUM — once summed, a
    laundered 1 or an invented 0 is indistinguishable from a real count and
    the validator can no longer see what it was. An integral float (10.0) is
    accepted, since JSON has one number type; a fractional one is not, since
    the schema's counts are integers.
    """
    if not _is_reported_amount(value):
        return None
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    return int(value)


# (wire key, aggregate key, required?) for the four OMP per-message counts.
# The two cache members are OPTIONAL: their absence is a true zero ("no cache
# read reported" and "no cache read" are the same statement, and the module
# already omits a zero cache member from the wire). The absence of input or
# output is NOT a zero — it means the message did not say, which is exactly
# what must never be rendered as a number.
_OMP_COUNT_FIELDS = (
    ("input", "input", True),
    ("output", "output", True),
    ("cacheRead", "cache_read", False),
    ("cacheWrite", "cache_write", False),
)


def _omp_message_totals(messages):
    """Aggregate an OMP `agent_end.messages[]` list into
    {(provider, model): totals}, keeping first-seen order.

    A message counts only when it carries BOTH a non-empty string `model` and
    a dict `usage` — that is the structural requirement for an attributable
    row, and it is what excludes the stream's `user`/`toolResult` messages
    without hard-coding a role whitelist. `provider` is deliberately NOT read
    from the message: OMP reports its own LANE id there (`xai-oauth`,
    `openai-codex`, `google-antigravity`), which is not an OTel semconv
    provider name, so the semconv name is derived from the served model via
    provider_for() — see usage_from_omp_json's docstring.
    """
    agg = {}
    for m in messages:
        if not isinstance(m, dict):
            continue
        model = m.get("model")
        usage = m.get("usage")
        if not _is_named_id(model) or not isinstance(usage, dict):
            continue
        key = (provider_for(model), model)
        tot = agg.setdefault(key, {"input": 0, "output": 0, "cache_read": 0,
                                   "cache_write": 0, "tokens_complete": True,
                                   "cost": 0, "cost_complete": True})

        # Read every count for THIS message first and only commit them if all
        # of them are usable. A message that cannot be read in full poisons
        # its group's totals: a sum missing one message's contribution is a
        # number that looks real and is not, which for a drift-detection feed
        # is worse than no number at all.
        counts, message_readable = {}, True
        for wire_key, agg_key, required in _OMP_COUNT_FIELDS:
            if wire_key not in usage:
                if required:
                    message_readable = False
                counts[agg_key] = 0
                continue
            count = _reported_count(usage[wire_key])
            if count is None:
                message_readable = False
                break
            counts[agg_key] = count
        if not message_readable:
            tot["tokens_complete"] = False
            tot["cost_complete"] = False
            continue
        for agg_key, count in counts.items():
            tot[agg_key] += count

        # OMP reports a per-message notional cost breakdown; only its own
        # `total` is read, and the group's cost is emitted ONLY when EVERY
        # contributing message reported a usable one. Skipping an unusable
        # component while summing its siblings would be arithmetically
        # identical to calling that component $0.00 — the same "this run was
        # free" fabrication the omit-never-coerce rule exists to prevent, and
        # worse for looking complete. One bad component therefore poisons the
        # whole group's cost (tokens are unaffected). No arithmetic on the
        # per-kind component figures either: this module has no price table
        # (module docstring, "Tokens only").
        cost = usage.get("cost")
        total = cost.get("total") if isinstance(cost, dict) else None
        if _is_reported_amount(total):
            tot["cost"] += total
        else:
            tot["cost_complete"] = False
    return agg


def _parse_omp_json(raw):
    """PARSE layer (layer 1) for OMP: a raw `omp -p --mode json` event stream
    (JSONL, one event object per line) -> a LIST of untrusted candidate
    dicts, or None on a structural parse failure.

    Unlike the Codex and Gemini parsers this one returns a LIST, because an
    OMP run is inherently multi-model: every assistant message carries its
    OWN `provider`/`model`/`usage`, so a run that switched models mid-session
    is attributable per model rather than fail-closed the way codex's single
    cumulative counter forces (see _parse_codex_exec_json's KNOWN
    LIMITATION).

    Reads the LAST TERMINAL `agent_end` event's `messages[]` and nothing
    else. The captured wire shape includes the whole `messages[]` array in
    the final `agent_end` event, so one read suffices.
    The per-message `message_end` events carry the same usage and
    are deliberately ignored — reading both would double-count.

    THREE fail-closed conditions, each returning None (-> [] at the adapter,
    i.e. a heartbeat-only line), because every one of them would otherwise
    put a number on the wire that looks like a run total and is not:

      truncated   no `agent_end` at all (a killed run, a `--max-time` cut).
      non-terminal  an `agent_end` present but not marked `isTerminal: true`.
                  Terminality — not position in the stream — is the selector:
                  a non-terminal event is a partial view of the run, and an
                  earlier terminal event is never overridden by a later
                  non-terminal one.
      damaged     any non-blank line that did not parse as a JSON object
                  (warns, naming the count). This is deliberately STRICTER
                  than _iter_provider_json_lines' shared "one corrupt line
                  must not sink a run" tolerance, which stays untouched for
                  codex: that tolerance is right when usage is CUMULATIVE and
                  a later line re-states it, and wrong here, where the whole
                  run's usage lives in ONE event that the unreadable line may
                  have been.
    """
    messages = None
    parsed_objects = 0
    for d in _iter_provider_json_lines(raw):
        parsed_objects += 1
        # TERMINALITY IS THE SELECTOR, not position. An `agent_end` that OMP
        # did not mark terminal is a partial view of the run, and emitting it
        # as the run's usage is an undercount that looks like a total.
        if (d.get("type") == "agent_end" and d.get("isTerminal") is True
                and isinstance(d.get("messages"), list)):
            messages = d["messages"]  # keep LAST terminal event
    if not parsed_objects and _is_nonblank_text(raw):
        # Non-blank text with not one parseable JSON object in it: the
        # `--mode json` flag was missing and this is the TUI transcript.
        raise _tui_wire_shape_error(
            "an OMP", "omp -p --mode json",
            "one JSON event object per line, ending in an `agent_end` event")

    # DAMAGED CAPTURE -> fail closed, deliberately stricter than
    # _iter_provider_json_lines' shared "one corrupt line must not sink a
    # run" tolerance (which stays untouched for codex). That tolerance is
    # right when usage is CUMULATIVE and a later line re-states it, as
    # codex's token_count does. It is wrong here: an OMP run's whole usage
    # lives in ONE terminal event, so a truncated or unreadable line may BE
    # the answer — reading around it can emit a stale earlier event, or
    # nothing, while looking like a clean parse either way.
    text = _as_text(raw)
    if text is not None:
        nonblank_lines = sum(1 for line in text.splitlines() if line.strip())
        if nonblank_lines > parsed_objects:
            warnings.warn(
                "omp stream: %d of %d non-blank line(s) were unparseable or "
                "were not JSON objects; refusing to emit usage from a damaged "
                "capture, because the unreadable line may be the terminal "
                "agent_end this adapter reads." %
                (nonblank_lines - parsed_objects, nonblank_lines), stacklevel=3)
            return None
    if messages is None:
        return None  # structural failure: no TERMINAL agent_end with messages[]

    agg = _omp_message_totals(messages)
    unreadable = [model for (_provider, model), tot in agg.items()
                  if not tot["tokens_complete"]]
    if unreadable:
        # Named, not silent: a dropped model is a gap someone has to be able
        # to diagnose, and this module's idiom for "attributable usage exists
        # but cannot be trusted" is a stderr warning plus fail-closed (see
        # _parse_codex_exec_json's multi-model case).
        warnings.warn(
            "omp stream: dropping usage for model(s) %s — at least one of "
            "their messages did not report readable token counts, so any "
            "total for them would be missing a contribution while looking "
            "complete." % ", ".join(sorted(unreadable)), stacklevel=3)
        agg = {k: v for k, v in agg.items() if v["tokens_complete"]}
    if not agg:
        return None  # structural failure: no attributable message in the run

    candidates = []
    for (provider, model), tot in sorted(agg.items()):
        candidate = {
            "gen_ai.provider.name": provider,
            "gen_ai.response.model": model,
            "gen_ai.usage.input_tokens": tot["input"],
            "gen_ai.usage.output_tokens": tot["output"],
        }
        if tot["cache_read"]:
            candidate["serotine.usage.cache_read_tokens"] = tot["cache_read"]
        if tot["cache_write"]:
            candidate["serotine.usage.cache_write_tokens"] = tot["cache_write"]
        if tot["cost_complete"]:
            candidate["serotine.usage.cost_usd"] = tot["cost"]
        candidates.append(candidate)
    return candidates


def usage_from_omp_json(raw, request_model=None):
    """serotine.usage array from a raw `omp -p --mode json` event stream.

    ONE ENTRY PER DISTINCT (provider, model) with summed tokens — the shape
    the schema's own description asks for ("One entry per (provider, model)
    invoked; multi-model runs get one entry each"). Per-message ids
    (`responseId`) are preserved in the fixture but not emitted; a
    per-message split would need wire fields the schema does not have.

    Same provenance contract as the Codex and Gemini adapters:
    `request_model` is the wrapper's OWN pinned canonical id (the `--model`
    it passed), never read from the stream; gen_ai.response.model is ONLY
    the served `model` OMP reports on the message.

    gen_ai.provider.name is DERIVED from the served model via provider_for(),
    NOT copied from the message's `provider` field. OMP's `provider` is its
    lane/credential id (`xai-oauth`, `openai-codex`, `google-antigravity`) —
    not an OTel semconv provider name, which is what this field is specified
    to carry, and the schema has no field for the lane. Known consequence: a
    model reached through a fronting lane (Claude via `google-antigravity`)
    reports the MODEL's semconv provider, not the lane's; the served model id
    — the thing drift detection actually keys on — is preserved exactly.

    Fail-closed, in three layers. (1) Structural: no terminal `agent_end`, a
    damaged capture, or no attributable message -> []. (2) Per group: a
    (provider, model) whose messages cannot ALL be read in full is dropped
    with a warning naming it — a sum missing one message's contribution is a
    number that looks complete and is not — and a healthy sibling model is
    never dropped with it. (3) Per member: the group's advisory cost is
    emitted only when EVERY contributing message reported a usable one.
    A missing or whitespace-only request_model also returns [].
    """
    candidates = _parse_omp_json(raw)
    if not candidates or not _is_named_id(request_model):
        return []
    entries = []
    for candidate in candidates:
        candidate["gen_ai.request.model"] = request_model
        entry = _validate_usage_entry(candidate)
        if entry:
            entries.append(entry)
    return entries


# --- Grok Build CLI `--output-format json` (single result object) ------------

class _DuplicateJsonKeyError(ValueError):
    """Internal: raised by _reject_duplicate_keys through json.loads. Never
    escapes _parse_grok_build_json, which converts it into a warning + a
    fail-closed None."""


def _reject_duplicate_keys(pairs):
    """json.loads object_pairs_hook that refuses an object naming the same
    key twice, instead of the stdlib's silent last-one-wins."""
    seen = set()
    for key, _value in pairs:
        if key in seen:
            raise _DuplicateJsonKeyError(repr(key))
        seen.add(key)
    return dict(pairs)


# The Grok Build CLI's per-model count members, same required/optional split
# as _OMP_COUNT_FIELDS above (cache members absent = a true zero; input or
# output absent = the CLI did not say, which is never a zero).
_GROK_COUNT_FIELDS = (
    ("inputTokens", "input", True),
    ("outputTokens", "output", True),
    ("cacheReadInputTokens", "cache_read", False),
    ("cacheCreationInputTokens", "cache_write", False),
)


def _parse_grok_build_json(raw):
    """PARSE layer (layer 1) for the Grok Build CLI: a raw `grok -p …
    --output-format json` RESULT OBJECT (one JSON object, not a stream) -> a
    LIST of untrusted candidate dicts, or None on a structural parse failure.

    Reads `modelUsage` and ONLY `modelUsage`. Its KEYS are the SERVED model
    ids — a captured run that requested `grok-4.6` reported the served id
    `grok-4.6-build`. Each value carries that model's own tokens/calls/cost.

    The object's top-level `usage` and `total_cost_usd` are deliberately NOT
    read: they are RUN-level totals with no model attached, so on a run that
    touched two models they could only be charged to one of them. On the
    captured single-model run the two readings agree field for field; per
    model is the one that stays correct when they stop agreeing.
    Consequence, fail-closed by design: a result object with no usable
    `modelUsage` yields None -> [] rather than an entry built from the
    run-level totals with a guessed model id.
    """
    if isinstance(raw, dict):
        obj = raw                       # already parsed by the caller
    else:
        text = _as_text(raw)
        if text is None or not text.strip():
            return None                 # absence, not a wiring bug
        try:
            obj = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        except _DuplicateJsonKeyError as exc:
            # RFC 8259 leaves duplicate names undefined and json.loads
            # silently keeps the LAST one — so a result object naming a
            # served id twice would report the last count as though it were
            # the whole story. An undefined payload is not a payload to emit
            # telemetry from.
            warnings.warn(
                "grok-build result: refusing a payload with a duplicate JSON "
                "key (%s). json.loads silently keeps the last value, so any "
                "usage read from it would be a number that looks real and is "
                "not." % exc, stacklevel=3)
            return None
        except json.JSONDecodeError:
            # Non-blank text that is not JSON at all: `--output-format json`
            # was missing (or a streaming format was captured instead of the
            # single result object) and this is the TUI transcript.
            raise _tui_wire_shape_error(
                "a Grok Build CLI", "grok -p ... --output-format json",
                "ONE JSON result object carrying `modelUsage`") from None
    if not isinstance(obj, dict):
        return None
    model_usage = obj.get("modelUsage")
    if not isinstance(model_usage, dict) or not model_usage:
        return None  # structural failure: nothing model-attributable

    candidates, unreadable = [], []
    # str() on the sort key only: a programmatic caller can hand this a dict
    # whose keys are not all strings, and a bare sorted() would TypeError
    # instead of dropping them at the isinstance check one line below.
    for model, u in sorted(model_usage.items(), key=lambda kv: str(kv[0])):
        if not _is_named_id(model) or not isinstance(u, dict):
            continue
        # Same discipline as the OMP path: read every count first and emit
        # this model only if all of them are readable. A missing/laundered
        # count must not become a plausible 0 (or, for a bool, a 1) on the
        # wire — and it must not take a SIBLING served id down with it.
        counts, model_readable = {}, True
        for wire_key, out_key, required in _GROK_COUNT_FIELDS:
            if wire_key not in u:
                if required:
                    model_readable = False
                counts[out_key] = 0
                continue
            count = _reported_count(u[wire_key])
            if count is None:
                model_readable = False
                break
            counts[out_key] = count
        if not model_readable:
            unreadable.append(model)
            continue

        candidate = {
            "gen_ai.provider.name": provider_for(model),
            "gen_ai.response.model": model,
            "gen_ai.usage.input_tokens": counts["input"],
            "gen_ai.usage.output_tokens": counts["output"],
        }
        if counts["cache_read"]:
            candidate["serotine.usage.cache_read_tokens"] = counts["cache_read"]
        if counts["cache_write"]:
            candidate["serotine.usage.cache_write_tokens"] = counts["cache_write"]
        cost = u.get("costUSD")
        if _is_reported_amount(cost):
            candidate["serotine.usage.cost_usd"] = cost
        candidates.append(candidate)
    if unreadable:
        warnings.warn(
            "grok-build result: dropping usage for served id(s) %s — their "
            "modelUsage entry did not report readable token counts, and a "
            "fabricated 0 is worse than a missing row."
            % ", ".join(sorted(unreadable)), stacklevel=3)
    return candidates or None


def usage_from_grok_build_json(raw, request_model=None):
    """serotine.usage array from a raw Grok Build CLI `--output-format json`
    result object — ONE ENTRY PER SERVED MODEL ID in `modelUsage`.

    Same provenance contract as every adapter here: `request_model` is the
    wrapper's OWN pinned canonical id (the `-m` it passed),
    gen_ai.response.model is ONLY the served id the CLI itself reports. This
    is the first adapter in this module where the two genuinely differ on a
    real capture (`grok-4.6` requested, `grok-4.6-build` served), which is
    precisely the drift signal the field exists for — so no alias
    normalization happens here either; the raw served id is emitted verbatim.

    serotine.usage.cost_usd carries the CLI's own per-model `costUSD` (the
    same figure its run-level `total_cost_usd` reports on a single-model
    run). It is PROVIDER-REPORTED AND ADVISORY, exactly as the schema says
    ("Ingest recomputes canonically from Serotine's pinned price table and
    stores both") — never this fleet's authoritative cost, and never
    computed here.

    Fail-closed: a structural parse failure (not one JSON object, a JSON
    object naming any key twice, or no usable `modelUsage`), or a missing /
    whitespace-only request_model, returns []. A served id whose per-model
    counts cannot be read in full is dropped with a warning naming it, and
    never takes a sibling served id down with it.
    """
    candidates = _parse_grok_build_json(raw)
    if not candidates or not _is_named_id(request_model):
        return []
    entries = []
    for candidate in candidates:
        candidate["gen_ai.request.model"] = request_model
        entry = _validate_usage_entry(candidate)
        if entry:
            entries.append(entry)
    return entries


_PROVIDER_ADAPTERS = {
    "codex": usage_from_codex_exec_json,
    "gemini": usage_from_gemini_json,
    "grok-build": usage_from_grok_build_json,
    "omp": usage_from_omp_json,
}


def _resolve_provider_usage(provider):
    """The SEROTINE_USAGE_PROVIDER opt-in path.
    Once a wrapper sets SEROTINE_USAGE_PROVIDER, that selector OWNS
    usage resolution for the run: it is checked BEFORE the explicit
    SEROTINE_USAGE_JSON/FILE (Claude-shape) path and the window fallback, and
    it never falls through to either — the window fallback in particular is
    Claude-Code-only and would silently find nothing for a non-Claude job, a
    silence indistinguishable from "no usage" when it is really "wrong
    adapter wired up".

    An unrecognized selector is a config bug (a typo'd provider name), not a
    "no usage configured" run, so it raises rather than returning []
    silently. The caller (build_line/main, or a direct library caller) lets
    it propagate as a hard, visible failure. lib.sh's _lk_emit_run_core still
    never fails the WRAPPER's own exit code — its `|| return 0` around the
    whole python3 call swallows this exactly like any other emit-hook error —
    but that run's line is simply not emitted, rather than being silently
    mis-attributed to a Claude window scan that found nothing.
    """
    key = provider.strip().lower()
    adapter = _PROVIDER_ADAPTERS.get(key)
    if adapter is None:
        raise ValueError(
            "SEROTINE_USAGE_PROVIDER=%r is not a recognized provider adapter "
            "(%s) -- refusing to silently fall back to the Claude "
            "session-window scan for a non-Claude job." %
            (provider, "|".join(sorted(_PROVIDER_ADAPTERS))))
    request_model = os.environ.get("SEROTINE_USAGE_REQUEST_MODEL") or None
    path = os.environ.get("SEROTINE_USAGE_INPUT") or ""
    if not path or not os.path.isfile(path):
        return []  # fail closed: no input to parse, never guess
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return []
    return adapter(raw, request_model=request_model)


# --- fallback path: ccusage-style scan of local session JSONL ----------------

def _session_files(projects_dir, session_id):
    """Transcript path(s) for one session id: <projects>/*/<session-id>.jsonl.

    The single `*` matches exactly one slug segment, so a sub-agent transcript
    nested at <slug>/<session-id>/subagents/*.jsonl is NOT matched — the rollup
    stays scoped to the primary session. Session ids are unique, so this is a
    0-or-1 element list."""
    pattern = os.path.join(projects_dir, "*", glob.escape(session_id) + ".jsonl")
    return glob.glob(pattern)


def _run_prefix_files(projects_dir, run_prefix):
    """Transcript paths for every session id sharing a run marker prefix:
    <projects>/*/<run-prefix>*.jsonl. Same idiom as _session_files (one slug
    segment `*` then a filename glob), so nested sub-agent transcripts a
    directory deeper stay excluded exactly as they do for a single session — a
    fan-out job's N sessions are attributed to the run, its sub-agents are not.

    REQUIREMENT for the §3 fan-out wrapper: the run marker MUST end in a
    non-alphanumeric delimiter (e.g. "run1-"). The glob has no word boundary, so
    a bare "run1" prefix ALSO matches "run10-*.jsonl" and would pull a sibling
    run's sessions into this run's rollup — the exact over-count this scoping
    exists to prevent. A prefix ending in an alphanumeric char emits a warning.

    An explicitly-provided-but-empty/whitespace prefix is fail-closed: it
    returns [] (scope requested, matched nothing), NOT a bare `*.jsonl` glob
    that would sweep in the whole slug — the anti-bleed invariant."""
    if not run_prefix or not run_prefix.strip():
        return []
    if run_prefix[-1].isalnum():
        warnings.warn(
            "run_prefix %r does not end in a delimiter: the glob has no word "
            "boundary, so it also matches sessions of any run whose id extends "
            "this prefix (e.g. 'run1' matches 'run10-...'). The fan-out wrapper "
            "must stamp a run marker ending in a non-alphanumeric delimiter."
            % run_prefix,
            stacklevel=2)
    pattern = os.path.join(projects_dir, "*", glob.escape(run_prefix) + "*.jsonl")
    return glob.glob(pattern)


def _session_list_files(projects_dir, session_ids):
    """Transcript paths for an explicit list of session ids (the sidecar-marker
    mode): the de-duplicated union of _session_files over each id, preserving
    first-seen order. Ids that match nothing contribute nothing."""
    paths, seen = [], set()
    for sid in session_ids:
        if not sid:
            continue
        for p in _session_files(projects_dir, sid):
            if p not in seen:
                seen.add(p)
                paths.append(p)
    return paths


def _scoped_paths(projects_dir, session_id, session_ids, run_prefix):
    """Resolve which transcript files the window scan reads, honoring the scope
    precedence RUN_PREFIX > SESSION_IDS > SESSION_ID > whole-tree.

    Requested vs unset is decided by `is None`, NOT truthiness: a scope of None
    means "not requested" (fall through); ANY non-None value — even an empty
    list/string — means "requested", so it returns the scoped result ([] if it
    matches nothing) and NEVER falls back to the whole-tree walk. That fallback
    is what re-introduces cross-session bleed, so an explicitly-empty scope must
    fail closed. (The env layer maps "" → None BEFORE calling, so an unset env
    var still correctly means whole-tree — see resolve_usage.) Only the fully
    unscoped case walks the whole projects tree."""
    if run_prefix is not None:
        return _run_prefix_files(projects_dir, run_prefix)
    if session_ids is not None:
        return _session_list_files(projects_dir, session_ids)
    if session_id is not None:
        return _session_files(projects_dir, session_id)
    return [os.path.join(root, fn)
            for root, _dirs, files in os.walk(projects_dir)
            for fn in files if fn.endswith(".jsonl")]


def usage_from_window(projects_dir, start, end, session_id=None,
                      *, session_ids=None, run_prefix=None):
    """serotine.usage array aggregated per (provider, model) from session JSONL
    records whose timestamp is within [start, end]. Dedup by (message.id,
    requestId) keeping the last cumulative usage — usage_guard.py semantics.

    Scope precedence (all opt-in, all default None=off — backward compatible):
      run_prefix    fan-out mode: scan every session whose id shares this run
                    marker prefix (<projects>/*/<run_prefix>*.jsonl), so one
                    job's MULTIPLE per-run sessions are attributed together. The
                    prefix MUST end in a non-alphanumeric delimiter (e.g.
                    "run1-") or it also matches sibling runs whose id extends it
                    ("run1" ⊇ "run10-..."); a non-delimited prefix warns.
      session_ids   an explicit iterable of session ids sharing a run (the
                    sidecar-marker equivalent of run_prefix).
      session_id    one session's transcript only, so a concurrent session
                    can't bleed into a monitored job's rollup.
      (all None)    walk the whole projects tree in-window.
    Requested vs unset is decided by `is None`: None = not requested; ANY
    non-None value (even [] or "") = requested. So a set-but-unmatched OR an
    explicitly-empty run/session scope yields [] — it deliberately does NOT fall
    back to the whole-tree walk (that would re-introduce the bleed). Callers who
    want whole-tree pass None, never an empty container."""
    start_dt, end_dt = _parse_ts(start), _parse_ts(end)
    if not os.path.isdir(projects_dir):
        return []

    paths = _scoped_paths(projects_dir, session_id, session_ids, run_prefix)

    records = {}  # (msg id|ts, requestId) -> {ts, model, usage}
    for path in paths:
        try:
            fh = open(path, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "assistant":
                    continue
                msg = d.get("message") or {}
                usage = msg.get("usage")
                ts = d.get("timestamp")
                if not usage or not ts:
                    continue
                key = (msg.get("id") or ts, d.get("requestId") or "")
                records[key] = {"ts": ts, "model": msg.get("model") or "unknown",
                                "usage": usage}

    agg = {}  # model -> totals
    for r in records.values():
        rts = _parse_ts(r["ts"])
        if rts is None:
            continue
        if start_dt is not None and rts < start_dt:
            continue
        if end_dt is not None and rts > end_dt:
            continue
        u = r["usage"]
        m = agg.setdefault(r["model"], {"input": 0, "output": 0,
                                        "cache_read": 0, "cache_write": 0})
        m["input"] += _int(u.get("input_tokens"))
        m["output"] += _int(u.get("output_tokens"))
        m["cache_read"] += _int(u.get("cache_read_input_tokens"))
        m["cache_write"] += _cache_write_from(u)

    return [
        _usage_entry(provider_for(model), model, model,
                     m["input"], m["output"], m["cache_read"], m["cache_write"])
        for model, m in sorted(agg.items())
    ]


def _parse_id_list(raw):
    """Parse SEROTINE_USAGE_SESSION_IDS: a comma/whitespace-separated list of
    session ids into a list, or None when empty/unset (so the caller falls
    through to the next scope tier). Empty tokens are dropped."""
    if not raw:
        return None
    ids = raw.replace(",", " ").split()
    return ids or None


def resolve_usage():
    """Pick a usage array from the environment.

    Precedence: SEROTINE_USAGE_PROVIDER (codex|gemini|omp|grok-build, the
    provider-adapter opt-in) wins over the explicit Claude-shape
    SEROTINE_USAGE_JSON/FILE path, which wins over the window fallback. skip
    runs and an explicit disable yield no usage regardless of which path would
    otherwise run.

    Error posture is NOT uniform across paths, by design (ADJUDICATED
    addendum, claude-toolkit U16 §8): the Claude-shape and window paths stay
    best-effort — any error there yields an empty array so a wrapper is never
    failed by a telemetry hook. The provider-adapter path is the one
    exception: config bugs there raise (see _resolve_provider_usage) instead
    of silently returning [] or falling through to the window scan — an
    UNRECOGNIZED SEROTINE_USAGE_PROVIDER selector, codex-stdout miswiring
    (SEROTINE_USAGE_INPUT pointed at `codex exec --json` stdout instead of the
    session rollout file → CodexStdoutWireShapeError), and TUI-transcript
    miswiring on the omp/grok-build seats (SEROTINE_USAGE_INPUT pointed at the
    client's human-readable output instead of its JSON →
    TuiOutputWireShapeError).
    That still can't fail the calling WRAPPER's own exit code — lib.sh's
    _lk_emit_run_core swallows any error from this whole subprocess — it just
    means that one run's line goes unemitted instead of being silently
    mis-attributed.
    """
    if os.environ.get("SEROTINE_LINE_STATUS") == "skip":
        return []
    if os.environ.get("SEROTINE_USAGE_DISABLE") == "1":
        return []

    provider = os.environ.get("SEROTINE_USAGE_PROVIDER") or ""
    if provider:
        return _resolve_provider_usage(provider)

    raw = os.environ.get("SEROTINE_USAGE_JSON") or ""
    path = os.environ.get("SEROTINE_USAGE_FILE") or ""
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            raw = ""
    if raw.strip():
        try:
            return usage_from_claude_json(json.loads(raw))
        except (json.JSONDecodeError, ValueError, TypeError):
            return []

    projects_dir = os.environ.get("SEROTINE_USAGE_PROJECTS_DIR") \
        or os.path.join(os.path.expanduser("~"), ".claude", "projects")
    return usage_from_window(
        projects_dir,
        os.environ.get("SEROTINE_USAGE_WINDOW_START"),
        os.environ.get("SEROTINE_USAGE_WINDOW_END"),
        os.environ.get("SEROTINE_USAGE_SESSION_ID") or None,
        session_ids=_parse_id_list(os.environ.get("SEROTINE_USAGE_SESSION_IDS")),
        run_prefix=os.environ.get("SEROTINE_USAGE_RUN_PREFIX") or None)


# --- failure forensics (serotine.run.failure) --------------------------------

# The wire's closed member set, and the per-member ceilings the builder
# guarantees. These MUST equal telemetry/serotine_trace.py's
# FAILURE_MESSAGE_CHARS / FAILURE_LAST_LOG_LINE_CHARS / FAILURE_STDERR_TAIL_CHARS
# — the two modules are version-locked in this repo and
# test-serotine-usage.py asserts the equality directly, so a bound can never
# drift on one side only.
_FAILURE_MEMBERS = ("message", "last_log_line", "stderr_tail")
_FAILURE_MEMBER_MAX_CHARS = {
    "message": 500,
    "last_log_line": 1000,
    "stderr_tail": 4000,
}


def resolve_failure():
    """The serotine.run.failure object for this line, or {} when there is none.

    Read from ONE variable, SEROTINE_LINE_FAILURE_JSON, which lib.sh passes
    EXPLICITLY on every emit — empty when the SEROTINE_EMIT_FAILURE gate is
    off. That explicit empty assignment is what makes the off path airtight: an
    inherited SEROTINE_FAILURE_* value in a wrapper's own environment is
    shadowed and can never arm this path by accident.

    ALL-OR-NOTHING, and deliberately so. An earlier version filtered per
    member — dropping the bad ones, forwarding the good ones from the same
    response. That reads as robustness and is actually the opposite: a response
    carrying an unknown member, a non-string, a blank, an over-long value or an
    embedded NUL is a response from a helper that is malfunctioning, mis-built
    or substituted, and there is no principled reason to trust the REST of what
    such a helper said. This field is persisted in serotine's `runs.raw` and
    rendered verbatim on an authenticated page, and per serotine's AGENTS.md
    the consequence is ONE-WAY (a later strip protects future lines only) — so
    the whole object is refused, matching the fail-closed posture the rest of
    this lane already takes on a missing redactor and on a timed-out helper.

    A response with NO members is absence, not a violation: {} in, {} out.

    Never raises: unparseable or non-object input answers {} too. A telemetry
    hook never fails its wrapper.
    """
    raw = os.environ.get("SEROTINE_LINE_FAILURE_JSON") or ""
    if not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}
    if not isinstance(obj, dict):
        return {}
    # additionalProperties:false on the wire, and ingest rejection is
    # WHOLE-BATCH ATOMIC — an unknown member would wedge every other line in
    # the same POST, so it is a reason to refuse, never a thing to strip.
    if set(obj) - set(_FAILURE_MEMBERS):
        return {}
    out = {}
    for key in _FAILURE_MEMBERS:
        if key not in obj:
            continue
        value = obj[key]
        if not isinstance(value, str):
            return {}
        # `pattern: "\\S"` on the wire refuses a blank member outright.
        if not value.strip():
            return {}
        if len(value) > _FAILURE_MEMBER_MAX_CHARS[key]:
            return {}
        # A NUL survives json.dumps legally but truncates in C-string consumers
        # downstream; the builder strips them, so one here means the builder is
        # not what produced this.
        if "\x00" in value:
            return {}
        out[key] = value
    return out


# --- line assembly -----------------------------------------------------------

def build_line():
    """Assemble the whole serotine.run.v1 line from SEROTINE_LINE_* env vars. Only
    schema-allowed keys are emitted (the schema is additionalProperties:false)."""
    line = {
        "serotine.schema": "serotine.run.v1",
        "serotine.run.id": os.environ.get("SEROTINE_LINE_RUN_ID", ""),
        "serotine.job.label": os.environ.get("SEROTINE_LINE_JOB_LABEL", ""),
        "host.name": os.environ.get("SEROTINE_LINE_HOST", ""),
        "serotine.run.start": os.environ.get("SEROTINE_LINE_START", ""),
        "serotine.run.end": os.environ.get("SEROTINE_LINE_END", ""),
        "serotine.run.exit_code": _int(os.environ.get("SEROTINE_LINE_EXIT_CODE")),
        "serotine.run.status": os.environ.get("SEROTINE_LINE_STATUS", "ok"),
        "serotine.run.trigger": os.environ.get("SEROTINE_LINE_TRIGGER", "launchd"),
    }
    usage = resolve_usage()
    if usage:
        operation = os.environ.get("SEROTINE_LINE_OPERATION") or "invoke_agent"
        line["gen_ai.operation.name"] = operation
        line["serotine.usage"] = usage
    # Optional, off by default, and omitted rather than emitted empty: a line
    # with no failure key is byte-for-byte the line this schema accepted before
    # the field existed (serotine AGENTS.md § "Failure forensics on a run line").
    failure = resolve_failure()
    if failure:
        line["serotine.run.failure"] = failure
    return line


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "build-line":
        print(json.dumps(build_line(), separators=(", ", ": ")))
        return 0
    if cmd == "usage":
        print(json.dumps(resolve_usage(), separators=(", ", ": ")))
        return 0
    print(f"usage: {os.path.basename(argv[0])} build-line|usage", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
