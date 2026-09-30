"""Hermes Agent platform adapter for OpenWA.

Drops into ``~/.hermes/plugins/openwa/`` (see README.md). Only this module imports Hermes;
``openwa.py`` holds the transport and stays independently testable.

Why this exists: Hermes already ships WhatsApp, but through Baileys. A number already paired
to OpenWA cannot also be paired by Hermes' bridge — two WhatsApp Web sessions cannot share one
number. This adapter fronts the **existing** OpenWA session instead of forcing a re-pair.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import extra_or_secret, get_scoped_secret, seed_extra_from_env
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import MessageDeduplicator
from gateway.status import acquire_scoped_lock, release_scoped_lock

try:  # package import when loaded as a plugin
    from .openwa import (
        WHATSAPP_TEXT_LIMIT,
        EchoGuard,
        OpenWaClient,
        OpenWaError,
        chunk_text,
        extract_trigger,
        is_self_chat,
        verify_signature,
    )
except ImportError:  # loaded as a flat module
    from openwa import (  # type: ignore[no-redef]
        WHATSAPP_TEXT_LIMIT,
        EchoGuard,
        OpenWaClient,
        OpenWaError,
        chunk_text,
        extract_trigger,
        is_self_chat,
        verify_signature,
    )

log = logging.getLogger(__name__)

PLATFORM_NAME = "openwa"
WEBHOOK_PATH = "/webhook"
DEDUP_TTL_SECONDS = 600
DEFAULT_WEBHOOK_PORT = 8790


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class OpenWaAdapter(BasePlatformAdapter):
    """Bridges Hermes to an OpenWA gateway over its webhook + REST API."""

    # WhatsApp renders ``` fences as monospace, so let the gateway send real code blocks.
    # Deliberately NOT supports_status_text: WhatsApp has a native typing indicator, not a
    # textual status line, so there is nothing for set_status_text() to render.
    supports_code_blocks = True

    def __init__(self, config: PlatformConfig):
        # Platform(name) resolves a dynamic member only for an already-registered platform
        # (gateway/config.py `_missing_` checks platform_registry). This works because the
        # adapter_factory runs after register(ctx); constructing the adapter before registration
        # would raise ValueError.
        super().__init__(config, Platform(PLATFORM_NAME))
        self.config = config
        extra = getattr(config, "extra", None) or {}

        self.base_url = extra_or_secret(extra, "base_url", "OPENWA_BASE_URL") or ""
        self.api_key = extra_or_secret(extra, "api_key", "OPENWA_API_KEY") or ""
        self.webhook_secret = extra_or_secret(extra, "webhook_secret", "OPENWA_WEBHOOK_SECRET")
        self.default_session_id = extra_or_secret(extra, "session_id", "OPENWA_SESSION_ID")
        self.allow_self_chat = _truthy(extra_or_secret(extra, "allow_self_chat", "OPENWA_ALLOW_SELF_CHAT"))
        self.webhook_host = extra_or_secret(extra, "webhook_host", "OPENWA_WEBHOOK_HOST") or "127.0.0.1"
        self.webhook_port = int(extra_or_secret(extra, "webhook_port", "OPENWA_WEBHOOK_PORT") or DEFAULT_WEBHOOK_PORT)
        # The account's own identities, comma-separated: WhatsApp addresses the self-chat with
        # BOTH the phone JID and the account LID depending on where the message originated.
        raw_self = extra_or_secret(extra, "self_jid", "OPENWA_SELF_JID") or ""
        self.self_ids = tuple(s.strip() for s in str(raw_self).split(",") if s.strip())
        # STRICT by default: only self-chat is forwarded, so nobody else can ever reach the
        # agent from this number. OPENWA_ALLOW_OTHERS=true hands authorization to the gateway
        # instead (allowlist + its own DM behavior).
        self.allow_others = _truthy(extra_or_secret(extra, "allow_others", "OPENWA_ALLOW_OTHERS"))
        # Belt to that layer: the gateway's default for an unknown DM is to DM BACK a pairing
        # code (gateway/config.py: unauthorized_dm_behavior = "pair"). On WhatsApp that means
        # strangers get bot replies. Seed "ignore" -- silence, not a pairing offer -- unless
        # the operator picks pair/decline explicitly.
        behavior = str(extra_or_secret(extra, "unauthorized_dm_behavior", "OPENWA_UNAUTHORIZED_DM_BEHAVIOR") or "ignore").strip().lower()
        self.unauthorized_dm_behavior = behavior if behavior in {"pair", "ignore", "decline"} else "ignore"
        # The gateway's authz mixin reads platforms[<name>].extra[...] at turn time, so seed it
        # on the config this adapter was constructed with (env_enablement seeds the same dict).
        if isinstance(extra, dict):
            extra["unauthorized_dm_behavior"] = self.unauthorized_dm_behavior

        self._client = OpenWaClient(self.base_url, self.api_key)
        self._dedup = MessageDeduplicator(ttl_seconds=DEDUP_TTL_SECONDS)
        self._echo = EchoGuard()
        # Which OpenWA session to send from, per chat. Inbound fills it; the configured
        # session id is the fallback for chats the adapter has not seen a message from.
        self._sessions: dict[str, str] = {}
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._runner: Any = None
        self._lock_key = self.default_session_id or self.base_url

    # ------------------------------------------------------------------------- lifecycle

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self.base_url or not self.api_key:
            log.error("openwa: OPENWA_BASE_URL and OPENWA_API_KEY are both required")
            return False

        if not self.webhook_secret:
            log.error("openwa: OPENWA_WEBHOOK_SECRET is required; refusing to accept unsigned webhooks")
            return False

        # One linked device cannot serve two adapters: two profiles pointed at the same OpenWA
        # session would fight over the same WhatsApp connection.
        acquired, existing = acquire_scoped_lock(PLATFORM_NAME, self._lock_key)
        if not acquired:
            log.error("openwa: session already in use by another profile (%s)", existing)
            return False

        try:
            from aiohttp import web  # bundled with Hermes Agent
        except ImportError:
            log.error("openwa: aiohttp is required to serve the webhook")
            release_scoped_lock(PLATFORM_NAME, self._lock_key)
            return False

        app = web.Application()
        app.router.add_post(WEBHOOK_PATH, self._on_webhook)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, self.webhook_host, self.webhook_port).start()
        self._runner = runner

        self._worker = asyncio.create_task(self._drain())
        self._mark_connected()
        log.info(
            "openwa: listening on http://%s:%s%s — point the OpenWA webhook here (events: message.received)",
            self.webhook_host,
            self.webhook_port,
            WEBHOOK_PATH,
        )
        return True

    async def disconnect(self) -> None:
        self._running = False
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown must not raise
                pass
            self._worker = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        release_scoped_lock(PLATFORM_NAME, self._lock_key)
        self._mark_disconnected()

    # ---------------------------------------------------------------------------- inbound

    async def _on_webhook(self, request: Any) -> Any:
        from aiohttp import web

        raw = await request.read()
        if not verify_signature(self.webhook_secret, raw, request.headers.get("X-OpenWA-Signature")):
            log.warning("openwa: rejected webhook with a bad or missing signature")
            return web.Response(status=401, text="invalid signature")

        try:
            envelope = json.loads(raw or b"{}")
        except ValueError:
            log.warning("openwa: webhook body was not valid JSON")
            return web.Response(status=400, text="invalid json")

        # Acknowledge first: OpenWA retries a webhook that does not answer promptly, and agent
        # turns take minutes. Delivery happens in the worker, not inside this request.
        await self._queue.put(envelope)
        return web.json_response({"ok": True})

    async def _drain(self) -> None:
        while self._running:
            envelope = await self._queue.get()
            try:
                await self._handle_envelope(envelope)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad event must not stop the queue
                log.exception("openwa: failed to handle a webhook event")

    async def _handle_envelope(self, envelope: dict[str, Any]) -> None:
        # message.received is the normal inbound path; message.sent additionally lets an
        # automation or API-initiated "message yourself" prompt reach the agent (it is then
        # gated by allow_self_chat + the mention/echo checks below).
        if envelope.get("event") not in {"message.received", "message.sent"}:
            return

        data = envelope.get("data") or {}
        key = envelope.get("idempotencyKey") or data.get("id")
        if key and self._dedup.is_duplicate(str(key)):
            return
        # Our own sends come back as events (message.sent) — never let them start a turn.
        if self._echo.is_echo(data.get("id")):
            return

        # STRICT by default: anything not a self-chat (groups, other people's DMs) is dropped
        # HERE, before Hermes, so the gateway can never DM a stranger a pairing code or a
        # decline. OPENWA_ALLOW_OTHERS=true forwards and lets Hermes' authorization govern.
        if not self.allow_others and not is_self_chat(data, self_ids=self.self_ids):
            return

        trigger = extract_trigger(
            data,
            self_ids=self.self_ids,
            allow_self_chat=self.allow_self_chat,
        )
        if trigger is None:
            return

        session_id = self.default_session_id or envelope.get("sessionId")
        if session_id:
            self._sessions[trigger.chat_id] = session_id
            try:
                await self._client.react(session_id, trigger.chat_id, trigger.message_id, "👾")
            except Exception:  # presence and reactions must never prevent a turn
                log.debug("openwa: acknowledgement reaction failed", exc_info=True)

        log.debug("openwa: inbound from %s", trigger.chat_id)
        await self.handle_message(
            MessageEvent(
                text=trigger.prompt,
                message_type=MessageType.TEXT,
                source=self.build_source(
                    chat_id=trigger.chat_id,
                    chat_name=data.get("chatName") or trigger.chat_id,
                    chat_type="group" if data.get("isGroup") else "dm",
                    # A verified self-chat may carry a device-qualified author LID. Hermes'
                    # allowlist needs the stable account identity, not that device suffix.
                    user_id=(self.self_ids[0] if self.self_ids and is_self_chat(data, self_ids=self.self_ids)
                             else data.get("author") or data.get("from") or trigger.chat_id),
                    user_name=data.get("pushName") or data.get("from"),
                ),
                message_id=trigger.message_id,
            )
        )

    # --------------------------------------------------------------------------- outbound

    def _session_for(self, chat_id: str) -> str | None:
        return self._sessions.get(chat_id) or self.default_session_id

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        session_id = self._session_for(chat_id)
        if not session_id:
            return SendResult(success=False, error="openwa: no session id for this chat")

        message_id: str | None = None
        try:
            header = "👾 *Agent · Hermes*\n\n"
            for chunk in chunk_text(str(content or ""), WHATSAPP_TEXT_LIMIT - len(header)):
                result = await self._client.send_text(session_id, chat_id, header + chunk)
                message_id = result.get("messageId") or message_id
                self._echo.remember(result.get("messageId"))
        except OpenWaError as exc:
            log.error("openwa: send to %s failed: %s", chat_id, exc)
            return SendResult(success=False, error=str(exc))

        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Best effort — a failed indicator must never fail a turn."""
        session_id = self._session_for(chat_id)
        if not session_id:
            return
        try:
            await self._client.send_chat_state(session_id, chat_id, "typing")
        except Exception:  # noqa: BLE001 - presence is cosmetic
            log.debug("openwa: typing indicator failed for %s", chat_id, exc_info=True)

    async def stop_typing(self, chat_id: str) -> None:
        session_id = self._session_for(chat_id)
        if session_id:
            try:
                await self._client.send_chat_state(session_id, chat_id, "paused")
            except Exception:
                log.debug("openwa: clearing typing failed for %s", chat_id, exc_info=True)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        # Derived locally: OpenWA exposes a chats route, but the kind is already implied by the
        # JID suffix and a lookup would add a network round-trip to every send.
        return {
            "name": chat_id,
            "type": "group" if str(chat_id).endswith("@g.us") else "dm",
        }


# --------------------------------------------------------------------------- plugin wiring


def check_requirements() -> bool:
    """PASSIVE probe — called from status displays, so it must never install anything.

    The webhook secret is part of the requirement, not a nicety: an unauthenticated webhook
    can start an agent turn with terminal access.
    """
    return bool(
        get_scoped_secret("OPENWA_BASE_URL")
        and get_scoped_secret("OPENWA_API_KEY")
        and get_scoped_secret("OPENWA_WEBHOOK_SECRET")
    )


def validate_config(config: Any) -> bool:
    extra = getattr(config, "extra", None) or {}
    return bool(
        extra_or_secret(extra, "base_url", "OPENWA_BASE_URL")
        and extra_or_secret(extra, "api_key", "OPENWA_API_KEY")
        and extra_or_secret(extra, "webhook_secret", "OPENWA_WEBHOOK_SECRET")
    )


def _env_enablement() -> dict[str, Any] | None:
    """Seed PlatformConfig.extra from env, so env-only setups show up in status."""
    base_url = get_scoped_secret("OPENWA_BASE_URL", "").strip()
    api_key = get_scoped_secret("OPENWA_API_KEY", "").strip()
    webhook_secret = get_scoped_secret("OPENWA_WEBHOOK_SECRET", "").strip()
    if not (base_url and api_key and webhook_secret):
        return None

    return {
        "base_url": base_url,
        "api_key": api_key,
        "webhook_secret": webhook_secret,
        **seed_extra_from_env(
            (
                ("OPENWA_SESSION_ID", "session_id", None),
                ("OPENWA_WEBHOOK_SECRET", "webhook_secret", None),
                ("OPENWA_WEBHOOK_HOST", "webhook_host", None),
                ("OPENWA_WEBHOOK_PORT", "webhook_port", int),
                ("OPENWA_SELF_JID", "self_jid", None),
                ("OPENWA_ALLOW_SELF_CHAT", "allow_self_chat", None),
            ),
            home_env="OPENWA_HOME_CHANNEL",
        ),
    }


def register(ctx: Any) -> None:
    """Plugin entry point, called once at startup by the Hermes plugin system."""
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="OpenWA (WhatsApp)",
        adapter_factory=OpenWaAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["OPENWA_BASE_URL", "OPENWA_API_KEY"],
        allowed_users_env="OPENWA_ALLOWED_USERS",
        allow_all_env="OPENWA_ALLOW_ALL_USERS",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="OPENWA_HOME_CHANNEL",
        max_message_length=WHATSAPP_TEXT_LIMIT,
        emoji="💬",
        platform_hint=(
            "You are chatting via WhatsApp through an OpenWA gateway. "
            "WhatsApp has no markdown tables and limited formatting: plain text, *bold*, "
            "_italic_ and ```monospace```. Keep replies short and mobile-friendly."
        ),
    )
