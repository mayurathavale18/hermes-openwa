# hermes-openwa

A [Hermes Agent](https://hermes-agent.nousresearch.com/) platform plugin that fronts an
existing **[OpenWA](https://github.com/rmyndharis/OpenWA)** session — so you can chat with
Hermes from WhatsApp.

## Why this exists

Hermes already ships WhatsApp, but through **Baileys**. A WhatsApp number can only be paired to
one WhatsApp Web session at a time, so if the number is already paired to OpenWA, Hermes'
built-in adapter cannot also use it. This plugin talks to the OpenWA gateway you already run
instead of forcing a re-pair — one linked device, whichever product owns it.

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
| `OPENWA_SELF_JID` | no | The account's own JID, when it cannot be derived from the message |
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
tests/          stdlib unittest suite
```

The split is deliberate: `openwa.py` can be read and tested without Hermes, and `adapter.py`
stays thin.

## Tests

```bash
python -m unittest tests.test_openwa -v     # 25 tests, stdlib only
```

The suite covers signature verification, the trigger/loop-prevention gate, chunking, and the
REST client against an injected transport. `adapter.py` is syntax-checked (it cannot be imported
without Hermes on the path).

## Honest status

Written against Hermes' published developer docs, and **not yet run against a live Hermes
install**. The transport is tested; the Hermes-facing seams below are the ones to confirm on
first run:

- `SendResult(...)` field names — this assumes `success`, `message_id`, and `error`.
- `seed_extra_from_env(...)` argument shape and `home_env` behaviour.
- `acquire_scoped_lock(...)` return signature (`(acquired, existing)`).
- `self.build_source(...)` keyword names and `MessageDeduplicator.is_duplicate(...)`.
- Whether the loader needs the `__init__.py` re-export for a `kind: platform` plugin; the
  import falls back to a flat `from openwa import ...` if it loads as a plain module.

`hermes plugins doctor . --ci` is the intended first check (it exercises discovery, the manifest,
`register(ctx)`, and the tool/hook registries without needing a gateway).

## License

MIT.
