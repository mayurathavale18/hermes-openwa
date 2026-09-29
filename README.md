# hermes-openwa

[![CI](https://github.com/mayurathavale18/hermes-openwa/actions/workflows/ci.yml/badge.svg)](https://github.com/mayurathavale18/hermes-openwa/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-e2b04a.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%2B-5b9dff)
![Hermes](https://img.shields.io/badge/Hermes%20Agent-platform%20plugin-7bd88f)

A [Hermes Agent](https://hermes-agent.nousresearch.com/) platform plugin that fronts an
existing **[OpenWA](https://github.com/rmyndharis/OpenWA)** session — so you can chat with
Hermes from WhatsApp.

## Why this exists

Hermes already ships WhatsApp, but through **Baileys**. A WhatsApp number can only be paired to
one WhatsApp Web session at a time, so if the number is already paired to OpenWA, Hermes'
built-in adapter cannot also use it. This plugin talks to the OpenWA gateway you already run
instead of forcing a re-pair — one linked device, whichever product owns it.

| | Built-in WhatsApp (Baileys) | OpenWA adapter (this plugin) |
| --- | --- | --- |
| Pairing | Its own WhatsApp Web session | Reuses your existing OpenWA session |
| OpenWA already paired? | ✗ — must re-pair the number | ✓ |
| Dependencies | Node.js bridge subprocess | none beyond Hermes (aiohttp) |
| Best for | Fresh Hermes-only setups | Operators already running OpenWA |

## How it works

```
you (phone) ──► WhatsApp self-chat  "@me what's the weather"
                        │
                        ▼
                 OpenWA gateway ──webhook──►  this adapter
                                              · verify X-OpenWA-Signature
                                              · dedupe · echo gate · self-chat gate
                        ┌─────────────────────┘
                        ▼
                 Hermes gateway ──► AIAgent (tools, skills, memory)
                        │
                        ▼
        reply via OpenWA send-text ──► WhatsApp (progress edited in place)
```

The transport (`openwa.py`) knows OpenWA; the adapter (`adapter.py`) knows Hermes. Neither
knows the other's internals, and only `adapter.py` imports `gateway.*`.

## Install

```bash
git clone <this repo> ~/.hermes/plugins/openwa
hermes plugins enable openwa-platform
```

Then configure it — either through `hermes config` / the Desktop **Capabilities → Plugins** tab,
or with env vars in `~/.hermes/.env`:

```ini
OPENWA_BASE_URL=http://127.0.0.1:2785
OPENWA_API_KEY=owa_k1_...          # OpenWA data/.api-key, or its dashboard
OPENWA_WEBHOOK_SECRET=...          # the secret you set on the OpenWA webhook
```

Finally point an OpenWA webhook at the adapter and subscribe it to `message.received`:

```
http://127.0.0.1:8790/webhook
```

```bash
curl -X POST http://127.0.0.1:2785/api/sessions/$SESSION_ID/webhooks \
  -H "X-API-Key: $OPENWA_API_KEY" -H 'Content-Type: application/json' \
  -d '{"url":"http://127.0.0.1:8790/webhook","events":["message.received"],
       "secret":"'"$OPENWA_WEBHOOK_SECRET"'"}'
```

## Configuration

| Variable | Required | Notes |
| --- | --- | --- |
| `OPENWA_BASE_URL` | yes | e.g. `http://127.0.0.1:2785` |
| `OPENWA_API_KEY` | yes | OpenWA gateway API key |
| `OPENWA_WEBHOOK_SECRET` | **yes** | HMAC secret from the OpenWA webhook. Required on purpose: an unsigned webhook can start an agent turn with terminal access |
| `OPENWA_SESSION_ID` | no | Session to send from; defaults to the session named in each webhook |
| `OPENWA_WEBHOOK_HOST` / `OPENWA_WEBHOOK_PORT` | no | default `127.0.0.1` / `8790` |
| `OPENWA_SELF_JID` | no | The account's own identities, **comma-separated** — WhatsApp uses the phone JID (`@c.us`) for phone-originated messages and the account LID (`@lid`) otherwise. List both, or phone-typed messages are dropped. e.g. `917972833243@c.us,157076097654949@lid` |
| `OPENWA_ALLOW_SELF_CHAT` | no | `true` lets "message yourself" traffic reach the agent (still needs a self-mention) |
| `OPENWA_ALLOWED_USERS` / `OPENWA_ALLOW_ALL_USERS` | no | Hermes' standard authorization gates |
| `OPENWA_HOME_CHANNEL` | no | Default chat for `deliver=openwa` cron jobs |

## Behaviour

- **Acknowledge first, then work.** OpenWA retries a webhook that does not answer promptly, and
  agent turns take minutes — the request returns `200` immediately and delivery happens on a
  worker queue. (The same pattern Hermes' own `wecom/callback_adapter.py` uses.)
- **Signature verified over the raw bytes.** OpenWA signs
  `sha256=HMAC_SHA256(secret, rawBody)`; re-serialising the JSON would change the digest.
- **The agent's own messages never re-enter.** Every reply the bot posts comes back as
  `fromMe`, so without this gate each reply would start another turn. `OPENWA_ALLOW_SELF_CHAT`
  opts into "message yourself" and then requires a self-mention (or a literal `@me`), reusing
  the trigger logic from `agent-bridge`.
- **Duplicate deliveries dropped** on `idempotencyKey` via `MessageDeduplicator`, which the
  gateway carries across adapter reconnects.
- **Long replies are chunked** on paragraph/word boundaries at WhatsApp's 4096-char limit.
- **Typing indicator** is set via `POST /api/sessions/{id}/chats/typing`, best-effort — a failed
  indicator never fails a turn.
- **One profile at a time.** The adapter takes a scoped lock on the OpenWA session, because two
  Hermes profiles driving one linked device would fight over it.

## Layout

```
plugin.yaml     manifest: kind: platform, env declarations
__init__.py     re-exports register() for the loader
adapter.py      OpenWaAdapter(BasePlatformAdapter) + register(ctx) — the only Hermes-aware file
openwa.py       transport: signature, trigger gate, chunking, REST client (no Hermes, no deps)
tests/          test_openwa (transport, stdlib) · test_adapter (adapter path, needs Hermes)
.github/        CI: the suite on Python 3.11–3.13
```

The split is deliberate: `openwa.py` can be read and tested without Hermes, and `adapter.py`
stays thin.

## Tests

Two tiers, both stdlib:

```bash
python -m unittest tests.test_openwa -v     # 28 tests — transport, gates, client; no Hermes needed
python -m unittest tests.test_adapter -v    # 10 tests — the Hermes-facing adapter path
```

`test_openwa` covers signature verification, the trigger/loop-prevention gate (including the
phone-originated self-chat shape), chunking, and the REST client against an injected transport —
it runs anywhere. `test_adapter` imports `gateway.*`, so it needs Hermes on the path
(`HERMES_REPO` env var, or it finds the default Windows install path) and skips automatically
when Hermes isn't there.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Unauthorized user: … on openwa` in the log | The sender isn't in `OPENWA_ALLOWED_USERS` — dropped before any agent turn. Add the JID if you want that chat, or ignore (it's the documented deny-by-default posture). |
| Delivery-failure rows with `fetch failed` and `lastStatusCode: null` | The webhook URL isn't reachable from **inside the OpenWA container** — use `host.docker.internal`, not `127.0.0.1`. |
| `Model '…' requires available credits` | A paid model with no credits: `/model` to a `:free` one or top up at portal.nousresearch.com. |
| `Model '…' not found` | The id doesn't exist on Nous Portal. Note OpenWA's own free variants get retired (`meituan/longcat-2.0:free` now says "no longer free"). |
| Reply is *"Your request was not processed…"* | The model answered with a silence token — ask a real question rather than a terse automation-style ping, or pick a chattier model. |
| No webhook event at all | Check `lastTriggeredAt` on the webhook record, and that the OpenWA webhook subscribes to `message.received` **and** `message.sent` (an API-initiated prompt only emits `message.sent`). |
| `Model '…' not found` on a model you know exists | `Platform(name)` only resolves *after* `register(ctx)` — never construct the adapter before the plugin registers. |

## Verified against a real Hermes install

Checked against a local Hermes (v0.21.5, git install) rather than only the published docs:

- **`hermes plugins doctor . --ci` → OK** — runtime discovery, manifest parsing, import and
  `register(ctx)` all pass.
- **The adapter path was exercised under Hermes' own interpreter** with a fake transport, which
  matters because `plugins doctor` defers the adapter factory:
  `Platform("openwa")` resolves; `send("hello world")` returns
  `SendResult(success=True, message_id=…)`; a 5000-character reply splits into 2 chunks;
  `send_typing()` is a clean no-op; and an unset session id returns `SendResult(success=False,
  error=…)` instead of raising.
- Every `gateway.*` symbol this plugin imports was read from the installed source:
  `SendResult(success, message_id, error)`; `build_source(chat_id, chat_name, chat_type,
  user_id, user_name, ...)`; `MessageEvent(text, message_type, source, message_id)`;
  `MessageType`; `extra_or_secret(extra, key, env)`;
  `get_scoped_secret(name, default)`; `seed_extra_from_env(spec, home_env=...)`;
  `acquire_scoped_lock(scope, identity) -> (acquired, existing)`; `release_scoped_lock(scope,
  identity)`; `MessageDeduplicator(ttl_seconds=...).is_duplicate(...)`.
- Every `ctx.register_platform(...)` keyword exists on `PlatformEntry` — worth checking, because
  an unknown key raises `TypeError` and would fail the load: `validate_config`, `required_env`,
  `allowed_users_env`, `allow_all_env`, `env_enablement_fn`, `cron_deliver_env_var`,
  `max_message_length`, `emoji`, `platform_hint`.

One ordering constraint worth knowing: `Platform(name)` only resolves a dynamic member for an
**already-registered** platform, so the adapter must not be constructed before `register(ctx)`
runs. Hermes guarantees that (the factory is deferred), and there is a comment on it in
`adapter.py`.

**Exercised live** against a real OpenWA session: webhook → adapter → Hermes turn → reply
delivered back into WhatsApp, with the gateway logging
`response ready: platform=openwa … time=11.0s`. Three things the live run caught and this repo
now handles:

- The webhook URL must be reachable from **inside the OpenWA container** — `host.docker.internal`,
  not `127.0.0.1` (which is the container's own loopback; the failure shows up as `fetch failed`
  after OpenWA's three retries, then a delivery-failure row).
- OpenWA emits **`message.sent`** for API- and automation-initiated sends, so a "message yourself"
  prompt never arrives on `message.received` alone. Both are subscribed now.
- The self-chat peer is an **`@lid` JID** (`157076097654949@lid`), not the phone JID — it must be
  in the allowlist (and `OPENWA_SELF_JID`) or Hermes rejects the sender as unauthorized.
- Worse: a **phone-originated** self-chat arrives as `from` = the phone JID and `to` = the LID,
  so a naive `from == to` check silently drops every message typed from the phone (two `@me`
  messages died exactly this way on the first live attempt). `OPENWA_SELF_JID` therefore takes a
  **comma-separated list of both identities**, and `is_self_chat` checks that both sides are the
  account's own.

One caveat that is *not* this plugin: the model. The install wizard defaults to
`z-ai/glm-5.3-flashx`, which is a **paid** Nous Portal model — with no credits the turn fails and
the provider's error text is delivered to WhatsApp instead of an answer. Pick a free one
(`config.yaml` → `model.default`, or `/model` in the chat): the Nous catalog lists nine `$0`
models, e.g. `poolside/laguna-s-2.1:free` (code-focused) — verified working. Note the portal
retires free variants over time (`meituan/longcat-2.0:free` now answers "no longer free"), and
that a terse automation-looking ping can get a silence-token reply — ask a real question.

Also verified: the gateway **denies every WhatsApp user not in `OPENWA_ALLOWED_USERS`** — the
user's other chats and groups appear in the log as `Unauthorized user … on openwa` and are
dropped without an agent turn.

## License

MIT.
