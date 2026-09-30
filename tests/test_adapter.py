"""Tests for the Hermes-facing adapter path.

These need Hermes Agent on the path (they import ``gateway.*``), so they SKIP on machines
without it and run in an environment that has it — e.g. inside the venv Hermes manages, or
with ``HERMES_REPO`` pointed at a Hermes checkout:

    <hermes venv>/python -m unittest tests.test_adapter -v

What is covered here is exactly what `hermes plugins doctor` defers: the adapter factory and
everything downstream of it (construction, ``send()``, the echo gate, webhook event routing).
"""

import asyncio
import os
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

HERMES_REPO = os.environ.get("HERMES_REPO", "")
for candidate in [HERMES_REPO, str(Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent")]:
    if candidate and Path(candidate, "gateway").exists():
        sys.path.insert(0, candidate)
        break

try:
    from gateway.config import Platform, PlatformConfig  # noqa: F401
    from gateway.platform_registry import PlatformEntry, platform_registry

    HAS_HERMES = True
except ImportError:
    HAS_HERMES = False

from openwa import EchoGuard, OpenWaError  # noqa: E402


@unittest.skipUnless(HAS_HERMES, "Hermes Agent source (gateway.*) not importable")
class AdapterTests(unittest.IsolatedAsyncioTestCase):
    """Drive OpenWaAdapter the way the gateway would, against a fake transport."""

    PHONE = "917972833243@c.us"
    LID = "157076097654949@lid"

    def setUp(self):
        import adapter as adapter_module

        self.module = adapter_module
        # Mirror the real order: register(ctx) runs before the factory constructs the adapter,
        # and Platform("openwa") only resolves for an already-registered platform.
        self.module.register(_StubContext())
        self.adapter = adapter_module.OpenWaAdapter(_config(self.PHONE, self.LID))

    def tearDown(self):
        self.adapter._sessions.clear()
        self.adapter._echo = EchoGuard()

    def test_construction_reads_every_setting(self):
        self.assertEqual(self.adapter.platform.value, "openwa")
        self.assertEqual(self.adapter.base_url, "http://127.0.0.1:2785")
        self.assertEqual(self.adapter.webhook_port, 8790)
        self.assertEqual(self.adapter.self_ids, (self.PHONE, self.LID))
        self.assertTrue(self.adapter.allow_self_chat)
        self.assertTrue(self.adapter.supports_code_blocks)

    def test_send_returns_a_real_SendResult_and_remembers_the_id(self):
        class Fake:
            async def send_text(self, session_id, chat_id, text, mentions=None):
                return {"messageId": "sent-1"}

        self.adapter._client = Fake()
        result = asyncio.run(self.adapter.send(self.LID, "hello"))
        self.assertTrue(result.success)
        self.assertEqual(result.message_id, "sent-1")
        self.assertIsNone(result.error)
        self.assertTrue(self.adapter._echo.is_echo("sent-1"), "the sent id must be an echo")

    def test_send_reports_a_transport_failure_without_raising(self):
        class Broken:
            async def send_text(self, *a, **k):
                raise OpenWaError("OpenWA -> HTTP 500")

        self.adapter._client = Broken()
        result = asyncio.run(self.adapter.send(self.LID, "hello"))
        self.assertFalse(result.success)
        self.assertIn("HTTP 500", result.error)

    def test_send_without_a_session_fails_cleanly(self):
        import adapter as adapter_module

        bare = adapter_module.OpenWaAdapter(_config(self.PHONE, self.LID, session_id=""))
        result = asyncio.run(bare.send(self.LID, "hello"))
        self.assertFalse(result.success)
        self.assertIn("no session id", result.error)

    def test_a_long_reply_is_chunked(self):
        class Fake:
            def __init__(self):
                self.sizes = []

            async def send_text(self, session_id, chat_id, text, mentions=None):
                assert text.startswith("👾 *Agent · Hermes*\n\n")
                self.sizes.append(len(text))
                return {"messageId": f"m{len(self.sizes)}"}

        fake = Fake()
        self.adapter._client = fake
        asyncio.run(self.adapter.send(self.LID, "x" * 9000))
        self.assertGreater(len(fake.sizes), 1, "a 9000-char reply must be split")
        for size in fake.sizes:
            self.assertLessEqual(size, 4096)

    def test_the_agents_own_sends_never_dispatch_back_to_the_agent(self):
        """An agent reply arrives again as message.sent — the echo gate must absorb it."""
        class Fake:
            async def send_text(self, *a, **k):
                return {"messageId": "true_157076097654949@lid_ABC_out"}

            async def send_chat_state(self, *a, **k):
                return {"success": True}

        self.adapter._client = Fake()
        asyncio.run(self.adapter.send(self.LID, "my own reply"))

        dispatched = []
        async def _capture(event):
            dispatched.append(event)
        self.adapter.handle_message = _capture  # shadow the base-class dispatcher

        asyncio.run(
            self.adapter._handle_envelope(
                {"event": "message.sent", "sessionId": "s", "data": {"id": "true_157076097654949@lid_ABC_out",
                 "from": self.LID, "to": self.LID, "chatId": self.LID, "body": "my own reply",
                 "fromMe": True, "isGroup": False}}
            )
        )
        self.assertEqual(dispatched, [], "an echo must not start a turn")

    def test_an_authorized_trigger_dispatches_to_the_agent(self):
        dispatched = []
        async def _capture(event):
            dispatched.append(event)
        self.adapter.handle_message = _capture

        asyncio.run(
            self.adapter._handle_envelope(
                {"event": "message.sent", "sessionId": "s",
                 "idempotencyKey": "idem-1",
                 "data": {"id": "phone-msg-1", "from": self.PHONE, "to": self.LID,
                          "chatId": self.LID, "body": "@me what is the time",
                          "author": "157076097654949:24@lid",
                          "fromMe": True, "isGroup": False, "kind": "individual",
                          "mentionedIds": [self.PHONE]}}
            )
        )
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0].text, "what is the time")
        self.assertEqual(dispatched[0].source.user_id, self.PHONE)

    def test_an_unrelated_dm_from_this_account_is_dropped(self):
        """A message this account sent to a FRIEND must not reach the agent."""
        dispatched = []
        async def _capture(event):
            dispatched.append(event)
        self.adapter.handle_message = _capture

        asyncio.run(
            self.adapter._handle_envelope(
                {"event": "message.sent", "sessionId": "s",
                 "data": {"id": "dm-1", "from": self.PHONE, "to": "29274698436841@lid",
                          "chatId": "29274698436841@lid", "body": "hey mate",
                          "fromMe": True, "isGroup": False, "kind": "individual"}}
            )
        )
        self.assertEqual(dispatched, [])

    def test_a_strangers_dm_is_dropped_before_hermes_by_default(self):
        """STRICT posture: nobody but the operator ever reaches the agent from this number."""
        adapter = self.module.OpenWaAdapter(_config(self.PHONE, self.LID))
        dispatched = []
        async def _capture(event):
            dispatched.append(event)
        adapter.handle_message = _capture

        asyncio.run(
            adapter._handle_envelope(
                {"event": "message.received", "sessionId": "s",
                 "data": {"id": "stranger-1", "from": "29274698436841@lid",
                          "to": self.PHONE, "chatId": "29274698436841@lid",
                          "body": "hey, can you help me?", "fromMe": False,
                          "isGroup": False, "kind": "individual"}}
            )
        )
        self.assertEqual(dispatched, [])

    def test_openwa_allow_others_hands_the_gate_to_hermes(self):
        adapter = self.module.OpenWaAdapter(_config(self.PHONE, self.LID, allow_others="true"))
        self.assertTrue(adapter.allow_others)
        dispatched = []
        async def _capture(event):
            dispatched.append(event)
        adapter.handle_message = _capture

        asyncio.run(
            adapter._handle_envelope(
                {"event": "message.received", "sessionId": "s",
                 "data": {"id": "stranger-2", "from": "29274698436841@lid",
                          "to": self.PHONE, "chatId": "29274698436841@lid",
                          "body": "hey, can you help me?", "fromMe": False,
                          "isGroup": False, "kind": "individual"}}
            )
        )
        self.assertEqual(len(dispatched), 1, "forwarded so HERMES' authorization governs")

    def test_unauthorized_dms_are_silenced_by_default(self):
        """The gateway's default for an unknown DM is to DM BACK a pairing code. Seeded to
        'ignore' so a stranger never gets a bot reply from this adapter."""
        adapter = self.module.OpenWaAdapter(_config(self.PHONE, self.LID))
        self.assertEqual(adapter.unauthorized_dm_behavior, "ignore")
        self.assertEqual(adapter.config.extra.get("unauthorized_dm_behavior"), "ignore")

    def test_send_typing_is_best_effort(self):
        class Broken:
            async def send_chat_state(self, *a, **k):
                raise RuntimeError("boom")

        self.adapter._client = Broken()
        asyncio.run(self.adapter.send_typing(self.LID, metadata={}))  # gateway supplies metadata
        asyncio.run(self.adapter.stop_typing(self.LID))

    async def test_typing_matches_gateway_call_and_clears_presence(self):
        calls = []
        class Fake:
            async def send_chat_state(self, session_id, chat_id, state):
                calls.append(state)
        self.adapter._client = Fake()
        await self.adapter.send_typing(self.LID, metadata={})
        await self.adapter.stop_typing(self.LID)
        self.assertEqual(calls, ["typing", "paused"])

    def test_get_chat_info_distinguishes_groups(self):
        async def check():
            group = await self.adapter.get_chat_info("120363420673987246@g.us")
            dm = await self.adapter.get_chat_info(self.PHONE)
            self.assertEqual(group["type"], "group")
            self.assertEqual(dm["type"], "dm")
        asyncio.run(check())


class _StubContext:
    """The slice of PluginContext that register() uses."""

    def register_platform(self, **kwargs):
        entry = PlatformEntry(**{k: v for k, v in kwargs.items()})
        platform_registry.register(entry)

    def register_tool(self, *a, **k):
        pass

    def register_hook(self, *a, **k):
        pass


def _config(phone, lid, session_id="sess-1", allow_others=None):
    cfg = PlatformConfig()
    cfg.extra = {
        "base_url": "http://127.0.0.1:2785",
        "api_key": "test-key",
        "webhook_secret": "test-secret",
        "session_id": session_id,
        "webhook_port": "8790",
        "allow_self_chat": "true",
        "self_jid": f"{phone},{lid}",
    }
    if allow_others is not None:
        cfg.extra["allow_others"] = allow_others
    return cfg


if __name__ == "__main__":
    unittest.main()
