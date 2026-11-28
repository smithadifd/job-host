#!/usr/bin/env python3
"""Unit tests for the Codex/Gemini/OMP/Grok-Build provider usage adapters in
serotine_usage.py and their model-id capture contract.

Run with:  python3 launchd-kit/test-serotine-usage.py
       or:  cd launchd-kit && python3 test-serotine-usage.py

Covers:
  1. The standard's two worked examples (§ Worked examples), reproduced
     EXACTLY (request_model constructed per the pinned input shapes; expected
     output matches the standard's fragment field-for-field).
  2. One request != response test per provider (the split
     usage_from_claude_json cannot express — the reason these adapters exist).
  3. The shared _validate_usage_entry() gate: whitelist + drop-unknown-with-
     warning, int coercion via the existing _int() posture, required-field
     rejection (whole entry dropped, not partial).
  4. Fail-closed posture: structural parse failures return [] (not zeros);
     a malformed individual token VALUE is _int()-coerced to 0 (contrast with
     #4); a missing/blank request_model returns [].
  5. resolve_usage()'s SEROTINE_USAGE_PROVIDER opt-in: precedence over
     SEROTINE_USAGE_JSON/FILE and the window fallback, unset = byte-identical,
     and an unrecognized selector is a hard error (raises), never a silent []
     and never a silent fall-through to the Claude session-window scan.
  6. Existing-adapter regression: usage_from_claude_json's token members are
     unaffected by any of the above (a light smoke check; the full regression suite lives in
     test-lib.sh section (l) and this repo's other existing tests).
  6b. The OMP and Grok Build providers,
     each against a REAL capture committed under testdata/ byte-for-byte:
       omp         `--mode json` event stream -> one entry per distinct
                   (provider, model), summed, from the terminal agent_end.
       grok-build  `--output-format json` result object -> one entry per
                   SERVED id in modelUsage (the module's first real
                   request≠response echo: `grok-4.6` in, `grok-4.6-build`
                   out).
     plus, for both: the provider-reported advisory cost member, and the
     TuiOutputWireShapeError refusal when the capture is the client's
     human-readable TUI transcript instead of its JSON.
  6c. serotine.run.v1 conformance of the ASSEMBLED line for both new
     providers, against the committed schema via the repo's own vendored
     validator (the wire is additionalProperties:false and ingest rejection
     is whole-batch atomic, so an unwireable member must fail here).
  7. codex-stdout STDOUT wire-shape misdetection:
     a real captured `codex exec --json` stdout specimen
     (testdata/codex-exec-stdout-turn-completed.jsonl) raises
     CodexStdoutWireShapeError loudly instead of returning [] silently;
     detection is pinned to turn.completed+dict-usage with no model-bearing
     event anywhere in the stream, and a mixed stream that ALSO carries a
     real model-bearing event must never be misclassified.

Fixtures under testdata/ are loaded relative to this file's own directory
(_HERE), not the process cwd, so `python3 test-serotine-usage.py` works from
either invocation form shown above.

Never touches the owner's real configuration: SEROTINE_USAGE_PROVIDER short-circuits
resolve_usage() before it would ever reach SEROTINE_USAGE_PROJECTS_DIR, and
every test that exercises resolve_usage() runs inside scrubbed_env() with an
explicit, temp-file-backed SEROTINE_USAGE_INPUT.
"""

import contextlib
import importlib.util
import json
import os
import re
import subprocess
import tempfile
import unittest
import warnings

_HERE = os.path.dirname(os.path.abspath(__file__))
_TESTDATA_DIR = os.path.join(_HERE, "testdata")

# Sentinel for "the provider did not report this member at all", which the
# adapters must distinguish from "the provider reported zero".
_ABSENT = object()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_fixture(name):
    """Read a testdata/ fixture file's raw text (relative to _HERE, not the
    process cwd — see the module docstring)."""
    with open(os.path.join(_TESTDATA_DIR, name), encoding="utf-8") as f:
        return f.read()


su = _load("serotine_usage", os.path.join(_HERE, "serotine_usage.py"))


@contextlib.contextmanager
def scrubbed_env(**extra):
    """Run with every SEROTINE_*/LK_/TOOL_NAME var removed, then `extra` set —
    mirrors test-serotine-spool.py's helper so these tests never inherit live
    telemetry config (or accidentally point at the owner's real data) from the
    invoking shell."""
    saved = {k: v for k, v in os.environ.items()
             if k.startswith(("SEROTINE_", "LK_")) or k == "TOOL_NAME"}
    for k in saved:
        del os.environ[k]
    os.environ.update(extra)
    try:
        yield
    finally:
        for k in list(extra):
            os.environ.pop(k, None)
        os.environ.update(saved)


# --- fixture builders ---------------------------------------------------

def codex_jsonl(model=None, input_tokens=0, output_tokens=0,
                cached_input_tokens=None, extra_lines=()):
    """Build a codex-exec-shaped JSONL string that is STRUCTURALLY FAITHFUL
    to the real on-disk rollout format observed on this machine
    ($HOME/.codex/sessions/**/*.jsonl, "originator": "codex_exec") — every VALUE
    is synthetic (invented model ids, token counts, timestamps, turn ids);
    only the KEY STRUCTURE mirrors the real format:

      turn_context  OUTER-typed: {"type": "turn_context", "payload": {...turn
                    body carrying "model", NO nested "type" key...}}. This is
                    the shape that requires retaining the outer event type:
                    descending into payload finds no inner type and would
                    silently drop the served model.
      token_count   WRAPPED: {"type": "event_msg", "payload": {"type":
                    "token_count", "info": {"total_token_usage": {...}}}}.
    """
    lines = list(extra_lines)
    if model is not None:
        lines.append(json.dumps({
            "timestamp": "2026-07-18T14:52:56.829Z",
            "type": "turn_context",
            "payload": {"turn_id": "0000-synthetic-turn",
                        "cwd": "/tmp/synthetic",
                        "model": model}}))
    totals = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    if cached_input_tokens is not None:
        totals["cached_input_tokens"] = cached_input_tokens
    lines.append(json.dumps({
        "timestamp": "2026-07-18T14:53:06.717Z",
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {"total_token_usage": totals}}}))
    return "\n".join(lines) + "\n"


def gemini_response(model_version=None, prompt_tokens=0, candidates_tokens=0,
                    cached_tokens=None):
    obj = {"usageMetadata": {"promptTokenCount": prompt_tokens,
                             "candidatesTokenCount": candidates_tokens}}
    if cached_tokens is not None:
        obj["usageMetadata"]["cachedContentTokenCount"] = cached_tokens
    if model_version is not None:
        obj["modelVersion"] = model_version
    return obj


# --- (1) the standard's two worked examples, verbatim -------------------

class WorkedExampleTests(unittest.TestCase):
    """Two worked examples with identical request/response ids.
    These tests construct provider inputs and assert the expected output
    fragments field-for-field."""

    def test_codex_worked_example(self):
        raw = codex_jsonl(model="gpt-5.4", input_tokens=4120, output_tokens=880)
        entries = su.usage_from_codex_exec_json(raw, request_model="gpt-5.4")
        self.assertEqual(entries, [{
            "gen_ai.provider.name": "openai",
            "gen_ai.request.model": "gpt-5.4",
            "gen_ai.response.model": "gpt-5.4",
            "gen_ai.usage.input_tokens": 4120,
            "gen_ai.usage.output_tokens": 880,
        }])

    def test_gemini_worked_example(self):
        obj = gemini_response(model_version="gemini-2.5-pro",
                              prompt_tokens=6300, candidates_tokens=1450)
        entries = su.usage_from_gemini_json(obj, request_model="gemini-2.5-pro")
        self.assertEqual(entries, [{
            "gen_ai.provider.name": "google",
            "gen_ai.request.model": "gemini-2.5-pro",
            "gen_ai.response.model": "gemini-2.5-pro",
            "gen_ai.usage.input_tokens": 6300,
            "gen_ai.usage.output_tokens": 1450,
        }])


# --- (2) request != response — the split these adapters exist for -------

class RequestResponseSplitTests(unittest.TestCase):
    """A provider can report a dated alias (gpt-5.4-2026-06-01) while the
    wrapper requests an undated id. Alias normalization stays a WRAPPER-layer
    concern — this adapter emits the raw response id verbatim, unnormalized.
    The adapter must preserve request != response, unlike
    usage_from_claude_json's single-id representation."""

    def test_codex_reports_dated_alias_verbatim(self):
        raw = codex_jsonl(model="gpt-5.4-2026-06-01",
                          input_tokens=4120, output_tokens=880)
        entries = su.usage_from_codex_exec_json(raw, request_model="gpt-5.4")
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["gen_ai.request.model"], "gpt-5.4")
        self.assertEqual(e["gen_ai.response.model"], "gpt-5.4-2026-06-01")
        self.assertNotEqual(e["gen_ai.request.model"], e["gen_ai.response.model"])

    def test_gemini_reports_dated_variant_verbatim(self):
        obj = gemini_response(model_version="gemini-2.5-pro-002",
                              prompt_tokens=6300, candidates_tokens=1450)
        entries = su.usage_from_gemini_json(obj, request_model="gemini-2.5-pro")
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["gen_ai.request.model"], "gemini-2.5-pro")
        self.assertEqual(e["gen_ai.response.model"], "gemini-2.5-pro-002")
        self.assertNotEqual(e["gen_ai.request.model"], e["gen_ai.response.model"])


# --- (2b) the real rollout event shapes — parsing regression -------------

class CodexEventShapeTests(unittest.TestCase):
    """Captured $HOME/.codex/sessions rollout files have event shapes that
    are NOT uniform: token_count is
    WRAPPED under {"type": "event_msg", "payload": {"type": ...}} while
    turn_context is OUTER-typed ({"type": "turn_context"} with the model in a
    payload body that has NO nested "type"). The original unwrap descended
    into any dict payload looking for an inner type, so it silently dropped
    every turn_context event — token counts captured but
    gen_ai.response.model was ALWAYS absent for Codex, i.e. the
    request != response capture this feature exists for never fired. These
    tests pin the fix. All fixture VALUES are synthetic; only the key
    structure mirrors the real format."""

    def test_rollout_outer_typed_turn_context_populates_response_model(self):
        # the exact regression: a structurally-faithful rollout stream (outer-
        # typed turn_context + wrapped token_count) MUST yield a response model.
        raw = "\n".join([
            json.dumps({
                "timestamp": "2026-07-18T14:52:56.829Z",
                "type": "turn_context",
                "payload": {"turn_id": "0000-synthetic-turn",
                            "cwd": "/tmp/synthetic",
                            "approval_policy": "never",
                            "model": "gpt-5.4-2026-06-01"}}),
            json.dumps({
                "timestamp": "2026-07-18T14:53:06.717Z",
                "type": "event_msg",
                "payload": {"type": "token_count",
                            "info": {"total_token_usage": {
                                "input_tokens": 11, "output_tokens": 7,
                                "reasoning_output_tokens": 2,
                                "total_tokens": 18}}}}),
        ]) + "\n"
        entries = su.usage_from_codex_exec_json(raw, request_model="gpt-5.4")
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["gen_ai.response.model"], "gpt-5.4-2026-06-01")
        self.assertEqual(e["gen_ai.request.model"], "gpt-5.4")
        self.assertNotEqual(e["gen_ai.request.model"], e["gen_ai.response.model"])
        self.assertEqual(e["gen_ai.usage.input_tokens"], 11)
        self.assertEqual(e["gen_ai.usage.output_tokens"], 7)

    def test_wrapped_turn_context_still_tolerated(self):
        # a hypothetical stdout wire format could wrap turn_context like the
        # other event_msg events do — the unwrap must absorb that too.
        raw = "\n".join([
            json.dumps({"type": "event_msg",
                        "payload": {"type": "turn_context", "model": "gpt-5.4-w"}}),
            json.dumps({"type": "event_msg",
                        "payload": {"type": "token_count",
                                    "info": {"total_token_usage": {
                                        "input_tokens": 1, "output_tokens": 2}}}}),
        ]) + "\n"
        entries = su.usage_from_codex_exec_json(raw, request_model="gpt-5.4")
        self.assertEqual(entries[0]["gen_ai.response.model"], "gpt-5.4-w")

    def test_bare_events_still_tolerated(self):
        raw = "\n".join([
            json.dumps({"type": "turn_context", "model": "gpt-5.4-b"}),
            json.dumps({"type": "token_count",
                        "info": {"total_token_usage": {
                            "input_tokens": 3, "output_tokens": 4}}}),
        ]) + "\n"
        entries = su.usage_from_codex_exec_json(raw, request_model="gpt-5.4")
        self.assertEqual(entries[0]["gen_ai.response.model"], "gpt-5.4-b")
        self.assertEqual(entries[0]["gen_ai.usage.input_tokens"], 3)

    def test_event_msg_wrapper_without_inner_type_is_skipped(self):
        # "event_msg" is a wrapper discriminator, never an event type — a
        # payload with no inner type is unidentifiable and must be skipped,
        # not treated as an event named "event_msg".
        raw = json.dumps({"type": "event_msg", "payload": {"model": "spoof"}}) + "\n"
        self.assertEqual(su.usage_from_codex_exec_json(raw, request_model="gpt-5.4"), [])

    def test_double_wrapped_payload_msg_shape_tolerated(self):
        # The documented protocol shape where
        # "payload" wraps an id'd "msg" envelope carrying the event's type —
        # {"payload": {"id": ..., "msg": {"type": "token_count", ...}}}.
        raw = "\n".join([
            json.dumps({"payload": {"id": "0", "msg": {
                "type": "turn_context", "model": "gpt-5.4-dw"}}}),
            json.dumps({"payload": {"id": "1", "msg": {
                "type": "token_count",
                "info": {"total_token_usage": {
                    "input_tokens": 21, "output_tokens": 8}}}}}),
        ]) + "\n"
        entries = su.usage_from_codex_exec_json(raw, request_model="gpt-5.4")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["gen_ai.usage.input_tokens"], 21)
        self.assertEqual(entries[0]["gen_ai.usage.output_tokens"], 8)
        self.assertEqual(entries[0]["gen_ai.response.model"], "gpt-5.4-dw")


# --- (2c) multi-model sessions fail closed -------------------------------

class CodexMultiModelTests(unittest.TestCase):
    """total_token_usage is ONE cumulative counter for the whole session, so
    a session whose turn_context events report multiple DISTINCT served
    models cannot be attributed — last-model-wins would charge every earlier
    model's tokens to the final model. Adjudicated fix: fail closed to []
    (heartbeat-only) with a warning naming the models seen; single-model
    sessions (the launchd `codex exec` case), including multi-TURN ones,
    keep normal last-wins totals."""

    @staticmethod
    def _turn(model):
        return json.dumps({
            "timestamp": "2026-07-18T14:52:56.829Z", "type": "turn_context",
            "payload": {"turn_id": "0000-synthetic", "model": model}})

    @staticmethod
    def _count(inp, out):
        return json.dumps({
            "timestamp": "2026-07-18T14:53:06.717Z", "type": "event_msg",
            "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": inp,
                                      "output_tokens": out}}}})

    def test_two_distinct_models_fail_closed_with_warning(self):
        raw = "\n".join([
            self._turn("gpt-5.4"), self._count(10, 2),
            self._turn("gpt-5.3-codex-spark"), self._count(50, 9),
        ]) + "\n"
        with self.assertWarns(UserWarning) as cm:
            entries = su.usage_from_codex_exec_json(raw, request_model="gpt-5.4")
        self.assertEqual(entries, [])  # heartbeat-only, never last-model-wins
        self.assertIn("gpt-5.4", str(cm.warning))
        self.assertIn("gpt-5.3-codex-spark", str(cm.warning))

    def test_single_model_multi_turn_keeps_last_wins_totals(self):
        raw = "\n".join([
            self._turn("gpt-5.4"), self._count(10, 2),
            self._turn("gpt-5.4"), self._count(50, 9),  # same model, 2nd turn
        ]) + "\n"
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any warning would raise
            entries = su.usage_from_codex_exec_json(raw, request_model="gpt-5.4")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["gen_ai.usage.input_tokens"], 50)
        self.assertEqual(entries[0]["gen_ai.usage.output_tokens"], 9)
        self.assertEqual(entries[0]["gen_ai.response.model"], "gpt-5.4")


# --- (2d) codex-stdout STDOUT wire-shape misdetection — DECIDED CONTRACT --

class CodexStdoutWireShapeTests(unittest.TestCase):
    """Codex input contract:
    SEROTINE_USAGE_INPUT for codex MUST be the session ROLLOUT file — raw
    `codex exec --json` STDOUT never carries a served model id at all, so
    pointing SEROTINE_USAGE_INPUT at it is a wiring bug, not an absent-usage
    run.

    A real specimen (codex-cli 0.144.1, `codex exec --json 'reply with the
    single word ok' < /dev/null`, captured verbatim, nothing redacted — the
    prompt is trivial) is committed at
    testdata/codex-exec-stdout-turn-completed.jsonl and confirms the shape
    contains a thread.started/item.completed/turn.started/
    turn.completed event stream where token usage rides on a top-level
    "turn.completed" event's "usage" object, and no event anywhere in the
    stream carries a model id (no turn_context event at all — that's a
    rollout-file-only event).

    Detection must fail LOUDLY (CodexStdoutWireShapeError, not a silent [])
    and stay conservative: pinned to turn.completed + a dict usage object
    AND no recognized model-bearing event (turn_context.model) anywhere in
    the same stream — a stream that also carries a real turn_context must
    never be misclassified."""

    def test_captured_stdout_specimen_raises_actionable_error(self):
        raw = _load_fixture("codex-exec-stdout-turn-completed.jsonl")
        with self.assertRaises(su.CodexStdoutWireShapeError) as cm:
            su.usage_from_codex_exec_json(raw, request_model="gpt-5.4")
        msg = str(cm.exception)
        self.assertIn("SEROTINE_USAGE_INPUT", msg)
        self.assertIn("rollout", msg.lower())

    def test_captured_specimen_shape_matches_the_ticket(self):
        # Pins what the real capture actually looks like, so a future
        # codex-CLI wire-format change shows up here first, not as a silent
        # detection gap.
        raw = _load_fixture("codex-exec-stdout-turn-completed.jsonl")
        events = [json.loads(line) for line in raw.splitlines() if line.strip()]
        types = [d.get("type") for d in events]
        self.assertIn("turn.completed", types)
        turn_completed = [d for d in events if d.get("type") == "turn.completed"]
        self.assertTrue(turn_completed)
        self.assertTrue(all(isinstance(d.get("usage"), dict) for d in turn_completed))
        self.assertNotIn("turn_context", types)  # no rollout-shape model event

    def test_minimal_synthetic_turn_completed_usage_raises(self):
        # Pins the rule independent of the captured specimen's incidental
        # extra lines (thread.started / item.completed / turn.started noise)
        # — the minimal signature alone must be sufficient.
        raw = json.dumps({"type": "turn.completed",
                          "usage": {"input_tokens": 10, "output_tokens": 2}}) + "\n"
        with self.assertRaises(su.CodexStdoutWireShapeError):
            su.usage_from_codex_exec_json(raw, request_model="gpt-5.4")

    def test_raises_even_without_a_request_model(self):
        # The loud failure fires inside the PARSE layer, before the
        # request_model gate — a wiring bug must not be masked by an
        # ALSO-missing pin quietly producing the ordinary [] instead.
        raw = json.dumps({"type": "turn.completed",
                          "usage": {"input_tokens": 10, "output_tokens": 2}}) + "\n"
        with self.assertRaises(su.CodexStdoutWireShapeError):
            su.usage_from_codex_exec_json(raw, request_model=None)

    def test_mixed_stream_with_model_bearing_event_does_not_raise(self):
        # turn.completed co-existing with a REAL turn_context (model-bearing)
        # must never trip the loud failure — the conservative half of the
        # pinned detection rule.
        lines = [
            json.dumps({"type": "turn_context",
                       "payload": {"turn_id": "t0", "model": "gpt-5.4"}}),
            json.dumps({"type": "event_msg", "payload": {"type": "token_count",
                       "info": {"total_token_usage": {"input_tokens": 10,
                                                       "output_tokens": 2}}}}),
            json.dumps({"type": "turn.completed",
                       "usage": {"input_tokens": 10, "output_tokens": 2}}),
        ]
        raw = "\n".join(lines) + "\n"
        entries = su.usage_from_codex_exec_json(raw, request_model="gpt-5.4")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["gen_ai.response.model"], "gpt-5.4")
        self.assertEqual(entries[0]["gen_ai.usage.input_tokens"], 10)

    def test_turn_completed_without_dict_usage_does_not_raise(self):
        # A turn.completed event whose usage is missing/not-a-dict isn't the
        # pinned signature — falls through to the ordinary structural-
        # failure path (returns []), not the loud error.
        raw = json.dumps({"type": "turn.completed", "usage": "not-a-dict"}) + "\n"
        self.assertEqual(
            su.usage_from_codex_exec_json(raw, request_model="gpt-5.4"), [])

    def test_error_is_a_value_error_subclass(self):
        # Matches the module's existing hard-error idiom (the unrecognized-
        # SEROTINE_USAGE_PROVIDER raise in _resolve_provider_usage) so any
        # generic `except ValueError` elsewhere still catches it.
        self.assertTrue(issubclass(su.CodexStdoutWireShapeError, ValueError))


# --- (2e) OMP `--mode json` adapter --------------------------------------

class OmpAdapterTests(unittest.TestCase):
    """`usage_from_omp_json` over the captured OMP `--mode json` event stream
    in testdata/omp-mode-json-bucket-grok-build.jsonl.

    Independent expected totals check the adapter's aggregation: 7 assistant
    messages use provider=xai-oauth and model=grok-build across 324 parseable
    events. The 132,971 tokens comprise 17,833 input, 2,498 output, and 112,640
    cache-read tokens; the separately reported reasoning count is 1,662.
    The advisory cost is $0.0511 to four decimal places; ingest recomputes it.
    """

    # --- synthetic builders, structurally faithful to the capture --------
    #
    # Same posture as codex_jsonl() above: the KEY STRUCTURE mirrors the real
    # `agent_end`/assistant-message shape verified against the committed
    # fixture; every VALUE is invented. Used only where the captured run
    # (single-model, well-formed, complete) cannot exercise the rule.

    @staticmethod
    def _msg(model, inp, out, cache_read=0, cache_write=0, cost=_ABSENT,
             usage=_ABSENT):
        m = {"role": "assistant", "provider": "xai-oauth", "model": model,
             "responseId": "rs_synthetic", "api": "openai-responses"}
        if usage is not _ABSENT:
            m["usage"] = usage
            return m
        m["usage"] = {"input": inp, "output": out,
                      "cacheRead": cache_read, "cacheWrite": cache_write}
        if cost is not _ABSENT:
            m["usage"]["cost"] = {"input": 0, "output": 0, "cacheRead": 0,
                                  "cacheWrite": 0, "total": cost}
        return m

    @staticmethod
    def _agent_end(messages, is_terminal=True, extra_lines=()):
        event = {"type": "agent_end", "messages": messages}
        if is_terminal is not None:
            event["isTerminal"] = is_terminal
        return "\n".join([json.dumps(event)] + list(extra_lines)) + "\n"

    def test_captured_specimen_shape_matches_the_memo(self):
        # Pins what the real capture actually looks like — the independent
        # source of truth every expectation in this class rests on, and the
        # place a future OMP wire change surfaces first. Memo § Verdicts 1:
        # "7 assistant messages, all provider=xai-oauth model=grok-build …
        # 324 events, 0 unparseable".
        raw = _load_fixture("omp-mode-json-bucket-grok-build.jsonl")
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 324)
        events = [json.loads(ln) for ln in lines]  # 0 unparseable, or this raises
        ends = [d for d in events if d.get("type") == "agent_end"]
        self.assertEqual(len(ends), 1)
        self.assertIs(ends[0]["isTerminal"], True)
        assistants = [m for m in ends[0]["messages"]
                      if m.get("role") == "assistant"]
        self.assertEqual(len(assistants), 7)
        self.assertEqual({m["provider"] for m in assistants}, {"xai-oauth"})
        self.assertEqual({m["model"] for m in assistants}, {"grok-build"})

    # --- only a TERMINAL, undamaged stream is attributable (FIX 3) -------

    def test_a_non_terminal_agent_end_is_not_the_runs_usage(self):
        raw = self._agent_end([self._msg("grok-build", 10, 3)],
                              is_terminal=False)
        self.assertEqual(su.usage_from_omp_json(raw, request_model="pin"), [])

    def test_an_agent_end_without_is_terminal_is_not_the_runs_usage(self):
        raw = self._agent_end([self._msg("grok-build", 10, 3)],
                              is_terminal=None)
        self.assertEqual(su.usage_from_omp_json(raw, request_model="pin"), [])

    def test_a_terminal_event_never_loses_to_a_later_non_terminal_one(self):
        # ordering must not decide it: terminality does.
        raw = (self._agent_end([self._msg("grok-build", 10, 3)]).rstrip("\n")
               + "\n"
               + json.dumps({"type": "agent_end", "isTerminal": False,
                             "messages": [
                                 {"role": "assistant", "provider": "xai-oauth",
                                  "model": "grok-build",
                                  "usage": {"input": 999, "output": 999,
                                            "cacheRead": 0, "cacheWrite": 0}}]})
               + "\n")
        entries = su.usage_from_omp_json(raw, request_model="pin")
        self.assertEqual(entries[0]["gen_ai.usage.input_tokens"], 10)

    def test_a_damaged_stream_is_refused_rather_than_read_around(self):
        # A truncated trailing line may BE the terminal event — OMP's usage
        # lives entirely in that one event, so "skip the bad line" can
        # silently emit a stale earlier one as the run's total.
        raw = self._agent_end(
            [self._msg("grok-build", 10, 3)],
            extra_lines=['{"type": "agent_en'])  # truncated mid-write
        with self.assertWarns(UserWarning) as cm:
            entries = su.usage_from_omp_json(raw, request_model="pin")
        self.assertEqual(entries, [])
        self.assertIn("unparseable", str(cm.warning).lower())

    def test_captured_stream_yields_one_entry_with_the_served_model(self):
        raw = _load_fixture("omp-mode-json-bucket-grok-build.jsonl")
        entries = su.usage_from_omp_json(raw, request_model="grok-build")
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["gen_ai.response.model"], "grok-build")
        self.assertEqual(entry["gen_ai.provider.name"], "x_ai")
        self.assertEqual(entry["gen_ai.usage.input_tokens"], 17833)
        self.assertEqual(entry["gen_ai.usage.output_tokens"], 2498)
        self.assertEqual(entry["serotine.usage.cache_read_tokens"], 112640)

    def test_captured_stream_carries_omps_notional_cost(self):
        # The captured stream reports an advisory cost of $0.0511 to four
        # decimal places. Ingest recomputes it rather than trusting this value.
        raw = _load_fixture("omp-mode-json-bucket-grok-build.jsonl")
        entries = su.usage_from_omp_json(raw, request_model="grok-build")
        self.assertAlmostEqual(entries[0]["serotine.usage.cost_usd"],
                               0.0511, places=4)

    def test_request_model_is_the_pin_and_is_never_read_from_the_stream(self):
        # The provenance rule the whole adapter family exists for: the pin is
        # the caller's, the served id is the provider's, and neither is ever
        # copied onto the other.
        raw = _load_fixture("omp-mode-json-bucket-grok-build.jsonl")
        entries = su.usage_from_omp_json(raw, request_model="xai-oauth/pinned")
        self.assertEqual(entries[0]["gen_ai.request.model"], "xai-oauth/pinned")
        self.assertEqual(entries[0]["gen_ai.response.model"], "grok-build")

    def test_one_entry_per_distinct_model_with_summed_tokens(self):
        # SYNTHETIC agent_end (structurally faithful to the captured stream's
        # event/message keys; every value invented) — the captured run is
        # single-model, so the per-model split is pinned here instead.
        raw = json.dumps({"type": "agent_end", "isTerminal": True, "messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "provider": "xai-oauth", "model": "grok-build",
             "usage": {"input": 10, "output": 3, "cacheRead": 100, "cacheWrite": 0}},
            {"role": "toolResult", "toolName": "bash", "content": "ok"},
            {"role": "assistant", "provider": "xai-oauth", "model": "grok-build",
             "usage": {"input": 5, "output": 2, "cacheRead": 20, "cacheWrite": 0}},
            {"role": "assistant", "provider": "openai-codex", "model": "gpt-5.6-sol",
             "usage": {"input": 7, "output": 1, "cacheRead": 0, "cacheWrite": 4}},
        ]}) + "\n"
        entries = su.usage_from_omp_json(raw, request_model="pinned-id")
        by_model = {e["gen_ai.response.model"]: e for e in entries}
        self.assertEqual(sorted(by_model), ["gpt-5.6-sol", "grok-build"])
        grok = by_model["grok-build"]
        self.assertEqual(grok["gen_ai.usage.input_tokens"], 15)
        self.assertEqual(grok["gen_ai.usage.output_tokens"], 5)
        self.assertEqual(grok["serotine.usage.cache_read_tokens"], 120)
        self.assertNotIn("serotine.usage.cache_write_tokens", grok)
        self.assertNotIn("serotine.usage.cost_usd", grok)  # OMP reported none
        sol = by_model["gpt-5.6-sol"]
        self.assertEqual(sol["gen_ai.provider.name"], "openai")
        self.assertEqual(sol["serotine.usage.cache_write_tokens"], 4)

    # --- token integrity across the multi-message sum (review FIX 2) -----
    #
    # Same principle as the cost rule below, applied to the counts: a total
    # is only a true statement about a run when every contributing message
    # reported usable counts. Each case here previously emitted a number that
    # LOOKED real — the worst failure mode for a drift-detection feed.

    def test_an_empty_usage_object_is_not_attributable(self):
        # `usage: {}` means "this message reported nothing", which is not
        # "this message used nothing" — it must not become an all-zero entry.
        raw = self._agent_end([self._msg("grok-build", 0, 0, usage={})])
        self.assertEqual(su.usage_from_omp_json(raw, request_model="pin"), [])

    def test_a_boolean_token_count_cannot_launder_into_a_number(self):
        # bool ⊂ int in Python, so summing True first would hand the shared
        # validator a plain int and defeat its bool check entirely.
        raw = self._agent_end([
            self._msg("grok-build", 0, 0, usage={"input": True, "output": 3,
                                                 "cacheRead": 0, "cacheWrite": 0}),
        ])
        self.assertEqual(su.usage_from_omp_json(raw, request_model="pin"), [])

    def test_a_negative_token_component_never_cancels_a_positive_one(self):
        raw = self._agent_end([
            self._msg("grok-build", -5, 3),
            self._msg("grok-build", 5, 3),
        ])
        self.assertEqual(su.usage_from_omp_json(raw, request_model="pin"), [])

    def test_a_missing_required_count_is_not_a_zero(self):
        raw = self._agent_end([
            self._msg("grok-build", 0, 0, usage={"input": 40}),  # no output
        ])
        self.assertEqual(su.usage_from_omp_json(raw, request_model="pin"), [])

    def test_a_poisoned_group_never_takes_a_healthy_sibling_with_it(self):
        # per-(provider, model) integrity: the model whose messages are all
        # sound still reports, the unattributable one simply does not.
        raw = self._agent_end([
            self._msg("grok-build", 10, 3),
            self._msg("gpt-5.6-sol", 0, 0, usage={"input": "?", "output": 1}),
        ])
        # …and the drop is NAMED on stderr, not silent — a gap nobody can see
        # is the failure this module warns about everywhere else.
        with self.assertWarns(UserWarning) as cm:
            entries = su.usage_from_omp_json(raw, request_model="pin")
        self.assertIn("gpt-5.6-sol", str(cm.warning))
        self.assertEqual([e["gen_ai.response.model"] for e in entries],
                         ["grok-build"])
        self.assertEqual(entries[0]["gen_ai.usage.input_tokens"], 10)

    def test_absent_cache_members_are_a_true_zero_not_a_poison(self):
        # the one absence that IS zero: "no cache read reported" and "no
        # cache read" are the same statement, and the module already omits
        # zero cache members from the wire.
        raw = self._agent_end([
            self._msg("grok-build", 0, 0, usage={"input": 40, "output": 9}),
        ])
        entries = su.usage_from_omp_json(raw, request_model="pin")
        self.assertEqual(entries[0]["gen_ai.usage.input_tokens"], 40)
        self.assertNotIn("serotine.usage.cache_read_tokens", entries[0])
        self.assertNotIn("serotine.usage.cache_write_tokens", entries[0])

    # --- cost integrity across the multi-message sum (review FIX 1) ------
    #
    # A summed cost is only a true statement about a run when EVERY
    # contributing message reported a usable one. Skipping an unusable
    # component while summing its siblings is arithmetically identical to
    # calling that component $0 — the exact "this run was free" fabrication
    # the omit-never-coerce rule exists to prevent, made worse by looking
    # complete. Every case below must therefore emit the TOKENS with NO cost
    # member at all.

    def test_a_negative_cost_component_never_cancels_a_positive_one(self):
        raw = self._agent_end([
            self._msg("grok-build", 10, 3, cost=-0.20),
            self._msg("grok-build", 10, 3, cost=0.30),
        ])
        entries = su.usage_from_omp_json(raw, request_model="pin")
        self.assertEqual(len(entries), 1)
        self.assertNotIn("serotine.usage.cost_usd", entries[0])
        self.assertEqual(entries[0]["gen_ai.usage.input_tokens"], 20)

    def test_an_unusable_cost_component_omits_the_whole_groups_cost(self):
        for bad in ("free", None, True, float("nan"), float("inf"), [0.3]):
            raw = self._agent_end([
                self._msg("grok-build", 10, 3, cost=0.30),
                self._msg("grok-build", 10, 3, cost=bad),
            ])
            entries = su.usage_from_omp_json(raw, request_model="pin")
            self.assertEqual(len(entries), 1, bad)
            self.assertNotIn("serotine.usage.cost_usd", entries[0], bad)

    def test_a_message_reporting_no_cost_at_all_omits_the_groups_cost(self):
        # "OMP said nothing about this message's cost" is not "$0" either.
        raw = self._agent_end([
            self._msg("grok-build", 10, 3, cost=0.30),
            self._msg("grok-build", 10, 3),  # no cost object at all
        ])
        entries = su.usage_from_omp_json(raw, request_model="pin")
        self.assertNotIn("serotine.usage.cost_usd", entries[0])

    def test_a_cost_is_emitted_only_when_every_component_reported_one(self):
        # the positive control for the three above
        raw = self._agent_end([
            self._msg("grok-build", 10, 3, cost=0.20),
            self._msg("grok-build", 10, 3, cost=0.30),
        ])
        entries = su.usage_from_omp_json(raw, request_model="pin")
        self.assertAlmostEqual(entries[0]["serotine.usage.cost_usd"], 0.50, places=6)

    # --- wire-shape guard: raw TUI output is REFUSED, never fabricated ---

    def test_raw_tui_text_is_refused_loudly(self):
        # SYNTHETIC specimen: the spike captured no TUI transcript, and the
        # detected signature is "not a JSON event stream at all", so any
        # non-JSON prose exercises the same rule. What is pinned is the
        # POSTURE — a wrapper that forgot `--mode json` gets an actionable
        # error, not a fabricated or silently-empty usage row.
        raw = ("  omp  $HOME/work\n"
               "> wrote wordstat.py and test_wordstat.py\n"
               "3 passed in 0.01s\n")
        with self.assertRaises(su.TuiOutputWireShapeError) as cm:
            su.usage_from_omp_json(raw, request_model="grok-build")
        msg = str(cm.exception)
        self.assertIn("SEROTINE_USAGE_INPUT", msg)
        self.assertIn("--mode json", msg)

    def test_raises_even_without_a_request_model(self):
        # A wiring bug must not be masked by an ALSO-missing pin quietly
        # producing the ordinary [] instead (codex's rule, same shape).
        with self.assertRaises(su.TuiOutputWireShapeError):
            su.usage_from_omp_json("plain tui text\n", request_model=None)

    def test_blank_input_fails_closed_quietly_not_loudly(self):
        # Absence is not a wiring bug: an empty capture is the ordinary
        # heartbeat-only case.
        for blank in ("", "   \n\n", None):
            self.assertEqual(su.usage_from_omp_json(blank, request_model="m"), [])

    # --- a blank-looking id is a blank id (review FIX 4) -----------------
    #
    # The schema's minLength:1 accepts "   ", so a whitespace-only id passes
    # both the documented required-pin gate and downstream validation while
    # carrying no information — defeating the pin invariant this lane exists
    # to enforce.

    def test_a_whitespace_only_pin_is_refused_like_a_missing_one(self):
        raw = _load_fixture("omp-mode-json-bucket-grok-build.jsonl")
        for pin in ("   ", "\t", "\n"):
            self.assertEqual(su.usage_from_omp_json(raw, request_model=pin), [], pin)

    def test_a_whitespace_only_served_model_is_not_attributable(self):
        raw = self._agent_end([self._msg("   ", 10, 3)])
        self.assertEqual(su.usage_from_omp_json(raw, request_model="pin"), [])

    def test_json_stream_without_agent_end_is_a_quiet_structural_failure(self):
        # Parseable JSON events but no terminal agent_end (a truncated /
        # killed run) is NOT the TUI signature — conservative, like codex's
        # mixed-stream rule.
        raw = "\n".join([
            json.dumps({"type": "agent_start"}),
            json.dumps({"type": "message_update", "delta": "hi"}),
        ]) + "\n"
        self.assertEqual(su.usage_from_omp_json(raw, request_model="m"), [])

    def test_error_is_a_value_error_subclass(self):
        # Matches the module's existing hard-error idiom, so any generic
        # `except ValueError` elsewhere still catches it.
        self.assertTrue(issubclass(su.TuiOutputWireShapeError, ValueError))


# --- (2f) Grok Build CLI `--output-format json` adapter ------------------

class GrokBuildAdapterTests(unittest.TestCase):
    """`usage_from_grok_build_json` over a REAL captured Grok Build CLI
    1.0.13 `--output-format json` result, committed verbatim at
    testdata/grok-build-output-format-json-run.json (byte-identical, sha256
    67e66be2…).

    EXPECTED VALUES ARE THE FIXTURE'S OWN LITERAL BYTES:
    "output is ONE JSON object with `usage{input_tokens 16,164 ·
    cache_read_input_tokens 64,000 · output_tokens 2,164 · reasoning_tokens
    1,265 · total_tokens 82,328}`, `total_cost_usd 0.0131` … and
    **`modelUsage` keyed by the SERVED id `grok-4.6-build`** (requested
    `grok-4.6`) with per-model tokens/calls/cost — a true request≠response
    echo, exactly what `gen_ai.response.model` wants and what the codex
    adapter never had."
    """

    def test_captured_specimen_shape_matches_the_memo(self):
        # Pins what the real capture looks like, so a future Grok-Build-CLI
        # wire change shows up here first rather than as a silent gap.
        obj = json.loads(_load_fixture("grok-build-output-format-json-run.json"))
        self.assertEqual(sorted(obj["modelUsage"]), ["grok-4.6-build"])
        per_model = obj["modelUsage"]["grok-4.6-build"]
        self.assertEqual(per_model["inputTokens"], 16164)
        self.assertEqual(per_model["outputTokens"], 2164)
        self.assertEqual(per_model["cacheReadInputTokens"], 64000)
        self.assertEqual(obj["usage"]["total_tokens"], 82328)
        self.assertEqual(obj["total_cost_usd"], 0.01314304)

    def test_captured_run_yields_one_entry_keyed_by_the_served_id(self):
        raw = _load_fixture("grok-build-output-format-json-run.json")
        entries = su.usage_from_grok_build_json(raw, request_model="grok-4.6")
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["gen_ai.request.model"], "grok-4.6")
        self.assertEqual(entry["gen_ai.response.model"], "grok-4.6-build")
        self.assertEqual(entry["gen_ai.provider.name"], "x_ai")
        self.assertEqual(entry["gen_ai.usage.input_tokens"], 16164)
        self.assertEqual(entry["gen_ai.usage.output_tokens"], 2164)
        self.assertEqual(entry["serotine.usage.cache_read_tokens"], 64000)
        self.assertAlmostEqual(entry["serotine.usage.cost_usd"], 0.0131, places=4)

    def test_multiple_served_ids_yield_one_entry_each(self):
        # SYNTHETIC (the captured run touched one model): modelUsage's KEYS
        # are the served ids, so a two-key map is two entries.
        raw = json.dumps({"modelUsage": {
            "grok-4.6-build": {"inputTokens": 10, "outputTokens": 3,
                               "cacheReadInputTokens": 100, "costUSD": 0.5},
            "grok-4.5-build": {"inputTokens": 7, "outputTokens": 1},
        }})
        entries = su.usage_from_grok_build_json(raw, request_model="grok-4.6")
        by_model = {e["gen_ai.response.model"]: e for e in entries}
        self.assertEqual(sorted(by_model), ["grok-4.5-build", "grok-4.6-build"])
        self.assertEqual(by_model["grok-4.6-build"]["serotine.usage.cost_usd"], 0.5)
        # a served id the CLI reported no cost for carries no cost claim
        self.assertNotIn("serotine.usage.cost_usd", by_model["grok-4.5-build"])
        # …and both still carry the caller's pin, unmodified
        self.assertEqual({e["gen_ai.request.model"] for e in entries}, {"grok-4.6"})

    # --- token integrity, per served id (review FIX 2) -------------------

    def test_a_missing_required_count_drops_that_model_not_a_zero(self):
        # …and the sibling that DID report complete counts still reports.
        raw = json.dumps({"modelUsage": {
            "grok-4.6-build": {"inputTokens": 10, "outputTokens": 3},
            "grok-4.5-build": {"inputTokens": 7},  # no outputTokens
        }})
        with self.assertWarns(UserWarning) as cm:
            entries = su.usage_from_grok_build_json(raw, request_model="grok-4.6")
        self.assertIn("grok-4.5-build", str(cm.warning))
        self.assertEqual([e["gen_ai.response.model"] for e in entries],
                         ["grok-4.6-build"])
        self.assertEqual(entries[0]["gen_ai.usage.output_tokens"], 3)

    def test_a_boolean_cache_count_cannot_launder_into_a_number(self):
        # _int(True) == 1: the cache members were coerced BEFORE the shared
        # validator could apply its bool check.
        raw = json.dumps({"modelUsage": {"grok-4.6-build": {
            "inputTokens": 10, "outputTokens": 3,
            "cacheReadInputTokens": True}}})
        self.assertEqual(su.usage_from_grok_build_json(raw, request_model="m"), [])

    def test_a_negative_count_drops_that_model(self):
        raw = json.dumps({"modelUsage": {"grok-4.6-build": {
            "inputTokens": -10, "outputTokens": 3}}})
        self.assertEqual(su.usage_from_grok_build_json(raw, request_model="m"), [])

    # --- wire-shape guard: raw TUI output is REFUSED, never fabricated ---

    def test_raw_tui_text_is_refused_loudly(self):
        # SYNTHETIC specimen — see the OMP class's identical note.
        raw = ("Grok Build CLI\n"
               "  Created wordstat.py\n"
               "  3 passed in 0.01s\n")
        with self.assertRaises(su.TuiOutputWireShapeError) as cm:
            su.usage_from_grok_build_json(raw, request_model="grok-4.6")
        msg = str(cm.exception)
        self.assertIn("SEROTINE_USAGE_INPUT", msg)
        self.assertIn("--output-format json", msg)

    def test_raises_even_without_a_request_model(self):
        with self.assertRaises(su.TuiOutputWireShapeError):
            su.usage_from_grok_build_json("plain tui text\n", request_model=None)

    def test_blank_input_fails_closed_quietly_not_loudly(self):
        for blank in ("", "   \n\n", None):
            self.assertEqual(
                su.usage_from_grok_build_json(blank, request_model="m"), [])

    # --- a blank-looking id is a blank id (review FIX 4) -----------------

    def test_a_whitespace_only_pin_is_refused_like_a_missing_one(self):
        raw = _load_fixture("grok-build-output-format-json-run.json")
        for pin in ("   ", "\t", "\n"):
            self.assertEqual(
                su.usage_from_grok_build_json(raw, request_model=pin), [], pin)

    def test_a_whitespace_only_served_id_is_not_attributable(self):
        raw = json.dumps({"modelUsage": {
            "   ": {"inputTokens": 10, "outputTokens": 3}}})
        self.assertEqual(su.usage_from_grok_build_json(raw, request_model="m"), [])

    # --- duplicate served ids are refused, not last-wins (review FIX 5) --

    def test_duplicate_model_usage_keys_are_refused_not_silently_last_wins(self):
        # json.loads keeps the LAST duplicate key silently, so this payload
        # would otherwise report 5 input tokens for a model the CLI also said
        # used 100 — a number that looks real and is not. RFC 8259 leaves
        # duplicate names undefined; an undefined payload is not a payload to
        # emit telemetry from.
        raw = ('{"modelUsage": {'
               '"grok-4.6-build": {"inputTokens": 100, "outputTokens": 40}, '
               '"grok-4.6-build": {"inputTokens": 5, "outputTokens": 1}}}')
        self.assertEqual(json.loads(raw)["modelUsage"]["grok-4.6-build"],
                         {"inputTokens": 5, "outputTokens": 1})  # stdlib's silent pick
        with self.assertWarns(UserWarning) as cm:
            entries = su.usage_from_grok_build_json(raw, request_model="grok-4.6")
        self.assertEqual(entries, [])
        self.assertIn("duplicate", str(cm.warning).lower())

    def test_a_duplicate_key_anywhere_in_the_result_object_is_refused(self):
        # not just inside modelUsage: a duplicate anywhere means the emitter
        # is not doing what this adapter assumes it does.
        raw = ('{"total_cost_usd": 0.1, "total_cost_usd": 0.2, '
               '"modelUsage": {"grok-4.6-build": '
               '{"inputTokens": 10, "outputTokens": 3}}}')
        with self.assertWarns(UserWarning):
            self.assertEqual(
                su.usage_from_grok_build_json(raw, request_model="m"), [])

    def test_valid_json_without_model_usage_is_a_quiet_structural_failure(self):
        # Well-formed JSON that simply carries no model-attributable usage is
        # NOT the TUI signature: the run-level `usage`/`total_cost_usd` are
        # deliberately not used to synthesize an entry with a guessed model.
        raw = json.dumps({"text": "ok", "stopReason": "end_turn",
                          "usage": {"input_tokens": 10, "output_tokens": 2},
                          "total_cost_usd": 0.01})
        self.assertEqual(su.usage_from_grok_build_json(raw, request_model="m"), [])
        # a JSON array is also well-formed JSON, just not this shape
        self.assertEqual(su.usage_from_grok_build_json("[1, 2]", request_model="m"), [])


# --- (3) fail-closed posture ---------------------------------------------

class FailClosedTests(unittest.TestCase):
    def test_codex_structural_parse_failure_returns_empty_not_zeros(self):
        self.assertEqual(
            su.usage_from_codex_exec_json("not json at all\nnope either",
                                          request_model="gpt-5.4"), [])

    def test_codex_no_token_count_event_returns_empty(self):
        raw = json.dumps({"type": "event_msg",
                          "payload": {"type": "turn_context", "model": "gpt-5.4"}}) + "\n"
        self.assertEqual(su.usage_from_codex_exec_json(raw, request_model="gpt-5.4"), [])

    def test_codex_empty_input_returns_empty(self):
        self.assertEqual(su.usage_from_codex_exec_json("", request_model="gpt-5.4"), [])
        self.assertEqual(su.usage_from_codex_exec_json(None, request_model="gpt-5.4"), [])

    def test_codex_missing_request_model_returns_empty(self):
        raw = codex_jsonl(model="gpt-5.4", input_tokens=1, output_tokens=1)
        self.assertEqual(su.usage_from_codex_exec_json(raw, request_model=None), [])
        self.assertEqual(su.usage_from_codex_exec_json(raw, request_model=""), [])

    def test_codex_malformed_token_value_coerced_not_dropped(self):
        # the STRUCTURE is present (a real token_count event) but one value
        # is garbage -- follows the existing _int() coercion posture (-> 0),
        # contrasted with a fully-missing structure (-> []).
        raw = codex_jsonl(model="gpt-5.4")
        raw = raw.replace('"input_tokens": 0', '"input_tokens": "oops"')
        entries = su.usage_from_codex_exec_json(raw, request_model="gpt-5.4")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["gen_ai.usage.input_tokens"], 0)

    def test_gemini_structural_parse_failure_returns_empty(self):
        self.assertEqual(su.usage_from_gemini_json({"no": "usageMetadata"},
                                                    request_model="gemini-2.5-pro"), [])
        self.assertEqual(su.usage_from_gemini_json([1, 2, 3],
                                                    request_model="gemini-2.5-pro"), [])
        self.assertEqual(su.usage_from_gemini_json("not json", request_model="x"), [])
        self.assertEqual(su.usage_from_gemini_json(None, request_model="x"), [])

    def test_gemini_missing_request_model_returns_empty(self):
        obj = gemini_response(model_version="gemini-2.5-pro",
                              prompt_tokens=1, candidates_tokens=1)
        self.assertEqual(su.usage_from_gemini_json(obj, request_model=None), [])

    def test_gemini_malformed_token_value_coerced_not_dropped(self):
        obj = gemini_response(model_version="gemini-2.5-pro", candidates_tokens=5)
        obj["usageMetadata"]["promptTokenCount"] = "oops"
        entries = su.usage_from_gemini_json(obj, request_model="gemini-2.5-pro")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["gen_ai.usage.input_tokens"], 0)

    def test_codex_response_model_absent_when_not_reported(self):
        # no turn_context event at all -> response.model omitted, not
        # fabricated, not copied from the request pin.
        raw = codex_jsonl(model=None, input_tokens=10, output_tokens=5)
        entries = su.usage_from_codex_exec_json(raw, request_model="gpt-5.4")
        self.assertEqual(len(entries), 1)
        self.assertNotIn("gen_ai.response.model", entries[0])
        self.assertEqual(entries[0]["gen_ai.request.model"], "gpt-5.4")

    def test_gemini_response_model_absent_when_not_reported(self):
        obj = gemini_response(model_version=None, prompt_tokens=10, candidates_tokens=5)
        entries = su.usage_from_gemini_json(obj, request_model="gemini-2.5-pro")
        self.assertEqual(len(entries), 1)
        self.assertNotIn("gen_ai.response.model", entries[0])

    def test_codex_cache_read_tokens_mapped(self):
        raw = codex_jsonl(model="gpt-5.4", input_tokens=100, output_tokens=20,
                          cached_input_tokens=40)
        entries = su.usage_from_codex_exec_json(raw, request_model="gpt-5.4")
        self.assertEqual(entries[0]["serotine.usage.cache_read_tokens"], 40)

    def test_gemini_cache_read_tokens_mapped(self):
        obj = gemini_response(model_version="gemini-2.5-pro", prompt_tokens=100,
                              candidates_tokens=20, cached_tokens=30)
        entries = su.usage_from_gemini_json(obj, request_model="gemini-2.5-pro")
        self.assertEqual(entries[0]["serotine.usage.cache_read_tokens"], 30)

    def test_codex_accepts_pre_parsed_list_of_dicts(self):
        # programmatic/test convenience: a caller that already parsed each
        # JSONL line doesn't have to re-serialize it to text.
        lines = [
            {"type": "event_msg", "payload": {"type": "turn_context", "model": "gpt-5.4"}},
            {"type": "event_msg", "payload": {"type": "token_count",
             "info": {"total_token_usage": {"input_tokens": 7, "output_tokens": 3}}}},
        ]
        entries = su.usage_from_codex_exec_json(lines, request_model="gpt-5.4")
        self.assertEqual(entries[0]["gen_ai.usage.input_tokens"], 7)

    def test_codex_last_token_count_wins_cumulative(self):
        # two token_count events (streaming-style cumulative updates) -> the
        # LAST one's totals win, same "keep last" idiom as usage_from_window.
        lines = [
            json.dumps({"type": "event_msg", "payload": {"type": "token_count",
                       "info": {"total_token_usage": {"input_tokens": 10, "output_tokens": 2}}}}),
            json.dumps({"type": "event_msg", "payload": {"type": "token_count",
                       "info": {"total_token_usage": {"input_tokens": 50, "output_tokens": 9}}}}),
        ]
        entries = su.usage_from_codex_exec_json("\n".join(lines), request_model="gpt-5.4")
        self.assertEqual(entries[0]["gen_ai.usage.input_tokens"], 50)
        self.assertEqual(entries[0]["gen_ai.usage.output_tokens"], 9)


# --- (4) the shared validate/copy gate -----------------------------------

class ValidateUsageEntryTests(unittest.TestCase):
    def test_unknown_key_dropped_with_warning(self):
        candidate = {
            "gen_ai.provider.name": "openai",
            "gen_ai.request.model": "gpt-5.4",
            "gen_ai.usage.input_tokens": 1,
            "gen_ai.usage.output_tokens": 2,
            "totally.unexpected.key": "should not survive",
        }
        with self.assertWarns(UserWarning):
            entry = su._validate_usage_entry(candidate)
        self.assertNotIn("totally.unexpected.key", entry)

    def test_missing_required_key_drops_whole_entry(self):
        candidate = {
            "gen_ai.provider.name": "openai",
            "gen_ai.usage.input_tokens": 1,
            "gen_ai.usage.output_tokens": 2,
            # gen_ai.request.model missing
        }
        self.assertIsNone(su._validate_usage_entry(candidate))

    def test_non_dict_candidate_rejected(self):
        self.assertIsNone(su._validate_usage_entry(None))
        self.assertIsNone(su._validate_usage_entry("not a dict"))
        self.assertIsNone(su._validate_usage_entry([1, 2, 3]))

    def test_wrong_typed_string_field_omitted_not_coerced(self):
        candidate = {
            "gen_ai.provider.name": "openai",
            "gen_ai.request.model": "gpt-5.4",
            "gen_ai.response.model": 12345,  # wrong type -- must not survive
            "gen_ai.usage.input_tokens": 1,
            "gen_ai.usage.output_tokens": 2,
        }
        entry = su._validate_usage_entry(candidate)
        self.assertNotIn("gen_ai.response.model", entry)

    def test_int_field_coerced_via_int_helper(self):
        candidate = {
            "gen_ai.provider.name": "openai",
            "gen_ai.request.model": "gpt-5.4",
            "gen_ai.usage.input_tokens": "42",
            "gen_ai.usage.output_tokens": None,
        }
        entry = su._validate_usage_entry(candidate)
        self.assertEqual(entry["gen_ai.usage.input_tokens"], su._int("42"))
        self.assertEqual(entry["gen_ai.usage.output_tokens"], 0)

    # --- schema minimum + bool-as-int -----------------------------------

    def test_negative_token_count_rejects_whole_entry(self):
        # the schema pins minimum: 0 on every token count; a negative value
        # could never validate downstream, so the ENTRY is rejected (None) —
        # a partially-scrubbed count would be a fabrication.
        candidate = {
            "gen_ai.provider.name": "openai",
            "gen_ai.request.model": "gpt-5.4",
            "gen_ai.usage.input_tokens": -1,
            "gen_ai.usage.output_tokens": 2,
        }
        self.assertIsNone(su._validate_usage_entry(candidate))
        # a negative OPTIONAL field rejects too — same gate semantics
        candidate2 = {
            "gen_ai.provider.name": "openai",
            "gen_ai.request.model": "gpt-5.4",
            "gen_ai.usage.input_tokens": 1,
            "gen_ai.usage.output_tokens": 2,
            "serotine.usage.cache_read_tokens": -7,
        }
        self.assertIsNone(su._validate_usage_entry(candidate2))
        # negative-as-string goes through _int() coercion first, then the
        # same minimum-0 bound
        candidate3 = dict(candidate, **{"gen_ai.usage.input_tokens": "-5"})
        self.assertIsNone(su._validate_usage_entry(candidate3))

    def test_bool_is_a_type_violation_not_an_int(self):
        # bool ⊂ int in Python (int(True) == 1) — a True/False token count is
        # a type violation checked BEFORE the int coercion. Required field →
        # omitted → required-key check rejects the entry.
        candidate = {
            "gen_ai.provider.name": "openai",
            "gen_ai.request.model": "gpt-5.4",
            "gen_ai.usage.input_tokens": True,
            "gen_ai.usage.output_tokens": 2,
        }
        self.assertIsNone(su._validate_usage_entry(candidate))
        # optional field → just omitted, entry survives without it
        candidate2 = {
            "gen_ai.provider.name": "openai",
            "gen_ai.request.model": "gpt-5.4",
            "gen_ai.usage.input_tokens": 1,
            "gen_ai.usage.output_tokens": 2,
            "serotine.usage.cache_read_tokens": False,
        }
        entry = su._validate_usage_entry(candidate2)
        self.assertIsNotNone(entry)
        self.assertNotIn("serotine.usage.cache_read_tokens", entry)

    # --- serotine.usage.cost_usd: the schema's one NUMBER-typed field ----
    #
    # serotine-run-v1.schema.json pins it {"type": "number", "minimum": 0,
    # "description": "Advisory only. Ingest recomputes canonically from
    # Serotine's pinned price table and stores both."} — the gate must let a
    # provider-reported notional cost through as a number, with the same
    # minimum-0 and bool-is-not-a-number bounds the integer fields get.

    def test_whitespace_only_string_is_blank_not_a_value(self):
        # the schema's minLength:1 accepts "   ", so the gate is what has to
        # refuse it; a required field then rejects the entry, an optional one
        # is simply omitted.
        candidate = {
            "gen_ai.provider.name": "x_ai",
            "gen_ai.request.model": "   ",
            "gen_ai.usage.input_tokens": 1,
            "gen_ai.usage.output_tokens": 2,
        }
        self.assertIsNone(su._validate_usage_entry(candidate))
        entry = su._validate_usage_entry(
            dict(candidate, **{"gen_ai.request.model": "grok-4.6",
                               "gen_ai.response.model": "  "}))
        self.assertIsNotNone(entry)
        self.assertNotIn("gen_ai.response.model", entry)

    def test_cost_usd_number_passes_through(self):
        candidate = {
            "gen_ai.provider.name": "x_ai",
            "gen_ai.request.model": "grok-4.6",
            "gen_ai.usage.input_tokens": 1,
            "gen_ai.usage.output_tokens": 2,
            "serotine.usage.cost_usd": 0.01314304,
        }
        entry = su._validate_usage_entry(candidate)
        self.assertEqual(entry["serotine.usage.cost_usd"], 0.01314304)

    def test_cost_usd_integer_json_number_accepted_as_a_number(self):
        candidate = {
            "gen_ai.provider.name": "x_ai",
            "gen_ai.request.model": "grok-4.6",
            "gen_ai.usage.input_tokens": 1,
            "gen_ai.usage.output_tokens": 2,
            "serotine.usage.cost_usd": 0,
        }
        entry = su._validate_usage_entry(candidate)
        self.assertEqual(entry["serotine.usage.cost_usd"], 0)

    def test_negative_cost_usd_rejects_whole_entry(self):
        candidate = {
            "gen_ai.provider.name": "x_ai",
            "gen_ai.request.model": "grok-4.6",
            "gen_ai.usage.input_tokens": 1,
            "gen_ai.usage.output_tokens": 2,
            "serotine.usage.cost_usd": -0.5,
        }
        self.assertIsNone(su._validate_usage_entry(candidate))

    def test_non_numeric_cost_usd_omitted_never_coerced_to_zero(self):
        # contrast with the int fields, where a malformed value _int()s to 0:
        # a 0-dollar cost is an affirmative (and wrong) CLAIM, so a
        # non-numeric cost is dropped instead of being coerced into one.
        for bad in ("$0.02", None, True, [0.02], float("nan"), float("inf")):
            candidate = {
                "gen_ai.provider.name": "x_ai",
                "gen_ai.request.model": "grok-4.6",
                "gen_ai.usage.input_tokens": 1,
                "gen_ai.usage.output_tokens": 2,
                "serotine.usage.cost_usd": bad,
            }
            entry = su._validate_usage_entry(candidate)
            self.assertIsNotNone(entry, bad)
            self.assertNotIn("serotine.usage.cost_usd", entry, bad)


# --- (5) resolve_usage(): SEROTINE_USAGE_PROVIDER opt-in -----------------

class ResolveUsageProviderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _input_file(self, content):
        path = os.path.join(self._tmp.name, "provider-input.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_codex_provider_end_to_end(self):
        raw = codex_jsonl(model="gpt-5.4", input_tokens=4120, output_tokens=880)
        path = self._input_file(raw)
        with scrubbed_env(SEROTINE_USAGE_PROVIDER="codex",
                          SEROTINE_USAGE_INPUT=path,
                          SEROTINE_USAGE_REQUEST_MODEL="gpt-5.4"):
            entries = su.resolve_usage()
        self.assertEqual(entries, [{
            "gen_ai.provider.name": "openai",
            "gen_ai.request.model": "gpt-5.4",
            "gen_ai.response.model": "gpt-5.4",
            "gen_ai.usage.input_tokens": 4120,
            "gen_ai.usage.output_tokens": 880,
        }])

    def test_gemini_provider_end_to_end(self):
        obj = gemini_response(model_version="gemini-2.5-pro",
                              prompt_tokens=6300, candidates_tokens=1450)
        path = self._input_file(json.dumps(obj))
        with scrubbed_env(SEROTINE_USAGE_PROVIDER="GEMINI",  # case-insensitive
                          SEROTINE_USAGE_INPUT=path,
                          SEROTINE_USAGE_REQUEST_MODEL="gemini-2.5-pro"):
            entries = su.resolve_usage()
        self.assertEqual(entries[0]["gen_ai.provider.name"], "google")

    def test_registered_selectors_are_the_documented_vocabulary(self):
        # The SEROTINE_USAGE_PROVIDER vocabulary is an INTERFACE (lib.sh
        # documents it, wrappers set it, an unrecognized value is a hard
        # error) — pin it, including the hyphen in `grok-build`.
        self.assertEqual(sorted(su._PROVIDER_ADAPTERS),
                         ["codex", "gemini", "grok-build", "omp"])

    def test_omp_provider_end_to_end(self):
        # Resolve the captured OMP stream from disk through the provider adapter.
        path = self._input_file(
            _load_fixture("omp-mode-json-bucket-grok-build.jsonl"))
        with scrubbed_env(SEROTINE_USAGE_PROVIDER="omp",
                          SEROTINE_USAGE_INPUT=path,
                          SEROTINE_USAGE_REQUEST_MODEL="grok-build"):
            entries = su.resolve_usage()
        self.assertEqual(len(entries), 1)
        entry = dict(entries[0])
        cost = entry.pop("serotine.usage.cost_usd")
        self.assertAlmostEqual(cost, 0.0511, places=4)  # advisory USD, four decimals
        self.assertEqual(entry, {
            "gen_ai.provider.name": "x_ai",
            "gen_ai.request.model": "grok-build",
            "gen_ai.response.model": "grok-build",
            "gen_ai.usage.input_tokens": 17833,
            "gen_ai.usage.output_tokens": 2498,
            "serotine.usage.cache_read_tokens": 112640,
        })

    def test_grok_build_provider_end_to_end(self):
        path = self._input_file(
            _load_fixture("grok-build-output-format-json-run.json"))
        with scrubbed_env(SEROTINE_USAGE_PROVIDER="Grok-Build",  # case-insensitive
                          SEROTINE_USAGE_INPUT=path,
                          SEROTINE_USAGE_REQUEST_MODEL="grok-4.6"):
            entries = su.resolve_usage()
        self.assertEqual(entries, [{
            "gen_ai.provider.name": "x_ai",
            "gen_ai.request.model": "grok-4.6",
            "gen_ai.response.model": "grok-4.6-build",
            "gen_ai.usage.input_tokens": 16164,
            "gen_ai.usage.output_tokens": 2164,
            "serotine.usage.cache_read_tokens": 64000,
            "serotine.usage.cost_usd": 0.01314304,
        }])

    def test_tui_input_raises_through_resolve_usage_for_both_new_providers(self):
        # A wrapper that captured the TUI transcript instead of the JSON hits
        # the loud path through the real wiring surface, not just the adapter.
        path = self._input_file("just some tui prose\nno json here\n")
        for provider in ("omp", "grok-build"):
            with scrubbed_env(SEROTINE_USAGE_PROVIDER=provider,
                              SEROTINE_USAGE_INPUT=path,
                              SEROTINE_USAGE_REQUEST_MODEL="pinned"):
                with self.assertRaises(su.TuiOutputWireShapeError, msg=provider):
                    su.resolve_usage()

    def test_provider_wins_over_explicit_claude_json(self):
        # SEROTINE_USAGE_PROVIDER must own resolution even if the Claude-shape
        # explicit vars are ALSO set (a misconfiguration a wrapper should
        # never produce, but the precedence must still be deterministic).
        raw = codex_jsonl(model="gpt-5.4", input_tokens=1, output_tokens=1)
        path = self._input_file(raw)
        claude_json = json.dumps({"usage": {"input_tokens": 999, "output_tokens": 999},
                                  "model": "claude-sonnet-5"})
        with scrubbed_env(SEROTINE_USAGE_PROVIDER="codex",
                          SEROTINE_USAGE_INPUT=path,
                          SEROTINE_USAGE_REQUEST_MODEL="gpt-5.4",
                          SEROTINE_USAGE_JSON=claude_json):
            entries = su.resolve_usage()
        self.assertEqual(entries[0]["gen_ai.provider.name"], "openai")
        self.assertNotEqual(entries[0]["gen_ai.usage.input_tokens"], 999)

    def test_unset_provider_is_byte_identical_to_prior_behavior(self):
        # no SEROTINE_USAGE_PROVIDER at all -> falls through to the existing
        # explicit-JSON path exactly as before this change.
        claude_json = json.dumps({"usage": {"input_tokens": 5, "output_tokens": 6},
                                  "model": "claude-sonnet-5"})
        with scrubbed_env(SEROTINE_USAGE_JSON=claude_json):
            entries = su.resolve_usage()
        self.assertEqual(entries, su.usage_from_claude_json(json.loads(claude_json)))

    def test_unrecognized_provider_raises_hard_error(self):
        with scrubbed_env(SEROTINE_USAGE_PROVIDER="bogus-provider"):
            with self.assertRaises(ValueError):
                su.resolve_usage()

    def test_codex_stdout_input_raises_through_resolve_usage(self):
        # End to end through the real wiring surface (SEROTINE_USAGE_PROVIDER
        # + SEROTINE_USAGE_INPUT -> resolve_usage()), not just the adapter
        # function directly — a wrapper that miswires SEROTINE_USAGE_INPUT at
        # a `codex exec --json` stdout capture instead of the session
        # rollout file hits this same loud path.
        raw = _load_fixture("codex-exec-stdout-turn-completed.jsonl")
        path = self._input_file(raw)
        with scrubbed_env(SEROTINE_USAGE_PROVIDER="codex",
                          SEROTINE_USAGE_INPUT=path,
                          SEROTINE_USAGE_REQUEST_MODEL="gpt-5.4"):
            with self.assertRaises(su.CodexStdoutWireShapeError):
                su.resolve_usage()

    def test_unrecognized_provider_never_falls_back_to_window_scan(self):
        # a fake .claude-shaped projects dir that WOULD have usage if the
        # (buggy) fallback ran -- proves the raise happens before any window
        # scan, not that the scan merely returns empty by coincidence.
        proj = os.path.join(self._tmp.name, "proj", "-slug")
        os.makedirs(proj, exist_ok=True)
        with open(os.path.join(proj, "s.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "assistant", "timestamp": "2026-07-08T12:00:30-04:00",
                "requestId": "r1",
                "message": {"id": "m1", "model": "claude-sonnet-5",
                           "usage": {"input_tokens": 1, "output_tokens": 1}}}) + "\n")
        with scrubbed_env(SEROTINE_USAGE_PROVIDER="bogus-provider",
                          SEROTINE_USAGE_PROJECTS_DIR=os.path.join(self._tmp.name, "proj")):
            with self.assertRaises(ValueError):
                su.resolve_usage()

    def test_missing_input_file_fails_closed_not_hard_error(self):
        # a KNOWN provider with a bad/missing SEROTINE_USAGE_INPUT is a data
        # problem, not a config typo -- fails closed to [], no raise.
        with scrubbed_env(SEROTINE_USAGE_PROVIDER="codex",
                          SEROTINE_USAGE_INPUT=os.path.join(self._tmp.name, "nope.json"),
                          SEROTINE_USAGE_REQUEST_MODEL="gpt-5.4"):
            self.assertEqual(su.resolve_usage(), [])

    def test_skip_status_short_circuits_provider_path_too(self):
        raw = codex_jsonl(model="gpt-5.4", input_tokens=1, output_tokens=1)
        path = self._input_file(raw)
        with scrubbed_env(SEROTINE_LINE_STATUS="skip",
                          SEROTINE_USAGE_PROVIDER="codex",
                          SEROTINE_USAGE_INPUT=path,
                          SEROTINE_USAGE_REQUEST_MODEL="gpt-5.4"):
            self.assertEqual(su.resolve_usage(), [])

    def test_usage_disable_short_circuits_provider_path_too(self):
        raw = codex_jsonl(model="gpt-5.4", input_tokens=1, output_tokens=1)
        path = self._input_file(raw)
        with scrubbed_env(SEROTINE_USAGE_DISABLE="1",
                          SEROTINE_USAGE_PROVIDER="codex",
                          SEROTINE_USAGE_INPUT=path,
                          SEROTINE_USAGE_REQUEST_MODEL="gpt-5.4"):
            self.assertEqual(su.resolve_usage(), [])


# --- (5b) the EMITTED LINE: serotine.run.v1 conformance ------------------

class EmittedLineConformanceTests(unittest.TestCase):
    """`build_line()` is the seam the wrapper actually crosses, and the line
    it returns is what gets spooled. The wire is additionalProperties:false
    and ingest rejection is WHOLE-BATCH ATOMIC, so a member this module emits
    that the schema does not accept would wedge every other line in the same
    POST. This validates the assembled line for both new providers against
    the COMMITTED schema using the repo's own vendored validator — the check
    that `serotine.usage.cost_usd` really is on the wire and not an invented
    key (it is: the schema pins it {"type": "number", "minimum": 0}).
    """

    _VALIDATOR = _load("validate_serotine_run",
                       os.path.join(_HERE, "telemetry", "validate_serotine_run.py"))

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(_HERE, "telemetry", "serotine-run-v1.schema.json"),
                  encoding="utf-8") as f:
            cls.schema = json.load(f)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _line(self, provider, fixture, pin):
        path = os.path.join(self._tmp.name, "provider-input")
        with open(path, "w", encoding="utf-8") as f:
            f.write(_load_fixture(fixture))
        with scrubbed_env(
                SEROTINE_LINE_RUN_ID="com.example.builder-seat/2026-09-02_150000-ab12",
                SEROTINE_LINE_JOB_LABEL="com.example.builder-seat",
                SEROTINE_LINE_HOST="host-a",
                SEROTINE_LINE_START="2026-09-02T15:00:00-04:00",
                SEROTINE_LINE_END="2026-09-02T15:04:00-04:00",
                SEROTINE_LINE_EXIT_CODE="0",
                SEROTINE_USAGE_PROVIDER=provider,
                SEROTINE_USAGE_INPUT=path,
                SEROTINE_USAGE_REQUEST_MODEL=pin):
            return su.build_line()

    def test_omp_line_validates_against_the_committed_schema(self):
        line = self._line("omp", "omp-mode-json-bucket-grok-build.jsonl",
                          "grok-build")
        self._VALIDATOR.validate(line, self.schema)
        self.assertEqual(line["gen_ai.operation.name"], "invoke_agent")
        self.assertEqual(line["serotine.usage"][0]["gen_ai.response.model"],
                         "grok-build")
        # and it survives the JSON encode the emitter actually performs
        json.loads(json.dumps(line))

    def test_grok_build_line_validates_against_the_committed_schema(self):
        line = self._line("grok-build", "grok-build-output-format-json-run.json",
                          "grok-4.6")
        self._VALIDATOR.validate(line, self.schema)
        self.assertEqual(line["serotine.usage"][0]["gen_ai.response.model"],
                         "grok-4.6-build")
        self.assertEqual(line["serotine.usage"][0]["serotine.usage.cost_usd"],
                         0.01314304)
        json.loads(json.dumps(line))


# --- (6) existing-adapter regression (light smoke check) ------------------

class ExistingAdapterUntouchedTests(unittest.TestCase):
    """usage_from_claude_json gained only an optional cost member; this is
    a light smoke check that its token behavior is unaffected. The authoritative
    regression coverage is test-lib.sh section (l) and this repo's other
    pre-existing tests, unchanged by this PR."""

    def test_model_usage_map_shape_unaffected(self):
        obj = {"modelUsage": {"claude-sonnet-5": {
            "inputTokens": 182304, "outputTokens": 9120,
            "cacheReadInputTokens": 120400, "cacheCreationInputTokens": 8100}}}
        entries = su.usage_from_claude_json(obj)
        self.assertEqual(entries, [{
            "gen_ai.provider.name": "anthropic",
            "gen_ai.request.model": "claude-sonnet-5",
            "gen_ai.response.model": "claude-sonnet-5",
            "gen_ai.usage.input_tokens": 182304,
            "gen_ai.usage.output_tokens": 9120,
            "serotine.usage.cache_read_tokens": 120400,
            "serotine.usage.cache_write_tokens": 8100,
        }])

    def test_modelless_entry_still_dropped(self):
        self.assertEqual(su.usage_from_claude_json({"usage": {"input_tokens": 1}}), [])


class ClaudeUsageCostTests(unittest.TestCase):
    """usage_from_claude_json carries a cost only where the file states one for
    exactly that entry; token members are unchanged by it."""

    COST = "serotine.usage.cost_usd"

    def _model_usage(self, **models):
        return {name: {"inputTokens": 10, "outputTokens": 2, **extra}
                for name, extra in models.items()}

    def test_model_cost_usd_is_carried(self):
        entries = su.usage_from_claude_json({"modelUsage": self._model_usage(
            **{"claude-sonnet-5": {"costUSD": 0.0123}})})
        self.assertEqual(entries[0][self.COST], 0.0123)
        self.assertEqual(entries[0]["gen_ai.usage.input_tokens"], 10)
        self.assertEqual(entries[0]["gen_ai.usage.output_tokens"], 2)

    def test_uncosted_model_carries_no_cost_even_with_costusd(self):
        entries = su.usage_from_claude_json({
            "modelUsage": self._model_usage(**{"claude-sonnet-5": {"costUSD": 0.5}}),
            "uncosted_models": ["claude-sonnet-5"]})
        self.assertNotIn(self.COST, entries[0])

    def test_unusable_cost_usd_is_absence(self):
        for bad in (True, False, "0.5", -0.01, float("nan"), float("inf"),
                    float("-inf"), None, [1]):
            with self.subTest(cost=bad):
                entries = su.usage_from_claude_json({"modelUsage": self._model_usage(
                    **{"claude-sonnet-5": {"costUSD": bad}})})
                self.assertNotIn(self.COST, entries[0])

    def test_zero_cost_is_a_reported_amount(self):
        entries = su.usage_from_claude_json({"modelUsage": self._model_usage(
            **{"claude-sonnet-5": {"costUSD": 0}})})
        self.assertEqual(entries[0][self.COST], 0)

    def test_only_the_priced_model_carries_a_cost(self):
        entries = su.usage_from_claude_json({
            "modelUsage": self._model_usage(**{
                "claude-sonnet-5": {"costUSD": 0.25},
                "claude-haiku-5": {}}),
            "total_cost_usd": 0.25})
        by_model = {e["gen_ai.request.model"]: e for e in entries}
        self.assertEqual(by_model["claude-sonnet-5"][self.COST], 0.25)
        self.assertNotIn(self.COST, by_model["claude-haiku-5"])

    def test_bare_usage_and_model_carries_total_cost(self):
        entries = su.usage_from_claude_json({
            "model": "claude-sonnet-5", "total_cost_usd": 0.07,
            "usage": {"input_tokens": 5, "output_tokens": 1}})
        self.assertEqual(entries[0][self.COST], 0.07)
        self.assertEqual(entries[0]["gen_ai.usage.input_tokens"], 5)

    def test_bare_path_unusable_total_is_absence(self):
        for bad in (True, "1", -1, float("nan"), float("inf")):
            with self.subTest(total=bad):
                entries = su.usage_from_claude_json({
                    "model": "claude-sonnet-5", "total_cost_usd": bad,
                    "usage": {"input_tokens": 5, "output_tokens": 1}})
                self.assertNotIn(self.COST, entries[0])

    def test_single_model_total_is_dropped_when_any_model_is_uncosted(self):
        for listed in ("claude-sonnet-5", "claude-haiku-5"):
            with self.subTest(listed=listed):
                entries = su.usage_from_claude_json({
                    "model": "claude-sonnet-5", "total_cost_usd": 0.5,
                    "uncosted_models": [listed],
                    "usage": {"input_tokens": 5, "output_tokens": 1}})
                self.assertNotIn(self.COST, entries[0])
                self.assertEqual(entries[0]["gen_ai.usage.input_tokens"], 5)

    def test_model_usage_path_never_takes_total_cost(self):
        entries = su.usage_from_claude_json({
            "modelUsage": self._model_usage(**{"claude-sonnet-5": {}}),
            "total_cost_usd": 0.9})
        self.assertNotIn(self.COST, entries[0])


class ResolveFailureTests(unittest.TestCase):
    """`resolve_failure` — the last member filter before the spool.

    This module never reads a log and never redacts (that is serotine_trace.py,
    where the redactor lives). Its whole job is the member filter, and the
    reason it is strict is that ingest rejection is WHOLE-BATCH ATOMIC: one
    malformed member would wedge every other line in the same POST.
    """

    NINE = ["serotine.schema", "serotine.run.id", "serotine.job.label",
            "host.name", "serotine.run.start", "serotine.run.end",
            "serotine.run.exit_code", "serotine.run.status",
            "serotine.run.trigger"]

    def test_unset_is_absence(self):
        with scrubbed_env():
            self.assertEqual(su.resolve_failure(), {})

    def test_empty_and_whitespace_are_absence(self):
        for raw in ("", "   ", "\n"):
            with scrubbed_env(SEROTINE_LINE_FAILURE_JSON=raw):
                self.assertEqual(su.resolve_failure(), {})

    def test_all_three_members_pass_through_verbatim(self):
        payload = {"message": "m", "last_log_line": "l", "stderr_tail": "a\nb"}
        with scrubbed_env(SEROTINE_LINE_FAILURE_JSON=json.dumps(payload)):
            self.assertEqual(su.resolve_failure(), payload)

    def test_an_unknown_member_refuses_the_WHOLE_object(self):
        """No partial pass-through: the good-looking sibling goes too."""
        with scrubbed_env(SEROTINE_LINE_FAILURE_JSON=json.dumps(
                {"message": "looks fine", "stdout_tail": "not a member"})):
            self.assertEqual(su.resolve_failure(), {})

    def test_a_blank_member_refuses_the_WHOLE_object(self):
        with scrubbed_env(SEROTINE_LINE_FAILURE_JSON=json.dumps(
                {"message": "looks fine", "last_log_line": "   "})):
            self.assertEqual(su.resolve_failure(), {})

    def test_a_nonstring_member_refuses_the_WHOLE_object(self):
        with scrubbed_env(SEROTINE_LINE_FAILURE_JSON=json.dumps(
                {"message": "looks fine", "stderr_tail": 7})):
            self.assertEqual(su.resolve_failure(), {})
        with scrubbed_env(SEROTINE_LINE_FAILURE_JSON=json.dumps(
                {"message": "looks fine", "last_log_line": ["a"]})):
            self.assertEqual(su.resolve_failure(), {})

    def test_an_oversized_member_refuses_the_WHOLE_object(self):
        """The builder guarantees these ceilings; exceeding one means the
        response did not come from the builder."""
        for key, cap in su._FAILURE_MEMBER_MAX_CHARS.items():
            with scrubbed_env(SEROTINE_LINE_FAILURE_JSON=json.dumps(
                    {"message": "looks fine", key: "x" * (cap + 1)})):
                self.assertEqual(su.resolve_failure(), {}, key)
            # Exactly at the cap is what the builder emits — accepted.
            with scrubbed_env(SEROTINE_LINE_FAILURE_JSON=json.dumps({key: "x" * cap})):
                self.assertEqual(su.resolve_failure(), {key: "x" * cap})

    def test_a_nul_bearing_member_refuses_the_WHOLE_object(self):
        with scrubbed_env(SEROTINE_LINE_FAILURE_JSON=json.dumps(
                {"message": "looks fine", "stderr_tail": "a\x00b"})):
            self.assertEqual(su.resolve_failure(), {})

    def test_the_member_ceilings_match_the_builders_own_bounds(self):
        """One number, two modules, version-locked in this repo. A bound that
        drifted on one side only would either truncate silently or reject every
        line the builder produces."""
        trace = _load("serotine_trace",
                      os.path.join(_HERE, "telemetry", "serotine_trace.py"))
        self.assertEqual(su._FAILURE_MEMBER_MAX_CHARS, {
            "message": trace.FAILURE_MESSAGE_CHARS,
            "last_log_line": trace.FAILURE_LAST_LOG_LINE_CHARS,
            "stderr_tail": trace.FAILURE_STDERR_TAIL_CHARS,
        })

    def test_a_real_builder_response_survives_the_gate(self):
        """The two modules must actually agree end to end — the strict gate is
        worthless if it rejects what the shipped builder produces."""
        trace = _load("serotine_trace",
                      os.path.join(_HERE, "telemetry", "serotine_trace.py"))
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
            fh.write("x" * 6000 + "\n" + "boom: it broke\n")
            path = fh.name
        built = trace.build_failure(path, message="m" * 900)
        self.assertEqual(set(built), {"message", "last_log_line", "stderr_tail"})
        with scrubbed_env(SEROTINE_LINE_FAILURE_JSON=json.dumps(built)):
            self.assertEqual(su.resolve_failure(), built)

    def test_unparseable_or_non_object_input_is_absence_never_a_raise(self):
        for raw in ("{not json", "[1, 2]", '"a string"', "null", "7"):
            with scrubbed_env(SEROTINE_LINE_FAILURE_JSON=raw):
                self.assertEqual(su.resolve_failure(), {})

    def test_build_line_omits_the_key_when_the_var_is_unset(self):
        """THE OFF PATH: the emitted line is the nine keys it has always been."""
        with scrubbed_env(SEROTINE_USAGE_DISABLE="1"):
            self.assertEqual(list(su.build_line().keys()), self.NINE)

    def test_build_line_omits_the_key_when_the_object_is_refused(self):
        with scrubbed_env(SEROTINE_USAGE_DISABLE="1",
                          SEROTINE_LINE_FAILURE_JSON='{"stdout_tail": "x"}'):
            self.assertEqual(list(su.build_line().keys()), self.NINE)

    def test_build_line_appends_the_key_last_when_armed(self):
        with scrubbed_env(SEROTINE_USAGE_DISABLE="1",
                          SEROTINE_LINE_FAILURE_JSON='{"message": "why"}'):
            line = su.build_line()
            self.assertEqual(list(line.keys()), self.NINE + ["serotine.run.failure"])
            self.assertEqual(line["serotine.run.failure"], {"message": "why"})


# --- (8) the fallback-chain's served-id case ----------------------------

class ChainRequestModelTests(unittest.TestCase):
    """A 2-link fallback chain (launchd-kit lib.sh's lk_run_chain)
    — claude(claude-sonnet-5-5) -> codex(gpt-5.6-sol) — where the run
    line's gen_ai.response.model must reflect whichever link actually
    SERVED the run, never a hardcoded "always link 1" default. The chain
    mechanics themselves (advance-on-hard-failure only) live in lib.sh and
    are proven by launchd-kit/test-lib.sh's (aa) chain matrix. The
    wrapper's SEROTINE_USAGE_* wiring is exercised through the same real
    surface (resolve_usage()) a fallback job would use, per the contract in
    serotine_usage.py's module docstring (`SEROTINE_USAGE_REQUEST_MODEL`
    is the caller's OWN pin for the link that served — never link 1's)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _rollout_file(self, model, input_tokens=11, output_tokens=7):
        raw = codex_jsonl(model=model, input_tokens=input_tokens,
                          output_tokens=output_tokens)
        path = os.path.join(self._tmp.name, "codex-rollout.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw)
        return path

    def test_fallen_back_run_records_link_2s_served_id_not_link_1s_pin(self):
        # The chain's link 1 is claude-sonnet-5-5; the wrapper's own pin for
        # link 2 (gen_ai.request.model, never read from the provider) is
        # gpt-5.6-sol. A wrapper bug
        # that always reported link 1's default regardless of which link
        # served would emit "claude-sonnet-5-5" here — this pins that it does
        # not.
        rollout = self._rollout_file(model="gpt-5.6-sol")
        with scrubbed_env(SEROTINE_USAGE_PROVIDER="codex",
                          SEROTINE_USAGE_INPUT=rollout,
                          SEROTINE_USAGE_REQUEST_MODEL="gpt-5.6-sol"):
            entries = su.resolve_usage()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["gen_ai.request.model"], "gpt-5.6-sol")
        self.assertEqual(entries[0]["gen_ai.response.model"], "gpt-5.6-sol")
        self.assertNotEqual(entries[0]["gen_ai.response.model"], "claude-sonnet-5-5")

    def test_fallen_back_run_surfaces_a_dated_codex_alias_verbatim(self):
        # The served id need not even match the wrapper's own pin (a dated
        # alias the account resolved to) — the adapter reports it verbatim
        # rather than laundering it back to the pin, exactly the
        # request!=response case usage_from_claude_json cannot express.
        rollout = self._rollout_file(model="gpt-5.6-sol-2026-08-20")
        with scrubbed_env(SEROTINE_USAGE_PROVIDER="codex",
                          SEROTINE_USAGE_INPUT=rollout,
                          SEROTINE_USAGE_REQUEST_MODEL="gpt-5.6-sol"):
            entries = su.resolve_usage()
        self.assertEqual(entries[0]["gen_ai.request.model"], "gpt-5.6-sol")
        self.assertEqual(entries[0]["gen_ai.response.model"], "gpt-5.6-sol-2026-08-20")

    def test_link_1_served_takes_the_unset_provider_window_path_unchanged(self):
        # When claude (link 1) serves — no fallback — a chain-aware wrapper
        # must NOT set SEROTINE_USAGE_PROVIDER at all: byte-identical to
        # today's single-engine path (BRIEF_CHAIN's own "unset = identical"
        # contract extends to the served-provider selector, not only to
        # whether the chain runs). SEROTINE_USAGE_PROJECTS_DIR is pointed at
        # an empty dir so the window scan can't pick up this MACHINE's real
        # session transcripts and make the assertion below
        # environment-dependent.
        with scrubbed_env(SEROTINE_USAGE_PROJECTS_DIR=self._tmp.name):
            entries = su.resolve_usage()
        # No claude session-window fixture is wired here (that path is
        # covered by usage_from_window's own suite) — the point pinned is
        # that resolve_usage() takes the non-provider branch at all, i.e.
        # _resolve_provider_usage is never invoked, so there is nothing
        # codex-shaped to find; a codex rollout accidentally left on
        # SEROTINE_USAGE_INPUT (unset here) could never leak in.
        self.assertEqual(entries, [])

    def test_provider_selector_matches_the_served_link_not_a_fixed_default(self):
        # A wrapper that hardcodes SEROTINE_USAGE_PROVIDER="codex" regardless
        # of outcome would wrongly attribute a claude-served (link 1) run to
        # codex. The selector must be set FROM the chain's own served-link
        # name (LK_CHAIN_SERVED_NAME in lib.sh) — simulated here by driving
        # resolve_usage() with each of the two real selectors run-brief.sh's
        # case statement can produce, and confirming each stays in its own
        # provider lane rather than bleeding into the other's.
        rollout = self._rollout_file(model="gpt-5.6-sol")
        with scrubbed_env(SEROTINE_USAGE_PROVIDER="codex",
                          SEROTINE_USAGE_INPUT=rollout,
                          SEROTINE_USAGE_REQUEST_MODEL="gpt-5.6-sol"):
            codex_entries = su.resolve_usage()
        # the "claude served, provider left unset" lane — isolated from this
        # machine's real session transcripts, same reasoning as the test above
        with scrubbed_env(SEROTINE_USAGE_PROJECTS_DIR=self._tmp.name):
            claude_entries = su.resolve_usage()
        self.assertEqual(codex_entries[0]["gen_ai.provider.name"], "openai")
        self.assertEqual(claude_entries, [])  # no window fixture wired; see above



_ROOT = os.path.dirname(_HERE)
_TELEMETRY = os.path.join(_HERE, "telemetry")

VALIDATORS = {
    "run": os.path.join(_TELEMETRY, "validate_serotine_run.py"),
}

GROUPS = [
    ("run", os.path.join(_TELEMETRY, "fixtures", "valid")),
    ("run", os.path.join(_TELEMETRY, "fixtures", "invalid")),
]

EXPECTED = json.loads(r'''{
  "launchd-kit/telemetry/fixtures/invalid/bad-run-id-pattern.jsonl": {
    "head": "$.serotine.run.id: [pattern]",
    "ok": false
  },
  "launchd-kit/telemetry/fixtures/invalid/bad-status-enum.jsonl": {
    "head": "$.serotine.run.status: [enum]",
    "ok": false
  },
  "launchd-kit/telemetry/fixtures/invalid/bad-timestamp.jsonl": {
    "head": "$.serotine.run.start: [pattern]",
    "ok": false
  },
  "launchd-kit/telemetry/fixtures/invalid/bad-token-type.jsonl": {
    "head": "$.serotine.usage[0].gen_ai.usage.input_tokens: [type]",
    "ok": false
  },
  "launchd-kit/telemetry/fixtures/invalid/bad-trigger-enum.jsonl": {
    "head": "$.serotine.run.trigger: [enum]",
    "ok": false
  },
  "launchd-kit/telemetry/fixtures/invalid/missing-required-status.jsonl": {
    "head": "$: [required]",
    "ok": false
  },
  "launchd-kit/telemetry/fixtures/invalid/not-one-line.jsonl": {
    "head": "$: [jsonl]",
    "ok": false
  },
  "launchd-kit/telemetry/fixtures/invalid/run-failure-blank-member.jsonl": {
    "head": "$.serotine.run.failure.message: [pattern]",
    "ok": false
  },
  "launchd-kit/telemetry/fixtures/invalid/run-failure-nonstring-member.jsonl": {
    "head": "$.serotine.run.failure.message: [type]",
    "ok": false
  },
  "launchd-kit/telemetry/fixtures/invalid/run-failure-unknown-member.jsonl": {
    "head": "$.serotine.run.failure: [additionalProperties]",
    "ok": false
  },
  "launchd-kit/telemetry/fixtures/invalid/unknown-top-level-key.jsonl": {
    "head": "$: [additionalProperties]",
    "ok": false
  },
  "launchd-kit/telemetry/fixtures/invalid/usage-empty-array.jsonl": {
    "head": "$.serotine.usage: [minItems]",
    "ok": false
  },
  "launchd-kit/telemetry/fixtures/invalid/usage-missing-input-tokens.jsonl": {
    "head": "$.serotine.usage[0]: [required]",
    "ok": false
  },
  "launchd-kit/telemetry/fixtures/invalid/usage-nonverbatim-genai-key.jsonl": {
    "head": "$.serotine.usage[0]: [additionalProperties]",
    "ok": false
  },
  "launchd-kit/telemetry/fixtures/invalid/wrong-schema-const.jsonl": {
    "head": "$.serotine.schema: [const]",
    "ok": false
  },
  "launchd-kit/telemetry/fixtures/valid/crash-run-with-forensics.jsonl": {
    "head": "",
    "ok": true
  },
  "launchd-kit/telemetry/fixtures/valid/crash-run.jsonl": {
    "head": "",
    "ok": true
  },
  "launchd-kit/telemetry/fixtures/valid/heartbeat-only-run.jsonl": {
    "head": "",
    "ok": true
  },
  "launchd-kit/telemetry/fixtures/valid/llm-run.jsonl": {
    "head": "",
    "ok": true
  },
  "launchd-kit/telemetry/fixtures/valid/skip-run.jsonl": {
    "head": "",
    "ok": true
  }
}''')


def _head(err: str) -> str:
    if not err:
        return ""
    m = re.match(r"^(.*?): \[([^\]]+)\]", err)
    if not m:
        return err
    return f"{m.group(1)}: [{m.group(2)}]"


def _first_error(stderr: str) -> str:
    for line in stderr.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("INVALID "):
            rest = line.split(": ", 1)
            if len(rest) == 2:
                return rest[1]
        return line
    return ""


def _check(kind: str, fixture: str) -> tuple[bool, str]:
    proc = subprocess.run(
        ["python3", VALIDATORS[kind], fixture],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    ok = proc.returncode == 0
    return ok, _head(_first_error(proc.stderr))


class FixtureVerdictPinTests(unittest.TestCase):
    def test_every_telemetry_fixture_keeps_its_verdict_head(self):
        seen = []
        for kind, folder in GROUPS:
            self.assertTrue(
                os.path.isdir(folder),
                f"missing fixture folder {os.path.relpath(folder, _ROOT)}")
            for name in sorted(os.listdir(folder)):
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(folder, name)
                rel = os.path.relpath(path, _ROOT)
                seen.append(rel)
                self.assertIn(rel, EXPECTED, f"unpinned fixture {rel}")
                ok, head = _check(kind, path)
                exp = EXPECTED[rel]
                self.assertEqual(ok, exp["ok"], f"{rel} verdict changed")
                self.assertEqual(head, exp["head"], f"{rel} first-error head changed")
        self.assertEqual(sorted(seen), sorted(EXPECTED), "fixture set drifted")


class CodexStdoutWireShapeMessagePinTests(unittest.TestCase):
    """Pins the exact operator-facing text of the codex stdout wire-shape
    error. The source spells its rollout-file path as two adjacent
    literals; a wrong split (a lost dot or slash) must go red here. The
    expected text builds that path from fragments split elsewhere, so this
    file carries no home-folder path of its own."""

    def test_codex_stdout_wire_shape_error_text_is_exact(self):
        rollout_glob = "(~" + "/" + ".codex/sessions/**/*.jsonl)"
        expected = (
            "SEROTINE_USAGE_INPUT looks like raw `codex exec --json` STDOUT "
            "(a turn.completed event carrying a usage object, with no served "
            "model id anywhere in the stream) rather than the codex session "
            "ROLLOUT file. Stdout never carries a provider model id, so this "
            "adapter refuses to emit a model-less or fabricated usage row from "
            "it. Fix: point SEROTINE_USAGE_INPUT at the session rollout file "
            + rollout_glob
            + " instead of the exec --json stdout "
            "capture -- see foundry standards/model-routing.md \u00a7 Fail-behavior "
            "when the id is missing.")
        message = str(su._codex_stdout_wire_shape_error())
        self.assertIn(".codex/sessions/**/*.jsonl", message)
        self.assertEqual(message, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
