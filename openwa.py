"""OpenWA transport primitives.

This module is deliberately free of Hermes imports and third-party dependencies, so the
transport can be read, tested and reasoned about on its own. Everything Hermes-specific lives
in ``adapter.py``.

The OpenWA routes used here were read from the gateway's own source:
  * ``POST /api/sessions/{sessionId}/messages/send-text``   body ``{chatId, text, mentions?}``
  * ``POST /api/sessions/{sessionId}/messages/edit``        body ``{chatId, messageId, body}``
  * ``POST /api/sessions/{sessionId}/chats/typing``         body ``{chatId, state}``, state is
                                                            ``typing`` | ``recording`` | ``paused``
Webhook deliveries carry ``X-OpenWA-Signature: sha256=HMAC_SHA256(secret, rawBody)`` plus
``X-OpenWA-Idempotency-Key``; OpenWA retries a webhook that does not answer promptly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.parse import quote

log = logging.getLogger(__name__)

WHATSAPP_TEXT_LIMIT = 4096
REQUEST_TIMEOUT = 15.0

_AT_ME = re.compile(r"@me\b", re.IGNORECASE)


# --------------------------------------------------------------------------- webhook signature


def compute_signature(secret: str, raw_body: bytes) -> str:
    """The ``sha256=<hex>`` value OpenWA puts in ``X-OpenWA-Signature``."""
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str | None, raw_body: bytes, header: str | None) -> bool:
    """Constant-time verification over the RAW body.

    A missing secret disables verification (returns True) — the caller is expected to warn
    loudly at startup rather than silently trusting an unauthenticated webhook.
    """
    if not secret:
        return True
    if not header:
        return False
    return hmac.compare_digest(compute_signature(secret, raw_body), header)


# ------------------------------------------------------------------------ inbound trigger gate


@dataclass(frozen=True)
class Trigger:
    """An inbound message that should become an agent turn."""

    chat_id: str
    prompt: str
    message_id: str


def _subscriber(jid: str) -> str:
    return "".join(ch for ch in jid.split("@")[0] if ch.isdigit())


def same_jid(a: str, b: str) -> bool:
    """Loose JID equality: exact, or the same subscriber digits across id forms (@c.us / @lid)."""
    if a == b:
        return True
    left, right = _subscriber(a), _subscriber(b)
    return bool(left) and left == right


def _self_id_set(self_ids) -> tuple[str, ...]:
    """Normalize the configured self identities (a JID, or a comma-separated list) to a tuple."""
    if not self_ids:
        return ()
    if isinstance(self_ids, str):
        self_ids = self_ids.split(",")
    return tuple(str(s).strip() for s in self_ids if str(s).strip())


def _matches_any(jid: str, ids: Sequence[str]) -> bool:
    return any(same_jid(jid, i) for i in ids)


def is_self_chat(message: Mapping[str, Any], self_ids: Sequence[str] = ()) -> bool:
    """A "message yourself" message: sent by this account, into this account, not a group.

    WhatsApp does not use one stable JID for the account. A phone-originated self-chat arrives
    as ``from`` = the phone JID (``@c.us``) and ``to`` = the account's LID (``@lid``), while an
    API-originated one can arrive with both as the LID — so ``from == to`` silently drops every
    message typed from the phone. The check is therefore "both sides are the account's own
    identities", against every identity configured in ``OPENWA_SELF_JID``.
    """
    if message.get("fromMe") is not True or message.get("isGroup"):
        return False

    sender = str(message.get("from") or "")
    recipient = str(message.get("to") or "")
    ids = _self_id_set(self_ids)
    if not ids:
        return sender == recipient  # no identities configured: exact-match fallback
    return _matches_any(sender, ids) and _matches_any(recipient, ids)


def extract_trigger(
    message: Mapping[str, Any],
    *,
    self_ids: Sequence[str] = (),
    allow_self_chat: bool = False,
    accept_at_me: bool = True,
) -> Trigger | None:
    """Decide whether an inbound message should reach the agent, and what its prompt is.

    Two gates, in order:

    1. **A message this account sent never re-enters.** Without this, every reply the agent
       posts lands back in the webhook as ``fromMe`` and starts another turn — a reply loop.
    2. ``allow_self_chat`` opts into the "message yourself" workflow. It then requires a real
       self-mention (or a literal ``@me``), so ordinary notes to self stay out.
    """
    if message.get("isStatusBroadcast"):
        return None

    body = str(message.get("body") or "")

    if message.get("fromMe") is True:
        if not allow_self_chat or not is_self_chat(message, self_ids):
            return None

        ids = _self_id_set(self_ids) or _self_id_set(message.get("from"))
        digits = {_subscriber(i) for i in ids if _subscriber(i)}
        mentioned = message.get("mentionedIds") or []
        mentioned_by_id = any(_matches_any(str(entry), ids) for entry in mentioned)
        mentioned_in_body = any(f"@{d}" in body for d in digits)
        said_at_me = accept_at_me and _AT_ME.search(body) is not None

        if not (mentioned_by_id or mentioned_in_body or said_at_me):
            return None
        prompt = _strip_trigger_tokens(body, digits)
    else:
        prompt = body.strip()

    if not prompt:
        return None

    return Trigger(
        chat_id=str(message.get("chatId") or message.get("from") or ""),
        prompt=prompt,
        message_id=str(message.get("id") or ""),
    )


def _strip_trigger_tokens(body: str, digits: set[str]) -> str:
    out = _AT_ME.sub(" ", body)
    for d in digits:
        out = re.sub(rf"@{re.escape(d)}(?!\d)", " ", out)
    return re.sub(r"\s+", " ", out).strip()


# ------------------------------------------------------------------------------- text chunking


def chunk_text(text: str, limit: int = WHATSAPP_TEXT_LIMIT) -> list[str]:
    """Split on paragraph/word boundaries, never exceeding ``limit`` where avoidable."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    if remaining:
        chunks.append(remaining)
    return chunks


# ------------------------------------------------------------------------------- REST client

PostFn = Callable[[str, Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


class OpenWaError(RuntimeError):
    """Raised when OpenWA answers with an error status."""


class EchoGuard:
    """Message ids this adapter sent.

    Subscribing the webhook to ``message.sent`` is what makes an API- or automation-initiated
    "message yourself" prompt reach the agent — but it also means every reply the agent posts
    comes back as an event. A sent id must therefore never start a turn, or an agent reply that
    happens to contain a self-mention would loop forever.
    """

    def __init__(self, max_ids: int = 500) -> None:
        self._ids: dict[str, None] = {}
        self._max = max_ids

    def remember(self, message_id: str | None) -> None:
        if not message_id:
            return
        self._ids[message_id] = None
        while len(self._ids) > self._max:
            oldest = next(iter(self._ids), None)
            if oldest is None:
                break
            del self._ids[oldest]

    def is_echo(self, message_id: str | None) -> bool:
        return bool(message_id) and message_id in self._ids


class OpenWaClient:
    """Minimal client for the OpenWA routes the adapter needs.

    The transport is injectable (``post=``) so the client can be exercised without a gateway
    or aiohttp in the room.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = REQUEST_TIMEOUT,
        post: PostFn | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._post = post or self._http_post

    async def _http_post(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            import aiohttp  # bundled with Hermes Agent
        except ImportError as exc:  # pragma: no cover - depends on the host
            raise OpenWaError("aiohttp is required to reach OpenWA") from exc

        url = f"{self.base_url}{path}"
        headers = {"content-type": "application/json", "x-api-key": self.api_key}
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=dict(payload), headers=headers) as response:
                text = await response.text()
                if response.status >= 400:
                    raise OpenWaError(f"OpenWA {path} -> HTTP {response.status}: {text[:300]}")
                return json.loads(text) if text.strip() else {}

    async def _request(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return dict(await self._post(path, payload) or {})

    async def send_text(
        self,
        session_id: str,
        chat_id: str,
        text: str,
        mentions: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chatId": chat_id, "text": text}
        if mentions:
            payload["mentions"] = list(mentions)
        return await self._request(f"/api/sessions/{quote(session_id)}/messages/send-text", payload)

    async def edit_text(
        self,
        session_id: str,
        chat_id: str,
        message_id: str,
        body: str,
    ) -> dict[str, Any]:
        return await self._request(
            f"/api/sessions/{quote(session_id)}/messages/edit",
            {"chatId": chat_id, "messageId": message_id, "body": body},
        )

    async def send_chat_state(
        self,
        session_id: str,
        chat_id: str,
        state: str = "typing",
    ) -> dict[str, Any]:
        """Show or clear the typing/recording indicator. Best effort by contract."""
        if state not in {"typing", "recording", "paused"}:
            raise ValueError(f"unsupported chat state: {state}")
        return await self._request(
            f"/api/sessions/{quote(session_id)}/chats/typing",
            {"chatId": chat_id, "state": state},
        )
