"""The shared mid-turn queue receipt: lifecycle, lock contract, and a ratchet.

Telegram and Discord grew this subsystem independently and kept ~560 duplicated
lines of it. The channel-neutral half now lives in
``messaging/queue_receipt.py``; these tests pin that channel-neutral behaviour
once, and add the
mechanism that stops a third channel from starting a third copy.
"""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from typing import Any

import kiro_crew.messaging.queue_receipt as Q
from kiro_crew.messaging.queue_receipt import (
    ATTACHMENT_PLACEHOLDER,
    RECEIPT_MAX_ITEMS,
    ReceiptQueue,
    receipt_text,
)


class _Surface:
    """Records what a channel would have put on the wire."""

    label = "fake"

    def __init__(
        self,
        *,
        send_id: Any = 7,
        edit_raises: bool = False,
        edit_returns_false: bool = False,
        receipt_key: str = "conv-1",
    ) -> None:
        #: What the channel answers a send with. ``None`` is how every client
        #: reports a send that did not land, and tests flip it mid-run to make the
        #: platform start or stop accepting posts.
        self.send_id = send_id
        self.edit_raises = edit_raises
        #: A real client answers a non-2xx with False rather than raising -- a rate
        #: limit, or Webex's cap of ten edits per message. The registry must treat
        #: that identically to an exception.
        self.edit_returns_false = edit_returns_false
        self.receipt_key = receipt_key
        self.sent: list[str] = []
        self.edits: list[tuple[Any, str]] = []

    async def send_receipt(self, body: str) -> Any | None:
        self.sent.append(body)
        return self.send_id

    async def edit_receipt(self, msg_id: Any, body: str) -> bool:
        self.edits.append((msg_id, body))
        if self.edit_raises:
            raise RuntimeError("edit failed mid-flush")
        return not self.edit_returns_false


class TestReceiptText:
    def test_queued_grows_with_the_count(self) -> None:
        assert receipt_text(["a"]).startswith("⏳ Queued (1):")
        assert receipt_text(["a", "b"]).startswith("⏳ Queued (2):")

    def test_past_the_cap_the_tail_is_summarised_not_dropped(self) -> None:
        texts = [f"m{i}" for i in range(RECEIPT_MAX_ITEMS + 3)]
        out = receipt_text(texts)
        # The count is the TRUE total even though only the cap is listed.
        assert f"({len(texts)})" in out
        assert "…and 3 more" in out

    def test_the_three_states_are_distinguishable(self) -> None:
        assert "Now answering" in receipt_text(["a"], answering=True)
        assert "Cancelled" in receipt_text(["a"], cancelled=True)


class TestLifecycle:
    def test_create_then_grow_edits_one_bubble(self) -> None:
        q, s = ReceiptQueue(), _Surface(send_id=42)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "first")
                await q.create_or_grow_locked("s", s, "second")

        asyncio.run(go())
        assert len(s.sent) == 1, "a second message would orphan the first bubble"
        assert s.edits == [(42, receipt_text(["first", "second"]))]

    def test_flip_drops_the_entry_so_the_next_burst_opens_a_fresh_bubble(self) -> None:
        q, s = ReceiptQueue(), _Surface()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])
                assert not q.has_receipt("s", s.receipt_key)
                await q.create_or_grow_locked("s", s, "b")

        asyncio.run(go())
        assert len(s.sent) == 2, "post-flip burst must start a NEW receipt"

    def test_deferred_remainder_is_stated_not_implied(self) -> None:
        q, s = ReceiptQueue(), _Surface()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"], deferred=4)

        asyncio.run(go())
        assert "+4 deferred" in s.edits[-1][1]

    def test_cancel_finalises_with_the_full_queued_list(self) -> None:
        q, s = ReceiptQueue(), _Surface()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.create_or_grow_locked("s", s, "b")
                await q.finish_cancelled_locked("s", s)

        asyncio.run(go())
        assert "Cancelled (2)" in s.edits[-1][1]
        assert not q.has_receipt("s", s.receipt_key)

    def test_a_failing_edit_never_escapes(self) -> None:
        """Receipt upkeep is cosmetic; it must not fail the turn around it."""
        q, s = ReceiptQueue(), _Surface(edit_raises=True)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.create_or_grow_locked("s", s, "b")  # edit raises
                await q.flip_answering_locked("s", s, ["a", "b"])  # raises too

        asyncio.run(go())  # must not raise

    def test_a_send_that_returns_no_id_records_no_receipt(self) -> None:
        """No id means no bubble to edit later -- storing one would 404 forever."""
        q, s = ReceiptQueue(), _Surface(send_id=None)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")

        asyncio.run(go())
        assert not q.has_receipt("s", s.receipt_key)


class TestConversationIdentity:
    """Two conversations sharing one session key must not share one receipt.

    ``messaging.dm_scope = "unified"`` collapses every allow-listed person's
    direct DM onto a single ``unified:{agent}`` session key on purpose, so the
    session key is NOT a chat. A receipt is a message in one chat, and its id is
    meaningless in any other -- which is what these pin.
    """

    SESSION = "unified:kirocrew"

    def test_a_second_conversation_opens_its_own_receipt(self) -> None:
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "hi")
                await q.create_or_grow_locked(self.SESSION, b, "yo")

        asyncio.run(go())
        assert len(a.sent) == 1, "the first sender's receipt is posted once"
        assert len(b.sent) == 1, "the SECOND sender must get a receipt of their own"
        assert b.edits == [], "a first message has no earlier bubble of its own to edit"

    def test_the_second_sender_is_never_shown_the_first_senders_text(self) -> None:
        """The failure this pins is a disclosure, not just a missing bubble.

        Keyed on the session alone, the second sender's arrival appended to the
        FIRST sender's receipt and pushed the combined body -- both people's
        message text -- at the second sender's chat.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "my salary is")
                await q.create_or_grow_locked(self.SESSION, b, "unrelated question")

        asyncio.run(go())
        addressed_to_b = b.sent + [body for _, body in b.edits]
        assert not any("my salary is" in body for body in addressed_to_b)
        addressed_to_a = a.sent + [body for _, body in a.edits]
        assert not any("unrelated question" in body for body in addressed_to_a)

    def test_the_first_receipt_survives_the_second_senders_arrival(self) -> None:
        """A's bubble must still be live, so A's own next message grows it."""
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "first")
                await q.create_or_grow_locked(self.SESSION, b, "other person")
                await q.create_or_grow_locked(self.SESSION, a, "second")

        asyncio.run(go())
        assert len(a.sent) == 1, "A's bubble was replaced instead of grown"
        assert [body for _, body in a.edits] == [receipt_text(["first", "second"])]

    def test_a_flip_only_resolves_the_conversation_that_owns_the_receipt(self) -> None:
        """B's flip may never take A's receipt as its own record.

        Shown on a conversation-scoped dequeue (Teams, Webex), where A's messages
        are still queued so A's receipt must survive untouched.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "a held")
                # B's turn drains and flips its OWN message. A's is untouched, so
                # the reconciliation must not claim A's text.
                await q.flip_answering_locked(self.SESSION, b, ["b held"], 1)
                # A's receipt must still be live, so A's next message GROWS it.
                await q.create_or_grow_locked(self.SESSION, a, "more")

        asyncio.run(go())
        assert b.edits == [], "B has no receipt to flip; A's is not B's to take"
        assert len(a.sent) == 1 and a.edits, "A's receipt was taken by B's flip"

    def test_a_cancel_finalises_every_conversation_whose_queue_it_cleared(self) -> None:
        """``/stop`` clears the queue for the WHOLE session, so it closes out all.

        ``clear_queue(session_key)`` drops the held messages of every conversation
        sharing the key. Leaving anyone's bubble on "Queued" over messages that no
        longer exist is the lie the receipt exists to prevent, so each one is
        finalized -- in its OWN chat, through its own surface, because the chat
        that typed ``/stop`` cannot address the other.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "a held")
                await q.create_or_grow_locked(self.SESSION, b, "b held")
                # B types /stop. A's messages are cleared too, so A must be told.
                await q.finish_cancelled_locked(self.SESSION, b)

        asyncio.run(go())
        assert "Cancelled" in a.edits[-1][1], "A's messages were dropped with no record"
        assert "Cancelled" in b.edits[-1][1]
        # Each record carries only its own chat's messages.
        assert "b held" not in a.edits[-1][1]
        assert "a held" not in b.edits[-1][1]

    def test_a_cancelled_session_keeps_no_live_receipt_behind(self) -> None:
        """Nothing may survive the clear, or the next burst grows a dead bubble."""
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "a held")
                await q.create_or_grow_locked(self.SESSION, b, "b held")
                await q.finish_cancelled_locked(self.SESSION, b)
                await q.create_or_grow_locked(self.SESSION, a, "after")

        asyncio.run(go())
        assert len(a.sent) == 2, "A's post-cancel message must open a FRESH receipt"

    def test_one_conversation_still_collapses_into_a_single_bubble(self) -> None:
        """The split is by CONVERSATION, not by surface object or by sender.

        Two people talking in one group share a chat, so they share its bubble --
        and each mid-turn message rebuilds the surface. Splitting on anything
        finer than the conversation would post a bubble per message. The edit goes
        through the surface that POSTED the bubble, not the one the later message
        arrived on, because only the poster's id and address belong together.
        """
        first = _Surface(send_id="mid-1", receipt_key="room-7")
        again = _Surface(send_id="mid-2", receipt_key="room-7")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, first, "a")
                await q.create_or_grow_locked(self.SESSION, again, "b")

        asyncio.run(go())
        assert again.sent == [], "a second bubble orphans the first"
        assert again.edits == [], "the later surface never addresses another's bubble"
        assert "Queued (2)" in first.edits[-1][1]

    def test_a_session_wide_dequeue_leaves_no_receipt_live_over_consumed_messages(
        self,
    ) -> None:
        """A drain that dequeues by session key alone consumes everyone's entries.

        Telegram and Discord do exactly that, so B's messages are gone once A's
        turn drains. Leaving B's receipt live would sit it on "Queued" and then
        grow it with text that has already been answered, so B's bubble is flipped
        too -- showing B's OWN messages, in B's OWN chat.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "a held")
                await q.create_or_grow_locked(self.SESSION, b, "b held")
                # The drain dequeued both and collapsed them into ONE turn, so
                # `answered` carries both texts -- which is how the registry knows
                # B's message was consumed too.
                await q.flip_answering_locked(self.SESSION, a, ["a held", "b held"])
                # B's next message must open a FRESH bubble, not grow a dead one.
                await q.create_or_grow_locked(self.SESSION, b, "b again")

        asyncio.run(go())
        assert "Now answering" in b.edits[0][1], "B's consumed receipt was left on Queued"
        assert "b held" in b.edits[0][1] and "a held" not in b.edits[0][1]
        assert len(b.sent) == 2, "B's later message grew a receipt over answered text"
        assert "Queued (1)" in b.sent[1], "the fresh bubble must not carry the old text"

    def test_a_conversation_scoped_dequeue_leaves_the_still_queued_alone(self) -> None:
        """Teams and Webex re-enqueue other conversations, so those stay live.

        Flipping a receipt whose messages are still sitting in the queue would
        claim they are being answered when nothing has read them.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "a held")
                await q.create_or_grow_locked(self.SESSION, b, "b held")
                # deferred=1: B's entry was re-enqueued, so B's receipt stays
                # live -- flipping it would claim a queued message is answered.
                await q.flip_answering_locked(self.SESSION, a, ["a held"], 1)
                # Still live, so B's next message GROWS B's bubble.
                await q.create_or_grow_locked(self.SESSION, b, "b again")

        asyncio.run(go())
        assert not any("Now answering" in body for _, body in b.edits)
        assert len(b.sent) == 1, "B's receipt was replaced while B's message still waited"
        assert "Queued (2)" in b.edits[-1][1]

    def test_a_capped_drain_finalises_the_consumed_and_spares_the_waiting(self) -> None:
        """A drain past a cap consumes some conversations and defers others.

        Telegram and Discord dequeue the whole session and re-enqueue the overflow,
        so one co-tenant's messages can be fully answered while another's are back
        in the queue. The one that was answered must not be left on "Queued" --
        its next message would grow a bubble carrying already-answered text -- and
        the one still waiting must not be finalized.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        c = _Surface(send_id="mid-c", receipt_key="chat-C")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "a held")
                await q.create_or_grow_locked(self.SESSION, b, "b held")
                await q.create_or_grow_locked(self.SESSION, c, "c held")
                # The turn answered A's and B's; C's was re-enqueued past the cap.
                await q.flip_answering_locked(self.SESSION, a, ["a held", "b held"], 1)
                # C's message is still queued, so C's bubble must still grow.
                await q.create_or_grow_locked(self.SESSION, c, "c again")

        asyncio.run(go())
        assert "b held" not in a.edits[-1][1], "B's message was shown in A's chat"
        assert "a held" in a.edits[-1][1]
        assert "Now answering" in b.edits[-1][1], "B's answered messages were left on Queued"
        assert "b held" in b.edits[-1][1] and "a held" not in b.edits[-1][1]
        assert not any(
            "Now answering" in body for _, body in c.edits
        ), "C's message is still queued; claiming it is being answered is a lie"
        assert len(c.sent) == 1 and "Queued (2)" in c.edits[-1][1]

    def test_a_co_tenants_deferred_count_is_not_printed_in_this_persons_chat(self) -> None:
        """The drain's deferred count spans the session, so it is not A's to show.

        Telegram and Discord report how many entries they re-enqueued for the WHOLE
        drain. On a unified DM those can be entirely someone else's, so printing the
        number in the answering person's finalized receipt tells them how many
        messages the OTHER person still has waiting.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "a held")
                await q.create_or_grow_locked(self.SESSION, b, "b one")
                await q.create_or_grow_locked(self.SESSION, b, "b two")
                # A's one message was answered; both of B's were re-enqueued.
                await q.flip_answering_locked(self.SESSION, a, ["a held"], 2)

        asyncio.run(go())
        assert "Now answering" in a.edits[-1][1]
        assert "deferred" not in a.edits[-1][1], "B's queued count was shown in A's chat"

    def test_the_answering_conversations_own_remainder_is_still_stated(self) -> None:
        """Hiding the session-wide count must not hide A's OWN remainder.

        A's bubble is the only record that a message of A's is still held, so a cap
        that left one behind is still called out -- with A's own number, which is
        what the split proved this turn did not answer, never the drain's total.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "a one")
                await q.create_or_grow_locked(self.SESSION, a, "a two")
                await q.create_or_grow_locked(self.SESSION, b, "b one")
                await q.create_or_grow_locked(self.SESSION, b, "b two")
                # The turn answered only A's first message. A's second and both of
                # B's went back, so the drain reports 3 -- of which exactly 1 is A's.
                await q.flip_answering_locked(self.SESSION, a, ["a one"], 3)

        asyncio.run(go())
        assert "+1 deferred" in a.edits[-1][1]
        assert "+3 deferred" not in a.edits[-1][1], "the drain's session-wide total leaked"

    def test_a_missing_keyed_receipt_finalises_nobody_else(self) -> None:
        """A receipt whose send failed was never recorded, so its share is unknown.

        Reconciliation works by taking the answering conversation's own texts out
        of the tally first. With no receipt there is nothing to take out, so that
        conversation's words stay in the tally and are credited to whoever happens
        to show the same words -- closing a bubble over a message still queued.
        """
        a = _Surface(send_id=None, receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                # A's receipt send fails, so A has no registry entry at all.
                await q.create_or_grow_locked(self.SESSION, a, "same words")
                await q.create_or_grow_locked(self.SESSION, b, "same words")
                await q.flip_answering_locked(self.SESSION, a, ["same words"], 1)

        asyncio.run(go())
        assert not any(
            "Now answering" in body for _, body in b.edits
        ), "B was finalized on A's text while B's own message is still queued"

    def test_a_text_two_conversations_share_is_claimed_by_neither(self) -> None:
        """Identical display text cannot say which conversation was consumed.

        Two people each send an attachment, so both bubbles read the same
        placeholder. A capped drain consumes ONE of them. Nothing in the registry
        says whose, so finalizing either is a coin flip that lands on the wrong
        chat half the time. Both stay live instead: a live receipt is resolved by
        that conversation's own next turn, where a wrongly finalized one is a
        permanent lie. Exact attribution would need each dequeued entry's own
        origin, which the drains do not carry.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        c = _Surface(send_id="mid-c", receipt_key="chat-C")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "a asked")
                await q.create_or_grow_locked(self.SESSION, b, "[attachment]")
                await q.create_or_grow_locked(self.SESSION, c, "[attachment]")
                # One of the two attachments got through; the registry cannot say which.
                await q.flip_answering_locked(self.SESSION, a, ["a asked", "[attachment]"], 1)

        asyncio.run(go())
        for who, surf in (("B", b), ("C", c)):
            assert not any(
                "Now answering" in body for _, body in surf.edits
            ), f"{who} was finalized on a text it may not have owned"

    def test_the_flipped_receipt_is_never_shown_the_whole_turns_text(self) -> None:
        """The turn's ``answered`` list is not one conversation's property.

        A session-wide dequeue hands ``flip_answering`` every message the turn
        consumed, co-tenants' included. Editing the keyed bubble to that whole list
        prints another person's queued words in this person's chat -- the exact
        disclosure the per-conversation key exists to stop.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "a secret")
                await q.create_or_grow_locked(self.SESSION, b, "b secret")
                # One drain consumed both; A's conversation finished the turn.
                await q.flip_answering_locked(self.SESSION, a, ["a secret", "b secret"])

        asyncio.run(go())
        assert "b secret" not in a.edits[-1][1], "B's message was shown in A's chat"
        assert "a secret" in a.edits[-1][1]
        assert "a secret" not in b.edits[-1][1], "A's message was shown in B's chat"
        assert "b secret" in b.edits[-1][1]

    def test_a_half_consumed_receipt_drops_the_answered_text_and_keeps_the_waiting(
        self,
    ) -> None:
        """A cap can split ONE conversation: some of its messages answered, some not.

        The receipt must stay live, because a message of its own really is still
        queued -- but it must stop listing the text this turn already answered.
        Leaving the whole list up means the bubble reads "Queued" over something
        that was answered, and its next message grows a receipt carrying that
        answered text forward for good.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        c = _Surface(send_id="mid-c", receipt_key="chat-C")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "a held")
                await q.create_or_grow_locked(self.SESSION, c, "c first")
                await q.create_or_grow_locked(self.SESSION, c, "c second")
                # The cap let A's and C's FIRST through; C's second went back.
                await q.flip_answering_locked(self.SESSION, a, ["a held", "c first"], 1)
                await q.create_or_grow_locked(self.SESSION, c, "c third")

        asyncio.run(go())
        body = c.edits[-1][1]
        assert "c first" not in body, "an answered message was left showing as queued"
        assert "c second" in body and "c third" in body
        assert "Now answering" not in body, "C still has a message queued"
        assert len(c.sent) == 1, "the receipt must be shrunk, not reopened"

    def test_identical_words_from_two_people_are_not_mistaken_for_each_other(self) -> None:
        """Reconciliation counts by multiset, and nets out the flipped receipt first.

        Two people both say "ok". The turn answers one of them. Without taking the
        flipped receipt's own share out of the tally first, the other person's
        identical word looks answered too, and their bubble is closed over a
        message still sitting in the queue.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "ok")
                await q.create_or_grow_locked(self.SESSION, b, "ok")
                # Only A's "ok" was answered; B's was re-enqueued past the cap.
                await q.flip_answering_locked(self.SESSION, a, ["ok"], 1)

        asyncio.run(go())
        assert not any(
            "Now answering" in body for _, body in b.edits
        ), "B's queued message was declared answered because A used the same word"

    def test_a_surplus_copy_is_not_handed_to_a_co_tenant(self) -> None:
        """A copy the keyed bubble cannot account for is not given to someone else.

        ``answered`` is meant to hold one conversation's messages, and normally the
        keyed split consumes every copy, so nothing is left for a co-tenant to take.
        This is the shape where that is not true: the turn reports two copies of a
        word the keyed bubble displays once -- what a channel whose rendering
        differs from its bubble produces. The surplus copy is not demonstrably B's,
        so B shrinks nothing and claims nothing; its own turn resolves it.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "ok")
                await q.create_or_grow_locked(self.SESSION, b, "ok")
                await q.create_or_grow_locked(self.SESSION, b, "ok")
                await q.flip_answering_locked(self.SESSION, a, ["ok", "ok"])

        asyncio.run(go())
        assert q.has_receipt(self.SESSION, "chat-B"), "B's bubble was retired on a surplus copy"
        assert not any(
            "Now answering" in body for _, body in b.edits
        ), "B was told a message was answered that the keyed bubble could not account for"
        # B's bubble still shows BOTH of its queued messages. Shrinking it to one
        # would say the surplus copy was B's, which nothing here establishes. The
        # edits it does have are its own growth from the second message.
        assert "(2)" in b.edits[-1][1], (
            "B's bubble was shrunk over a copy that was not provably its own: " f"{b.edits[-1][1]}"
        )

    def test_a_queued_message_with_no_bubble_keeps_its_copy_from_a_co_tenant(self) -> None:
        """A failed receipt send does not make that message's copy free for others.

        A's "ok" is queued but its receipt send failed, so no bubble displays it.
        A's own bubble shows only "hi". When A's turn answers both messages the
        copy of "ok" is A's, and B -- which displays "ok" and is still waiting --
        must not be told its message was answered.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                a.send_id = None  # the platform refuses the receipt post
                await q.create_or_grow_locked(self.SESSION, a, "ok")
                a.send_id = "mid-a"
                await q.create_or_grow_locked(self.SESSION, a, "hi")
                await q.create_or_grow_locked(self.SESSION, b, "ok")
                await q.flip_answering_locked(self.SESSION, a, ["hi", "ok"])

        asyncio.run(go())
        assert q.has_receipt(self.SESSION, "chat-B"), (
            "B's bubble was retired over a copy belonging to A's message, which has "
            "no bubble of its own to account for it"
        )
        assert not any("Now answering" in body for _, body in b.edits), (
            "B was told its message was answered while it was still queued: " f"{b.edits}"
        )

    def test_identical_words_survive_a_failed_receipt_send(self) -> None:
        """The same words from two people stay apart when one send failed.

        A sends "ok" twice; the first receipt post fails, so A's bubble displays
        one copy while A holds two queued messages. A's turn answers both. The
        second copy is A's, not B's.
        """
        a = _Surface(send_id=None, receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "ok")
                a.send_id = "mid-a"
                await q.create_or_grow_locked(self.SESSION, a, "ok")
                await q.create_or_grow_locked(self.SESSION, b, "ok")
                await q.flip_answering_locked(self.SESSION, a, ["ok", "ok"])

        asyncio.run(go())
        assert q.has_receipt(
            self.SESSION, "chat-B"
        ), "B's queued message was finalized over A's second copy"

    def test_a_bubble_less_claim_is_released_once_its_message_is_answered(self) -> None:
        """The claim goes when the message does, or it over-blocks for ever.

        A's first message never got a bubble and is answered by A's own turn. Held
        past that, its claim reads as demand A does not have, and the next turn
        that would have resolved B exactly declines to -- permanently, since the
        claim has nothing left to release it.
        """
        a = _Surface(send_id=None, receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "ok")  # post refused
                await q.flip_answering_locked(self.SESSION, a, ["ok"])  # answered anyway
                a.send_id = "mid-a"
                await q.create_or_grow_locked(self.SESSION, a, "ok")
                await q.create_or_grow_locked(self.SESSION, b, "ok")
                # Two copies answered, two copies displayed: the accounting is exact
                # and each conversation takes its own.
                await q.flip_answering_locked(self.SESSION, a, ["ok", "ok"])

        asyncio.run(go())
        assert not q.has_receipt(self.SESSION, "chat-B"), (
            "B was left waiting although every displayed copy was answered: the claim "
            "of A's long-answered message is still being counted as demand"
        )
        assert any(
            "Now answering" in body for _, body in b.edits
        ), f"B's bubble never got its record: {b.edits}"

    def test_a_co_tenants_bubble_less_message_protects_another_co_tenant(self) -> None:
        """The tally, not the spending, is what covers a claim that is not the keyed one.

        A's own bubble-less messages are covered because A's turn spends their
        copies. C's are not: C is not draining, so nothing of C's is spent here and
        only the tally can speak for it. Two copies are answered and three
        conversations hold that word, so one of them is still waiting and no text
        says which -- B must not be picked.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        c = _Surface(send_id=None, receipt_key="chat-C")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "ok")
                await q.create_or_grow_locked(self.SESSION, b, "ok")
                await q.create_or_grow_locked(self.SESSION, c, "ok")  # post refused
                # A's channel reports two copies while A's bubble displays one.
                await q.flip_answering_locked(self.SESSION, a, ["ok", "ok"])

        asyncio.run(go())
        assert q.has_receipt(self.SESSION, "chat-B"), (
            "B was finalized on a copy that C's bubble-less queued message may "
            "account for: a claim with no bubble was left out of the tally"
        )
        assert not any(
            "Now answering" in body for _, body in b.edits
        ), f"B was told its message was answered while it was still queued: {b.edits}"

    def test_the_keyed_turn_releases_its_own_bubble_less_claim(self) -> None:
        """A turn that answers a bubble-less message releases its claim as it goes.

        Distinct from the case above, where the conversation had no bubble at all:
        here it has one, so the release happens beside the bubble's own split. Left
        behind, the claim survives the message and the next turn that should have
        resolved B declines to.
        """
        a = _Surface(send_id=None, receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "ok")  # post refused
                a.send_id = "mid-a"
                await q.create_or_grow_locked(self.SESSION, a, "hi")  # bubble shows "hi"
                await q.flip_answering_locked(self.SESSION, a, ["hi", "ok"])
                await q.create_or_grow_locked(self.SESSION, a, "ok")
                await q.create_or_grow_locked(self.SESSION, b, "ok")
                await q.flip_answering_locked(self.SESSION, a, ["ok", "ok"])

        asyncio.run(go())
        assert not q.has_receipt(self.SESSION, "chat-B"), (
            "B was left waiting although every displayed copy was answered: the claim "
            "the keyed turn answered is still being counted as demand"
        )

    def test_a_stop_drops_a_claim_it_discarded_the_message_for(self) -> None:
        """``clear_queue`` took the bubble-less messages too, so their claim goes.

        Kept, it outlives the message it stands for, and the accounting it distorts
        leaves a later co-tenant waiting over copies that were all answered.
        """
        a = _Surface(send_id=None, receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "ok")  # post refused
                await q.finish_cancelled_locked(self.SESSION, a)  # /stop discards it
                a.send_id = "mid-a"
                await q.create_or_grow_locked(self.SESSION, a, "ok")
                await q.create_or_grow_locked(self.SESSION, b, "ok")
                await q.flip_answering_locked(self.SESSION, a, ["ok", "ok"])

        asyncio.run(go())
        assert not q.has_receipt(self.SESSION, "chat-B"), (
            "B was left waiting although every displayed copy was answered: a claim "
            "for a message /stop discarded is still being counted as demand"
        )

    def test_the_answering_conversation_attributes_its_own_duplicate(self) -> None:
        """A shared word does not stop a conversation attributing ITS OWN copy.

        A and B both say "ok"; A also says "a2". The turn answered A's two
        messages -- every drain collapses one sender's entries and defers the rest,
        so a copy in ``answered`` is A's by construction and there is nothing for it
        to be ambiguous with. Filtering A's own split against the session tally
        stranded it instead: A's message was answered and its bubble still read
        "⏳ Queued". B's copy was NOT answered, so B's bubble is still untouched.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "ok")
                await q.create_or_grow_locked(self.SESSION, a, "a2")
                await q.create_or_grow_locked(self.SESSION, b, "ok")
                await q.flip_answering_locked(self.SESSION, a, ["a2", "ok"])

        asyncio.run(go())
        flip = a.edits[-1][1]
        assert "Now answering (2)" in flip, f"A did not attribute its own two messages: {flip}"
        assert "a2" in flip and "ok" in flip
        # Nothing of A's was left waiting, so nothing is owed.
        assert "deferred" not in flip, f"A was told it still has messages waiting: {flip}"
        # B's "ok" was not in this turn, so B keeps its bubble untouched.
        assert q.has_receipt(self.SESSION, "chat-B")
        assert not any(
            "Now answering" in body for _, body in b.edits
        ), "B's queued message was declared answered because A used the same word"

    def test_enough_answered_copies_still_attribute_exactly(self) -> None:
        """Ambiguity is short SUPPLY, not a shared word.

        Two people both say "ok" and the turn answered both copies. There is
        nothing to guess: one copy each. Refusing every duplicate outright would
        strand both bubbles on "Queued" over messages that are demonstrably gone.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "ok")
                await q.create_or_grow_locked(self.SESSION, b, "ok")
                await q.flip_answering_locked(self.SESSION, a, ["ok", "ok"])

        asyncio.run(go())
        assert any(
            "Now answering" in body for _, body in b.edits
        ), "B's answered message was not recorded"
        assert not q.has_receipt(self.SESSION, "chat-B")
        assert not q.has_receipt(self.SESSION, "chat-A")

    def test_a_co_tenant_whose_terminal_edit_failed_stays_resolvable(self) -> None:
        """A receipt is retired only once its record is ON the bubble.

        B's messages were consumed by A's session-wide drain, so B's bubble must
        become a record. The platform refuses that edit. Dropping the entry
        destroyed the bubble's only handle: it read "⏳ Queued" for good, and B's
        next message opened a second bubble beside it. Kept, the entry is a retry
        handle -- and NOT a live receipt, or B's next message grows it and one
        bubble shows an answered message and a queued one as a single queue.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B", edit_raises=True)
        q = ReceiptQueue()
        live_after_failure: list[bool] = []

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "a said")
                await q.create_or_grow_locked(self.SESSION, b, "b said")
                await q.flip_answering_locked(self.SESSION, a, ["a said", "b said"])
                live_after_failure.append(q.has_receipt(self.SESSION, "chat-B"))
                # The platform recovers and B sends again.
                b.edit_raises = False
                await q.create_or_grow_locked(self.SESSION, b, "b again")

        asyncio.run(go())
        assert not live_after_failure[0], "a record still owed is not a live receipt"
        retry = b.edits[-1][1]
        assert "Now answering" in retry, f"the record B was owed was never written: {b.edits}"
        assert "b again" not in retry, "the answered record was grown with a new message"
        assert len(b.sent) == 2, "B's new message did not get its own bubble"
        assert "b said" not in b.sent[1], "the fresh bubble inherited answered text"
        # A's edit landed, so A's entry is gone -- the two are judged separately.
        assert not q.has_receipt(self.SESSION, "chat-A")

    def test_a_terminal_keyed_entry_still_reconciles_the_co_tenants(self) -> None:
        """A record owed on this key does not excuse leaving a co-tenant stale.

        A's own record failed to land, so A's key holds a terminal retry handle.
        B then queues, and the next drain runs under A's envelope and consumes B's
        message. A's entry says nothing is queued for A -- its messages left in the
        earlier transition, and a later one would have replaced it with a live
        bubble -- so there is no unseen share to net out and B is attributable.
        Stopping at the retry leaves B reading "⏳ Queued" over a message that was
        answered, which is the state this whole reconciliation exists to prevent.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A", edit_raises=True)
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()
        owed_after_first: list[bool] = []

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "a said")
                # A's own flip cannot land, so A's key keeps a terminal record.
                await q.flip_answering_locked(self.SESSION, a, ["a said"])
                owed_after_first.append(q.has_receipt(self.SESSION, "chat-A"))
                a.edit_raises = False
                # B queues, then the drain answers B's message under A's envelope.
                await q.create_or_grow_locked(self.SESSION, b, "b said")
                await q.flip_answering_locked(self.SESSION, a, ["b said"])

        asyncio.run(go())
        assert not owed_after_first[0], "a record still owed is not a live receipt"
        assert any(
            "Now answering" in body and "a said" in body for _, body in a.edits
        ), f"the record A was owed was never retried: {a.edits}"
        assert any(
            "Now answering" in body and "b said" in body for _, body in b.edits
        ), f"B was answered but its bubble was left on Queued: {b.edits}"
        assert not q.has_receipt(self.SESSION, "chat-B"), "B's record landed but its entry stayed"
        assert not q.has_receipt(self.SESSION, "chat-A"), "the retried entry kept A's key"

    def test_the_turn_opener_is_not_finalised_over_its_own_deferred_message(self) -> None:
        """Opening the turn does not mean your message was answered.

        A and B share a unified DM key. The drain fills its cap with B's message
        and puts A's back in the queue, then flips through A's surface because A's
        inbound finished the turn. None of A's text is in ``answered``, and the
        fallback would print A's whole bubble as "Now answering" and drop it --
        over a message still sitting in the queue, which is the one record this
        bubble exists to keep honest.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "a held")
                await q.create_or_grow_locked(self.SESSION, b, "b said")
                # The cap took B's and deferred A's: one entry re-enqueued.
                await q.flip_answering_locked(self.SESSION, a, ["b said"], 1)

        asyncio.run(go())
        assert not a.edits, f"A's bubble was rewritten over a queued message: {a.edits}"
        assert q.has_receipt(self.SESSION, "chat-A"), "A's still-queued message lost its receipt"
        assert any("Now answering" in body for _, body in b.edits), "B's record is missing"
        assert not q.has_receipt(self.SESSION, "chat-B")

    def test_two_co_tenants_sending_the_same_text_both_resolve(self) -> None:
        """Identical everyday text must not strand BOTH bubbles for good.

        Two people on one unified key each send the same thing -- an attachment, so
        both bubbles read the same placeholder, or plainly "ok". Each one's own drain
        answers its own message and defers the other's. Filtering the keyed split
        against the session tally made every such turn decline to attribute the copy
        it had just answered, and the next turn read the same two receipts and
        declined again, so neither bubble ever left "⏳ Queued" -- the stale read the
        design treats as self-correcting, made permanent by being circular.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, ATTACHMENT_PLACEHOLDER)
                await q.create_or_grow_locked(self.SESSION, b, ATTACHMENT_PLACEHOLDER)
                # A's turn: A's own copy is answered, B's is deferred back.
                await q.flip_answering_locked(self.SESSION, a, [ATTACHMENT_PLACEHOLDER], 1)
            async with q.lock:
                # B's own turn follows and answers B's copy.
                await q.flip_answering_locked(self.SESSION, b, [ATTACHMENT_PLACEHOLDER])

        asyncio.run(go())
        assert any(
            "Now answering" in body for _, body in a.edits
        ), "A's answered message was left reading Queued by its own turn"
        assert any(
            "Now answering" in body for _, body in b.edits
        ), "B's answered message was left reading Queued by its own turn"
        assert not q.has_receipt(self.SESSION, "chat-A"), "A's bubble was never retired"
        assert not q.has_receipt(self.SESSION, "chat-B"), "B's bubble was never retired"
        # A was told about its own remainder only, and it had none: the deferred
        # entry was B's, and naming it would promise A a message it never sent.
        assert "deferred" not in a.edits[-1][1], "A was promised a co-tenant's deferred message"

    def test_a_reported_failure_keeps_the_record_as_surely_as_a_raised_one(self) -> None:
        """A channel that answers False has not written the record.

        This is the ORDINARY failure, not the exotic one: every channel client
        reports a non-2xx as False rather than raising -- a rate limit, or Webex's
        cap of ten edits per message. Read only the exception and that is
        indistinguishable from success, so the entry is retired over a bubble still
        reading "⏳ Queued", with the retry that would have rescued it never created.
        """
        s = _Surface(edit_returns_false=True)
        q = ReceiptQueue()
        live_after_failure: list[bool] = []

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])
                live_after_failure.append(q.has_receipt("s", s.receipt_key))
                s.edit_returns_false = False
                await q.create_or_grow_locked("s", s, "b")

        asyncio.run(go())
        assert not live_after_failure[0], "a record still owed is not a live receipt"
        records = [body for _, body in s.edits if "Now answering" in body]
        assert len(records) == 2, f"a reported failure was presumed written: {s.edits}"
        assert len(s.sent) == 2, "the new message did not get its own bubble"

    def test_a_surface_that_reports_nothing_is_not_read_as_a_failure(self) -> None:
        """Silence is not a reported failure.

        The contract asks a surface to answer, but a channel that has not adopted
        it yet returns None. Reading that as False would keep every receipt in the
        registry for ever and open a second bubble beside each one.
        """

        class _Quiet(_Surface):
            async def edit_receipt(self, msg_id: Any, body: str) -> None:  # type: ignore[override]
                self.edits.append((msg_id, body))
                return None

        s = _Quiet()
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])
                # A retained entry would be RETRIED here, adding a second edit.
                await q.create_or_grow_locked("s", s, "b")

        asyncio.run(go())
        assert len(s.edits) == 1, f"silence was read as a failure and retried: {s.edits}"
        assert len(s.sent) == 2, "the new message did not get its own bubble"

    def test_a_stop_does_not_relabel_a_record_another_transition_owes(self) -> None:
        """``/stop`` speaks for the messages IT discarded, not for answered ones.

        A's flip failed, so its bubble owes "Now answering" over messages that were
        answered. B then types ``/stop``, which is session-wide. Recomputing every
        entry's body would write "Cancelled" over A's answered messages -- the
        opposite of what happened, and permanent.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A", edit_returns_false=True)
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "a said")
                await q.flip_answering_locked(self.SESSION, a, ["a said"])  # edit fails
                await q.create_or_grow_locked(self.SESSION, b, "b said")
                await q.finish_cancelled_locked(self.SESSION, b)

        asyncio.run(go())
        assert not any(
            "Cancelled" in body for _, body in a.edits
        ), f"an answered message was relabelled cancelled: {a.edits}"
        assert any("Cancelled" in body for _, body in b.edits), "B's own record is missing"

    def test_the_turn_openers_failed_record_stays_resolvable(self) -> None:
        """The opener's entry is its bubble's only handle too, and is not growable.

        Its messages really were answered, so a fresh bubble for its next message
        would be truthful -- but only once the record is ON this one. Dropped on a
        transient failure, the bubble reads "⏳ Queued" for good with nothing left
        able to rewrite it. Kept as a LIVE receipt instead, the next mid-turn
        message grows it, and one bubble presents an answered message and a queued
        one as the same queue. So it is kept, terminal, carrying the record it owes.
        """
        s = _Surface(edit_raises=True)
        q = ReceiptQueue()
        live_after_failure: list[bool] = []

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])
                live_after_failure.append(q.has_receipt("s", s.receipt_key))
                # The platform recovers and the same conversation sends again.
                s.edit_raises = False
                await q.create_or_grow_locked("s", s, "b")

        asyncio.run(go())
        assert not live_after_failure[0], "a record still owed is not a live receipt"
        records = [body for _, body in s.edits if "Now answering" in body]
        assert len(records) == 2, f"the owed record was attempted {len(records)}x, not retried"
        assert "b" not in records[-1], "the answered record was grown with a new message"
        assert len(s.sent) == 2, "the new message did not get its own bubble"
        assert q.has_receipt("s", s.receipt_key), "the fresh bubble is not live"

    def test_a_cancelled_record_that_failed_never_reenters_consumption(self) -> None:
        """A kept cancelled entry is a retry handle, not a queued message.

        ``clear_queue`` already discarded these messages. The entry is held back
        only so the "🛑 Cancelled" edit can be retried; treated as live, a later
        drain would flip that bubble to "Now answering" over messages that no
        longer exist anywhere.
        """
        s = _Surface(edit_raises=True)
        q = ReceiptQueue()
        live: list[bool] = []

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.finish_cancelled_locked("s", s)  # edit fails
                live.append(q.has_receipt("s", s.receipt_key))
                await q.flip_answering_locked("s", s, ["a"])

        asyncio.run(go())
        assert live == [False], "a cancelled entry was reported as a live receipt"
        assert not any(
            "Now answering" in body for _, body in s.edits
        ), "a discarded message was recorded as answered"

    def test_a_cancelled_record_is_retried_once_then_yields_its_key(self) -> None:
        """The next thing the conversation does is the first chance to recover.

        Here the platform has recovered, so the edit lands and the key goes to the
        new bubble. A record still unwritten keeps the key instead -- see the two
        cases below.
        """
        s = _Surface(edit_raises=True)
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.finish_cancelled_locked("s", s)  # edit fails
                s.edit_raises = False
                await q.create_or_grow_locked("s", s, "b")

        asyncio.run(go())
        cancels = [body for _, body in s.edits if "Cancelled" in body]
        assert (
            len(cancels) == 2
        ), f"the record was attempted {len(cancels)}x, not retried: {s.edits}"
        assert len(s.sent) == 2, "the new message did not get its own bubble"
        assert "a" not in s.sent[1], "the fresh bubble inherited the cancelled text"
        assert q.has_receipt("s", s.receipt_key)

    def test_a_record_the_bubble_will_not_take_is_posted_as_a_new_message(self) -> None:
        """A bubble can stop accepting edits for good, so the record goes elsewhere.

        Webex refuses edits on a message past a fixed count, and no retry on that
        id can ever land. Retrying for ever would leave the bubble reading
        "⏳ Queued" over messages that are gone, so the record is posted as its own
        message and the key is released then.
        """
        s = _Surface(edit_raises=True)
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.finish_cancelled_locked("s", s)  # edit fails
                await q.create_or_grow_locked("s", s, "b")  # retry fails, post instead

        asyncio.run(go())
        posted = [body for body in s.sent if "Cancelled" in body]
        assert len(posted) == 1, (
            "the record the bubble refused was never posted anywhere the reader can "
            f"see it: {s.sent}"
        )
        assert "a" in posted[0], f"the posted record lost the text it stands for: {posted[0]}"
        assert q.has_receipt("s", s.receipt_key), "the new message did not get its own bubble"

    def test_a_record_that_cannot_be_written_at_all_keeps_its_handle(self) -> None:
        """Nothing is released while the record is still unwritten.

        Both routes failed, so the only thing that could ever state what happened
        is this entry. Dropping it here is what stranded the bubble on "⏳ Queued"
        with nothing able to rewrite it.
        """
        s = _Surface(edit_raises=True)
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.finish_cancelled_locked("s", s)  # edit fails
                s.send_id = None  # and the platform refuses posts too
                await q.create_or_grow_locked("s", s, "b")
                # The platform recovers. The owed record must still be writable.
                s.edit_raises = False
                s.send_id = "mid-2"
                await q.create_or_grow_locked("s", s, "c")

        asyncio.run(go())
        cancels = [body for _, body in s.edits if "Cancelled" in body]
        assert len(cancels) == 3, (
            "the handle was released while the record was unwritten, so recovery "
            f"could not write it: attempts={len(cancels)} edits={s.edits}"
        )

    def test_a_new_message_does_not_take_a_key_that_still_owes_a_record(self) -> None:
        """A fresh bubble must not overwrite the only handle an owed record has.

        The platform refuses one post and then recovers, which is what separates
        the two things that would be posted through that key: the record it owes,
        and a bubble for the new message. Taking the key for the bubble loses the
        record for good -- nothing else refers to that message. Tracked as
        queued-without-a-bubble instead, the new message keeps its claim and the
        record is still written on the next action.
        """

        class _RefusesOnePost(_Surface):
            """Refuses exactly one post, then accepts. Only landed posts are recorded."""

            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                self.refuse_next = False
                self.refused: list[str] = []

            async def send_receipt(self, body: str) -> Any | None:
                if self.refuse_next:
                    self.refuse_next = False
                    self.refused.append(body)
                    return None
                return await super().send_receipt(body)

        s = _RefusesOnePost(edit_raises=True)
        other = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.finish_cancelled_locked("s", s)  # edit fails, record owed
                s.refuse_next = True
                # The retry's edit fails and its replacement post is refused, so the
                # record is still owed. The platform accepts posts again after this.
                await q.create_or_grow_locked("s", s, "ok")
                await q.create_or_grow_locked("s", other, "ok")
                await q.flip_answering_locked("s", s, ["ok"])

        asyncio.run(go())
        assert any("Cancelled" in body for body in s.sent), (
            "the owed record was never written: a new bubble took the key and "
            f"destroyed its only handle (landed posts: {s.sent}, refused: {s.refused})"
        )
        assert q.has_receipt("s", "chat-B"), (
            "B was finalized over the copy belonging to the message that got no "
            "bubble because the key still owed a record"
        )

    def test_a_discarded_message_cannot_make_a_live_conversations_text_ambiguous(self) -> None:
        """A terminal entry is not a co-tenant holding a queued message.

        A's ``/stop`` discarded its message and the record failed to land, so its
        entry survives only as a retry handle. B then queues the same word and its
        own turn answers it. Counted as live, A's gone message makes B's word look
        like a duplicate in short supply -- so B's bubble is left "⏳ Queued" over a
        message that was demonstrably answered, and the retry handle has started
        deciding other conversations' records.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A", edit_raises=True)
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "ok")
                await q.finish_cancelled_locked(self.SESSION, a)  # A's edit fails
                await q.create_or_grow_locked(self.SESSION, b, "ok")
                await q.flip_answering_locked(self.SESSION, b, ["ok"])

        asyncio.run(go())
        assert any(
            "Now answering" in body for _, body in b.edits
        ), "a discarded message blocked a live conversation's record"
        assert not q.has_receipt(self.SESSION, "chat-B")
        assert not any(
            "Now answering" in body for _, body in a.edits
        ), "a discarded message was recorded as answered"

    def test_two_threads_of_one_room_are_two_conversations(self) -> None:
        """A Webex space's threads do not separate on the session key.

        A space is keyed ``space:{id}``, so two threads share it. Their bubbles
        live in different threads, so a room-only receipt key would append thread
        B's text to thread A's bubble and give B nothing.
        """
        t1 = _Surface(send_id="mid-1", receipt_key="room-7:thread-1")
        t2 = _Surface(send_id="mid-2", receipt_key="room-7:thread-2")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("webex:space:7", t1, "in thread one")
                await q.create_or_grow_locked("webex:space:7", t2, "in thread two")

        asyncio.run(go())
        assert len(t2.sent) == 1, "the second thread got no receipt of its own"
        assert not any("in thread one" in b for b in t2.sent + [e[1] for e in t2.edits])

    def test_a_transition_that_resolves_nothing_while_another_holds_a_receipt_warns(
        self, caplog
    ) -> None:
        """A receipt left sitting on "Queued" is a real loss, so it is reported.

        Nothing resolves, which is safe -- no edit reaches a chat it does not
        belong to -- but silence would make the stranded bubble invisible.
        """
        a = _Surface(send_id="mid-a", receipt_key="chat-A")
        b = _Surface(send_id="mid-b", receipt_key="chat-B")
        q = ReceiptQueue()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked(self.SESSION, a, "held")
                await q.flip_answering_locked(self.SESSION, b, ["held"])

        with caplog.at_level(logging.WARNING, logger=Q.__name__):
            asyncio.run(go())
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "a stranded receipt must not be silent"
        # Naming the tracked cause, so an operator reading this does not have to
        # go digging for why a drain reached a conversation that owns no receipt.
        assert "#12574" in warnings[-1].getMessage()

    def test_an_ordinary_transition_with_nothing_queued_stays_quiet(self, caplog) -> None:
        """Most turns queue nothing at all; warning on those would be noise."""
        q, s = ReceiptQueue(), _Surface(receipt_key="chat-A")

        async def go() -> None:
            async with q.lock:
                await q.flip_answering_locked("s", s, [])
                await q.finish_cancelled_locked("s", s)

        with caplog.at_level(logging.WARNING, logger=Q.__name__):
            asyncio.run(go())
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]


class TestLockIsCallerHeld:
    def test_the_transitions_do_not_take_the_lock_themselves(self) -> None:
        """The atomicity contract, asserted rather than documented.

        Callers hold the lock ACROSS enqueue+receipt (and dequeue+flip), which is
        what makes the subsystem race-free against the drain. If a future change
        moved the acquire inside these methods, this deadlocks -- so the bounded
        wait is the assertion, not a timeout guard.
        """
        q, s = ReceiptQueue(), _Surface()

        async def go() -> None:
            async with q.lock:
                await asyncio.wait_for(q.create_or_grow_locked("s", s, "a"), timeout=2)
                await asyncio.wait_for(q.flip_answering_locked("s", s, ["a"]), timeout=2)
                await asyncio.wait_for(q.finish_cancelled_locked("s", s), timeout=2)

        asyncio.run(go())


def _dispatchers() -> list[Path]:
    pkg = Path(Q.__file__).resolve().parent.parent
    found = sorted(pkg.glob("*/transport_dispatch.py"))
    assert len(found) >= 5, f"expected the dispatcher set, found {found}"
    return found


class TestRatchet:
    def test_no_channel_keeps_its_own_receipt_registry_or_lock(self) -> None:
        """A third copy of this subsystem must fail here, not in production."""
        offenders: dict[str, list[str]] = {}
        for path in _dispatchers():
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
            names = {
                node.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute)
                and node.attr
                in {
                    "_queue_receipts",
                    "_receipt_lock",
                }
            }
            if names:
                offenders[path.parent.name] = sorted(names)
        assert not offenders, (
            "these channels carry a private receipt registry/lock instead of the "
            f"shared ReceiptQueue, so the lock discipline can drift again: {offenders}"
        )

    def test_every_channel_with_a_queue_uses_the_shared_one(self) -> None:
        missing = []
        for path in _dispatchers():
            src = path.read_text(encoding="utf-8")
            if "_enqueue_with_receipt" in src and "ReceiptQueue" not in src:
                missing.append(path.parent.name)
        assert not missing, f"{missing} implement a mid-turn queue without the shared ReceiptQueue"
