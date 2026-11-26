# Trace conformance fixtures

`serotine-trace-sample.jsonl` is a **verbatim copy of serotine's own**
`test/fixtures/trace-sample.jsonl` (the body `test/trace-store.test.ts` feeds to
`storeTrace`). It is vendored here as the **known-good control** for the
wrapper-side trace payload: `test-serotine-trace.py` runs the same server-contract
checker over serotine's fixture and over the body this repo produces, so the
checker cannot quietly drift into "whatever our emitter happens to emit".

Refresh it by re-copying from serotine if that fixture ever changes; do not edit
it here.
