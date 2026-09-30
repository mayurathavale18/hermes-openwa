"""Tests for the transport layer.

Stdlib only, so they run anywhere: ``python -m unittest tests.test_openwa -v``

``adapter.py`` is not imported (it needs Hermes on the path), but its syntax is still checked,
which catches the most common plugin-authoring mistake before it reaches a real install.
"""

import py_compile
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openwa import (  # noqa: E402
    EchoGuard,
    OpenWaClient,
    OpenWaError,
    chunk_text,
    compute_signature,
    extract_trigger,
    is_self_chat,
    same_jid,
    verify_signature,
)

SELF = "917972833243@c.us"


def message(**overrides):
    base = {
        "id": "msg-1",
        "from": SELF,
        "to": SELF,
        "chatId": SELF,
        "body": "",
        "type": "chat",
        "timestamp": 1700000000,
        "fromMe": True,
        "isGroup": False,
        "kind": "individual",
    }
    base.update(overrides)
    return base


class RecordingPost:
    """Stands in for the HTTP transport so the client can be tested without aiohttp."""

    def __init__(self, response=None, raises=None):
        self.calls = []
        self.response = response if response is not None else {}
        self.raises = raises

    async def __call__(self, path, payload):
        self.calls.append((path, dict(payload)))
        if self.raises is not None:
            raise self.raises
        return self.response


# ------------------------------------------------------------------------------ signature


class SignatureTests(unittest.TestCase):
    def test_compute_matches_the_documented_shape(self):
        self.assertRegex(compute_signature("s", b"{}"), r"^sha256=[0-9a-f]{64}$")

    def test_verifies_the_raw_body(self):
        body = b'{"event":"message.received"}'
        self.assertTrue(verify_signature("s", body, compute_signature("s", body)))

    def test_rejects_a_tampered_body(self):
        header = compute_signature("s", b'{"amount":1}')
        self.assertFalse(verify_signature("s", b'{"amount":10}', header))

    def test_rejects_a_missing_header_and_the_wrong_secret(self):
        body = b"{}"
        self.assertFalse(verify_signature("s", body, None))
        self.assertFalse(verify_signature("s", body, ""))
        self.assertFalse(verify_signature("other-secret", body, compute_signature("s", body)))

    def test_a_missing_secret_disables_verification(self):
        # The adapter refuses to start without a secret; this is the primitive's own contract.
        self.assertTrue(verify_signature(None, b"{}", None))


# -------------------------------------------------------------------------------- trigger


class TriggerTests(unittest.TestCase):
    def test_self_chat_detection(self):
        self.assertTrue(is_self_chat(message()))
        self.assertFalse(is_self_chat(message(fromMe=False)))
        self.assertFalse(is_self_chat(message(isGroup=True)))
        self.assertFalse(is_self_chat(message(to="friend@c.us")))

    def test_the_agents_own_messages_never_re_enter_by_default(self):
        # This is the reply-loop guard: without it every reply the agent posts starts a turn.
        self.assertIsNone(extract_trigger(message(body="done (exit 0)")))

    def test_an_inbound_message_from_someone_else_passes_through(self):
        # `from` is a Python keyword, so the key is splatted rather than passed by name.
        trigger = extract_trigger(message(**{"from": "friend@c.us"}, fromMe=False, body="hello there"))
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.prompt, "hello there")
        self.assertEqual(trigger.chat_id, SELF)

    def test_self_chat_is_opt_in(self):
        msg = message(body=f"@{SELF.split('@')[0]} summarize the repo", mentionedIds=[SELF])
        self.assertIsNone(extract_trigger(msg))
        trigger = extract_trigger(msg, allow_self_chat=True)
        self.assertEqual(trigger.prompt, "summarize the repo")

    def test_at_me_works_without_a_mention_id(self):
        trigger = extract_trigger(message(body="@me fix the failing test"), allow_self_chat=True)
        self.assertEqual(trigger.prompt, "fix the failing test")

    def test_a_self_chat_message_without_a_mention_is_ignored(self):
        self.assertIsNone(extract_trigger(message(body="remember to buy milk"), allow_self_chat=True))

    def test_a_mention_by_lid_matches_on_subscriber_digits(self):
        trigger = extract_trigger(
            message(body="do it", mentionedIds=["917972833243@lid"]),
            allow_self_chat=True,
        )
        self.assertEqual(trigger.prompt, "do it")

    def test_a_mention_with_no_instruction_is_ignored(self):
        msg = message(body=f"@{SELF.split('@')[0]}", mentionedIds=[SELF])
        self.assertIsNone(extract_trigger(msg, allow_self_chat=True))

    def test_status_broadcasts_are_ignored(self):
        self.assertIsNone(extract_trigger(message(isStatusBroadcast=True, fromMe=False, body="hi")))

    def test_same_jid_compares_digits_across_id_forms(self):
        self.assertTrue(same_jid(SELF, "917972833243@lid"))
        self.assertFalse(same_jid(SELF, "919999999999@c.us"))


class PhoneOriginatedSelfChatTests(unittest.TestCase):
    """WhatsApp uses DIFFERENT JIDs for the two sides of a phone-originated self-chat:

    from = the phone JID (@c.us), to = the account LID (@lid). This is the shape that a
    `from == to` check silently drops — every message typed from the phone.
    """

    PHONE = "917972833243@c.us"
    LID = "157076097654949@lid"
    BOTH = (PHONE, LID)

    def message(self, **overrides):
        base = message(**{"from": self.PHONE, "to": self.LID, "chatId": self.LID})
        base.update(overrides)
        return base

    def test_is_self_chat_accepts_both_identities(self):
        self.assertTrue(is_self_chat(self.message(), self_ids=self.BOTH))

    def test_is_self_chat_rejects_a_real_recipient(self):
        # A message this account sent to a friend is not a self-chat, even though fromMe.
        msg = self.message(**{"to": "29274698436841@lid", "chatId": "29274698436841@lid"}, body="hi")
        self.assertFalse(is_self_chat(msg, self_ids=self.BOTH))

    def test_a_phone_originated_trigger_is_extracted(self):
        trigger = extract_trigger(
            self.message(body="@me fix the failing test", mentionedIds=[self.PHONE]),
            self_ids=self.BOTH,
            allow_self_chat=True,
        )
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.prompt, "fix the failing test")
        self.assertEqual(trigger.chat_id, self.LID)

    def test_a_body_mention_of_the_phone_jid_matches(self):
        trigger = extract_trigger(
            self.message(body=f"@{self.PHONE.split('@')[0]} hello"),
            self_ids=self.BOTH,
            allow_self_chat=True,
        )
        self.assertEqual(trigger.prompt, "hello")

    def test_a_friend_dm_is_not_a_trigger_even_with_a_mention_of_self(self):
        msg = self.message(**{"to": "29274698436841@lid", "chatId": "29274698436841@lid"}, body="@me hi")
        self.assertIsNone(
            extract_trigger(msg, self_ids=self.BOTH, allow_self_chat=True)
        )

    def test_self_ids_also_accept_a_comma_separated_string(self):
        self.assertTrue(is_self_chat(self.message(), self_ids=f"{self.PHONE},{self.LID}"))

    def test_without_ids_the_legacy_from_equals_to_check_applies(self):
        self.assertTrue(is_self_chat(message()))
        self.assertFalse(is_self_chat(self.message()))


# ------------------------------------------------------------------------------- chunking


class ChunkTests(unittest.TestCase):
    def test_a_short_message_is_one_chunk(self):
        self.assertEqual(chunk_text("hello", 100), ["hello"])

    def test_splits_on_boundaries_within_the_limit(self):
        text = "\n".join(f"line {i} of some text" for i in range(40))
        chunks = chunk_text(text, 100)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 100)
        # Chunking trims the whitespace at each split boundary (no blank lines in a bubble),
        # so compare with all whitespace removed: no word may be lost.
        strip = lambda s: "".join(s.split())  # noqa: E731
        self.assertEqual(strip("".join(chunks)), strip(text))

    def test_falls_back_to_a_hard_cut_when_there_is_no_boundary(self):
        chunks = chunk_text("x" * 250, 100)
        self.assertEqual(len(chunks), 3)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 100)


# --------------------------------------------------------------------------------- client


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_reaction_posts_to_the_message_route(self):
        post = RecordingPost({"success": True})
        client = OpenWaClient("http://x", "key", post=post)
        await client.react("sess-1", SELF, "m-1", "👾")
        self.assertEqual(post.calls, [("/api/sessions/sess-1/messages/react",
                                     {"chatId": SELF, "messageId": "m-1", "emoji": "👾"})])

    async def test_send_text_posts_to_the_send_text_route(self):
        post = RecordingPost({"messageId": "m-1", "timestamp": 1})
        client = OpenWaClient("http://127.0.0.1:2785/", "key", post=post)

        result = await client.send_text("sess-1", SELF, "hello")

        self.assertEqual(result["messageId"], "m-1")
        path, payload = post.calls[0]
        self.assertEqual(path, "/api/sessions/sess-1/messages/send-text")
        self.assertEqual(payload, {"chatId": SELF, "text": "hello"})

    async def test_mentions_are_only_sent_when_present(self):
        post = RecordingPost()
        client = OpenWaClient("http://x", "key", post=post)

        await client.send_text("s", SELF, "hi")
        await client.send_text("s", SELF, "hi", mentions=[SELF])

        self.assertNotIn("mentions", post.calls[0][1])
        self.assertEqual(post.calls[1][1]["mentions"], [SELF])

    async def test_edit_text_posts_to_the_edit_route(self):
        post = RecordingPost({"messageId": "m-1"})
        client = OpenWaClient("http://x", "key", post=post)

        await client.edit_text("sess-1", SELF, "m-1", "new body")

        path, payload = post.calls[0]
        self.assertEqual(path, "/api/sessions/sess-1/messages/edit")
        self.assertEqual(payload, {"chatId": SELF, "messageId": "m-1", "body": "new body"})

    async def test_chat_state_posts_to_the_typing_route(self):
        post = RecordingPost({"success": True})
        client = OpenWaClient("http://x", "key", post=post)

        await client.send_chat_state("sess-1", SELF, "typing")

        path, payload = post.calls[0]
        self.assertEqual(path, "/api/sessions/sess-1/chats/typing")
        self.assertEqual(payload, {"chatId": SELF, "state": "typing"})

    async def test_chat_state_rejects_an_unknown_state(self):
        client = OpenWaClient("http://x", "key", post=RecordingPost())
        with self.assertRaises(ValueError):
            await client.send_chat_state("sess-1", SELF, "dancing")

    async def test_a_gateway_error_propagates_as_OpenWaError(self):
        client = OpenWaClient("http://x", "key", post=RecordingPost(raises=OpenWaError("HTTP 500")))
        with self.assertRaises(OpenWaError):
            await client.send_text("sess-1", SELF, "hello")


# ------------------------------------------------------------------------------ echo guard


class EchoGuardTests(unittest.TestCase):
    def test_remembers_sent_ids(self):
        guard = EchoGuard()
        guard.remember("m-1")
        self.assertTrue(guard.is_echo("m-1"))
        self.assertFalse(guard.is_echo("m-2"))

    def test_ignores_empty_ids(self):
        guard = EchoGuard()
        guard.remember("")
        self.assertFalse(guard.is_echo(""))

    def test_evicts_oldest_past_the_cap(self):
        guard = EchoGuard(2)
        guard.remember("a")
        guard.remember("b")
        guard.remember("c")
        self.assertFalse(guard.is_echo("a"))
        self.assertTrue(guard.is_echo("c"))


# ---------------------------------------------------------------------- adapter sanity


class AdapterSyntaxTests(unittest.TestCase):
    def test_adapter_module_compiles(self):
        """Catches syntax errors without importing (Hermes is not on the path here)."""
        py_compile.compile(str(ROOT / "adapter.py"), doraise=True)


if __name__ == "__main__":
    unittest.main()
