# Architecture

Technical reference for how synapse-engine actually works internally — the
audience is a developer (human or AI) about to change code here, not someone
setting it up. For setup/deployment, see [`README.md`](../README.md).

## The dispatch pipeline

Every input source — the email listener, the Telegram listener, and the
reminder scheduler — funnels through the same two-step call:

```python
prompt = build_prompt(IncomingMessage(...))
result = pipe_to_provider(prompt, session_id=..., extra_env=...)
```

`build_prompt()` (`core/pipe.py`) wraps the raw message in a standardized YAML
metadata block (`Type`, `Sender`, `Context`, `Current Time`) and runs a
pre-flight `git pull --rebase` against the vault. `pipe_to_provider()` resolves
the configured provider via `providers.get_provider()`, invokes its
`generate_response()`, and normalizes the result.

**This envelope is not optional.** A prompt built as a bare string instead of
`build_prompt(IncomingMessage(...))` arrives at the AI provider looking like an
unattributed instruction injected mid-conversation — and gets treated as a
prompt-injection attempt, including by the provider's own safety behavior.
This has happened in production (an early version of the retry-after-auth
feature built its retry prompt as a raw string). Any code that needs to feed
text back into the pipeline — retries, nudges, internally-generated
follow-ups — must go through `build_prompt`, never around it.

### Providers

`providers/` implements a small `AIProvider` ABC (`base.py`) with one concrete
class per backend: `ClaudeProvider`, `GeminiProvider` (deprecated), `AgyProvider`,
and `EchoProvider` (a no-op stub for tests/dev). `providers/__init__.py`'s
`get_provider()` is a factory that constructs a fresh provider instance per
call, reading the active provider name from `config.get_ai_provider()`.

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

**Any code running as a thread inside the ingestion service that touches
`git` against `VAULT_PATH` or `REMINDERS_JSON_PATH` must hold this lock
first.** Skipping it has caused real corruption before (concurrent `git
pull`s racing and corrupting `FETCH_HEAD`). There is no single chokepoint
enforcing this today — each in-process call site (`pipe.py`, `scheduler.py`)
is individually responsible for remembering.

`tools/reminder_cli.py`'s own git sync doesn't acquire the lock itself — but
that's fine for its normal usage. It's invoked by the AI's shell tool from
*within* an active provider session, and `with GLOBAL_PROVIDER_LOCK:` wraps
the *entire* `subprocess.run()` call for that session in all three providers
(`claude.py`/`gemini.py`/`agy.py`) — which blocks until the CLI process
exits, including everything it shells out to internally. So the
ingestion-service thread that spawned that session already holds the lock
for reminder_cli.py's whole invocation; nothing else in the service can race
it during that window. The actual gap is narrower: `reminder_cli.py` run
genuinely standalone — a human at a terminal, no provider session in
flight — has no ingestion-service thread involved at all, so nothing
prevents that invocation from racing the service's own git operations.

## State and lifetime

Three long-running components share one process (wired up in `main.py`):
`EmailListener`, `TelegramListener`, and `ReminderScheduler`, each running as
a daemon thread. `main.py` constructs exactly one `RateLimiter` and passes it
into both listeners for this reason — the class's own docstring says it's
"shared across all ingestion channels," and a single shared limiter is what
makes that true.

**`SessionManager` does not get the same treatment.** Each of the three
components independently constructs its own `SessionManager()`. Session-ID
data is fine across instances (it's read from and written to one shared JSON
file), but `SessionManager` also holds a second, purely **in-memory** field —
`_stats_prefs`, the per-user `/stats on|off` preference set via
`set_stats_enabled()`. That preference is invisible to any instance other
than the one it was set on. Toggling `/stats off` in Telegram has no effect
on a reminder later delivered by `ReminderScheduler`, because
`ReminderScheduler` is checking its own, separate `SessionManager` instance.

This exact bug shape — a fresh `SessionManager()` silently losing a caller's
live preferences — was found and fixed once already, one call site at a time
(`tools/stocks/etrade_pin_auth.py`'s `complete_and_maybe_retry`, which now
takes the caller's live instance as a required parameter instead of
constructing its own). The same fix has not yet been applied at the
composition root: **`SessionManager` should be constructed once in `main.py`,
exactly like `RateLimiter`, and passed into all three components** rather than
left to each to construct on its own.

If you're writing new code that needs `SessionManager`: accept it as a
parameter from whatever constructed you. Never call `SessionManager()`
yourself outside `main.py`.

## Delivery: getting a reply back out

Each channel has one function meant to be the single place outbound
formatting happens:

- **Email** — `channels/email/reply.py`'s `send_reply(..., stats=None)`.
  Formats the stats footer (`format_stats_email`), form tables, and
  task-checkbox `mailto:` links internally. Callers pass a raw `stats` dict
  (or `None`) straight through — they should never pre-format or concatenate
  it into the body themselves.
- **Telegram** — `channels/telegram/reply_dispatch.py`'s `safe_reply_text()`
  (parse-mode-fallback retry when Telegram rejects malformed HTML/Markdown)
  and `build_reply_keyboard()` (truncation + Actionable-Form/task-checklist
  detection + keyboard construction). New Telegram-outbound code should call
  these rather than `message.reply_text()`/`send_telegram_message()` directly.

**Telegram's stats delivery is currently asymmetric with email's.**
`channels/telegram/sender.py`'s `send_telegram_message()` has no `stats`
parameter — unlike `send_reply()`, it just sends plain text. Every Telegram
call site that needs to show stats (the listener, the scheduler, the E*TRADE
retry path) currently does the gating and formatting manually:

```python
if session_manager.get_stats_enabled(key):
    text += format_stats_telegram(stats)
```

...repeated at each call site rather than centralized. Until
`send_telegram_message` grows an equivalent `stats` kwarg, new code should
follow this same manual pattern rather than inventing a fourth variant.

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
