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
from typing import Any, Awaitable, Callable, Mapping
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


def is_self_chat(message: Mapping[str, Any]) -> bool:
    """A "message yourself" message: sent by this account, into this account, not a group."""
    return (
        message.get("fromMe") is True
        and not message.get("isGroup")
        and message.get("from") == message.get("to")
    )


def extract_trigger(
    message: Mapping[str, Any],
    *,
    self_jid: str | None = None,
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
        if not allow_self_chat or not is_self_chat(message):
            return None

        jid = self_jid or str(message.get("from") or "")
        digits = _subscriber(jid)
        mentioned = message.get("mentionedIds") or []
        mentioned_by_id = any(same_jid(str(entry), jid) for entry in mentioned)
        mentioned_in_body = bool(digits) and f"@{digits}" in body
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


def _strip_trigger_tokens(body: str, digits: str) -> str:
    out = _AT_ME.sub(" ", body)
    if digits:
        out = re.sub(rf"@{re.escape(digits)}(?!\d)", " ", out)
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
