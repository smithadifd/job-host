# Webhook notifier contract

How scheduled jobs post to a chat webhook. [`discord-notify.sh`](./discord-notify.sh) is the implementation; this file is the contract it and its callers honor. The script is the single notifier for shell and launchd jobs — never hand-roll another one. Callers in other runtimes (a Python automation, a web service) keep their own small notifier and follow the same contract rather than sharing a library.

Reference points:

- **Shell and launchd jobs** — `launchd-kit/discord-notify.sh` is the canonical, version-controlled copy. A machine-local mirror may be what wrappers actually call, and it can lag the checkout. A caller that depends on a newer flag should verify the resolved script supports it: a stale copy silently swallows an unknown flag as a positional argument.
- **Interpreter contract** — leaf programs are regular tracked `100644` files. Run this shell notifier with `bash <path>`, including from Python argv lists (`["bash", str(path), ...]`). `NOTIFY_SCRIPT` names one readable regular shell script, never a command string. No executable bit is needed; libraries are still sourced and Python modules imported normally.
- **Escalation** — pinging a human about a blocking ask is the caller's concern, not the notifier's. The notifier only delivers what it is given; a caller that needs a human now builds its own message and passes it with `--content` (see § Mentions).

## When to use what

| Context | Use |
|---|---|
| launchd / cron / any shell automation | `bash launchd-kit/discord-notify.sh` |
| A Python automation (no web framework) | A short module modeled on this contract; import the HTTP client lazily |
| A running app (web service) | A repo-local notifier module following this contract |

## The contract

### Webhook resolution

The script resolves the webhook URL in this order, first hit wins:

1. `DISCORD_WEBHOOK_URL`, if already exported.
2. The channel's `webhook` in `notify.json`, the one fixed configuration file (the channel `--channel <name>` names, else the one `NOTIFY_CHANNEL` names, else `default`). See the [configuration location](README.md#env-var-contract). A file that breaks the schema, or a missing channel, exits `2`.
3. Only while `notify.json` is absent, for one release: `DISCORD_WEBHOOK_URL` read as data from the env file named by `WEBHOOK_ENV_FILE`, else the script's default env file under the caller's secrets directory, with a deprecation line on standard error. The file is never sourced.

If none resolves, it prints `discord-notify: no DISCORD_WEBHOOK_URL configured` and exits non-zero. It never sends to a hard-coded fallback and never silently no-ops. Implementations in other runtimes follow the same order: an explicit argument first, then the environment, then a secrets file, then (services only) a per-user override stored by the app.

A value that starts with `op://` is a 1Password reference and is resolved before use; a literal URL behaves exactly as before. A resolution failure prints a distinct `discord-notify: 1Password resolution failed` message and exits non-zero, so a vault outage is distinguishable from a webhook or HTTP failure.

For `op://` values, `OP_CONNECT_HOST` is required and passed unchanged to the
Connect resolver. If it is unset or empty, the script prints exactly
`discord-notify: OP_CONNECT_HOST is required for op:// webhooks` on stderr and
exits `1`, without invoking the Connect resolver or legacy `op read`. With a
non-empty host, Connect is tried first; only a Connect failure or unavailable
resolver permits the legacy `op read` fallback. Plaintext webhook URLs do not
require `OP_CONNECT_HOST` and follow the same delivery path as before.

### Secrets hygiene

- The webhook URL is a secret. Never log it, echo it back in a response, or write it to a state or data file. Where state must be keyed on a webhook (for example a cooldown map), key on its `sha256`.
- Secrets come from the environment or a secret store only. No notifier path may leak the URL into digests, feeds or run summaries. Dry-run output redacts the webhook and any token.

### Colors

Brand-palette decimals with consistent meanings across every caller:

| Meaning | Decimal | Hex |
|---|---|---|
| Success / all-clear / green | `5763719` | `0x57F287` |
| Warning / attention | `16705372` | `0xFEE75C` |
| Failure / error / red | `15548997` | `0xED4245` |
| Neutral / informational (default) | `5793266` | `0x5865F2` (blurple) |

A findings-style job (a health report, an audit) is **quiet when healthy, loud when flagged**: post green only on a real all-clear, and red or yellow when something needs eyes.

### Payload shape

Send an **embed**, not raw content, for anything structured: `{ embeds: [{ title, description, color, fields?, footer?, timestamp? }] }`.

- The title is capped at 256 characters by the service; an over-long title is rejected with a 400, not truncated. Keep it short and put detail in the body.
- **Truncate** the description to stay under the 4096-character limit. The script caps the body at 3900 and appends a `…(truncated — see local log for full output)` marker. Point at the local log for the full output rather than spilling.
- Field values are limited to 1024 characters, and an embed carries at most 25 fields.

### Mentions — an embed can never ping anyone

The service parses `<@…>` mentions only in the top-level `content` field. A mention placed in an embed title, description or field value renders as inert text and notifies nobody. Anything whose job is to get a human's attention must send `content`, not just an embed; the script exposes this as `bash discord-notify.sh --content "<text>"`, and the embed still rides along for the structured detail. With `--content` and no body the payload is content-only.

- **Cap `content` at 1900 characters** (the hard limit is 2000). Truncate, never reject: a shortened ping still reaches its human, a rejected one silently does not. The truncation marker counts inside the cap, and truncation is from the tail, so a mention placed at the start can never be cut into a broken `<@…` fragment.
- **Pin `allowed_mentions` to `{"parse": ["users"]}`** on any payload carrying `content`. Without it, interpolated text containing `@everyone` or `@here` would ping the whole channel. It is set on the `content` path only, so embed-only payloads stay byte-identical. `--no-mentions` pins it to an empty parse list for content that only informs.

#### The mention target is a numeric id, supplied by configuration

`<@…>` resolves a user id — a numeric snowflake such as `123456789012345678` — not a username. `<@some-username>` renders as literal text and pings nobody.

- Never hardcode a handle. The id is a configuration value (an environment variable or a secret-store field), not a literal in source.
- A user id is not a secret: anyone who can read a message can see it. Keep it as a plain config value.
- Degrade cleanly when it is unset or malformed: send the message without a mention rather than emitting `<@>`, `<@username>` or a half-built fragment. Warn on stderr, and never echo the offending value back, since someone may have pasted a secret into the variable.

### Delivery robustness

- **Every request has a timeout.** The script uses `--connect-timeout 10 --max-time 20`, and bounds the 1Password read separately. Implementations in other runtimes set explicit connect and read timeouts rather than relying on a client default, which is often none.
- **A non-2xx response fails the call.** The script prints `discord-notify: HTTP <code>`, echoes the response body to stderr, and exits non-zero. It does not retry. An implementation that retries on HTTP 429 must honor the service's `retry_after`, cap the wait, and fail fast on a hard throttle instead of blocking a scheduled task; after a hard throttle it should cool down, persisting the cooldown across restarts and keying it by the webhook's hash, so a penalized webhook is not re-poked every tick.
- **A notify failure never breaks the job.** Notification is a side effect: the caller logs the error and carries on, and the primary work still succeeds. Swallowing happens at the caller boundary, not inside the notifier — the script exits non-zero and the caller decides. A caller whose message is itself the deliverable can instead surface that exit code; escalation is the caller's concern.

## Checklist for a new notifier

- [ ] Webhook resolved by the order above; loud failure if unset.
- [ ] URL never logged, echoed or persisted in plaintext.
- [ ] Shared color decimals for success, warning, failure and neutral.
- [ ] Embed payload; description truncated under the limit with a log pointer.
- [ ] Request timeout set.
- [ ] Non-2xx handled; any 429 retry honors `retry_after`, capped, with fail-fast and cooldown on a hard throttle.
- [ ] Notify errors are logged and never fail the caller.
- [ ] If it must ping a human: the mention is in top-level `content`, not an embed.
- [ ] `content` capped at 1900 and truncated (not rejected); `allowed_mentions` pinned to `users`.
- [ ] The mention target is a configured numeric id, and an unset or malformed one degrades to no mention.
- [ ] For a shell or launchd job: you used `discord-notify.sh` and did not hand-roll one.
