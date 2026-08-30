# Architecture

Technical reference for how synapse-engine actually works internally — the
audience is a developer (human or AI) about to change code here, not someone
setting it up. For setup/deployment, see [`README.md`](../README.md).

## The dispatch pipeline

Every input source — the email listener, the Telegram listener, and the
reminder scheduler — funnels through the same two-step call:

```python
prompt = sync_and_build_prompt(IncomingMessage(...))
result = pipe_to_provider(prompt, session_id=..., extra_env=...)
```

`sync_and_build_prompt()` (`core/pipe.py`) runs a pre-flight `git pull
--rebase` against the vault, then wraps the raw message in a standardized
YAML metadata block (`Type`, `Sender`, `Context`, `Current Time`) that
includes the git sync result. It's named for both things it does — the git
status text is embedded in the returned prompt, not a side channel you can
ignore. `pipe_to_provider()` resolves
the configured provider via `providers.get_provider()`, invokes its
`generate_response()`, and normalizes the result.

**This envelope is not optional.** A prompt built as a bare string instead of
`sync_and_build_prompt(IncomingMessage(...))` arrives at the AI provider looking like an
unattributed instruction injected mid-conversation — and gets treated as a
prompt-injection attempt, including by the provider's own safety behavior.
This has happened in production (an early version of the retry-after-auth
feature built its retry prompt as a raw string). Any code that needs to feed
text back into the pipeline — retries, nudges, internally-generated
follow-ups — must go through `sync_and_build_prompt`, never around it.

### Providers

`providers/` implements a small `AIProvider` ABC (`base.py`) with one concrete
class per backend: `ClaudeProvider`, `GeminiProvider` (deprecated), `AgyProvider`,
and `EchoProvider` (a no-op stub for tests/dev). `providers/__init__.py`'s
`get_provider()` is a factory that constructs a fresh provider instance per
call, reading the active provider name from `config.get_ai_provider()`.

**Rule: valid provider names live in exactly one place —
`providers/__init__.py`'s `PROVIDER_REGISTRY` dict** (`{name: class}`).
`get_provider()` and both channels' `/provider` command handlers all check
membership against it. Never hardcode a tuple/list of provider names at a
new call site — a hardcoded tuple in one listener but not the other is
exactly how `/provider agy` worked on Telegram but not email.

Each real provider shells out to its CLI binary as a subprocess, passing the
prompt on stdin/argv and parsing JSON from stdout. All three currently
duplicate their own subprocess-env setup, locking, and error handling rather
than sharing an implementation in `base.py` — see "Known rough edges" below.

### `GLOBAL_PROVIDER_LOCK`

The vault (`VAULT_PATH`) is a shared mutable git repo: the AI provider's own
shell tool can run arbitrary `git` commands against it, and the ingestion
service independently runs `git pull`/`commit`/`push` against the same
directory from other threads (`pipe.py`'s pre-flight sync, `scheduler.py`'s
`reminders.json` auto-commit). `GLOBAL_PROVIDER_LOCK` (`providers/base.py`) is
a process-wide `threading.Lock` that serializes all of this.

**Rule: any code running as a thread inside the ingestion service that
touches `git` against `VAULT_PATH` or `REMINDERS_JSON_PATH` must hold this
lock first** (`pipe.py`'s pre-flight sync and `scheduler.py`'s
`reminders.json` auto-commit both do). Skipping it risks corrupting
`FETCH_HEAD` via racing concurrent `git pull`s. There is no single
chokepoint enforcing this — each in-process call site is individually
responsible for acquiring it.

**Exception: `tools/reminder_cli.py`'s own git sync doesn't acquire the lock
itself, and that's fine for its normal usage.** It's invoked by the AI's
shell tool from *within* an active provider session, and `with
GLOBAL_PROVIDER_LOCK:` wraps the *entire* `subprocess.run()` call for that
session in all three providers (`claude.py`/`gemini.py`/`agy.py`) — blocking
until the CLI process exits, including everything it shells out to
internally. So the ingestion-service thread that spawned that session
already holds the lock for reminder_cli.py's whole invocation. The gap is
narrower than "unprotected": `reminder_cli.py` run genuinely standalone — a
human at a terminal, no provider session in flight — has no
ingestion-service thread involved at all, so nothing prevents that
invocation from racing the service's own git operations.

## State and lifetime

Three long-running components share one process (wired up in `main.py`):
`EmailListener`, `TelegramListener`, and `ReminderScheduler`, each running as
a daemon thread.

**Rule: `RateLimiter` and `SessionManager` are each constructed exactly
once, in `main.py`, and passed as constructor params into all three
components.** Never construct either one anywhere else — accept it as a
parameter from whatever constructed you.

This matters most for `SessionManager`, which holds a purely **in-memory**
field — `_stats_prefs`, the per-user `/stats on|off` preference set via
`set_stats_enabled()`. A component holding its own separate instance would
never see another component's `/stats` toggle (session-ID data itself is
fine across instances, since it's read from and written to one shared JSON
file — it's specifically this in-memory field that breaks). `RateLimiter`
matters for the analogous reason: its own docstring says it's "shared across
all ingestion channels," which is only true if one instance is actually
shared.

### `UserSession`: the per-identity handle

Message-handling code doesn't call `session_manager.get_session(key)` /
`.save_session(key, id)` / `.get_stats_enabled(key)` directly with a raw key
string repeated at each call. Instead it constructs one
`UserSession(session_manager, key, stats_key=None)` (`core/session_manager.py`)
near the top of the function and passes that single handle around:

```python
session = UserSession(session_manager, key)   # stats_key defaults to key
session_id = session.session_id
session.save(result.session_id)
if session.stats_enabled:
    ...
```

`UserSession` is a thin wrapper — every method just calls the same method on
the `session_manager` it was given, so it composes with a mocked
`session_manager` in tests exactly like the manager itself would.
**Construct it directly (`UserSession(session_manager, key)`), not via a
factory method on `session_manager`** — a factory method called on a mocked
`session_manager` would return an unrelated mock, disconnected from
whatever the test configured.

`stats_key` only needs to be passed when it differs from `key` — the one
place this happens is the email listener, where session continuity is
per-thread (`key`) but the `/stats` preference is per-sender (`stats_key`),
since one person can have many threads. Everywhere else (Telegram, the
scheduler, the E*TRADE retry path) the two are the same identity, so
`stats_key` is omitted.

`get_message_session()`/`save_message_session()` (keyed by Telegram message
ID, a different identity than the user) and one-off writes to a
locally-constructed key (e.g. the scheduler's synthesized email-thread ID
for a reminder reply) still go straight through `session_manager` — they
aren't identities a `UserSession` handle is built for.

## Delivery: getting a reply back out

Each channel has one function meant to be the single place outbound
formatting happens:

- **Email** — `channels/email/reply.py`'s `send_reply(..., stats=None,
  session=None)`. Formats form tables and task-checkbox `mailto:` links
  internally.
- **Telegram** — `channels/telegram/reply_dispatch.py`'s `safe_reply_text()`
  (parse-mode-fallback retry when Telegram rejects malformed HTML/Markdown),
  `safe_edit_text()` (the same fallback for callback-query flows that edit
  an existing message instead of sending a new one, e.g. the quota-retry
  button), and `build_reply_keyboard()` (truncation + Actionable-Form/
  task-checklist detection + keyboard construction) for inline replies.
  `channels/telegram/sender.py`'s `send_telegram_message(..., stats=None,
  session=None)` for standalone sends.

**Rule: pass the raw `stats` dict plus a `session` handle through
`send_reply()`/`send_telegram_message()` — never pre-gate or pre-format
`stats` yourself.** Both call `utils/stats_formatter.py`'s
`append_stats_email`/`append_stats_telegram` internally, which gate on
`session.stats_enabled` (when a `session` is given — omitted, they use
`stats` as-is) before formatting and appending. This is the **one**
formatting+gating implementation for both channels — every call site uses
it, including the two below that can't call `send_reply`/
`send_telegram_message` directly:

**`channels/telegram/listener.py`** (3 call sites) and
**`core/scheduler.py`**'s inline-reply path build `reply_text`/
`response_text` themselves rather than calling `send_telegram_message` for
their primary replies, because that text also feeds
`build_reply_keyboard()`'s truncation and Actionable-Form detection, which
must run *after* the stats footer is appended (send_telegram_message's own
truncation runs too late for that ordering). They call
`append_stats_telegram`/`append_stats_email` directly instead:

```python
reply_text = append_stats_telegram(reply_text, result.stats, session)
```

Same function, called one layer up the stack — not a reimplementation.
Never reimplement the gate-and-format logic itself at a new call site;
always go through `append_stats_email`/`append_stats_telegram` (either
directly, or via `send_reply`/`send_telegram_message`).

## The E*TRADE PIN-auth fallback and retry mechanism

E*TRADE's automated login is reliably blocked by bot detection. When it
fails, `tools/etrade_cli.py` (shared by `options_bot_cli.py` too) falls back
to a manual OAuth PIN flow: it sends a Telegram/email prompt with a login
URL, a human completes login in their own browser and replies with the
verification code, and whichever listener receives that reply completes the
token exchange.

What makes this more than a one-off auth prompt: once the human completes the
PIN, the **original request that triggered the fallback** gets retried
automatically, and the result is delivered back to wherever that original
request came from — even if the PIN reply arrived on a different channel.

This works by threading a small set of `SYNAPSE_*` environment variables from
whichever component made the original dispatch call, down through
`pipe_to_provider`'s `extra_env` parameter, into the E*TRADE CLI subprocess's
environment:

| Var | Set by | Meaning |
|---|---|---|
| `SYNAPSE_SESSION_KEY` | Telegram/email listener | The session to resume on retry (interactive case) |
| `SYNAPSE_SESSION_ID` | Telegram/email listener | The active session id, if one exists yet |
| `SYNAPSE_CHANNEL` | listener or scheduler | `"telegram"` or `"email"` — where to deliver the retry result |
| `SYNAPSE_CHAT_ID` | Telegram listener | The chat whose request is being retried |
| `SYNAPSE_EMAIL_TO` / `_SUBJECT` / `_MESSAGE_ID` / `_REFERENCES` | Email listener or scheduler | Threading headers for the retry reply |
| `SYNAPSE_REMINDER_TASK` | Scheduler | The literal reminder text to replay fresh (scheduled case — never has a session to resume) |

`etrade_cli.py`'s `_fallback_to_pin_auth` reads whichever of these are
present and merges them onto a pending-request record
(`~/.etrade_pending_auth.json`, 30-minute TTL). **Presence of
`SYNAPSE_SESSION_KEY` or `SYNAPSE_REMINDER_TASK` is itself the signal that
this fallback was triggered by a task failure** (as opposed to a manual
`/update-etrade-auth` run, which sets none of these and is correctly excluded
from any retry). Once the human completes the PIN,
`tools/stocks/etrade_pin_auth.py`'s `complete_and_maybe_retry(pending,
session_manager)` either resumes the exact failed session (interactive case)
or replays the reminder task fresh via the same call shape
`scheduler.py`'s `_handle_work_reminder` uses (scheduled case), then delivers
the result back via the stored channel/target.

If you're adding a new dispatch site that could hit an E*TRADE auth wall
(or extending this mechanism to another tool), thread the same `SYNAPSE_*`
vars through your `pipe_to_provider(..., extra_env=...)` call the way the
existing listeners and scheduler do — there's no shared builder for this
today, each producer constructs its own `extra_env` dict.

## Two CLI conventions in `tools/`

New tool CLIs (`etrade_cli.py`, `options_bot_cli.py`, `amazon_fresh_cli.py`,
`reminder_cli.py`) follow one convention: JSON output via shared `_out(data)`/
`_err(message, code)` helpers with a documented error-code taxonomy in the
module docstring, `sys.path.insert(0, ...)` bootstrapping at the top, and path
resolution exclusively through `services.ingestion.config` constants.

Older CLIs (`gmail_cli.py`, `calendar_cli.py`) predate this and use plain
human-readable text output with bare `print(..., file=sys.stderr); exit(1)`,
and recompute their own default paths instead of importing `config.py`'s
`VAULT_PATH`/`CALENDAR_*_PATH` constants.

**Follow the JSON/`_err`/`_out` convention for anything new** — it's the
majority pattern and the more machine-parseable one for the AI provider that
actually calls these tools.

## SmartThings

`tools/smartthings_cli.py` is a thin argparse CLI over the
`tools/smartthings/` package (`auth.py`, `client.py`, `resolver.py`),
following the JSON/`_err`/`_out` convention above. `auth.py` implements the
OAuth2 authorization-code flow: `smartthings auth` runs a one-time
interactive browser flow to capture the first access/refresh token pair,
and every later call goes through `get_valid_access_token()`, which
transparently refreshes and re-persists the token when it's near expiry —
the rotated `refresh_token` must be saved immediately, since SmartThings
invalidates the old one as soon as a new one is issued. `client.py` wraps
the Devices REST API with 429 backoff-and-retry-once; `resolver.py`
resolves a fuzzy device name to a device id via a short-TTL local cache so
repeated resolutions don't burn API calls.

There is no MCP wrapper for this integration. Unlike `tools/calendar_mcp.py`
(unused legacy — see `CLAUDE.md`), MCP was deliberately never built here;
every SmartThings operation goes through the CLI.

## Tests

Tests live under `services/ingestion/tests/<module>/`, mirroring the source
tree one level up (`tests/channels/`, `tests/core/`, `tests/tools/`,
`tests/utils/`). `services/ingestion/providers/tests/` is a known exception —
provider tests live inside `providers/` itself instead of under the mirrored
`tests/` tree. `pytest.ini`'s `testpaths=services` + `python_files=test_*.py`
collects both locations regardless, but new tests should go under
`services/ingestion/tests/<module>/` — don't add a third convention.

```bash
python -m pytest services/ -v
```

There is no CI configured in this repo — running the suite before claiming
something works is on you.

## Known rough edges

Things worth knowing about before you touch the surrounding code, even though
none of them are on a roadmap to fix immediately:

- **Provider subprocess plumbing is triplicated.** `claude.py`, `gemini.py`,
  and `agy.py` each hand-roll their own env setup, `GLOBAL_PROVIDER_LOCK`
  acquisition, and subprocess error handling instead of sharing an
  implementation in `base.py`. Adding a parameter to the provider interface
  (e.g. `extra_env`) means editing all three by hand.
- **Quota/transient-error classification disagrees across four places** —
  `channels/telegram/listener.py`, `core/scheduler.py`,
  `providers/claude.py`, and `providers/gemini.py` each maintain their own
  keyword list for "is this error worth a fallback retry," and
  `scheduler.py`'s is narrower than the others — background/scheduled work
  gets weaker automatic-fallback coverage than interactive chat for the
  identical underlying failure.
- **`gmail_cli.py`/`calendar_cli.py`/`setup_google.py` each redefine their own
  OAuth `get_credentials()`** instead of sharing one implementation, despite
  `gmail_cli.py`'s own docstring saying the intent was one shared auth.
- **`RateLimiter`'s constructor has a `rate_limiter or RateLimiter(...)`
  fallback** in both listeners — harmless today because `main.py` always
  supplies the shared instance, but a silent-second-instance trap if a future
  caller ever omits it.

This list will drift as fixes land — treat it as a pointer to what's worth
being careful around, not an exhaustive or current inventory.
